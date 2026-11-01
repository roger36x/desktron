"""一条飞书消息从进到出的完整链路(docs/agent-hosting-design.md「架构」)。

只 mock 两处外部世界:飞书 REST(larkapi)与引擎进程(runner.run)。其余——路由、
上下文组装、红线闸、会话记录——都跑真的,因为要验的正是它们串起来之后的行为。

最要紧的一条在最后:模型从别处读到红线内容并转述时,那段内容不进飞书。
"""
import os
import json
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from larkbot import handler, store  # noqa: E402

BOT_OPEN_ID = "ou_bot"
APP = "cli_app1"


class E2E(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, ".memory", "_sealed"), exist_ok=True)
        _, err = store.save(self.d, "sre-bot", {
            "name": "SRE 助手", "lark_app_id": APP, "engine": "claude",
            "context": {"role": "你是 SRE 助手", "memory": False, "knowledge": False, "history": 5},
            "outbound": {"card": False},
        })
        self.assertEqual(err, "")
        handler._seen.clear()
        self.sent = []

        def _send_text(app_id, chat_id, text, domain=""):
            self.sent.append(("text", chat_id, text))
            return {"code": 0, "data": {"message_id": "om_out"}}

        def _reply_text(app_id, mid, text, domain="", in_thread=False):
            self.sent.append(("reply", mid, text))
            return {"code": 0, "data": {"message_id": "om_out"}}

        self.p = [
            mock.patch("larkbot.larkapi.send_text", _send_text),
            mock.patch("larkbot.larkapi.reply_text", _reply_text),
            mock.patch("larkbot.larkapi.bot_info", return_value=({"open_id": BOT_OPEN_ID, "app_id": APP}, "")),
            mock.patch("larkbot.larkapi.chat_mode", return_value="group"),
            mock.patch("larkbot.larkapi.app_secret", return_value="fake-secret"),
        ]
        for x in self.p:
            x.start()
        self.addCleanup(lambda: [x.stop() for x in self.p])

    def _event(self, text, chat_type="p2p", mentions=None, eid="e1", **msg_over):
        m = {"chat_type": chat_type, "chat_id": "oc_1", "message_id": "om_in",
             "message_type": "text", "content": '{"text":"%s"}' % text,
             "mentions": mentions or []}
        m.update(msg_over)
        return {"event_id": eid, "app_id": APP, "message": m,
                "sender": {"sender_id": {"open_id": "ou_human"}}}

    def _run(self, out="好的,我看一下。", err=""):
        return mock.patch("larkbot.runner.run", return_value=(out, err))

    # ---- 主链 ----

    def test_timing_separates_routing_and_engine_without_message_content(self):
        clock = [0.0]
        logs = []

        def chat(*args):
            clock[0] += 63
            return "group"

        def run(*args, **kwargs):
            clock[0] += 8
            return "你好", ""

        with mock.patch.object(handler.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch("larkbot.larkapi.chat_mode", side_effect=chat), \
             mock.patch("larkbot.runner.run", side_effect=run):
            result = handler.handle(self.d, self._event(
                "private-message", chat_type="group",
                mentions=[{"id": {"open_id": BOT_OPEN_ID}}]),
                log=lambda tag, text: logs.append(text))
        self.assertTrue(result["ok"])
        line = next(x for x in logs if x.startswith("timing "))
        timing = json.loads(line.split(" ms=", 1)[1])
        self.assertEqual(timing["chat_lookup"], 63000)
        self.assertEqual(timing["engine"], 8000)
        self.assertEqual(timing["total"], 71000)
        self.assertIn("send", timing)
        self.assertIn("context", timing)
        self.assertNotIn("private-message", line)
        self.assertNotIn("fake-secret", line)

    def test_timing_records_failed_phase(self):
        logs = []
        with mock.patch("larkbot.larkapi.bot_info", side_effect=RuntimeError("lookup failed")):
            result = handler.handle(self.d, self._event("private-message"),
                                    log=lambda tag, text: logs.append(text))
        self.assertFalse(result["ok"])
        line = next(x for x in logs if x.startswith("timing "))
        timing = json.loads(line.split(" ms=", 1)[1])
        self.assertIn("bot_lookup", timing)
        self.assertNotIn("engine", timing)
        self.assertGreaterEqual(timing["total"], timing["bot_lookup"])

    def test_timing_logging_failure_does_not_break_reply(self):
        with self._run():
            result = handler.handle(self.d, self._event("在吗"),
                                    log=mock.Mock(side_effect=OSError("log unavailable")))
        self.assertTrue(result["ok"])

    def test_私聊一句话_引擎的回答原样发出(self):
        with self._run("磁盘是 82%,还行。"):
            r = handler.handle(self.d, self._event("磁盘怎么样"))
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.sent[-1][2], "磁盘是 82%,还行。")

    def test_角色与当前问题都进了_prompt(self):
        with mock.patch("larkbot.runner.run", return_value=("嗯", "")) as run:
            handler.handle(self.d, self._event("磁盘怎么样"))
        prompt = run.call_args.args[1]
        self.assertIn("你是 SRE 助手", prompt)
        self.assertIn("磁盘怎么样", prompt)

    def test_群里没_at_不回(self):
        with self._run():
            r = handler.handle(self.d, self._event("你们看这个", chat_type="group"))
        self.assertEqual(r.get("skipped"), "no-mention")
        self.assertEqual(self.sent, [])

    def test_群里_at_了就回(self):
        with self._run():
            r = handler.handle(self.d, self._event(
                "@_user_1 帮我看看", chat_type="group",
                mentions=[{"id": {"open_id": BOT_OPEN_ID}}]))
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(self.sent), 1)

    def test_重复推送只回一次(self):
        with self._run():
            handler.handle(self.d, self._event("在吗", eid="same"))
            handler.handle(self.d, self._event("在吗", eid="same"))
        self.assertEqual(len(self.sent), 1)

    def test_两轮之后历史带进第三轮(self):
        with self._run("第一次回答"):
            handler.handle(self.d, self._event("第一句", eid="e1"))
        with mock.patch("larkbot.runner.run", return_value=("第二次回答", "")) as run:
            handler.handle(self.d, self._event("第二句", eid="e2"))
        prompt = run.call_args.args[1]
        self.assertIn("第一句", prompt)
        self.assertIn("第一次回答", prompt)
        self.assertIn("第二句", prompt)

    def test_不同话题的历史不串(self):
        ev1 = self._event("甲话题的事", chat_type="group", eid="e1",
                          mentions=[{"id": {"open_id": BOT_OPEN_ID}}],
                          root_id="om_a", thread_id="t1")
        ev2 = self._event("乙话题的事", chat_type="group", eid="e2",
                          mentions=[{"id": {"open_id": BOT_OPEN_ID}}],
                          root_id="om_b", thread_id="t2")
        with self._run("甲的回答"):
            handler.handle(self.d, ev1)
        with mock.patch("larkbot.runner.run", return_value=("乙的回答", "")) as run:
            handler.handle(self.d, ev2)
        self.assertNotIn("甲话题的事", run.call_args.args[1])

    def test_引擎跑不动时如实说不装作没事(self):
        with mock.patch("larkbot.runner.run", return_value=("", "claude 的命令不在 PATH 上")):
            r = handler.handle(self.d, self._event("在吗"))
        self.assertFalse(r["ok"])
        self.assertIn("PATH", self.sent[-1][2])

    def test_超时时把已生成的部分发出来并带上说明(self):
        with mock.patch("larkbot.runner.run", return_value=("查到一半:磁盘 82%", "超时(300s),已终止")):
            r = handler.handle(self.d, self._event("查一下"))
        self.assertIn("磁盘 82%", self.sent[-1][2])
        self.assertIn("超时", self.sent[-1][2])

    # ---- 红线 ----

    def test_模型转述封存内容时不出站(self):
        """这是第二道闸要防的:第一道(不取)管住了上下文,但模型可能从代码、文档、
        工具返回、别人贴的消息里读到红线内容。"""
        secret_line = "这一整行是封存里的内容,长度足够被滑窗指纹认出来,不该出现在飞书上"
        with open(os.path.join(self.d, ".memory", "_sealed", "x.md"), "w", encoding="utf-8") as f:
            f.write(secret_line + "\n")
        with self._run("我在文档里看到:" + secret_line):
            r = handler.handle(self.d, self._event("那个事怎么说的"))
        self.assertFalse(r["ok"])
        self.assertTrue(r["held"])
        self.assertNotIn("封存里的内容", self.sent[-1][2])
        self.assertIn("Desk", self.sent[-1][2])

    def test_被拦的内容原文留在本机(self):
        with open(os.path.join(self.d, ".memory", "_sealed", "x.md"), "w", encoding="utf-8") as f:
            f.write("这一整行是封存里的内容,长度足够被滑窗指纹认出来,不该出现在飞书上\n")
        with self._run("原话是这一整行是封存里的内容,长度足够被滑窗指纹认出来,不该出现在飞书上"):
            handler.handle(self.d, self._event("那个事怎么说的"))
        import json
        with open(store.held_path(self.d), encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertIn("封存里的内容", items[-1]["text"])

    def test_凭证不出站(self):
        with self._run("token 是 ghp_abcdefghijklmnopqrstuvwxyz0123"):
            r = handler.handle(self.d, self._event("token 多少"))
        self.assertFalse(r["ok"])
        self.assertNotIn("ghp_", self.sent[-1][2])

    def test_被拦的内容不进会话历史(self):
        """它没发出去,对面没看见。记进去下一轮模型会以为自己说过了。"""
        with self._run("ghp_abcdefghijklmnopqrstuvwxyz0123"):
            handler.handle(self.d, self._event("token", eid="e1"))
        turns = store.session(self.d, "sre-bot", "chat:oc_1")["turns"]
        self.assertEqual([t["role"] for t in turns], ["user"])

    def test_合规词默认只提示不拦(self):
        with self._run("那份数据含 PII,要走脱敏。"):
            r = handler.handle(self.d, self._event("数据在哪"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["level"], "warn")
        self.assertIn("PII", self.sent[-1][2])

    def test_strict_打开后合规词也拦(self):
        a = store.load(self.d, "sre-bot")
        a["outbound"]["strict"] = True
        store.save(self.d, "sre-bot", a)
        with self._run("那份数据含 PII。"):
            r = handler.handle(self.d, self._event("数据在哪"))
        self.assertFalse(r["ok"])
        self.assertNotIn("PII", self.sent[-1][2])

    # ---- 斜杠命令 ----

    def test_斜杠命令不惊动引擎(self):
        with mock.patch("larkbot.runner.run") as run:
            r = handler.handle(self.d, self._event("/status"))
        self.assertFalse(run.called)
        self.assertTrue(r.get("slash"))
        self.assertIn("SRE 助手", self.sent[-1][2])

    def test_clear_之后历史真的没了(self):
        with self._run("回答一"):
            handler.handle(self.d, self._event("第一句", eid="e1"))
        handler.handle(self.d, self._event("/clear", eid="e2"))
        with mock.patch("larkbot.runner.run", return_value=("回答二", "")) as run:
            handler.handle(self.d, self._event("第二句", eid="e3"))
        self.assertNotIn("第一句", run.call_args.args[1])

    def test_群里改配置立刻生效(self):
        handler.handle(self.d, self._event("/history 0", eid="e1"))
        with self._run("回答一"):
            handler.handle(self.d, self._event("第一句", eid="e2"))
        with mock.patch("larkbot.runner.run", return_value=("回答二", "")) as run:
            handler.handle(self.d, self._event("第二句", eid="e3"))
        self.assertNotIn("第一句", run.call_args.args[1])

    # ---- 边界 ----

    def test_没配凭证时不白跑引擎(self):
        """发不出消息的话,跑一轮引擎(几十秒到几分钟)最后仍然是同一个错。"""
        with mock.patch("larkbot.larkapi.app_secret", return_value=""), \
             mock.patch("larkbot.runner.run") as run:
            r = handler.handle(self.d, self._event("在吗"))
        self.assertFalse(run.called)
        self.assertEqual(r.get("skipped"), "no-secret")

    def test_没有_agent_时安静跳过(self):
        empty = tempfile.mkdtemp()
        r = handler.handle(empty, self._event("在吗"))
        self.assertEqual(r.get("skipped"), "no-agent")

    def test_停用的_agent_不接消息(self):
        a = store.load(self.d, "sre-bot")
        a["enabled"] = False
        store.save(self.d, "sre-bot", a)
        r = handler.handle(self.d, self._event("在吗"))
        self.assertEqual(r.get("skipped"), "no-agent")

    def test_不在允许名单里的人被挡(self):
        a = store.load(self.d, "sre-bot")
        a["allowed_users"] = ["ou_someone_else"]
        store.save(self.d, "sre-bot", a)
        r = handler.handle(self.d, self._event("在吗"))
        self.assertEqual(r.get("skipped"), "user-not-allowed")
        self.assertEqual(self.sent, [])

    def test_空消息不惊动引擎(self):
        with mock.patch("larkbot.runner.run") as run:
            r = handler.handle(self.d, self._event(""))
        self.assertFalse(run.called)
        self.assertEqual(r.get("skipped"), "empty-text")

    def test_任何异常都变成返回值不抛出去(self):
        """它跑在 HTTP 请求线程里。抛出去 sidecar 只看到 500,真正的原因就丢了。"""
        with mock.patch("larkbot.store.list_all", side_effect=RuntimeError("磁盘炸了")):
            r = handler.handle(self.d, self._event("在吗"))
        self.assertFalse(r["ok"])
        self.assertIn("磁盘炸了", r["err"])


if __name__ == "__main__":
    unittest.main()
