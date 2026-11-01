"""会话名不再是「首条消息前 60 字」的原样切片。

2026-09-03 用户实见一段 claude 会话叫「起手已知### 项目记忆摘要thinking 工作区放不归属…」。
一条名字里凑齐了三类毛病:

  一、切到了 Desk 自己前置的当前上下文。它由 domains.py 组装、以 SCENE_SEP 分隔，只加在送给
      引擎的那一份里,界面上的用户气泡仍是原话;但 claude 重建历史时的标题兜底读的正是
      引擎记录,于是把 Desk 的开场白当成了用户的第一句。codex 那侧 2026-08-26 修过
      (term_titles._engine_title_clean),claude 当时落下了——与侧栏多标签同一种形状:
      一个问题在一条路上修过,另一条路没跟上。
  二、markdown 标记原样留着:`## `、`- `、`**`、反引号在一行标题里全是噪音。
  三、换行被折没,前后两行的字直接粘成一个词——「摘要」接「thinking」就是这么来的。

清洗集中在 bus.clean_session_title,两条写入路径共用:live 首轮走 cli_store._touch,
claude 重建历史走 claude_transcript。

返回空串是有意义的答案而不是失败:整条都是当前上下文、或者只有一段代码围栏时,正确的做法是
跳过这一条去看下一条消息。所以 claude 那条兜底必须 continue,不能 break。
"""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
# deskcfg 在 import 期就读 sys.argv,与 test_claude_turn_boundary.py 同一套垫片
_argv = sys.argv
sys.argv = ["x", "frontend", "7765"]
try:
    from engine_adapters.bus import (clean_session_title, SCENE_SEP, SCENE_HEAD,
                                     LEGACY_SCENE_SEPS, LEGACY_SCENE_HEADS)
finally:
    sys.argv = _argv

STORE = open(os.path.join(ROOT, "desktron", "lib", "engine_adapters", "cli_store.py"),
             encoding="utf-8").read()
TRANS = open(os.path.join(ROOT, "desktron", "lib", "engine_adapters", "claude_transcript.py"),
             encoding="utf-8").read()


class 当前上下文不进会话名(unittest.TestCase):
    def test_上下文在前用户的话在后只取后半截(self):
        got = clean_session_title(SCENE_HEAD + "\n\n### 项目记忆摘要\n\n略" + SCENE_SEP
                                  + "我觉得我们不一定要依赖 fornax。")
        self.assertEqual(got, "我觉得我们不一定要依赖 fornax。")

    def test_整条都是当前上下文返回空串(self):
        """空串=「这条消息里没有用户的话」,调用方据此接着找下一条。"""
        self.assertEqual(clean_session_title(SCENE_HEAD + "\n\n### 项目记忆摘要\n\nthinking 工作区"), "")

    def test_旧版上下文仍能识别(self):
        old_head, old_sep = LEGACY_SCENE_HEADS[0], LEGACY_SCENE_SEPS[0]
        self.assertEqual(clean_session_title(old_head + "\n\n旧上下文" + old_sep + "用户原话"),
                         "用户原话")
        self.assertEqual(clean_session_title(old_head + "\n\n只有旧上下文"), "")

    def test_用户实见的那条名字不会再出现(self):
        bad = (SCENE_HEAD + "\n\n### 项目记忆摘要\n\nthinking 工作区放不归属事故与交付的治理思考："
               "AI Native 如何落地、资产与运营、Desk 形态；产出是可被推翻的推理链。")
        self.assertEqual(clean_session_title(bad), "")
        self.assertNotIn("起手已知###", clean_session_title(bad))


class markdown标记不进会话名(unittest.TestCase):
    def test_标题行去掉井号(self):
        self.assertEqual(clean_session_title("## 想讨论后训练这条路"), "想讨论后训练这条路")

    def test_列表与引用去掉前缀(self):
        self.assertEqual(clean_session_title("- 第一条要点"), "第一条要点")
        self.assertEqual(clean_session_title("> 引用的一句"), "引用的一句")
        self.assertEqual(clean_session_title("1. 编号的一条"), "编号的一条")

    def test_行内强调与反引号去掉(self):
        self.assertEqual(clean_session_title("把 **加粗** 和 `代码` 都去掉"), "把 加粗 和 代码 都去掉")

    def test_链接只留文字(self):
        self.assertEqual(clean_session_title("[方案文档](http://x/y) 里的第三节"), "方案文档 里的第三节")

    def test_代码围栏整段跳过(self):
        """跳到闭合围栏为止;只跳开头那一行的话,拿到的会是第一行代码。"""
        self.assertEqual(clean_session_title("```python\nprint(1)\n```\n真正的问题是这样的"),
                         "真正的问题是这样的")
        self.assertEqual(clean_session_title("```\nonly code\n```"), "")

    def test_表格与分隔线跳过(self):
        self.assertEqual(clean_session_title("---\n\n只有分隔线在前"), "只有分隔线在前")
        self.assertEqual(clean_session_title("| a | b |\n真正的一句"), "真正的一句")


class 换行折成空格而不是粘在一起(unittest.TestCase):
    def test_只取第一段有内容的行(self):
        """原来是整块切 60 字,换行在显示时被折没,上下两行的字粘成一个词。
        现在按行取,第一句就是名字,后面的内容根本不参与。"""
        self.assertEqual(clean_session_title("摘要\n\nthinking 工作区"), "摘要")

    def test_同一行内的连续空白折成一个空格(self):
        self.assertEqual(clean_session_title("前面   一段    后面"), "前面 一段 后面")


class 截断落在标点上(unittest.TestCase):
    def test_够短不动也不加省略号(self):
        s = "短句"
        self.assertEqual(clean_session_title(s), s)

    def test_超长在标点处断开(self):
        s = "先说结论，然后展开分析，接着给出取舍，最后提出下一步行动方案，这样读起来会比较顺畅一些。"
        got = clean_session_title(s, limit=30)
        self.assertTrue(got.endswith("…"))
        self.assertLessEqual(len(got), 31)
        self.assertFalse(got.rstrip("…").endswith("，"))

    def test_没有标点也要截断(self):
        got = clean_session_title("这是一句很长的话" * 12, limit=30)
        self.assertTrue(got.endswith("…"))
        self.assertLessEqual(len(got), 31)

    def test_断点只在末尾四分之一里找(self):
        """找得太靠前的话,一句长话会被砍成半截意思。"""
        s = "开头，" + "接下来是一段没有标点的长内容" * 3
        got = clean_session_title(s, limit=30)
        self.assertNotEqual(got, "开头…")


class 两条写入路径都接上了(unittest.TestCase):
    def test_live首轮走cli_store(self):
        self.assertIn('s["title"] = clean_session_title(title) or title[:60]', STORE)

    def test_压不出人话时退回原文(self):
        """宁可难看也别让会话没有名字。"""
        self.assertIn("or title[:60]", STORE)

    def test_claude兜底继续找下一条而不是break(self):
        """清洗返回空串表示这条消息里没有用户的话。break 的话标题就落空,
        又会退到别处去猜——而那正是这个 bug 的来源。"""
        seg = TRANS[TRANS.index("if not title:\n                    for m in msgs:"):]
        seg = seg[:seg.index("if title:")]
        self.assertIn("t0 = clean_session_title(t0)", seg)
        self.assertIn("continue", seg)
        self.assertIn("if t0:", seg)
