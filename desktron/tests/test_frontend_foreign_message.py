"""别处发来的那一句要在这一侧显示出来,而且不能把本地那一轮认走。

手机与桌面同时开着同一段会话时,消息可能从任一端发出。后端把每条用户消息经 SSE 广播
(message.updated 先到,只有 id;正文在随后的 message.part.updated 里),而前端原本一律丢掉
用户消息的 part——气泡由本地发送那一路自己画。于是别处发的那句在这一侧只见回复不见提问,
要刷新之后从记录里重建才看得到(2026-08-18 实见)。

补气泡这一步由引擎适配层加上;这里守的是它的认领判据。原来的判据是「末尾那一轮有没有 uid」,
而本地发出到自己的 message.updated 回来之间有一个往返的空窗——这期间别处那条先到的话,
它的 id 会被盖到本地这一轮头上:外来那句因为本地这轮已有正文而被跳过、永不显示;本地自己的
id 随后又建一个回合,同一句话出现两次;助手回复按 uid 找宿主,还可能挂错。
判据因此要带上「这一轮是不是我发的」。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()


def fn(name, sig="v"):
    m = re.search(r"\n(?:async )?function %s\(%s.*?\n}" % (re.escape(name), sig), CHAT, re.S)
    assert m, "%s 不见了" % name
    return m.group(0)


class ForeignMessageShows(unittest.TestCase):
    def test_没见过的id就补一个回合(self):
        seg = fn("hsApply", "v, type, p")
        self.assertIn("!v.turns.some((x) => x.uid === info.id)", seg)
        self.assertIn('hsNewTurn(v, "", info.id, []', seg)

    def test_外来新回合先释放上一轮状态(self):
        """实时视图复用一枚状态节点；不切断归属就会把上一轮状态搬到新回合。"""
        seg = fn("hsApply", "v, type, p")
        branch = seg[seg.index('else if (info.id && !v.turns.some'):]
        self.assertLess(branch.index("hsStatusClear(v)"), branch.index("hsNewTurn(v"))
        self.assertLess(branch.index("v.stats = null"), branch.index("hsNewTurn(v"))
        self.assertLess(branch.index('v.todoNow = v.todoNext = ""'), branch.index("hsNewTurn(v"))

    def test_正文由随后那一帧填进去(self):
        seg = fn("hsApply", "v, type, p")
        self.assertIn("hsSetTurnUser(v, t, q.text, q.peer_head)", seg)
        # 只填还空着的那一轮:本地已经画好的不能被覆盖
        i = seg.index("hsSetTurnUser")
        self.assertIn("!t.user", seg[max(0, i - 80):i])

    def test_空正文也建得出气泡(self):
        """补的那一轮先建空气泡、正文后填。hsNewTurn 只在 userText 为 null 时不建气泡,
        传空串仍要建——否则后面 hsSetTurnUser 找不到 .hs-bubble,那句话永远填不进去。"""
        seg = fn("hsNewTurn", "v, userText")
        self.assertIn("if (userText != null)", seg)


class OnlyClaimOwnTurn(unittest.TestCase):
    def test_认领要带上_是我发的(self):
        seg = fn("hsApply", "v, type, p")
        self.assertIn("if (t && !t.uid && t.mine)", seg)
        self.assertNotIn("if (t && !t.uid) { t.uid = info.id;", seg)

    def test_认领之后撤标记(self):
        """不撤的话,下一条别处来的消息会被同一轮再认领一次。"""
        seg = fn("hsApply", "v, type, p")
        i = seg.index("if (t && !t.uid && t.mine)")
        self.assertIn("t.mine = false", seg[i:i + 120])

    def test_标记只打在真会产生后端消息的那条路上(self):
        seg = fn("hsDispatch", "v, text, atts")
        self.assertIn("t0.mine = true;", seg)
        # /compact 那一轮后端不经 SSE 发用户消息,不打标记,免得它去认领别人的 id
        cmp_seg = re.search(r'hsNewTurn\(v, "/compact"[^\n]*\n', CHAT).group(0)
        self.assertNotIn("mine", cmp_seg)

    def test_发失败要撤标记(self):
        """后端根本没有这条消息,标记留着就会让它认领后来别处那一句。"""
        seg = fn("hsDispatch", "v, text, atts")
        i = seg.index("catch")
        self.assertIn("t0.mine = false", seg[i:])


if __name__ == "__main__":
    unittest.main()
