import json
import importlib.util
import os
from pathlib import Path
from _platform import database
import sys
import tempfile
import time
import types


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktron" / "lib"))

from usage_stats import aggregate, delete_session, normalize_usage, record_turn  # noqa: E402


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _projection(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"info": row, "parts": []}) + "\n" for row in rows),
                    encoding="utf-8")


def test_projection_backfill_and_durable_detail_override():
    with tempfile.TemporaryDirectory() as td:
        now = time.time()
        now_ms = int(now * 1000)
        adapter = Path(td) / "adapter" / "shepherd"
        _write(adapter / "sessions.json", {"adp_demo": {
            "title": "用量会话", "domain": "desk-iter", "engine": "shepherd"}})
        _projection(adapter / "adp_demo.jsonl", [
            {"role": "assistant", "seq": 101, "done_at": now_ms - 1000,
             "engine": "codex", "usage": {"tokens": 100, "cached": 60}},
            {"role": "assistant", "seq": 102, "done_at": now_ms,
             "engine": "pi", "usage": {"tokens": 50, "cached": 20}},
        ])
        assert record_turn(td, "adp_demo", at=now_ms - 1000, seq=101, engine="codex",
                           model="gpt-test", domain="desk-iter", usage={"tokens": 110, "input": 80,
                                                     "output": 30, "cached": 50})
        result = aggregate(td, {"desk-iter": "Desk 迭代"}, days=30, now=now)

        assert result["summary"]["total"] == 110
        assert result["summary"]["turns"] == 1
        assert result["summary"]["historical_total"] == 50
        assert result["summary"]["recorded_total"] == 160
        assert result["summary"]["input"] == 80
        assert result["summary"]["output"] == 30
        assert result["summary"]["detail_coverage"] == 1
        assert result["workflows"][0]["name"] == "Desk 迭代"
        assert result["workflows"][0]["sessions"] == 1
        session = result["sessions"][0]
        assert session["title"] == "用量会话"
        assert session["engine"] == "codex"
        assert session["model"] == "gpt-test"
        assert sum(day["recorded_total"] for day in result["days"]) == 160
        assert result["coverage"]["start"] is not None
        assert result["health"]["migration_complete"] is True
        assert result["local_only"] is True


def test_range_ignores_old_turns_and_deleted_sessions():
    with tempfile.TemporaryDirectory() as td:
        now = time.time()
        adapter = Path(td) / "adapter" / "pi"
        _write(adapter / "sessions.json", {"adp_keep": {
            "title": "保留", "domain": "_desktop"}})
        old_ms = int((now - 40 * 86400) * 1000)
        assert record_turn(td, "adp_keep", at=old_ms, seq=1, engine="pi",
                           usage={"total": 99, "cached": 0})
        result = aggregate(td, {}, days=30, now=now)
        assert result["summary"]["total"] == 0
        assert result["range"]["days"] == 30

        delete_session(td, "adp_keep")
        result = aggregate(td, {}, days=365, now=now)
        assert result["summary"]["recorded_total"] == 99
        assert result["summary"]["archived_sessions"] == 1
        assert result["sessions"][0]["title"] == "已删除会话"
        assert result["sessions"][0]["archived"] is True


def test_malformed_rows_fail_closed_without_exposing_unknown_session():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "usage" / "sessions"
        root.mkdir(parents=True)
        (root / "bad.jsonl").write_text('{"usage":{"total":999}}\nnot-json\n', encoding="utf-8")
        result = aggregate(td, {}, days=365, now=time.time())
        assert result["summary"]["total"] == 0
        assert result["sessions"] == []


def test_unknown_detail_fields_are_not_reported_as_zeroes():
    usage = normalize_usage({"total": 70, "cached": 20})

    assert usage == {"total": 70, "cached": 20}
    assert "input" not in usage
    assert "output" not in usage


def test_sqlite_ledger_is_idempotent_incremental_and_quarantines_anomalies():
    with tempfile.TemporaryDirectory() as td:
        now = time.time()
        now_ms = int(now * 1000)
        adapter = Path(td) / "adapter" / "pi"
        _write(adapter / "sessions.json", {"adp_live": {
            "title": "实时", "domain": "desk-iter"}})
        assert record_turn(td, "adp_live", at=now_ms, seq=1, engine="pi",
                           domain="desk-iter", usage={"total": 120, "cached": 80})
        assert record_turn(td, "adp_live", at=now_ms, seq=1, engine="pi",
                           domain="desk-iter", usage={"total": 120, "cached": 80})
        assert record_turn(td, "adp_live", at=now_ms + 1, seq=2, engine="codex",
                           domain="desk-iter", usage={"total": 900_000_000, "cached": 0})

        result = aggregate(td, {"desk-iter": "Desk 迭代"}, days=30, now=now + 1)
        assert result["summary"]["total"] == 120
        assert result["summary"]["turns"] == 1
        assert result["summary"]["anomalous_turns"] == 1
        assert result["summary"]["anomalous_total"] == 900_000_000
        assert result["health"]["daily_reconciled"] is True

        db_path = Path(td) / "usage" / "usage.sqlite3"
        with database(db_path) as db:
            assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 2
            assert db.execute("SELECT SUM(turns) FROM daily").fetchone()[0] == 2
            assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_historical_projection_is_imported_once_not_scanned_on_every_query():
    with tempfile.TemporaryDirectory() as td:
        now = time.time()
        now_ms = int(now * 1000)
        adapter = Path(td) / "adapter" / "codex"
        _write(adapter / "sessions.json", {"adp_old": {
            "title": "旧会话", "domain": "_desktop"}})
        path = adapter / "adp_old.jsonl"
        _projection(path, [{"role": "assistant", "seq": 1, "done_at": now_ms,
                            "usage": {"total": 40, "cached": 10}}])
        first = aggregate(td, {}, days=30, now=now)
        assert first["summary"]["historical_total"] == 40

        _projection(path, [{"role": "assistant", "seq": 1, "done_at": now_ms,
                            "usage": {"total": 40, "cached": 10}},
                           {"role": "assistant", "seq": 2, "done_at": now_ms,
                            "usage": {"total": 999, "cached": 0}}])
        second = aggregate(td, {}, days=30, now=now)
        assert second["summary"]["historical_total"] == 40
        assert second["health"]["migrated_now"] is False


def test_usage_api_is_read_only_and_forwards_range(monkeypatch):
    calls = {}
    usage_module = types.ModuleType("usage_stats")
    usage_module.aggregate = lambda data, names, days: calls.update(
        data=data, names=names, days=days) or {"summary": {"total": 7}}
    deskcfg_module = types.ModuleType("deskcfg")
    deskcfg_module.DATA_HOME = "/tmp/desk-usage-test"
    workbench_module = types.ModuleType("workbench")
    workbench_module._wb_domains_read = lambda: [{"id": "demo", "name": "演示"}]
    monkeypatch.setitem(sys.modules, "usage_stats", usage_module)
    monkeypatch.setitem(sys.modules, "deskcfg", deskcfg_module)
    monkeypatch.setitem(sys.modules, "workbench", workbench_module)

    spec = importlib.util.spec_from_file_location(
        "_usage_api_test", ROOT / "desktron" / "lib" / "usage" / "api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)

    class Handler:
        def _send_json(self, value):
            return value

    assert api.owns("/workbench/usage", "GET")
    assert not api.owns("/workbench/usage", "POST")
    assert api.handle(Handler(), "/workbench/usage", "days=90", "GET") == {
        "summary": {"total": 7}}
    assert calls == {"data": "/tmp/desk-usage-test", "names": {"demo": "演示"},
                     "days": 90}
