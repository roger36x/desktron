import hashlib
import json
import os
from _platform import assert_private_file, assert_private_directory, symlink
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from performance_telemetry import PerformanceTelemetry, RETENTION_DAYS


class PerformanceTelemetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PerformanceTelemetry(self.tmp.name, "pi")

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        paths = list((Path(self.tmp.name) / "diagnostics" / "performance" / "pi").glob("*.jsonl"))
        rows = []
        for path in paths:
            rows.extend(json.loads(line) for line in path.read_text().splitlines())
        return paths, rows

    def test_hmac_is_domain_separated_local_and_not_plain_sha(self):
        first = self.store.fingerprint("system_fp", "common prompt")
        self.assertEqual(first, self.store.fingerprint("system_fp", "common prompt"))
        self.assertNotEqual(first, self.store.fingerprint("tools_segment_fp", "common prompt"))
        self.assertNotEqual(first, hashlib.sha256(b"common prompt").hexdigest()[:32])
        other = PerformanceTelemetry(self.tmp.name, "codex")
        self.assertEqual(first, other.fingerprint("system_fp", "common prompt"))
        with tempfile.TemporaryDirectory() as another:
            self.assertNotEqual(first, PerformanceTelemetry(another, "pi").fingerprint(
                "system_fp", "common prompt"))

    def test_writer_allows_only_bounded_scalar_metadata(self):
        fp = self.store.fingerprint("system_fp", "private system")
        self.assertTrue(self.store.append("pi_usage", "adp_private", {
            "seq": 4, "request_seq": 2, "input": 100, "cache_read": 80,
            "total": 120, "model": "openai/gpt-5", "provider": "openai",
            "system_fp": fp, "resident_reused": True, "transport": "http_sse",
            "continuation_used": False,
        }))
        paths, rows = self.rows()
        self.assertEqual(len(paths), 1)
        self.assertEqual(rows[0]["system_fp"], fp)
        self.assertNotIn("adp_private", paths[0].name)
        for invalid in (
            {"prompt": "secret"}, {"input": -1}, {"transport": "https://secret"},
            {"resident_reused": 1}, {"system_fp": "short"},
            {"model": "model with spaces"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.append("pi_usage", "adp_private", invalid)

    def test_canaries_never_reach_files(self):
        canaries = {
            "SYSTEM_CANARY": self.store.fingerprint("system_segment_fp", "SYSTEM_CANARY"),
            "USER_CANARY": self.store.fingerprint("history_segment_fp", "USER_CANARY"),
            "TOOL_CANARY": self.store.fingerprint("tools_segment_fp", {"description":"TOOL_CANARY"}),
            "ARG_CANARY": self.store.fingerprint("tail_segment_fp", {"argument":"ARG_CANARY"}),
            "RESULT_CANARY": self.store.fingerprint("reasoning_segment_fp", "RESULT_CANARY"),
            "RAW_SESSION_CANARY": self.store.fingerprint("session_fp", "RAW_SESSION_CANARY"),
        }
        self.store.append("pi_usage", "adp_canary", {
            "input": 9, "cache_read": 0, "total": 10,
            "system_segment_fp": canaries["SYSTEM_CANARY"],
            "history_segment_fp": canaries["USER_CANARY"],
            "tools_segment_fp": canaries["TOOL_CANARY"],
            "tail_segment_fp": canaries["ARG_CANARY"],
            "reasoning_segment_fp": canaries["RESULT_CANARY"],
            "session_fp": canaries["RAW_SESSION_CANARY"],
        })
        root = Path(self.tmp.name) / "diagnostics"
        blob = b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())
        for marker in canaries:
            self.assertNotIn(marker.encode(), blob)

    def test_private_permissions_and_symlink_rejection(self):
        self.store.append("pi_usage", "adp_one", {"input": 1, "cache_read": 0, "total": 2})
        paths, _ = self.rows()
        key = Path(self.tmp.name) / "diagnostics" / "keys" / "ttft-hmac-v1"
        assert_private_file(key)
        assert_private_file(paths[0])
        assert_private_directory(paths[0].parent)
        target = Path(self.tmp.name) / "victim"
        target.write_text("unchanged")
        paths[0].unlink()
        symlink(target, paths[0])
        self.assertFalse(self.store.append("pi_usage", "adp_one", {
            "input": 1, "cache_read": 0, "total": 2}))
        self.assertEqual(target.read_text(), "unchanged")

    def test_retention_and_session_delete_cover_daily_buckets(self):
        self.store.append("pi_usage", "old", {"input": 1, "cache_read": 0, "total": 2})
        current = self.store._event_path("old")
        old_day = time.strftime("%Y%m%d", time.gmtime(time.time() - (RETENTION_DAYS + 2) * 86400))
        old_path = self.store._event_path("old", old_day)
        current.rename(old_path)
        # An active session can create a fresh bucket without refreshing the old bucket.
        self.store.append("pi_usage", "old", {"input": 1, "cache_read": 0, "total": 2})
        self.store.cleanup(force=True)
        self.assertFalse(old_path.exists())
        self.assertTrue(self.store._event_path("old").exists())
        yesterday = time.strftime("%Y%m%d", time.gmtime(time.time() - 86400))
        extra = self.store._event_path("old", yesterday)
        extra.write_text("{}\n")
        self.store.delete("old")
        self.assertFalse(self.store._event_path("old").exists())
        self.assertFalse(extra.exists())

    def test_intermediate_engine_symlink_is_rejected_without_touching_target(self):
        target = Path(self.tmp.name) / "outside"
        target.mkdir()
        performance = Path(self.tmp.name) / "diagnostics" / "performance"
        performance.mkdir(parents=True)
        (performance / "pi").symlink_to(target, target_is_directory=True)
        self.assertFalse(self.store.append("pi_usage", "adp", {
            "input":1, "cache_read":0, "total":2}))
        self.assertEqual(list(target.iterdir()), [])

    def test_timing_schema_flattens_marks_without_free_text(self):
        self.store.record_timing("adp_timing", 7, {
            "stored_ms": 1.25, "submitted_ms": 4, "first_text_ms": 19.5,
            "unknown_ms": 999,
        }, "completed")
        _, rows = self.rows()
        self.assertEqual(rows[0]["seq"], 7)
        self.assertEqual(rows[0]["first_text_ms"], 19.5)
        self.assertNotIn("unknown_ms", rows[0])
        self.assertNotIn("ms", rows[0])


if __name__ == "__main__":
    unittest.main()
