"""发送钮要分「按下去有事发生」与「按下去什么也不会发生」两档。

原来只有一档:框里空着和写满长得一样,深浅两个主题都如此(2026-08-21 用户实见)。
判据不能只看有没有文字——hsSend 收的是 text || atts,只挂了一张图同样发得出去,
所以由 hsSendSync() 一处算,挂 .on 类。

「内容变了」这件事在这份代码里没有单一入口:打字、粘贴、翻历史、取回排队消息、发完清空、
增删附件各走各的路。hsSendSync 因此挂在 hsCmdPaint / hsDraftSave / hsAttPaint 三处开头,
它们合起来盖住全部路径,漏一条就会出现「写了字但钮还是灰的」。

无头浏览器实测两档的取值:
    浅色  空 底 rgb(185,185,185) 箭头 rgb(98,98,98)   可发 底 rgb(87,87,87)  箭头 rgb(249,249,249)
    深色  空 底 rgb(74,74,74)    箭头 rgb(165,165,165) 可发 底 rgb(177,177,177) 箭头 rgb(30,30,30)
"""
import json
import os
import re
import subprocess
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT_PATH = _srcmap.chat_src_file()   # 2026-08-27 拆片:node 侧读拼接整体
CHAT = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
SYNC = re.search(r"function hsSendSync\(v\)[\s\S]+?\n}", CHAT).group(0)

HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.env.CHAT_JS, "utf8");
eval(src.match(/function hsSendSync\(v\)[\s\S]+?\n}/)[0]);
function hsInpText(el) { return el.textContent; }

const cls = new Set();
const btn = { classList: { toggle: (n, on) => (on ? cls.add(n) : cls.delete(n)),
                           contains: (n) => cls.has(n) } };
const mk = (text, atts) => ({ el: { querySelector: () => btn }, inp: { textContent: text }, atts });
const ask = (text, atts) => { hsSendSync(mk(text, atts)); return btn.classList.contains("on"); };

console.log(JSON.stringify({
  空: ask("", []),
  只有空白: ask("   \n  ", []),
  有字: ask("写点什么", []),
  只有附件: ask("", [{ name: "a.png" }]),
  字和附件: ask("写点什么", [{ name: "a.png" }]),
  附件字段缺失: ask("", undefined),
  没有钮时不炸: (() => { try { hsSendSync({ el: { querySelector: () => null }, inp: {} }); return true; }
                        catch { return false; } })(),
}));
"""


def _run():
    env = dict(os.environ, CHAT_JS=CHAT_PATH)
    out = subprocess.run(["node", "-e", HARNESS], env=env,
                         capture_output=True, text=True, timeout=60, encoding='utf-8')
    if out.returncode:
        raise AssertionError(out.stderr[-800:])
    return json.loads(out.stdout.strip().splitlines()[-1])


class 判据(unittest.TestCase):
    def test_空框不可发(self):
        r = _run()
        self.assertFalse(r["空"])
        self.assertFalse(r["只有空白"], "全是空白等同于没写")

    def test_有字可发(self):
        self.assertTrue(_run()["有字"])

    def test_只挂附件也可发(self):
        """hsSend 收的是 text || atts,判据不跟上的话,挂了图的钮还是灰的。"""
        r = _run()
        self.assertTrue(r["只有附件"])
        self.assertTrue(r["字和附件"])

    def test_附件字段还没建也不炸(self):
        r = _run()
        self.assertFalse(r["附件字段缺失"])
        self.assertTrue(r["没有钮时不炸"])


class 两侧接上(unittest.TestCase):
    def test_三个入口都调了(self):
        for fn in ("hsCmdPaint", "hsDraftSave", "hsAttPaint"):
            seg = re.search(r"function %s\(v(?:, now)?\) \{\n(.+)" % fn, CHAT)
            self.assertTrue(seg, "%s 不见了" % fn)
            self.assertIn("hsSendSync(v)", seg.group(1),
                          "%s 开头没有同步发送钮" % fn)

    def test_挂在早退之前(self):
        """hsCmdPaint 头一行就早退(没有 CSS Highlight 的浏览器),放在它之后等于不生效。"""
        seg = re.search(r"function hsCmdPaint\(v\)[\s\S]+?\n}", CHAT).group(0)
        self.assertLess(seg.index("hsSendSync(v)"), seg.index("if (!window.CSS"))

    def test_判据与hsSend的入口一致(self):
        """两处各写一份迟早会岔开:hsSend 认的是 text || atts,这里也必须是。"""
        send = re.search(r"async function hsSend\(v\)[\s\S]+?\n}\n", CHAT).group(0)
        self.assertIn("if (!text && !atts.length) return;", send)
        self.assertIn("(v.atts || []).length", SYNC)
        self.assertIn("hsInpText(v.inp).trim()", SYNC)


class 两档样式(unittest.TestCase):
    def _rule(self, sel):
        m = re.search(r"\n\.hs-page %s \{[^}]*\}" % re.escape(sel), CSS)
        self.assertTrue(m, "找不到 %s" % sel)
        return m.group(0)

    def test_空态与可发态不同色(self):
        off, on = self._rule(".hs-send"), self._rule(".hs-send.on")
        self.assertIn("background: var(--ink-29)", off)
        self.assertIn("background: var(--ink-60)", on)
        self.assertIn("color: var(--ink-55)", off)
        self.assertIn("color: var(--ink-12)", on)

    def test_悬停只在可发时提亮(self):
        """空着的时候变亮等于说「可以按」,而按下去什么也不会发生。"""
        self.assertIn(".hs-page .hs-send.on:hover", CSS)
        self.assertNotIn(".hs-page .hs-send:hover {", CSS)

    def test_busy那条排在后面才压得住(self):
        """两条特指度相同,谁在后谁生效;跑动中要盖过可发态。"""
        self.assertLess(CSS.index(".hs-page .hs-send.on {"),
                        CSS.index(".hs-page.hs-busy .hs-send"))


if __name__ == "__main__":
    unittest.main()
