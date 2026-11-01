"""排队的消息不能卡死在清单里。

排队是「一回合在跑的时候接着输入,轮到它自然会发」。原来这个「轮到」只有一个来源:
回合边界(hsApply 收到收场帧那一路,加 hsBusyVerify 的兜底)。那一下一段会话只发生一次,
而队列会跨越它活下来——它跟草稿一起进 localStorage(hsDraftWrite 的 d.q),刷新之后由
hsDraftLoad 恢复。恢复的那一刻回合早已收尾,再没有任何东西会去发它。

2026-08-17 实见:desktron rc 那段会话,AI 回完了,排队那条还停在原位。后端查过
(49 个回合、在跑的 0 个)确实空闲,所以不是「还没轮到」,是这一条路根本没有出口。

补的出口在 hsFollowTick——那是唯一还在跑的、能确认「此刻不忙」的地方。
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


class QueueAlwaysDrains(unittest.TestCase):
    def test_空闲时补一个出口(self):
        seg = fn("hsFollowTick")
        self.assertIn("if (!v.busy && (v.queue || []).length) {", seg)
        self.assertIn("if (v.turnReady) hsQFlush(v)", seg)
        self.assertIn("else await hsStateSync(v)", seg)

    def test_不忙才发(self):
        self.assertIn("v.busy || !v.turnReady", fn("hsQFlush"))

    def test_原来那道拦截已撤(self):
        """老代码是「有队列就直接 return」,把唯一可能救它的那一拍也关掉了。"""
        seg = fn("hsFollowTick")
        self.assertNotIn("if ((v.queue || []).length) return;", seg)

    def test_页面级调度不依赖当前会话可见(self):
        self.assertIn("hsFollowTick(v, true)", fn("hsQueuePump"))
        self.assertIn("hsHidden(v) && !queueOnly", fn("hsFollowTick"))

    def test_翻过页也照发(self):
        """v.paged 拦的是「以存储为准重建」,与发不发队列无关——拦在它前面。"""
        seg = fn("hsFollowTick")
        self.assertLess(seg.index("hsQFlush(v)"), seg.index("v.paged"))

    def test_队列确实会跨刷新活下来(self):
        # 出口是为这条路补的:存进草稿、再读回来
        self.assertIn("q.length", fn("hsDraftWrite"))
        self.assertIn("v.queue = d.q.map", fn("hsDraftLoad"))

    def test_先摘掉再派发_不重发(self):
        """摘掉在派发之前:下一拍(十秒后)不能把同一批又发一遍。"""
        seg = fn("hsQFlush")
        self.assertLess(seg.index("v.queue = []"), seg.index("hsDispatch"))
        self.assertIn("v.qSending", seg)          # 同一拍内也不并发
        self.assertIn("v.busy || !v.turnReady", seg)  # 控制面确认 ready 前不碰 writer

    def test_没发成要放回清单(self):
        """摘掉是界面动作,发出去才是结果。两者之间任何一步出错都不能让消息无声消失——
        2026-08-17 实见:清单里那条不见了,后端存储里却没有对应的用户消息(25 条用户记录
        正好是 STORE_TURNS 上限,末条仍是上一轮的助手回复),trace 里也没有 turn.begin,
        也就是 prompt_async 根本没被调用过,而界面上什么提示都没有。"""
        seg = fn("hsQFlush")
        self.assertIn("v.queue = list.concat(v.queue || [])", seg)
        self.assertIn('hsErrorNotice(v, "队列发送失败："', seg)
        self.assertNotIn('hsStatus(v, "error", "队列发送失败', seg)
        # 半路抛出时 hsDispatch 已经切成「处理中」,要还原,否则页面白转
        self.assertIn("hsBusy(v, false)", seg)

    def test_派发的成败要能被上层看见(self):
        """原来 hsDispatch 自己吞掉 POST 失败,调用方无从判断——队列于是把失败当成功。"""
        seg = fn("hsDispatch")
        self.assertIn("return true;", seg)
        self.assertIn("return false;", seg)
        self.assertLess(seg.index("prompt_async"), seg.index("return true;"))

    def test_直接发那一路也不静默(self):
        seg = fn("hsSend")
        # 2026-08-25 起是 .then(看返回值).catch(兜异常):hsDispatch 自己吞掉 POST 的
        # 异常并回 false,只挂 catch 的话失败时什么都不做,人打的那句话就没了。
        self.assertIn("hsDispatch(v, text, atts).then(", seg)
        self.assertIn(".catch((e) => {", seg)
        self.assertIn("hsRestoreUnsent(v, text, atts)", seg, "失败要把话放回输入框")


if __name__ == "__main__":
    unittest.main()
