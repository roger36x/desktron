"""Durable admission receipts for product-owned manual compaction.

A receipt proves that Desk accepted an operation.  Recovery may classify existing
Coordinator evidence, but must never start the compaction again.
"""
import os
import re
import time
import uuid

import durable_json


_ACTIVE = frozenset(("running", "verifying"))
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
BOOT_ID = "compact_boot_" + uuid.uuid4().hex


class CompactOperationConflict(RuntimeError):
    pass


class CompactOperations:
    def __init__(self, root, boot_id=BOOT_ID):
        self.root = os.path.join(os.path.abspath(root), "compact-operations")
        os.makedirs(self.root, exist_ok=True)
        self.boot_id = str(boot_id)

    def path(self, sid):
        sid = self._id(sid, "conversation_id")
        return os.path.join(self.root, sid + ".json")

    @staticmethod
    def _id(value, label):
        value = str(value or "")
        if not _ID.fullmatch(value):
            raise ValueError("invalid " + label)
        return value

    @staticmethod
    def public(row):
        row = row if isinstance(row, dict) else {}
        status = str(row.get("status") or "")
        result = str(row.get("result") or ("running" if status in _ACTIVE else ""))
        return {key: row.get(key) for key in
                ("operation_id", "status", "started", "before_seq", "expected_turn_id", "boot_id",
                 "result", "error", "updated_at") if row.get(key) not in (None, "") } | {
                    "running": status in _ACTIVE,
                    "trigger": "manual",
                    "result": result,
                }

    def _deleted(self, sid):
        from .physical_delete import is_deleted
        return is_deleted(os.path.dirname(self.root), sid)

    def _guard(self, sid):
        from .physical_delete import deletion_guard
        return deletion_guard(os.path.dirname(self.root), sid)

    def get(self, sid, operation_id):
        operation_id = self._id(operation_id, "operation_id")
        if self._deleted(sid):
            return {"operation_id":operation_id, "status":"finished", "running":False,
                    "trigger":"manual", "result":"conversation_deleted"}
        return self.public(durable_json.read(self.path(sid)).get(operation_id))

    def current(self, sid):
        if self._deleted(sid):
            return {}
        rows = durable_json.read(self.path(sid))
        values = [row for row in rows.values() if isinstance(row, dict)]
        if not values:
            return {}
        active = [row for row in values if row.get("status") in _ACTIVE]
        pool = active or values
        return self.public(max(pool, key=lambda row: (float(row.get("started") or 0),
                                                       float(row.get("updated_at") or 0))))

    def claim(self, sid, operation_id, *, before_seq):
        operation_id = self._id(operation_id, "operation_id")
        path = self.path(sid)
        with self._guard(sid):
            if self._deleted(sid):
                raise CompactOperationConflict("Shepherd Conversation 已删除")
            with durable_json.locked(path + ".transaction"):
                rows = durable_json.read(path)
                existing = rows.get(operation_id)
                if isinstance(existing, dict):
                    return self.public(existing), False
                blocking = next((row for row in rows.values() if isinstance(row, dict)
                                 and row.get("status") in _ACTIVE), None)
                if blocking:
                    raise CompactOperationConflict("已有压缩操作正在执行")
                now = time.time()
                before_seq = int(before_seq or 0)
                row = {"operation_id":operation_id, "status":"running", "result":"running",
                       "started":int(now * 1000), "before_seq":before_seq,
                       "expected_turn_id":f"{sid}:{before_seq + 1}",
                       "boot_id":self.boot_id, "updated_at":now}
                rows[operation_id] = row
                durable_json.write(path, rows)
                return self.public(row), True

    def cancel_unaccepted(self, sid, operation_id):
        """Fence a request that may not have reached the admission endpoint."""
        operation_id = self._id(operation_id, "operation_id")
        path = self.path(sid)
        with self._guard(sid):
            if self._deleted(sid):
                return {"operation_id":operation_id, "status":"finished", "running":False,
                        "trigger":"manual", "result":"conversation_deleted"}
            with durable_json.locked(path + ".transaction"):
                rows = durable_json.read(path)
                row = rows.get(operation_id)
                if isinstance(row, dict):
                    return self.public(row)
                now = time.time()
                row = {"operation_id":operation_id, "status":"cancelled",
                       "result":"not_started", "boot_id":self.boot_id,
                       "started":int(now * 1000), "updated_at":now}
                rows[operation_id] = row
                durable_json.write(path, rows)
                return self.public(row)

    def delete_public(self, sid):
        path = self.path(sid)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def verifying(self, sid, operation_id):
        operation_id = self._id(operation_id, "operation_id")
        path = self.path(sid)
        with self._guard(sid):
            if self._deleted(sid):
                return {"operation_id":operation_id, "status":"finished", "running":False,
                        "trigger":"manual", "result":"conversation_deleted"}
            with durable_json.locked(path + ".transaction"):
                rows = durable_json.read(path)
                row = rows.get(operation_id)
                if not isinstance(row, dict):
                    raise CompactOperationConflict("压缩操作不存在")
                if row.get("status") == "running":
                    row.update(status="verifying", result="running", updated_at=time.time())
                    durable_json.write(path, rows)
                return self.public(row)

    def finish(self, sid, operation_id, *, result, error=""):
        operation_id = self._id(operation_id, "operation_id")
        result = str(result or "failed")[:40]
        path = self.path(sid)
        with self._guard(sid):
            if self._deleted(sid):
                return {"operation_id":operation_id, "status":"finished", "running":False,
                        "trigger":"manual", "result":"conversation_deleted"}
            with durable_json.locked(path + ".transaction"):
                rows = durable_json.read(path)
                row = rows.get(operation_id)
                if not isinstance(row, dict):
                    raise CompactOperationConflict("压缩操作不存在")
                if row.get("status") not in _ACTIVE:
                    return self.public(row)
                row.update(status="finished", result=result, error=str(error or "")[:300],
                           updated_at=time.time())
                durable_json.write(path, rows)
                return self.public(row)
