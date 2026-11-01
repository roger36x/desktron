"""Aggregate privacy-safe performance telemetry without exporting per-session identifiers."""
from __future__ import annotations

import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import argparse
from collections import defaultdict
import json
import math
import os
from pathlib import Path
import re
import stat

MIN_PERCENTILE_SAMPLES = 5
SAFE_LABEL = re.compile(r"[A-Za-z0-9._:/+\-]{1,160}\Z")
PHASES = {
    "stored_to_grounded_ms": ("stored_ms", "grounded_ms"),
    "grounded_to_prepared_ms": ("grounded_ms", "prepared_ms"),
    "prepared_to_submitted_ms": ("prepared_ms", "submitted_ms"),
    "submitted_to_first_event_ms": ("submitted_ms", "first_event_ms"),
    "submitted_to_first_text_ms": ("submitted_ms", "first_text_ms"),
    "send_to_first_event_ms": (None, "first_event_ms"),
    "send_to_first_text_ms": (None, "first_text_ms"),
    "send_to_ready_ms": (None, "ready_ms"),
}


def _percentile(values, percentile):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank); upper = math.ceil(rank)
    if lower == upper:
        return round(ordered[lower], 2)
    return round(ordered[lower] * (upper - rank) + ordered[upper] * (rank - lower), 2)


def _summary(values, eligible):
    result = {"n": len(values), "missing": eligible - len(values),
              "sufficient": len(values) >= MIN_PERCENTILE_SAMPLES}
    if values:
        result.update(p50=_percentile(values, 0.50), p95=_percentile(values, 0.95),
                      p99=_percentile(values, 0.99))
    return result


def _request_ratio(row):
    denominator = int(row.get("input") or 0) + int(row.get("cache_read") or 0)
    return int(row.get("cache_read") or 0) / denominator if denominator > 0 else None


def _strict_requests(requests):
    identities = [(row.get("request_epoch_fp"), row.get("request_seq")) for row in requests]
    if not requests or any(not isinstance(epoch, str) or len(epoch) != 32
                           or isinstance(seq, bool) or not isinstance(seq, int) or seq < 1
                           for epoch, seq in identities):
        return []
    if len(set(identities)) != len(identities):
        return []
    epochs = {epoch for epoch, _ in identities}
    sequences = sorted(seq for _, seq in identities)
    contiguous = sequences == list(range(sequences[0], sequences[0] + len(requests)))
    return requests if len(epochs) == 1 and contiguous else []


def _cache_stats(requests):
    strict = _strict_requests(requests)
    ratios = [ratio for ratio in map(_request_ratio, strict) if ratio is not None]
    continuations = [ratio for ratio in map(_request_ratio, strict[1:]) if ratio is not None]
    initial_misses = 0
    for ratio in ratios:
        if ratio > 0:
            break
        initial_misses += 1
    return {
        "observed_requests": len(requests),
        "eligible_requests": len(ratios),
        "miss_requests": sum(ratio == 0 for ratio in ratios),
        "hit90_requests": sum(ratio >= 0.9 for ratio in ratios),
        "mean_ratio": round(sum(ratios) / len(ratios), 4) if ratios else None,
        "first_ratio": round(ratios[0], 4) if ratios else None,
        "strictly_linked_requests": len(strict),
        "continuation_eligible": len(continuations),
        "continuation_hit90_rate": (round(sum(ratio >= 0.9 for ratio in continuations)
                                                   / len(continuations), 4)
                                     if continuations else None),
        "initial_consecutive_misses": initial_misses,
    }


def _cache_class(requests):
    stats = _cache_stats(requests)
    if not stats["eligible_requests"]:
        return "unknown"
    if stats["miss_requests"] == stats["eligible_requests"]:
        return "miss"
    return "hit90" if stats["mean_ratio"] >= 0.9 else "partial"


def _first_event(timing):
    values = [timing.get(name) for name in ("first_event_ms", "first_reasoning_ms",
              "first_text_ms", "first_tool_ms") if timing.get(name) is not None]
    return min(values) if values else None


def _uniform(requests, field, default="unknown"):
    values = {row.get(field) for row in requests if isinstance(row.get(field), str)
              and row.get(field) not in (None, "")}
    return next(iter(values)) if len(values) == 1 else "mixed" if values else default


def _identity_mode(requests):
    if not requests or any(row.get("identity_mode") not in ("legacy", "native") for row in requests):
        return "unknown"
    values = {row["identity_mode"] for row in requests}
    return next(iter(values)) if len(values) == 1 else "mixed"


def load_turns(root, integrity=None):
    root = Path(root)
    integrity = integrity if integrity is not None else {"files": 0, "rows": 0,
        "corrupt_rows": 0, "invalid_rows": 0,
        "unlinked_usage_turns": 0, "timing_without_usage": 0, "unknown_identity_turns": 0}
    turns = []
    for engine_dir in sorted(root.glob("*")):
        if not engine_dir.is_dir() or engine_dir.is_symlink():
            continue
        engine = engine_dir.name
        sessions = defaultdict(list)
        for path in sorted(engine_dir.glob("*.jsonl")):
            if not path.is_symlink():
                sessions[path.name.split('.', 1)[0]].append(path)
        for paths in sessions.values():
            grouped = defaultdict(lambda: {"usage": [], "timing": None})
            for path in paths:
                integrity["files"] += 1
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    integrity["corrupt_rows"] += 1
                    continue
                for line in lines:
                    try:
                        row = json.loads(line)
                    except (TypeError, ValueError):
                        integrity["corrupt_rows"] += 1
                        continue
                    integrity["rows"] += 1
                    if not isinstance(row, dict):
                        integrity["invalid_rows"] += 1
                        continue
                    seq = row.get("seq")
                    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
                        integrity["invalid_rows"] += 1
                        continue
                    if row.get("event") == "pi_usage":
                        required = ("input", "cache_read", "total")
                        if any(isinstance(row.get(key), bool) or not isinstance(row.get(key), int)
                               or not 0 <= row[key] <= 10**9 for key in required):
                            integrity["invalid_rows"] += 1
                            continue
                        request_seq = row.get("request_seq")
                        if request_seq is not None and (isinstance(request_seq, bool)
                                or not isinstance(request_seq, int) or request_seq < 1):
                            integrity["invalid_rows"] += 1
                            continue
                        if any(label in row and (not isinstance(row[label], str)
                                or not SAFE_LABEL.fullmatch(row[label]))
                               for label in ("provider", "model")):
                            integrity["invalid_rows"] += 1
                            continue
                        if row.get("identity_mode") not in (None, "legacy", "native"):
                            integrity["invalid_rows"] += 1
                            continue
                        grouped[seq]["usage"].append(row)
                    elif row.get("event") == "response_timing":
                        timing_fields = {field for pair in PHASES.values() for field in pair if field}
                        timing_fields.update({"first_reasoning_ms", "first_tool_ms", "resident_ms",
                                              "engine_done_ms", "terminal_ms", "committed_ms"})
                        stage_values = [row[name] for name in timing_fields if name in row]
                        if (row.get("outcome") not in ("completed", "cancelled", "error")
                                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                                       or not math.isfinite(value) or value < 0 or value > 10**9
                                       for value in stage_values)):
                            integrity["invalid_rows"] += 1
                            continue
                        grouped[seq]["timing"] = row
            for data in grouped.values():
                timing = data["timing"]
                if not timing:
                    integrity["unlinked_usage_turns"] += bool(data["usage"])
                    continue
                requests = sorted(data["usage"], key=lambda row: row.get("request_seq", 0))
                if not requests:
                    integrity["timing_without_usage"] += 1
                if timing.get("first_event_ms") is None:
                    timing = {**timing, "first_event_ms": _first_event(timing)}
                phases = {}
                for name, (start, end) in PHASES.items():
                    if timing.get(end) is None or start and timing.get(start) is None:
                        continue
                    value = float(timing[end]) - (float(timing[start]) if start else 0.0)
                    if value >= 0:
                        phases[name] = round(value, 2)
                resident_values = {row.get("resident_reused") for row in requests
                                   if isinstance(row.get("resident_reused"), bool)}
                resident = ("warm" if resident_values == {True} else "cold"
                            if resident_values == {False} else "mixed"
                            if len(resident_values) > 1 else "unknown")
                provider = _uniform(requests, "provider")
                identity_mode = _identity_mode(requests)
                turns.append({
                    "engine": engine, "provider": provider,
                    "model": _uniform(requests, "model"),
                    "identity_mode": identity_mode,
                    "cache": _cache_class(requests),
                    "resident": resident, "outcome": timing.get("outcome") or "unknown",
                    "request_count": len(_strict_requests(requests)),
                    "observed_request_count": len(requests), "cache_stats": _cache_stats(requests),
                    "phases": phases,
                })
    return turns


def aggregate(root):
    integrity = {"files": 0, "rows": 0, "corrupt_rows": 0, "invalid_rows": 0,
                 "unlinked_usage_turns": 0, "timing_without_usage": 0,
                 "unknown_identity_turns": 0}
    turns = load_turns(root, integrity)
    groups = defaultdict(list)
    for turn in turns:
        key = (turn["engine"], turn["provider"], turn["model"], turn["identity_mode"],
               turn["cache"], turn["resident"], turn["outcome"])
        groups[key].append(turn)
    output = []
    for key, rows in sorted(groups.items()):
        phase_names = sorted(PHASES)
        cache = [row["cache_stats"] for row in rows]
        eligible = sum(item["eligible_requests"] for item in cache)
        continuation_eligible = sum(item["continuation_eligible"] for item in cache)
        continuation_hits = sum(round(item["continuation_hit90_rate"] * item["continuation_eligible"])
                                for item in cache if item["continuation_hit90_rate"] is not None)
        output.append({
            "engine": key[0], "provider": key[1], "model": key[2], "identity_mode": key[3],
            "cache": key[4], "resident": key[5], "outcome": key[6], "turns": len(rows),
            "provider_requests": sum(row["request_count"] for row in rows),
            "observed_requests": sum(row["observed_request_count"] for row in rows),
            "cache_metrics": {
                "eligible_requests": eligible,
                "strictly_linked_requests": sum(item["strictly_linked_requests"] for item in cache),
                "miss_requests": sum(item["miss_requests"] for item in cache),
                "hit90_requests": sum(item["hit90_requests"] for item in cache),
                "continuation_eligible": continuation_eligible,
                "continuation_hit90_rate": (round(continuation_hits / continuation_eligible, 4)
                                             if continuation_eligible else None),
                "max_initial_consecutive_misses": max(
                    (item["initial_consecutive_misses"] for item in cache), default=0),
            },
            "phases": {name: _summary([row["phases"][name] for row in rows
                                       if name in row["phases"]], len(rows))
                       for name in phase_names},
        })
    return {"schema": "desk-performance-report-v1", "turns": len(turns),
            "minimum_percentile_samples": MIN_PERCENTILE_SAMPLES,
            "integrity": integrity, "groups": output}


def _private_write(path, content):
    path = Path(path).absolute()
    for ancestor in reversed(path.parent.parents):
        if ancestor.exists() and ancestor.is_symlink():
            raise ValueError("unsafe report directory")
    if path.parent.exists() and path.parent.is_symlink():
        raise ValueError("unsafe report directory")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for ancestor in path.parent.parents:
        if ancestor.is_symlink():
            raise ValueError("unsafe report directory")
    info = path.parent.lstat()
    if path.parent.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("unsafe report directory")
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC |
                 getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Aggregate Desk performance metadata")
    parser.add_argument("root", help="diagnostics/performance directory")
    parser.add_argument("-o", "--output")
    args = parser.parse_args(argv)
    result = json.dumps(aggregate(args.root), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        _private_write(args.output, result)
    else:
        print(result, end="")


if __name__ == "__main__":
    main()
