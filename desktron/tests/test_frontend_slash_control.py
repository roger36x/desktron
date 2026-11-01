"""手机上打断会话的入口是命令:/interrupt 与 /unsend。

桌面这两件事在 Esc 上(hsEscape → hsInterrupt),窄屏没有 Esc,也没地方再摆一枚钮。
输入框是手机上唯一始终在视线里的控件,所以入口放在它里面。

**这两条命令成立的前提是「不排队」。** 它们要在回合正跑的时候用,而那恰好是普通消息
被塞进队列的时刻——排了队的打断要等回合结束才发出去,那就完全没有意义了。hsSend 里
本地命令的分支排在队列分支之前,所以打了就立刻执行。这条顺序本不是为打断准备的,
但它对;下面第一条测试钉的就是它,别在中间插东西。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
COMMANDS = open(os.path.join(ROOT, "desktron/lib/engine_adapters/command_manifest.py"),
                encoding="utf-8").read()


def fn(name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), CHAT, re.S)
    assert m, "%s 不见了" % name
    return m.group(0)


class RunsImmediatelyNotQueued(unittest.TestCase):
    def test_本地命令排在队列之前(self):
        """排在后面的话,回合正跑时 /interrupt 会被当成普通消息排队,等回合结束才发。"""
        seg = fn("hsSend")
        i_cmd = seg.index("hsRunLocalSlash(v, localCmd.cmd, localCmd.args)")
        i_queue = seg.index("(v.queue = v.queue || []).push(")
        self.assertLess(i_cmd, i_queue, "本地命令的分支跑到队列后面去了")

    def test_也排在提问面板之前(self):
        """面板挂着时打断同样该立刻生效,不该被当成对那个问题的回答。"""
        seg = fn("hsSend")
        self.assertLess(seg.index("hsRunLocalSlash"), seg.index("if (v.ask && !v.ask.permission) {"))

    def test_PC回车发送且不看忙闲(self):
        """PC 的普通 Enter 直接发送，中间没有 busy 拦截。"""
        i = CHAT.index("const enterKey =")
        seg = CHAT[i:i + 1000]
        self.assertIn("const sendKey = enterKey && !phoneInput && !ev.shiftKey", seg)
        self.assertIn("if (sendKey)", seg)
        self.assertIn("hsSend(v)", seg)
        self.assertNotIn("v.busy", seg)

    def test_手机回车与PC组合回车留给换行(self):
        self.assertIn("function hsPhoneInput()", CHAT)
        i = CHAT.index("const enterKey =")
        seg = CHAT[i:i + 1000]
        self.assertIn("const newlineKey = enterKey && (phoneInput || ev.shiftKey)", seg)
        self.assertIn("v.slashEl && !sendKey && !newlineKey", seg)

    def test_挂着附件也拦不住控制命令(self):
        """普通本地命令在附件在场时不认(那多半是想把命令当正文发),但打断这件事
        与挂没挂图无关。"""
        seg = fn("hsSend")
        self.assertIn("localCmd.cmd.ctl || !atts.length", seg)


class TwoCommands(unittest.TestCase):
    def test_两条都在后端manifest里(self):
        self.assertIn("('/interrupt'", COMMANDS)
        self.assertIn("('/unsend'", COMMANDS)
        self.assertIn('"/interrupt": (v) => hsInterrupt(v)', CHAT)
        self.assertIn('"/unsend": (v) => hsUnsend(v)', CHAT)

    def test_打断复用桌面那一条路(self):
        """与 Esc 走同一个函数:分档(中断还是撤回)、乐观 UI、丢帧窗口都在那儿,
        另写一份迟早漂移。"""
        self.assertIn('"/interrupt": (v) => hsInterrupt(v)', CHAT)

    def test_撤回办不到就明说_不做一半(self):
        """后端那侧末尾不是真人发言就不摘那条消息(_pop_last_user)。硬撤的话记录里
        留一份、输入框里又有一份,下一次对账就重了。"""
        seg = fn("hsUnsend")
        self.assertIn("!t.parts.size", seg, "没有判「这一轮产出没有」")
        self.assertIn("收不回来", seg)
        self.assertIn("/interrupt", seg, "该给出还能做什么")
        i_guard = seg.index("if (!fresh)")
        i_do = seg.index("return hsInterrupt(v)")
        self.assertLess(i_guard, i_do, "守卫要挡在动手之前")

    def test_空闲时不列出来(self):
        """点了什么也不会发生的命令不该占补全的位置。"""
        seg = fn("hsSlashAll")
        self.assertIn("if (c.ctl) return !!v.busy;", seg)


class ShadowingIsNoticed(unittest.TestCase):
    def test_同名时本地赢但要出声(self):
        """claude 上报的那份就是装了的 skill 清单,会长。将来谁装一个叫 unsend 的
        skill,它会从补全里整个消失,而且没有任何提示。"""
        seg = fn("hsSlashAll")
        self.assertIn("own.has(c.n)", seg)
        self.assertIn("deskLog", seg, "撞名要留痕")
        self.assertIn("_slashShadowSaid", seg, "每段会话只说一次,别刷屏")


if __name__ == "__main__":
    unittest.main()
