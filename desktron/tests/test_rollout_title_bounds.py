"""Codex rollout 标题索引不得在列表请求中扫完大文件。"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktron/lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", str(ROOT / "frontend"), "7999", "7998", str(ROOT)]
try:
    import term  # noqa: E402
finally:
    sys.argv = _argv


SID = "12345678-1234-1234-1234-123456789abc"


def _line(obj):
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


def _user(text):
    return {"type": "response_item", "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}]}}


class TitleSourceFallbacks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.managed = self.root / "managed"
        self.global_home = self.root / "global"
        self.managed.mkdir()
        self.global_home.mkdir()
        patches = [
            mock.patch.object(term, "_STATE_DB", {"codex": str(self.managed / "state_5.sqlite")}),
            mock.patch.object(term, "_STATE_DB_GLOBAL", {"codex": str(self.global_home / "state_5.sqlite")}),
            mock.patch.object(term, "_SESSION_INDEX", {"codex": str(self.managed / "session_index.jsonl")}),
            mock.patch.object(term, "_SESSION_INDEX_GLOBAL", {"codex": str(self.global_home / "session_index.jsonl")}),
            mock.patch.object(term, "_THREAD_TITLE_CACHE", {}),
            mock.patch.object(term, "_SESSION_INDEX_CACHE", {}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def make_db(path, rows):
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE threads (id TEXT, title TEXT)")
            conn.executemany("INSERT INTO threads VALUES (?, ?)", rows)
            conn.commit()
        finally:
            conn.close()

    def test_global_state_db_is_fallback_and_managed_db_wins(self):
        shared, global_only = str(uuid.uuid4()), str(uuid.uuid4())
        self.make_db(self.global_home / "state_5.sqlite",
                     [(shared, "全局标题"), (global_only, "只有全局有")])
        self.make_db(self.managed / "state_5.sqlite", [(shared, "隔离标题")])
        self.assertEqual(term._state_db_titles("codex")[shared], "隔离标题")
        self.assertEqual(term._state_db_titles("codex")[global_only], "只有全局有")

    def test_global_session_index_is_fallback_and_managed_index_wins(self):
        shared, global_only = str(uuid.uuid4()), str(uuid.uuid4())
        (self.global_home / "session_index.jsonl").write_text(
            json.dumps({"id": shared, "thread_name": "全局标题"}) + "\n" +
            json.dumps({"id": global_only, "thread_name": "只有全局有"}) + "\n")
        (self.managed / "session_index.jsonl").write_text(
            json.dumps({"id": shared, "thread_name": "隔离标题"}) + "\n")
        self.assertEqual(term._session_index_titles("codex")[shared], "隔离标题")
        self.assertEqual(term._session_index_titles("codex")[global_only], "只有全局有")

    def test_global_db_hit_does_not_open_rollout(self):
        sid = str(uuid.uuid4())
        self.make_db(self.global_home / "state_5.sqlite", [(sid, "数据库标题")])
        missing = self.root / ("rollout-x-%s.jsonl" % sid)
        with mock.patch.object(term, "_rollout_title_state",
                               side_effect=AssertionError("不应读取 rollout")):
            self.assertEqual(term._codex_rollout_title(str(missing)), "数据库标题")


class RolloutTitleBounds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(
            self.tmp.name, "rollout-2026-09-23T00-00-00-%s.jsonl" % SID)
        self.saved = term._RT_CACHE
        term._RT_CACHE = {"codex": {}}
        self.addCleanup(setattr, term, "_RT_CACHE", self.saved)

    def resolve(self, **kwargs):
        patches = (
            mock.patch.object(term, "_session_index_titles", return_value={}),
            mock.patch.object(term, "_state_db_titles", return_value={}),
        )
        with patches[0], patches[1]:
            return term._codex_rollout_title_bounded(self.path, "codex", **kwargs)

    def test_large_rollout_reads_only_bounded_windows_on_request(self):
        text = "用户首条消息作为失败时的默认标题而且需要截断且不能超过长度"
        payload = _line({"type": "session_meta", "payload": {"id": SID}}) + _line(_user(text))
        payload += _line({"type": "noop", "payload": {}}) * 30000
        Path(self.path).write_bytes(payload)
        original_consume = term._rollout_title_consume
        with mock.patch.object(term, "_rollout_title_consume", wraps=original_consume) as consume, \
                mock.patch.object(term, "_rollout_title_schedule") as schedule:
            title = self.resolve()
        state = term._RT_CACHE["codex"][self.path]
        self.assertEqual(title, text[:23] + "…")
        self.assertLess(state["offset"], len(payload))
        self.assertFalse(state["complete"])
        self.assertLess(consume.call_count, 10000, "不得解析巨型 rollout 的中间正文")
        schedule.assert_called_once_with(self.path, "codex")

    def test_repeated_request_does_not_take_over_background_middle_scan(self):
        payload = _line({"type": "session_meta", "payload": {"id": SID}}) + _line(_user("默认名"))
        payload += _line({"type": "noop", "payload": {}}) * 30000
        Path(self.path).write_bytes(payload)
        with mock.patch.object(term, "_rollout_title_schedule"):
            self.assertEqual(self.resolve(), "默认名")
            old_offset = term._RT_CACHE["codex"][self.path]["offset"]
            self.assertEqual(self.resolve(), "默认名")
        self.assertEqual(term._RT_CACHE["codex"][self.path]["offset"], old_offset)

    def test_background_scan_can_later_find_middle_rename(self):
        text = "默认名"
        payload = _line({"type": "session_meta", "payload": {"id": SID}}) + _line(_user(text))
        payload += _line({"type": "noop", "payload": {}}) * 9000
        renamed = _line({"type": "event_msg", "payload": {
            "type": "thread_name_updated", "thread_name": "中部旧改名"}})
        payload += renamed
        payload += _line({"type": "noop", "payload": {}}) * 9000
        Path(self.path).write_bytes(payload)
        with mock.patch.object(term, "_rollout_title_schedule"):
            self.assertEqual(self.resolve(), "默认名")
        while True:
            state = term._rollout_title_scan(self.path, "codex", 512 * 1024)
            if state.get("complete"):
                break
        self.assertEqual(self.resolve(), "中部旧改名")

    def test_append_resumes_at_old_cursor_and_finds_rename(self):
        Path(self.path).write_bytes(
            _line({"type": "session_meta", "payload": {"id": SID}}) + _line(_user("默认名")))
        self.assertEqual(self.resolve(), "默认名")
        old_offset = term._RT_CACHE["codex"][self.path]["offset"]
        renamed = _line({"type": "event_msg", "payload": {
            "type": "thread_name_updated", "thread_name": "后续改名"}})
        with open(self.path, "ab") as stream:
            stream.write(renamed)
        self.assertEqual(self.resolve(), "后续改名")
        state = term._RT_CACHE["codex"][self.path]
        self.assertEqual(state["offset"] - old_offset, len(renamed))

    def test_persisted_first_user_fallback_avoids_rollout_read(self):
        Path(self.path).write_bytes(b"must not be read")
        with mock.patch.object(term, "_rollout_title_scan") as scan, \
                mock.patch.object(term, "_rollout_title_state") as state:
            title = self.resolve(fallback="持久化默认名", trusted_fallback=True)
        self.assertEqual(title, "持久化默认名")
        scan.assert_not_called()
        state.assert_not_called()

    def test_native_title_avoids_rollout_read(self):
        Path(self.path).write_bytes(b"must not be read")
        with mock.patch.object(term, "_session_index_titles", return_value={SID: "原生改名"}), \
                mock.patch.object(term, "_state_db_titles", return_value={}), \
                mock.patch.object(term, "_rollout_title_state") as state:
            title = term._codex_rollout_title_bounded(self.path, "codex")
        self.assertEqual(title, "原生改名")
        state.assert_not_called()

    def test_bad_persisted_cursor_is_reset(self):
        Path(self.path).write_bytes(_line({"type": "session_meta", "payload": {"id": SID}})
                                   + _line(_user("默认名")))
        st = os.stat(self.path)
        term._RT_CACHE["codex"][self.path] = {"identity": [st.st_dev, st.st_ino],
                                              "offset": "bad"}
        self.assertEqual(self.resolve(), "默认名")
        self.assertEqual(term._RT_CACHE["codex"][self.path]["offset"], os.path.getsize(self.path))


if __name__ == "__main__":
    unittest.main()
