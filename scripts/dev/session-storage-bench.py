#!/usr/bin/env python3
"""Reproducible offline benchmark for session storage/list hot paths.

Uses synthetic data only.  The history and archive "after" cases call the shipped
implementations.  "Before" reproduces the algorithms at base commit
f0f49ddef095bc01164fce93f024ef62f636e851: full JSONL scan for every window and a
fresh SQLite connection/schema setup for every archive transaction.  The sidebar
case is an explicit complexity model of per-domain filtering versus one-pass buckets.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "desktron" / "lib"
sys.path.insert(0, str(LIB))

spec = importlib.util.spec_from_file_location(
    "session_storage_bench_index", LIB / "engine_adapters" / "message_index.py")
index_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(index_module)
JsonlTurnIndex = index_module.JsonlTurnIndex

from shepherd.unified_archive import UnifiedArchive, _ArchiveConnections  # noqa: E402

BASE_COMMIT = "f0f49ddef095bc01164fce93f024ef62f636e851"


def median_ms(fn, repeats=21):
    samples = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        fn()
        samples.append((time.perf_counter_ns() - start) / 1_000_000)
    return round(statistics.median(samples), 3)


def message(number, role, payload_bytes):
    return {"info": {"id": f"m{number}", "role": role},
            "parts": [{"type": "text", "text": "x" * payload_bytes}]}


def is_turn_start(row):
    return (row.get("info") or {}).get("role") == "user"


def old_window(path, have=0, limit=30):
    with open(path, encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    starts = [i for i, row in enumerate(rows) if is_turn_start(row)]
    end = max(0, len(starts) - have)
    start = max(0, end - limit)
    if start == end:
        return []
    begin_row = starts[start]
    end_row = starts[end] if end < len(starts) else len(rows)
    return rows[begin_row:end_row]


def history_bench(root, turns, payload_bytes):
    path = root / f"history-{turns}.jsonl"
    with path.open("w", encoding="utf-8") as stream:
        for turn in range(turns):
            for role in ("user", "assistant"):
                stream.write(json.dumps(message(turn * 2 + (role == "assistant"), role,
                                                payload_bytes), ensure_ascii=False) + "\n")
    index = JsonlTurnIndex(path, is_turn_start)
    rebuild_ms = median_ms(index.refresh, repeats=3)
    raw = index.raw_window(limit=30)
    expected = old_window(path, limit=30)
    assert [json.loads(line) for line in raw.splitlines()] == expected

    def indexed():
        return [json.loads(line) for line in index.raw_window(limit=30).splitlines()]

    source_bytes = path.stat().st_size
    window_bytes = len(raw)
    old_ms = median_ms(lambda: old_window(path, limit=30))
    indexed_ms = median_ms(indexed)
    sidecar_before = Path(index.sidecar).stat().st_size
    index.append(message(turns * 2, "user", payload_bytes))
    sidecar_delta = Path(index.sidecar).stat().st_size - sidecar_before
    return {
        "turns": turns,
        "messages": turns * 2,
        "source_bytes": source_bytes,
        "window_bytes": window_bytes,
        "bytes_read_reduction_x": round(source_bytes / max(1, window_bytes), 2),
        "before_full_scan_ms": old_ms,
        "after_indexed_window_ms": indexed_ms,
        "latency_speedup_x": round(old_ms / max(.000001, indexed_ms), 2),
        "cold_index_rebuild_ms": rebuild_ms,
        "append_sidecar_bytes": sidecar_delta,
    }


def sidebar_bench(session_count=5000, domain_count=100):
    sessions = [{"id": i, "domain": f"d{i % domain_count}",
                 "refs": [f"d{(i + 7) % domain_count}"] if i % 9 == 0 else []}
                for i in range(session_count)]
    domains = [f"d{i}" for i in range(domain_count)]

    before_checks = 0
    def before():
        nonlocal before_checks
        before_checks = 0
        result = {}
        for domain in domains:
            rows = []
            count = 0
            for row in sessions:
                before_checks += 2
                owned = row["domain"] == domain
                shown = owned or domain in row["refs"]
                if shown:
                    rows.append(row)
                if owned:
                    count += 1
            result[domain] = (rows, count)
        return result

    after_checks = 0
    def after():
        nonlocal after_checks
        after_checks = 0
        buckets = {domain: [] for domain in domains}
        counts = {domain: 0 for domain in domains}
        for row in sessions:
            after_checks += 1
            counts[row["domain"]] += 1
            for domain in {row["domain"], *row["refs"]}:
                buckets[domain].append(row)
        return {domain: (buckets[domain], counts[domain]) for domain in domains}

    assert before() == after()
    before_ms = median_ms(before)
    old_checks = before_checks
    after_ms = median_ms(after)
    new_checks = after_checks
    return {"sessions": session_count, "domains": domain_count,
            "before_filter_checks": old_checks, "after_visibility_checks": new_checks,
            "check_reduction_x": round(old_checks / new_checks, 2),
            "before_ms": before_ms, "after_ms": after_ms,
            "latency_speedup_x": round(before_ms / max(.000001, after_ms), 2),
            "kind": "complexity model of the exact grouping algorithms"}


def archive_bench(root, operations=100):
    archive = UnifiedArchive(root, "bench")
    # Build once so both cases benchmark steady-state transaction overhead.
    with archive.db() as db:
        db.execute("SELECT 1").fetchone()
    _ArchiveConnections.close_all()

    def before():
        for _ in range(operations):
            connection = archive._connect()
            try:
                connection.execute("SELECT 1").fetchone()
                connection.commit()
            finally:
                connection.close()

    def after():
        for _ in range(operations):
            with archive.db() as db:
                db.execute("SELECT 1").fetchone()

    before_ms = median_ms(before, repeats=7)
    after_ms = median_ms(after, repeats=7)
    with archive.db() as db:
        synchronous = db.execute("PRAGMA synchronous").fetchone()[0]
        schema_version = db.execute("PRAGMA user_version").fetchone()[0]
    _ArchiveConnections.close_all()
    return {"transactions": operations, "before_connections": operations,
            "after_connections": 1, "before_ms": before_ms, "after_ms": after_ms,
            "latency_speedup_x": round(before_ms / max(.000001, after_ms), 2),
            "synchronous": synchronous, "synchronous_name": "FULL" if synchronous == 2 else "unexpected",
            "schema_version": schema_version}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", default="100,1000,5000")
    parser.add_argument("--payload-bytes", type=int, default=2048)
    parser.add_argument("--output")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="desk-session-bench-") as tmp:
        root = Path(tmp)
        result = {
            "benchmark": "session-storage-performance",
            "base_commit": BASE_COMMIT,
            "environment": {"python": platform.python_version(), "machine": platform.machine(),
                            "platform": platform.platform()},
            "method": {"samples": "median; history=21, archive=7",
                       "data": "temporary synthetic JSONL/SQLite only",
                       "history_after": "shipped JsonlTurnIndex.raw_window",
                       "archive_after": "shipped UnifiedArchive.db connection pool",
                       "sidebar": "complexity model; not browser runtime"},
            "history": [history_bench(root, int(turns), args.payload_bytes)
                        for turns in args.turns.split(",")],
            "sidebar": sidebar_bench(),
            "archive": archive_bench(root),
        }
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
