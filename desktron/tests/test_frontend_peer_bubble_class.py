"""「别处递来的消息」这枚紫气泡,归属由正文那一处认,不在建回合时另认一次。

2026-09-03 用户实见:同一段会话里,已完成的那条 peer 消息是紫的,正在跑的那条却是灰的,
而首行来路(Desktron · … )两条都画着。

成因是同一个判据摆了两处,而两处拿到正文的时机不同:

    建回合(hsTurn)      按 userText 判,决定 .hs-u 上挂不挂 hs-u-peer
    填气泡(hsUserBubbleText) 按 text 判,决定画不画来路那一行

别处递来的消息走两帧:message.updated 先到,那时只有 id、没有正文;正文在随后的
message.part.updated 里,经 hsSetTurnUser 回填(那两行注释本来就写着这件事)。
于是建回合那一次按空串判,一律不是 peer;等正文来了,填气泡这一处把来路画了出来,
却没有人回头改 .hs-u 的类——头是 peer 的、底色不是。

本地自己发的、以及刷新后从历史重建的,建回合时正文就在手上,所以只有「正在跑的那条」
会露出来。这也是它看起来像「未完成的回合才不紫」的原因。

归到有正文的那一处之后,两条路径共用同一个判据。CDP 实测三种走法:

    建时就有正文        hs-u hs-u-peer   rgb(49,42,74)   有来路行
    两帧:补正文前       hs-u             rgb(58,58,61)   无
    两帧:补正文后       hs-u hs-u-peer   rgb(49,42,74)   有来路行
    正文改回普通消息    hs-u             rgb(58,58,61)   无
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MODEL = open(os.path.join(ROOT, "frontend", "app-chat-model.js"), encoding="utf-8").read()

BUBBLE_FN = re.search(r"\nfunction hsUserBubbleText\([\s\S]+?\n}\n", MODEL).group(0)


class 判据只有一处(unittest.TestCase):
    def test_填气泡那一处认peer并改类(self):
        self.assertIn("const peer = rest.includes(HS_PEER_TAIL);", BUBBLE_FN)
        self.assertIn('host.classList.toggle("hs-u-peer", incoming)', BUBBLE_FN)
        self.assertIn('const incoming = peer || !!child || !!childBatch;', BUBBLE_FN)

    def test_建回合那一处不再另判(self):
        """两处各判一次,迟早只改一边;而这次正是两边拿到正文的时机不同暴露出来的。"""
        self.assertIn('u.className = "hs-u";', MODEL)
        self.assertEqual(MODEL.count("includes(HS_PEER_TAIL)"), 1)

    # 判据 2026-09-03 从首行前缀改成外框尾句:首段换成了机器那一侧(desktop / server),
    # 会随部署变,不适合再当判据。这里只钉「一处判定」这条不变量,不钉具体认哪个串。
    def test_来路行与底色同一个判据(self):
        """首行来路和气泡底色必须同生同灭,否则就是这次的现象:头是 peer 的、底色不是。"""
        self.assertEqual(BUBBLE_FN.count("if (incoming) {"), 2)
        # 函数体内 startsWith 只出现在 const peer 那一行(注释里提名字不算)
        self.assertEqual(len(re.findall(r"rest\.includes\(HS_PEER_TAIL\)", BUBBLE_FN)), 1)


class 后来改正文也要跟着改(unittest.TestCase):
    def test_用toggle而不是add(self):
        """add 的话,正文从 peer 改成普通消息(编辑、快照替换)时退不回灰底。
        toggle 带第二个参数是幂等的,重复填同一段正文不会翻来翻去。"""
        self.assertIn('classList.toggle("hs-u-peer", incoming)', BUBBLE_FN)
        self.assertNotIn('classList.add("hs-u-peer")', MODEL)

    def test_气泡尚未入文档时也认得到宿主(self):
        """建回合那条路上,bub 所在的 .hs-u 还没进文档。closest 在游离子树里照样可用,
        但要挡住宿主环境没有 closest 的情形。"""
        self.assertIn("bub.closest && bub.closest(\".hs-u\")", BUBBLE_FN)
