"""常驻系统任务状态与记忆引擎手动运行入口。"""
import json

import deskcfg
from deskcfg import REPO_ROOT


def owns(p, method):
    return p in ("/workbench/agents/status", "/workbench/agents/run-once")


def handle(h, p, q, method):
    if p == "/workbench/agents/status" and method == "GET":
        import agents_status
        return h._send_json(agents_status.agents_status(REPO_ROOT))
    if p == "/workbench/agents/run-once" and method == "POST":
        import threading
        ln = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(ln) or b"{}")
        except Exception:
            body = {}
        agent = (body.get("agent") or "").strip()
        if agent != "memory":
            return h._send_json({"ok": False, "error": "agent 必须是 memory"}, 422)

        def _mem_once():
            try:
                import memory_scheduler
                memory_scheduler.dream_tick()
            except Exception as exc:
                deskcfg.log("agents", f"记忆引擎手动运行失败:{str(exc)[:160]}")

        threading.Thread(target=_mem_once, daemon=True, name="agents-mem-once").start()
        return h._send_json({"ok": True, "started": True})
    return h._send_json({"error": "unknown"}, 404)
