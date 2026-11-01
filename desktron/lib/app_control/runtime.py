"""App Control 的进程内协调器与 SQLite 事实源。

SSE/前端只负责通知。租约、幂等、审批和最终回执都落库，因此刷新、引擎退出或
Runtime 重启后仍能确定一次动作是否执行过。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import urllib.parse
import time
import uuid

from deskcfg import DATA_HOME, FRONTEND
from workbench import _wb_apps_read

from .manifest import action_spec, validate_agent_control, validate_value
from .native_l1 import NATIVE_OBSERVATION_TIMEOUT_MS, NativeL1Mixin


# 等人确认的期限。比动作超时宽得多:人要拿起手机、点开卡片,几秒钟不够。
# 三分钟仍不点就按失败落地——让 Agent 拿到明确结果重来,好过无限期挂着。
APPROVAL_TTL_S = 180
# 已经推过飞书的那条,期限单独放宽。默认 180 秒是给「人就在屏幕前」定的;而通知本身
# 延后 25 秒才发(见 NOTIFY_DELAY_S),人还要解锁手机、点开链接、等 Desk 加载,余量所剩
# 无几——赶到时看到的常是一张已超时的死卡,比不通知更让人无从判断。
NOTIFIED_TTL_S = 900
HOST_ONLINE_TTL_S = 5
DESKTOP_STATUS_TTL_S = 30
DESKTOP_PROTOCOL = "desk.desktop-control/v1alpha1"
DESKTOP_HOST_BUNDLE_ID = "io.github.desktron"

TERMINAL = {"succeeded", "failed", "rejected", "cancelled"}
WRITE_EFFECTS = {"external_write", "destructive"}


class AppControlError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value, default=None):
    try:
        return json.loads(value) if value else default
    except Exception:
        return default


def _contains_key(value, wanted):
    if isinstance(value, dict):
        return wanted in value or any(_contains_key(v, wanted) for v in value.values())
    if isinstance(value, list):
        return any(_contains_key(v, wanted) for v in value)
    return False


def _redact_input(value):
    """诊断只保留定位与动作形状；输入文字和表单值永不进入 trace。"""
    if isinstance(value, dict):
        return {k: ("[redacted]" if k in {"value", "values", "text"} else _redact_input(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_input(v) for v in value]
    return value


def _clean_intent(value):
    """审批用的一句话业务意图；只改善说明，不参与权限判断。"""
    text = " ".join(str(value or "").split()).strip()
    if text.lower() in {"intent", "todo", "placeholder", "n/a"} or text in {
            "意图", "执行意图", "待补充", "暂无"}:
        return ""
    return text[:160]


class AppControlRuntime(NativeL1Mixin):
    def __init__(self, db_path=None, apps_provider=None, clock=None, evidence_dir=None):
        self.db_path = db_path or os.path.join(DATA_HOME, "app-control.sqlite3")
        self.apps_provider = apps_provider or self._read_apps
        self.clock = clock or time.time
        self.evidence_dir = evidence_dir or os.path.join(DATA_HOME, "app-control", "evidence")
        self._lock = threading.RLock()
        self._host_seen = {}
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._schema()
        self._migrate()
        self._recover()

    def close(self):
        with self._lock:
            self.db.close()

    def _schema(self):
        with self.db:
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS leases (
                  id TEXT PRIMARY KEY,
                  app_id TEXT NOT NULL,
                  resource_id TEXT NOT NULL,
                  window_id TEXT NOT NULL,
                  session_id TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  expires_at REAL NOT NULL,
                  revoked_reason TEXT NOT NULL DEFAULT '',
                  kind TEXT NOT NULL DEFAULT 'delegated'
                );
                CREATE INDEX IF NOT EXISTS leases_scope
                  ON leases(app_id, resource_id, status, updated_at DESC);
                -- 站点常驻授权:人一次点头,之后同一 origin 上不再逐个标签问。
                -- 只对「带用户身份」的标签有意义;隔离标签本来就不需要授权。
                CREATE TABLE IF NOT EXISTS site_grants (
                  id TEXT PRIMARY KEY,
                  app_id TEXT NOT NULL,
                  origin TEXT NOT NULL,
                  scope_key TEXT NOT NULL DEFAULT '',
                  created_at REAL NOT NULL,
                  expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS site_grants_lookup
                  ON site_grants(app_id, origin, expires_at DESC);
                CREATE TABLE IF NOT EXISTS operations (
                  id TEXT PRIMARY KEY,
                  lease_id TEXT NOT NULL REFERENCES leases(id),
                  app_id TEXT NOT NULL,
                  resource_id TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  turn_id TEXT NOT NULL DEFAULT '',
                  turn_seq INTEGER NOT NULL DEFAULT 0,
                  after_part_id TEXT NOT NULL DEFAULT '',
                  after_part_index INTEGER NOT NULL DEFAULT 0,
                  kind TEXT NOT NULL,
                  name TEXT NOT NULL,
                  effect TEXT NOT NULL,
                  intent TEXT NOT NULL DEFAULT '',
                  input_json TEXT NOT NULL,
                  expected_version TEXT NOT NULL DEFAULT '',
                  idempotency_key TEXT NOT NULL DEFAULT '',
                  timeout_ms INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  approval_state TEXT NOT NULL DEFAULT '',
                  approval_cleared_at REAL,
                  approval_ticket_hash TEXT NOT NULL DEFAULT '',
                  approval_ticket_expires_at REAL,
                  approval_ticket_used_at REAL,
                  output_json TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT '',
                  before_version TEXT NOT NULL DEFAULT '',
                  after_version TEXT NOT NULL DEFAULT '',
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  started_at REAL,
                  finished_at REAL
                );
                CREATE INDEX IF NOT EXISTS operations_queue
                  ON operations(app_id, resource_id, status, created_at);
                CREATE INDEX IF NOT EXISTS operations_idempotency
                  ON operations(lease_id, idempotency_key);
                CREATE TABLE IF NOT EXISTS observations (
                  app_id TEXT NOT NULL,
                  resource_id TEXT NOT NULL,
                  version TEXT NOT NULL,
                  value_json TEXT NOT NULL,
                  updated_at REAL NOT NULL,
                  PRIMARY KEY(app_id, resource_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  type TEXT NOT NULL,
                  app_id TEXT NOT NULL,
                  resource_id TEXT NOT NULL,
                  lease_id TEXT NOT NULL DEFAULT '',
                  operation_id TEXT NOT NULL DEFAULT '',
                  payload_json TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
                -- 原生宿主只上报系统能力状态；这里没有 AX 元素、截图或动作入口。
                CREATE TABLE IF NOT EXISTS desktop_host_capabilities (
                  capability TEXT PRIMARY KEY,
                  protocol TEXT NOT NULL,
                  host_instance_id TEXT NOT NULL,
                  host_bundle_id TEXT NOT NULL,
                  host_pid INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  checked_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                );
                -- 预授权 capability 与单 operation 审批票据严格分表、分前缀、分语义。
                -- bearer 明文只在签发回执中出现一次，数据库仅保存 SHA-256。
                CREATE TABLE IF NOT EXISTS desktop_capability_tickets (
                  id TEXT PRIMARY KEY,
                  token_hash TEXT NOT NULL UNIQUE,
                  app_id TEXT NOT NULL,
                  bundle_id TEXT NOT NULL,
                  pid INTEGER NOT NULL,
                  window_id TEXT NOT NULL,
                  allowed_actions_json TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  task_id TEXT NOT NULL,
                  issued_by TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  expires_at REAL NOT NULL,
                  revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS desktop_capability_scope
                  ON desktop_capability_tickets(app_id,bundle_id,pid,window_id,status,expires_at);
                -- 原生窗口委托只能由宿主建立。Agent 看不到窗口列表，只能认领已由人选择、
                -- 且预先绑定到当前会话的精确目标。
                CREATE TABLE IF NOT EXISTS desktop_targets (
                  lease_id TEXT PRIMARY KEY REFERENCES leases(id),
                  app_id TEXT NOT NULL,
                  bundle_id TEXT NOT NULL,
                  pid INTEGER NOT NULL,
                  process_start_token TEXT NOT NULL,
                  code_identity TEXT NOT NULL,
                  window_id TEXT NOT NULL,
                  target_generation TEXT NOT NULL,
                  interaction_epoch INTEGER NOT NULL,
                  host_instance_id TEXT NOT NULL,
                  host_pid INTEGER NOT NULL,
                  manifest_fingerprint TEXT NOT NULL,
                  status TEXT NOT NULL,
                  selected_at REAL NOT NULL,
                  last_verified_at REAL NOT NULL,
                  invalidated_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS desktop_targets_scope
                  ON desktop_targets(app_id,status,target_generation);
            """)

    def _migrate(self):
        """旧库补列。CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段。"""
        with self.db:
            have = {r["name"] for r in self.db.execute("PRAGMA table_info(leases)")}
            if "kind" not in have:
                self.db.execute("ALTER TABLE leases ADD COLUMN kind TEXT NOT NULL DEFAULT 'delegated'")
            if "needs_inspect" not in have:
                self.db.execute("ALTER TABLE leases ADD COLUMN needs_inspect INTEGER NOT NULL DEFAULT 0")
            cols = {r["name"] for r in self.db.execute("PRAGMA table_info(operations)")}
            if "notified_at" not in cols:
                self.db.execute("ALTER TABLE operations ADD COLUMN notified_at REAL")
            capability_have = {r["name"] for r in self.db.execute(
                "PRAGMA table_info(desktop_host_capabilities)")}
            if "monitoring_status" not in capability_have:
                self.db.execute(
                    "ALTER TABLE desktop_host_capabilities ADD COLUMN monitoring_status "
                    "TEXT NOT NULL DEFAULT 'unavailable'")
            op_have = {r["name"] for r in self.db.execute("PRAGMA table_info(operations)")}
            for name, ddl in (("turn_id", "TEXT NOT NULL DEFAULT ''"),
                              ("turn_seq", "INTEGER NOT NULL DEFAULT 0"),
                              ("after_part_id", "TEXT NOT NULL DEFAULT ''"),
                              ("after_part_index", "INTEGER NOT NULL DEFAULT 0"),
                              ("approval_cleared_at", "REAL"),
                              ("approval_ticket_hash", "TEXT NOT NULL DEFAULT ''"),
                              ("approval_ticket_expires_at", "REAL"),
                              ("approval_ticket_used_at", "REAL"),
                              ("wait_json", "TEXT NOT NULL DEFAULT ''"),
                              ("wait_deadline", "REAL"),
                              ("poll_after", "REAL NOT NULL DEFAULT 0"),
                              ("approval_guard_json", "TEXT NOT NULL DEFAULT ''"),
                              ("dispatch_id", "TEXT NOT NULL DEFAULT ''"),
                              ("intent", "TEXT NOT NULL DEFAULT ''"),
                              ("native_epoch", "INTEGER NOT NULL DEFAULT -1")):
                if name not in op_have:
                    self.db.execute("ALTER TABLE operations ADD COLUMN %s %s" % (name, ddl))

    def _recover(self):
        """只恢复只读条件探测；执行中的写动作与宿主能力绝不跨重启沿用。"""
        now = self.clock()
        with self.db:
            self.db.execute("DELETE FROM desktop_host_capabilities")
            native = self.db.execute(
                "SELECT lease_id,app_id FROM desktop_targets WHERE status='active'").fetchall()
            for target in native:
                self.db.execute(
                    "UPDATE desktop_targets SET status='invalidated',invalidated_reason='runtime_restarted' "
                    "WHERE lease_id=?", (target["lease_id"],))
                self.db.execute(
                    "UPDATE leases SET status='revoked',updated_at=?,revoked_reason='runtime_restarted' "
                    "WHERE id=? AND status IN ('offered','active','paused')",
                    (now, target["lease_id"]))
                self.db.execute(
                    "UPDATE operations SET status='cancelled',error='lease_revoked:runtime_restarted',"
                    "updated_at=?,finished_at=? WHERE lease_id=? AND status IN "
                    "('queued','waiting_user','running')", (now, now, target["lease_id"]))
            self.db.execute(
                "UPDATE desktop_capability_tickets SET status='revoked',revoked_at=? "
                "WHERE status='active'", (now,))
            self.db.execute(
                "UPDATE operations SET status='queued',started_at=NULL,dispatch_id='',poll_after=? "
                "WHERE status='running' AND wait_json<>''", (now,))
            rows = self.db.execute(
                "SELECT id,app_id,resource_id,lease_id FROM operations WHERE status='running'"
            ).fetchall()
            self.db.execute(
                "UPDATE operations SET status='failed', error=?, updated_at=?, finished_at=? "
                "WHERE status='running'",
                ("runtime_restarted_during_execution", now, now),
            )
            for row in rows:
                self._event("failed", row["app_id"], row["resource_id"],
                            row["lease_id"], row["id"], {"reason": "runtime_restarted"})

    @staticmethod
    def _read_apps():
        rows = []
        try:
            with open(os.path.join(FRONTEND, "workbench.json"), encoding="utf-8") as f:
                manifest = json.load(f)
                rows.extend(manifest.get("desktopApps") or [])
                rows.extend(manifest.get("hostApps") or [])
                # 原生 manifest 只能来自发行版清单；运行态 .workbench/apps.json 不得
                # 借宿主的全局 AX 权限扩展到任意 bundle。
                for app in manifest.get("nativeApps") or []:
                    if isinstance(app, dict):
                        app = dict(app)
                        app["_native_manifest_source"] = "builtin"
                    rows.append(app)
        except Exception:
            pass
        rows.extend(_wb_apps_read())
        return rows

    @staticmethod
    def _self_open_capabilities(control):
        scopes = {str(row.get("scope") or "") for row in control.get("resources") or []
                  if isinstance(row, dict)}
        return {
            "isolated": "agent-owned-isolated-tab" in scopes,
            "identified": "agent-owned-identified-tab" in scopes,
        }

    @staticmethod
    def _delegation_action(control):
        scopes = {str(row.get("scope") or "") for row in control.get("resources") or []
                  if isinstance(row, dict)}
        if "delegated-application-window" in scopes:
            return "delegate_native_window"
        return ("delegate_current_surface" if "delegated-current-surface" in scopes
                else "delegate_visible_tab")

    def controlled_apps(self):
        out = []
        seen = set()
        for app in self.apps_provider() or []:
            if not isinstance(app, dict) or not isinstance(app.get("agent_control"), dict):
                continue
            app_id = str(app.get("id") or "")
            if not app_id or app_id in seen:
                continue
            errors = validate_agent_control(app["agent_control"])
            out.append({
                "id": app_id,
                "uid": app.get("uid") or "",
                "label": app.get("label") or app_id,
                "level": app["agent_control"].get("level"),
                "protocol": app["agent_control"].get("protocol"),
                "resources": app["agent_control"].get("resources") or [],
                "observations": app["agent_control"].get("observations") or [],
                "actions": app["agent_control"].get("actions") or [],
                "native_application": app["agent_control"].get("native_application"),
                "available": not errors,
                "errors": errors,
            })
            seen.add(app_id)
        return out

    def host_seen(self, app_id):
        """登记现有 adapter 的一次取活；原生应用尚无 operation 取活路径。"""
        self._control_for_existing_adapter(app_id)
        with self._lock:
            self._host_seen[app_id] = self.clock()
        return self.host_status(app_id)

    def host_status(self, app_id):
        with self._lock:
            seen_at = self._host_seen.get(app_id)
        online = seen_at is not None and self.clock() - seen_at <= HOST_ONLINE_TTL_S
        return {"status": "online" if online else "unavailable",
                "last_seen_at": seen_at}

    def report_desktop_capability(self, host_instance_id, host_bundle_id, host_pid,
                                  status, checked_at=None, monitoring_status="unavailable"):
        """记录原生宿主的只读 Accessibility 探针，不接受 Agent 自报。"""
        instance = str(host_instance_id or "").strip()
        bundle = str(host_bundle_id or "").strip()
        try:
            pid = int(host_pid)
        except (TypeError, ValueError):
            raise AppControlError("host_pid 非法", 422)
        if not instance or pid <= 0:
            raise AppControlError("host_instance_id 与 host_pid 必填", 422)
        if bundle != DESKTOP_HOST_BUNDLE_ID:
            raise AppControlError("宿主 bundle identity 不匹配", 403)
        expected_instance = os.environ.get("DESKTRON_HOST_INSTANCE_ID", "")
        if expected_instance and not hmac.compare_digest(instance, expected_instance):
            raise AppControlError("宿主 instance identity 不匹配", 403)
        if os.environ.get("DESKTRON_NATIVE_LAUNCH") == "1" and pid != os.getppid():
            raise AppControlError("宿主 pid identity 不匹配", 403)
        state = str(status or "")
        # unsupported:宿主平台没有对应的只读观察实现(当前为 Windows 壳)，不同于“未授权”。
        if state not in {"trusted", "not_trusted", "unsupported"}:
            raise AppControlError("Accessibility 状态只能是 trusted/not_trusted/unsupported", 422)
        monitoring = str(monitoring_status or "unavailable")
        if monitoring not in {"ready", "unavailable"}:
            raise AppControlError("用户介入监测状态非法", 422)
        if state != "trusted":
            monitoring = "unavailable"
        now = self.clock()
        observed = now if checked_at is None else float(checked_at)
        # 不接受非有限值、未来或陈旧进程的状态，避免坏回执冻结当前事实。
        if not math.isfinite(observed) or observed > now + 5 or observed < now - DESKTOP_STATUS_TTL_S:
            raise AppControlError("Accessibility 探针时间无效", 422)
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO desktop_host_capabilities(capability,protocol,host_instance_id,"
                "host_bundle_id,host_pid,status,checked_at,updated_at,monitoring_status) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(capability) DO UPDATE SET "
                "protocol=excluded.protocol,host_instance_id=excluded.host_instance_id,"
                "host_bundle_id=excluded.host_bundle_id,host_pid=excluded.host_pid,"
                "status=excluded.status,checked_at=excluded.checked_at,updated_at=excluded.updated_at,"
                "monitoring_status=excluded.monitoring_status "
                "WHERE excluded.checked_at>=desktop_host_capabilities.checked_at",
                ("accessibility", DESKTOP_PROTOCOL, instance, bundle, pid, state, observed, now,
                 monitoring))
            # 宿主换代后旧实例绑定的窗口、进程与人介入监测都不再可信。
            current = self.db.execute(
                "SELECT host_instance_id,status,monitoring_status FROM desktop_host_capabilities "
                "WHERE capability='accessibility'").fetchone()
            if current:
                self._invalidate_foreign_host_targets_locked(current["host_instance_id"])
                if current["status"] != "trusted" or current["monitoring_status"] != "ready":
                    rows = self.db.execute(
                        "SELECT lease_id FROM desktop_targets WHERE status='active'").fetchall()
                    for target in rows:
                        self._drop_target_locked(target["lease_id"], "host_capability_lost")
        return self.desktop_status()

    def desktop_status(self):
        """返回能力事实；第一切片无论是否 trusted 都不宣称可执行原生动作。"""
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM desktop_host_capabilities WHERE capability='accessibility'"
            ).fetchone()
        capability = {"status": "not_reported"}
        monitoring = {"status": "not_reported"}
        host = {"status": "unavailable"}
        reason = "accessibility_not_reported"
        if row:
            capability = {"status": row["status"], "checked_at": row["checked_at"]}
            monitoring = {"status": row["monitoring_status"]}
            host = {"status": "online" if self.clock() - row["updated_at"] <= DESKTOP_STATUS_TTL_S
                    else "unavailable",
                    "instance_id": row["host_instance_id"],
                    "bundle_id": row["host_bundle_id"], "pid": row["host_pid"]}
            if host["status"] != "online":
                reason = "host_status_stale"
            elif row["status"] == "not_trusted":
                reason = "accessibility_not_trusted"
            elif row["status"] == "unsupported":
                reason = "platform_unsupported"
            elif row["monitoring_status"] != "ready":
                reason = "user_intervention_monitor_unavailable"
            else:
                reason = ""
        execution = ({"status": "ready"} if not reason else
                     {"status": "unavailable", "reason": reason})
        return {"protocol": DESKTOP_PROTOCOL, "host": host,
                "capabilities": {"accessibility": capability,
                                 "user_intervention_monitor": monitoring},
                "execution": execution}

    @staticmethod
    def _desktop_ticket_digest(ticket):
        return hashlib.sha256(str(ticket or "").encode("utf-8")).hexdigest()

    def _native_application(self, app_id):
        app = self._app(app_id)
        control = app.get("agent_control")
        native = control.get("native_application") if isinstance(control, dict) else None
        if not isinstance(native, dict):
            raise AppControlError("应用未声明独立 native_application identity", 422)
        if app.get("_native_manifest_source") != "builtin":
            raise AppControlError("原生应用 manifest 不属于受信任发行清单", 403)
        return control, native

    @staticmethod
    def _manifest_fingerprint(control):
        canonical = json.dumps(control, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def desktop_apps_for_host(self):
        """仅供可信宿主读取的原生 allowlist；Agent 不获得窗口或进程枚举。"""
        apps = []
        for raw in self.apps_provider() or []:
            if not isinstance(raw, dict) or raw.get("_native_manifest_source") != "builtin":
                continue
            control = raw.get("agent_control")
            if validate_agent_control(control):
                continue
            native = control.get("native_application")
            if not isinstance(native, dict):
                continue
            apps.append({
                "id": str(raw.get("id") or ""),
                "label": str(raw.get("label") or raw.get("id") or ""),
                "bundle_id": str(native.get("bundle_id") or ""),
                "code_requirement": str(native.get("code_requirement") or ""),
                "projector": str(native.get("projector") or ""),
                "allowed_roles": list(native.get("allowed_roles") or []),
                "allowed_semantics": list(native.get("allowed_semantics") or []),
                "allowed_values": list(native.get("allowed_values") or []),
                "label_values": dict(native.get("label_values") or {}),
                "manifest_fingerprint": self._manifest_fingerprint(control),
            })
        return apps

    def _control_for_existing_adapter(self, app_id):
        control = self._control(app_id)
        if isinstance(control.get("native_application"), dict):
            raise AppControlError(
                "原生应用第一切片不可创建 lease 或 operation；需专用 capability 执行路径", 409)
        return control

    def issue_desktop_capability(self, app_id, bundle_id, pid, window_id,
                                 allowed_actions, session_id, task_id,
                                 ttl_seconds=300, issued_by="native_user"):
        """签发精确作用域的预授权 capability；不创建 operation，也不进入派发队列。"""
        control, native = self._native_application(str(app_id or ""))
        bundle = str(bundle_id or "").strip()
        if bundle != str(native.get("bundle_id") or ""):
            raise AppControlError("bundle_id 超出应用声明", 403)
        try:
            target_pid = int(pid)
        except (TypeError, ValueError):
            raise AppControlError("pid 非法", 422)
        window = str(window_id or "").strip()
        sid = str(session_id or "").strip()
        tid = str(task_id or "").strip()
        if target_pid <= 0 or not window or not sid or not tid:
            raise AppControlError("pid、window_id、session_id 与 task_id 必填", 422)
        requested = sorted(set(str(x or "").strip() for x in (allowed_actions or []) if str(x or "").strip()))
        declared = {str(row.get("name") or "") for row in control.get("actions") or []}
        if not requested or any(action not in declared for action in requested):
            raise AppControlError("allowed_actions 超出应用动作白名单", 403)
        try:
            ttl = int(300 if ttl_seconds is None else ttl_seconds)
        except (TypeError, ValueError):
            raise AppControlError("ttl_seconds 非法", 422)
        if ttl <= 0 or ttl > 900:
            raise AppControlError("ttl_seconds 必须在 1 到 900 之间", 422)
        now = self.clock()
        ticket_id = "dcap_" + uuid.uuid4().hex
        bearer = ticket_id + "." + secrets.token_urlsafe(32)
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO desktop_capability_tickets(id,token_hash,app_id,bundle_id,pid,window_id,"
                "allowed_actions_json,session_id,task_id,issued_by,status,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ticket_id, self._desktop_ticket_digest(bearer), str(app_id), bundle, target_pid,
                 window, _json(requested), sid, tid, str(issued_by or "native_user"),
                 "active", now, now + ttl))
        return {"id": ticket_id, "ticket": bearer, "protocol": DESKTOP_PROTOCOL,
                "app_id": str(app_id), "bundle_id": bundle, "pid": target_pid,
                "window_id": window, "allowed_actions": requested,
                "session_id": sid, "task_id": tid, "expires_at": now + ttl}

    def verify_desktop_capability(self, ticket, app_id, bundle_id, pid, window_id,
                                  action, session_id, task_id):
        """按所有 claims 精确校验；成功只证明授权，不代表动作已创建或执行。"""
        digest = self._desktop_ticket_digest(ticket)
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT * FROM desktop_capability_tickets WHERE token_hash=?", (digest,)
            ).fetchone()
            if not row or row["status"] != "active":
                raise AppControlError("桌面 capability ticket 无效", 403)
            try:
                control, native = self._native_application(row["app_id"])
            except AppControlError:
                raise AppControlError("桌面 capability ticket 对应应用已失效", 403)
            declared = {str(item.get("name") or "") for item in control.get("actions") or []}
            stored_actions = set(_loads(row["allowed_actions_json"], []))
            if (row["bundle_id"] != str(native.get("bundle_id") or "")
                    or not stored_actions or not stored_actions.issubset(declared)):
                raise AppControlError("桌面 capability ticket 对应 manifest 已变化", 403)
            if row["expires_at"] <= self.clock():
                self.db.execute(
                    "UPDATE desktop_capability_tickets SET status='expired' WHERE id=?",
                    (row["id"],))
                raise AppControlError("桌面 capability ticket 已过期", 403)
        claims = (str(app_id), str(bundle_id), int(pid), str(window_id),
                  str(session_id), str(task_id))
        expected = (row["app_id"], row["bundle_id"], row["pid"], row["window_id"],
                    row["session_id"], row["task_id"])
        if claims != expected or str(action) not in set(_loads(row["allowed_actions_json"], [])):
            raise AppControlError("桌面 capability ticket 作用域不匹配", 403)
        return {"id": row["id"], "protocol": DESKTOP_PROTOCOL,
                "app_id": row["app_id"], "bundle_id": row["bundle_id"],
                "pid": row["pid"], "window_id": row["window_id"],
                "allowed_actions": _loads(row["allowed_actions_json"], []),
                "session_id": row["session_id"], "task_id": row["task_id"],
                "expires_at": row["expires_at"]}

    @staticmethod
    def _decorate_execution_status(result):
        """保留授权/租约事实，同时明确当前是否有宿主可以执行。"""
        control = result["control"]
        control_status = control.get("status")
        self_open = result.get("self_open") or {}
        if not result.get("app", {}).get("available"):
            control["execution_status"] = "unavailable"
            control["execution_reason"] = "app_unavailable"
            unavailable_reason = "app_unavailable"
        elif result.get("host", {}).get("status") != "online":
            control["execution_status"] = "unavailable"
            control["execution_reason"] = "host_unavailable"
            unavailable_reason = "host_unavailable"
        elif control_status == "ready":
            control["execution_status"] = "ready"
            unavailable_reason = ""
        else:
            control["execution_status"] = "blocked"
            control["execution_reason"] = control_status
            unavailable_reason = ""
        if unavailable_reason:
            for capability in self_open.values():
                capability["status"] = "unavailable"
                capability["reason"] = unavailable_reason
        return result

    def probe(self, session_id="", app_id="browser"):
        """只读地说明 Desk 原生控制通道与当前委托状态。

        Browser Plugin/node_repl 属于 ChatGPT/Codex 宿主的原生管道，不能连接 Desk 的
        WKWebView。把这点作为机器可读状态返回，避免 Agent 因工具描述相似而走错通道。
        """
        apps = self.controlled_apps()
        app = next((row for row in apps if row.get("id") == app_id), None)
        result = {
            "protocol": "desk.app-control/v1alpha1",
            "transport": {"kind": "desk_app_control_mcp", "status": "ready"},
            "node_repl": {"status": "not_used"},
            "browser_plugin": {
                "status": "browser_plugin_unavailable",
                "reason": "OpenAI Browser Plugin native pipe does not host Desk WKWebView",
            },
            "app": app or {"id": app_id, "available": False},
            "host": self.host_status(app_id),
            "control": {"status": "app_unavailable"},
        }
        if app and isinstance(app.get("native_application"), dict):
            desktop = self.desktop_status()
            result["desktop"] = desktop
            result["host"] = desktop["host"]
            builtin = self._app(app_id).get("_native_manifest_source") == "builtin"
            if (desktop["execution"]["status"] != "ready" or not app.get("available")
                    or not builtin):
                result["control"] = {
                    "status": "unavailable", "execution_status": "unavailable",
                    "execution_reason": ("native_manifest_untrusted" if not builtin else
                                         desktop["execution"].get("reason") or "app_unavailable"),
                }
                return result
        if not app or not app.get("available"):
            return self._decorate_execution_status(result)
        with self._lock, self.db:
            self._expire()
            rows = self.db.execute(
                "SELECT * FROM leases WHERE app_id=? AND status IN ('offered','active','paused') "
                "ORDER BY updated_at DESC", (app_id,)).fetchall()
        active = next((row for row in rows if row["status"] == "active"
                       and session_id and row["session_id"] == session_id), None)
        # 无会话的设置页只探测宿主健康，不能借此看见任何可认领资源。
        offered = next((row for row in rows if session_id and row["status"] == "offered"
                        and (not row["session_id"] or row["session_id"] == session_id)), None)
        occupied = next((row for row in rows if row["status"] == "active"), None)
        paused = next((row for row in rows if row["status"] == "paused"
                       and session_id and row["session_id"] == session_id), None)
        if active:
            result["control"] = {"status": "ready", "lease": self._lease_dict(active)}
        elif paused:
            result["control"] = {"status": "paused", "reason": paused["revoked_reason"],
                                 "lease": self._lease_dict(paused)}
        elif offered:
            result["control"] = {"status": "available_to_claim",
                                 "lease": self._lease_dict(offered)}
        elif occupied:
            result["control"] = {"status": "busy"}
        control = self._control(app_id)
        if result["control"]["status"] == "app_unavailable":
            result["control"] = {"status": "needs_authorization",
                                 "action": self._delegation_action(control)}
        capabilities = self._self_open_capabilities(control)
        if any(capabilities.values()):
            result["self_open"] = {}
            if capabilities["isolated"]:
                result["self_open"]["isolated"] = {
                    "status": "ready", "needs_approval": False}
            if capabilities["identified"]:
                result["self_open"]["identified"] = {
                    "status": "ready", "needs_approval": True}
        return self._decorate_execution_status(result)

    def _control(self, app_id):
        for app in self.apps_provider() or []:
            if isinstance(app, dict) and app.get("id") == app_id:
                control = app.get("agent_control")
                errors = validate_agent_control(control) if control is not None else ["应用未声明 agent_control"]
                if errors:
                    raise AppControlError("; ".join(errors), 422)
                return control
        raise AppControlError("应用不存在或不可控制", 404)

    def _event(self, typ, app_id, resource_id, lease_id="", operation_id="", payload=None):
        self.db.execute(
            "INSERT INTO events(type,app_id,resource_id,lease_id,operation_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (typ, app_id, resource_id, lease_id, operation_id, _json(payload or {}), self.clock()),
        )

    def _expire(self):
        now = self.clock()
        # 探测丢回执只重试读取。总期限独立于每轮宿主超时，重启也不会重置。
        self.db.execute(
            "UPDATE operations SET status='queued',started_at=NULL,dispatch_id='',poll_after=? "
            "WHERE status='running' AND wait_json<>'' AND started_at+5<=?",
            (now + 1, now))
        expired_waits = self.db.execute(
            "SELECT * FROM operations WHERE wait_json<>'' AND status IN ('queued','running') "
            "AND wait_deadline<=?", (now,)).fetchall()
        for row in expired_waits:
            reason = 'postcondition_timeout' if row['name'] != 'wait' else 'wait_timeout'
            output = {**_loads(row['output_json'], {}), 'phase': 'failed', 'condition_met': False}
            self.db.execute(
                "UPDATE operations SET status='failed',error=?,output_json=?,updated_at=?,finished_at=? WHERE id=?",
                (reason, _json(output), now, now, row['id']))
            self._event('failed', row['app_id'], row['resource_id'], row['lease_id'], row['id'],
                        {'reason': reason})
        timed_out = self.db.execute(
            "SELECT id,app_id,resource_id,lease_id FROM operations WHERE status='running' "
            "AND wait_json='' AND started_at IS NOT NULL AND started_at + (timeout_ms / 1000.0) <= ?", (now,)
        ).fetchall()
        for row in timed_out:
            self.db.execute(
                "UPDATE operations SET status='failed',error='operation_timeout',updated_at=?,finished_at=? "
                "WHERE id=? AND status='running'", (now, now, row["id"]),
            )
            self._event("failed", row["app_id"], row["resource_id"], row["lease_id"], row["id"],
                        {"reason": "timeout"})

        # 等人确认的也要有期限。原来只超时 running,waiting_user 一挂就是永远——
        # 人没看见(在手机上、或页面没刷新)那条就一直悬着,Agent 那侧同样一直等
        # (实见一条挂了 156 秒仍在队列里)。等不到答复就是没批准,按失败落地,
        # 让 Agent 拿到明确结果重来,而不是无限期挂着。
        # 期限单独一档、比动作本身宽:人要拿起手机、点开卡片,几秒的动作超时不够用。
        # 推过通知的走宽档:notified_at 有值即认。两档写在同一条查询里,免得两次扫描
        # 之间又有动作被通知,落进错的那一档。
        stale = self.db.execute(
            "SELECT id,app_id,resource_id,lease_id FROM operations WHERE status='waiting_user' "
            "AND created_at + (CASE WHEN COALESCE(notified_at,0)>0 THEN ? ELSE ? END) <= ?",
            (NOTIFIED_TTL_S, APPROVAL_TTL_S, now)
        ).fetchall()
        for row in stale:
            self.db.execute(
                "UPDATE operations SET status='failed',error='approval_timeout',"
                "approval_state='timeout',updated_at=?,finished_at=? "
                "WHERE id=? AND status='waiting_user'", (now, now, row["id"]),
            )
            self._event("failed", row["app_id"], row["resource_id"], row["lease_id"], row["id"],
                        {"reason": "approval_timeout"})
        rows = self.db.execute(
            "SELECT id,app_id,resource_id FROM leases WHERE status IN ('offered','active','paused') "
            "AND expires_at<=?", (now,),
        ).fetchall()
        for row in rows:
            self.db.execute(
                "UPDATE leases SET status='expired',updated_at=?,revoked_reason='ttl' WHERE id=?",
                (now, row["id"]),
            )
            self.db.execute(
                "UPDATE operations SET status='cancelled',error='lease_expired',"
                "approval_state=CASE WHEN approval_state='pending' THEN 'cancelled' ELSE approval_state END,"
                "updated_at=?,finished_at=? "
                "WHERE lease_id=? AND (status IN ('queued','waiting_user') OR "
                "(status='running' AND effect='read_only'))",
                (now, now, row["id"]),
            )
            self._event("lease_revoked", row["app_id"], row["resource_id"], row["id"],
                        payload={"reason": "ttl"})
        self._invalidate_dead_lease_targets_locked()

    @staticmethod
    def _lease_dict(row):
        return dict(row) if row else None

    @staticmethod
    def _operation_dict(row):
        if not row:
            return None
        out = dict(row)
        out["input"] = _loads(out.pop("input_json", ""), {})
        out["output"] = _loads(out.pop("output_json", ""), None)
        out.pop('wait_json', None)  # 内部探测参数可能含正文，不作为诊断旁路暴露。
        out.pop('approval_guard_json', None)
        out.pop('approval_ticket_hash', None)
        out.pop('approval_ticket_expires_at', None)
        out.pop('approval_ticket_used_at', None)
        status = out.get('status')
        out['next_step'] = ('wait_operation' if status in ('queued', 'running', 'waiting_user') else
                            'stop' if status in ('rejected', 'cancelled') else
                            'inspect_or_verify_postcondition' if status == 'succeeded' else
                            'inspect_before_new_request')
        return out

    def offer(self, app_id, resource_id, window_id, ttl_seconds=300, session_id=""):
        self._control_for_existing_adapter(app_id)
        if not resource_id or not window_id:
            raise AppControlError("resource_id 与 window_id 必填")
        ttl = max(30, min(int(ttl_seconds or 300), 3600))
        now = self.clock()
        lease_id = "lease_" + uuid.uuid4().hex
        with self._lock, self.db:
            self._expire()
            old = self.db.execute(
                "SELECT id FROM leases WHERE app_id=? AND resource_id=? "
                "AND status IN ('offered','active','paused')", (app_id, resource_id),
            ).fetchall()
            for row in old:
                self._revoke_locked(row["id"], "replaced_by_user_delegation")
            self.db.execute(
                "INSERT INTO leases(id,app_id,resource_id,window_id,session_id,status,created_at,updated_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (lease_id, app_id, resource_id, window_id, str(session_id or ""),
                 "offered", now, now, now + ttl),
            )
            self._event("ready", app_id, resource_id, lease_id, payload={"status": "offered"})
        return self.get_lease(lease_id)

    def claim(self, session_id, app_id="", resource_id="", ttl_seconds=300):
        if not session_id:
            raise AppControlError("session_id 必填")
        now = self.clock()
        ttl = max(30, min(int(ttl_seconds or 300), 3600))
        with self._lock, self.db:
            self._expire()
            active_query = ("SELECT * FROM leases WHERE status='active' AND session_id=?")
            active_args = [session_id]
            if app_id:
                active_query += " AND app_id=?"; active_args.append(app_id)
            if resource_id:
                active_query += " AND resource_id=?"; active_args.append(resource_id)
            active_query += " ORDER BY updated_at DESC LIMIT 1"
            active = self.db.execute(active_query, active_args).fetchone()
            if active:
                self._control_for_agent(active["app_id"], active["id"])
                self.db.execute("UPDATE leases SET updated_at=?,expires_at=? WHERE id=?",
                                (now, now + ttl, active["id"]))
                return self.get_lease(active["id"])
            query = "SELECT * FROM leases WHERE status='offered' AND (session_id='' OR session_id=?)"
            args = [session_id]
            if app_id:
                query += " AND app_id=?"; args.append(app_id)
            if resource_id:
                query += " AND resource_id=?"; args.append(resource_id)
            query += " ORDER BY updated_at DESC LIMIT 1"
            row = self.db.execute(query, args).fetchone()
            if not row:
                raise AppControlError("没有等待认领的用户委托", 403)
            self._control_for_agent(row["app_id"], row["id"])
            self.db.execute(
                "UPDATE leases SET session_id=?,status='active',updated_at=?,expires_at=? WHERE id=?",
                (session_id, now, now + ttl, row["id"]),
            )
            self._event("changed", row["app_id"], row["resource_id"], row["id"],
                        payload={"status": "active", "session_id": session_id})
        return self.get_lease(row["id"])

    @staticmethod
    def _origin(url):
        """把 URL 归一成授权粒度:scheme://host[:port]。

        授权按 origin 记而不是整条 URL:人授权的是「这个站点」,不是某一个页面;
        但也不放宽到裸域名——http 与 https、不同端口是不同的信任面。
        """
        from urllib.parse import urlsplit
        parts = urlsplit(str(url or "").strip())
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise AppControlError("只允许 http/https 地址", 422)
        return "%s://%s" % (parts.scheme, parts.netloc.lower())

    def site_granted(self, app_id, origin, scope_key=""):
        now = self.clock()
        row = self.db.execute(
            "SELECT id FROM site_grants WHERE app_id=? AND origin=? AND scope_key IN ('', ?) "
            "AND expires_at>? LIMIT 1", (app_id, origin, str(scope_key or ""), now)).fetchone()
        return bool(row)

    def grant_site(self, app_id, origin, scope_key="", ttl_seconds=86400):
        """记下一次站点授权。origin 已归一;过期后自动失效,不做永久授权。"""
        origin = self._origin(origin) if "://" not in str(origin or "") else str(origin)
        now = self.clock()
        ttl = max(60, min(int(ttl_seconds or 86400), 30 * 86400))
        with self._lock, self.db:
            self.db.execute("DELETE FROM site_grants WHERE app_id=? AND origin=? AND scope_key=?",
                            (app_id, origin, str(scope_key or "")))
            self.db.execute(
                "INSERT INTO site_grants(id,app_id,origin,scope_key,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?)",
                ("grant_" + uuid.uuid4().hex, app_id, origin, str(scope_key or ""), now, now + ttl))
        return {"app_id": app_id, "origin": origin, "scope_key": str(scope_key or ""),
                "expires_at": now + ttl}

    def site_grants(self, app_id="", scope_key=""):
        now = self.clock()
        with self._lock, self.db:
            self.db.execute("DELETE FROM site_grants WHERE expires_at<=?", (now,))
            query = "SELECT app_id,origin,scope_key,created_at,expires_at FROM site_grants WHERE 1=1"
            args = []
            if app_id:
                query += " AND app_id=?"; args.append(app_id)
            if scope_key:
                query += " AND scope_key IN ('', ?)"; args.append(scope_key)
            return [dict(r) for r in self.db.execute(query + " ORDER BY created_at DESC", args)]

    def revoke_site(self, app_id, origin, scope_key=""):
        """撤销一个精确 origin 的带身份开页授权，不影响其他站点或普通标签委托。"""
        if not app_id:
            raise AppControlError("缺少 app_id", 422)
        origin = self._origin(origin)
        with self._lock, self.db:
            cur = self.db.execute(
                "DELETE FROM site_grants WHERE app_id=? AND origin=? AND scope_key=?",
                (app_id, origin, str(scope_key or "")),
            )
        return {"ok": True, "revoked": bool(cur.rowcount), "app_id": app_id,
                "origin": origin, "scope_key": str(scope_key or "")}

    def open_tab(self, session_id, app_id, url, isolated=True, scope_key="", anchor=None,
                 intent="", allowed_main_frame_hosts=None):
        """Agent 自行开一个标签并直接持有它。

        与「用户委托」是两种授权来源,不该混为一谈:
        - isolated=True:标签用隔离存储,不带用户的 cookie 与 SSO。它的能力上限就是
          一个刚装好的浏览器,不触及用户身份,因此无需任何人点头,Runtime 自签租约。
        - isolated=False:标签带用户身份(Desk 内置浏览器共享持久存储与内网 SSO),
          必须有该 origin 的站点授权;没有就把这次开页挂成待确认,由宿主问人,
          批准的同时记下授权,之后同一站点不再逐个标签问。
        """
        control = self._control_for_existing_adapter(app_id)
        if not session_id:
            raise AppControlError("session_id 必填", 400)
        capability = "identified" if not isolated else "isolated"
        if not self._self_open_capabilities(control)[capability]:
            raise AppControlError("应用未声明 Agent 自开资源，只能由用户显式委托", 403)
        origin = self._origin(url)
        parsed_host = (urllib.parse.urlsplit(url).hostname or "").lower().strip(".")
        raw_hosts = [] if allowed_main_frame_hosts is None else allowed_main_frame_hosts
        if not isinstance(raw_hosts, (list, tuple)) or len(raw_hosts) > 8:
            raise AppControlError("主框架域名白名单无效", 422)
        hosts = []
        for raw in raw_hosts:
            host = str(raw or "").lower().strip().strip(".")
            if not host or not re.fullmatch(r"[a-z0-9.-]+", host):
                raise AppControlError("主框架域名白名单无效", 422)
            if parsed_host != host:
                raise AppControlError("主框架域名白名单只能使用当前精确主机", 422)
            hosts.append(host)
        now = self.clock()
        isolated = bool(isolated)
        kind = "agent_isolated" if isolated else "agent_identified"
        resource_id = ("agent-" if isolated else "user-") + uuid.uuid4().hex[:12]
        lease_id = "lease_" + uuid.uuid4().hex
        with self._lock, self.db:
            self._expire()
            granted = isolated or self.site_granted(app_id, origin, scope_key)
            self.db.execute(
                "INSERT INTO leases(id,app_id,resource_id,window_id,session_id,status," 
                "created_at,updated_at,expires_at,kind) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (lease_id, app_id, resource_id, "agent", str(session_id), "active",
                 now, now, now + 900, kind))
            self._event("ready", app_id, resource_id, lease_id,
                        payload={"status": "active", "kind": kind, "origin": origin})
        # 开页本身是一次动作:走同一条队列,宿主取活执行,回执照常。带身份且未授权时
        # approval=always,宿主的确认框问的就是「要不要让 Agent 以你的身份进这个站点」。
        return self._enqueue(lease_id, session_id, "act", "open",
                             {"url": url, "isolated": isolated, "origin": origin,
                              "allowed_main_frame_hosts": sorted(set(hosts))},
                             "read_only" if granted else "external_write",
                             15000, "none", "", "",
                             approval="never" if granted else "always", anchor=anchor,
                             intent=intent)

    def get_lease(self, lease_id):
        with self._lock, self.db:
            self._expire()
            row = self.db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
            if not row:
                raise AppControlError("租约不存在", 404)
            return self._lease_dict(row)

    def heartbeat(self, lease_id, session_id, ttl_seconds=300):
        now = self.clock()
        with self._lock, self.db:
            row = self._active_lease(lease_id, session_id)
            self.db.execute("UPDATE leases SET updated_at=?,expires_at=? WHERE id=?",
                            (now, now + max(30, min(int(ttl_seconds), 3600)), row["id"]))
        return self.get_lease(lease_id)

    def _active_lease(self, lease_id, session_id=""):
        self._expire()
        row = self.db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
        if not row:
            raise AppControlError("租约不存在", 404)
        if session_id and row["session_id"] != session_id:
            raise AppControlError("租约不属于当前 Agent 会话", 403)
        if row["status"] == "paused":
            reason = row["revoked_reason"] or "user_interaction"
            recovery = ("请等待用户切回原标签，再 inspect document；也可 yield 归还控制"
                        if reason == "tab_switched" else
                        "请等待宿主确认用户停止操作，再 inspect document；也可 yield 归还控制")
            raise AppControlError(
                "租约已暂停(%s)。%s，不要用 activate 抢回控制。" % (reason, recovery), 409)
        if row["status"] != "active":
            reason = row["revoked_reason"] or ""
            hint = ""
            if reason == "browser_tab_unavailable":
                hint = "。标签视图已不存在(多半是 Desk 重启或标签被关),请重新 open,或请用户重新委托"
            raise AppControlError("租约不可用:%s%s%s" % (
                row["status"], "(%s)" % reason if reason else "", hint), 409)
        if session_id and row["session_id"] != session_id:
            raise AppControlError("租约不属于当前 Agent 会话", 403)
        return row

    def _revoke_locked(self, lease_id, reason):
        now = self.clock()
        row = self.db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
        if not row or row["status"] in ("revoked", "expired"):
            return self._lease_dict(row)
        self.db.execute(
            "UPDATE leases SET status='revoked',updated_at=?,revoked_reason=? WHERE id=?",
            (now, reason, lease_id),
        )
        self.db.execute(
            "UPDATE operations SET status='cancelled',error=?,"
            "approval_state=CASE WHEN approval_state='pending' THEN 'cancelled' ELSE approval_state END,"
            "updated_at=?,finished_at=? "
            "WHERE lease_id=? AND status IN ('queued','waiting_user','running')",
            ("lease_revoked:" + reason, now, now, lease_id),
        )
        self._invalidate_target_locked(lease_id, reason)
        self._event("lease_revoked", row["app_id"], row["resource_id"], lease_id,
                    payload={"reason": reason})
        return self._lease_dict(self.db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone())

    def revoke(self, lease_id, reason="user_takeover"):
        with self._lock, self.db:
            return self._revoke_locked(lease_id, reason)

    def pause(self, lease_id, reason="user_interaction"):
        """人碰了页面:让开,但不销毁租约。

        manifest 声明的就是 `user_interaction: pause`,而实现一直走的是 revoke——
        用户看一眼、点一下页面,租约就永久没了,必须从头重新委托(2026-09-02 实见)。
        同一个 DOM 不能有两个操作者,所以必须立刻让开;但让开不等于收回。

        DOM surface 的排队动作取消，恢复后先重新观察。已执行动作保留回执，不自动重放。
        """
        now = self.clock()
        with self._lock, self.db:
            row = self.db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
            if not row:
                raise AppControlError("租约不存在", 404)
            if row["status"] == "paused":
                self.db.execute("UPDATE leases SET updated_at=?,revoked_reason=? WHERE id=?",
                                (now, 'tab_switched' if reason == 'tab_switched'
                                 else row['revoked_reason'], lease_id))
                return self._lease_dict(self.db.execute(
                    "SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone())
            if row["status"] != "active":
                raise AppControlError("租约不处于可暂停状态:%s" % row["status"], 409)
            self.db.execute(
                "UPDATE leases SET status='paused',updated_at=?,revoked_reason=? WHERE id=?",
                (now, reason, lease_id))
            # 原生目标：推进人介入代次，已派发的观察回执随即作废。
            self.db.execute(
                "UPDATE desktop_targets SET interaction_epoch=interaction_epoch+1,"
                "last_verified_at=? WHERE lease_id=? AND status='active'", (now, lease_id))
            self.db.execute("UPDATE leases SET needs_inspect=1 WHERE id=?", (lease_id,))
            self.db.execute(
                "UPDATE operations SET status='cancelled',error='human_intervention_reinspect_required',"
                "approval_state=CASE WHEN approval_state='pending' THEN 'cancelled' ELSE approval_state END,"
                "updated_at=?,finished_at=? WHERE lease_id=? AND "
                "(status IN ('queued','waiting_user') OR (status='running' AND effect='read_only'))",
                (now, now, lease_id))
            self._event("waiting_user", row["app_id"], row["resource_id"], lease_id,
                        payload={"status": "paused", "reason": reason})
            return self._lease_dict(self.db.execute(
                "SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone())

    def resume(self, lease_id, ttl_seconds=900):
        """交还租约，但写操作仍需新的结构化观察解锁。"""
        now = self.clock()
        with self._lock, self.db:
            self._expire()
            row = self.db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
            if not row:
                raise AppControlError("租约不存在", 404)
            if row["status"] == "active":
                return self._lease_dict(row)
            if row["status"] != "paused":
                raise AppControlError("租约不处于暂停状态:%s" % row["status"], 409)
            ttl = max(30, min(int(ttl_seconds or 900), 3600))
            self.db.execute(
                "UPDATE leases SET status='active',updated_at=?,expires_at=?,revoked_reason='' "
                "WHERE id=?", (now, now + ttl, lease_id))
            self._event("changed", row["app_id"], row["resource_id"], lease_id,
                        payload={"status": "active", "reason": "user_idle"})
            return self._lease_dict(self.db.execute(
                "SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone())

    def yield_lease(self, lease_id, session_id):
        with self._lock, self.db:
            self._expire()
            row = self.db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
            if not row:
                raise AppControlError("租约不存在", 404)
            if not session_id or row['session_id'] != session_id:
                raise AppControlError("租约不属于当前 Agent 会话", 403)
            if row['status'] in ('revoked', 'expired'):
                return self._lease_dict(row)
            if row['status'] not in ('active', 'paused'):
                raise AppControlError("租约不可归还:%s" % row['status'], 409)
            return self._revoke_locked(lease_id, "agent_yield")

    def _observation_version(self, app_id, resource_id):
        row = self.db.execute(
            "SELECT version FROM observations WHERE app_id=? AND resource_id=?",
            (app_id, resource_id),
        ).fetchone()
        return row["version"] if row else ""

    def inspect(self, lease_id, session_id, observation="document", expected_version="",
                idempotency_key="", anchor=None, settle_ms=None, params=None, intent=""):
        with self._lock, self.db:
            lease = self._active_lease(lease_id, session_id)
            if self._is_native(lease["app_id"]):
                self._native_inspect(lease, observation, params, expected_version,
                                     idempotency_key, anchor, intent)
                native = True
            else:
                native = False
        if native:
            # 只读、人已选定窗口：不需审批，也不透传 settle_ms 等浏览器参数。
            return self._enqueue(lease_id, session_id, "inspect", observation, {}, "read_only",
                                 NATIVE_OBSERVATION_TIMEOUT_MS, "keyed", expected_version,
                                 idempotency_key, approval="never", anchor=anchor,
                                 intent=intent)
        with self._lock, self.db:
            lease = self._active_lease(lease_id, session_id)
            control = self._control_for_existing_adapter(lease["app_id"])
            spec = next((row for row in control.get("observations") or []
                         if row.get("name") == observation), None)
            if not spec:
                raise AppControlError("观察未声明:%s" % observation, 404)
            value = dict(params or {})
            errors = validate_value(spec.get("input_schema") or {}, value, "input")
            if errors:
                raise AppControlError("; ".join(errors), 422)
            if observation == "screenshot" and value.get("full_page") and value.get("clip"):
                raise AppControlError("截图的 full_page 与 clip 不能同时使用", 422)
            approval = "always" if self._observation_needs_approval(control, lease, spec) else "never"
        # settle_ms 交给宿主:重前端应用在 load 之后还会接着挂节点,读早了拿到的是半成品。
        # 宿主等到页面安定再读,并把 settled 一并放进输出;给 0 就是明说「现在就读」。
        if settle_ms is not None:
            value["settle_ms"] = float(settle_ms)
        return self._enqueue(lease_id, session_id, "inspect", observation, value, "read_only",
                             15000, "keyed", expected_version, idempotency_key,
                             approval=approval, anchor=anchor,
                             intent=intent)

    @staticmethod
    def _lease_scope(control, lease):
        """把租约的授权来源映射回应用在 manifest 里声明的资源 scope。

        租约的 kind 记录的是这份控制权从哪来:agent 自开的隔离标签、agent 自开的带身份
        标签、还是用户交出来的界面。三者对应 resources 里的三种 scope;应用没声明对应
        scope 时返回空串,由调用方按最严处理。
        """
        scopes = {str(row.get("scope") or "") for row in control.get("resources") or []
                  if isinstance(row, dict)}
        try:
            kind = str(lease["kind"] or "delegated")
        except (KeyError, IndexError, TypeError):
            kind = "delegated"
        named = {"agent_isolated": "agent-owned-isolated-tab",
                 "agent_identified": "agent-owned-identified-tab"}.get(kind)
        if named:
            return named if named in scopes else ""
        for scope in ("delegated-current-surface", "delegated-visible-tab"):
            if scope in scopes:
                return scope
        return ""

    def _observation_needs_approval(self, control, lease, spec):
        """observations 的 sensitive 标记是否真的要逐次问人,按资源类型分级。

        审批问的是「能不能让 Agent 看用户的东西」。agent 自己 open 的隔离标签用独立存储,
        没有用户的 cookie 与内网 SSO,页面也是 agent 自己打开的,对它截图与读它的文本
        敏感度相同,不再逐次确认。带用户身份的标签(agent-owned-identified-tab)与用户
        交出来的界面(delegated-*)照旧逐次确认。

        只放开审批这一面。脱敏是另一件事:由 observations 的 redaction 声明、宿主在取快照
        时执行(密码/令牌类输入遮挡、Desk 自身受保护界面隐藏),与本判定无关,任何资源
        类型下都照常生效。
        """
        if not spec.get("sensitive"):
            return False
        return self._lease_scope(control, lease) not in self._APPROVAL_EXEMPT_SCOPES

    # 无用户身份、由 agent 自己打开的资源:敏感观察免逐次审批。
    _APPROVAL_EXEMPT_SCOPES = frozenset({"agent-owned-isolated-tab"})

    def act(self, lease_id, session_id, name, value, expected_version="", idempotency_key="",
            anchor=None, intent=""):
        with self._lock, self.db:
            lease = self._active_lease(lease_id, session_id)
            control = self._control_for_existing_adapter(lease["app_id"])
            spec = action_spec(control, name)
            if not spec:
                raise AppControlError("动作未声明:%s" % name, 404)
            errors = validate_value(spec.get("input_schema") or {}, value, "input")
            if errors:
                raise AppControlError("; ".join(errors), 422)
            def validate_target(target, label=""):
                target = target if isinstance(target, dict) else {}
                locator = target.get("locator")
                if locator is not None and not any(locator.get(key) for key in (
                        "selector", "role", "name", "text", "test_id")):
                    raise AppControlError("%slocator 至少提供 selector、role、name、text 或 test_id"
                                          % label, 422)
                if not (target.get("node_id") or target.get("selector") or locator):
                    raise AppControlError("%s必须提供 node_id、selector 或 locator" % label, 422)

            if value.get("locator") is not None:
                validate_target(value)
            if name in {"fill", "select", "click", "hover", "dblclick", "check", "uncheck",
                        "focus", "blur"} and not (
                    value.get("node_id") or value.get("selector") or value.get("locator")):
                raise AppControlError("该动作必须提供 node_id、selector 或 locator", 422)
            if name == "drag":
                validate_target(value.get("source"), "source ")
                validate_target(value.get("target"), "target ")
            if name == "select":
                has_value = isinstance(value.get("value"), str)
                has_values = isinstance(value.get("values"), list) and bool(value.get("values"))
                if has_value == has_values:
                    raise AppControlError("select 必须且只能提供 value 或非空 values", 422)
            if name == "wait" and any(value.get(key) is not None for key in
                                      ("state", "value", "values")) and not (
                    value.get("selector") or value.get("locator")):
                raise AppControlError("wait 使用状态或值条件时必须提供 selector 或 locator", 422)
            condition = value.get('wait_for') if name != 'wait' else value
            if condition is not None:
                if name != 'wait':
                    wait_spec = action_spec(control, 'wait')
                    errors = validate_value(wait_spec['input_schema'], condition, 'input.wait_for')
                    if errors:
                        raise AppControlError('; '.join(errors), 422)
                if name != 'wait' and not any(condition.get(k) for k in
                        ('selector', 'locator', 'text', 'url', 'network_idle', 'value', 'values')):
                    raise AppControlError("wait_for 必须提供可观察条件", 422)
                if condition.get('locator') is not None:
                    validate_target(condition, 'wait_for ')
                if any(condition.get(key) is not None for key in ('state', 'value', 'values')) and not (
                        condition.get('selector') or condition.get('locator')):
                    raise AppControlError("等待状态或值必须提供 selector 或 locator", 422)
            # 坐标没有 DOM 节点身份可复核，只能绑定最近一次结构化观察。没有版本时，
            # 页面在截图与点击之间发生滚动/重排也看不出来，宁可拒绝也不能点错对象。
            if name == "coordinate_click" and not expected_version:
                raise AppControlError("坐标点击必须提供最近一次 inspect 的 expected_version", 422)
        return self._enqueue(
            lease_id, session_id, "act", name, value, spec["effect"], spec["timeout_ms"],
            spec["idempotency"], expected_version, idempotency_key,
            approval=spec.get("approval", "risk_based"), anchor=anchor, intent=intent,
        )

    def _begin_wait(self, row, condition, action_output=None):
        now = self.clock()
        delay = int(condition.get('milliseconds', 0))
        timeout = max(int(condition.get('timeout_ms', 10000)), delay + 1500 if delay else 0)
        deadline = now + timeout / 1000.0
        condition = dict(condition)
        if condition.get('milliseconds'):
            condition['_not_before'] = now + condition.pop('milliseconds') / 1000.0
        output = {'phase': 'waiting', 'deadline': deadline}
        if action_output is not None:
            output.update(action_executed=True, action_output=action_output, requires_reinspect=True)
        self.db.execute(
            "UPDATE operations SET status='queued',wait_json=?,wait_deadline=?,poll_after=?,"
            "started_at=NULL,dispatch_id='',output_json=?,updated_at=? WHERE id=?",
            (_json(condition), deadline, now, _json(output), now, row['id']))
        self.db.execute('UPDATE leases SET expires_at=MAX(expires_at,?) WHERE id=?',
                        (deadline + 30, row['lease_id']))
        self._event('changed', row['app_id'], row['resource_id'], row['lease_id'], row['id'],
                    {'phase': 'waiting', 'deadline': deadline, 'action_executed': action_output is not None})

    def resources(self, session_id, app_id=""):
        """列出当前会话实际持有的资源，不泄露别的会话或用户未委托的标签。"""
        if not session_id:
            raise AppControlError("session_id 必填", 400)
        with self._lock, self.db:
            self._expire()
            query = ("SELECT l.*,o.version,o.value_json FROM leases l LEFT JOIN observations o "
                     "ON o.app_id=l.app_id AND o.resource_id=l.resource_id "
                     "WHERE l.session_id=? AND l.status IN ('active','paused')")
            args = [session_id]
            if app_id:
                query += " AND l.app_id=?"; args.append(app_id)
            query += " ORDER BY l.updated_at DESC"
            rows = self.db.execute(query, args).fetchall()
        out = []
        for row in rows:
            item = self._lease_dict(row)
            value = _loads(item.pop("value_json", ""), {}) or {}
            item["observation_version"] = item.pop("version", "") or ""
            item["title"] = str(value.get("title") or "")
            item["url"] = str(value.get("url") or "")
            handoff = value.get("handoff") if isinstance(value.get("handoff"), dict) else {}
            item["handoff"] = {
                "current_step": str(handoff.get("current_step") or ""),
                "unsaved": bool(handoff.get("unsaved")),
                "submission_status": str(handoff.get("submission_status") or "unknown"),
                "ticket_id": str(handoff.get("ticket_id") or ""),
            }
            out.append(item)
        return out

    def successful_sequence(self, session_id, app_id="browser", limit=100):
        """把可移植的成功动作整理成 recipe；一次性 node_id 与输入值不会被带出。"""
        if not session_id:
            raise AppControlError("session_id 必填", 400)
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM operations WHERE session_id=? AND app_id=? AND kind='act' "
                "AND status='succeeded' ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (session_id, app_id, max(1, min(int(limit or 100), 500))),
            ).fetchall()
        steps = []
        for row in reversed(rows):
            value = _loads(row["input_json"], {}) or {}
            # DOM node id 只在一次 document epoch 内有效；表单值可能含业务或凭证数据。
            # 自动固化只收无需秘密且有稳定语义目标的步骤，其余由人显式参数化后再加入。
            if (_contains_key(value, "node_id") or _contains_key(value, "value")
                    or _contains_key(value, "values")):
                continue
            name = row["name"]
            if name in {"open", "activate", "handoff", "close"}:
                continue
            if name in {"click", "press", "scroll"} and not (
                    value.get("selector") or value.get("locator")):
                continue
            steps.append({"action": name, "input": value})
        return {"schema": "desk.app-control.sequence/v1", "app": app_id, "steps": steps,
                "source": {"session_id": session_id, "successful_operations": len(steps)}}

    def trace(self, operation_id, session_id):
        """返回一条动作的安全诊断，不暴露其他会话或页面正文。"""
        if not session_id:
            raise AppControlError("session_id 必填", 400)
        with self._lock, self.db:
            self._expire()
            row = self.db.execute(
                "SELECT o.* FROM operations o JOIN leases l ON l.id=o.lease_id "
                "WHERE o.id=? AND l.session_id=?", (operation_id, session_id)).fetchone()
            if not row:
                raise AppControlError("动作不存在或不属于当前会话", 404)
            events = self.db.execute(
                "SELECT seq,type,payload_json,created_at FROM events WHERE operation_id=? "
                "ORDER BY seq", (operation_id,)).fetchall()
            obs = self.db.execute(
                "SELECT version,value_json,updated_at FROM observations WHERE app_id=? AND resource_id=?",
                (row["app_id"], row["resource_id"])).fetchone()
        op = self._operation_dict(row)
        output = op.pop("output", None)
        op["input"] = _redact_input(op.get("input") or {})
        safe_output = {}
        if isinstance(output, dict):
            for key in ("diagnostic", "changes", "requires_reinspect", "modal_count", "input_path",
                        "action", "checked", "desired_checked", "url", "needs_user", "side", "matches",
                        "action_executed", "condition_met", "phase", "deadline",
                        "approval_revalidated", "next_step", "delivery", "effect_verified", "outcome"):
                if key in output:
                    safe_output[key] = output[key]
        page = _loads(obs["value_json"], {}) if obs else {}
        page = page if isinstance(page, dict) else {}
        timeline = [{"seq": e["seq"], "type": e["type"],
                     "payload": _loads(e["payload_json"], {}) or {}, "at": e["created_at"]}
                    for e in events]
        return {"operation": op, "output": safe_output,
                "page": {"title": str(page.get("title") or ""), "url": str(page.get("url") or ""),
                         "observation_version": obs["version"] if obs else "",
                         "observed_at": obs["updated_at"] if obs else None},
                "timing": {"queued_ms": round(max(0, ((row["started_at"] or row["updated_at"])
                                                         - row["created_at"]) * 1000)),
                           "run_ms": round(max(0, ((row["finished_at"] or row["updated_at"])
                                                    - (row["started_at"] or row["updated_at"])) * 1000))},
                "timeline": timeline}

    def _enqueue(self, lease_id, session_id, kind, name, value, effect, timeout_ms,
                 idempotency, expected_version, idempotency_key, approval="risk_based", anchor=None,
                 intent=""):
        import lifecycle
        if not lifecycle.begin(critical="app_control_enqueue"):
            raise AppControlError("Desk 正在重启，操作未提交", 409)
        try:
            return self._enqueue_admitted(lease_id, session_id, kind, name, value, effect,
                timeout_ms, idempotency, expected_version, idempotency_key,
                approval=approval, anchor=anchor, intent=intent)
        finally:
            lifecycle.finish(critical="app_control_enqueue")

    def _enqueue_admitted(self, lease_id, session_id, kind, name, value, effect, timeout_ms,
                 idempotency, expected_version, idempotency_key, approval="risk_based", anchor=None,
                 intent=""):
        now = self.clock()
        anchor = anchor if isinstance(anchor, dict) else {}
        intent = _clean_intent(intent)
        with self._lock, self.db:
            lease = self._active_lease(lease_id, session_id)
            if idempotency == "required" and not idempotency_key:
                raise AppControlError("该动作必须提供 idempotency_key", 422)
            if idempotency_key:
                old = self.db.execute(
                    "SELECT * FROM operations WHERE lease_id=? AND idempotency_key=? "
                    "ORDER BY created_at DESC LIMIT 1", (lease_id, idempotency_key)
                ).fetchone()
                if old:
                    return self._operation_dict(old)
            if self._is_native(lease["app_id"]):
                if kind != "inspect" or effect != "read_only":
                    raise AppControlError("原生 L1 只允许只读观察", 409)
                self._active_native_target(lease["id"], lease["app_id"])
            if lease['needs_inspect'] and effect != 'read_only':
                raise AppControlError("human_intervention_reinspect_required: 请先重新 inspect document", 409)
            # 有动作就续期。长任务原来会在跑到一半时因为 TTL 到点而丢租约,而 CLI 没有
            # 心跳命令;把「还在干活」本身当心跳,比另开一条心跳路径少一处要维护的东西。
            self.db.execute("UPDATE leases SET updated_at=?,expires_at=? WHERE id=?",
                            (now, max(lease['expires_at'], now + 900), lease["id"]))
            current = self._observation_version(lease["app_id"], lease["resource_id"])
            if expected_version and current and expected_version != current:
                raise AppControlError("观察版本已过期，请重新 inspect", 409)
            operation_id = "op_" + uuid.uuid4().hex
            # 关闭 Agent 自己创建的无身份隔离标签只回收本次会话资源，不触及用户数据。
            # 普通用户标签和带身份标签仍按 destructive 动作要求确认。
            own_isolated_close = (name == "close" and lease["kind"] == "agent_isolated")
            needs_approval = (approval == "always" or effect in WRITE_EFFECTS) and not own_isolated_close
            status = "waiting_user" if needs_approval else "queued"
            approval_state = "pending" if needs_approval else "not_required"
            # 见下:落库之后再推飞书。这里只记一笔,推送不能在锁里做——lark-cli 是子进程,
            # 慢的时候几秒,占着锁会把整个 runtime 卡住。
            self.db.execute(
                "INSERT INTO operations(id,lease_id,app_id,resource_id,session_id,turn_id,turn_seq,"
                "after_part_id,after_part_index,kind,name,effect,intent,"
                "input_json,expected_version,idempotency_key,timeout_ms,status,approval_state," 
                "before_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (operation_id, lease_id, lease["app_id"], lease["resource_id"], session_id,
                 str(anchor.get("turn_id") or ""), int(anchor.get("turn_seq") or 0),
                 str(anchor.get("after_part_id") or ""), int(anchor.get("after_part_index") or 0),
                 kind, name, effect, intent, _json(value), expected_version or "", idempotency_key or "",
                 int(timeout_ms), status, approval_state, current, now, now),
            )
            # 只有宿主 document 快照可提供续办凭据，Agent 入参不能声明“仍是原目标”。
            # 首版仅覆盖 click；坐标、拖拽、键盘、截断快照仍严格要求重观察/重批。
            if (needs_approval and lease['app_id'] == 'browser' and kind == 'act'
                    and name == 'click' and expected_version):
                observed = self.db.execute(
                    'SELECT value_json FROM observations WHERE app_id=? AND resource_id=?',
                    (lease['app_id'], lease['resource_id'])).fetchone()
                snapshot = _loads(observed['value_json'], {}) if observed else {}
                guard = snapshot.get('approval_guard') if isinstance(snapshot, dict) else None
                if (isinstance(guard, dict) and guard.get('token')
                        and guard.get('version') == expected_version
                        and snapshot.get('observation_version') == expected_version):
                    self.db.execute('UPDATE operations SET approval_guard_json=? WHERE id=?',
                                    (_json(guard), operation_id))
            self._event("waiting_user" if needs_approval else "changed", lease["app_id"],
                        lease["resource_id"], lease_id, operation_id,
                        {"status": status, "name": name, "effect": effect})
            if lease['app_id'] == 'browser' and kind == 'act' and name == 'wait':
                self._begin_wait(self.db.execute('SELECT * FROM operations WHERE id=?',
                                                (operation_id,)).fetchone(), value)
            out = self._operation_dict(
                self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())
        # 锁外再推:lark-cli 是子进程,慢的时候几秒,占着锁会把整个 runtime 卡住。
        if needs_approval:
            self._notify_later(out)
        return out

    # 动作的人话说明。原来直接拼 kind · name,推出去是「inspect · screenshot」——
    # 那是内部叫法,人看不出自己要同意什么(2026-09-03 用户实见)。
    _ASK_LABEL = {
        "open": "打开网页", "navigate": "跳转网页", "click": "点击页面元素",
        "coordinate_click": "按坐标点击页面", "press": "在页面上按键", "type": "在页面上输入文字",
        "fill": "填写表单", "select": "选择下拉项", "hover": "悬停在页面元素上",
        "dblclick": "双击页面元素", "check": "勾选页面选项", "uncheck": "取消勾选页面选项",
        "drag": "拖动页面元素", "focus": "聚焦页面元素", "blur": "移开页面焦点",
        "submit": "提交表单", "screenshot": "对当前页面截图",
        "document": "读取页面内容", "scroll": "滚动页面", "wait": "等待页面就绪",
        "back": "后退到上一页", "forward": "前进到下一页", "reload": "重新加载页面",
        "activate": "激活执行目标（不切换前台）", "handoff": "显示并接管原标签",
        "close": "关闭这个标签",
    }

    def _ask_label(self, op):
        intent = _clean_intent(op.get("intent"))
        if intent:
            return intent
        name = str(op.get("name") or "")
        base = self._ASK_LABEL.get(name) or f'{op.get("kind")} · {name}'
        url = (op.get("input") or {}).get("url")
        return f"{base}：{url}" if url else base

    # 挂起到推送之间等这么久。人在屏幕前时审批往往几秒就点了,这段静默期把「本来就
    # 看得见的那些」全部滤掉;真不在场的才发得出去。
    NOTIFY_DELAY_S = 25

    def _notify_later(self, op):
        """延后再推,别把在场的人也一起吵。

        原来是一挂起就发。而界面那张卡只有「人不在」时才需要飞书补一条——人在场时它
        就在眼前,发过去只是噪音。2026-09-03 实见:codex 连续试开页,九次待确认全推了
        飞书,而人当时正盯着屏幕。

        判据不是猜人在不在,是看结果:等 NOTIFY_DELAY_S 之后回查这条动作还在不在等人。
        已经批了、拒了、超时了,就什么都不发。顺带把这段时间里积压的合成一条——一次
        操作里连着几个待确认是常态,逐条推就是一串。
        """
        def fire():
            try:
                row = self.db.execute(
                    "SELECT status FROM operations WHERE id=?", (op.get("id"),)).fetchone()
                if not row or row["status"] != "waiting_user":
                    return          # 人已经处理过了,这条不必再提
                if not self._notify_pending(op):
                    return
                # 记下「已通知」:超时扫描据此换宽档。发失败不记——那种情况人根本没被
                # 叫到,再给它十五分钟只是让 Agent 空等更久。
                with self._lock, self.db:
                    self.db.execute("UPDATE operations SET notified_at=? WHERE id=?",
                                    (self.clock(), op.get("id")))
            except Exception:
                pass

        if not self.NOTIFY_DELAY_S:
            fire()                      # 零延时:同步发。测试据此免于等真实计时器
            return
        try:
            timer = threading.Timer(self.NOTIFY_DELAY_S, fire)
            timer.daemon = True
            timer.start()
        except Exception:
            pass

    def _pending_others(self, op):
        """同一段会话里此刻还有几条在等人。用来把一串提醒合成一句。"""
        try:
            rows = self.db.execute(
                "SELECT COUNT(*) c FROM operations WHERE status='waiting_user' AND session_id=?",
                (str(op.get("session_id") or ""),)).fetchone()
            return max(0, int(rows["c"]) - 1)
        except Exception:
            return 0

    def _notify_pending(self, op):
        """把待确认的审批推送到人不在场时也能看到的渠道。

        本版没有可从外部打开的 Desk 入口，推送出去的链接无处可点，因此不发送；
        审批仍在界面上的卡片里完成。返回 False 表示没有通知到人，超时仍按窄档计。
        """
        return False

    @staticmethod
    def _approval_ticket_digest(ticket):
        return hashlib.sha256(str(ticket or "").encode("utf-8")).hexdigest()

    def create_approval_ticket(self, operation_id):
        """签发只授权一条审批的一次性票据；数据库只保存摘要。"""
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock, self.db:
            self._expire()
            row = self.db.execute(
                "SELECT status FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not row:
                raise AppControlError("动作不存在", 404)
            if row["status"] != "waiting_user":
                raise AppControlError("动作当前不等待确认", 409)
            self.db.execute(
                "UPDATE operations SET approval_ticket_hash=?,"
                "approval_ticket_expires_at=?,approval_ticket_used_at=NULL WHERE id=?",
                (self._approval_ticket_digest(token), now + NOTIFIED_TTL_S, operation_id))
        return token

    def approval_for_ticket(self, operation_id, ticket):
        """票据只能读取它所指向的单条审批，不能枚举全局账本。"""
        now = self.clock()
        digest = self._approval_ticket_digest(ticket)
        with self._lock, self.db:
            self._expire()
            row = self.db.execute(
                "SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if (not row or not ticket or not row["approval_ticket_hash"]
                    or not hmac.compare_digest(row["approval_ticket_hash"], digest)
                    or float(row["approval_ticket_expires_at"] or 0) < now
                    or row["approval_ticket_used_at"] is not None
                    or row["status"] != "waiting_user"):
                raise AppControlError("审批票据无效、已过期或已使用", 403)
            return self._with_page([self._operation_dict(row)])[0]

    def approve_with_ticket(self, operation_id, ticket, approved, anchor=None):
        """校验并消费单条票据，再执行用户决定。"""
        now = self.clock()
        digest = self._approval_ticket_digest(ticket)
        with self._lock, self.db:
            self._expire()
            row = self.db.execute(
                "SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if (not row or not ticket or not row["approval_ticket_hash"]
                    or not hmac.compare_digest(row["approval_ticket_hash"], digest)
                    or float(row["approval_ticket_expires_at"] or 0) < now
                    or row["approval_ticket_used_at"] is not None):
                raise AppControlError("审批票据无效、已过期或已使用", 403)
            if row["status"] != "waiting_user":
                raise AppControlError("动作当前不等待确认", 409)
            self.db.execute(
                "UPDATE operations SET approval_ticket_used_at=?,approval_ticket_hash='' WHERE id=?",
                (now, operation_id))
            return self.approve(operation_id, approved, "remote_user", anchor)

    def approve(self, operation_id, approved, approver="user", anchor=None):
        now = self.clock()
        anchor = anchor if isinstance(anchor, dict) else {}
        with self._lock, self.db:
            self._expire()
            row = self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not row:
                raise AppControlError("动作不存在", 404)
            if row["status"] != "waiting_user":
                raise AppControlError("动作当前不等待确认", 409)
            # 升级前创建的待批没有回合坐标。决定发生时卡片已经落在一个明确回合里，
            # 顺手补齐；后续刷新便能恢复到同一处，而不是把旧记录永远丢掉。
            if not row["turn_id"] and not row["turn_seq"] and anchor:
                self.db.execute(
                    "UPDATE operations SET turn_id=?,turn_seq=?,after_part_id=?,"
                    "after_part_index=? WHERE id=?",
                    (str(anchor.get("turn_id") or ""), int(anchor.get("turn_seq") or 0),
                     str(anchor.get("after_part_id") or ""),
                     int(anchor.get("after_part_index") or 0), operation_id))
            if approved:
                self.db.execute(
                    "UPDATE operations SET status='queued',approval_state=?,updated_at=?,"
                    "approval_ticket_hash='',approval_ticket_used_at=COALESCE(approval_ticket_used_at,?) "
                    "WHERE id=?",
                    ("approved:" + approver, now, now, operation_id),
                )
                typ = "changed"
                # 批准一次带身份的开页 = 授权这个站点。不记的话,同一站点的下一个标签
                # 还要再问一次,那就退回「逐标签点头」,方案二等于没做。
                if row["name"] == "open":
                    value = _loads(row["input_json"], {}) or {}
                    if not value.get("isolated") and value.get("origin"):
                        self.db.execute(
                            "DELETE FROM site_grants WHERE app_id=? AND origin=? AND scope_key=?",
                            (row["app_id"], value["origin"], ""))
                        self.db.execute(
                            "INSERT INTO site_grants(id,app_id,origin,scope_key,created_at,expires_at) "
                            "VALUES(?,?,?,?,?,?)",
                            ("grant_" + uuid.uuid4().hex, row["app_id"], value["origin"], "",
                             now, now + 86400))
            else:
                self.db.execute(
                    "UPDATE operations SET status='rejected',approval_state=?,error='user_rejected'," 
                    "updated_at=?,finished_at=?,approval_ticket_hash='',"
                    "approval_ticket_used_at=COALESCE(approval_ticket_used_at,?) WHERE id=?",
                    ("rejected:" + approver, now, now, now, operation_id),
                )
                typ = "failed"
            self._event(typ, row["app_id"], row["resource_id"], row["lease_id"], operation_id,
                        {"status": "queued" if approved else "rejected"})
            return self.get_operation(operation_id)

    def next_operation(self, app_id, resource_id=""):
        self._control_for_existing_adapter(app_id)
        import lifecycle
        if not lifecycle.begin(critical="app_control_dispatch"):
            return None
        try:
            return self._next_operation_admitted(app_id, resource_id)
        finally:
            lifecycle.finish(critical="app_control_dispatch")

    def _next_operation_admitted(self, app_id, resource_id=""):
        now = self.clock()
        with self._lock, self.db:
            self._expire()
            query = ("SELECT o.* FROM operations o JOIN leases l ON l.id=o.lease_id "
                     "WHERE o.status='queued' AND l.status='active' AND o.app_id=? AND o.poll_after<=? "
                     "AND NOT EXISTS (SELECT 1 FROM operations busy WHERE busy.app_id=o.app_id "
                     "AND busy.resource_id=o.resource_id AND busy.id<>o.id AND "
                     "(busy.status='running' OR (o.effect<>'read_only' AND busy.wait_json<>'' "
                     "AND busy.status='queued')))")
            args = [app_id, now]
            if resource_id:
                query += " AND o.resource_id=?"; args.append(resource_id)
            query += " ORDER BY o.created_at LIMIT 1"
            row = self.db.execute(query, args).fetchone()
            if not row:
                return None
            current = self._observation_version(row['app_id'], row['resource_id'])
            guarded = bool(row['approval_guard_json'] and row['approval_state'].startswith('approved:'))
            if not guarded and not row['wait_json'] and row['expected_version'] and current and row['expected_version'] != current:
                self.db.execute("UPDATE operations SET status='failed',error='stale_observation_version',"
                                "updated_at=?,finished_at=? WHERE id=?", (now, now, row['id']))
                self._event('failed', row['app_id'], row['resource_id'], row['lease_id'], row['id'],
                            {'reason': 'stale_observation_version'})
                return None
            ticket = uuid.uuid4().hex if row['wait_json'] else ''
            self.db.execute(
                "UPDATE operations SET status='running',started_at=?,updated_at=?,dispatch_id=? WHERE id=? AND status='queued'",
                (now, now, ticket, row["id"]),
            )
            out = self.get_operation(row["id"])
            if guarded and not row['wait_json']:
                out['input'] = {**out['input'], '_approval_guard': _loads(row['approval_guard_json'], {})}
            if row['wait_json']:
                condition = _loads(row['wait_json'], {})
                not_before = condition.pop('_not_before', 0)
                condition['timeout_ms'] = min(1000, max(100, int((row['wait_deadline']-now)*1000)))
                condition['milliseconds'] = min(1000, max(0, int((not_before-now)*1000)))
                out.update(kind='act', name='wait', effect='read_only', input=condition)
            return out

    def pending_approval(self, app_id, resource_id=""):
        self._control_for_existing_adapter(app_id)
        with self._lock, self.db:
            self._expire()
            query = ("SELECT o.* FROM operations o JOIN leases l ON l.id=o.lease_id "
                     "WHERE o.status='waiting_user' AND l.status='active' AND o.app_id=?")
            args = [app_id]
            if resource_id:
                query += " AND o.resource_id=?"; args.append(resource_id)
            query += " ORDER BY o.created_at LIMIT 1"
            return self._operation_dict(self.db.execute(query, args).fetchone())

    def lifecycle_critical_count(self):
        """Effectful operations already handed to the native page cannot be replayed."""
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT COUNT(*) c FROM operations WHERE status='running' AND effect<>'read_only'").fetchone()
            return int(row["c"] if row else 0)

    def pending_all(self, limit=20):
        """所有等待确认的动作,不限 app。给界面用——原生壳那条只问自己那一个 app、
        且只取一条,因为它就在那台机器上,弹一个框足够。界面这条要面对的是另一种处境:
        人可能在手机上,而弹框在 Mac 上没人点(2026-09-02 用户指出),所以要能一次看全。"""
        with self._lock, self.db:
            self._expire()
            rows = self.db.execute(
                "SELECT o.* FROM operations o JOIN leases l ON l.id=o.lease_id "
                "WHERE o.status='waiting_user' AND l.status='active' "
                "ORDER BY o.created_at LIMIT ?", (int(limit),)).fetchall()
            return self._with_page([self._operation_dict(r) for r in rows])

    def _with_page(self, ops):
        """给每条动作附上它所在页面的标题与地址(最近一次 document 观察)。

        待批条目要说清「在哪一页上做什么」,而 click/fill 的入参里只有 node_id;
        没有这两项,人看到的是「点击页面元素」却不知道点的是哪个站。"""
        cache = {}
        for op in ops:
            key = (op.get("app_id"), op.get("resource_id"))
            if key not in cache:
                row = self.db.execute(
                    "SELECT value_json FROM observations WHERE app_id=? AND resource_id=?", key
                ).fetchone()
                value = _loads(row["value_json"], {}) if row else {}
                value = value if isinstance(value, dict) else {}
                cache[key] = (str(value.get("title") or ""), str(value.get("url") or ""))
            op["page_title"], op["page_url"] = cache[key]
        return ops

    def approval_history(self, limit=200):
        """近期审批账本。决定后仍返回，供会话正文刷新或跨端重建时恢复回执。"""
        with self._lock, self.db:
            self._expire()
            rows = self.db.execute(
                "SELECT * FROM operations WHERE approval_state NOT IN ('', 'not_required') "
                "ORDER BY created_at DESC LIMIT ?", (max(1, min(int(limit or 200), 500)),)
            ).fetchall()
            return self._with_page([self._operation_dict(r) for r in reversed(rows)])

    def clear_approval_history(self, before=0):
        """从信息卡清走已结束审批；会话正文、动作、事件与回执均完整保留。

        before 是用户点击清空时看到的最新更新时间。它避免请求在途期间刚结束的另一条
        审批被顺带清掉。待审批永远不受影响。
        """
        now = self.clock()
        cutoff = float(before or 0)
        with self._lock, self.db:
            # 清空是展示操作，不触发超时/撤销，也不改更新时间或审批决定。
            # 旧租约撤销曾留下 status=cancelled、approval_state=pending 的记录；
            # 与前端 _apprPending 一致，只有两字段同时为待批才应保留。
            sql = (
                "UPDATE operations SET approval_cleared_at=? "
                "WHERE approval_state NOT IN ('', 'not_required') "
                "AND NOT (approval_state='pending' AND status='waiting_user') "
                "AND approval_cleared_at IS NULL"
            )
            args = [now]
            if cutoff > 0:
                sql += " AND updated_at<=?"
                args.append(cutoff)
            cur = self.db.execute(sql, tuple(args))
            return {"cleared": int(cur.rowcount or 0)}

    def complete(self, operation_id, ok, output=None, error="", observation_version="", dispatch_id=""):
        now = self.clock()
        with self._lock, self.db:
            self._expire()
            row = self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            lease = self.db.execute(
                "SELECT status,needs_inspect FROM leases WHERE id=?",
                (row["lease_id"],)).fetchone() if row else None
            if (row and row["app_id"] == "desk" and row["effect"] != "read_only"
                    and row["status"] == "running"
                    and lease and (lease["status"] == "paused" or lease["needs_inspect"])):
                ok = False
                output = None
                error = "human_intervention_reinspect_required"
            if not row:
                raise AppControlError("动作不存在", 404)
            if self._is_native(row["app_id"]):
                # 原生回执必须经 generation/epoch 校验与白名单投影，不能走通用回执。
                raise AppControlError("原生应用动作只能经原生回执路径完成", 409)
            if row["status"] in TERMINAL:
                return self._operation_dict(row)
            if row['wait_json'] and (not dispatch_id or dispatch_id != row['dispatch_id']):
                return self._operation_dict(row)  # 旧探测/旧宿主回执不能结束新一轮等待。
            if row["status"] != "running":
                raise AppControlError("动作尚未进入执行态", 409)
            if (ok and row['approval_guard_json'] and not row['wait_json']
                    and not (isinstance(output, dict) and output.get('approval_revalidated') is True)):
                # 新 Runtime 配旧宿主不能默默略过校验；结果不确定时绝不自动重放。
                ok, error = False, 'approval_revalidation_unconfirmed'
            if not ok and str(error).startswith('approval_'):
                output = {**(output if isinstance(output, dict) else {}),
                          'requires_reinspect': True, 'next_step': 'inspect_then_request_new_approval'}
            if not ok and error == 'human_intervention_reinspect_required':
                output = {**(output if isinstance(output, dict) else {}),
                          'execution_uncertain': True, 'requires_reinspect': True,
                          'next_step': 'inspect_before_new_request'}
            if row['wait_json']:
                prior = _loads(row['output_json'], {})
                condition = _loads(row['wait_json'], {})
                pending = (not ok and error in ('wait_timeout', 'wait_context_changed')) or (ok and now < condition.get('_not_before', 0))
                if pending and now < row['wait_deadline']:
                    self.db.execute(
                        "UPDATE operations SET status='queued',started_at=NULL,dispatch_id='',poll_after=?,updated_at=? WHERE id=?",
                        (now + 1, now, operation_id))
                    return self.get_operation(operation_id)
                if now >= row['wait_deadline']:
                    ok, error = False, 'postcondition_timeout' if row['name'] != 'wait' else 'wait_timeout'
                output = {**prior, 'phase': 'completed' if ok else 'failed', 'condition_output': output,
                          'condition_met': bool(ok), 'requires_reinspect': True}
            elif ok and row['kind'] == 'act' and row['app_id'] == 'browser':
                condition = _loads(row['input_json'], {}).get('wait_for')
                if condition:
                    lease = self.db.execute('SELECT * FROM leases WHERE id=?', (row['lease_id'],)).fetchone()
                    if lease['needs_inspect']:
                        ok, error = False, 'human_intervention_reinspect_required'
                        output = {'action_executed': True, 'action_output': output, 'requires_reinspect': True}
                    else:
                        self._begin_wait(row, condition, action_output=output or {})
                        self.db.execute('UPDATE operations SET after_version=? WHERE id=?',
                                        (str(observation_version or ''), operation_id))
                        if observation_version:
                            self.db.execute(
                                "INSERT INTO observations(app_id,resource_id,version,value_json,updated_at) VALUES(?,?,?,?,?) "
                                "ON CONFLICT(app_id,resource_id) DO UPDATE SET version=excluded.version,updated_at=excluded.updated_at",
                                (row['app_id'], row['resource_id'], str(observation_version), '{}', now))
                        return self.get_operation(operation_id)
            if ok and row["kind"] == "inspect" and row["name"] == "screenshot":
                try:
                    output = self._store_screenshot(operation_id, output)
                except AppControlError as exc:
                    ok, output, error = False, None, str(exc)
            after = str(observation_version or "")
            structured_observation = (
                row["kind"] == "inspect" and (
                    (row["app_id"] == "browser" and row["name"] == "document") or
                    (row["app_id"] == "desk" and row["name"] == "surface")
                )
            )
            if ok and structured_observation:
                # 暂停前已经在途的快照不能解除交接校验。
                self.db.execute(
                    "UPDATE leases SET needs_inspect=0 WHERE id=? AND status='active' AND updated_at<=?",
                    (row['lease_id'], row['created_at']))
                if not after:
                    after = "%d" % int(now * 1000)
                self.db.execute(
                    "INSERT INTO observations(app_id,resource_id,version,value_json,updated_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(app_id,resource_id) DO UPDATE SET version=excluded.version," 
                    "value_json=excluded.value_json,updated_at=excluded.updated_at",
                    (row["app_id"], row["resource_id"], after, _json(output), now),
                )
            elif ok and after and row["kind"] == "act":
                # 动作改变页面后立即推进版本；value_json 仍是上次快照，仅用于诊断，
                # Agent 必须重新 inspect 才能取得与新版本匹配的节点集合。
                self.db.execute(
                    "INSERT INTO observations(app_id,resource_id,version,value_json,updated_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(app_id,resource_id) DO UPDATE SET version=excluded.version," 
                    "updated_at=excluded.updated_at",
                    (row["app_id"], row["resource_id"], after, "{}", now),
                )
            status = "succeeded" if ok else "failed"
            self.db.execute(
                "UPDATE operations SET status=?,output_json=?,error=?,after_version=?,updated_at=?," 
                "finished_at=? WHERE id=?",
                (status, _json(output) if output is not None else "", str(error or ""), after,
                 now, now, operation_id),
            )
            self._event("action_completed" if ok else "failed", row["app_id"], row["resource_id"],
                        row["lease_id"], operation_id, {"status": status, "error": error or ""})
            # 宿主说这张标签的视图不在了(Desk 重启、标签被关):租约已经没有东西可控,
            # 留着只会让调用方对着它一次次拿到同一个错误(历史上 38/385 次都是这个形状)。
            # 当场撤掉,下一次调用得到的是带原因的 409,而不是再排一次队。
            if not ok and str(error or "") == "browser_tab_unavailable":
                self._revoke_locked(row["lease_id"], "browser_tab_unavailable")
            return self.get_operation(operation_id)

    def _store_screenshot(self, operation_id, output):
        if not isinstance(output, dict) or output.get("mime_type") != "image/png":
            raise AppControlError("截图 adapter 返回格式非法", 422)
        try:
            raw = base64.b64decode(str(output.get("data") or ""), validate=True)
        except Exception:
            raise AppControlError("截图数据不是合法 base64", 422)
        if not raw or len(raw) > 20 * 1024 * 1024:
            raise AppControlError("截图为空或超过 20MB", 413)
        os.makedirs(self.evidence_dir, mode=0o700, exist_ok=True)
        path = os.path.join(self.evidence_dir, operation_id + ".png")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return {"mime_type": "image/png", "path": path, "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "width": output.get("width"), "height": output.get("height"),
                "full_page": bool(output.get("full_page")),
                "truncated": bool(output.get("truncated"))}

    def get_operation(self, operation_id, session_id=""):
        with self._lock, self.db:
            self._expire()
            query = "SELECT * FROM operations WHERE id=?"
            args = [operation_id]
            if session_id:
                query += " AND session_id=?"
                args.append(session_id)
            row = self.db.execute(query, tuple(args)).fetchone()
            if not row:
                raise AppControlError("动作不存在", 404)
            return self._operation_dict(row)

    def wait_operation(self, operation_id, timeout_seconds=30, session_id=""):
        deadline = time.monotonic() + max(0, min(float(timeout_seconds), 60.0))
        while True:
            operation = self.get_operation(operation_id, session_id)
            if operation["status"] in TERMINAL:
                return operation
            if time.monotonic() >= deadline:
                return operation
            time.sleep(0.05)

    def receipt(self, operation_id, session_id=""):
        operation = self.get_operation(operation_id, session_id)
        if operation["status"] not in TERMINAL:
            raise AppControlError("动作尚未形成最终回执", 409)
        return operation

    def events_after(self, session_id, seq=0, app_id="", limit=100):
        if not session_id:
            raise AppControlError("session_id 必填", 400)
        query = ("SELECT e.* FROM events e JOIN leases l ON l.id=e.lease_id "
                 "WHERE e.seq>? AND l.session_id=?")
        args = [int(seq or 0), session_id]
        if app_id:
            query += " AND app_id=?"; args.append(app_id)
        query += " ORDER BY seq LIMIT ?"; args.append(max(1, min(int(limit or 100), 500)))
        with self._lock:
            rows = self.db.execute(query, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item.pop("payload_json", ""), {})
            out.append(item)
        return out


_RUNTIME = None
_RUNTIME_LOCK = threading.Lock()


def runtime():
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = AppControlRuntime()
        return _RUNTIME
