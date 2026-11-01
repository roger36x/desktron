"""折叠遮罩要收在气泡自己的底色上,不是写死的常规灰。

2026-09-03 用户实见:跨会话递来的那种紫色气泡内容一长就折叠,而折叠处那一行是灰的。
成因是 .hs-u-collapsed::after 的渐变里写死了 var(--ink-23)——常规气泡的底色。紫气泡靠
另开一条 `background: var(--peer-bubble)` 换底,遮罩那一条并不知情,于是在紫底上收成灰。

改法是把「这枚气泡是什么颜色」收成一个 token:.hs-bubble 定义 --bubble-bg 并用它上色,
变体只改 token,遮罩读同一个 token。再加气泡变体时照这个写法改,遮罩自动跟上。

CDP 实测(两枚气泡并排,都撑到折叠):
    常规  气泡 rgb(59,59,59)   遮罩 linear-gradient(…, rgb(59,59,59) 58%)
    peer  气泡 rgb(49,42,74)   遮罩 linear-gradient(…, rgb(49,42,74) 58%)
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


def rule(sel):
    """按选择器匹配规则，允许与其他选择器共用声明。"""
    target = sel.removesuffix("{").strip()
    source = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", source):
        if target in [s.strip() for s in m.group(1).split(",")]:
            return m.group(0)
    raise AssertionError(sel)


class 气泡底色收成一个token(unittest.TestCase):
    def test_基础气泡定义并使用token(self):
        r = rule(".hs-page .hs-bubble {")
        self.assertIn("--bubble-bg: var(--user-bubble)", r)
        self.assertIn("background: var(--bubble-bg)", r)
        self.assertIn("--user-bubble: #3b3b3b", CSS)
        self.assertIn("--user-bubble: var(--ink-23)", CSS)

    def test_变体只改token不直接写background(self):
        """直接写 background 的话遮罩不知情,又会收成常规灰——这正是这次的 bug。"""
        r = rule(".hs-page .hs-u-peer .hs-bubble {")
        self.assertIn("--bubble-bg: var(--peer-bubble)", r)
        self.assertNotIn("background:", r)

    def test_ai消息采用实色气泡且不描边(self):
        r = rule(".hs-page .hs-u-peer .hs-bubble {")
        self.assertNotIn("border:", r)
        self.assertIn("border-radius: 20px", r)
        self.assertIn("padding: 12px 16px", r)
        self.assertIn("color: var(--peer-text)", r)
        self.assertIn("box-sizing: border-box", r)

    def test_ai消息来源沿用脚本标签样式(self):
        r = rule(".hs-page .hs-u-peer-head {")
        self.assertIn("font-family: var(--mono-code)", r)
        self.assertIn("font-size: 12px", r)
        self.assertIn("font-weight: 350", r)
        self.assertIn("line-height: 1.6", r)
        self.assertIn("color: var(--peer-head)", r)

    def test_遮罩读同一个token(self):
        r = rule(".hs-page .hs-u-body.hs-u-collapsed::after {")
        self.assertIn("var(--bubble-bg) 58%", r)
        self.assertNotIn("--ink-23", r)


class 浅色不必另写一条(unittest.TestCase):
    def test_两套主题共用同一条规则(self):
        """--user-bubble 与 --peer-bubble 各自按主题取值,token 一层转发下来就都对了;
        为遮罩单独补一条浅色覆盖等于把同一件事写两遍,改一处漏一处。"""
        self.assertIsNone(re.search(r'html\[data-theme="light"\][^{]*hs-u-collapsed::after', CSS))
        self.assertIn("--peer-bubble", CSS[CSS.index('html[data-theme="light"]'):])

    def test_ai气泡复用脚本块配色(self):
        self.assertEqual(CSS.count("--peer-bubble: var(--code-block-bg)"), 2)
        self.assertEqual(CSS.count("--peer-text: var(--code-block-text)"), 2)
        self.assertIn("--code-block-bg: #232323", CSS)
        self.assertIn("--code-block-text: #e8e8e8", CSS)
        self.assertEqual(CSS.count("--peer-head: var(--text-weaker)"), 2)
        self.assertIn("--code-block-bg: #f9f9f9", CSS)
        self.assertIn("--code-block-text: #24292f", CSS)
        self.assertIn(".cd-code { background: var(--code-block-bg)", CSS)
        self.assertIn(".cd-code .hljs { color: var(--code-block-text); }", CSS)
        self.assertNotIn("--peer-border:", CSS)

    def test_折叠行与来源行不混入紫灰(self):
        self.assertIn(".hs-page .hs-u-peer .hs-u-body.hs-u-collapsed::after", CSS)
        self.assertIn(".hs-page .hs-u-peer .hs-u-more:hover { color: var(--peer-text); }", CSS)
