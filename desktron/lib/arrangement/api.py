"""安排管理面：声明、测试、启停、立即运行与运行记录。"""
import json
import urllib.parse

from deskcfg import REPO_ROOT, UI_PORT
from larkbot import peers
from larkbot import store as agent_store

from . import scheduler, store


PREFIX = "/workbench/arrangements"


def owns(p, method):
    return p == PREFIX or p.startswith(PREFIX + "/")


def _body(h):
    try:
        n = int(h.headers.get("Content-Length", 0))
        data = json.loads(h.rfile.read(n) or b"{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _me():
    try:
        import remote_auth
        return "server" if remote_auth.role() == "remote" else "desktop"
    except Exception:
        return "desktop"


def _query(q, key):
    return (urllib.parse.parse_qs(q or "").get(key) or [""])[0].strip()


def _forward(h, p, q, method):
    want = _query(q, "backend")
    if not want or want == _me() or peers.is_hop(h.headers):
        return False
    peer, err = peers.find(want, f"http://127.0.0.1:{UI_PORT}", _me())
    if err:
        h._send_json({"ok": False, "error": err}, 404)
        return True
    body = _body(h) if method in ("POST", "PUT", "PATCH") else None
    data, err = peers.call(peer, p, method, body, timeout=360)
    if err:
        h._send_json({"ok": False, "error": f"{want}:{err}"}, 502)
    else:
        h._send_json(data if isinstance(data, dict) else {"ok": True})
    return True


def _local_list():
    return [dict(x, backend=_me()) for x in scheduler.public_items(REPO_ROOT)]


def _agent_available(aid):
    if aid == scheduler.DEFAULT_AGENT_ID:
        return True
    agent = agent_store.load(REPO_ROOT, aid)
    # 已有飞书 Bot 也可提供执行配置；绑定不会建立消息触发或外发结果。
    return bool(agent)


def handle(h, p, q, method):
    if _forward(h, p, q, method):
        return

    rest = p[len(PREFIX):].strip("/")
    if not rest and method == "GET":
        out = {"items": _local_list(), "backend": _me(), "peers": []}
        if not peers.is_hop(h.headers):
            for peer in peers.others(f"http://127.0.0.1:{UI_PORT}", _me()):
                data, err = peers.call(peer, PREFIX)
                if err or not isinstance(data, dict):
                    out["peers"].append({"name": peer["name"], "ok": False, "error": err or "返回异常"})
                    continue
                out["peers"].append({"name": peer["name"], "ok": True,
                                     "count": len(data.get("items") or [])})
                out["items"] += [dict(x, backend=peer["name"]) for x in (data.get("items") or [])]
        return h._send_json(out)

    if not rest and method == "POST":
        body = _body(h)
        try:
            decl = scheduler.declaration(body)
        except ValueError as ex:
            return h._send_json({"ok": False, "error": str(ex)}, 422)
        if not _agent_available(decl["agent_id"]):
            return h._send_json({"ok": False, "error": "当前运行端没有这个 Agent"}, 422)
        row = store.create(REPO_ROOT, decl)
        return h._send_json({"ok": True, "item": dict(row, backend=_me())})

    if rest == "test" and method == "POST":
        body = _body(h)
        try:
            decl = scheduler.declaration(dict(body, trigger={"kind": "manual"}))
        except ValueError as ex:
            return h._send_json({"ok": False, "error": str(ex)}, 422)
        if not _agent_available(decl["agent_id"]):
            return h._send_json({"ok": False, "error": "当前运行端没有这个 Agent"}, 422)
        run, err = scheduler.start_run(REPO_ROOT, decl, test=True)
        return h._send_json({"ok": not err, "run": run, "error": err}, 202 if not err else 409)

    if rest.startswith("runs/") and method == "GET":
        rid = rest.split("/", 1)[1]
        run = store.run_get(REPO_ROOT, rid)
        return h._send_json({"run": run}) if run else h._send_json({"error": "没有这次运行"}, 404)

    seg = rest.split("/") if rest else []
    aid = seg[0] if seg else ""
    action = seg[1] if len(seg) > 1 else ""
    item = store.get(REPO_ROOT, aid)
    if not item:
        return h._send_json({"ok": False, "error": "没有这项安排"}, 404)

    if not action and method == "PUT":
        body = _body(h)
        try:
            changes = scheduler.declaration(body, item)
        except ValueError as ex:
            return h._send_json({"ok": False, "error": str(ex)}, 422)
        if not _agent_available(changes["agent_id"]):
            return h._send_json({"ok": False, "error": "当前运行端没有这个 Agent"}, 422)
        row = store.update(REPO_ROOT, aid, changes)
        return h._send_json({"ok": True, "item": dict(row, backend=_me())})

    if not action and method == "DELETE":
        return h._send_json({"ok": store.delete(REPO_ROOT, aid)})

    if action == "run" and method == "POST":
        run, err = scheduler.start_run(REPO_ROOT, item)
        return h._send_json({"ok": not err, "run": run, "error": err}, 202 if not err else 409)

    if action == "toggle" and method == "POST":
        enabled = not bool(item.get("enabled", True))
        due = scheduler.next_at(item.get("trigger")) if enabled else 0
        if enabled and item.get("trigger", {}).get("kind") == "once" and not due:
            return h._send_json({"ok": False, "error": "单次执行时间已过，请先修改执行日期与时间"}, 422)
        if enabled and item.get("trigger", {}).get("kind") == "custom" and not due:
            return h._send_json({"ok": False, "error": "重复日程已结束，请先修改日期范围或重复规则"}, 422)
        row = store.update(REPO_ROOT, aid, {"enabled": enabled,
                                            "next_at": due})
        return h._send_json({"ok": True, "item": dict(row, backend=_me())})

    return h._send_json({"error": "method"}, 405)
