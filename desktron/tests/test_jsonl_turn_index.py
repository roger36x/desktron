import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MODULE = os.path.join(ROOT, "desktron", "lib", "engine_adapters", "message_index.py")
spec = importlib.util.spec_from_file_location("message_index_under_test", MODULE)
message_index = importlib.util.module_from_spec(spec)
spec.loader.exec_module(message_index)
JsonlTurnIndex = message_index.JsonlTurnIndex


def message(number, role="user"):
    return {"info": {"id": "m%d" % number, "role": role},
            "parts": [{"type": "text", "text": "x" * 1000}]}


def turn_start(row):
    return (row.get("info") or {}).get("role") == "user"


class JsonlTurnIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "messages.jsonl")
        self.index = JsonlTurnIndex(self.path, turn_start)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def decode(raw):
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def fill(self, turns):
        for number in range(turns):
            self.index.append(message(number * 2))
            self.index.append(message(number * 2 + 1, "assistant"))

    def test_prelude_records_are_included_only_on_oldest_page(self):
        self.index.append(message(99, "assistant"))
        self.fill(3)
        oldest = self.decode(self.index.raw_window(have=2, limit=1))
        newer = self.decode(self.index.raw_window(have=0, limit=2))
        self.assertEqual([row["info"]["id"] for row in oldest], ["m99", "m0", "m1"])
        self.assertEqual([row["info"]["id"] for row in newer], ["m2", "m3", "m4", "m5"])

    def test_pages_target_byte_range(self):
        self.fill(8)
        page = self.decode(self.index.raw_window(have=2, limit=3))
        self.assertEqual([row["info"]["id"] for row in page],
                         ["m6", "m7", "m8", "m9", "m10", "m11"])

    def test_corrupt_sidecar_rebuilds_from_authoritative_jsonl(self):
        self.fill(4)
        with open(self.index.sidecar, "w", encoding="utf-8") as stream:
            stream.write("not json")
        self.index._cached = None
        self.assertEqual(self.index.turn_count(), 4)
        self.assertEqual(self.decode(self.index.raw_window(limit=1))[0]["info"]["id"], "m6")

    def test_invalid_offsets_rebuild_from_authoritative_jsonl(self):
        self.fill(2)
        with open(self.index.sidecar, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"version": message_index.VERSION,
                                     "identity": {key: self.index._stamp(self.path)[key]
                                                  for key in ("dev", "ino")}}) + "\n")
            stream.write(json.dumps([999999, True]) + "\n")
            stream.write(json.dumps({"source": self.index._stamp(self.path)}) + "\n")
        self.index._cached = None
        self.assertEqual(self.index.turn_count(), 2)

    def test_append_does_not_rewrite_existing_sidecar(self):
        self.fill(50)
        before = os.stat(self.index.sidecar)
        self.index.append(message(100))
        after = os.stat(self.index.sidecar)
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertLess(after.st_size - before.st_size, 300)

    def test_incomplete_tail_is_excluded_and_next_append_repairs_source(self):
        self.fill(2)
        with open(self.path, "ab") as stream:
            stream.write(b'{"info":{"id":"partial"')
        self.index._cached = None
        page = self.decode(self.index.raw_window(limit=2))
        self.assertEqual([row["info"]["id"] for row in page], ["m0", "m1", "m2", "m3"])
        self.index.append(message(4))
        self.index._cached = None
        self.assertEqual([row["info"]["id"] for row in self.decode(self.index.raw_window(limit=3))],
                         ["m0", "m1", "m2", "m3", "m4"])
        with open(self.path, "rb") as stream:
            self.assertNotIn(b'partial', stream.read())

    def test_complete_final_record_without_newline_survives_append(self):
        first = json.dumps(message(0), ensure_ascii=False).encode()
        with open(self.path, "wb") as stream:
            stream.write(first)
        self.index.refresh()
        self.index.append(message(1, "assistant"))
        with open(self.path, "rb") as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
        self.assertEqual([row["info"]["id"] for row in rows], ["m0", "m1"])

    def test_corrupt_authoritative_middle_row_is_not_silently_skipped(self):
        self.fill(2)
        with open(self.path, "ab") as stream:
            stream.write(b"not-json\n")
            stream.write((json.dumps(message(99), ensure_ascii=False) + "\n").encode())
        self.index._cached = None
        with self.assertRaises(ValueError):
            self.index.turn_count()

    def test_external_rewrite_invalidates_source_stamp(self):
        self.fill(3)
        with open(self.path, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(message(99), ensure_ascii=False) + "\n")
        self.assertEqual(self.index.turn_count(), 1)
        self.assertEqual(self.decode(self.index.raw_window(limit=1))[0]["info"]["id"], "m99")

    def test_continuously_replaced_source_has_bounded_rebuild_retries(self):
        self.fill(2)
        self.index._cached = None
        real_stamp = self.index._stamp
        calls = 0
        def unstable(path):
            nonlocal calls
            value = real_stamp(path)
            calls += 1
            return {**value, "ino": value["ino"] + calls}
        with mock.patch.object(self.index, "_stamp", side_effect=unstable):
            with self.assertRaisesRegex(RuntimeError, "every index rebuild"):
                self.index._rebuild()
        self.assertEqual(calls, message_index.MAX_SNAPSHOT_RETRIES)

    def test_continuously_replaced_source_has_bounded_read_retries(self):
        self.fill(2)
        row = self.index._load()
        calls = 0
        def unstable(_st):
            nonlocal calls
            calls += 1
            return {**row["source"], "ino": row["source"]["ino"] + calls}
        with (mock.patch.object(self.index, "_load", return_value=row),
              mock.patch.object(self.index, "_stat_stamp", side_effect=unstable)):
            with self.assertRaisesRegex(RuntimeError, "every indexed read"):
                self.index.raw_window(limit=1)
        self.assertEqual(calls, message_index.MAX_SNAPSHOT_RETRIES)

    def test_trim_streams_recent_turns_and_refreshes_index(self):
        self.fill(7)
        self.assertTrue(self.index.trim_to_recent(2))
        self.assertEqual(self.index.turn_count(), 2)
        self.assertEqual([row["info"]["id"] for row in self.decode(self.index.raw_window(limit=2))],
                         ["m10", "m11", "m12", "m13"])

    def test_missing_source_is_empty(self):
        self.assertEqual(self.index.raw_window(limit=3), b"")
        self.assertEqual(self.index.turn_count(), 0)


if __name__ == "__main__":
    unittest.main()
