"""回合之间也读管道:自续轮当场作为独立一轮发布,不再躺到用户下一次发话。

常驻进程在两个回合之间并不安静:子代理交回时 harness 注入一条通知,引擎就着它自己续一轮。
此前间隙里没人读 stdout,那一轮的帧一直等到用户下一次发话才在读循环开头被消费——界面上是
「刷新才出来」;更糟的是它们与用户那一轮连成一串,首段不能追认(里面可能有用户自己的话),
于是自续轮正文重复落进用户那一轮,前端又按执行返回的路由把用户的回答带进返回卡
(2026-09-08 用户实见:正文重复、回复跑到自己的提问上面)。

现在回合收尾后起一条空闲读(cli_turn._persist_idle):见到通知或 init 就开一个自己的 ctx,
以 msg_x_ 为 id、task-notification 为来路独立发布并落盘;用户发话时回合读先接管管道,
空闲读正收着一轮就等它收完再让路,管道里于是不再有残帧。
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(os.path.dirname(__file__), "..", "..", "frontend"),
            "7999", "7998", os.path.join(os.path.dirname(__file__), "..", "..")]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, ev):
        self.events.append(ev)

    def of(self, kind):
        return [e.get("properties") or {} for e in self.events if e.get("type") == kind]

    def wait(self, pred, timeout=3.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if pred():
                return True
            time.sleep(0.02)
        return pred()


class Sink(io.StringIO):
    """引擎的 stdin。记下每一帧写入的时刻:接管顺序要靠它证明。"""
    def __init__(self):
        super().__init__()
        self.frames = []

    def write(self, s):
        self.frames.append((time.time(), s))
        return super().write(s)

    def close(self):
        pass


class Pipe:
    """假引擎:stdout 是一条真管道,测试往里喂帧,泵线程在另一头读。"""
    def __init__(self):
        r, w = os.pipe()
        self.stdout = os.fdopen(r, "r", encoding="utf-8")
        self._w = os.fdopen(w, "w", encoding="utf-8")
        self.stdin = Sink()
        self.stderr = io.StringIO("")
        self.returncode = None
        self.pid = 4242

    def feed(self, *frames):
        for f in frames:
            self._w.write(json.dumps(f, ensure_ascii=False) + "\n")
        self._w.flush()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9
        try:
            self._w.close()
        except Exception:
            pass


def _init():
    return {"type": "system", "subtype": "init", "session_id": "eng-1", "uuid": "u-init"}


def _notif(task="agent:t1"):
    return {"type": "system", "subtype": "task_notification", "task_id": task,
            "status": "completed", "summary": 'Agent "写排序" completed'}


def _text(idx, s):
    return {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": idx,
        "delta": {"type": "text_delta", "text": s}}}


def _result(ms, origin=None):
    return {"type": "result", "subtype": "success", "duration_ms": ms,
            "usage": {"input_tokens": 1, "output_tokens": 1}, "origin": origin}


def _self_result(ms):
    return _result(ms, {"kind": "task-notification"})


# A turn normally finishes in milliseconds; this bound only has to outlast a loaded
# Windows CI worker (one turn took over 3 s there in run 37659906654).
TURN_JOIN_S = 15


class IdleAside(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.sid = self.ad.create_session()["id"]
        self.proc = Pipe()
        self.cmd, self.env = ["claude"], {}
        key = (self.ad._persist_key(self.cmd), self.ad.persist_env_key(self.env))
        self.ep = ea._EngineProc(self.proc, key)
        ea._PERSIST[self.sid] = self.ep
        self.n = 0

    def tearDown(self):
        ea._PERSIST.pop(self.sid, None)
        self.proc.kill()
        time.sleep(0.05)
        self.tmp.cleanup()

    # ---- 夹具 ----

    def _ctx(self, text="hi"):
        self.n += 1
        now = int(time.time() * 1000)
        return {"sid": self.sid, "msg_id": f"msg_a_{self.n}", "parts": {}, "order": [],
                "last_tool": None, "t0": now, "saved": [], "seq": self.ad._seq_next(self.sid),
                "source": self.ad.SRC_USER, "peer_request_id": "", "prev": None,
                "text": text, "opts": {}}

    def _turn_async(self, ctx):
        """在线程里跑一回合(接管管道、写用户帧、读到 result 为止),然后照 _run 收尾。"""
        out = {}

        def go():
            out["abnormal"] = self.ad._turn_persist(ctx, self.cmd, self.env,
                                                    self.ad.build_user_frame(ctx["text"], []))
            self.ad._finish_turn(self.sid, ctx, out["abnormal"])
            self.ad._persist_idle_after(ctx)
        th = threading.Thread(target=go, daemon=True)
        th.start()
        return th, out

    def _turn(self, ctx, frames):
        n = len(self.proc.stdin.frames)
        th, out = self._turn_async(ctx)
        # 等这一轮自己的用户帧写出去再喂回应:写出去意味着回合读已经接管管道,
        # 早喂的话帧会被空闲读当成引擎自己起的一轮收走。
        self.assertTrue(self.bus.wait(lambda: len(self.proc.stdin.frames) > n), "用户帧没写出去")
        self.proc.feed(*frames)
        th.join(TURN_JOIN_S)
        self.assertFalse(th.is_alive(), "回合没收尾")
        return out["abnormal"]

    def _texts(self, ctx):
        return "".join(p.get("text", "") for p in ctx["parts"].values() if p["type"] == "text")

    def _stored(self):
        return self.ad.messages(self.sid)

    def _aside_msgs(self):
        return [m for m in self._stored() if (m.get("info") or {}).get("aside")]

    def _seen(self, text):
        """这段字有没有经事件流出去(流式增量或整块快照,哪种都算)。"""
        return (any(text in str(p.get("delta") or "") for p in self.bus.of("message.part.delta"))
                or any(text in str((p.get("part") or {}).get("text") or "")
                       for p in self.bus.of("message.part.updated")))

    # ---- 用例 ----

    def test_间隙里的自续轮独立成轮并落盘(self):
        c1 = self._ctx()
        self.assertIsNone(self._turn(c1, [_init(), _text(0, "第一轮"), _result(100)]))
        self.assertEqual(self._texts(c1), "第一轮")
        # 回合结束、没人发话,引擎自己续了一轮
        self.proc.feed(_notif(), _init(), _text(0, "看了子代理的报告"), _self_result(3287))
        self.assertTrue(self.bus.wait(lambda: bool(self._aside_msgs())), "自续轮没落盘")
        stored = self._stored()
        aside = self._aside_msgs()[0]
        info = aside["info"]
        self.assertTrue(info["id"].startswith("msg_x_"), info["id"])
        self.assertEqual(info["source"], self.ad.SRC_ASIDE)
        self.assertEqual(info["aside"]["presentation"], "continuation")
        self.assertEqual(info["aside"]["ms"], 3287)
        self.assertEqual("".join(p.get("text", "") for p in aside["parts"]), "看了子代理的报告")
        # 返回卡单独一条 tool 消息,排在正文之前,正文经 return_id 指回它——与记录投影同形
        cards = [m for m in stored if (m.get("info") or {}).get("role") == "tool"]
        self.assertEqual(len(cards), 1)
        card = cards[0]["parts"][0]
        self.assertEqual(card["type"], "execution-result")
        self.assertEqual(info["aside"]["return_id"], card["id"])
        self.assertLess(stored.index(cards[0]), stored.index(aside))
        self.assertFalse(any(p["type"] == "execution-result" for p in aside["parts"]))
        # 实时那一路:正文的块挂在 msg_x_ 那条消息下,状态帧按它自己的序号走 running → ready
        mids = {p.get("messageID") for p in self.bus.of("message.part.delta")
                if "报告" in str(p.get("delta") or "")}
        mids |= {p["part"]["messageID"] for p in self.bus.of("message.part.updated")
                 if p["part"].get("type") == "text" and "报告" in p["part"].get("text", "")}
        self.assertEqual(mids, {info["id"]})
        def states():
            return [(s["state"], s["generation"]) for s in self.bus.of("session.state")
                    if s["generation"] == info["seq"]]
        self.assertTrue(self.bus.wait(lambda: (self.ad.TURN_READY, info["seq"]) in states()))
        self.assertEqual(states(), [(self.ad.TURN_RUNNING, info["seq"]), (self.ad.TURN_READY, info["seq"])])
        # 用户接着发话:管道里没有残帧,这一轮只有自己的内容
        c2 = self._ctx("再来")
        self.assertIsNone(self._turn(c2, [_init(), _text(0, "第二轮"), _result(200)]))
        self.assertEqual(self._texts(c2), "第二轮")
        self.assertFalse(any(p["type"] == "execution-result" for p in c2["parts"].values()))
        self.assertEqual(c2["ms"], 200)

    def test_用户发话要等正在收的自续轮收完(self):
        c1 = self._ctx()
        self._turn(c1, [_init(), _text(0, "第一轮"), _result(100)])
        self.proc.feed(_notif(), _init(), _text(0, "还在读报告"))
        self.assertTrue(self.bus.wait(lambda: self._seen("还在读报告")))
        n_before = len(self.proc.stdin.frames)
        c2 = self._ctx("插一句")
        th, out = self._turn_async(c2)
        time.sleep(0.8)
        self.assertEqual(len(self.proc.stdin.frames), n_before, "自续轮没收完就把用户帧写出去了")
        self.assertTrue(th.is_alive())
        # 自续轮收场,回合读才接管、写帧
        self.proc.feed(_self_result(500))
        self.assertTrue(self.bus.wait(lambda: len(self.proc.stdin.frames) > n_before), "接管没发生")
        self.proc.feed(_init(), _text(0, "给用户的回答"), _result(300))
        th.join(TURN_JOIN_S)
        self.assertFalse(th.is_alive())
        self.assertEqual(self._texts(c2), "给用户的回答")
        # 落盘顺序:返回卡、自续轮正文、用户这一轮
        roles = [((m.get("info") or {}).get("role"), bool((m.get("info") or {}).get("aside")))
                 for m in self._stored()]
        self.assertEqual(roles[-4:], [("assistant", False), ("tool", False),
                                      ("assistant", True), ("assistant", False)])
        aside = self._aside_msgs()[0]
        self.assertEqual("".join(p.get("text", "") for p in aside["parts"]), "还在读报告")

    def test_通知之后引擎没续轮_只留返回卡(self):
        self.ad.ASIDE_OPEN_GRACE = 0.3
        c1 = self._ctx()
        self._turn(c1, [_init(), _text(0, "第一轮"), _result(100)])
        self.proc.feed(_notif())
        self.assertTrue(self.bus.wait(
            lambda: any((m.get("info") or {}).get("role") == "tool" for m in self._stored()),
            timeout=2.0), "返回卡没落盘")
        time.sleep(0.2)
        self.assertEqual(self._aside_msgs(), [])       # 没有正文就没有那条 aside 消息
        self.assertFalse(self.ep.busy)
        # 之后发话照常
        c2 = self._ctx("再来")
        self._turn(c2, [_init(), _text(0, "第二轮"), _result(200)])
        self.assertEqual(self._texts(c2), "第二轮")

    def test_没开场的当场给用户让路(self):
        c1 = self._ctx()
        self._turn(c1, [_init(), _text(0, "第一轮"), _result(100)])
        self.proc.feed(_notif())
        self.assertTrue(self.bus.wait(lambda: any(
            p["part"].get("type") == "execution-result" for p in self.bus.of("message.part.updated"))))
        c2 = self._ctx("马上问")
        th, out = self._turn_async(c2)
        self.assertTrue(self.bus.wait(lambda: len(self.proc.stdin.frames) >= 2, timeout=1.5),
                        "只有一张返回卡时不该让用户等")
        self.proc.feed(_init(), _text(0, "答"), _result(50))
        th.join(TURN_JOIN_S)
        self.assertEqual(self._texts(c2), "答")
        self.assertFalse(any(p["type"] == "execution-result" for p in c2["parts"].values()))
        self.assertTrue(any((m.get("info") or {}).get("role") == "tool" for m in self._stored()))

    def test_进程没了空闲读也收场(self):
        c1 = self._ctx()
        self._turn(c1, [_init(), _text(0, "第一轮"), _result(100)])
        self.proc.feed(_notif(), _init(), _text(0, "读到一半"))
        self.assertTrue(self.bus.wait(lambda: self._seen("读到一半")))
        self.proc.kill()
        self.assertTrue(self.bus.wait(lambda: bool(self._aside_msgs())), "进程死了自续轮没收尾")
        self.assertTrue(self._aside_msgs()[0]["info"].get("local_only"))
        self.assertTrue(self.bus.wait(lambda: not self.ep.reader_lock.locked()))


if __name__ == "__main__":
    unittest.main()
