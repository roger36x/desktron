"""Local-only, incremental token usage accounting.

New turns are written to a small SQLite WAL ledger and daily rollups in the same
transaction.  The settings page reads SQL aggregates only; retained chat JSONL
is scanned once for a resumable historical import and is never used as the hot
query path.

Only scalar accounting metadata is stored. Prompts, messages, tool arguments,
file paths, credentials and session titles never enter this database.
"""
from __future__ import annotations

import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time


RETENTION_DAYS = 370
# A single provider request can legitimately be large, especially in an agent
# loop. Values above this guard are retained for diagnostics but excluded from
# trusted and historical totals until a provider-specific proof says otherwise.
MAX_TURN_TOKENS = 50_000_000
_SCHEMA_VERSION = "2"
_MIGRATION_KEY = "legacy_jsonl_v2"
_SID = re.compile(r"[A-Za-z0-9_-]{3,100}\Z")
_LABEL = re.compile(r"[A-Za-z0-9._:/+ -]{1,160}\Z")
_DOMAIN = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
_LOCK = threading.RLock()
_QUALITY = ("measured", "historical", "anomalous")


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= 10**12 else None


def _ordinal(value):
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= 10**15 else None


def normalize_usage(value):
    """Return the stable public accounting shape; omit unknown detail fields."""
    if not isinstance(value, dict):
        return None
    total = _number(value.get("total"))
    if total is None:
        total = _number(value.get("tokens"))
    cached = _number(value.get("cached"))
    if cached is None:
        cached = _number(value.get("cache_read"))
    out = {"total": int(total or 0), "cached": int(cached or 0)}
    for key in ("input", "output", "cache_write"):
        item = _number(value.get(key))
        if item is not None:
            out[key] = item
    if not any(out.values()):
        return None
    return out


def _usage_root(data_dir):
    return Path(data_dir) / "usage"


def _legacy_root(data_dir):
    return _usage_root(data_dir) / "sessions"


def _db_path(data_dir):
    return _usage_root(data_dir) / "usage.sqlite3"


def _legacy_name(sid):
    return hashlib.sha256(str(sid).encode("utf-8")).hexdigest() + ".jsonl"


def _private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or info.st_uid != os.getuid():
        raise ValueError("unsafe usage directory")
    os.chmod(path, 0o700)


@contextmanager
def _connect(data_dir):
    root = _usage_root(data_dir)
    _private_directory(root)
    path = _db_path(data_dir)
    if path.exists() and path.is_symlink():
        raise ValueError("unsafe usage database")
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS turns (
                sid TEXT NOT NULL,
                seq INTEGER NOT NULL,
                at INTEGER NOT NULL,
                day TEXT NOT NULL,
                domain TEXT NOT NULL,
                engine TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                total INTEGER NOT NULL,
                input INTEGER,
                output INTEGER,
                cached INTEGER NOT NULL DEFAULT 0,
                cache_write INTEGER NOT NULL DEFAULT 0,
                detail INTEGER NOT NULL DEFAULT 0,
                quality TEXT NOT NULL,
                source TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (sid, seq),
                CHECK (quality IN ('measured', 'historical', 'anomalous'))
            );
            CREATE INDEX IF NOT EXISTS turns_day ON turns(day);
            CREATE INDEX IF NOT EXISTS turns_domain_day ON turns(domain, day);
            CREATE INDEX IF NOT EXISTS turns_engine_day ON turns(engine, day);
            CREATE TABLE IF NOT EXISTS daily (
                day TEXT NOT NULL,
                domain TEXT NOT NULL,
                engine TEXT NOT NULL,
                quality TEXT NOT NULL,
                total INTEGER NOT NULL DEFAULT 0,
                input INTEGER NOT NULL DEFAULT 0,
                output INTEGER NOT NULL DEFAULT 0,
                cached INTEGER NOT NULL DEFAULT 0,
                cache_write INTEGER NOT NULL DEFAULT 0,
                turns INTEGER NOT NULL DEFAULT 0,
                detail_turns INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, domain, engine, quality),
                CHECK (quality IN ('measured', 'historical', 'anomalous'))
            );
            CREATE TABLE IF NOT EXISTS sidecar (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at INTEGER NOT NULL,
                day TEXT NOT NULL,
                kind TEXT NOT NULL,
                sid TEXT NOT NULL DEFAULT '',
                domain TEXT NOT NULL DEFAULT '',
                engine TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                input INTEGER NOT NULL DEFAULT 0,
                output INTEGER NOT NULL DEFAULT 0,
                cached INTEGER NOT NULL DEFAULT 0,
                cache_write INTEGER NOT NULL DEFAULT 0,
                outcome TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS sidecar_kind_day ON sidecar(kind, day);
            CREATE TABLE IF NOT EXISTS migration_files (
                name TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                imported_at INTEGER NOT NULL
            );
        """)
        db.execute("INSERT INTO meta(key,value) VALUES('schema_version',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_SCHEMA_VERSION,))
        db.commit()
        os.chmod(path, 0o600)
        # Connection.__exit__ commits/rolls back but does not close the handle.
        # Always release it, including query and transaction failures.
        with db:
            yield db
    finally:
        db.close()


def _meta_get(db, key, default=""):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def _meta_set(db, key, value):
    db.execute("INSERT INTO meta(key,value) VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def _day(at):
    return datetime.fromtimestamp(int(at) / 1000).astimezone().date().isoformat()


def _quality_for(usage, requested):
    requested = requested if requested in _QUALITY else "measured"
    if usage["total"] > MAX_TURN_TOKENS or usage.get("cached", 0) > usage["total"]:
        return "anomalous"
    return requested


def _row_values(row):
    return (
        row["day"], row["domain"], row["engine"], row["quality"], row["total"],
        int(row.get("input") or 0), int(row.get("output") or 0), row["cached"],
        int(row.get("cache_write") or 0), 1, int(row.get("detail") or 0),
    )


def _daily_apply(db, row, sign):
    values = list(_row_values(row))
    for index in range(4, len(values)):
        values[index] *= sign
    db.execute("""
        INSERT INTO daily(day,domain,engine,quality,total,input,output,cached,cache_write,turns,detail_turns)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(day,domain,engine,quality) DO UPDATE SET
          total=total+excluded.total,
          input=input+excluded.input,
          output=output+excluded.output,
          cached=cached+excluded.cached,
          cache_write=cache_write+excluded.cache_write,
          turns=turns+excluded.turns,
          detail_turns=detail_turns+excluded.detail_turns
    """, values)
    db.execute("DELETE FROM daily WHERE turns<=0")


def _turn_dict(sid, *, at, seq, engine, model, domain, usage, quality, source, deleted=0):
    clean = normalize_usage(usage)
    at, seq = _ordinal(at), _ordinal(seq)
    if not clean or not at or not seq or not _SID.fullmatch(str(sid or "")):
        return None
    engine, model = str(engine or "unknown"), str(model or "")
    domain = str(domain or "_desktop")
    if not _LABEL.fullmatch(engine) or (model and not _LABEL.fullmatch(model)):
        return None
    if not _DOMAIN.fullmatch(domain):
        domain = "_desktop"
    detail = int("input" in clean and "output" in clean)
    return {
        "sid": str(sid), "seq": seq, "at": at, "day": _day(at), "domain": domain,
        "engine": engine, "model": model, "total": clean["total"],
        "input": clean.get("input"), "output": clean.get("output"),
        "cached": clean.get("cached", 0), "cache_write": clean.get("cache_write", 0),
        "detail": detail, "quality": _quality_for(clean, quality),
        "source": str(source or "unknown")[:80], "deleted": int(bool(deleted)),
    }


def _upsert_turn(db, row):
    old = db.execute("SELECT * FROM turns WHERE sid=? AND seq=?",
                     (row["sid"], row["seq"])).fetchone()
    if old and old["quality"] == "measured" and row["quality"] != "measured":
        return False
    if old:
        _daily_apply(db, dict(old), -1)
    db.execute("""
        INSERT INTO turns(sid,seq,at,day,domain,engine,model,total,input,output,cached,
                          cache_write,detail,quality,source,deleted)
        VALUES(:sid,:seq,:at,:day,:domain,:engine,:model,:total,:input,:output,:cached,
               :cache_write,:detail,:quality,:source,:deleted)
        ON CONFLICT(sid,seq) DO UPDATE SET
          at=excluded.at, day=excluded.day, domain=excluded.domain,
          engine=excluded.engine, model=excluded.model, total=excluded.total,
          input=excluded.input, output=excluded.output, cached=excluded.cached,
          cache_write=excluded.cache_write, detail=excluded.detail,
          quality=excluded.quality, source=excluded.source,
          deleted=MAX(turns.deleted, excluded.deleted)
    """, row)
    _daily_apply(db, row, 1)
    return True


def record_turn(data_dir, sid, *, at, seq, engine, model="", domain="_desktop", usage=None):
    """Idempotently record one measured user-turn usage row.

    The `(sid, seq)` key prevents retries, duplicate terminal events and process
    reconciliation from charging the same Desk turn twice.
    """
    row = _turn_dict(sid, at=at, seq=seq, engine=engine, model=model, domain=domain,
                     usage=usage, quality="measured", source="live_v2")
    if not row:
        return False
    try:
        with _LOCK, _connect(data_dir) as db:
            _upsert_turn(db, row)
        return True
    except (OSError, ValueError, sqlite3.Error):
        return False


_KIND = re.compile(r"[a-z][a-z0-9_-]{0,40}\Z")


def record_sidecar(data_dir, kind, *, engine="", model="", sid="", domain="", usage=None,
                   outcome="", at=None):
    """Record one model call made beside the user's turns (recap, memory rebuild).

    Sidecar calls are kept out of `turns` and the daily rollups: they have no
    Desk turn sequence and must not be counted as a user turn.  `input` here is
    the uncached input only; `cached` and `cache_write` are reported separately
    so the cache hit ratio can be measured directly.  Numbers only.
    """
    if not _KIND.fullmatch(str(kind or "")):
        return False
    usage = usage if isinstance(usage, dict) else {}
    values = {}
    for key in ("input", "output", "cached", "cache_write"):
        number = _number(usage.get(key))
        values[key] = int(number or 0)
    at = _ordinal(at) if at is not None else int(time.time())
    if at is None:
        return False
    label = lambda value, pattern: str(value or "")[:160] if pattern.fullmatch(str(value or "")) else ""
    row = {"at": at, "day": datetime.fromtimestamp(at).strftime("%Y-%m-%d"), "kind": str(kind),
           "sid": str(sid) if _SID.fullmatch(str(sid or "")) else "",
           "domain": label(domain, _DOMAIN), "engine": label(engine, _LABEL),
           "model": label(model, _LABEL), "outcome": label(outcome, _KIND), **values}
    try:
        with _LOCK, _connect(data_dir) as db:
            db.execute("""
                INSERT INTO sidecar(at,day,kind,sid,domain,engine,model,input,output,cached,
                                    cache_write,outcome)
                VALUES(:at,:day,:kind,:sid,:domain,:engine,:model,:input,:output,:cached,
                       :cache_write,:outcome)
            """, row)
        return True
    except (OSError, ValueError, sqlite3.Error):
        return False


def sidecar_summary(data_dir, since=0):
    """Per kind/engine/model totals and cache-read ratio since a timestamp."""
    try:
        with _LOCK, _connect(data_dir) as db:
            rows = db.execute("""
                SELECT kind, engine, model, outcome, COUNT(*) AS calls, SUM(input) AS input,
                       SUM(output) AS output, SUM(cached) AS cached,
                       SUM(cache_write) AS cache_write
                FROM sidecar WHERE at>=? GROUP BY kind, engine, model, outcome
                ORDER BY kind, engine, model, outcome
            """, (int(since or 0),)).fetchall()
    except (OSError, ValueError, sqlite3.Error):
        return []
    out = []
    for row in rows:
        item = dict(row)
        read_side = int(item["input"] or 0) + int(item["cached"] or 0) + int(item["cache_write"] or 0)
        item["cache_ratio"] = round(int(item["cached"] or 0) / read_side, 4) if read_side else None
        out.append(item)
    return out


def delete_session(data_dir, sid):
    """Forget session attribution while retaining anonymous aggregate usage."""
    if not _SID.fullmatch(str(sid or "")):
        return
    try:
        with _LOCK, _connect(data_dir) as db:
            db.execute("UPDATE turns SET deleted=1, model='' WHERE sid=?", (str(sid),))
        legacy = _legacy_root(data_dir) / _legacy_name(sid)
        if not legacy.is_symlink():
            legacy.unlink(missing_ok=True)
    except (OSError, ValueError, sqlite3.Error):
        pass


def _json(path, fallback):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, TypeError, ValueError):
        return fallback


def _catalog(data_dir):
    sessions = {}
    adapter_root = Path(data_dir) / "adapter"
    try:
        engine_dirs = list(adapter_root.iterdir())
    except OSError:
        engine_dirs = []
    for directory in engine_dirs:
        if not directory.is_dir() or directory.is_symlink():
            continue
        index = _json(directory / "sessions.json", {})
        if not isinstance(index, dict):
            continue
        for sid, raw in index.items():
            if not _SID.fullmatch(str(sid or "")) or not isinstance(raw, dict):
                continue
            sessions[str(sid)] = {
                "id": str(sid),
                "title": str(raw.get("title") or raw.get("desk_name_fallback") or "新会话")[:160],
                "domain": str(raw.get("domain") or "_desktop")[:100],
                "default_engine": directory.name,
                "path": directory / (str(sid) + ".jsonl"),
            }
    return sessions


def _legacy_projection(session):
    out = {}
    path = session["path"]
    try:
        if path.is_symlink():
            return out
        stream = open(path, encoding="utf-8")
    except OSError:
        return out
    with stream:
        for line in stream:
            try:
                info = (json.loads(line).get("info") or {})
            except (AttributeError, TypeError, ValueError):
                continue
            if info.get("role") != "assistant" or not isinstance(info.get("usage"), dict):
                continue
            row = _turn_dict(session["id"], at=info.get("done_at") or info.get("at"),
                seq=info.get("seq"), engine=info.get("engine") or session["default_engine"],
                model=info.get("model") or "", domain=session["domain"], usage=info.get("usage"),
                quality="historical", source="projection_v1")
            if row:
                out[row["seq"]] = row
    return out


def _legacy_ledger(data_dir, session):
    out = {}
    path = _legacy_root(data_dir) / _legacy_name(session["id"])
    try:
        if path.is_symlink():
            return out
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            raw = json.loads(line)
        except (TypeError, ValueError):
            continue
        row = _turn_dict(session["id"], at=raw.get("at"), seq=raw.get("seq"),
            engine=raw.get("engine") or session["default_engine"], model=raw.get("model") or "",
            domain=session["domain"], usage=raw.get("usage"), quality="historical",
            source="ledger_v1") if isinstance(raw, dict) else None
        if row:
            out[row["seq"]] = row
    return out


def _fingerprint(paths):
    parts = []
    for path in paths:
        try:
            info = path.stat()
            parts.append(f"{path.name}:{info.st_size}:{info.st_mtime_ns}")
        except OSError:
            parts.append(f"{path.name}:missing")
    return "|".join(parts)


def _migrate_legacy(data_dir, db, sessions):
    if _meta_get(db, _MIGRATION_KEY) == "done":
        return False
    cutoff = int((time.time() - RETENTION_DAYS * 86400) * 1000)
    for sid, session in sessions.items():
        projection = session["path"]
        ledger = _legacy_root(data_dir) / _legacy_name(sid)
        fingerprint = _fingerprint((projection, ledger))
        checkpoint = db.execute("SELECT fingerprint FROM migration_files WHERE name=?",
                                (sid,)).fetchone()
        if checkpoint and checkpoint[0] == fingerprint:
            continue
        chosen = _legacy_projection(session)
        chosen.update(_legacy_ledger(data_dir, session))  # durable v1 ledger wins
        with db:
            for row in chosen.values():
                if row["at"] >= cutoff:
                    _upsert_turn(db, row)
            db.execute("INSERT INTO migration_files(name,fingerprint,imported_at) VALUES(?,?,?) "
                       "ON CONFLICT(name) DO UPDATE SET fingerprint=excluded.fingerprint, "
                       "imported_at=excluded.imported_at",
                       (sid, fingerprint, int(time.time() * 1000)))
    with db:
        _meta_set(db, _MIGRATION_KEY, "done")
        _meta_set(db, "coverage_started_at", int(time.time() * 1000))
    return True


def _rebuild_daily(db):
    db.execute("DELETE FROM daily")
    db.execute("""
        INSERT INTO daily(day,domain,engine,quality,total,input,output,cached,cache_write,turns,detail_turns)
        SELECT day,domain,engine,quality,SUM(total),SUM(COALESCE(input,0)),
               SUM(COALESCE(output,0)),SUM(cached),SUM(cache_write),COUNT(*),SUM(detail)
        FROM turns GROUP BY day,domain,engine,quality
    """)


def _reconcile_daily(db, today):
    if _meta_get(db, "last_reconcile_day") == today:
        return True
    cutoff = int((time.time() - RETENTION_DAYS * 86400) * 1000)
    with db:
        db.execute("DELETE FROM turns WHERE at<?", (cutoff,))
        turn = db.execute("SELECT COUNT(*),COALESCE(SUM(total),0),COALESCE(SUM(cached),0) FROM turns").fetchone()
        daily = db.execute("SELECT COALESCE(SUM(turns),0),COALESCE(SUM(total),0),"
                           "COALESCE(SUM(cached),0) FROM daily").fetchone()
        ok = tuple(turn) == tuple(daily)
        if not ok:
            _rebuild_daily(db)
        _meta_set(db, "last_reconcile_day", today)
        _meta_set(db, "last_reconcile_ok", "1")
    return True


def _empty():
    return {"total": 0, "input": 0, "output": 0, "cached": 0,
            "cache_write": 0, "turns": 0, "detail_turns": 0}


def _add(target, source):
    for key in target:
        target[key] += int(source.get(key) or 0)
    return target


def _public_one(stats):
    out = dict(stats)
    out["cache_rate"] = round(out["cached"] / out["total"], 4) if out["total"] else None
    out["detail_coverage"] = round(out["detail_turns"] / out["turns"], 4) if out["turns"] else 0
    return out


def _bundle():
    return {quality: _empty() for quality in _QUALITY}


def _bundle_add(bundle, quality, row):
    if quality in bundle:
        _add(bundle[quality], row)


def _bundle_merge(target, source):
    for quality in _QUALITY:
        _add(target[quality], source[quality])
    return target


def _public(bundle):
    measured = _public_one(bundle["measured"])
    historical = _public_one(bundle["historical"])
    anomalous = _public_one(bundle["anomalous"])
    return {
        **measured,
        "historical_total": historical["total"],
        "historical_input": historical["input"],
        "historical_output": historical["output"],
        "historical_cached": historical["cached"],
        "historical_cache_write": historical["cache_write"],
        "historical_turns": historical["turns"],
        "historical_detail_turns": historical["detail_turns"],
        "historical_cache_rate": historical["cache_rate"],
        "historical_detail_coverage": historical["detail_coverage"],
        "recorded_total": measured["total"] + historical["total"],
        "recorded_turns": measured["turns"] + historical["turns"],
        "anomalous_total": anomalous["total"],
        "anomalous_turns": anomalous["turns"],
    }


def _stats_from_sql(row):
    return {key: int(row[key] or 0) for key in
            ("total", "input", "output", "cached", "cache_write", "turns", "detail_turns")}


def aggregate(data_dir, domain_names=None, days=365, now=None):
    started = time.perf_counter()
    days = days if days in (30, 90, 365) else 365
    now_dt = datetime.fromtimestamp(now).astimezone() if now is not None else datetime.now().astimezone()
    end = now_dt.date()
    start = end - timedelta(days=days - 1)
    sessions = _catalog(data_dir)
    migrated = False
    with _LOCK, _connect(data_dir) as db:
        migrated = _migrate_legacy(data_dir, db, sessions)
        _reconcile_daily(db, end.isoformat())
        daily_rows = db.execute("""
            SELECT day,quality,SUM(total) total,SUM(input) input,SUM(output) output,
                   SUM(cached) cached,SUM(cache_write) cache_write,SUM(turns) turns,
                   SUM(detail_turns) detail_turns
            FROM daily WHERE day BETWEEN ? AND ? GROUP BY day,quality
        """, (start.isoformat(), end.isoformat())).fetchall()
        first = db.execute("SELECT MIN(day) FROM turns WHERE quality!='anomalous'").fetchone()[0]
        grouped = db.execute("""
            SELECT sid,domain,engine,model,quality,MAX(at) last_at,MAX(deleted) deleted,
                   SUM(total) total,SUM(COALESCE(input,0)) input,
                   SUM(COALESCE(output,0)) output,SUM(cached) cached,
                   SUM(cache_write) cache_write,COUNT(*) turns,SUM(detail) detail_turns
            FROM turns WHERE day BETWEEN ? AND ?
            GROUP BY sid,domain,engine,model,quality
        """, (start.isoformat(), end.isoformat())).fetchall()
        per_day = db.execute("""
            SELECT sid,day,quality,SUM(total) total,SUM(COALESCE(input,0)) input,
                   SUM(COALESCE(output,0)) output,SUM(cached) cached,
                   SUM(cache_write) cache_write,COUNT(*) turns,SUM(detail) detail_turns
            FROM turns WHERE day BETWEEN ? AND ? GROUP BY sid,day,quality
        """, (start.isoformat(), end.isoformat())).fetchall()
        archived_count = db.execute("""
            SELECT COUNT(DISTINCT sid) FROM turns
            WHERE day BETWEEN ? AND ? AND deleted=1 AND quality!='anomalous'
        """, (start.isoformat(), end.isoformat())).fetchone()[0]
        migration_done = _meta_get(db, _MIGRATION_KEY) == "done"
        reconcile_ok = _meta_get(db, "last_reconcile_ok") == "1"

    daily = defaultdict(_bundle)
    for row in daily_rows:
        _bundle_add(daily[row["day"]], row["quality"], _stats_from_sql(row))

    session_bundles = defaultdict(_bundle)
    session_daily = defaultdict(lambda: defaultdict(_bundle))
    session_domains = defaultdict(lambda: defaultdict(int))
    session_engines = defaultdict(lambda: defaultdict(int))
    session_models = defaultdict(lambda: defaultdict(int))
    session_deleted = defaultdict(bool)
    last_at = defaultdict(int)
    for row in grouped:
        stats = _stats_from_sql(row)
        sid, quality = row["sid"], row["quality"]
        _bundle_add(session_bundles[sid], quality, stats)
        weight = stats["total"] if quality != "anomalous" else 0
        session_domains[sid][row["domain"]] += weight
        session_engines[sid][row["engine"]] += weight
        if row["model"]:
            session_models[sid][row["model"]] += weight
        session_deleted[sid] = session_deleted[sid] or bool(row["deleted"])
        last_at[sid] = max(last_at[sid], int(row["last_at"] or 0))
    for row in per_day:
        _bundle_add(session_daily[row["sid"]][row["day"]], row["quality"], _stats_from_sql(row))

    names = dict(domain_names or {})
    names.setdefault("_desktop", "桌面")
    public_sessions = []
    archived = defaultdict(_bundle)
    archived_daily = defaultdict(lambda: defaultdict(_bundle))
    archived_last = defaultdict(int)
    archived_engines = defaultdict(lambda: defaultdict(int))
    for sid, bundle in session_bundles.items():
        public = _public(bundle)
        if public["recorded_turns"] <= 0:
            continue
        domain_counts = session_domains[sid]
        domain = max(domain_counts, key=domain_counts.get) if domain_counts else "_desktop"
        engine_counts = session_engines[sid]
        engine = max(engine_counts, key=engine_counts.get) if engine_counts else "unknown"
        model_counts = session_models[sid]
        model = max(model_counts, key=model_counts.get) if model_counts else ""
        meta = sessions.get(sid)
        if not meta or session_deleted[sid]:
            _bundle_merge(archived[domain], bundle)
            for day, value in session_daily[sid].items():
                _bundle_merge(archived_daily[domain][day], value)
            archived_last[domain] = max(archived_last[domain], last_at[sid])
            archived_engines[domain][engine] += public["recorded_total"]
            continue
        public_sessions.append({
            **public, "id": sid, "title": meta["title"], "domain": domain,
            "workflow": names.get(domain, domain), "engine": engine, "model": model,
            "last_at": last_at[sid], "archived": False,
            "daily": {day: _public(value) for day, value in sorted(session_daily[sid].items())},
        })
    for domain, bundle in archived.items():
        public = _public(bundle)
        engines = archived_engines[domain]
        public_sessions.append({
            **public, "id": "archived:" + hashlib.sha256(domain.encode()).hexdigest()[:12],
            "title": "已删除会话", "domain": domain, "workflow": names.get(domain, domain),
            "engine": max(engines, key=engines.get) if engines else "unknown", "model": "",
            "last_at": archived_last[domain], "archived": True,
            "daily": {day: _public(value) for day, value in sorted(archived_daily[domain].items())},
        })

    public_sessions.sort(key=lambda row: (-row["recorded_total"], row["title"]))
    workflow_bundles = defaultdict(_bundle)
    workflow_session_ids = defaultdict(set)
    workflow_last = defaultdict(int)
    for row in public_sessions:
        domain = row["domain"]
        for quality, prefix in (("measured", ""), ("historical", "historical_"),
                                ("anomalous", "anomalous_")):
            stats = _empty()
            if quality == "measured":
                for key in stats:
                    stats[key] = int(row.get(key) or 0)
            elif quality == "historical":
                for key in stats:
                    stats[key] = int(row.get(prefix + key) or 0)
            else:
                stats["total"] = int(row.get("anomalous_total") or 0)
                stats["turns"] = int(row.get("anomalous_turns") or 0)
            _add(workflow_bundles[domain][quality], stats)
        workflow_session_ids[domain].add(row["id"])
        workflow_last[domain] = max(workflow_last[domain], row["last_at"])
    workflows = [{**_public(bundle), "id": domain, "name": names.get(domain, domain),
                  "sessions": len(workflow_session_ids[domain]), "last_at": workflow_last[domain]}
                 for domain, bundle in workflow_bundles.items()]
    workflows.sort(key=lambda row: (-row["recorded_total"], row["name"]))

    total_bundle = _bundle()
    day_rows = []
    cursor = start
    recorded_days = 0
    while cursor <= end:
        iso = cursor.isoformat()
        bundle = daily[iso]
        _bundle_merge(total_bundle, bundle)
        public = _public(bundle)
        if public["recorded_total"] > 0:
            recorded_days += 1
        public["availability"] = "unrecorded" if not first or iso < first else "recorded"
        day_rows.append({"date": iso, **public})
        cursor += timedelta(days=1)
    total = _public(total_bundle)
    active_count = sum(1 for sid, bundle in session_bundles.items()
                       if _public(bundle)["recorded_turns"] > 0
                       and sid in sessions and not session_deleted[sid])
    distinct_sessions = active_count + int(archived_count or 0)
    coverage_days = 0 if not first else max(0, (end - max(start, datetime.fromisoformat(first).date())).days + 1)
    summary = {**total, "sessions": distinct_sessions, "active_sessions": active_count,
               "archived_sessions": int(archived_count or 0), "workflows": len(workflows)}
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    return {
        "range": {"days": days, "start": start.isoformat(), "end": end.isoformat()},
        "coverage": {"start": first or None, "recorded_days": recorded_days,
                     "covered_window_days": coverage_days,
                     "unavailable_before": first or None},
        "summary": summary, "days": day_rows, "workflows": workflows,
        "sessions": public_sessions, "method": "provider_usage_v2", "local_only": True,
        "health": {"migration_complete": migration_done, "migrated_now": migrated,
                   "daily_reconciled": reconcile_ok, "anomalous_turns": total["anomalous_turns"],
                   "query_ms": elapsed_ms},
    }
