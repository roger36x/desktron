"""状态行副行钉成一行:等待期间画面不再定期跳。

2026-09-05 用户报「回合在等待中的时候画面会定期跳动」,补充「主要是 codex 的会话,
在长时间没有输出的时候」。

状态行有两行:顶行是阶段与秒表(.hs-st-t),副行是预告或最近一步(.hs-st-next,内容来自
hsStatusPaint 里的 todoNext / stepLog)。顶行早就钉死了一行(nowrap + 省略号),副行没有,
于是它跟着文字长短在一行与两行之间来回换高。

WebKit 探针实测(1100×700 窗口,副行可用宽 684px,内容截到 64 字):
    13 字 → 18px    23 字 → 18px    53 字 → 35px
副行一变两行,正文总高从 9730 跳到 9747,差 17px。而回合跑动中视口是逐帧钉在底部的
(hsFollowLoop),这 17px 会让整屏抬一下;下一句短了又落回来。

为什么是 codex:它长时间只推理不出正文时仍在推理由摘要,每落一个完整句子 hsLogThink 就
换一次 stepLog(见 app-chat-model.js)。句子长短天然不齐,来回跨过换行阈值。claude 那侧
副行多由工具步驱动,短且不密,所以少见。

排除过的解释(都实测过,不是它们):
  · 不是跟随环的缓动没收住——把会话按到 busy + stalled 静置 30 秒,视口跳动 0 次、
    长帧(>60ms)0 次,几何一动不动;
  · 不是 statusTick 每秒重绘本身——重绘只改文字,文字不换行时高度不变;
  · 不是情境卡每秒重算变更数(hsSituationPaintChanges 由挂在 v.stream 上的
    MutationObserver 触发,统计过 30 秒 30 次)——那一块高度为 0 时照样不跳;
  · 不是 hsBusyVerify 每 60 秒对一次账——session.state=running 在已经 busy 时不动界面。

修好之后同一组句子全是 18px,正文总高恒定 9730。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
MODEL = open(os.path.join(ROOT, "frontend", "app-chat-model.js"), encoding="utf-8").read()


def rule(sel):
    """取某条选择器的声明块。选择器里有 . 和 - ,整条转义后再拼花括号。"""
    m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", CSS)
    return m.group(1) if m else ""


class 副行不换行(unittest.TestCase):
    BASE = ".hs-page .hs-st-next"

    def test_钉成一行(self):
        """换行是高度变化的唯一来源。三件事缺一不可:不换行、超出裁掉、裁掉处收省略号。"""
        body = rule(self.BASE)
        self.assertTrue(body, "副行那条规则不见了")
        self.assertRegex(body, r"white-space:\s*nowrap")
        self.assertRegex(body, r"overflow:\s*hidden")
        self.assertRegex(body, r"text-overflow:\s*ellipsis")

    def test_能收缩(self):
        """flex 项默认 min-width:auto,不给 0 的话 overflow:hidden 收不住内容宽度。"""
        body = rule(self.BASE)
        self.assertRegex(body, r"min-width:\s*0")
        self.assertRegex(body, r"max-width:\s*calc\(100%\s*-\s*12px\)")

    def test_与顶行同一处理(self):
        """顶行早就是这么写的。两行一个规矩,免得日后只改一边。"""
        top = rule(".hs-page .hs-status .hs-st-t")
        for prop in ("white-space", "overflow", "text-overflow"):
            self.assertIn(prop, top)
            self.assertIn(prop, rule(self.BASE))

    def test_仍然独占一行(self):
        """钉成一行是控高度,不是把它并回顶行——并回去顶行就被挤没了。"""
        self.assertRegex(rule(self.BASE), r"flex:\s*1 0 100%")

    def test_父级和状态槽不按内容撑宽(self):
        """省略号只有在父级存在确定宽度时才生效；行内 flex 会被长摘要按内容撑出页面。"""
        bar = rule(".hs-page .hs-proc-bar")
        slot = rule(".hs-page .hs-proc-slot")
        self.assertRegex(bar, r"width:\s*100%")
        self.assertRegex(bar, r"min-width:\s*0")
        self.assertRegex(slot, r"flex:\s*1 1 0")
        self.assertRegex(slot, r"min-width:\s*0")


class 副行内容仍有长度上限(unittest.TestCase):
    def test_截断口还在(self):
        """CSS 的省略号是显示层的兜底;文本这一侧的截断口不能因此撤掉——
        它同时挡住了把整段思考塞进 DOM。"""
        self.assertRegex(MODEL, r"hsEllipsis\(v\.stepLog \|\| \"\", 64\)")


class 手机端压缩进度不越界(unittest.TestCase):
    def test_状态行钉住正文宽度_只收缩轨道(self):
        start = CSS.index("/* 手机正文列只有约 280px")
        mobile = CSS[start:CSS.index("/* 展开之后副行就撤掉", start)]
        self.assertIn(".hs-page .hs-status { width: 100%; box-sizing: border-box; }", mobile)
        self.assertIn(".hs-page .hs-status .hs-cbar { flex: 0 1 112px", mobile)
        self.assertIn("max-width: 40%", mobile)
        self.assertIn(".hs-page .hs-status .hs-cbar-t { flex: 1 1 auto", mobile)
        self.assertIn("min-width: 32px", mobile)

    def test_百分比仍然不收缩(self):
        body = rule(".hs-page .hs-cbar-n")
        self.assertIn("flex: 0 0 auto", body)
        self.assertIn("white-space: nowrap", body)


if __name__ == "__main__":
    unittest.main()
