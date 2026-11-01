"""刷新 / sync home 自动重载时,历史不能灌进一个非空的视图。

页面一建好就注册进 _hsViews 开始收 SSE,而历史要等一次 /session/<id>/message 往返才回来。
回合正在流式输出时增量帧会先到:那一刻 v.turns 还是空的,hsTurnFor 只好新建一个【没有
用户气泡】的回合把这条消息认领下来(实跑验证过:uid=null、msgId 被认领);随后 hsLoad
把整段历史 append 在它下面。于是界面上正在生成的那段回答孤零零排在最上方、上面没有提问,
真正的提问排在最下方、答复区空着。

sync home 会让 /dev/token 变、app-init.js 的 devAutoReload 在 1.5 秒内无条件 reload,
所以开发态下「回合跑到一半重载」是常态路径,不是窄窗口竞态。

hsRebuild 那侧本来就是先清后灌,hsLoad 要对齐它。
"""
import os
import re
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _chat():
    return _srcmap.chat_src()


class LoadIntoDirtyView(unittest.TestCase):
    def setUp(self):
        self.js = _chat()
        m = re.search(r"\nasync function hsLoad\(v\)[\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(m, "hsLoad 不见了")
        self.fn = m.group(0)

    def test_灌历史之前先清视图(self):
        i = self.fn.index("v.turns = []")
        j = self.fn.index("hsPourFast(v, msgs)")
        self.assertLess(i, j, "清空要排在灌历史之前,否则孤儿回合会留在最上面")

    def test_认领表也一起清(self):
        """v.roles 与 v.asideOf 记的都是「哪条消息归哪个回合」。留着旧的,历史灌进来之后
        同一个 messageID 会被认回那个已经不存在的回合。"""
        head = self.fn[:self.fn.index("hsPourFast(v, msgs)")]
        self.assertIn("v.roles = new Map()", head)
        self.assertIn("v.asideOf = new Map()", head)

    def test_只在脏视图上清(self):
        """常态(冷页首次打开)v.turns 本来就是空的,不该白跑一趟清空与状态复位。"""
        self.assertIn("if (v.turns.length) {", self.fn)

    def test_与_hsrebuild_同一套动作(self):
        """两条路都是「拿一份完整历史重铺」,清的东西要一致——不一致就会在其中一条上留残影。"""
        rb = re.search(r"\nfunction hsRebuild\(v, msgs\)[\s\S]*?\n}\n", self.js).group(0)
        for act in ("v.turns = []", "v.roles = new Map()", "hsStatusClear(v)"):
            self.assertIn(act, rb, act)
            self.assertIn(act, self.fn, act)


class DoneFlagAfterReload(unittest.TestCase):
    """刷新恢复之后,还在长的那一块不能留着 done。

    hsPour 按「历史一律 markdown」把快照里的块一律标成 done,而正在跑那条消息的块也从那儿
    过一遍。done 一旦为真就再没有地方改回来:之后每一帧都跳过 hsPaintGap 的节流,走
    md.render(全文) + linkify + 整棵 querySelectorAll。用真源码量过——同一块 20000 字、
    3 秒 200 帧增量,done=true 走 200 次全量重排,done=false 只走 14 次(14 倍)。
    长回复中途刷新之后界面发卡就是这么来的;思考块的标题也会提前定型成「思考」。
    """

    def setUp(self):
        self.js = _chat()

    def test_增量到达时收回_done(self):
        fn = re.search(r"\nfunction hsQueue\(v, part, delta\)[\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(fn, "hsQueue 不见了")
        head = fn.group(0)[:fn.group(0).index("part.buf =")]
        self.assertIn("hsOpenNarrative(part)", head, "增量进来时要先重新打开这一块")
        reopen = re.search(r"\nfunction hsOpenNarrative\(part\)[\s\S]*?\n}\n", self.js).group(0)
        self.assertIn("part.done = false", reopen, "重新打开时必须收回 done")
        self.assertIn("part.settled = false", reopen, "重新打开时也必须收回分段定稿状态")

    def test_节流仍按终态或分段定稿状态分流(self):
        """收回状态之所以有用,是因为 hsPaintLive 在节流前直接绘制定稿内容。"""
        fn = re.search(r"\nfunction hsPaintLive\(v, part\)[\s\S]*?\n}\n", self.js).group(0)
        self.assertLess(fn.index('if (part.type === "tool") {'), fn.index("hsPaintGap("))
        self.assertLess(fn.index("if (part.done || part.settled) { hsPaint(v, part); return; }"),
                        fn.index("hsPaintGap("))


if __name__ == "__main__":
    unittest.main()
