"""Desk → memory-engine 的宿主适配层:把 Desk 侧候选与运行信息注入引擎 Host。

- memos:备忘候选(memory_inputs.collect_candidates;notepad / TASKS 是 Desk 数据,引擎不认识)
- log:deskcfg.log(统一 [模块] 前缀,进日志悬浮窗)

会话流水由各引擎适配器的 recap 路径直接写 journal,不再经 Host 注入离线会话源。

引擎不 import Desk;依赖方向自此单向(Desk → engine → curator)。
deskcfg 在 import 时读 sys.argv(ui_server 进程形状),故本模块也只能惰性构造。
"""
import os


def build_host():
    from deskcfg import REPO_ROOT, DATA_HOME, log
    from memory_engine.providers import Host
    from memory_store import _memory_root

    def _memos():
        import memory_inputs
        return memory_inputs.collect_candidates(REPO_ROOT) if memory_inputs.enabled() else {}

    def _memos_done(ws, memos):
        import memory_inputs
        memory_inputs.mark_done(REPO_ROOT, ws, memos)

    def _memory_candidates(ws):
        try:
            import task_ledger
            rows = task_ledger.read(DATA_HOME).get("memory_candidates", {}).values()
            return [dict(x) for x in rows if isinstance(x, dict) and x.get("workflow") == ws]
        except Exception:
            return []

    def _workspace_context(ws):
        import domains
        entry = domains._domain_entry(ws) or {}
        runtime_dir = os.path.join(REPO_ROOT, "domains", ws)
        repos = [item.get("dir") for item in domains._repo_items(ws) if item.get("dir")]
        return {
            "description": entry.get("desc") or "",
            "workspace_dir": runtime_dir if os.path.isdir(runtime_dir) else "",
            "repos": repos,
        }

    def _record_usage(kind, model, usage, outcome):
        from usage_stats import record_sidecar
        record_sidecar(DATA_HOME, kind, engine="pi", model=model, usage=usage, outcome=outcome)

    return Host(
        repo_root=REPO_ROOT,
        mem_root=_memory_root(),
        knowledge_root=os.path.join(REPO_ROOT, "knowledge"),
        memos=_memos,
        memos_done=_memos_done,
        memory_candidates=_memory_candidates,
        workspace_context=_workspace_context,
        record_usage=_record_usage,
        log=log,
    )
