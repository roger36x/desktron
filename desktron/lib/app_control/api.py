"""App Control Runtime 的 transport-neutral HTTP 面。

Agent 端只能认领用户已委托的租约并请求观察/动作；创建委托、adapter 取活与
结果回写要求原生宿主令牌。审批另支持已登录手机的同源 Cookie 请求。
"""
import hmac
import json
import os
import urllib.parse

from .runtime import AppControlError, runtime
from .capability import verify as verify_session_capability


PREFIX = "/workbench/app-control"
HOST_HEADER = "X-Desk-App-Control-Host"
SESSION_HEADER = "X-Desk-App-Control-Session"

# 宿主令牌在本模块导入时就从进程环境里摘走,只留在这个变量里。
# 原来 _host() 每次去 os.environ 读,而各引擎的子进程环境都是 dict(os.environ) 整份复制
# (cli_turn、claude 回退、codex 压缩、pi 控制 RPC、recap 支线五处),于是令牌原样进了
# 每个 Agent 会话——2026-09-02 在 claude 会话里 `env` 直接看到它。持有它的 Agent 可以调
# /host/approve 给自己的外部写入放行,整道审批闸形同虚设。摘走之后所有 dict(os.environ)
# 自然干净;测试仍可事后往环境里放令牌(见 _host 的回退)。
HOST_TOKEN = os.environ.pop("DESKTRON_APP_CONTROL_HOST_TOKEN", "")


def owns(path, method):
    return path == PREFIX or path.startswith(PREFIX + "/")


def _body(h):
    try:
        length = int(h.headers.get("Content-Length", 0))
        value = json.loads(h.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("not object")
        return value
    except Exception:
        raise AppControlError("请求体必须是 JSON 对象", 400)


def _log(msg):
    try:
        import deskcfg
        deskcfg.log("app-control", msg)
    except Exception:
        pass


def _turn_anchor(session_id):
    """审批创建当下的会话坐标。只认活动回合，不拿“最后一轮”猜后台操作的归属。"""
    if not session_id:
        return {}
    try:
        from engine_adapters.persist import _ADAPTERS
        for ad in list(_ADAPTERS):
            ctx = (getattr(ad, "_live", {}) or {}).get(session_id)
            if not isinstance(ctx, dict):
                continue
            order = ctx.get("order") or []
            return {"turn_id": str(ctx.get("msg_id") or ""),
                    "turn_seq": int(ctx.get("seq") or 0),
                    "after_part_id": str(order[-1] if order else ""),
                    "after_part_index": len(order)}
    except Exception:
        pass
    return {}


def _host(h):
    # 认两处:导入时摘走的那份(生产态唯一来源),以及事后放进环境的那份(测试与手工
    # 调试用;生产态环境里已经没有它)。任一匹配即放行。
    expected = [x for x in (HOST_TOKEN, os.environ.get("DESKTRON_APP_CONTROL_HOST_TOKEN", "")) if x]
    actual = h.headers.get(HOST_HEADER, "")
    if not expected:
        _log("宿主端点被调但令牌未启用(后端不是原生壳拉起的?)")
        raise AppControlError("原生宿主令牌未启用", 503)
    if not actual or not any(hmac.compare_digest(actual, x) for x in expected):
        _log("宿主端点令牌不匹配,已拒绝")
        raise AppControlError("仅原生宿主可执行该操作", 403)


def _ui(h):
    """全局账本只接受主 WKWebView 经固定原生桥代发的宿主能力。"""
    _host(h)


def _approval_ui(h):
    # 仅审批账本与决定可由已登录手机访问；授权管理、租约与宿主执行面仍仅限原生桥。
    if h.headers.get(HOST_HEADER):
        return _ui(h)
    from .browser_auth import require_browser
    return require_browser(h)


def _agent(h, session_id):
    """验证请求确实来自该 Desk 会话的 Agent 进程。"""
    sid = str(session_id or "")
    if not sid:
        raise AppControlError("session_id 必填", 400)
    if not verify_session_capability(sid, h.headers.get(SESSION_HEADER, ""),
                                     getattr(h, "app_control_peer_pid", 0)):
        raise AppControlError("Agent 会话能力无效", 403)
    return sid


def _ok(h, value, code=200):
    return h._send_json(value, code)


def handle(h, path, query, method):
    rt = runtime()
    try:
        if path == PREFIX and method == "GET":
            return _ok(h, {"protocol": "desk.app-control/v1alpha1", "apps": rt.controlled_apps()})

        if path == PREFIX + "/probe" and method == "GET":
            params = urllib.parse.parse_qs(query)
            sid = (params.get("session_id") or [""])[0]
            if sid:
                _agent(h, sid)
            return _ok(h, rt.probe(sid,
                                   (params.get("app_id") or ["browser"])[0]))

        if path == PREFIX + "/desktop/status" and method == "GET":
            params = urllib.parse.parse_qs(query)
            _agent(h, (params.get("session_id") or [""])[0])
            return _ok(h, rt.desktop_status())

        if path == PREFIX + "/claim" and method == "POST":
            b = _body(h)
            sid = _agent(h, b.get("session_id"))
            return _ok(h, rt.claim(sid, str(b.get("app_id") or ""),
                                   str(b.get("resource_id") or ""), b.get("ttl_seconds", 300)))

        if path == PREFIX + "/open" and method == "POST":
            b = _body(h)
            # isolated 缺省为真:不写就是隔离标签。默认必须落在权限更小的那一侧,
            # 漏传参数不该悄悄拿到用户身份。
            isolated = b.get("isolated")
            sid = _agent(h, b.get("session_id"))
            op = rt.open_tab(sid, str(b.get("app_id") or "browser"),
                             str(b.get("url") or ""),
                             True if isolated is None else bool(isolated),
                             str(b.get("scope_key") or ""), _turn_anchor(sid),
                             str(b.get("intent") or ""),
                             b.get("allowed_main_frame_hosts") or [])
            return _ok(h, op, 202)

        if path == PREFIX + "/grants" and method == "GET":
            _ui(h)
            params = urllib.parse.parse_qs(query)
            return _ok(h, {"grants": rt.site_grants((params.get("app_id") or [""])[0],
                                                    (params.get("scope_key") or [""])[0])})

        if path == PREFIX + "/resources" and method == "GET":
            params = urllib.parse.parse_qs(query)
            sid = _agent(h, (params.get("session_id") or [""])[0])
            return _ok(h, {"resources": rt.resources(
                sid,
                (params.get("app_id") or [""])[0])})

        if path == PREFIX + "/sequence" and method == "GET":
            params = urllib.parse.parse_qs(query)
            sid = _agent(h, (params.get("session_id") or [""])[0])
            return _ok(h, rt.successful_sequence(
                sid,
                (params.get("app_id") or ["browser"])[0],
                (params.get("limit") or ["100"])[0]))

        if path.startswith(PREFIX + "/trace/") and method == "GET":
            operation_id = urllib.parse.unquote(path[len(PREFIX + "/trace/"):]).strip("/")
            params = urllib.parse.parse_qs(query)
            sid = _agent(h, (params.get("session_id") or [""])[0])
            return _ok(h, rt.trace(operation_id, sid))

        if path == PREFIX + "/grants" and method == "DELETE":
            _ui(h)
            b = _body(h)
            return _ok(h, rt.revoke_site(str(b.get("app_id") or "browser"),
                                         str(b.get("origin") or ""),
                                         str(b.get("scope_key") or "")))

        if path == PREFIX + "/inspect" and method == "POST":
            b = _body(h)
            sid = _agent(h, b.get("session_id"))
            op = rt.inspect(str(b.get("lease_id") or ""), sid,
                            str(b.get("observation") or "document"),
                            str(b.get("expected_version") or ""),
                            str(b.get("idempotency_key") or ""), _turn_anchor(sid),
                            b.get("settle_ms"), b.get("input") or {},
                            str(b.get("intent") or ""))
            return _ok(h, op, 202)

        if path == PREFIX + "/act" and method == "POST":
            b = _body(h)
            sid = _agent(h, b.get("session_id"))
            op = rt.act(str(b.get("lease_id") or ""), sid,
                        str(b.get("action") or ""), b.get("input") or {},
                        str(b.get("expected_version") or ""),
                        str(b.get("idempotency_key") or ""), _turn_anchor(sid),
                        str(b.get("intent") or ""))
            return _ok(h, op, 202)

        if path == PREFIX + "/yield" and method == "POST":
            b = _body(h)
            sid = _agent(h, b.get("session_id"))
            return _ok(h, rt.yield_lease(str(b.get("lease_id") or ""),
                                         sid))

        if path.startswith(PREFIX + "/leases/") and path.endswith("/heartbeat") and method == "POST":
            lease_id = urllib.parse.unquote(path[len(PREFIX + "/leases/"):-len("/heartbeat")]).strip("/")
            b = _body(h)
            sid = _agent(h, b.get("session_id"))
            return _ok(h, rt.heartbeat(lease_id, sid,
                                       b.get("ttl_seconds", 300)))

        if path.startswith(PREFIX + "/operations/") and method == "GET":
            tail = urllib.parse.unquote(path[len(PREFIX + "/operations/"):]).strip("/")
            params = urllib.parse.parse_qs(query)
            sid = _agent(h, (params.get("session_id") or [""])[0])
            if tail.endswith("/wait"):
                op_id = tail[:-len("/wait")].strip("/")
                timeout = (params.get("timeout") or ["25"])[0]
                return _ok(h, rt.wait_operation(op_id, float(timeout), sid))
            return _ok(h, rt.get_operation(tail, sid))

        if path.startswith(PREFIX + "/receipts/") and method == "GET":
            op_id = urllib.parse.unquote(path[len(PREFIX + "/receipts/"):]).strip("/")
            params = urllib.parse.parse_qs(query)
            sid = _agent(h, (params.get("session_id") or [""])[0])
            return _ok(h, rt.receipt(op_id, sid))

        if path == PREFIX + "/events" and method == "GET":
            params = urllib.parse.parse_qs(query)
            sid = _agent(h, (params.get("session_id") or [""])[0])
            return _ok(h, {"events": rt.events_after(
                sid,
                (params.get("after") or ["0"])[0],
                (params.get("app_id") or [""])[0],
                (params.get("limit") or ["100"])[0])})

        # 界面用的审批面。待处理与回执都呈现在发起动作的会话正文，桌面与手机共用；
        # 原生壳不再弹第二套 NSAlert，避免两个入口竞速。
        if path == PREFIX + "/approvals" and method == "GET":
            params = urllib.parse.parse_qs(query)
            _approval_ui(h)
            limit = (params.get("limit") or ["20"])[0]
            out = {"pending": rt.pending_all(limit)}
            if (params.get("include_resolved") or [""])[0] in ("1", "true", "yes"):
                out["approvals"] = rt.approval_history(limit)
            return _ok(h, out)

        if path == PREFIX + "/approvals/ticket" and method == "POST":
            b = _body(h)
            return _ok(h, {"approval": rt.approval_for_ticket(
                str(b.get("operation_id") or ""), str(b.get("ticket") or ""))})

        if path == PREFIX + "/approvals/decide" and method == "POST":
            b = _body(h)
            ticket = str(b.get("ticket") or "")
            if ticket:
                return _ok(h, rt.approve_with_ticket(
                    str(b.get("operation_id") or ""), ticket,
                    bool(b.get("approved")), b.get("anchor")))
            _approval_ui(h)
            # approver 记成 ui_user,与原生那条的 native_user 分开——事后查回执时
            # 要看得出这一次是谁在哪儿点的。
            return _ok(h, rt.approve(str(b.get("operation_id") or ""),
                                     bool(b.get("approved")), "ui_user", b.get("anchor")))

        if path == PREFIX + "/approvals/clear" and method == "POST":
            _approval_ui(h)
            b = _body(h)
            return _ok(h, rt.clear_approval_history(b.get("before", 0)))

        # 以下路径不是 Agent transport：必须由主 WKWebView → 原生桥带宿主令牌调用。
        if path == PREFIX + "/host/desktop-capability" and method == "POST":
            _host(h); b = _body(h)
            return _ok(h, rt.report_desktop_capability(
                str(b.get("host_instance_id") or ""), str(b.get("host_bundle_id") or ""),
                b.get("host_pid"), str(b.get("status") or ""), b.get("checked_at"),
                str(b.get("monitoring_status") or "unavailable")))

        # 原生 L1：宿主取 allowlist、登记人选定的窗口、报告介入/漂移、取活与回执。
        if path == PREFIX + "/host/desktop-apps" and method == "GET":
            _host(h)
            return _ok(h, {"apps": rt.desktop_apps_for_host()})

        if path == PREFIX + "/host/desktop-target" and method == "POST":
            _host(h); b = _body(h)
            out = rt.select_desktop_target(
                str(b.get("app_id") or ""), str(b.get("bundle_id") or ""), b.get("pid"),
                str(b.get("process_start_token") or ""), str(b.get("code_requirement") or ""),
                str(b.get("window_id") or ""), str(b.get("host_instance_id") or ""),
                b.get("host_pid"), str(b.get("session_id") or ""), b.get("ttl_seconds"))
            _log("原生委托 %s → %s(session=%s)" % (b.get("app_id"), out["lease"].get("id"),
                                                  b.get("session_id")))
            return _ok(h, out, 201)

        if path == PREFIX + "/host/desktop-target-event" and method == "POST":
            _host(h); b = _body(h)
            return _ok(h, {"target": rt.desktop_target_event(
                str(b.get("lease_id") or ""), str(b.get("target_generation") or ""),
                str(b.get("host_instance_id") or ""), str(b.get("event") or ""))})

        if path == PREFIX + "/host/desktop-target-status" and method == "GET":
            _host(h)
            params = urllib.parse.parse_qs(query)
            return _ok(h, rt.desktop_target_status(
                (params.get("lease_id") or [""])[0],
                (params.get("target_generation") or [""])[0],
                (params.get("host_instance_id") or [""])[0]))

        if path == PREFIX + "/host/desktop-next" and method == "GET":
            _host(h)
            params = urllib.parse.parse_qs(query)
            return _ok(h, {"work": rt.desktop_next_operation(
                (params.get("app_id") or [""])[0],
                (params.get("host_instance_id") or [""])[0])})

        if path == PREFIX + "/host/desktop-result" and method == "POST":
            _host(h); b = _body(h)
            return _ok(h, rt.desktop_complete(
                str(b.get("operation_id") or ""), str(b.get("dispatch_id") or ""),
                str(b.get("host_instance_id") or ""), str(b.get("target_generation") or ""),
                b.get("interaction_epoch"), bool(b.get("ok")), b.get("output"),
                str(b.get("error") or "")))

        if path == PREFIX + "/host/desktop-tickets" and method == "POST":
            _host(h); b = _body(h)
            return _ok(h, rt.issue_desktop_capability(
                str(b.get("app_id") or ""), str(b.get("bundle_id") or ""), b.get("pid"),
                str(b.get("window_id") or ""), b.get("allowed_actions") or [],
                str(b.get("session_id") or ""), str(b.get("task_id") or ""),
                b.get("ttl_seconds", 300), "native_user"), 201)

        if path == PREFIX + "/host/delegate" and method == "POST":
            _host(h); b = _body(h)
            lease = rt.offer(str(b.get("app_id") or ""), str(b.get("resource_id") or ""),
                             str(b.get("window_id") or ""), b.get("ttl_seconds", 300),
                             str(b.get("session_id") or ""))
            _log("委托 %s/%s → %s(session=%s)" % (b.get("app_id"), b.get("resource_id"),
                                                    lease.get("id"), b.get("session_id") or "任意"))
            return _ok(h, lease, 201)

        if path == PREFIX + "/host/pause" and method == "POST":
            _host(h); b = _body(h)
            return _ok(h, rt.pause(str(b.get("lease_id") or ""),
                                   str(b.get("reason") or "user_interaction")))

        if path == PREFIX + "/host/resume" and method == "POST":
            _host(h); b = _body(h)
            return _ok(h, rt.resume(str(b.get("lease_id") or ""),
                                    b.get("ttl_seconds", 900)))

        if path == PREFIX + "/host/grant" and method == "POST":
            _host(h); b = _body(h)
            return _ok(h, rt.grant_site(str(b.get("app_id") or "browser"),
                                        str(b.get("origin") or ""),
                                        str(b.get("scope_key") or ""),
                                        b.get("ttl_seconds", 86400)), 201)

        if path == PREFIX + "/host/takeover" and method == "POST":
            _host(h); b = _body(h)
            return _ok(h, rt.revoke(str(b.get("lease_id") or ""),
                                    str(b.get("reason") or "user_takeover")))

        if path == PREFIX + "/host/approve" and method == "POST":
            _host(h); b = _body(h)
            return _ok(h, rt.approve(str(b.get("operation_id") or ""),
                                     bool(b.get("approved")), "native_user"))

        if path == PREFIX + "/host/next" and method == "GET":
            _host(h)
            params = urllib.parse.parse_qs(query)
            app_id = (params.get("app_id") or [""])[0]
            host = rt.host_seen(app_id)
            return _ok(h, {"host": host, "operation": rt.next_operation(
                app_id,
                (params.get("resource_id") or [""])[0])})

        if path == PREFIX + "/host/pending-approval" and method == "GET":
            _host(h)
            params = urllib.parse.parse_qs(query)
            return _ok(h, {"operation": rt.pending_approval(
                (params.get("app_id") or [""])[0],
                (params.get("resource_id") or [""])[0])})

        if path == PREFIX + "/host/result" and method == "POST":
            _host(h); b = _body(h)
            return _ok(h, rt.complete(str(b.get("operation_id") or ""), bool(b.get("ok")),
                                      b.get("output"), str(b.get("error") or ""),
                                      str(b.get("observation_version") or ""),
                                      str(b.get("dispatch_id") or "")))

        return _ok(h, {"error": "unknown app-control route"}, 404)
    except AppControlError as exc:
        if path.startswith(PREFIX + "/host/"):
            _log("宿主调用 %s 失败:%s" % (path[len(PREFIX):], exc))
        return _ok(h, {"error": str(exc)}, exc.code)
    except (TypeError, ValueError) as exc:
        return _ok(h, {"error": "参数非法:%s" % str(exc)}, 400)
