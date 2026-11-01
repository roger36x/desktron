"""引擎自己合成的那句报错不进正文,改挂输入框上方的系统信息条。

claude 在重试耗尽之后会替模型写一条 assistant 条目交代原因,记录里认得出来:
`message.model` 写作 `<synthetic>`,报错那类还带 `isApiErrorMessage`。此前它照正文渲染,
于是回复的末尾冒出一句「API Error: Unable to connect to API (…CERTIFICATE…)」——
读起来像是模型在回答里报证书错误(2026-08-14 实见)。

这类信息讲的是引擎此刻通不通,与某一轮的产出无关,该走窄条那条路(session.environment,
见 app-chat.js 的 hsEnvironment),和 codex 启动态用的是同一条。恢复之后自己撤掉:
报错之后又跑出了真的产出,就说明已经通了。

样本照本机记录的形状抄(claude-code 2.1.226),不是照文档编的。
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv

CERT = "API Error: Unable to connect to API (UNKNOWN_CERTIFICATE_VERIFICATION_ERROR)"


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, e):
        self.events.append(e)

    def envs(self):
        return [e.get("properties") or {} for e in self.events
                if e.get("type") == "session.environment"]


def _at(sec):
    return "2026-08-14T12:%02d:%02d.000Z" % (sec // 60, sec % 60)


def _user(uuid, text, sec):
    return {"type": "user", "uuid": uuid, "timestamp": _at(sec),
            "message": {"role": "user", "content": text}}


def _text(uuid, sec, txt):
    return {"type": "assistant", "uuid": uuid, "timestamp": _at(sec),
            "message": {"role": "assistant", "content": [{"type": "text", "text": txt}]}}


def _synthetic(uuid, sec, txt, api_error=True):
    """CLI 替模型写的那条。model 是 <synthetic>,usage 全零,stop_reason 是 stop_sequence。"""
    return {"type": "assistant", "uuid": uuid, "timestamp": _at(sec),
            "isApiErrorMessage": api_error,
            "message": {"role": "assistant", "model": "<synthetic>",
                        "stop_reason": "stop_sequence",
                        "content": [{"type": "text", "text": txt}]}}


class SyntheticNotice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.sid = self.ad.create_session()["id"]
        self.path = os.path.join(self.tmp.name, "transcript.jsonl")
        self.ad._transcript_path = lambda esid: self.path

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rows):
        with open(self.path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _group(self, rows):
        self._write(rows)
        sink = {}
        return self.ad._group_transcript(self.path, self.sid, "esid-1", sink=sink), sink

    def _texts(self, msgs):
        return ["".join(q.get("text") or "" for q in m.get("parts") or []
                        if q.get("type") == "text") for m in msgs]

    # ---- 正文 ----

    def test_报错那条不进正文(self):
        msgs, sink = self._group([
            _user("u1", "改一下目录树的展开", 0),
            _text("a1", 10, "找到了,是 CSS 覆盖掉了 hidden。"),
            _synthetic("a2", 20, CERT),
        ])
        self.assertEqual(self._texts(msgs), ["改一下目录树的展开", "找到了,是 CSS 覆盖掉了 hidden。"])
        self.assertEqual(sink["notice"], CERT)

    def test_没让模型说话那条也不进正文(self):
        """同样是 <synthetic>,但不是报错(isApiErrorMessage 为假),摘掉即可,不必声张。"""
        msgs, sink = self._group([
            _user("u1", "跑一下测试", 0),
            _synthetic("a1", 10, "No response requested.", api_error=False),
        ])
        self.assertEqual(self._texts(msgs), ["跑一下测试"])
        self.assertFalse(sink.get("notice"))

    def test_后面又有产出就不再挂着(self):
        msgs, sink = self._group([
            _user("u1", "改一下目录树的展开", 0),
            _synthetic("a1", 10, CERT),
            _user("u2", "再试一次", 20),
            _text("a2", 30, "这次通了。"),
        ])
        self.assertEqual(self._texts(msgs)[-1], "这次通了。")
        self.assertEqual(sink["notice"], "")

    # ---- 窄条 ----

    def test_导入时把报错送上窄条并跨刷新留着(self):
        self._write([
            _user("u1", "改一下目录树的展开", 0),
            _text("a1", 10, "找到了。"),
            _synthetic("a2", 20, CERT),
        ])
        self.ad.import_session("esid-1", sid=self.sid)
        got = self.bus.envs()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["state"], "error")
        self.assertEqual(got[0]["label"], CERT)
        self.assertEqual(got[0]["sessionID"], self.sid)
        # 刷新之后经 /config 再拿一遍,还在
        self.assertEqual((self.ad.session_environment(self.sid) or {}).get("label"), CERT)

    def test_同一条不重复发(self):
        rows = [_user("u1", "改一下", 0), _synthetic("a1", 20, CERT)]
        self._write(rows)
        self.ad.import_session("esid-1", sid=self.sid)
        self.ad.import_session("esid-1", sid=self.sid)
        self.assertEqual(len(self.bus.envs()), 1)

    def test_恢复之后窄条撤掉(self):
        self._write([_user("u1", "改一下", 0), _synthetic("a1", 20, CERT)])
        self.ad.import_session("esid-1", sid=self.sid)
        self._write([_user("u1", "改一下", 0), _synthetic("a1", 20, CERT),
                     _user("u2", "再试一次", 30), _text("a2", 40, "这次通了。")])
        self.ad.import_session("esid-1", sid=self.sid)
        got = self.bus.envs()
        self.assertEqual([g["state"] for g in got], ["error", "ready"])
        self.assertIsNone(self.ad.session_environment(self.sid))


if __name__ == "__main__":
    unittest.main()
