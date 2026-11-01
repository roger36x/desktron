"""macOS 原生应用 L1 只读观察链路。

边界：
- 目标只能由原生宿主在人选定窗口后建立，Agent 看不到窗口或进程列表；
- 目标绑定 bundle/code requirement/pid/进程启动令牌/window，并带 target_generation；
- 人碰窗口会推进 interaction_epoch，在途观察回执随即作废；
- 宿主回执必须通过 manifest 声明的角色/语义/取值白名单投影，任何多余字段整份拒绝；
- 本切片不存在原生写动作，也不接受 Agent 参数。
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from .manifest import validate_agent_control

NATIVE_OBSERVATION_SCHEMA = "desk.native-window/v1"
NATIVE_OBSERVATION_TIMEOUT_MS = 10000
NATIVE_TARGET_TTL_S = 900
NATIVE_MAX_NODES = 200
NATIVE_NODE_KEYS = {"ref", "parent", "role", "semantic", "value", "enabled", "selected"}
NATIVE_REF_RE = re.compile(r"^n[0-9]{1,4}$")
NATIVE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
NATIVE_WINDOW_RE = re.compile(r"^[0-9]{1,12}$")
# 这些事件证明原目标已不存在或不可信，直接撤销，不留可恢复窗口。
NATIVE_TERMINAL_EVENTS = {"identity_changed", "window_closed", "process_exited", "host_released"}
NATIVE_TARGET_EVENTS = NATIVE_TERMINAL_EVENTS | {"user_interaction", "user_idle"}
# 宿主只能用枚举报告失败原因；自由文本可能夹带 AX 原文。
NATIVE_HOST_ERRORS = {
    "native_identity_changed", "native_window_closed", "native_process_exited",
    "native_ax_unavailable", "native_projection_failed", "native_observation_timeout",
    "human_intervention_reinspect_required",
}
NATIVE_INVALIDATING_ERRORS = {
    "native_identity_changed": "identity_changed",
    "native_window_closed": "window_closed",
    "native_process_exited": "process_exited",
    "native_target_invalidated": "target_invalidated",
    "native_host_mismatch": "host_mismatch",
    "native_target_generation_changed": "generation_changed",
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class NativeL1Mixin:
    """依赖 AppControlRuntime 的 db/_lock/clock/_expire/_revoke_locked 等成员。"""

    # ---- manifest ----

    def _app(self, app_id):
        for app in self.apps_provider() or []:
            if isinstance(app, dict) and app.get("id") == app_id:
                return app
        from .runtime import AppControlError
        raise AppControlError("应用不存在或不可控制", 404)

    def _native_l1(self, app_id):
        from .runtime import AppControlError
        control, native = self._native_application(app_id)
        errors = validate_agent_control(control)
        if errors:
            raise AppControlError("; ".join(errors), 422)
        if control.get("level") != "L1" or control.get("actions"):
            raise AppControlError("原生应用当前只开放 L1 只读观察", 409)
        return control, native

    def _is_native(self, app_id):
        try:
            control = self._control(app_id)
        except Exception:
            return False
        return isinstance(control.get("native_application"), dict)

    # ---- target 状态 ----

    @staticmethod
    def _target_public(row, native=None):
        out = {
            "lease_id": row["lease_id"], "app_id": row["app_id"],
            "bundle_id": row["bundle_id"], "pid": row["pid"],
            "process_start_token": row["process_start_token"],
            "window_id": row["window_id"],
            "target_generation": row["target_generation"],
            "interaction_epoch": row["interaction_epoch"],
            "manifest_fingerprint": row["manifest_fingerprint"],
            "status": row["status"],
        }
        if native is not None:
            out["code_requirement"] = str(native.get("code_requirement") or "")
            out["projector"] = str(native.get("projector") or "")
        return out

    def _invalidate_target_locked(self, lease_id, reason):
        self.db.execute(
            "UPDATE desktop_targets SET status='invalidated',invalidated_reason=? "
            "WHERE lease_id=? AND status='active'", (str(reason or ""), lease_id))

    def _drop_target_locked(self, lease_id, reason):
        """目标失效即撤销 lease；_revoke_locked 会取消在途读取。"""
        self._invalidate_target_locked(lease_id, reason)
        self._revoke_locked(lease_id, "native_" + str(reason or "invalidated"))

    def _native_host_ready(self, host_instance_id=""):
        status = self.desktop_status()
        if status["execution"]["status"] != "ready":
            return False, status["execution"].get("reason") or "desktop_unavailable"
        if host_instance_id and status["host"].get("instance_id") != host_instance_id:
            return False, "native_host_mismatch"
        return True, ""

    def _active_native_target(self, lease_id, app_id):
        """Agent 路径的统一闸门：manifest、宿主就绪、目标仍有效且未漂移。"""
        from .runtime import AppControlError
        control, native = self._native_l1(app_id)
        target = self.db.execute(
            "SELECT * FROM desktop_targets WHERE lease_id=?", (lease_id,)).fetchone()
        if not target or target["status"] != "active":
            raise AppControlError("原生目标已失效，请用户重新选择窗口", 409)
        if target["manifest_fingerprint"] != self._manifest_fingerprint(control):
            self._drop_target_locked(lease_id, "manifest_changed")
            raise AppControlError("原生应用 manifest 已变化，请用户重新选择窗口", 409)
        ready, reason = self._native_host_ready(target["host_instance_id"])
        if not ready:
            if reason == "native_host_mismatch":
                self._drop_target_locked(lease_id, "host_mismatch")
            raise AppControlError("原生宿主不可执行:%s" % reason, 409)
        return control, native, target

    def _control_for_agent(self, app_id, lease_id=""):
        control = self._control(app_id)
        if isinstance(control.get("native_application"), dict):
            control, _native, _target = self._active_native_target(lease_id, app_id)
        return control

    # ---- 宿主：人选定窗口 ----

    def select_desktop_target(self, app_id, bundle_id, pid, process_start_token,
                              code_requirement, window_id, host_instance_id, host_pid,
                              session_id, ttl_seconds=NATIVE_TARGET_TTL_S):
        from .runtime import AppControlError
        app_id = str(app_id or "")
        control, native = self._native_l1(app_id)
        bundle = str(bundle_id or "").strip()
        if bundle != str(native.get("bundle_id") or ""):
            raise AppControlError("bundle_id 超出应用声明", 403)
        if str(code_requirement or "") != str(native.get("code_requirement") or ""):
            raise AppControlError("目标代码签名要求与 manifest 不一致", 403)
        try:
            target_pid = int(pid)
            owner_pid = int(host_pid)
        except (TypeError, ValueError):
            raise AppControlError("pid 非法", 422)
        token = str(process_start_token or "")
        window = str(window_id or "")
        instance = str(host_instance_id or "").strip()
        sid = str(session_id or "").strip()
        if target_pid <= 1 or owner_pid <= 1 or target_pid == owner_pid:
            raise AppControlError("pid 非法", 422)
        if not NATIVE_TOKEN_RE.fullmatch(token):
            raise AppControlError("process_start_token 非法", 422)
        if not NATIVE_WINDOW_RE.fullmatch(window):
            raise AppControlError("window_id 非法", 422)
        if not instance or not sid:
            raise AppControlError("host_instance_id 与 session_id 必填", 422)
        try:
            ttl = int(NATIVE_TARGET_TTL_S if ttl_seconds is None else ttl_seconds)
        except (TypeError, ValueError):
            raise AppControlError("ttl_seconds 非法", 422)
        if ttl < 30 or ttl > NATIVE_TARGET_TTL_S:
            raise AppControlError("ttl_seconds 必须在 30 到 %d 之间" % NATIVE_TARGET_TTL_S, 422)
        now = self.clock()
        with self._lock, self.db:
            self._expire()
            ready, reason = self._native_host_ready(instance)
            if not ready:
                raise AppControlError("原生宿主不可执行:%s" % reason, 409)
            status = self.desktop_status()["host"]
            if status.get("pid") not in (None, owner_pid):
                raise AppControlError("宿主 PID 与已登记探针不一致", 403)
            old = self.db.execute(
                "SELECT lease_id FROM desktop_targets WHERE app_id=? AND status='active'",
                (app_id,)).fetchall()
            for row in old:
                self._drop_target_locked(row["lease_id"], "replaced_by_user_delegation")
            generation = "tg_" + uuid.uuid4().hex
            lease_id = "lease_" + uuid.uuid4().hex
            resource_id = "native-window:" + generation
            self.db.execute(
                "INSERT INTO leases(id,app_id,resource_id,window_id,session_id,status,"
                "created_at,updated_at,expires_at,kind) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (lease_id, app_id, resource_id, window, sid, "offered", now, now,
                 now + ttl, "native_delegated"))
            self.db.execute(
                "INSERT INTO desktop_targets(lease_id,app_id,bundle_id,pid,process_start_token,"
                "code_identity,window_id,target_generation,interaction_epoch,host_instance_id,"
                "host_pid,manifest_fingerprint,status,selected_at,last_verified_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lease_id, app_id, bundle, target_pid, token,
                 hashlib.sha256(str(code_requirement).encode("utf-8")).hexdigest(),
                 window, generation, 0, instance, owner_pid,
                 self._manifest_fingerprint(control), "active", now, now))
            self._event("ready", app_id, resource_id, lease_id,
                        payload={"status": "offered", "kind": "native_delegated"})
            target = self.db.execute(
                "SELECT * FROM desktop_targets WHERE lease_id=?", (lease_id,)).fetchone()
            return {"lease": self.get_lease(lease_id),
                    "target": self._target_public(target, native)}

    def desktop_target_event(self, lease_id, target_generation, host_instance_id, event):
        """宿主报告人介入或目标漂移；过期 generation 不得影响新目标。"""
        from .runtime import AppControlError
        name = str(event or "")
        if name not in NATIVE_TARGET_EVENTS:
            raise AppControlError("原生目标事件非法", 422)
        now = self.clock()
        with self._lock, self.db:
            self._expire()
            target = self.db.execute(
                "SELECT * FROM desktop_targets WHERE lease_id=?", (str(lease_id or ""),)).fetchone()
            if (not target or target["target_generation"] != str(target_generation or "")
                    or target["host_instance_id"] != str(host_instance_id or "")):
                raise AppControlError("原生目标 generation 或宿主不匹配", 409)
            if target["status"] != "active":
                return self._target_public(target)
            if name in NATIVE_TERMINAL_EVENTS:
                self._drop_target_locked(target["lease_id"], name)
            elif name == "user_interaction":
                lease = self.get_lease(target["lease_id"])
                if lease and lease["status"] == "active":
                    # pause 在同一事务内推进 interaction_epoch 并取消在途读取。
                    self.pause(target["lease_id"], "user_interaction")
                else:
                    self.db.execute(
                        "UPDATE desktop_targets SET interaction_epoch=interaction_epoch+1,"
                        "last_verified_at=? WHERE lease_id=?", (now, target["lease_id"]))
            else:
                lease = self.get_lease(target["lease_id"])
                if lease and lease["status"] == "paused":
                    self.resume(target["lease_id"], NATIVE_TARGET_TTL_S)
            return self._target_public(self.db.execute(
                "SELECT * FROM desktop_targets WHERE lease_id=?",
                (target["lease_id"],)).fetchone())

    def desktop_target_status(self, lease_id, target_generation, host_instance_id):
        """宿主对账：Runtime 已撤销的目标，本机也必须停止监测与取活。"""
        with self._lock, self.db:
            self._expire()
            target = self.db.execute(
                "SELECT * FROM desktop_targets WHERE lease_id=?", (str(lease_id or ""),)).fetchone()
            if (not target or target["target_generation"] != str(target_generation or "")
                    or target["host_instance_id"] != str(host_instance_id or "")):
                return {"status": "unknown"}
            lease = self.db.execute(
                "SELECT status FROM leases WHERE id=?", (target["lease_id"],)).fetchone()
            active = (target["status"] == "active" and lease
                      and lease["status"] in ("offered", "active", "paused"))
            return {"status": "active" if active else "invalidated",
                    "lease_status": lease["status"] if lease else "",
                    "reason": target["invalidated_reason"]}

    def _invalidate_foreign_host_targets_locked(self, host_instance_id):
        rows = self.db.execute(
            "SELECT lease_id FROM desktop_targets WHERE status='active' AND host_instance_id<>?",
            (host_instance_id,)).fetchall()
        for row in rows:
            self._drop_target_locked(row["lease_id"], "host_mismatch")

    def _invalidate_dead_lease_targets_locked(self):
        self.db.execute(
            "UPDATE desktop_targets SET status='invalidated',invalidated_reason='lease_ended' "
            "WHERE status='active' AND lease_id IN "
            "(SELECT id FROM leases WHERE status IN ('revoked','expired'))")

    # ---- Agent：只读观察 ----

    def _native_inspect(self, lease, observation, params, expected_version,
                        idempotency_key, anchor, intent):
        from .runtime import AppControlError
        control, _native, _target = self._active_native_target(lease["id"], lease["app_id"])
        spec = next((row for row in control.get("observations") or []
                     if row.get("name") == observation), None)
        if not spec:
            raise AppControlError("观察未声明:%s" % observation, 404)
        if params:
            raise AppControlError("原生 L1 观察不接受参数", 422)
        return spec

    # ---- 宿主：取活与回执 ----

    def desktop_next_operation(self, app_id, host_instance_id):
        from .runtime import AppControlError
        app_id = str(app_id or "")
        control, native = self._native_l1(app_id)
        instance = str(host_instance_id or "")
        if not instance:
            raise AppControlError("host_instance_id 必填", 422)
        import lifecycle
        if not lifecycle.begin(critical="app_control_dispatch"):
            return None
        try:
            now = self.clock()
            with self._lock, self.db:
                self._host_seen[app_id] = now
                self._expire()
                ready, reason = self._native_host_ready(instance)
                if not ready:
                    if reason == "native_host_mismatch":
                        self._invalidate_foreign_host_targets_locked(
                            self.desktop_status()["host"].get("instance_id") or "")
                    return None
                row = self.db.execute(
                    "SELECT o.* FROM operations o JOIN leases l ON l.id=o.lease_id "
                    "JOIN desktop_targets t ON t.lease_id=l.id "
                    "WHERE o.status='queued' AND l.status='active' AND t.status='active' "
                    "AND o.app_id=? AND t.host_instance_id=? AND o.kind='inspect' "
                    "AND o.effect='read_only' AND NOT EXISTS (SELECT 1 FROM operations busy "
                    "WHERE busy.resource_id=o.resource_id AND busy.status='running') "
                    "ORDER BY o.created_at LIMIT 1", (app_id, instance)).fetchone()
                if not row:
                    return None
                target = self.db.execute(
                    "SELECT * FROM desktop_targets WHERE lease_id=?", (row["lease_id"],)).fetchone()
                if target["manifest_fingerprint"] != self._manifest_fingerprint(control):
                    self._drop_target_locked(row["lease_id"], "manifest_changed")
                    return None
                dispatch = uuid.uuid4().hex
                self.db.execute(
                    "UPDATE operations SET status='running',started_at=?,updated_at=?,"
                    "dispatch_id=?,native_epoch=? WHERE id=? AND status='queued'",
                    (now, now, dispatch, target["interaction_epoch"], row["id"]))
                return {"operation": {"id": row["id"], "kind": row["kind"], "name": row["name"],
                                      "dispatch_id": dispatch,
                                      "timeout_ms": row["timeout_ms"]},
                        "target": self._target_public(target, native)}
        finally:
            lifecycle.finish(critical="app_control_dispatch")

    def _project_native_observation(self, native, output):
        from .runtime import AppControlError
        if not isinstance(output, dict) or set(output) - {"nodes", "truncated"}:
            raise AppControlError("native_observation_rejected", 422)
        nodes = output.get("nodes")
        truncated = output.get("truncated", False)
        if (not isinstance(nodes, list) or not nodes or len(nodes) > NATIVE_MAX_NODES
                or not isinstance(truncated, bool)):
            raise AppControlError("native_observation_rejected", 422)
        roles = set(native.get("allowed_roles") or [])
        semantics = set(native.get("allowed_semantics") or [])
        values = set(native.get("allowed_values") or [])
        seen = set()
        clean = []
        for node in nodes:
            if not isinstance(node, dict) or set(node) - NATIVE_NODE_KEYS:
                raise AppControlError("native_observation_rejected", 422)
            ref = node.get("ref")
            parent = node.get("parent", "")
            if (not isinstance(ref, str) or not NATIVE_REF_RE.fullmatch(ref) or ref in seen
                    or not isinstance(parent, str) or (parent and parent not in seen)
                    or node.get("role") not in roles or node.get("semantic") not in semantics):
                raise AppControlError("native_observation_rejected", 422)
            item = {"ref": ref, "role": node["role"], "semantic": node["semantic"]}
            if parent:
                item["parent"] = parent
            if "value" in node:
                if node["value"] not in values:
                    raise AppControlError("native_observation_rejected", 422)
                item["value"] = node["value"]
            for key in ("enabled", "selected"):
                if key in node:
                    if not isinstance(node[key], bool):
                        raise AppControlError("native_observation_rejected", 422)
                    item[key] = node[key]
            seen.add(ref)
            clean.append(item)
        return {"schema": NATIVE_OBSERVATION_SCHEMA, "nodes": clean, "truncated": truncated}

    def desktop_complete(self, operation_id, dispatch_id, host_instance_id, target_generation,
                         interaction_epoch, ok, output=None, error=""):
        from .runtime import AppControlError, TERMINAL, _json
        now = self.clock()
        with self._lock, self.db:
            self._expire()
            row = self.db.execute(
                "SELECT * FROM operations WHERE id=?", (str(operation_id or ""),)).fetchone()
            if not row:
                raise AppControlError("动作不存在", 404)
            if not self._is_native(row["app_id"]):
                raise AppControlError("非原生应用动作不能走原生回执", 409)
            # 迟到或重复回执不能改写已落地结果，也不能结束新一轮派发。
            if (row["status"] in TERMINAL or row["status"] != "running"
                    or not dispatch_id or dispatch_id != row["dispatch_id"]):
                return self.get_operation(row["id"])
            try:
                control, native = self._native_l1(row["app_id"])
            except AppControlError:
                control = native = None
            target = self.db.execute(
                "SELECT * FROM desktop_targets WHERE lease_id=?", (row["lease_id"],)).fetchone()
            lease = self.get_lease(row["lease_id"])
            try:
                epoch = int(interaction_epoch)
            except (TypeError, ValueError):
                epoch = -1
            reason = ""
            if control is None:
                reason = "native_target_invalidated"
            elif not target or target["status"] != "active":
                reason = "native_target_invalidated"
            elif target["host_instance_id"] != str(host_instance_id or ""):
                reason = "native_host_mismatch"
            elif target["target_generation"] != str(target_generation or ""):
                reason = "native_target_generation_changed"
            elif target["manifest_fingerprint"] != self._manifest_fingerprint(control):
                reason = "native_target_invalidated"
            elif epoch != target["interaction_epoch"] or row["native_epoch"] != epoch:
                reason = "human_intervention_reinspect_required"
            elif not lease or lease["status"] != "active":
                reason = "human_intervention_reinspect_required"
            projected = None
            if reason:
                ok, error = False, reason
            elif ok:
                try:
                    projected = self._project_native_observation(native, output)
                except AppControlError:
                    ok, error = False, "native_observation_rejected"
            else:
                error = str(error or "")
                if error not in NATIVE_HOST_ERRORS:
                    error = "native_observation_failed"
            after = ""
            if ok:
                digest = hashlib.sha256(_canonical(projected).encode("utf-8")).hexdigest()[:16]
                after = "%s-%d-%s" % (target["target_generation"][3:11],
                                      target["interaction_epoch"], digest)
                self.db.execute(
                    "UPDATE leases SET needs_inspect=0 WHERE id=? AND status='active' "
                    "AND updated_at<=?", (row["lease_id"], row["created_at"]))
                self.db.execute(
                    "INSERT INTO observations(app_id,resource_id,version,value_json,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(app_id,resource_id) DO UPDATE SET "
                    "version=excluded.version,value_json=excluded.value_json,"
                    "updated_at=excluded.updated_at",
                    (row["app_id"], row["resource_id"], after, _json(projected), now))
                self.db.execute(
                    "UPDATE desktop_targets SET last_verified_at=? WHERE lease_id=?",
                    (now, row["lease_id"]))
            status = "succeeded" if ok else "failed"
            self.db.execute(
                "UPDATE operations SET status=?,output_json=?,error=?,after_version=?,"
                "updated_at=?,finished_at=? WHERE id=?",
                (status, _json(projected) if ok else "", "" if ok else error, after,
                 now, now, row["id"]))
            self._event("action_completed" if ok else "failed", row["app_id"],
                        row["resource_id"], row["lease_id"], row["id"],
                        {"status": status, "error": "" if ok else error})
            if not ok and error in NATIVE_INVALIDATING_ERRORS and target:
                self._drop_target_locked(row["lease_id"], NATIVE_INVALIDATING_ERRORS[error])
            return self.get_operation(row["id"])
