"""跨会话问询通过会话路由只派发一次，并把请求号交给公共回合。"""
from io import BytesIO
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import peer_mailbox
    import ui_contract
finally:
    sys.argv = _argv


class Adapter:
    name = "codex"

    def __init__(self, fail=False):
        self.calls = []
        self.peer_calls = []
        self.fail = fail
        self.peer = {"active": False, "revision": 0}

    def peer_state(self, _sid):
        return dict(self.peer)

    def session_options(self, _sid, _legacy):
        return {"model": "test"}

    def prompt_async(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError("thread unavailable")
        self.calls.append((args, kwargs))

    def peer_prompt_async(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError("thread unavailable")
        self.peer_calls.append((args, kwargs))


class Handler(ui_contract.UiContractMixin):
    def __init__(self, sid, body):
        raw = json.dumps(body).encode()
        self.path = f"/session/{sid}/prompt_async"
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = BytesIO(raw)
        self.code = None
        self.out = None

    def send_response(self, code):
        self.code = code

    def end_headers(self):
        pass

    def _send_json(self, obj, code=200):
        self.out, self.code = obj, code
        return obj


class PeerMessageRoute(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory()
        self.old = os.environ.get("DESKTRON_DATA_HOME")
        os.environ["DESKTRON_DATA_HOME"] = self.data.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop("DESKTRON_DATA_HOME", None)
        else:
            os.environ["DESKTRON_DATA_HOME"] = self.old
        self.data.cleanup()

    def _call(self, adapter, request_id, sid="target", peer_head="", extra=None, parts=None):
        body = {"parts": parts or [{"type": "text", "text": "question", "peer_head": peer_head}],
                "peer_request_id": request_id}
        body.update(extra or {})
        h = Handler(sid, body)
        with mock.patch.object(ui_contract, "_adapter", return_value=adapter), \
             mock.patch.object(ui_contract, "_engine_pref", return_value="codex"), \
             mock.patch.object(ui_contract, "_wb_state_read", return_value={}), \
             mock.patch.object(Handler, "_adapter_owning", return_value=adapter):
            h._adapter_route("POST")
        return h

    def test_valid_request_is_dispatched_once_with_request_id(self):
        row = peer_mailbox.create("source", "target")
        ad = Adapter()
        first = self._call(ad, row["id"])
        second = self._call(ad, row["id"])
        self.assertEqual(first.code, 204)
        self.assertEqual(second.code, 400)
        self.assertEqual(ad.calls, [])
        self.assertEqual(len(ad.peer_calls), 1)
        self.assertEqual(ad.peer_calls[0][1]["peer_request_id"], row["id"])

    def test_source_title_is_passed_separately_from_prompt(self):
        ad = Adapter()
        self.assertEqual(self._call(ad, "", peer_head="来源标题").code, 204)
        self.assertEqual(ad.calls[0][0][1], "question")
        self.assertEqual(ad.calls[0][1]["peer_head"], "来源标题")
        self.assertEqual(ad.peer_calls, [])

    def test_ordinary_prompt_is_accepted_while_peer_runs(self):
        ad = Adapter()
        ad.peer = {"active": True, "revision": 3, "requestID": "pm_" + "a" * 32,
                   "phase": "running", "source": "desktop · 来源会话"}
        accepted = self._call(ad, "")
        self.assertEqual(accepted.code, 204)
        self.assertEqual(len(ad.calls), 1)
        self.assertEqual(ad.calls[0][1]["confirmed_peer_cancel"], "")

    def test_request_for_another_target_is_rejected(self):
        row = peer_mailbox.create("source", "other")
        ad = Adapter()
        got = self._call(ad, row["id"])
        self.assertEqual(got.code, 400)
        self.assertEqual(ad.calls, [])

    def test_peer_request_rejects_attachments_and_engine_retargeting(self):
        for extra, parts in (({}, [{"type":"text", "text":"question"},
                                    {"type":"file", "path":"/tmp/x"}]),
                             ({"engine":"codex"}, None)):
            with self.subTest(extra=extra, parts=parts):
                row = peer_mailbox.create("source", "target")
                ad = Adapter()
                got = self._call(ad, row["id"], extra=extra, parts=parts)
                self.assertEqual(got.code, 400)
                self.assertEqual(ad.calls, [])
                self.assertEqual(ad.peer_calls, [])
                self.assertEqual(peer_mailbox.get(row["id"])["status"], "failed")

    def test_unknown_shepherd_engine_is_rejected_before_mailbox_accept(self):
        row = peer_mailbox.create("source", "target")
        ad = Adapter()
        ad.name = "shepherd"
        got = self._call(ad, row["id"], extra={"engine": "unknown-engine"})
        self.assertEqual(got.code, 400)
        state = peer_mailbox.get(row["id"])
        self.assertEqual(state["status"], "queued")
        self.assertNotIn("accepted_at", state)
        self.assertEqual(ad.peer_calls, [])

    def test_legacy_watcher_result_is_released_without_ordinary_prompt(self):
        row = peer_mailbox.create("source", "target")
        peer_mailbox.update(row["id"], "replied", reply="late")
        peer_mailbox.claim_result(row["id"], "delivering")
        text = (f"另一个 Desk 会话已完成问询 {row['id']}。\n\nlate\n\n"
                "——这是先前跨会话问询的迟到结果，不是用户的新指令。")
        self.assertTrue(ui_contract._release_legacy_peer_result("source", text))
        self.assertEqual(peer_mailbox.get(row["id"])["status"], "replied")
        # Startup may release the claim just before the old process POSTs; the
        # retained result_status still proves this is a legacy watcher payload.
        self.assertTrue(ui_contract._release_legacy_peer_result("source", text))
        pruned = text.replace(row["id"], "pm_" + "f" * 32)
        self.assertTrue(ui_contract._release_legacy_peer_result("source", pruned))

    def test_dispatch_failure_becomes_terminal_result(self):
        row = peer_mailbox.create("source", "target")
        got = self._call(Adapter(fail=True), row["id"])
        self.assertEqual(got.code, 500)
        state = peer_mailbox.get(row["id"])
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], "隔离问询执行失败（RuntimeError）")

    def test_option_failure_becomes_terminal_result(self):
        row = peer_mailbox.create("source", "target")
        ad = Adapter()
        ad.session_options = mock.Mock(side_effect=RuntimeError("bad options"))
        got = self._call(ad, row["id"])
        self.assertEqual(got.code, 500)
        state = peer_mailbox.get(row["id"])
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], "隔离问询执行失败（RuntimeError）")


if __name__ == "__main__":
    unittest.main()
