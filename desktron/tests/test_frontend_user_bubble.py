"""用户气泡按字面显示,只认代码块与行内码。

粘贴一段长提示词时三反引号围起来的整段要能立住;而正文里的 **、裸路径、正则是字面量,
渲染整套 markdown 会把它们重新解释——这类内容在会话里出现得比强调语法频繁得多。
"""
import json
import os
import re
import shutil
import subprocess
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT_JS = os.path.join(ROOT, "frontend", "app-chat.js")
CHAT = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()

# 用最小的 DOM 替身跑真函数,产出一串 [类型, 文本],断言切分本身。
HARNESS = r"""
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this._text = ""; this.className = ""; }
  set textContent(v) { this._text = v; this.children = []; }
  get textContent() { return this._text; }
  appendChild(n) { this.children.push(n); return n; }
}
const document = {
  createElement: (tag) => new Node(tag),
  createTextNode: (t) => { const n = new Node("#text"); n._text = t; return n; },
};
SRC
function dump(n) {
  const out = [];
  for (const c of n.children) {
    if (c.tag === "#text") out.push(["TXT", c.textContent]);
    else if (c.tag === "pre") out.push(["PRE", c.children[0].textContent]);
    else if (c.tag === "code") out.push(["CODE", c.textContent]);
    else if (c.className === "hs-u-cmd") out.push(["CMD", c.textContent]);
    else out.push([c.tag, c.textContent]);
  }
  return out;
}
console.log(JSON.stringify(CASES.map((t) => {
  const el = new Node("div");
  hsUserText(el, t);
  return dump(el);
})));
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class UserBubbleRendering(unittest.TestCase):
    def _run(self, cases):
        src = _srcmap.chat_src()
        parts = [re.search(r"^const HS_U_FENCE = .*$", src, re.M).group(0),
                 re.search(r"^const HS_U_TICK = .*$", src, re.M).group(0)]
        for name in ("hsUserText", "hsUserPlain", "hsUserLiteral"):
            m = re.search(r"\nfunction " + name + r"\(.*?\n}\n", src, re.S)
            self.assertIsNotNone(m, name + " 不见了")
            parts.append(m.group(0))
        js = (HARNESS.replace("SRC", "\n".join(parts))
              .replace("CASES", json.dumps(cases, ensure_ascii=False)))
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_围栏渲染成代码块且不留空行(self):
        got, = self._run(["前言\n```js\nconst a = 1;\n```\n后语"])
        self.assertEqual(got, [["TXT", "前言"], ["PRE", "const a = 1;"], ["TXT", "后语"]])

    def test_行内码单独成节点(self):
        got, = self._run(["没有围栏的 `行内` 文本"])
        self.assertEqual(got, [["TXT", "没有围栏的 "], ["CODE", "行内"], ["TXT", " 文本"]])

    def test_斜杠命令仍着色且行内码不干扰(self):
        got, = self._run(["/goal 完成 `abc` 的事"])
        self.assertEqual(got[0], ["CMD", "/goal"])
        self.assertIn(["CODE", "abc"], got)

    def test_绝对路径不被当成命令(self):
        got, = self._run(["/Users/alice/x.md 这是路径"])
        self.assertEqual(got, [["TXT", "/Users/alice/x.md 这是路径"]])

    def test_未闭合的围栏保持字面(self):
        got, = self._run(["未闭合 ```js\nconst a=1;"])
        self.assertEqual(got, [["TXT", "未闭合 ```js\nconst a=1;"]])

    def test_强调与表格不被渲染(self):
        got, = self._run(["**粗体** 与 | 表格 | 都按字面"])
        self.assertEqual(got, [["TXT", "**粗体** 与 | 表格 | 都按字面"]])


class UserBubbleFolding(unittest.TestCase):
    def test_实时与历史共用同一折叠入口(self):
        self.assertIn("function hsUserBubbleText(v, bub, text, peerHead)", CHAT)
        self.assertIn("if (bub) { hsUserBubbleText(v, bub, text, t.peerHead); hsTurnImgs(v, t); }", CHAT)
        self.assertIn("hsUserBubbleText(v, bub, userText, t.peerHead);", CHAT)

    def test_按实际高度判断且宽度变化会复验(self):
        fn = re.search(r"function hsUserBubbleText\(v, bub, text, peerHead\)[\s\S]+?\n}\n", CHAT)
        self.assertIsNotNone(fn)
        src = fn.group(0)
        self.assertIn("body.scrollHeight > limit + 2", src)
        self.assertIn('getPropertyValue("--hs-u-fold-height")', src)
        self.assertIn('new ResizeObserver', src)
        self.assertIn('v.userFoldObserver.observe(foldRoot)', src)
        self.assertIn('v.el.querySelector(".hs-stream")', src)
        self.assertNotIn('observe(bub)', src)

    def test_展开控件具备状态与键盘语义(self):
        self.assertIn('more.type = "button"', CHAT)
        self.assertIn('more.setAttribute("aria-expanded"', CHAT)
        self.assertIn('DeskI18n.t(open ? "common.less" : "common.showMore")', CHAT)
        self.assertIn(".hs-page .hs-u-more:focus-visible", CSS)

    def test_折叠只收正文不隐藏附件(self):
        self.assertIn(".hs-page .hs-u-body.hs-u-collapsed", CSS)
        self.assertNotIn(".hs-page .hs-bubble.hs-u-collapsed", CSS)

    def test_盖着时建的气泡露面补测(self):
        """量高度要页面露着:盖着的时候 getClientRects 为空,折叠测不了,那一刻建出来的
        气泡就一直保持全文。别处递来的长消息正是在盖着的时候到的——2026-09-10 用户实见
        一条 6000 字的交接消息整篇铺开。露面时补测一次,并走布局事务,免得高度变化后
        阅读位置跟着跳。"""
        fn = re.search(r"function hsShown\(v\)[\s\S]+?\n}\n", CHAT)
        self.assertIsNotNone(fn)
        self.assertIn('hsViewportChange(v, () => {}, false, "fold-shown")', fn.group(0))

    def test_行内码过长时折行而不是顶出气泡(self):
        """别处递来的消息里常有一整条命令写成行内码。实测 605px 的气泡里那条命令
        有 910px,nowrap 时右侧溢出 321px,压到信息卡上(2026-09-10 用户实见)。
        先在空格处折,单个 token 实在放不下才断——与正文代码块同一个取舍。"""
        rule = re.search(r"\.hs-page \.hs-bubble code \{([^}]*)\}", CSS).group(1)
        self.assertIn("white-space: pre-wrap", rule)
        self.assertIn("overflow-wrap: anywhere", rule)
        self.assertNotIn("white-space: nowrap", rule)

    def test_手机气泡字号单独增加半像素(self):
        marker = "/* 手机阅读距离更远，用户侧气泡单独提半像素；桌面端继续沿用正文基础字号。 */"
        mobile = CSS[CSS.index(marker):]
        self.assertIn(".hs-page .hs-u-body { font-size: 15px; }", mobile)
        self.assertIn(".hs-page .hs-u-body code, .hs-page .hs-u-body pre { font-size: 13px; }", mobile)
        desktop_code = re.search(r"\n\.hs-page \.hs-bubble code \{([^}]*)\}", CSS).group(1)
        self.assertIn("font-size: 12.5px", desktop_code)
