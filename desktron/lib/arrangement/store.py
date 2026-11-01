"""安排的文件存储。

声明与运行记录分开：前者是人维护的配置，后者是有上限的机器流水。两份都只落当前
Desk 的 .workbench；安排在哪台机器创建，就由哪台机器执行。
"""
import copy
import json
import os
import threading
import time
import uuid


_LOCK = threading.RLock()
MAX_RUNS = 200


def _dir(root):
    return os.path.join(root, ".workbench")


def path(root):
    return os.path.join(_dir(root), "arrangements.json")


def runs_path(root):
    return os.path.join(_dir(root), "arrangement-runs.json")


def _read(p, key):
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        rows = data.get(key) if isinstance(data, dict) else None
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _write(p, key, rows):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({key: rows}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def list_all(root):
    with _LOCK:
        return copy.deepcopy(_read(path(root), "items"))


def get(root, aid):
    return next((x for x in list_all(root) if x.get("id") == aid), None)


def create(root, item):
    now = int(time.time())
    row = dict(item)
    row["id"] = "arr_" + uuid.uuid4().hex[:12]
    row["created_at"] = now
    row["updated_at"] = now
    with _LOCK:
        rows = _read(path(root), "items")
        rows.append(row)
        _write(path(root), "items", rows)
    return copy.deepcopy(row)


def update(root, aid, changes):
    with _LOCK:
        rows = _read(path(root), "items")
        hit = None
        for row in rows:
            if row.get("id") == aid:
                row.update(changes)
                row["updated_at"] = int(time.time())
                hit = copy.deepcopy(row)
                break
        if hit is not None:
            _write(path(root), "items", rows)
        return hit


def delete(root, aid):
    with _LOCK:
        rows = _read(path(root), "items")
        kept = [x for x in rows if x.get("id") != aid]
        if len(kept) == len(rows):
            return False
        _write(path(root), "items", kept)
        return True


def claim_due(root, now, next_fn):
    """原子领取到期项，并在启动前先推进 next_at，避免同一时刻重复触发。"""
    claimed = []
    with _LOCK:
        rows = _read(path(root), "items")
        changed = False
        for row in rows:
            due = int(row.get("next_at") or 0)
            if not row.get("enabled", True) or not due or due > now:
                continue
            row["next_at"] = int(next_fn(row.get("trigger") or {}, now) or 0)
            row["last_scheduled_at"] = due
            row["updated_at"] = int(time.time())
            claimed.append(copy.deepcopy(row))
            changed = True
        if changed:
            _write(path(root), "items", rows)
    return claimed


def runs(root, limit=MAX_RUNS):
    with _LOCK:
        rows = _read(runs_path(root), "runs")
        return copy.deepcopy(rows[-max(1, int(limit)):][::-1])


def run_get(root, rid):
    return next((x for x in runs(root, MAX_RUNS) if x.get("id") == rid), None)


def run_create(root, row):
    item = dict(row)
    item.setdefault("id", "run_" + uuid.uuid4().hex[:14])
    item.setdefault("created_at", int(time.time()))
    item.setdefault("status", "queued")
    with _LOCK:
        rows = _read(runs_path(root), "runs")
        rows.append(item)
        _write(runs_path(root), "runs", rows[-MAX_RUNS:])
    return copy.deepcopy(item)


def run_update(root, rid, changes):
    with _LOCK:
        rows = _read(runs_path(root), "runs")
        hit = None
        for row in rows:
            if row.get("id") == rid:
                row.update(changes)
                hit = copy.deepcopy(row)
                break
        if hit is not None:
            _write(runs_path(root), "runs", rows[-MAX_RUNS:])
        return hit


def recover_interrupted(root):
    """进程退出不会等待普通安排；重启后把悬空运行如实记成中断。"""
    now = int(time.time())
    with _LOCK:
        rows = _read(runs_path(root), "runs")
        changed = False
        for row in rows:
            if row.get("status") in ("queued", "running"):
                row.update(status="failed", finished_at=now, error="Desk 进程在运行期间退出")
                changed = True
        if changed:
            _write(runs_path(root), "runs", rows[-MAX_RUNS:])
