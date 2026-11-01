#!/usr/bin/env python3
"""右缘系统车道的覆盖卡点(实测半)。

起 headless Chrome 打开运行中的 desk,逐页沿程序坞感应线纵向采样,用
elementsFromPoint 取该点最上层的非系统元素,凡是画了不透明底色的就判定为
"内容铺进了车道",报出页面、元素、颜色与它的右缘位置。

车道宽度读 frontend/style.css 的 --edge-rail,与 desktron/tests/test_frontend_edge_rail.py
同一口径。静态那道只认登记在案的选择器,这道认所有实际画出来的东西。

用法:
    python3 scripts/check_edge_rail.py [--port 7765] [--chrome-port 9224]

前置:desk ui_server 正在跑(默认 7765),本机装了 Google Chrome。
退出码:0 无覆盖;1 有覆盖(stderr 列出);2 环境不满足(Chrome/desk 不可用)。
"""
import argparse
import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STYLE = os.path.join(ROOT, "frontend", "style.css")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# 要巡的页面:(名字, 打开动作, 选中态动作)。加新页面就在这里补一行。
# 第三项在探针算出车道位置后执行(window.__railBand 给出车道的 x/top/h),用来把页面
# 推进"会画底色"的状态——底色多半挂在 :hover / .sel 上,只看默认态等于什么都没测。
# 传空串表示这页没有需要额外触发的选中态。
OPEN_APP = ("(() => { const a = (desktopApps || []).find((x) => (x.uid || x.id) === %r);"
            " if (a) _openDomainApp(a, '_desktop'); })()")
PAGES = [
    ("应用页", 'openStore("app")', ""),
    ("工坊", 'openStore("asset")', ""),
    ("Excalidraw 应用", OPEN_APP % "desktron.excalidraw.be284", ""),
]


def rail_px():
    with open(STYLE, encoding="utf-8") as f:
        m = re.search(r"--edge-rail:\s*(\d+)px", f.read())
    if not m:
        print("✗ style.css 里没有 --edge-rail,车道宽度没有单一口径", file=sys.stderr)
        sys.exit(2)
    return int(m.group(1))


class WS:
    """够用的 CDP over WebSocket:不引第三方依赖,只发 Runtime.evaluate 这类同步请求。"""

    def __init__(self, url):
        _, rest = url.split("://", 1)
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.s = socket.create_connection((host, int(port)), timeout=20)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.n = 0

    def send(self, method, params=None):
        self.n += 1
        payload = json.dumps({"id": self.n, "method": method, "params": params or {}}).encode()
        mask, ln = os.urandom(4), len(payload)
        hdr = b"\x81"
        if ln < 126:
            hdr += bytes([0x80 | ln])
        elif ln < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", ln)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", ln)
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        return self.n

    def _read(self, k):
        while len(self.buf) < k:
            self.buf += self.s.recv(65536)
        out, self.buf = self.buf[:k], self.buf[k:]
        return out

    def recv(self, want):
        while True:
            _, b1 = self._read(2)
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            msg = json.loads(self._read(ln))
            if msg.get("id") == want:
                return msg

    def eval(self, expr, timeout=30):
        r = self.recv(self.send("Runtime.evaluate", {
            "expression": expr, "awaitPromise": True, "returnByValue": True}))
        res = r.get("result", {})
        if "exceptionDetails" in res:
            raise RuntimeError(res["exceptionDetails"].get("text", "页面里报错了"))
        return res.get("result", {}).get("value")


# 第一步:打开页面,算出车道位置,挂到 window.__railBand 供后续的选中态动作/鼠标移动用
OPEN = r"""
(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  await wait(%(settle)d);
  %(open)s;
  await wait(2500);
  const sensor = document.getElementById("dock-sensor");
  if (!sensor) return { skip: "页面上没有 #dock-sensor" };
  const sb = sensor.getBoundingClientRect();
  const cs = getComputedStyle(sensor, "::after");
  const lineW = parseFloat(cs.width), lineR = parseFloat(cs.right);
  const band = { x: sb.right - lineR - lineW / 2,      // 感应线的横向中线
                 top: sb.top + parseFloat(cs.top), h: parseFloat(cs.height) };
  window.__railBand = band;
  return band;
})()
"""

# 第二步:沿车道采样。:hover 只认真实指针,所以由 CDP 的 Input.dispatchMouseEvent 把鼠标
# 移到同一行靠左的位置(不能停在感应线上,那会把程序坞唤出来、线反而隐去),再来这里取样。
SAMPLE = r"""
(() => {
  const rail = %(rail)d, b = window.__railBand;
  const opaque = (c) => c && c !== "transparent" &&
    !(c.startsWith("rgba") && parseFloat(c.split(",").pop()) === 0);
  const sys = (el) => el.closest("#dock-sensor, #app-dock, .sb-thumb, #ctx-menu") ||
                      el === document.documentElement || el === document.body;
  // 判违规不是"车道上有底色"——页面/面板的满铺底色本来就铺到窗口右缘,线画在均匀底上是设计本意。
  // 出问题的是**在车道里收边的块**:右缘落进 0<x<rail,或者带圆角(卡片/行,不是承载面),
  // 这两种情况线会看起来把块压穿。
  // 应用页都在同源 iframe 里,elementsFromPoint 到 <iframe> 就停了,不下钻等于测不到应用。
  // 同源可以直接进它的 document 接着取(坐标换成 iframe 内坐标)。
  // 顺序要紧:框里的东西盖在框自身之上、但在框之上的外层元素之下,所以要**就地展开**在
  // iframe 所在的位置,不能把外层整摞排在前面——否则先撞上外层的满铺承载面就 break 了,框里永远看不到。
  const stack = (doc, x, y, depth) => {
    const out = [];
    for (const el of doc.elementsFromPoint(x, y)) {
      let inner = null;
      if (el.tagName === "IFRAME" && depth < 4) {
        try { inner = el.contentDocument; } catch { inner = null; }       // 跨域就当它是个不透明框
      }
      if (inner) {
        const fb = el.getBoundingClientRect();
        out.push(...stack(inner, x - fb.left, y - fb.top, depth + 1));     // 框本身不算内容
      } else {
        out.push(el);
      }
    }
    return out;
  };
  const hits = new Map();
  for (let i = 0; i <= 24; i++) {
    const y = b.top + (b.h * i) / 24;
    for (const el of stack(document, b.x, y, 0)) {
      if (sys(el)) continue;
      const bg = getComputedStyle(el).backgroundColor;
      if (!opaque(bg)) continue;
      const box = el.getBoundingClientRect();
      const fromRight = +(innerWidth - box.right).toFixed(1);
      const radius = Math.max(...["borderTopRightRadius", "borderBottomRightRadius"]
        .map((k) => parseFloat(getComputedStyle(el)[k]) || 0));
      let why = null;
      if (fromRight > 0.5 && fromRight < rail) why = "右缘收在车道里";
      else if (radius > 0) why = "圆角块压在车道上";
      if (!why) break;                                 // 满铺承载面:线画在它上面是设计本意
      const key = (el.className || el.tagName) + "|" + bg;
      if (!hits.has(key)) hits.set(key, {
        sel: el.tagName.toLowerCase() + (typeof el.className === "string" && el.className
              ? "." + el.className.trim().split(/\s+/).join(".") : ""),
        bg, fromRight, radius, why, y: +y.toFixed(0),
      });
      break;                                           // 只看最上层那个
    }
  }
  return { rail, lineX: +b.x.toFixed(1), hits: [...hits.values()] };
})()
"""


def probe(ws, open_expr, state_expr, rail, settle=4000):
    """打开页面 → 触发选中态 → 真实鼠标扫过 → 采样。返回 {lineX, hits} 或 {skip}。"""
    band = ws.eval(OPEN % {"open": open_expr, "settle": settle})
    if not band or band.get("skip"):
        return band or {"skip": "无返回"}
    if state_expr:
        ws.eval(state_expr)
        time.sleep(0.6)
    hits, line_x = {}, band["x"]
    # 沿车道分几档把真实指针移到同一 y、靠左 200px 的位置,逐档采样,收集各 hover 态
    for i in range(0, 5):
        y = band["top"] + band["h"] * i / 4
        ws.recv(ws.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": max(0, line_x - 200), "y": y}))
        time.sleep(0.25)
        r = ws.eval(SAMPLE % {"rail": rail})
        for h in (r or {}).get("hits", []):
            hits[h["sel"] + "|" + h["bg"]] = h
    return {"lineX": line_x, "hits": list(hits.values())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("DESKTRON_UI_PORT", 7765)))
    ap.add_argument("--chrome-port", type=int, default=9224)
    args = ap.parse_args()

    rail = rail_px()
    base = f"http://127.0.0.1:{args.port}/"
    try:
        urllib.request.urlopen(base, timeout=3)
    except Exception:
        print(f"✗ desk 没在 {base} 跑,先起 ui_server 再跑本检查", file=sys.stderr)
        return 2
    if not (os.path.exists(CHROME) or shutil.which("google-chrome")):
        print("✗ 找不到 Google Chrome,装了再跑(本检查靠 headless Chrome 实测)", file=sys.stderr)
        return 2

    profile = tempfile.mkdtemp(prefix="edge-rail-")
    chrome = subprocess.Popen(
        [CHROME if os.path.exists(CHROME) else shutil.which("google-chrome"),
         "--headless=new", f"--remote-debugging-port={args.chrome_port}",
         f"--user-data-dir={profile}", "--window-size=1400,900",
         "--no-first-run", "--disable-gpu", base],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        dev = f"http://127.0.0.1:{args.chrome_port}"
        for _ in range(60):
            try:
                urllib.request.urlopen(dev + "/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("✗ headless Chrome 起不来", file=sys.stderr)
            return 2

        bad = []
        for name, open_expr, state_expr in PAGES:
            tabs = json.load(urllib.request.urlopen(dev + "/json/list", timeout=3))
            page = next(t for t in tabs if t.get("type") == "page")
            ws = WS(page["webSocketDebuggerUrl"])
            ws.recv(ws.send("Page.enable"))
            ws.recv(ws.send("Page.navigate", {"url": base}))
            for _ in range(40):                       # 等页面 ready,再跑探针
                v = ws.eval("location.href + '|' + document.readyState")
                if v and v.startswith(base) and v.endswith("complete"):
                    break
                time.sleep(0.5)
            try:
                r = probe(ws, open_expr, state_expr, rail)
            except Exception as e:
                print(f"  ! {name} 探测失败:{str(e)[:160]}", file=sys.stderr)
                continue
            if not r or r.get("skip"):
                print(f"  - {name} 跳过:{(r or {}).get('skip', '无返回')}")
                continue
            for h in r["hits"]:
                bad.append((name, h))
            print(f"  {'✗' if r['hits'] else '✓'} {name}"
                  f"(感应线 x={r['lineX']},车道 {rail}px)")

        if bad:
            print(f"\n✗ 有内容铺进了右缘系统车道(应内缩 >= {rail}px):", file=sys.stderr)
            for name, h in bad:
                print(f"  [{name}] {h['sel']} —— {h['why']}\n"
                      f"      底色 {h['bg']},右缘距窗口右 {h['fromRight']}px,"
                      f"圆角 {h['radius']}px(y≈{h['y']})", file=sys.stderr)
            print("  修法:把该容器右侧内缩改成 var(--edge-rail)", file=sys.stderr)
            return 1
        print(f"\nedge-rail 通过:{len(PAGES)} 个页面都没有内容铺进右缘 {rail}px 车道")
        return 0
    finally:
        chrome.terminate()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
