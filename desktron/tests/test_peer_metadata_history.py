"""来源元数据跨过 Pi 原生投影、重启与窗口裁剪，不进入模型正文。"""
import json
from pathlib import Path
import tempfile
import unittest

import test_pi_engine as pi_test
from peer_message import MODEL_HEAD
from peer_metadata import restore

HEAD = "desktop · 示例项目 · Codex · 架构讨论："
TEXT = MODEL_HEAD + '\n\n请讨论架构。\n\n——以上来自另一个 Desk 会话，要回话:desk-msg send adp_source "…"'


def message(head="", text=TEXT):
    part = {"type": "text", "text": text}
    if head:
        part["peer_head"] = head
    return {"info": {"id": "msg_u_local", "role": "user", "at": 100}, "parts": [part]}


class PeerHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = pi_test.ea.PiAdapter(self.tmp.name, self.tmp.name, pi_test.Bus())
        self.sid = "adp_test"
        self.native = Path(self.tmp.name) / "native.jsonl"
        rows = [{"type": "session", "id": "pi-1", "cwd": self.tmp.name},
                {"type": "message", "id": "native-id", "message": {
                    "role": "user", "content": [{"type": "text", "text": TEXT}], "timestamp": 120}}]
        self.native.write_text("\n".join(json.dumps(row) for row in rows))

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_rebuild_trim_restart_and_history(self):
        self.ad._append_msg(self.sid, message(HEAD))
        projected = self.ad._pi_group(str(self.native), self.sid)
        self.assertEqual(projected[0]["parts"][0]["peer_head"], HEAD)
        self.assertNotEqual(projected[0]["info"]["id"], "msg_u_local")
        self.ad._rewrite_msgs(self.sid, [message()])
        self.assertEqual(self.ad.messages(self.sid)[0]["parts"][0]["peer_head"], HEAD)
        self.ad._rewrite_msgs(self.sid, [])  # 滚出本地消息窗口后，来源仍需独立保存
        fresh = pi_test.ea.PiAdapter(self.tmp.name, self.tmp.name, pi_test.Bus())
        restored = fresh._pi_group(str(self.native), self.sid)
        self.assertEqual(restored[0]["parts"][0]["peer_head"], HEAD)
        self.assertEqual(restored[0]["parts"][0]["text"], TEXT)
        self.assertNotIn(HEAD, self.native.read_text())

    def test_migrate_metadata_from_old_projection_before_rewrite(self):
        path = Path(self.ad._msg_path(self.sid))
        path.write_text(json.dumps(message(HEAD)) + "\n")
        self.ad._rewrite_msgs(self.sid, [message()])
        self.assertEqual(self.ad.messages(self.sid)[0]["parts"][0]["peer_head"], HEAD)
        self.assertTrue(Path(str(path) + ".peer-heads.json").exists())

    def test_missing_and_different_session_never_guess(self):
        self.ad._append_msg(self.sid, message(HEAD))
        self.assertNotIn("peer_head", self.ad._pi_group(str(self.native), "adp_other")[0]["parts"][0])
        other = message(text=TEXT.replace("adp_source", "adp_another"))
        self.ad._rewrite_msgs(self.sid, [other])
        self.assertNotIn("peer_head", self.ad.messages(self.sid)[0]["parts"][0])

    def test_ambiguous_sources_not_assigned_to_missing_head(self):
        path = self.ad._msg_path(self.sid)
        restore(path, [message(HEAD), message("server · 另一来源")])
        rebuilt = [message()]
        restore(path, rebuilt)
        self.assertNotIn("peer_head", rebuilt[0]["parts"][0])

    def test_delete_removes_display_metadata(self):
        self.ad._append_msg(self.sid, message(HEAD))
        path = Path(self.ad._msg_path(self.sid) + ".peer-heads.json")
        self.assertTrue(path.exists())
        self.ad.delete_session(self.sid)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
