"""回合跑到一半换模型:要在下一轮真的生效,且钮上不再倒回旧模型。

2026-09-04 用户实见:一段 claude 会话,菜单圆点在 Opus 5,输入框上的档位钮与实跑
都是 Fable 5.1。发生在「AI 正在回复的过程中切换」这一刻。查下来是两件事叠在一起,
两件都只在「换的时候有回合在跑」时出现,平时怎么试都对。

一、排到队里的那一轮用的是按下发送那一刻的档位快照。
    ui_contract 在收到 HTTP 请求时就把 session_options 读出来交给 prompt_async,而
    prompt_async 起的线程要先在会话回合锁上排队(_run_serial)。人正是在这段排队时间里
    改档位的——改完菜单已经是新模型,前面排着的几轮却仍按旧模型跑,看起来就是「切换不
    生效」。队列里没有「每条消息各自的档位」这回事:档位是会话的当前属性,以开跑那一刻
    的为准。改法是多给一个重取口(refresh_opts),在拿到回合锁之后再读一次。

    另有一层不是缺陷、但要说清:模型是进程启动参数(build_cmd 的 --model),常驻进程按
    选项指纹换代(_persist_key)。正在跑的那一轮改不了,同一进程里的自续轮与子代理也改
    不了,只能等下一个回合换代。所以「下一轮生效」是这件事能做到的上限。

二、正在跑的旧回合会把刚清掉的自报模型写回去。
    换模型时 update_session_options 清掉 index[sid]["model"],为的是让档位钮先显示人选
    的那个,等引擎在新设置下自报了再换成真实版本。但旧回合还在吐 message_start,每条都
    带着旧模型,几毫秒内就把刚清掉的值写了回来。回合结束后这个值也不会自己消失,换个窗口
    或刷新一次,看到的就是截图里那一幕。改法是划一条时刻线:比这次改动更早开始的回合再
    报模型也不作数(model_cut 与 set_session_model 的 since)。

排除过的解释:
  · 不是前端 modelPick 那道保险失效——它在做出选择的那个窗口里确实挡住了,截图里
    显示成旧模型是因为刷新后 modelPick(纯内存)没了,退回读后端的自报值;
  · 不是选项没存上——事后查 sessions.json,那一段会话的 opts.model 一直是 opus;
  · 不是常驻进程指纹漏了 --model——_persist_key 只摘 --resume 与
    --append-system-prompt,模型在指纹里,下一轮确实会换代。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv

TURN = open(os.path.join(ROOT, "desktron", "lib", "engine_adapters", "cli_turn.py"),
            encoding="utf-8").read()
CONTRACT = open(os.path.join(ROOT, "desktron", "lib", "ui_contract.py"),
                encoding="utf-8").read()


class _Bus:
    def publish(self, ev):
        pass


def _start(model):
    return {"type": "stream_event", "event": {
        "type": "message_start", "message": {"model": model}}}


class 排队的回合按开跑那一刻的档位(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, _Bus())
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_拿到回合锁之后重取一次(self):
        """真正跑的是重取回来的那份,不是发起时传进来的快照。"""
        seen = {}
        self.ad._run = lambda sid, text, opts, atts: seen.update(opts or {})
        self.ad._run_serial(self.sid, "hi", {"model": "fable"}, [],
                            refresh_opts=lambda: {"model": "opus"})
        self.assertEqual(seen.get("model"), "opus",
                         "排队期间人改成了 opus,这一轮就该按 opus 跑")

    def test_没给重取口时沿用快照(self):
        """内部调用方传的是刻意指定的档位,不该被会话当前值顶掉。"""
        seen = {}
        self.ad._run = lambda sid, text, opts, atts: seen.update(opts or {})
        self.ad._run_serial(self.sid, "hi", {"model": "fable"}, [])
        self.assertEqual(seen.get("model"), "fable")

    def test_重取失败不拖垮这一轮(self):
        """重取只是让档位跟上,读不到就用快照,不能让一条消息因此发不出去。"""
        seen = {}
        self.ad._run = lambda sid, text, opts, atts: seen.update(opts or {})

        def boom():
            raise RuntimeError("配置读不到")

        self.ad._run_serial(self.sid, "hi", {"model": "fable"}, [], refresh_opts=boom)
        self.assertEqual(seen.get("model"), "fable")

    def test_内部排队栅栏异常不能被当成普通重取失败吞掉(self):
        seen=[]
        self.ad._run=lambda *_args:seen.append(True)
        class Fence(RuntimeError):
            abort_queued_turn=True
        with self.assertRaises(Fence):
            self.ad._run_serial(self.sid,"internal",{},[],refresh_opts=lambda:(_ for _ in ()).throw(Fence()))
        self.assertFalse(seen)

    def test_发送入口把重取口交下去(self):
        """光有形参没用,得有人传。会话档位的权威读法是 session_options(sid, 引擎级默认),
        少了后半截,没存过档位的会话会读成空,引擎级默认整个丢掉。"""
        self.assertIn("refresh_opts = lambda", CONTRACT)
        refresh = CONTRACT.split("refresh_opts = lambda", 1)[1][:700]
        self.assertIn("ad.session_options(", refresh)
        self.assertIn("engine_opts", refresh)
        prompt = CONTRACT.split("receipt = ad.prompt_async", 1)[1][:600]
        self.assertIn("refresh_opts=refresh_opts", prompt)

    def test_模型仍在常驻指纹里(self):
        """模型与系统正文都参与常驻身份；只有 resume ID 不影响复用。"""
        self.assertIn('drop_pairs = ("--resume",)', TURN)


class 换模型之后旧回合的自报不作数(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, _Bus())
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, t0):
        return {"sid": self.sid, "msg_id": "m1", "parts": {}, "order": [],
                "last_tool": None, "t0": t0}

    def test_旧回合写不回来(self):
        """截图里那一幕:菜单是 Opus 5,钮上却是 Fable 5.1。"""
        self.ad.handle_event(_start("claude-fable-5-1"), self._ctx(1000))
        self.ad.update_session_options(self.sid, {"model": "opus"})
        self.ad.handle_event(_start("claude-fable-5-1"), self._ctx(1000))
        self.assertEqual(self.ad.session_model(self.sid), "",
                         "这一轮比换模型早,它报的 fable 不作数")
        self.assertEqual(self.ad.config_label(self.sid), "claude")

    def test_换代之后的回合照记(self):
        """挡的是「比改动早」的回合,不是所有回合;不然换完模型钮就永远显示不出真实版本。"""
        self.ad.update_session_options(self.sid, {"model": "opus"})
        cut = self.ad.index[self.sid]["model_cut"]
        self.ad.handle_event(_start("claude-opus-5"), self._ctx(cut + 1))
        self.assertEqual(self.ad.session_model(self.sid), "claude-opus-5")

    def test_没换过模型时一切照旧(self):
        """时刻线只在换过模型的会话上存在,没换过的不受影响。"""
        self.ad.handle_event(_start("claude-opus-5"), self._ctx(1))
        self.assertEqual(self.ad.session_model(self.sid), "claude-opus-5")

    def test_不带回合时刻的调用不受影响(self):
        """since 缺省是「不知道这一轮什么时候开的」,此时照记——宁可显示旧值,
        也不要因为一个未知量把自报整个丢掉。"""
        self.ad.update_session_options(self.sid, {"model": "opus"})
        self.ad.set_session_model(self.sid, "claude-opus-5")
        self.assertEqual(self.ad.session_model(self.sid), "claude-opus-5")

    def test_引擎级兜底照更新(self):
        """挡的是这一段会话的记录。没有会话的场合(引擎页)仍要答得出话。"""
        self.ad.update_session_options(self.sid, {"model": "opus"})
        self.ad.handle_event(_start("claude-fable-5-1"), self._ctx(1000))
        self.assertEqual(self.ad.last_model, "claude-fable-5-1")


if __name__ == "__main__":
    unittest.main()
