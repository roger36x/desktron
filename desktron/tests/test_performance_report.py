import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from performance_report import aggregate, _private_write
from performance_telemetry import PerformanceTelemetry


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_report_strictly_joins_file_and_turn_then_exports_only_aggregates():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        private_fp = "a" * 32
        write(root / "pi" / "PRIVATE_FILE_A.jsonl", [
            {"event":"pi_usage", "seq":1, "request_seq":1, "request_epoch_fp":"1" * 32, "input":100,
             "cache_read":0, "total":110, "provider":"example-gw", "model":"gpt-test",
             "identity_mode":"native", "resident_reused":False, "session_fp":private_fp},
            {"event":"pi_usage", "seq":1, "request_seq":2, "request_epoch_fp":"1" * 32, "input":10,
             "cache_read":90, "total":105, "provider":"example-gw", "model":"gpt-test",
             "identity_mode":"native", "resident_reused":False, "session_fp":private_fp},
            {"event":"response_timing", "seq":1, "outcome":"completed",
             "stored_ms":2, "prepared_ms":5, "submitted_ms":8,
             "first_reasoning_ms":18, "first_text_ms":28, "ready_ms":40},
        ])
        write(root / "pi" / "PRIVATE_FILE_B.jsonl", [
            {"event":"pi_usage", "seq":1, "request_seq":1, "request_epoch_fp":"2" * 32, "input":10,
             "cache_read":90, "total":105, "provider":"example-gw", "model":"gpt-test",
             "resident_reused":True, "session_fp":"b" * 32},
            {"event":"response_timing", "seq":1, "outcome":"completed",
             "stored_ms":1, "prepared_ms":3, "submitted_ms":5,
             "first_text_ms":15, "ready_ms":20},
            # A usage event without timing is intentionally not a complete turn.
            {"event":"pi_usage", "seq":2, "request_seq":1, "input":10,
             "cache_read":90, "total":105, "provider":"example-gw", "model":"gpt-test"},
        ])
        result = aggregate(root)
        assert result["turns"] == 2
        assert sum(group["provider_requests"] for group in result["groups"]) == 3
        by_resident = {group["resident"]: group for group in result["groups"]}
        assert by_resident["cold"]["cache"] == "partial"
        assert by_resident["cold"]["identity_mode"] == "native"
        assert by_resident["warm"]["identity_mode"] == "unknown"
        assert by_resident["cold"]["phases"]["submitted_to_first_event_ms"] == {
            "n":1, "missing":0, "sufficient":False,
            "p50":10.0, "p95":10.0, "p99":10.0}
        assert by_resident["cold"]["cache_metrics"]["continuation_hit90_rate"] == 1.0
        assert by_resident["cold"]["cache_metrics"]["max_initial_consecutive_misses"] == 1
        assert by_resident["warm"]["cache"] == "hit90"
        assert result["integrity"]["unlinked_usage_turns"] == 1
        serialized = json.dumps(result)
        assert "PRIVATE_FILE" not in serialized
        assert private_fp not in serialized
        assert "session_fp" not in serialized


def test_report_marks_partial_or_mixed_identity_as_unknown_or_mixed():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder); path = root / "pi" / "private.jsonl"
        write(path, [
            {"event":"pi_usage", "seq":1, "request_seq":1, "request_epoch_fp":"3" * 32,
             "input":10, "cache_read":0, "total":10, "provider":"example-gw", "model":"gpt"},
            {"event":"pi_usage", "seq":1, "request_seq":2, "request_epoch_fp":"3" * 32,
             "input":1, "cache_read":9, "total":10, "provider":"example-gw", "model":"gpt",
             "identity_mode":"native"},
            {"event":"response_timing", "seq":1, "outcome":"completed",
             "submitted_ms":5, "first_text_ms":15},
            {"event":"pi_usage", "seq":2, "request_seq":1, "request_epoch_fp":"4" * 32,
             "input":10, "cache_read":0, "total":10, "provider":"example-gw", "model":"gpt",
             "identity_mode":"legacy"},
            {"event":"pi_usage", "seq":2, "request_seq":2, "request_epoch_fp":"4" * 32,
             "input":1, "cache_read":9, "total":10, "provider":"example-gw", "model":"gpt",
             "identity_mode":"native"},
            {"event":"response_timing", "seq":2, "outcome":"completed",
             "submitted_ms":5, "first_text_ms":15},
        ])
        result = aggregate(root)
        assert {group["identity_mode"] for group in result["groups"]} == {"unknown", "mixed"}


def test_report_marks_mixed_routes_and_counts_corrupt_rows():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder); path = root / "pi" / "private.jsonl"
        write(path, [
            {"event":"pi_usage", "seq":1, "request_seq":1, "request_epoch_fp":"3" * 32,
             "input":10, "cache_read":0, "total":10, "provider":"one", "model":"a"},
            {"event":"pi_usage", "seq":1, "request_seq":2, "request_epoch_fp":"3" * 32,
             "input":1, "cache_read":9, "total":10, "provider":"two", "model":"b"},
            {"event":"response_timing", "seq":1, "outcome":"completed",
             "submitted_ms":5, "first_text_ms":15},
        ])
        with path.open("a") as stream: stream.write("{broken\n")
        result = aggregate(root)
        assert result["integrity"]["corrupt_rows"] == 1
        assert result["turns"] == 1
        assert result["groups"][0]["provider"] == "mixed"
        assert result["groups"][0]["model"] == "mixed"
        assert result["groups"][0]["phases"]["prepared_to_submitted_ms"]["missing"] == 1


def test_report_merges_daily_buckets_and_rejects_invalid_scalar_rows():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder); engine = root / "pi"; prefix = "a" * 32
        write(engine / f"{prefix}.20260918.jsonl", [
            {"event":"pi_usage", "seq":7, "request_seq":1,
             "request_epoch_fp":"4" * 32, "input":10, "cache_read":0, "total":10,
             "provider":"openai", "model":"gpt"},
            {"event":"pi_usage", "seq":8, "request_seq":"bad",
             "input":10, "cache_read":0, "total":10},
        ])
        write(engine / f"{prefix}.20260919.jsonl", [
            {"event":"response_timing", "seq":7, "outcome":"completed",
             "submitted_ms":5, "first_text_ms":15},
            {"event":"pi_usage", "seq":9, "request_seq":1,
             "input":"bad", "cache_read":0, "total":10},
            {"event":"pi_usage", "seq":11, "request_seq":1,
             "input":10, "cache_read":0, "total":10, "identity_mode":"future"},
            {"event":"response_timing", "seq":10, "outcome":"completed",
             "submitted_ms":"bad", "first_text_ms":15},
        ])
        result = aggregate(root)
        assert result["turns"] == 1
        assert result["groups"][0]["provider_requests"] == 1
        assert result["integrity"]["invalid_rows"] == 4


def test_writer_to_report_accepts_production_epoch_millisecond_sequence():
    with tempfile.TemporaryDirectory() as folder:
        seq = 1_789_783_200_123
        store = PerformanceTelemetry(folder, "pi", now=lambda: 1_789_783_200.5)
        epoch_fp = store.fingerprint("request_epoch_fp", "epoch")
        assert store.append("pi_usage", "adp", {"seq":seq, "request_seq":1,
            "request_epoch_fp":epoch_fp, "input":100, "cache_read":0, "total":110,
            "provider":"openai", "model":"gpt"})
        assert store.record_timing("adp", seq, {"submitted_ms":5, "first_text_ms":15},
                                   "completed")
        result = aggregate(Path(folder) / "diagnostics" / "performance")
        assert result["turns"] == 1
        assert result["integrity"]["invalid_rows"] == 0
        assert result["groups"][0]["phases"]["submitted_to_first_text_ms"]["p50"] == 10.0


def test_private_report_write_rejects_symlink_parent():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder); outside = root / "outside"; outside.mkdir()
        linked = root / "linked"; linked.symlink_to(outside, target_is_directory=True)
        try:
            _private_write(linked / "report.json", "{}\n")
            assert False, "symlink parent should be rejected"
        except ValueError:
            pass
        assert not (outside / "report.json").exists()
