"""别处递来的消息按 markdown 渲染;用户自己打的字仍按字面显示。

2026-09-03 用户实见:紫气泡里 `**背景**`、`- 列表` 的记号一个不落地显在字面上。

这不是漏了渲染,是用户气泡本来就有意按字面走(见 hsUserText 的注释):粘一段提示词进来,
正文里的 ** 、裸路径(src/**/*.js)、正则都是字面量,渲染 markdown 会把它们重新解释,
而这类内容在这个界面里出现得比强调语法频繁得多。

但别处递来的那一条不是用户打的字——它是另一个 agent 写的 markdown,与压缩摘要同一种情形
(hsCompactBox 早就为此渲染了摘要正文,理由写在那里)。两类内容的默认恰好相反,所以按来路
分流:peer 走 md.render,其余照旧走 hsUserText。

换行按聊天体处理。markdown 默认(breaks:false)把单个换行并进同一段,实测那条
`curl -fsSL …` 被接到「已发布:」后面;而这类消息是一行一条写的(命令、现场记录、时间线)。
只在渲染这一条时临时打开 breaks,渲完立刻还原——助手正文那条路仍按文档体走。

用同一个 markdown-it 实例而不另建一个:围栏渲染、linkify 的 fuzzyLink、CJK 强调那几处
补丁都挂在它身上(见 app-core.js),另起一个就要把那几处再抄一遍,迟早只改一边。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MODEL = open(os.path.join(ROOT, "frontend", "app-chat-model.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()

BUBBLE_FN = re.search(r"\nfunction hsUserBubbleText\([\s\S]+?\n}\n", MODEL).group(0)


class 按来路分流(unittest.TestCase):
    def test_peer走markdown(self):
        self.assertIn("if (incoming) {", BUBBLE_FN)
        self.assertIn("md.render(rest)", BUBBLE_FN)
        self.assertIn('body.classList.add("md")', BUBBLE_FN)

    def test_其余仍按字面(self):
        """用户自己打的 ** 、裸路径、正则是字面量,不能被重新解释。"""
        self.assertIn("hsUserText(body, rest);", BUBBLE_FN)
        seg = BUBBLE_FN[BUBBLE_FN.rindex("if (incoming) {"):]
        self.assertIn("} else {", seg)

    def test_围栏那条路仍在(self):
        """hsUserText 认三反引号与行内码,这两样对用户消息仍然有效,不能顺手删掉。"""
        self.assertIn("function hsUserText(el, text)", MODEL)
        self.assertIn("HS_U_FENCE", MODEL)


class 换行按聊天体(unittest.TestCase):
    def test_渲染时临时打开breaks(self):
        self.assertIn("md.set({ breaks: true });", BUBBLE_FN)

    def test_渲完立刻还原(self):
        """共享实例,不还原会把助手正文那条路一并改成聊天体。
        用 finally:render 抛错时也要还原。"""
        self.assertIn("finally { md.set({ breaks: false }); }", BUBBLE_FN)

    def test_不另建实例(self):
        """另起一个就要把围栏渲染、fuzzyLink、CJK 强调那几处补丁再抄一遍。"""
        self.assertNotIn("markdownit(", MODEL)


class 气泡里的md要撤掉pre_wrap(unittest.TestCase):
    def test_有对应样式(self):
        """.hs-bubble 是 pre-wrap;留着的话渲染后的块级元素之间会再叠上源文里的空行。"""
        m = re.search(r"\.hs-page \.hs-u-body\.md \{[^}]*\}", CSS)
        self.assertTrue(m, "缺 .hs-u-body.md 样式")
        self.assertIn("white-space: normal", m.group(0))

    def test_段距比常规正文收一档(self):
        """气泡本来就窄,.md 的 12px 段距在这个宽度里显得散。"""
        self.assertIn(".hs-page .hs-u-body.md p, .hs-page .hs-u-body.md ul, "
                      ".hs-page .hs-u-body.md ol { margin: 0 0 9px; }", CSS)

    def test_末块不留下边距(self):
        self.assertIn(".hs-page .hs-u-body.md > :last-child { margin-bottom: 0; }", CSS)
