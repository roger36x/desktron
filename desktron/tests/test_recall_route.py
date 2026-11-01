"""撤回路由必须在接收新消息之前拒绝，并使用事务身份处理恢复。"""
import contextlib
from unittest import mock
import unittest

from test_peer_message_route import Handler, Adapter, ui_contract


class RecallRoute(unittest.TestCase):
    def call(self, adapter, body, action=False):
        h = Handler("session-1", body)
        if action:
            h.path = "/session/session-1/recall"
        with mock.patch.object(ui_contract, "_adapter", return_value=adapter), \
                mock.patch.object(ui_contract, "_engine_pref", return_value="codex"), \
                mock.patch.object(ui_contract, "_wb_state_read", return_value={}), \
                mock.patch.object(Handler, "_adapter_owning", return_value=adapter), \
                mock.patch("self_update.admit_work", return_value=contextlib.nullcontext(True)), \
                mock.patch("self_update.tracked_work", return_value=contextlib.nullcontext(True)):
            h._adapter_route("POST")
        return h

    def test_pending_returns_structured_conflict_without_accepting_prompt(self):
        adapter = Adapter()
        adapter.recall_block = lambda sid: {"code": "recall_pending", "error": "pending",
                                             "recall": {"id": "op", "phase": "blocked"}}
        h = self.call(adapter, {"parts": [{"type": "text", "text": "request"}]})
        self.assertEqual(h.code, 409)
        self.assertEqual(h.out["code"], "recall_pending")
        self.assertEqual(adapter.calls, [])

    def test_recall_requested_between_route_check_and_prompt_keeps_structured_conflict(self):
        from engine_adapters.recall import RecallPendingError
        adapter = Adapter()
        adapter.recall_block = lambda sid: None
        adapter.prompt_async = mock.Mock(side_effect=RecallPendingError({
            "code": "recall_pending", "error": "pending", "recall": {"id": "op"}}))
        h = self.call(adapter, {"parts": [{"type": "text", "text": "request"}]})
        self.assertEqual(h.code, 409)
        self.assertEqual(h.out["code"], "recall_pending")

    def test_recovery_action_uses_explicit_operation_and_rejects_unknown_action(self):
        adapter = Adapter()
        adapter.resolve_recall = mock.Mock(return_value={"ok": True, "session_id": "restored"})
        h = self.call(adapter, {"operation_id": "op", "action": "recover"}, action=True)
        self.assertEqual(h.code, 200)
        adapter.resolve_recall.assert_called_once_with("session-1", "op", "recover")
        h = self.call(adapter, {"operation_id": "op", "action": "delete-all"}, action=True)
        self.assertEqual(h.code, 400)
        self.assertEqual(adapter.resolve_recall.call_count, 1)


if __name__ == "__main__":
    unittest.main()
