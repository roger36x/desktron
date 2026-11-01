"""Durable delivery receipts for ordinary (non-Shepherd) GUI turns.

This is not a replay queue or transcript.  It records only whether a request
was accepted, durably projected, may have crossed the native dispatch fence,
or reached a Desk terminal.  After a backend crash, an uncertain request blocks
new dispatch only while the old native writer may still be alive; once its exit
is proven, the adapter closes the request from native evidence (see
delivery_recovery.py).  An explicit user decision remains the fallback.
"""
import hashlib
import json
import os
import time
import uuid

import durable_json


BOOT_ID = "boot_" + uuid.uuid4().hex
_ACTIVE = frozenset(("accepted", "storing", "stored", "cleanup_required", "dispatched", "uncertain"))
_TERMINAL = frozenset(("finished", "rejected", "cancelled"))


class DeliveryConflict(RuntimeError):
    status = 409

    def __init__(self, message, code="delivery_conflict"):
        super().__init__(message)
        self.code = code


def _valid_id(value, label="id"):
    value = str(value or "")
    if not value or len(value) > 160 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for c in value):
        raise ValueError(f"{label} 无效")
    return value


class DeliveryReceipts:
    def __init__(self, adapter_dir, boot_id=BOOT_ID):
        self.root = os.path.join(os.path.abspath(adapter_dir), "delivery")
        self.boot_id = str(boot_id)

    def path(self, sid):
        return os.path.join(self.root, _valid_id(sid, "session_id") + ".json")

    @staticmethod
    def public(row):
        keys = ("request_id", "status", "reason", "message_id", "engine_session_id",
                "native_turn_id", "cleanup_from")
        return {key: row[key] for key in keys if row.get(key) not in (None, "")}

    def get(self, sid, request_id):
        rid = _valid_id(request_id, "request_id")
        row = durable_json.read(self.path(sid)).get(rid)
        return self.public(row) if isinstance(row, dict) else {"request_id":rid, "status":"not_found"}

    def claim(self, sid, request_id, payload):
        rid = _valid_id(request_id, "request_id")
        fingerprint = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        path = self.path(sid)
        with durable_json.locked(path + ".transaction"):
            rows = durable_json.read(path)
            old = rows.get(rid)
            if isinstance(old, dict):
                if old.get("fingerprint") not in (None, fingerprint):
                    raise DeliveryConflict("同一请求 ID 的内容不一致，已拒绝重复发送")
                return self.public(old), False
            blocking = next((row for row in rows.values() if isinstance(row, dict)
                             and row.get("status") in ("cleanup_required", "uncertain")), None)
            if blocking:
                code = ("delivery_cleanup" if blocking.get("status") == "cleanup_required"
                        else "delivery_uncertain")
                raise DeliveryConflict("上一条请求尚未安全收口，不能开始新回合", code=code)
            row = {"request_id":rid, "fingerprint":fingerprint,
                   "status":"accepted", "boot_id":self.boot_id,
                   "updated_at":time.time()}
            rows[rid] = row
            durable_json.write(path, rows)
            return self.public(row), True

    def storing(self, sid, request_id, *, message_id):
        row = self._advance(sid, request_id, ("accepted",), "storing",
                            message_id=_valid_id(message_id, "message_id"))
        return self._require_stage(row, "storing")

    def stored(self, sid, request_id, *, message_id):
        row = self._advance(sid, request_id, ("accepted", "storing"), "stored",
                            message_id=_valid_id(message_id, "message_id"))
        return self._require_stage(row, "stored")

    def dispatched(self, sid, request_id, *, engine_session_id="", native_turn_id=""):
        fields = {}
        if engine_session_id:
            fields["engine_session_id"] = _valid_id(engine_session_id, "engine_session_id")
        if native_turn_id:
            fields["native_turn_id"] = _valid_id(native_turn_id, "native_turn_id")
        row = self._advance(sid, request_id, ("stored",), "dispatched", **fields)
        return self._require_stage(row, "dispatched")

    def bind_native(self, sid, request_id, *, engine_session_id="", native_turn_id=""):
        fields = {}
        if engine_session_id:
            fields["engine_session_id"] = _valid_id(engine_session_id, "engine_session_id")
        if native_turn_id:
            fields["native_turn_id"] = _valid_id(native_turn_id, "native_turn_id")
        return self._advance(sid, request_id, ("dispatched", "uncertain"), None, **fields)

    def uncertain(self, sid, request_id, *, reason, engine_session_id="", native_turn_id=""):
        fields = {"reason":str(reason or "")[:160]}
        if engine_session_id:
            fields["engine_session_id"] = _valid_id(engine_session_id, "engine_session_id")
        if native_turn_id:
            fields["native_turn_id"] = _valid_id(native_turn_id, "native_turn_id")
        return self._advance(sid, request_id, ("dispatched",), "uncertain", **fields)

    def finish(self, sid, request_id, *, reason, outcome=""):
        fields = {"reason":str(reason or "")[:160]}
        if outcome:
            fields["outcome"] = str(outcome)[:80]
        return self._advance(sid, request_id, ("stored", "dispatched"), "finished", **fields)

    def reject(self, sid, request_id, *, reason, cancelled=False):
        return self._advance(sid, request_id, ("accepted", "stored"),
                             "cancelled" if cancelled else "rejected",
                             reason=str(reason or "")[:160])

    def cancel_unsent(self, sid, request_id):
        """Persist a cancellation before a delayed POST can claim the same ID."""
        rid = _valid_id(request_id, "request_id")
        path = self.path(sid)
        with durable_json.locked(path + ".transaction"):
            rows = durable_json.read(path)
            row = rows.get(rid)
            if not isinstance(row, dict):
                row = {"request_id":rid, "fingerprint":None, "boot_id":self.boot_id,
                       "status":"cancelled", "reason":"cancelled_before_accept",
                       "updated_at":time.time()}
                rows[rid] = row
                durable_json.write(path, rows)
                return {**self.public(row), "retryable": True}
            retryable = row.get("status") == "accepted"
            if retryable:
                row.update(status="cancelled", reason="cancelled_before_dispatch",
                           updated_at=time.time())
                durable_json.write(path, rows)
            elif row.get("status") == "rejected":
                retryable = row.get("reason") in ("restart_before_store", "restart_before_dispatch")
                row.update(status="cancelled", reason="cancelled_after_recovery", updated_at=time.time())
                durable_json.write(path, rows)
            elif row.get("status") == "cleanup_required":
                row.update(reason="cancelled_projection_cleanup", updated_at=time.time())
                durable_json.write(path, rows)
            elif row.get("status") in ("storing", "stored"):
                row.update(status="cleanup_required", reason="cancelled_projection_cleanup",
                           cleanup_from=row.get("status"), updated_at=time.time())
                durable_json.write(path, rows)
            return {**self.public(row), "retryable": retryable}

    def finish_cleanup(self, sid, request_id):
        # Cleanup and user cancellation compete under the same receipt lock.
        # A stale read before _advance could otherwise restore retry permission.
        rid = _valid_id(request_id, "request_id")
        path = self.path(sid)
        with durable_json.locked(path + ".transaction"):
            rows = durable_json.read(path)
            row = rows.get(rid)
            if not isinstance(row, dict):
                raise DeliveryConflict("请求回执不存在", code="request_not_found")
            if row.get("status") in ("rejected", "cancelled"):
                return self.public(row)
            if row.get("status") != "cleanup_required":
                raise DeliveryConflict("请求阶段与持久派发证据不一致")
            reason = ("cancelled_before_dispatch" if row.get("reason") == "cancelled_projection_cleanup"
                      else "restart_before_dispatch")
            row.update(status="rejected", reason=reason, updated_at=time.time())
            durable_json.write(path, rows)
            return self.public(row)

    def require_cleanup(self, sid, request_id, *, reason="projection_cleanup_required"):
        current = self.get(sid, request_id)
        if current.get("status") == "cleanup_required":
            return current
        return self._advance(sid, request_id, ("storing", "stored"), "cleanup_required",
                             reason=str(reason or "")[:160], cleanup_from=current.get("status"))

    def resolve_uncertain(self, sid, request_id, *, reason="user_acknowledged_unknown"):
        return self._advance(sid, request_id, ("dispatched", "uncertain"), "finished",
                             reason=str(reason or "")[:160], outcome="unknown")

    def resolve_recovered(self, sid, request_id, *, outcome, reason):
        """旧写入进程已确认退出后，按取得的证据结案；不重发、不改写历史。"""
        return self._advance(sid, request_id, ("dispatched", "uncertain"), "finished",
                             reason=str(reason or "")[:160], outcome=str(outcome or "unknown")[:80])

    def recover(self, sid):
        """Classify rows left by an older backend; never schedules native work."""
        path = self.path(sid)
        changed = []
        with durable_json.locked(path + ".transaction"):
            rows = durable_json.read(path)
            for row in rows.values():
                if not isinstance(row, dict) or row.get("boot_id") == self.boot_id:
                    continue
                status = row.get("status")
                if status == "accepted":
                    row.update(status="rejected", reason="restart_before_store",
                               updated_at=time.time())
                    changed.append(self.public(row))
                elif status in ("storing", "stored"):
                    row.update(status="cleanup_required", reason="restart_before_dispatch_cleanup",
                               cleanup_from=status, updated_at=time.time())
                    changed.append(self.public(row))
                elif status == "cleanup_required":
                    changed.append(self.public(row))
                elif status == "dispatched":
                    row.update(status="uncertain", reason="restart_after_dispatch",
                               updated_at=time.time())
                    changed.append(self.public(row))
            if changed:
                durable_json.write(path, rows)
        return changed

    def unresolved(self, sid):
        rows = durable_json.read(self.path(sid))
        return [self.public(row) for row in rows.values()
                if isinstance(row, dict) and row.get("status") in _ACTIVE]

    def blocking_uncertain(self, sid, *, exclude=""):
        exclude = str(exclude or "")
        return next((row for row in self.unresolved(sid)
                     if row.get("request_id") != exclude
                     and row.get("status") in ("cleanup_required", "uncertain")), None)

    @staticmethod
    def _require_stage(row, expected):
        if row.get("status") == expected:
            return row
        if row.get("status") in ("rejected", "cancelled", "cleanup_required"):
            raise DeliveryConflict("请求已在原生派发前取消", code="prompt_not_started")
        raise DeliveryConflict("请求阶段与持久派发证据不一致")

    def _advance(self, sid, request_id, allowed, status, **fields):
        rid = _valid_id(request_id, "request_id")
        path = self.path(sid)
        with durable_json.locked(path + ".transaction"):
            rows = durable_json.read(path)
            row = rows.get(rid)
            if not isinstance(row, dict):
                raise DeliveryConflict("请求回执不存在", code="request_not_found")
            current = row.get("status")
            if current in _TERMINAL:
                if current == status:
                    return self.public(row)
                raise DeliveryConflict("请求已结束，不能推进到其他阶段",
                    code="prompt_not_started" if current in ("rejected", "cancelled")
                    else "delivery_conflict")
            if current not in allowed:
                raise DeliveryConflict("请求阶段与持久派发证据不一致")
            if status:
                row["status"] = status
            row.update(fields, updated_at=time.time())
            durable_json.write(path, rows)
            return self.public(row)
