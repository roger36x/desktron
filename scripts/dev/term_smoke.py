#!/usr/bin/env python3
"""终端生命周期冒烟。全引擎转 TUI 后,核心路径是 PTY 终端(xterm ↔ /term WebSocket ↔ 引擎),
不再是结构化 prompt_async(旧 chat_smoke.py 已废弃)。本测对运行中的工作台(127.0.0.1:7765)
断言终端管线不变量:
  1. 列表接口可用(GET /workbench/terminals);
  2. 开终端(POST)返回 id;
  3. 新终端出现在列表且 alive;
  4. WS 附着握手成功(101)且能收到 PTY 输出(嵌入管线活着);
  5. 删除后从列表消失。
用法: term_smoke.py [claude|codex]   # 缺省 claude
退出码 0/1。改终端 / 持久化相关代码后必跑。
"""
import base64
import hashlib
import json
import os
import socket
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:7765"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(path, data=None, method=None, timeout=15):
    r = urllib.request.Request(
        BASE + path,
        data=(json.dumps(data).encode() if data is not None else None),
        method=method or ("POST" if data is not None else "GET"),
        headers={"content-type": "application/json"})
    with opener.open(r, timeout=timeout) as resp:
        body = resp.read()
        return resp.status, (json.loads(body) if body else None)


def ws_probe(sid, timeout=8):
    """裸 WebSocket 握手 + 读一帧,验证 /term 嵌入管线。返回收到的字节数。"""
    s = socket.create_connection(("127.0.0.1", 7765), timeout=timeout)
    s.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((
        f"GET /term?session={sid} HTTP/1.1\r\nHost: 127.0.0.1:7765\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = s.recv(4096)
        if not d:
            break
        buf += d
    head = buf.split(b"\r\n\r\n", 1)[0].decode("latin1")
    statusline = head.split("\r\n", 1)[0]
    ok101 = " 101" in statusline   # HTTP/1.0 或 1.1 都可能
    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    ok_accept = accept in head
    # 读一会儿 PTY 输出(server→client 帧),统计 payload 字节
    got = len(buf.split(b"\r\n\r\n", 1)[1]) if b"\r\n\r\n" in buf else 0
    t0 = time.time()
    while time.time() - t0 < 3:
        try:
            d = s.recv(65536)
        except socket.timeout:
            break
        if not d:
            break
        got += len(d)
        if got > 0:
            break
    try: s.close()
    except Exception: pass
    return ok101 and ok_accept, got


def main():
    engine = (sys.argv[1] if len(sys.argv) > 1 else "claude") or "claude"
    fails = []

    try:
        st, lst0 = req("/workbench/terminals")
    except Exception as e:
        print("FAIL 列表接口不可用:", e); return 1
    if st != 200 or not isinstance(lst0, list):
        fails.append(f"GET /workbench/terminals 状态/类型异常: {st}")

    st, r = req("/workbench/terminals", {"engine": engine, "domain": "_desktop"})
    sid = (r or {}).get("id")
    if st != 200 or not sid:
        print(f"FAIL 开终端失败: status={st} resp={r}"); return 1
    print(f"开了终端 {sid}（{engine}）")

    try:
        time.sleep(1.5)
        st, lst = req("/workbench/terminals")
        live = next((t for t in lst if t["id"] == sid), None)
        if not live:
            fails.append("新终端未出现在列表")
        elif not live.get("alive"):
            fails.append("新终端 alive=False")
        elif live.get("engine") != engine:
            fails.append(f"新终端 engine={live.get('engine')} ≠ {engine}")

        ok_ws, got = ws_probe(sid)
        if not ok_ws:
            fails.append("WS 握手未成功(无 101 / accept 不匹配)")
        elif got <= 0:
            fails.append("WS 附着后未收到任何 PTY 输出")
        else:
            print(f"WS 附着 OK,收到 {got} 字节 PTY 输出")
    finally:
        try:
            req(f"/workbench/terminals/{sid}", method="DELETE")
        except Exception as e:
            fails.append(f"删除请求异常: {e}")

    time.sleep(0.8)
    st, lst2 = req("/workbench/terminals")
    if any(t["id"] == sid for t in (lst2 or [])):
        fails.append("删除后终端仍在列表")

    if fails:
        print("FAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("PASS 终端生命周期冒烟通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
