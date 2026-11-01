"""断线时用户打的那句话不能丢,切会话要重试。

两件事同源:网络会整段断开十几秒到几分钟,而这一侧原来处处按「请求要么成功要么立刻失败」写。

1. 发送:原来先清输入框、把「空」立刻写进草稿,再发 POST。那句话在落地之前只活在内存里,
   请求失败或页面被关掉,草稿是空的、后端也没有,重开就没了(2026-08-25 用户实见)。
2. 切会话:一次连接级失败就弹「打不开:load failed」,而那多半只是网络正在恢复。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()


def fn(name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), CHAT, re.S)
    assert m, "%s 不见了" % name
    return m.group(0)


class UnsentSurvivesReload(unittest.TestCase):
    """没收到回执的那一句,要在草稿里留着存根。"""

    def test_草稿里有正在发的那一格(self):
        seg = fn("hsDraftWrite")
        self.assertIn("v.sending", seg)
        self.assertIn("s: sd", seg, "存根没写进落盘的那个对象")

    def test_有存根就不算空草稿(self):
        """判空那一行漏掉它的话,存根写进去下一拍就被当成空草稿删掉。"""
        seg = fn("hsDraftWrite")
        # 取那一行本身,不去解析括号里的表达式:里面有 t.trim() 这样的调用,
        # 按括号切会在第一个右括号处断开。
        line = next((l for l in seg.splitlines() if "localStorage.setItem(HS_DRAFT_KEY" in l
                     and "JSON.stringify({ t, a, q" in l), "")
        self.assertTrue(line, "落盘那一行的写法变了")
        cond = next((l for l in seg.splitlines() if l.strip().startswith("if (t.trim()")), "")
        self.assertTrue(cond, "判空那一行的写法变了")
        self.assertIn("|| sd", cond, "判空漏了存根:写进去下一拍就被当成空草稿删掉")

    def test_发送前落存根_成功才撤(self):
        seg = fn("hsDispatch")
        i_set = seg.index("v.sending = { text, atts }")
        i_post = seg.index("prompt_async")
        self.assertLess(i_set, i_post, "存根要在 POST 之前落盘,之后就晚了")
        self.assertIn("v.sending = null", seg)

    def test_重开时挂成待判_不直接回填也不重发(self):
        """「在飞」不等于「没到」。直接回填的话那一句可能已经在会话里跑着,人再按一次
        就是第二遍;自动重发更糟。挂起来,问过后端再决定。"""
        seg = fn("hsDraftLoad")
        self.assertIn("d.s", seg)
        self.assertIn("v.pendingSend", seg)
        self.assertNotIn("hsRestoreUnsent", seg, "不该在问过后端之前就回填")
        self.assertNotIn("hsDispatch", seg, "恢复不该顺手替人重发")

    def test_直发失败要看返回值(self):
        """hsDispatch 自己吞掉 POST 异常并回 false,只挂 catch 的话失败时什么都不做。"""
        seg = fn("hsSend") if "function hsSend(" in CHAT else CHAT
        i = CHAT.index("hsDispatch(v, text, atts).then")
        tail = CHAT[i:i + 400]
        self.assertIn("if (!ok) hsRestoreUnsent(v, text, atts)", tail)

    def test_放回时不覆盖已经打了的内容(self):
        """等待期间人可能又打了一句,冲掉它等于丢第二次。"""
        seg = fn("hsRestoreUnsent")
        self.assertIn("hsInpText(v.inp)", seg)
        self.assertIn("now.indexOf(back) < 0", seg, "没有防重复插入")


class SwitchRetries(unittest.TestCase):
    def test_连接级失败要退避重试(self):
        seg = fn("termToHtml")
        self.assertIn("[0, 600, 1800]", seg, "没有退避序列")
        self.assertIn("/session/adopt", seg)

    def test_后端答过话就不重试(self):
        """4xx/5xx 是后端明确拒绝,再问一遍还是同一个答案。"""
        seg = fn("termToHtml")
        self.assertIn("break", seg)
        self.assertRegex(seg, r"test\(lastErr\)")

    def test_重试期间换了目标就作废(self):
        """快速连点两段会话时,后点的先回、先点的后回会切错。"""
        seg = fn("termToHtml")
        head = seg[:seg.index("/session/adopt")]
        self.assertIn("if (stale()) return;", head)


if __name__ == "__main__":
    unittest.main()


class DoesNotGuessWhetherItArrived(unittest.TestCase):
    """请求失败 ≠ 没发出去。

    prompt_async 是先起回合、再回 204(ui_server 那一路 `ad.prompt_async(...)` 之后才
    `send_response(204)`)。204 在隧道里丢掉时回合已经在跑,把话退回输入框就会变成
    「消息还在会话里,同一句又回到框里」,人再按一次就发了两遍(2026-08-25 用户指出)。
    """

    def test_失败后先问后端(self):
        seg = fn("hsDispatch")
        self.assertIn("await hsSentOrNot(v, text, evidence)", seg)
        i_ask = seg.index("hsSentOrNot")
        i_fail = seg.index('hsErrorNotice(v, "发送失败："')
        self.assertLess(i_ask, i_fail, "还没问就先判了失败")
        self.assertNotIn('hsStatus(v, "error", "发送失败', seg)

    def test_三种结论各有各的去处(self):
        seg = fn("hsDispatch")
        self.assertIn('got === "sent"', seg)
        self.assertIn('got === "unknown"', seg)
        for want in ("v.sending = null", "hsStateSync(v)", 'v.pendingSend = { ...attempt.sending }'):
            self.assertIn(want, seg)

    def test_确认发出去了就不算失败(self):
        """回 false 的话调用方会把话退回输入框或队列,而那正是要避免的。"""
        seg = fn("hsDispatch")
        tail = seg[seg.index('got === "sent"'):]
        self.assertIn("return true;", tail[:300])

    def test_问不到时不动界面(self):
        """两种做法都会错一次:退回可能发两遍,当成成功可能永远等一个不存在的回合。
        选不动,并且说清楚现在是什么处境。"""
        seg = fn("hsDispatch")
        blk = seg[seg.index('got === "unknown"'):]
        blk = blk[:500]
        self.assertNotIn("hsRestoreUnsent", blk)
        self.assertIn('hsStatus(v, "working", "发送中")', blk)

    def test_旧请求缺少编号时不猜测送达(self):
        seg = fn("hsSentOrNot")
        self.assertIn('return "unknown"', seg)
        self.assertNotIn('/state`', seg)
        self.assertNotIn('/message`', seg)


    def test_悬着的那句会被复核(self):
        """「可能已经发出去」不是一个能一直停在那儿的状态。"""
        seg = fn("hsFollowTick")
        self.assertIn("v.pendingSend", seg)
        self.assertIn("hsSentOrNot(v, text, evidence)", seg)
        self.assertIn("hsRestoreUnsent(v, text, atts)", seg, "判明没到就得把话还给人")
