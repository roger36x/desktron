"""会话移动须按真实存储归属更新，Shepherd 不依赖终端 meta。"""
from io import BytesIO
import json
import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", "/tmp/frontend", "7999", "7998", "/tmp/workspace"]
try:
    from engine_adapters.cli_store import SessionStoreMixin
    from session import api as session_api
finally:
    sys.argv = _argv


class SessionMove(unittest.TestCase):
    @staticmethod
    def _owner(row):
        owner = object.__new__(SessionStoreMixin)
        owner.name = "shepherd"
        owner.index = {"adp_s": row}
        owner._lock = threading.Lock()
        owner._save_index = mock.Mock()
        return owner

    @staticmethod
    def _handler(owner, domain="wf-b"):
        raw = json.dumps({"domain": domain}).encode()
        return SimpleNamespace(
            headers={"Content-Length": str(len(raw))},
            rfile=BytesIO(raw),
            _adapter_owning=lambda _sid: owner,
            _send_json=lambda body, code=200: (body, code),
        )

    def _move(self, owner):
        with mock.patch.object(session_api, "_wb_domains_read", return_value=[{"id": "wf-b"}]), \
             mock.patch.object(session_api, "_builtin_domain_ids", return_value=[]), \
             mock.patch.object(session_api, "_term_move_domain") as terminal_move:
            result = session_api.handle(self._handler(owner),
                "/workbench/terminals/adp_s/domain", "", "POST")
        return result, terminal_move

    def test_shepherd_moves_in_adapter_index_without_terminal_meta(self):
        owner = self._owner({"id": "adp_s", "domain": "wf-a", "shepherd_mode": True})
        result, terminal_move = self._move(owner)
        self.assertEqual(({"ok": True, "cd": False}, 200), result)
        self.assertEqual("wf-b", owner.index["adp_s"]["domain"])
        owner._save_index.assert_called_once_with()
        terminal_move.assert_not_called()

    def test_shepherd_task_record_still_cannot_move(self):
        owner = self._owner({"id": "adp_s", "domain": "wf-a", "task": True})
        result, terminal_move = self._move(owner)
        self.assertEqual(422, result[1])
        self.assertEqual("任务执行记录不可移动", result[0]["error"])
        self.assertEqual("wf-a", owner.index["adp_s"]["domain"])
        owner._save_index.assert_not_called()
        terminal_move.assert_not_called()

    def test_indexed_gui_session_updates_adapter_and_terminal_meta(self):
        owner = self._owner({"id": "adp_s", "engine_sid": "native-1", "domain": "wf-a"})
        owner.name = "codex"
        with mock.patch.object(session_api, "_wb_domains_read", return_value=[{"id": "wf-b"}]), \
             mock.patch.object(session_api, "_builtin_domain_ids", return_value=[]), \
             mock.patch.object(session_api, "_term_move_domain",
                               return_value=(True, "", False)) as terminal_move:
            result = session_api.handle(self._handler(owner),
                "/workbench/terminals/native-1/domain", "", "POST")
        self.assertEqual(({"ok": True, "cd": False}, 200), result)
        self.assertEqual("wf-b", owner.index["adp_s"]["domain"])
        owner._save_index.assert_called_once_with()
        terminal_move.assert_called_once_with("native-1", "wf-b")

    def test_terminal_meta_write_error_rolls_back_adapter_index(self):
        owner = self._owner({"id": "adp_s", "engine_sid": "native-1", "domain": "wf-a"})
        owner.name = "codex"
        with mock.patch.object(session_api, "_wb_domains_read", return_value=[{"id": "wf-b"}]), \
             mock.patch.object(session_api, "_builtin_domain_ids", return_value=[]), \
             mock.patch.object(session_api, "_term_move_domain", side_effect=OSError("disk")):
            result = session_api.handle(self._handler(owner),
                "/workbench/terminals/native-1/domain", "", "POST")
        self.assertEqual(500, result[1])
        self.assertEqual("wf-a", owner.index["adp_s"]["domain"])
        self.assertEqual(2, owner._save_index.call_count)

    def test_unindexed_non_shepherd_keeps_terminal_meta_path(self):
        owner = SimpleNamespace(name="codex", index={"adp_s": {"id": "adp_s"}})
        with mock.patch.object(session_api, "_wb_domains_read", return_value=[{"id": "wf-b"}]), \
             mock.patch.object(session_api, "_builtin_domain_ids", return_value=[]), \
             mock.patch.object(session_api, "_term_move_domain",
                               return_value=(True, "", False)) as terminal_move:
            result = session_api.handle(self._handler(owner),
                "/workbench/terminals/native-1/domain", "", "POST")
        self.assertEqual(({"ok": True, "cd": False}, 200), result)
        terminal_move.assert_called_once_with("native-1", "wf-b")


if __name__ == "__main__":
    unittest.main()
