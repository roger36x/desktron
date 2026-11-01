# 由 engine_adapters.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)
import copy
import json
import os
import re
import threading
import time
import uuid
from .bus import SESSION_WINDOW
from .cli import CliAdapter
from .codex_rollout import CodexRolloutMixin
from .codex_subagents import CodexSubagentsMixin
from .model_catalog import order_gpt_model_slots
from .recall import RecallMixin
from .codex_tool_result import result_state


# Codex 不会自动加载 Desk 仓内与用户全局 skill。系统提示不再枚举名称，
# 这里只补一条读取入口，具体选择仍按 orientation 的「技能发现」执行。
CODEX_DELTA = (
    "Desk 操作说明不会自动加载；需要时按“查找操作说明”读取相应的 `SKILL.md`。"
)


# ---------------------------------------------------------------------------
# 订阅引擎二:codex
#
# 各适配器的差别只在两件事:怎么起一回合(build_cmd)、引擎吐的那串 JSON 怎么翻成契约事件
# (handle_event)。其余(会话索引、消息落盘、live 快照、撤回、卡住探测)都在 CliAdapter 里,
# 不各写一份。
#
# 出字的粒度是这个引擎与 claude 的实际差距:claude 有字符级 delta,codex 只在
# 一个块完成时整块发一次。前端那条流式路径不用改——建块之后补一次整块的 delta,渲染路径一致,
# 只是「逐字长出来」变成「整段落下来」。这一条写进 caps.stream,界面据此决定要不要显示光标。


class CodexAdapter(RecallMixin, CodexRolloutMixin, CodexSubagentsMixin, CliAdapter):
    """codex exec --json:一行一个事件的 JSONL。

    会话是 codex 的 thread:首个事件 thread.started 报 thread_id,续接走 `codex exec resume <id>`。
    沙箱档位一律经 `-c sandbox_mode=…` 传,不用 `-s`——0.146 起 `exec resume` 不认 `-s`,
    两条路各用一套写法会在续接时炸(实测 `error: unexpected argument '-s'`)。
    """

    name = "codex"
    streams_text = True    # app-server 逐字发 item/agentMessage/delta(2026-08-10 实测)
    model_label = "codex · 订阅"
    binary = "codex"
    use_stdin_prompt = False        # exec 的提示词走位置参数;stdin 留给 DEVNULL,否则它会等输入
    fork_flag = None                # codex 无「从同一条线另开一支」的标志
    interrupt_kill = False          # 回合活在常驻 app-server 里,杀连接不是打断
    INTERRUPT_FALLBACK_SECONDS = 5.0  # 给 turn/start 回执和中断确认留出完整协议窗口。
    rewind_supported = False
    compact_supported = True
    UI_MODE_KEY = "collaboration_mode"   # 协作档,与 claude 的权限档不是一个键
    UI_MODE_DEFAULT = "default"
    MANAGED_HOME_ENV = "CODEX_HOME"
    TITLE_ENGINE = "codex"
    # 快速 Esc 撤回不仅要删 Desk 投影，也要从引擎原生上下文删除这一轮。
    # 子类按实际 app-server 协议覆写；空串表示不支持持久撤回。
    RECALL_METHOD = "revert"
    # 前端显示与后端白名单共用这一份。空值代表不覆盖,由模型默认决定。
    AUTO_COMPACTS = (("200000", "20 万"), ("400000", "40 万"), ("600000", "60 万"))

    def search_source_path(self, sid, record):
        esid = str((record or {}).get("engine_sid") or "")
        path = self._rollout_path(esid) if esid else ""
        return path if path and os.path.isfile(path) else super().search_source_path(sid, record)

    def search_documents(self, sid, record, path):
        if path and path != self._msg_path(sid):
            from session_search import codex_documents
            return codex_documents(self, path, sid, record)
        return super().search_documents(sid, record, path)

    def recap_generate(self, sid, run, snap):
        from .recap import codex_recap
        return codex_recap(self, sid, run, snap)

    # Sandbox 与 collaboration mode 是两条正交的轴。旧版把 sandbox 存在 mode 键里，
    # normalize_options 会在首次保存会话设置时把它迁到 sandbox。
    SANDBOX = ("read-only", "workspace-write", "danger-full-access")
    DEFAULT_SANDBOX = "workspace-write"

    # ---- 审批 ----
    # app-server 在越权动作前会发一条服务端请求等人拍板。此前 GUI 这侧没有回路,一律
    # 当场拒绝,并把 approvalPolicy 压成 never——于是 codex 家族在工作台里遇到沙箱外的
    # 动作只会失败,人连「要不要放行」都看不到,而终端那条路是能问的。现在接进 claude
    # 那套现成的权限回路:同一个 permission.asked 事件、同一个 /session/<id>/permission
    # 接口、同一张卡片,前端不必分引擎。
    asks_permission = True

    PERMISSION_PROFILE_METHOD = "item/permissions/requestApproval"

    APPROVAL_METHODS = (PERMISSION_PROFILE_METHOD,
                        "item/commandExecution/requestApproval",
                        "item/fileChange/requestApproval",
                        "applyPatchApproval", "execCommandApproval",
                        "mcpServer/elicitation/request")

    # 请求里的字段随类别不同:命令给 command,改文件给 path/changes。取出来喂给前端那张卡,
    # 让它照普通工具卡的写法显示做什么。tool 借用 claude 那套工具名,前端已有译名可用。
    APPROVAL_TOOL = {PERMISSION_PROFILE_METHOD: "权限",
                     "item/commandExecution/requestApproval": "Bash",
                     "execCommandApproval": "Bash",
                     "item/fileChange/requestApproval": "Edit",
                     "applyPatchApproval": "Edit",
                     "mcpServer/elicitation/request": "MCP"}

    @staticmethod
    def _requested_permissions(params):
        """只接受协议里已知层级类型正确的权限配置；异常输入一律按拒绝处理。"""
        if not isinstance(params, dict) or "permissions" not in params:
            return {}
        permissions = params.get("permissions")
        if not isinstance(permissions, dict):
            return None
        network = permissions.get("network")
        if "network" in permissions and not isinstance(network, dict):
            return None
        fs_key = "fileSystem" if "fileSystem" in permissions else "file_system"
        fs = permissions.get(fs_key)
        if fs_key in permissions and not isinstance(fs, dict):
            return None
        if isinstance(fs, dict):
            for key in ("read", "write", "entries"):
                if key in fs and not isinstance(fs[key], (list, tuple)):
                    return None
        return permissions

    @staticmethod
    def _permission_scope(params):
        """把 request_permissions 的结构化范围压成审批卡片的一行说明。"""
        params = params if isinstance(params, dict) else {}
        permissions = CodexAdapter._requested_permissions(params)
        invalid = permissions is None
        permissions = permissions or {}
        bits = []
        network = permissions.get("network")
        if isinstance(network, dict) and network.get("enabled"):
            bits.append("访问网络")
        fs = permissions.get("fileSystem") or permissions.get("file_system")
        fs = fs if isinstance(fs, dict) else {}

        def _path(value):
            if isinstance(value, str):
                return value
            if not isinstance(value, dict):
                return str(value or "")
            if value.get("path"):
                return str(value["path"])
            if value.get("pattern"):
                return str(value["pattern"])
            special = value.get("value")
            if isinstance(special, dict):
                kind = special.get("kind") or special.get("path") or ""
                subpath = special.get("subpath")
                return "/".join(x for x in (str(kind), str(subpath or "")) if x)
            return str(special or value.get("kind") or "")

        for access, label in (("read", "读取"), ("write", "写入")):
            values = fs.get(access) or []
            if not isinstance(values, (list, tuple)):
                values = [values]
            paths = [_path(x) for x in values]
            paths = [x for x in paths if x]
            if paths:
                bits.append(label + "、".join(paths))
        entries = fs.get("entries") or []
        if not isinstance(entries, (list, tuple)):
            entries = [entries]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            access = str(entry.get("access") or "").lower()
            path = _path(entry.get("path"))
            if path:
                bits.append({"read": "读取", "write": "写入",
                             "deny": "禁止读取"}.get(access, "访问") + path)
        cwd = str(params.get("cwd") or "").strip()
        if cwd:
            bits.append("工作目录 " + cwd)
        if invalid:
            bits.insert(0, "无法识别权限格式")
        return "；".join(bits) or "未提供具体权限范围"

    def _approval_properties(self, sid, rid, method, params):
        """把不同版本的审批请求归一成前端已有的 permission.asked 卡片。"""
        p = params if isinstance(params, dict) else {}
        inp = {k: copy.deepcopy(p[k])
               for k in ("command", "cwd", "path", "changes", "reason",
                         "message", "serverName", "permissions")
               if p.get(k)}
        reason = str(p.get("reason") or p.get("message") or "")
        if method == self.PERMISSION_PROFILE_METHOD:
            # 权限申请发生在具体工具 item/started 之前，补一条可读范围避免卡片空白。
            inp["command"] = self._permission_scope(p)
            requested = self._requested_permissions(p)
            inp["permissions"] = copy.deepcopy(requested) if requested is not None else {}
            reason = reason or "Codex 请求额外权限"
        return {"sessionID": sid, "id": rid,
                "tool": self.APPROVAL_TOOL.get(method, "tool"),
                "reason": reason, "input": inp}

    def _approval_ask(self, sid, msg, conn):
        """服务端要人拍板:转成 permission.asked 发去界面,这条请求先挂着不应答。

        返回 True 表示接管。挂起期间连接层不再计静默与总时限(见 _Connection.take_approval),
        兜底定时器到点自动拒绝,fail-closed 与 claude 那侧同一口径。"""
        method = str(msg.get("method") or "")
        if method not in self.APPROVAL_METHODS:
            return False
        raw_params = msg.get("params")
        p = raw_params if isinstance(raw_params, dict) else {}
        rid = "cx_%s_%d" % (msg.get("id"), int(time.time() * 1000))
        with self._plock:
            self._approvals[rid] = (conn, msg.get("id"), method, p, sid)
        props = self._approval_properties(sid, rid, method, p)
        self.bus.publish({"type": "permission.asked", "properties": props})
        # MCP elicitation 位于真正工具执行之前；node_repl 的 timeout_ms 管不到这里。
        # 最多给十秒处理可见卡片，避免无人响应时把整个会话挂五分钟。
        approval_timeout = 10 if method == "mcpServer/elicitation/request" else self.PERMISSION_TIMEOUT
        _t = threading.Timer(approval_timeout,
                             lambda: self.respond_permission(rid, False, resolution="timeout"))
        _t.daemon = True   # 进程要退时不等权限窗口到期
        _t.start()
        return True

    def respond_permission(self, request_id, allow, answers=None, response="", resolution="",
                           asks_provider=None):
        with self._plock:
            hit = self._approvals.pop(request_id, None)
        if not hit:
            return False       # 已经答过,或兜底定时器先到了
        conn, msg_id, method, params = hit[:4]
        sid = hit[4] if len(hit) > 4 else ""
        if method == "mcpServer/elicitation/request":
            result = {"action": "accept" if allow else "decline"}
        elif method == self.PERMISSION_PROFILE_METHOD:
            requested = self._requested_permissions(params)
            granted = copy.deepcopy(requested) if allow and requested is not None else {}
            # 新协议要求返回一份 GrantedPermissionProfile。只回显本次明确申请的范围，
            # 拒绝与异常输入都给空权限，不能把工作台审批变成全盘放行。
            result = {"permissions": granted, "scope": "turn"}
        elif method in ("applyPatchApproval", "execCommandApproval"):
            # 旧协议的 ReviewDecision 是个带内容的枚举,与 v2 那组字面量不是一回事。
            result = {"decision": ({"approved": {}} if allow else
                                   {"denied": {"rejection": "用户在工作台拒绝了该操作"}})}
        else:
            from codex_appserver import pick_decision
            result = {"decision": pick_decision(params, allow)}
        try:
            conn.answer_approval(msg_id, result)
        except Exception:
            self._approval_resolved(sid, request_id, "connection_error", asks_provider)
            return False
        self._approval_resolved(sid, request_id,
                                resolution or ("approved" if allow else "denied"),
                                asks_provider)
        return True

    def _approval_resolved(self, sid, request_id, reason, asks_provider=None):
        provider = asks_provider or getattr(self, '_asks_provider', None)
        try:
            asks = provider(sid) if provider else self._pending_ask(sid)
        except Exception:
            try: asks = self._pending_ask(sid)
            except Exception: asks = []
        try:
            self.bus.publish({"type": "permission.resolved", "properties": {
                "sessionID": sid, "id": request_id, "resolution": reason,
                "asks": asks}})
        except Exception:
            pass

    def _pending_ask(self, sid):
        """补回断线或刷新期间漏掉的 Codex 审批卡。"""
        with self._plock:
            pending = [(rid, value[2], copy.deepcopy(value[3]))
                       for rid, value in self._approvals.items()
                       if len(value) > 4 and value[4] == sid]
        return [self._approval_properties(sid, rid, method, params)
                for rid, method, params in pending]

    def _clear_session_approvals(self, sid):
        """回合结束或连接被杀后清掉属于它的悬挂请求。"""
        with self._plock:
            hits = [(rid, value) for rid, value in self._approvals.items()
                    if len(value) > 4 and value[4] == sid]
            for rid, _value in hits:
                self._approvals.pop(rid, None)
        for _rid, value in hits:
            conn, msg_id, method, _params = value[:4]
            try:
                if method == "mcpServer/elicitation/request":
                    conn.answer_approval(msg_id, {"action": "decline"})
                elif method == self.PERMISSION_PROFILE_METHOD:
                    conn.answer_approval(msg_id, {"permissions": {}, "scope": "turn"})
                else:
                    conn.answer_approval(msg_id, {"decision": "cancel"})
            except Exception:
                pass
            self._approval_resolved(sid, _rid, "cancelled")


    def resident_engine_sids(self):
        try:
            from codex_appserver import live_session_ids
            sids = live_session_ids()
        except Exception:
            return set()
        return {e for e in (self._esid_of(x) for x in sids) if e}

    def release_resident(self, esid):
        sid = self._sid_by_esid(esid)
        if not sid:
            return False
        try:
            from codex_appserver import drop_session, live_session_ids
            if sid not in live_session_ids():
                return False
            drop_session(sid)
        except Exception:
            return False
        return True

    def delete_session(self, sid):
        # app-server 与 Desk 会话同寿命；删除索引时一并释放常驻进程。
        try:
            from codex_appserver import drop_session
            drop_session(sid)
        finally:
            super().delete_session(sid)

    def config_label(self, sid=None):
        """首回合前也显示 app-server 公布的默认模型，而不是泛化的“默认”。
        有 sid 按这一段会话自报的来(见 cli_store.session_model),没有才用引擎级兜底。"""
        m = self.session_model(sid) if sid else self.last_model
        if m:
            return f"{self.name} · {m}"
        # 带会话 id 时，没有自报就必须明确返回“未知”。刚从菜单换模型会清掉旧自报；
        # 此处若又补 app-server 默认值，刷新后就会把人选的 Seed 盖回 GPT-5.4/5.5。
        if sid:
            return self.name
        data = self.options_catalog()
        models = data.get("models") or []
        row = next((x for x in models if x.get("default")), None) or (models[0] if models else None)
        return f'{self.name} · {(row or {}).get("label") or "默认"}'

    def ui_contract(self):
        out = super().ui_contract()
        if self.AUTO_COMPACTS:
            out["autocompacts"] = ([{"id": "", "label": "自动"}] +
                                     [{"id": key, "label": label}
                                      for key, label in self.AUTO_COMPACTS])
        return out

    def options_catalog(self):
        try:
            from codex_appserver import catalog
            raw = catalog(cwd=self.workspace, binary=self.APPSERVER_BIN)
            models = []
            for m in order_gpt_model_slots(raw.get("models") or []):
                efforts = [{"id": e.get("reasoningEffort"), "label": str(e.get("reasoningEffort") or "").title(),
                            "description": e.get("description") or ""}
                           for e in (m.get("supportedReasoningEfforts") or []) if e.get("reasoningEffort")]
                models.append({
                    "id": m.get("model") or m.get("id"),
                    "label": m.get("displayName") or m.get("model") or m.get("id"),
                    "description": m.get("description") or "",
                    "default": bool(m.get("isDefault")),
                    "defaultEffort": m.get("defaultReasoningEffort") or "",
                    "efforts": efforts,
                })
            modes = [{"id": m.get("mode"), "label": m.get("name") or str(m.get("mode") or "").title(),
                      "defaultEffort": m.get("reasoning_effort") or ""}
                     for m in (raw.get("modes") or []) if m.get("mode")]
            return {"models": models, "modes": modes, "ui": self.ui_contract(), "sandboxes": [
                {"id": "read-only", "label": "Read only"},
                {"id": "workspace-write", "label": "Workspace write"},
                {"id": "danger-full-access", "label": "Full access"},
            ]}
        except Exception as exc:
            # app-server 不可用时界面仍能修改权限，模型/Mode 明确退到当前配置，不伪造目录。
            return {"models": [], "modes": [
                {"id": "default", "label": "Default"}, {"id": "plan", "label": "Plan"}],
                "ui": self.ui_contract(),
                "sandboxes": [
                    {"id": "read-only", "label": "Read only"},
                    {"id": "workspace-write", "label": "Workspace write"},
                    {"id": "danger-full-access", "label": "Full access"}],
                "error": str(exc)[:240]}

    def normalize_options(self, opts):
        opts = dict(opts or {})
        legacy = opts.get("mode")
        if not opts.get("sandbox") and legacy in self.SANDBOX:
            opts["sandbox"] = legacy
        out = {}
        mode = str(opts.get("collaboration_mode") or "default")
        out["collaboration_mode"] = mode if mode in ("default", "plan") else "default"
        sandbox = str(opts.get("sandbox") or self.DEFAULT_SANDBOX)
        out["sandbox"] = sandbox if sandbox in self.SANDBOX else self.DEFAULT_SANDBOX
        for key in ("model", "effort"):
            if opts.get(key) is not None:
                out[key] = str(opts.get(key) or "")
        ac = str(opts.get("autocompact") or "").strip()
        if ac in {key for key, _label in self.AUTO_COMPACTS}:
            out["autocompact"] = ac
        if "desk_role" in opts:
            out["desk_role"] = self._desk_role_name(opts.get("desk_role"))
        roots = opts.get("runtime_roots")
        if isinstance(roots, list):
            out["runtime_roots"] = [str(x) for x in roots if str(x)]
        return out

    def slash_commands(self):
        """Codex 原生命令交给 app-server 执行；Desk 本地命令在前端同名优先。"""
        return [
            {"n": "approve", "d": "批准一次自动审查拒绝后的重试"},
            {"n": "cloud", "d": "在云端运行当前会话"},
            {"n": "cloud-environment", "d": "选择云端运行环境"},
            {"n": "compact", "d": "压缩当前上下文"},
            {"n": "fast", "d": "切换 Fast 服务档位"},
            {"n": "feedback", "d": "提交反馈并可附带日志"},
            {"n": "fork", "d": "复制当前会话或 worktree"},
            {"n": "goal", "d": "设置持续目标"},
            {"n": "ide-context", "d": "开关 IDE 上下文"},
            {"n": "init", "d": "生成 AGENTS.md 脚手架"},
            {"n": "local", "d": "切到本地项目运行"},
            {"n": "mcp", "d": "查看 MCP 状态"},
            {"n": "memories", "d": "配置记忆能力"},
            {"n": "model", "d": "选择模型"},
            {"n": "pet", "d": "开关桌面宠物"},
            {"n": "personality", "d": "选择回复风格"},
            {"n": "plan", "d": "切换计划模式"},
            {"n": "project", "d": "选择项目"},
            {"n": "reasoning", "d": "选择推理强度"},
            {"n": "review", "d": "进入代码审查模式"},
            {"n": "side", "d": "开启临时侧边会话"},
            {"n": "status", "d": "显示会话状态"},
            {"n": "task", "d": "开始无项目会话"},
            {"n": "worktree", "d": "在新 worktree 中运行"},
        ]

    def compact_session(self, sid):
        esid = (self.index.get(sid) or {}).get("engine_sid") or ""
        if not esid:
            return {"ok": False, "error": f"这段 {self.APPSERVER_LABEL} 会话还没有可压缩的历史"}
        opts = self.session_options(sid, {})
        env = os.environ.copy()
        env.update(self.spawn_env(opts))
        cmd_at = int(time.time() * 1000)
        self._append_compact_command(sid, cmd_at)
        started = int(time.time() * 1000)
        self._compact_state[sid] = {
            "running": True, "started": started, "trigger": "manual"}

        def relay(method, params):
            if method == "thread/compacted":
                from .session_naming import invalidate
                invalidate(self, sid)
                self.bus.publish({"type": "session.compact", "properties": {
                    "sessionID": sid, "running": False, "trigger": "manual"}})

        self.bus.publish({"type": "session.compact", "properties": {
            "sessionID": sid, "running": True, "started": started,
            "trigger": "manual"}})
        out = {"ok": False, "error": "压缩未完成"}
        try:
            try:
                from codex_appserver import compact_thread
                out = compact_thread(sid, esid, cwd=self.session_cwd(sid), env=env,
                                     on_notification=relay, binary=self.APPSERVER_BIN,
                                     label=self.APPSERVER_LABEL)
            except Exception as exc:
                out = {"ok": False, "error": str(exc)[:300]}
            if out.get("ok"):
                try:
                    self.import_session(esid, sid=sid, limit=50)
                    self._append_compact_command(sid, cmd_at)
                except Exception as exc:
                    out = {"ok": False, "error": str(exc)[:300]}
        finally:
            result = "success" if out.get("ok") else "failed"
            error = "" if out.get("ok") else str(out.get("error") or "引擎未说明原因")[:300]
            self._compact_state[sid] = {
                "running": False, "started": started, "result": result,
                "error": error, "trigger": "manual"}
            self.bus.publish({"type": "session.compact", "properties": {
                "sessionID": sid, "running": False, "started": started,
                "result": result, "error": error, "trigger": "manual"}})
        return out

    def _append_compact_command(self, sid, at):
        """GUI 的 /compact 是 app-server RPC，不会自然出现在 rollout 里。

        Claude Code 的 /compact 会在记录里留下用户命令；Codex 这里补同一条投影，
        让刷新前后都能看到这是一轮用户发起的命令。
        """
        msgs = self.messages(sid)
        for m in reversed(msgs[-5:]):
            info = m.get("info") or {}
            if info.get("role") != "user" or info.get("at") != at:
                continue
            txt = "".join(q.get("text") or "" for q in m.get("parts") or []
                          if q.get("type") == "text").strip()
            if txt == "/compact":
                return
        mid = "msg_u_compact_" + uuid.uuid4().hex
        self._append_msg(sid, {"info": {"id": mid, "role": "user", "sessionID": sid, "at": at},
                               "parts": [{"id": mid + "_t", "messageID": mid, "sessionID": sid,
                                          "type": "text", "text": "/compact"}]})
        self._trim_store(sid)

    def spawn_env(self, opts):
        # 用户的 login shell 可能在每次启动时执行 `go env -w`。Codex GUI 默认沙箱不能写
        # ~/Library/Application Support/go/env，于是每条工具记录前都会重复一组权限警告。
        # 把 Go 的配置文件放进 Desk 工作区，既保留用户 profile 行为，也落在可写根内。
        path = os.path.join(self.workspace, ".workbench", "codex-go-env")
        env = {}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            env["GOENV"] = os.environ.get("GOENV") or path
        except OSError:
            pass
        # 准入闸:重定向到 desk 管的隔离 CODEX_HOME(软链共享登录/会话,挂 PreToolUse hook)。
        # 这一步原来只在 TUI 那条路上做(engines/codex.py 的 provision_gate),TUI 2026-08-20
        # 退场后 GUI 是唯一入口,不搬过来的话闸的配置根本不在 codex 读得到的地方。
        # 待验证:TUI 那条路还带 `--dangerously-bypass-hook-trust` 让 desk 自审的 hook 免信任
        # 直接生效,而 `codex app-server` 没有这个标志(2026-08-20 实测 --help 里没有),
        # 未持久化 trust 时 hook 可能不 fire。所以这一档按 best-effort 记:配置就位、
        # 能 fire 就拦,fire 不了也不阻断启动(与 TUI 退场前的 GUI 一样没有硬闸,不是回退)。
        # provision 失败就不设:回退全局 ~/.codex,引擎照常起得来。
        home = self._managed_home()
        if home:
            env[self.MANAGED_HOME_ENV] = home
        return env

    # 首回合前置的〈系统约定〉:codex 没有 --append-system-prompt,角色纪律只能进正文。
    # 分隔符与 PTY 那条路一致(engines.base.SEED_SEP),两个入口注进去的形状相同。
    SEED_HEAD = "〈系统约定,本次会话持续有效〉\n"
    SEED_SEP = "\n\n———\n\n"
    # 通道同时决定回合的生死:写进 final_answer 的那一句就是收尾,commentary 不收尾。
    # 这条约定原先只有一半——它防的是「该让用户处理的话只写在 commentary,人看不见」,
    # 于是一路把内容往 final_answer 推,却没说过什么不该写在那里。反向的故障因此照发:
    # 模型把「我会先读取这两份文档」这样的预告当成终答,回合三秒收尾、一个工具都没调,
    # 界面上就是莫名停下来,人再问一句它才继续(2026-09-09 用户实见)。
    CHANNEL_RULE = (
        "`final_answer` 会结束本回合。工作未完成时继续执行，不要用计划或进度收尾。"
        "得出结论，或需要用户执行、确认、补充时，再写 `final_answer`。"
        "需要用户处理的内容不得只写在 `commentary`。"
    )
    # 工具卡的摘要要说「这一步想干什么」,而 codex 家族的 commandExecution 只带命令本体,
    # 没有 claude 的 Bash 那样的 description 字段。Code Mode 外层是一段调用 functions.exec
    # 的 JS,所以意图写在外层 `// @intent:`;直接 shell 则仍用首行 `# ...`。前端拿它当摘要
    # (见 app-chat.js 的 hsToolObj,该分支早就在,原为 pi 而设)。
    # 没有这一行时前端会去猜:常见命令有意图词表,而检索式与长管道猜不出来,只能把原文摆上去
    # ——一行 `rg -n "goal|create_goal|update_goal|…"` 截断在屏幕右边,读的人得自己解码
    # (2026-08-20 用户实见)。
    INTENT_RULE = (
        "调用 `functions.exec` 时，在第一条非 `// @exec:` 的代码前写 "
        "`// @intent: <十来个中文字>`；直接执行 shell 命令时，第一行写 "
        "`# <十来个中文字>`。只说明目的，不写占位词，不复述命令。"
    )

    def _session_roots(self, sid, opts):
        """这段会话里 codex 可写的全部根:工作流注册表 repos[] + 会话显式配的 runtime_roots。

        工作流的域目录是主 cwd,repos[] 里每个项目都是同一段会话的真实可写根。TUI 那条路
        把这份清单拼成 --add-dir(engines/codex.py 的 _codex_access_dirs),app-server 这条路
        对应的是 runtimeWorkspaceRoots——同一份真相源,两种表达。TUI 退场后只剩后者。
        """
        return super()._session_roots(sid, opts)

    def system_body(self, opts):
        import prompt_locale
        body = super().system_body(opts)
        locale = prompt_locale.of(opts)
        return "\n\n".join(x for x in (body, prompt_locale.text(self.CHANNEL_RULE, locale),
                                        prompt_locale.text(self.INTENT_RULE, locale)) if x)

    SEED_DELTA = CODEX_DELTA   # 引擎专属起手提示

    def _seed_extra(self, opts):
        """Compatibility accessor; new requests never build a user-message seed."""
        from context_delivery import system_context
        return "\n\n".join(x for x in (system_context(opts), self.SEED_DELTA) if x)

    def _turn_system_prompt(self, opts):
        from context_delivery import compose_system
        return compose_system(self.system_body(opts), opts, self.SEED_DELTA)

    def build_cmd(self, text, engine_sid, opts):
        opts = opts or {}
        mode = opts.get("sandbox") or opts.get("mode")
        mode = mode if mode in self.SANDBOX else self.DEFAULT_SANDBOX
        cmd = ["codex", "exec"]
        if engine_sid:
            cmd += ["resume", engine_sid]
        # 只用 `exec` 与 `exec resume` 都认的那几个标志。resume 的参数表是 exec 的一个子集,
        # 它不认 -s 也不认 --color(实测 0.146:`error: unexpected argument '--color' found`),
        # 而首回合与续接分别走这两条路——只在一边成立的写法会在第二回合才炸。
        # 认得的:--json / --skip-git-repo-check / -c / -m / --dangerously-bypass-*。
        cmd += ["--json", "--skip-git-repo-check"]
        if mode == "danger-full-access":
            cmd += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            cmd += ["-c", f'sandbox_mode="{mode}"', "-c", 'approval_policy="never"']
        # approval_policy=never:GUI 这侧还没有权限询问的回路,留 on-request 会让它停在
        # 一个没人看得见的问句上,整回合静默超时。宁可让越权的动作直接失败,失败看得见。
        if opts.get("model"):
            cmd += ["-m", str(opts["model"])]
        if opts.get("effort"):
            cmd += ["-c", f'model_reasoning_effort="{opts["effort"]}"']
        ac = str(opts.get("autocompact") or "").strip()
        if ac in {key for key, _label in self.AUTO_COMPACTS}:
            cmd += ["-c", f"model_auto_compact_token_limit={ac}"]
        # exec/resume share the documented config override carrier. Do not hide
        # a missing developer channel by prepending rules to the user's request.
        body = self._turn_system_prompt(opts)
        if body:
            cmd += ['-c', 'developer_instructions=' + json.dumps(body, ensure_ascii=False)]
        cmd.append(text)
        return cmd

    # 分支:app-server 的 thread/fork。claude 那条路是「下一回合带上 --fork-session」,
    # 分叉发生在下次说话时;codex 这条是一次 RPC 当场分出新 thread,所以不需要 fork_pending,
    # 新会话建好就已经指向新 thread 了。
    fork_flag = None          # 不走命令行标志
    fork_supported = True     # 但下面覆写了 fork_session,能力位据此上报

    def fork_session(self, sid, msg_id=""):
        """从这段会话另开一支。记录抄一份,引擎侧经 thread/fork 分出新 thread。

        与 claude 同一个语义:只能从当前末尾分,不回滚任何已经发生的事。
        """
        from codex_appserver import fork_thread
        esid = (self.index.get(sid) or {}).get("engine_sid") or ""
        if not esid:
            return None
        msgs = self.messages(sid)
        if not msgs:
            return None
        new_thread = fork_thread(esid, cwd=self.session_cwd(sid), binary=self.APPSERVER_BIN)
        if not new_thread:
            return None
        new = self.create_session()
        nsid = new["id"]
        msgs = self._fork_reference_attachments(sid, nsid, msgs)
        for m in msgs:
            m = json.loads(json.dumps(m))
            info = m.setdefault("info", {})
            info["sessionID"] = nsid
            for q in m.get("parts") or []:
                q["sessionID"] = nsid
            self._append_msg(nsid, m)
        src = self.index.get(sid) or {}
        with self._lock:
            e = self.index.get(nsid)
            if e:
                e["engine_sid"] = new_thread
                e["forked_from"] = sid
                if src.get("title"):
                    e["title"] = ("分支 · " + src["title"])[:60]
                if isinstance(src.get("opts"), dict):
                    e["opts"] = dict(src["opts"])
            self._save_index()
        self.bus.publish({"type": "session.updated", "properties": {"sessionID": nsid}})
        return dict(self.index.get(nsid) or {}, id=nsid)

    def _soft_interrupt(self, sid, proc):
        """请 app-server 停掉这一回合,不杀进程。

        回合走的是常驻的 app-server(JSON-RPC over stdio),ctx["proc"] 指的是那个 server
        本身,不是这一回合——杀它既不精确(整条连接一起没了、下一回合要冷起),实测也停不下
        那一回合:活在 server 那侧继续跑。协议里有 turn/interrupt(threadId + turnId),
        发它才是对的。发不出去返回 False,由调用方退回杀进程。"""
        try:
            from codex_appserver import interrupt
            return bool(interrupt(sid))
        except Exception:
            return False

    def _recall_native(self, sid, pending, env, checkpoint):
        """原生协议适配；状态机不依赖 Codex 的 RPC 名称。"""
        from codex_appserver import revert_thread
        return revert_thread(sid, pending.get("thread_id"), pending.get("before_turn_id"),
                             cwd=self.session_cwd(sid), env=env, binary=self.APPSERVER_BIN,
                             method=self.RECALL_METHOD, expected=pending.get("expected"),
                             checkpoint=checkpoint)

    def abort(self, sid, recall=False, client_request_id=''):
        rid = str(client_request_id or '')
        if recall and self.delivery and rid:
            receipt = self.delivery.get(sid, rid)
            if receipt.get('status') in ('accepted', 'storing', 'stored', 'cleanup_required'):
                from .cli_turn import TurnRunnerMixin
                return TurnRunnerMixin.abort(self, sid, recall=True, client_request_id=rid)
            if receipt.get('status') in ('dispatched', 'uncertain'):
                recall = False
        with self._lock:
            ctx = self._live.get(sid)
            if not ctx:
                return None
            guard = ctx.setdefault("abort_guard", threading.RLock())
        with guard:
            if self._live.get(sid) is not ctx:
                return None
            return self._abort_live(sid, ctx, recall)

    def _abort_live(self, sid, ctx, recall):
        if ctx.get("recall_response") is not None:
            return {"text": ctx["recall_response"], **self.turn_state(sid)}
        if recall and self.RECALL_METHOD:
            # 先记意图，再发中断；进程退出也不会丢失需要确认的原生撤回。
            if not self._recall_begin(ctx):
                recall = False  # 不能用新回合覆盖上一笔尚未确认的撤回。
        ctx["aborted"] = "recall" if recall else "interrupt"
        ctx["interrupt_id"] = ctx.get("interrupt_id") or f"int-{uuid.uuid4()}"
        self._turn_state_publish(sid, ctx, self.TURN_CANCELLING)
        if not recall:
            ctx["interrupt_snapshot"] = {
                "order": list(ctx.get("order") or []),
                "parts": copy.deepcopy(ctx.get("parts") or {}),
            }
        proc = ctx.get("proc")
        if self._soft_interrupt(sid, proc):
            # 正常路径保留 app-server：turn/completed 让 run_turn 返回，基类随后发布 ready。
            # 只有协议确认迟迟不来才断连，避免每次 Esc 都冷启动并等待整棵进程树退出。
            threading.Timer(self.INTERRUPT_FALLBACK_SECONDS,
                            lambda: self._interrupt_fallback(sid, ctx, proc)).start()
        else:
            self._interrupt_fallback(sid, ctx, proc)
        gone = self._pop_last_user_record(sid) if recall else None
        if recall:
            ctx["recall_response"] = (gone or {}).get("text", "")
            # live ctx 在连续发送/撤回的竞态中可能已换轮；投影 cut 必须从实际摘掉的消息开始。
            self._mark_recall(sid, (gone or {}).get("at") or ctx.get("t0") or 0)
            # turn/start 已返回时现在就能记；按得更早则由 on_turn_started 补记。
            if self.RECALL_METHOD:
                self._remember_recall_revert(ctx)
        return {"text": (gone or {}).get("text", ""), **self.turn_state(sid)}

    def _interrupt_fallback(self, sid, ctx, proc):
        """Codex 的回合跑在 app-server 连接里,兜底要摘掉整条连接。

        基类只 kill `proc`,但这里的 proc 是 app-server 顶层进程。用池里的 drop_session 收整棵
        进程树并等待退出,读线程才会从 conn.read 里醒过来,随后 _finish_turn 发布 ready,队列才能
        接着发送。
        """
        if self._live.get(sid) is not ctx or ctx.get("engine_turn_done"):
            return
        try:
            from codex_appserver import drop_session
            drop_session(sid)
            return
        except Exception:
            pass
        super()._interrupt_fallback(sid, ctx, proc)

    def _turn_dispatch(self, ctx, cmd, env, uframe):
        """GUI 回合走 app-server，Mode/Model/Effort 才能成为真实 thread 设置。"""
        from codex_appserver import run_turn
        entry = self.index.get(ctx["sid"]) or {}
        esid = entry.get("engine_sid") or ""
        saved = ctx.get("saved") or []
        images = [x[1] for x in saved if x[0] == "image" and x[1]]
        opts = self.normalize_options(ctx.get("opts") or {})

        # 上次 Esc 后若 Desk 恰好退出，原生撤回凭据仍在索引里。新消息进入 Codex 前必须
        # 先兑现，不能只在界面上藏掉旧消息、却继续把它留在模型上下文里。
        if self.RECALL_METHOD:
            reverted = self._apply_recall_revert(ctx["sid"], env)
            if not reverted.get("ok"):
                error = f"上一条已中断消息尚未从 {self.APPSERVER_LABEL} 上下文撤回：" + str(
                    reverted.get("error") or "未知错误")[:220]
                self._emit_text(ctx, error)
                return error

        def on_proc(proc):
            ctx["proc"] = proc
            self._note_proc(ctx["sid"], proc)

        def on_event(method, params):
            self._app_event(method, params, ctx)

        def on_model_selected(model):
            on_event("model/selected", {"model": model})

        def on_turn_started(thread_id, turn_id):
            ctx["esid"] = thread_id
            ctx["engine_turn_id"] = turn_id
            self._delivery_bind_native(ctx, engine_session_id=thread_id, native_turn_id=turn_id)
            # thread/started 只在新开线程时发。resume 回来的会话不再有那个事件,
            # 侧栏登记于是永远等不到,人发多少条消息聊天区都不显示这段会话。
            # 每回合按当前 thread id 补一次,已登记的在 _term_register 里直接返回。
            self._term_register(ctx["sid"], thread_id)
            if ctx.get("aborted") == "recall":
                self._remember_recall_revert(ctx)

        def on_turn_submitting(thread_id):
            # 与 abort 的持久意图共用索引锁：不是明确未发送，就是可能已发送。
            with self._lock:
                ctx["esid"] = thread_id
                if ctx.get("aborted"):
                    return False
                self._delivery_dispatched(ctx, engine_session_id=thread_id)
                ctx["recall_dispatched"] = True
                from .response_timing import mark
                mark(ctx, "submitted_ms")
                return True

        def on_environment(payload):
            payload = dict(payload or {})
            if payload.get("stage") in ("process", "initialize", "config", "thread", "ready"):
                from .response_timing import mark
                mark(ctx, "environment_" + payload["stage"] + "_ms")
            if payload.get("state") == "error" and ctx.get("aborted"):
                payload = {"state": "ready", "stage": "cancelled",
                           "label": f"{self.APPSERVER_LABEL} 启动已取消",
                           "details": [], "mcp": payload.get("mcp") or {}}
            ctx["environment"] = payload
            self._ctx_env_publish(ctx, payload)
            self._checkpoint_live(ctx, force=True)

        try:
            writer_logged = False
            for attempt in range(5):
                try:
                    turn_text = ctx.get("text") or ""
                    new_esid = run_turn(
                        self.session_cwd(ctx["sid"]), env, esid, turn_text, opts,
                        # Native option normalization deliberately drops host-owned
                        # context fields. Assemble from the untouched turn context.
                        system_text=self._turn_system_prompt(ctx.get('opts') or {}), image_paths=images,
                        runtime_roots=self._session_roots(ctx["sid"], opts),
                        on_notification=on_event, on_process=on_proc, on_environment=on_environment,
                        session_id=ctx["sid"], binary=self.APPSERVER_BIN, label=self.APPSERVER_LABEL,
                        on_approval=lambda m, c: self._approval_ask(ctx["sid"], m, c),
                        on_turn_started=on_turn_started, on_turn_submitting=on_turn_submitting,
                        on_model_selected=on_model_selected,
                        session_name=(ctx.get("_session_name") or {}).get("native_title", ""),
                        on_name_sync=lambda state: self._name_sync_status(ctx, state),
                        cancelled=lambda: bool(ctx.get("aborted")), owner=self,
                        **self._appserver_turn_kwargs(ctx))
                    # 撤回 RPC 仍发生在 _finish_turn 之前；给中断兜底一个明确边界，避免它
                    # 因 _live 尚未摘除而在撤回途中误杀已经完成的常驻连接。
                    ctx["engine_turn_done"] = True
                    if new_esid and new_esid != esid:
                        ctx["esid"] = new_esid
                        self._set_engine_sid(ctx["sid"], new_esid)
                    return None
                except Exception as exc:
                    msg = str(exc)
                    # 「already has an active writer」:codex 对同一 thread 只准一个活跃写入方。
                    # 用户刚打断上一轮时,旧 writer 释放可能比 idle 晚一点;这里摘连接、退避再试。
                    if "active writer" not in msg.lower():
                        from error_notice import failure
                        ctx['engine_failure'] = ctx.get('engine_failure') or failure(
                            msg, source='transport', code=getattr(exc, 'code', 'TRANSPORT'))
                        ctx["engine_error"] = ctx['engine_failure']['message']
                        # A declared native failed is authoritative; only EOF
                        # during Desk's own shutdown is an expected interruption.
                        if (ctx.get('_shutdown_reason') and getattr(exc, 'code', '') == 'NATIVE_EOF'
                                and (ctx.get('native_terminal') or {}).get('status') != 'failed'):
                            ctx['aborted'] = 'interrupt'
                        return ctx["engine_error"]
                    if not writer_logged:
                        writer_logged = True
                        try:
                            from codex_appserver import writer_probe
                            import deskcfg   # 同 cli_store.grounding:原来缺 import,触发即 NameError
                            deskcfg.log(self.name, "写入权冲突现场 " + str(esid)[:13] + "\n"
                                        + writer_probe(esid, home=env.get(self.MANAGED_HOME_ENV, "")))
                        except Exception:
                            pass
                    try:
                        from codex_appserver import drop_session
                        drop_session(ctx["sid"])
                    except Exception:
                        pass
                    if attempt < 4:
                        time.sleep(0.5 + attempt * 0.75)
                        continue
                    msg = (f"这段会话正在别处使用(终端那侧或另一个窗口),"
                           f"{self.APPSERVER_LABEL} 同一时间只允许一处写入。关掉那一处再试。")
                    ctx["engine_error"] = msg[:300]
                    return msg[:300]
        finally:
            if ctx.get("aborted") == "recall" and self.RECALL_METHOD:
                # HTTP 撤回线程先完成投影与 cut；收尾不能抢先放行下一轮。
                with ctx.get("abort_guard") or threading.RLock():
                    self._remember_recall_revert(ctx)
                    self._apply_recall_revert(ctx["sid"], env)
            self._clear_session_approvals(ctx["sid"])
            if ctx.get("drop_appserver_after_abort"):
                try:
                    from codex_appserver import drop_session
                    drop_session(ctx["sid"])
                except Exception:
                    pass

    def _name_sync_status(self, ctx, state):
        from .session_naming import status
        status(self, ctx['sid'], (ctx.get('_session_name') or {}).get('native_title', ''), state)

    def _appserver_turn_kwargs(self, ctx):
        """Optional protocol extensions; ordinary engine sessions retain their contract."""
        return {}

    _APP_ITEM = {
        "agentMessage": "agent_message", "reasoning": "reasoning",
        "commandExecution": "command_execution", "fileChange": "file_change",
        "mcpToolCall": "mcp_tool_call", "dynamicToolCall": "mcp_tool_call",
        "webSearch": "web_search", "plan": "todo_list",
        # 子代理:collabAgentToolCall 是派发那一下(spawn / sendInput / wait / close),
        # subAgentActivity 是它启动、被追加输入、被打断这几件事。不接的话 _item 认不出类型、
        # 整条跳过——界面上派了子代理却什么也没有,而回合又确实在等它们。
        "collabAgentToolCall": "collab_agent", "subAgentActivity": "sub_agent_activity",
    }

    @classmethod
    def _app_item(cls, item):
        """app-server camelCase ThreadItem → 现有 Codex item 适配器的字段。"""
        item = dict(item or {})
        kind = cls._APP_ITEM.get(item.get("type"), item.get("type") or "")
        out = dict(item, type=kind)
        for old, new in (("aggregatedOutput", "aggregated_output"), ("exitCode", "exit_code")):
            if old in item:
                out[new] = item.get(old)
        status = item.get("status")
        out["status"] = {"inProgress": "in_progress", "declined": "failed"}.get(status, status)
        if kind == "reasoning":
            summary = item.get("summary") or []
            out["summary"] = [{"text": x if isinstance(x, str) else x.get("text", "")}
                              for x in summary if isinstance(x, (str, dict))]
        if kind == "todo_list":
            out["items"] = [{"text": item.get("text") or "更新计划"}]
        if item.get("type") == "dynamicToolCall":
            out["server"] = item.get("namespace") or "tool"
        return out

    def _app_event(self, method, params, ctx):
        from .lifecycle import _current
        if not _current(self, ctx):
            return
        # Every accepted notification proves transport activity. Only non-empty
        # model output and known tool lifecycle steps renew semantic liveness;
        # usage/status/empty delta/current-turn metadata must not hide a stall.
        now = time.time()
        ctx["_line_at"] = now
        ctx.setdefault("_progress_at", ctx.get("t0", 0) / 1000 or now)
        from codex_appserver import _turn_semantic_event
        event_thread = params.get("threadId") or params.get("thread_id")
        event_turn = params.get("turnId") or params.get("turn_id")
        if method.startswith("turn/"):
            event_turn = ((params.get("turn") or {}).get("id") or event_turn)
        current_event = bool(event_thread and event_thread == ctx.get("esid")
                             and event_turn and event_turn == ctx.get("engine_turn_id"))
        semantic = bool(current_event and _turn_semantic_event(method, params))
        if semantic:
            ctx["_progress_at"] = now
        if method == "turn/retry":
            from .lifecycle import retry_clear, retry_update
            if params.get("active") is False:
                retry_status = str(ctx.pop("capacity_retrying", "") or "")
                retry_clear(self, ctx)
                reason = str(params.get("reason") or "")
                if retry_status and reason in ("progress", "settled"):
                    routing = retry_status == "workspace_routing"
                    self._ctx_env_publish(ctx, {"state":"ready",
                        "stage":"workspace_routing" if routing else "capacity",
                        "label":("工作区后端路由已恢复，继续执行" if routing
                                 else "模型容量已恢复，继续执行")})
                return
            try:
                attempt = max(1, int(params.get("attempt") or 1))
                maximum = max(attempt, int(params.get("max") or attempt))
                delay_ms = max(0, int(params.get("delay_ms") or 0))
                deadline_at = max(0, int(params.get("deadline_at") or 0))
            except (TypeError, ValueError):
                return
            status = str(params.get("status") or "capacity")
            routing = status == "workspace_routing"
            retry_update(self, ctx, {"provider":self.name, "source":"desk",
                "signal":str(params.get("signal") or "turn.serverOverloaded")[:100],
                "status":status, "attempt":attempt, "max":maximum,
                "delay_ms":delay_ms, "deadline_at":deadline_at,
                "error":str(params.get("error") or "")[:300]})
            ctx["capacity_retrying"] = status
            seconds = max(0, (deadline_at - int(time.time() * 1000) + 999) // 1000)
            stage = "workspace_routing" if routing else "capacity"
            label = (f"工作区后端路由暂不可用，{seconds} 秒后自动重试" if routing
                     else f"模型繁忙，{seconds} 秒后自动重试")
            self._ctx_env_publish(ctx, {"state":"working", "stage":stage,
                "label":label,
                "details":[f"第 {attempt}/{maximum} 次自动重试；不会重发原始输入"]})
            return
        # Codex 把响应流断开的自动重试也包成 error 通知,区别只在
        # willRetry=true。这不是回合失败:原生 Codex 把它放在运行中状态栏,
        # 收到后续流帧就恢复 Working。Desk 此前忽略这个字段,把 2/5 也标成
        # 终局红色错误,既误导用户又把 engine_error 留在回合上。
        if method == "error" and (params.get("willRetry") or params.get("will_retry")):
            self._stream_retrying(ctx, params)
            return
        if ctx.get("stream_retrying"):
            if method == "error" or (method == "turn/failed" and current_event):
                ctx.pop("stream_retrying", None)
            elif method == "turn/completed" and current_event:
                ctx.pop("stream_retrying", None)
                if (params.get("turn") or {}).get("status") != "failed":
                    self._stream_recovered(ctx)
            elif semantic:
                self._stream_recovered(ctx)
        if method == "thread/started":
            tid = ((params.get("thread") or {}).get("id") or "")
            if tid:
                ctx["esid"] = tid
                self._set_engine_sid(ctx["sid"], tid)
            return
        if method == "item/agentMessage/delta":
            # app-server 是逐字发的(实测「你好世界」分两帧到)。此前只认 item/started 与
            # item/completed,delta 全丢,于是 codex 的正文要等整块写完才一次出现——
            # 界面上是长时间空白然后突然落一整段,和「没在动」分不出来。
            # 块的归属按 itemId,与 _item 里的 pid 算法保持一致。
            txt = params.get("delta") or ""
            iid = params.get("itemId") or ""
            if not (txt and iid):
                return
            pid = f'{ctx["msg_id"]}_{iid}'
            phase = (ctx.setdefault("phase", {})).get(iid) or "text"
            part = self._ensure_part(ctx, pid, phase, text="")
            part["text"] = (part.get("text") or "") + txt
            self._delta(ctx, pid, phase, txt)
            (ctx.setdefault("streamed", set())).add(iid)
            return
        if method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            # 思考也是逐字发的,方法名和 agentMessage 那条不同,此前没接,于是 codex 这侧
            # 整个回合都收不到 field=reasoning 的增量——顶行的「正在思考」永远不出现,
            # 展开区里的思考也要等 item/completed 才一次落下。
            # 同一条 item 可能既发 summary 又发原文,两路都收会把同一段思考记两遍;
            # 有 summary 就以 summary 为准,原文那路让开(codex 终端显示的也是 summary)。
            txt = params.get("delta") or ""
            iid = params.get("itemId") or ""
            if not (txt and iid):
                return
            summ = ctx.setdefault("rsummary", set())
            summary = method.endswith("summaryTextDelta")
            if not summary and iid in summ:
                return
            pid = f'{ctx["msg_id"]}_{iid}'
            part = self._ensure_part(ctx, pid, "reasoning", text="")
            if summary and iid not in summ:
                # 同一 reasoning item 会先流原始思考、再从头流一份摘要。旧实现把两份
                # 直接拼在一起；完成帧与历史记录却只有摘要，于是每轮结束都被前端判为
                # shape 漂移并整页重建。首个摘要增量明确替换原文，后续摘要再正常追加。
                summ.add(iid)
                if part.get("text"):
                    part["text"] = txt
                    self._part_replaced(ctx, part)
                    (ctx.setdefault("streamed", set())).add(iid)
                    return
            part["text"] = (part.get("text") or "") + txt
            self._delta(ctx, pid, "reasoning", txt)
            (ctx.setdefault("streamed", set())).add(iid)
            return
        if method in ("item/started", "item/completed"):
            item = params.get("item") or {}
            if item.get("type") == "contextCompaction":
                from .lifecycle import compact_update
                # 自动压缩与手动 /compact 走不同入口:前者是普通回合里的 item,
                # app-server 会给 started / completed 两帧。两帧都转成同一套 session.compact,
                # 前端才有起点可画进度；开始时间留在 live ctx,刷新页面后也能接着估。
                running = method == "item/started"
                if running:
                    ctx["compact_t0"] = int(time.time() * 1000)
                started = ctx.get("compact_t0") or 0
                compact_update(self, ctx, {
                    "sessionID": ctx["sid"], "running": running,
                    "started": started if running else 0,
                    "trigger": "auto"})
                if not running:
                    from .session_naming import invalidate
                    invalidate(self, ctx['sid'])
                    ctx["compact_t0"] = 0
                return
            # 记下这一块的去向:commentary 进「处理中」,final_answer 才是正文。
            # delta 先于 completed 到,所以归类要在 started 的时候就定下来。
            if item.get("type") == "agentMessage" and item.get("id"):
                ctx.setdefault("phase", {})[item["id"]] = (
                    "reasoning" if item.get("phase") == "commentary" else "text")
            self._item(self._app_item(item), ctx, done=method == "item/completed")
            return
        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or params.get("token_usage") or {}
            total = usage.get("total") or usage.get("totalTokenUsage") \
                or usage.get("total_token_usage") or usage
            last = usage.get("last") or usage.get("lastTokenUsage") \
                or usage.get("last_token_usage")

            def token_shape(value):
                value = value if isinstance(value, dict) else {}
                out = {
                    "total": int(value.get("totalTokens") or value.get("total_tokens") or 0),
                    "cached": int(value.get("cachedInputTokens") or value.get("cached_input_tokens")
                                  or value.get("cacheReadTokens") or value.get("cache_read_tokens") or 0),
                }
                if any(key in value for key in ("inputTokens", "input_tokens")):
                    out["input"] = int(value.get("inputTokens") or value.get("input_tokens") or 0)
                if any(key in value for key in ("outputTokens", "output_tokens")):
                    out["output"] = int(value.get("outputTokens") or value.get("output_tokens") or 0)
                return out

            cumulative = token_shape(total)
            key = str(ctx.get("esid") or ctx.get("sid") or "")
            baselines = getattr(self, "_codex_usage_baselines", None)
            if baselines is None:
                baselines = self._codex_usage_baselines = {}
            previous = baselines.get(key)
            baselines[key] = cumulative
            duplicate = bool(previous and cumulative["total"] == previous["total"])
            increment = None
            if duplicate and isinstance(last, dict):
                increment = token_shape(last)  # richer repeat of the same snapshot
            elif duplicate and cumulative.get("total") and (
                    "input" in cumulative or "output" in cumulative):
                increment = cumulative
            elif not duplicate and isinstance(last, dict):
                increment = token_shape(last)
            elif not duplicate and previous:
                # Compatibility for older app-server builds without `last`:
                # thread totals are cumulative, so only a proven positive delta
                # is a per-turn measurement.
                increment = {"total": max(0, cumulative["total"] - previous["total"]),
                             "cached": max(0, cumulative["cached"] - previous["cached"])}
                if "input" in cumulative and "input" in previous:
                    increment["input"] = max(0, cumulative["input"] - previous["input"])
                if "output" in cumulative and "output" in previous:
                    increment["output"] = max(0, cumulative["output"] - previous["output"])
            elif not duplicate and int(ctx.get("seq") or 0) == 1:
                # A brand-new Desk thread has no prior native usage.
                increment = cumulative

            if increment and increment["total"] > 0:
                # Repeated notifications may first carry totals only and later
                # refine the same snapshot with input/output detail. Key each
                # native snapshot by its cumulative total, replace refinements,
                # then recompute the Desk-turn sum without double charging.
                contributions = ctx.setdefault("_codex_usage_contributions", {})
                contributions[str(cumulative["total"])] = increment
                accumulated = {"total": 0, "input": 0, "output": 0, "cached": 0}
                complete = True
                for contribution in contributions.values():
                    accumulated["total"] += contribution["total"]
                    accumulated["cached"] += contribution["cached"]
                    if "input" not in contribution or "output" not in contribution:
                        complete = False
                    accumulated["input"] += contribution.get("input", 0)
                    accumulated["output"] += contribution.get("output", 0)
                detail = {"total": accumulated["total"], "cached": accumulated["cached"]}
                if complete:
                    detail.update(input=accumulated["input"], output=accumulated["output"])
                ctx["_token_usage"] = detail
                ctx["tokens"] = detail["total"]
                ctx["cached_tokens"] = detail["cached"]
            self.bus.publish({"type": "session.stats", "properties": {
                "sessionID": ctx["sid"], "tokens": int(ctx.get("tokens") or 0),
                "ms": int(time.time() * 1000) - (ctx.get("t0") or 0), "cost": 0}})
            return
        if method in ("model/selected", "model/rerouted"):
            to = (params.get("model") if method == "model/selected" else
                  params.get("toModel") or params.get("to_model"))
            if to:
                ctx["actual_model"] = str(to)
                self.set_session_model(ctx.get("sid"), to, since=ctx.get("t0"))   # 按会话记,见 cli_store
            return
        if method == "turn/completed":
            turn = params.get("turn") or {}
            ctx["native_terminal"] = {"source": params.get("terminalSource") or method,
                                      "status": turn.get("status") or "unknown"}
            ctx["ms"] = int(turn.get("durationMs") or 0) or int(time.time() * 1000) - (ctx.get("t0") or 0)
            # tokenUsage 通知并非每轮都有；耗时不能跟着它一起缺席。收尾前固定再发一次，
            # 前端的「已处理 12.3s」因此不依赖目录对账或页面刷新才能补出来。
            self.bus.publish({"type": "session.stats", "properties": {
                "sessionID": ctx["sid"], "tokens": int(ctx.get("tokens") or 0),
                "ms": ctx["ms"], "cost": 0}})
            if turn.get("status") == "failed":
                err = turn.get("error") or {}
                self._turn_failed(ctx, err or f"{self.APPSERVER_LABEL} 回合失败")
            return
        if method in ("error", "turn/failed"):
            err = params.get("error") or {}
            self._turn_failed(ctx, err or params.get("message")
                              or f"{self.APPSERVER_LABEL} 错误")

    def _stream_retrying(self, ctx, params):
        """响应流的可恢复断开:只更新运行状态,不结束回合。"""
        from .lifecycle import retry_update
        err = params.get("error") or {}
        message = str(err.get("message") or params.get("message") or "").strip()
        count = re.search(r"(\d+\s*/\s*\d+)", message)
        label = "连接中断，正在重连" + (f" · {count.group(1)}" if count else "")
        detail = str(err.get("additionalDetails") or err.get("additional_details") or "").strip()
        if not retry_update(self, ctx, {"provider": self.name,
                "source": "native", "signal": "error.willRetry",
                "error": message[:300]}):
            return
        ctx["stream_retrying"] = True
        self._ctx_env_publish(ctx, {
            "state": "working", "stage": "reconnect", "label": label,
            "details": [detail[:300]] if detail else []})

    def _stream_recovered(self, ctx):
        """第一条恢复的流帧到达时撤掉重连提示。"""
        from .lifecycle import retry_clear
        ctx.pop("stream_retrying", None)
        retry_clear(self, ctx)
        self._ctx_env_publish(ctx, {
            "state": "ready", "stage": "reconnect",
            "label": "连接已恢复，继续执行"})

    def _turn_failed(self, ctx, message):
        """回合被引擎判失败：记住失败终态，完整错误挂到输入框上方。

        过程状态只显示中性的「已停止」；诊断正文集中在可展开提示条。下一轮开始会清除
        上一轮通用错误，认证、恢复等专用提示仍由各自状态机管理。
        """
        from error_notice import failure
        fact = failure(message, source='native')
        msg = fact['message']
        if not msg:
            return
        ctx["engine_error"] = msg
        ctx['engine_failure'] = fact
        head = self._notice_head(msg)
        self._ctx_env_publish(ctx, {
            "state": "error", "stage": "turn", "label": head,
            "details": [msg] if msg != head else []})

    # item.type → 工具卡上的工具名。前端按小写工具名取动词与图标(HS_TOOL_VERBS),
    # 所以这里翻成它认得的那批名字,而不是原样传 codex 的类型名。
    # ---- 搬迁:把 codex 的 rollout 记录翻成契约消息 ----
    # codex 没有 serve,历史只能读它自己的记录。记录按日期分层落在
    # ~/.codex/sessions/<年>/<月>/<日>/rollout-<时刻>-<thread_id>.jsonl,归档的另在
    # archived_sessions/。文件名尾部就是 thread_id,也就是我们存的 engine_sid。
    def _codex_title(self, esid):
        try:
            from term import _session_index_titles, _state_db_titles
            return str(_session_index_titles(self.TITLE_ENGINE).get(esid)
                       or _state_db_titles(self.TITLE_ENGINE).get(esid) or "").strip()
        except Exception:
            pass
        return ""

    def _external_title(self, esid, sid="", path=""):
        """只读取原生标题；Desk 手动名由统一名称投影叠加。"""
        try:
            from term import _codex_rollout_title
            title = _codex_rollout_title(path, self.TITLE_ENGINE) if path else ""
            return str(title or "").strip()
        except Exception:
            return self._codex_title(esid)

    def sync_external_title(self, esid=""):
        """只同步引擎原生标题；Desk 手动名在读取时投影，清空后可回落。"""
        changed = []
        try:
            from term import _codex_sessions
            native_titles = {x.get("id"): x.get("title")
                             for x in _codex_sessions(self.TITLE_ENGINE)}
        except Exception:
            native_titles = {}
        with self._lock:
            rows = [(sid, dict(row)) for sid, row in self.index.items()
                    if not esid or row.get("engine_sid") == esid]
        for sid, row in rows:
            native = row.get("engine_sid") or ""
            if not native:
                continue
            title = native_titles.get(native) or ""
            if not title and esid:
                title = self._external_title(native, sid, self._rollout_path(native))
            if not title or title == row.get("title"):
                continue
            with self._lock:
                if sid in self.index:
                    self.index[sid]["title"] = title[:60]
            changed.append(sid)
        if changed:
            with self._lock:
                self._save_index()
            for sid in changed:
                self.bus.publish({"type": "session.updated", "properties": {"sessionID": sid}})
        return changed

    def list_sessions(self):
        self.sync_external_title()
        return super().list_sessions()




    # ---- 搬迁:从 rollout 记录重建 ----
    # codex 没有 serve 可问,只能像 claude 那样读它自己的记录。记录在
    # ~/.codex/sessions/<年>/<月>/<日>/rollout-<时刻>-<thread_id>.jsonl,归档的另在
    # archived_sessions/。thread_id 就是我们存的 engine_sid(thread.started 给的那个)。
    ROLLOUT_DIRS = ("~/.codex/sessions", "~/.codex/archived_sessions")
    # app-server 那一层的二进制与文案。协议同构的派生适配器只换这几项:
    # 二进制名、界面上报的引擎名、记录目录、模型标签。整套连接、会话缓存、事件翻译照用。
    APPSERVER_BIN = "codex"
    APPSERVER_LABEL = "Codex"

    # 记录里的工具名与实时那条路的 item 类型是两套词汇:实时给的是 command_execution 这类
    # 语义名,记录里是模型实际调的函数名。翻到同一批前端认得的工具卡上。
    CODEX_TOOL = {
        "exec_command": "bash", "exec": "bash", "shell": "bash", "write_stdin": "bash", "wait": "bash",
        "apply_patch": "patch", "update_plan": "todowrite",
        "create_goal": "task", "update_goal": "task",
    }

    def import_session(self, esid, sid=None, limit=SESSION_WINDOW):
        """把终端那侧的一段 codex 会话搬进 HTML 视图。整份覆盖,理由与另两个引擎一致。

        读法只有一处:_group_rollout。搬迁与往上翻页看的是同一份解析结果,
        两处各写一套的话,同一段会话在「打开」和「翻上去」时会长得不一样。"""
        path = self._rollout_path(esid)
        if not path:
            return None
        if not sid:
            with self._lock:
                sid = next((k for k, v in self.index.items() if v.get("engine_sid") == esid), None)
            if not sid:
                sid = self.create_session(self.adopt_domain(esid))["id"]
        # 回合运行中由 _live 快照负责界面恢复。此时重读 rollout 会拿到半截记录并整份覆盖
        # 消息投影；等本轮收尾再按 t0 清理时，前一轮也可能随错误时刻一起被删掉。
        if self._live.get(sid):
            return dict(self.index.get(sid) or {}, id=sid)
        # 切 GUI 只要最近若干回合,与往上翻页走同一条尾窗(_rollout_tail)。
        # 这里原来另抄了一份「开 4MB 窗口、不够翻四倍」的循环:注释写着「读法只有一处」,
        # 说的是解析,开窗却是两套。于是 2026-08-23 把翻页那条换成字节标记定位之后,
        # 切会话仍走旧的——用户说的「Desk Opt、Desk Iter 切起来特别慢」正是这条路
        # (383MB 那段要开窗四次、累计读 324MB)。合成一处,两边不会再各自漂。
        msgs = self._rollout_tail(path, sid, limit)
        if msgs is None:
            return None
        if self._import_poorer(sid, msgs):
            return dict(self.index.get(sid) or {}, id=sid)   # 记录比现有存储穷,不覆盖
        # rollout 不保存 Desk 侧量到的回合耗时；直接覆盖会把刚落盘的 info.ms 抹掉。
        # 从旧投影按助手最终正文匹配回填，正文相同才继承，避免把相邻回合的耗时串错。
        old = super().messages(sid)
        def newest_user(rows):
            return max((int((m.get('info') or {}).get('at') or 0)
                        for m in self._filter_recalled_turns(sid, rows)
                        if self._turn_start(m)), default=0)
        old_at, new_at = newest_user(old), newest_user(msgs)
        if old_at and new_at and old_at > new_at + 1000:
            # A longer old branch is still stale. Counting turns alone permits it
            # to overwrite a shorter recent window after a native revert.
            return dict(self.index.get(sid) or {}, id=sid)
        timed = {}
        for m in old:
            info = m.get("info") or {}
            if info.get("role") != "assistant" or not info.get("ms"):
                continue
            text = "".join(str(q.get("text") or "") for q in m.get("parts") or []
                           if q.get("type") == "text").strip()
            if text:
                timed.setdefault(text, []).append(info["ms"])
        # 旧投影通常只保留最近 50 回合，而 rollout 是全量；从末尾对齐，重复的短回复
        # （如“完成。”）也不会把最近一轮的耗时误填到很早以前。
        for m in reversed(msgs):
            info = m.get("info") or {}
            if info.get("role") != "assistant" or info.get("ms"):
                continue
            text = "".join(str(q.get("text") or "") for q in m.get("parts") or []
                           if q.get("type") == "text").strip()
            values = timed.get(text) if text else None
            if values:
                info["ms"] = values.pop()
        msgs = self._reinstate_local_only(sid, msgs)
        title = self._external_title(esid, sid, path)
        for m in msgs:
            if title:
                break
            if not self._turn_start(m):
                continue
            t0 = ((m["parts"] or [{}])[0].get("text") or "").strip()
            if t0:
                title = t0
                break
        if limit:
            starts = [i for i, m in enumerate(msgs) if self._turn_start(m)]
            if len(starts) > limit:
                msgs = msgs[starts[-limit]:]
        # 解析大 rollout 期间可能刚好开始了下一轮。写入前再检查一次，避免用解析前的
        # 历史覆盖刚落盘的用户消息与正在流出的回复。
        if self._live.get(sid):
            return dict(self.index.get(sid) or {}, id=sid)
        self._rewrite_msgs(sid, msgs)
        with self._lock:
            e = self.index.get(sid)
            if e is not None:
                e["engine_sid"] = esid
                if title:
                    e["title"] = title[:60]
                self._save_index()
        self._term_register(sid, esid)   # 找回聊天走的是这条路,登记不能只挂在新建那条
        return dict(self.index.get(sid) or {}, id=sid)

    def handle_event(self, obj, ctx):
        t = obj.get("type")
        if t == "thread.started":
            tid = obj.get("thread_id")
            if tid:
                ctx["esid"] = tid
                self._set_engine_sid(ctx["sid"], tid)
            return
        if t in ("item.started", "item.updated", "item.completed"):
            return self._item(obj.get("item") or {}, ctx, done=(t == "item.completed"))
        if t == "turn.completed":
            u = obj.get("usage") or {}
            tok = sum(int(u.get(k) or 0) for k in
                      ("input_tokens", "output_tokens", "cached_input_tokens",
                       "cache_write_input_tokens"))
            ctx["tokens"] = tok
            ctx["cached_tokens"] = int(u.get("cached_input_tokens") or 0)
            ctx["_token_usage"] = {
                "total": tok,
                "input": int(u.get("input_tokens") or 0),
                "output": int(u.get("output_tokens") or 0),
                "cached": int(u.get("cached_input_tokens") or 0),
                "cache_write": int(u.get("cache_write_input_tokens") or 0),
            }
            ctx["ms"] = int(time.time() * 1000) - (ctx.get("t0") or 0)
            self.bus.publish({"type": "session.stats", "properties": {
                "sessionID": ctx["sid"], "tokens": tok, "ms": ctx["ms"], "cost": 0}})
            return
        if t in ("turn.failed", "error"):
            err = obj.get("error") or {}
            self._turn_failed(ctx, err or obj.get('message'))
            self._publish_turn_error(ctx['sid'], ctx, ctx['engine_failure'])

    def _item(self, item, ctx, done):
        iid = item.get("id") or ""
        kind = item.get("type") or ""
        pid = f'{ctx["msg_id"]}_{iid or kind}'
        if kind in ("agent_message", "reasoning"):
            if not done:
                return          # 整块出字:块没完成时没有内容可画,等 completed
            # Codex 的可读过程说明不是 reasoning item。后者通常只有 encrypted_content，
            # summary 也可能为空；真正给 UI 的说明是 agent_message + phase=commentary。
            # final_answer 才能进入正文，避免“我先检查…”与最终答复混在一起。
            commentary = kind == "reasoning" or item.get("phase") == "commentary"
            field = "reasoning" if commentary else "text"
            txt = item.get("text") or ""
            if not txt and kind == "reasoning":
                txt = self._rollout_text(item.get("summary") or [])
            if not txt:
                return
            part = self._ensure_part(ctx, pid, "text" if field == "text" else "reasoning", text="")
            if part.get("text"):
                # 同一个 item 只画一次(item.updated 与 completed 都会到这里)。
                # 流式已经画过时,以完成帧为准补齐差额:delta 丢一帧的话正文会少一截,
                # 而完成帧带的是全文。前缀不同时是摘要化或引擎改写,明确发替换帧。
                if txt.startswith(part["text"]) and len(txt) > len(part["text"]):
                    add = txt[len(part["text"]):]
                    part["text"] = txt
                    self._delta(ctx, pid, field, add)
                elif txt != part["text"]:
                    # 完成帧是这一块的权威全文。摘要化或引擎改写时它可能比流式值更短、
                    # 也可能不是旧值的前缀；留着旧值会让实时页与 rollout 永久不同。
                    part["text"] = txt
                    self._part_replaced(ctx, part)
                    return
                # delta 是瞬时流,EventSource 断线后不会补发。完成帧必须再给一份全文快照:
                # 手机切后台或网络切换若恰好漏掉最终 delta,仍可由这一帧恢复正文,不用等刷新。
                self._part_updated(ctx, part)
                return
            part["text"] = txt
            self._delta(ctx, pid, field, txt)
            self._part_updated(ctx, part)
            return
        if kind == "error":
            self._emit_text(ctx, "\n(引擎错误:" + str(item.get("message") or "")[:300] + ")")
            return
        tool = self._item_tool(kind, item)
        if not tool:
            return              # 不认识的 item 类型:不画空壳,留给下一版按需补
        part = self._ensure_part(ctx, pid, "tool", tool=tool,
                                 state={"status": "running", "input": {}})
        part["call_id"] = item.get("call_id") or item.get("id") or pid
        if kind in {"collab_agent", "sub_agent_activity"}:
            part["execution_kind"] = "internal"  # 派发/等待回执不冒充子代理报告。
        ctx["last_tool"] = pid
        inp = self._tool_input(item)
        if inp:
            part["state"]["input"] = inp
        st = self._DONE.get(item.get("status") or "", "running" if not done else "done")
        # 退出码不为零就是失败,不管 status 写的是什么:命令跑完了但没跑成,这两件事 codex
        # 分在两个字段上,只看 status 会把失败画成成功。
        if st == "done" and (item.get("exit_code") or 0) != 0:
            st = "error"
        part["state"].pop("failure", None)
        part["state"].update(result_state(item, st))
        if item.get("exit_code") is not None:
            part["state"]["exit_code"] = item["exit_code"]
        out = self._tool_output(item)
        if out:
            part["state"]["output"] = out
        self._part_updated(ctx, part)


# 分拆胶水:mixin 方法体按全局名引用装配类(调用时才查),模块级互 import 成环,
# 改为装配完成后回注绑定。mixin 文本因此一字不动。
from . import codex_rollout as _codex_rollout
_codex_rollout.CodexAdapter = CodexAdapter
