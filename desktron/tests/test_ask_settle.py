"""卡片点完之后要能收尾(2026-09-03 用户实见:点了「已允许」,状态一直停在「处理中」)。

等人回应期间不收尾是对的:收尾会挂上结束时间与复制/分支那一排,而人一批准回合还会
继续吐字,界面就自相矛盾。错的是那道闸的两处副作用——

  · 它把这一代记进了 settledGeneration。记账的语义是「这一代已经收过尾」,而这里恰恰
    没收;等人批准、回合真结束时,收尾帧带着同一个 generation 到达,被开头那道去重闸
    挡掉,于是永远不收尾。
  · 人点完只清 awaitAsk、置「处理中」,不核状态。等待期间到过的收尾帧已经被挡下,
    它不会重来——界面就一直停着。
"""
import os
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CORE = open(os.path.join(ROOT, "frontend", "app-chat-core.js"), encoding="utf-8").read()
OVL = open(os.path.join(ROOT, "frontend", "app-chat-overlays.js"), encoding="utf-8").read()


def _fn(src, name):
    i = src.index("function %s(" % name)
    return src[i:src.index("\n}", i)]


class 等人回应时不记账(unittest.TestCase):
    def test_awaitAsk_那一路不写_settledGeneration(self):
        seg = _fn(CORE, "hsTurnSettled")
        # 只看代码:注释里正解释着「为什么不记 settledGeneration」,连注释一起算就永远红。
        # 段落取到这一段自己的 return 为止(按第一个 "}" 截会把注释里的括号算进去)。
        import re as _re
        i = seg.index("if (v.awaitAsk)")
        block = seg[i:seg.index("return;", i)]
        code = _re.sub(r"(?m)^\s*//.*$", "", block)
        self.assertNotIn("settledGeneration", code,
                         "这一代还没真收尾,记了账之后真正的收尾帧会被去重闸挡掉")

    def test_去重闸仍在(self):
        """不记账是对的,但去重本身要留着——同一代重复收尾会挂两排收尾标记。"""
        seg = _fn(CORE, "hsTurnSettled")
        self.assertIn("v.settledGeneration === gen", seg)

    def test_真收尾时才记(self):
        seg = _fn(CORE, "hsTurnSettled")
        self.assertIn("if (gen) v.settledGeneration = gen;", seg)
        # 记账要排在真正收尾的动作之后
        self.assertLess(seg.index("hsBusy(v, false)"),
                        seg.rindex("v.settledGeneration = gen"))


class 回应之后核一次状态(unittest.TestCase):
    def test_点完要核对(self):
        """等待期间到过的收尾帧被挡下了,而它不会重来。"""
        seg = _fn(OVL, "hsAsk")
        i = seg.index("const reply")
        self.assertIn("hsStateSync(v)", seg[i:])

    def test_排在提交回应之后(self):
        """先让后端知道人的决定,再问状态;反过来问到的还是旧的。"""
        seg = _fn(OVL, "hsAsk")
        self.assertLess(seg.index("/permission`"), seg.index("hsStateSync(v)"))

    def test_核对失败不抛(self):
        """网络抖一下不该让点击这条路炸掉——人已经点了,决定也发出去了。"""
        seg = _fn(OVL, "hsAsk")
        i = seg.index("hsStateSync(v)")
        self.assertIn("catch", seg[i:i + 80])


if __name__ == "__main__":
    unittest.main()
