"""用户主动往上走时,贴底跟随要立刻让路。

流式期间视图贴着底(stick),打字机每帧钉一次底。刻度点击的平滑滚动每帧只挪几十像素,
挪出 80px 阈值之前就被钉回去,平滑动画同时被程序化滚动取消——表现为「点了刻度没反应、
整页拖不动」,直到吐字出现间隙才跳过去。整段提交(而非逐字流)的引擎一块排空几百毫秒,
锁死窗口最长,最先在它那儿暴露(2026-08-15)。

修法:点刻度/预览卡、滚轮向上,都先 hsUnstick(置 stick=false 并取消已排队的钉底帧),
再发起滚动。这里守住三处接线和 hsUnstick 自身的两个动作。
"""
import os
import re
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT_JS = os.path.join(ROOT, "frontend", "app-chat.js")


class ScrollUnstick(unittest.TestCase):
    def setUp(self):
        self.src = _srcmap.chat_src()

    def test_hsUnstick_置位并取消排队帧(self):
        m = re.search(r"function hsUnstick\(v\) \{(.*?)\n}", self.src, re.S)
        self.assertIsNotNone(m, "hsUnstick 不见了")
        body = m.group(1)
        self.assertIn("hsSetStick(v, false)", body)
        setter = re.search(r"function hsSetStick\(v, on\) \{(.*?)\n}", self.src, re.S).group(1)
        self.assertIn("hsViewportCancel(v)", setter)

    def test_钉底帧回调要再核一次stick(self):
        # hsScroll 的 rAF 是先排队后执行:排队时 stick 为真、执行前被 hsUnstick 掉,
        # 回调若不再核一次就照样钉底,脱钩等于没脱。
        m = re.search(r"function hsViewportSchedule\(v\) \{.*?requestAnimationFrame\(\(\) => \{(.*?)\}\);",
                      self.src, re.S)
        self.assertIsNotNone(m)
        self.assertIn("s.epoch !== epoch", m.group(1))
        self.assertIn("hsViewportCommit(v, v.stick && !!v.busy)", m.group(1))
        commit = re.search(r"function hsScrollCommit\(v[^)]*\) \{(.*?)\n}", self.src, re.S).group(1)
        self.assertIn("!v.stick", commit)
        self.assertIn("return false", commit)

    def test_刻度点击先脱钩再滚动(self):
        m = re.search(r'v\.toc\.addEventListener\("click".*?\}\);', self.src, re.S)
        self.assertIsNotNone(m)
        seg = m.group(0)
        self.assertIn("hsUnstick(v)", seg)
        self.assertLess(seg.index("hsUnstick(v)"), seg.index("hsViewportReveal"))

    def test_预览卡点击先脱钩再滚动(self):
        m = re.search(r"v\.peekEl\.onclick = \(\) => \{.*?\};", self.src, re.S)
        self.assertIsNotNone(m)
        seg = m.group(0)
        self.assertIn("hsUnstick(v)", seg)
        self.assertLess(seg.index("hsUnstick(v)"), seg.index("hsViewportReveal"))

    def test_滚轮向上即脱钩(self):
        m = re.search(r'v\.body\.addEventListener\("wheel", \(e\) => \{(.*?)\n  \}', self.src, re.S)
        self.assertIsNotNone(m, "wheel 监听不见了")
        self.assertIn("deltaY < 0", m.group(1))
        self.assertIn("hsUnstick(v)", m.group(1))

    def test_触摸和键盘向上也立即脱钩(self):
        build = re.search(r"function hsBuild\(.*?\n}", self.src, re.S).group(0)
        touch = build[build.index('v.body.addEventListener("touchmove"'):]
        self.assertIn("hsUnstick(v)", touch[:touch.index("}, { passive: true });")])
        keys = build[build.index('v.body.addEventListener("keydown"'):]
        self.assertIn("hsUnstick(v)", keys[:keys.index("});")])


if __name__ == "__main__":
    unittest.main()
