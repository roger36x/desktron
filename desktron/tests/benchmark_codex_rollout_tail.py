#!/usr/bin/env python3
"""Repeatable Codex rollout tail benchmark.

Usage:
  python3 desktron/tests/benchmark_codex_rollout_tail.py /path/to/rollout.jsonl

Only timings, counts, and a semantic hash are printed; message contents stay local.
"""
import argparse
import collections
import hashlib
import json
import os
import resource
import sys
import tempfile
import time


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea
    import engine_adapters.codex_rollout as rollout_mod
finally:
    sys.argv = _argv


class Bus:
    def publish(self, _event):
        pass


def _engine_sid(path):
    stem = os.path.basename(path).removesuffix(".jsonl")
    return "-".join(stem.rsplit("-", 5)[-5:])


def _rss_mb():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / (1024 * 1024) if sys.platform == "darwin" else value / 1024


def run(path, want, runs):
    size = os.path.getsize(path)
    esid = _engine_sid(path)
    with tempfile.TemporaryDirectory() as tmp:
        adapter = ea.CodexAdapter(tmp, tmp, Bus())
        sid = "adp_rollout_benchmark"
        adapter.index[sid] = {"id": sid, "engine_sid": esid}
        marks = want + want // 2 + 4
        for number in range(1, runs + 1):
            parse_ns = 0
            parse_bytes = 0
            parse_calls = 0
            original_loads = rollout_mod.json.loads

            def timed_loads(value, *args, **kwargs):
                nonlocal parse_ns, parse_bytes, parse_calls
                started = time.perf_counter_ns()
                try:
                    return original_loads(value, *args, **kwargs)
                finally:
                    parse_ns += time.perf_counter_ns() - started
                    parse_bytes += len(value)
                    parse_calls += 1

            started = time.perf_counter_ns()
            offset = adapter._rollout_window_start(path, size, marks)
            located = time.perf_counter_ns()
            rollout_mod.json.loads = timed_loads
            try:
                messages = adapter._group_rollout(path, sid, offset) or []
            finally:
                rollout_mod.json.loads = original_loads
            finished = time.perf_counter_ns()
            encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8")
            roles = collections.Counter((m.get("info") or {}).get("role") for m in messages)
            group_ns = finished - located
            print(json.dumps({
                "run": number,
                "file_mb": round(size / 1_000_000, 2),
                "window_mb": round((size - offset) / 1_000_000, 2),
                "locate_ms": round((located - started) / 1_000_000, 2),
                "parse_ms": round(parse_ns / 1_000_000, 2),
                "project_ms": round((group_ns - parse_ns) / 1_000_000, 2),
                "total_ms": round((finished - started) / 1_000_000, 2),
                "json_loads": parse_calls,
                "parsed_mb": round(parse_bytes / 1_000_000, 2),
                "messages": len(messages),
                "roles": dict(roles),
                "semantic_sha256": hashlib.sha256(encoded).hexdigest(),
                "process_max_rss_mb": round(_rss_mb(), 2),
            }, ensure_ascii=False, sort_keys=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--want", type=int, default=25)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    run(os.path.abspath(args.path), max(0, args.want), max(1, args.runs))


if __name__ == "__main__":
    main()
