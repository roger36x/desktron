"""常驻记忆引擎、驻留资源与 Desk 后端状态，只读聚合。"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import json
import os
import re
import time

DREAM_INTERVAL_DEFAULT = 1800   # memory_engine.scheduler 的默认 tick 间隔(秒)


def _read(p):
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _read_json(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, type(default)) else default
    except Exception:
        return default


def memory_engine_status(repo_root, data_home=None):
    """记忆引擎:调度器随 ui_server 常驻(running 恒真),细节从 dream 落盘痕读。"""
    # sidecar 目录 2026-08-13 随引擎更名从 .kbkit/ 改到 .curator/;这里是只读聚合,
    # 不触发迁移,新目录不在而旧目录还在时照读旧的。
    mem_root = os.path.join(repo_root, ".memory")
    dream = os.path.join(mem_root, ".curator", "dream")
    if not os.path.isdir(dream) and os.path.isdir(os.path.join(mem_root, ".kbkit", "dream")):
        dream = os.path.join(mem_root, ".kbkit", "dream")
    out = {"id": "memory-engine", "running": True, "interval_s": _dream_interval(),
           "recap": _recap_status(data_home)}
    m = re.match(r"(\d+)\s+candidates=(\d+)", _read(os.path.join(dream, "last-run")).strip())
    if m:
        out["last_run"] = int(m.group(1))
        out["candidates"] = int(m.group(2))
        out["next_run"] = out["last_run"] + out["interval_s"]
    pm = _read_json(os.path.join(mem_root, ".curator", "project-memory", "engine-state.json"), {})
    p_fails = int(pm.get("fails") or 0)
    p_done = pm.get("last_run_ws") if isinstance(pm.get("last_run_ws"), dict) else {}
    p_err = pm.get("last_error") if isinstance(pm.get("last_error"), dict) else {}
    out["project_memory"] = {
        "fails": p_fails,
        "stopped": p_fails >= 3,
        "ws_done": len(p_done),
        "last_ok": int(pm.get("last_ok") or 0),
        "last_error": {"ts": int(p_err.get("ts") or 0),
                       "reason": str(p_err.get("reason") or ""),
                       "detail": str(p_err.get("detail") or "")[:200]} if p_err else {},
    }
    report = _read(os.path.join(dream, "latest.md"))
    out["report_tail"] = [l for l in report.splitlines() if l.strip()][-12:]
    out["project_memory_pending"] = _project_memory_pending(report)
    out["sot_nominations"] = _sot_nominations(report)
    return out


def _recap_status(data_home):
    """真实 recap 收据；没有跑过与失败必须分开。

    2026-09 起 GUI 会话的公开收口进入 Shepherd 统一层,旧 claude/codex/pi
    adapter 目录仍保留历史收据。若继续把旧目录的一次失败算成当前故障,Agents 页会被
    陈旧的断流永久顶成「需处理」,而实际 Shepherd recap 与项目记忆已经正常。
    """
    engines = {}
    last_ok = last_write = pending = 0
    failed = []
    legacy_failed = []
    names = ["shepherd", "claude", "codex", "pi"]
    for name in names:
        root = os.path.join(data_home, "adapter", name) if data_home else ""
        state = _read_json(os.path.join(root, "recap-state.json"), {}) if root else {}
        requests = os.path.join(root, "recap-requests") if root else ""
        exists = bool(state) or os.path.isdir(requests)
        if name == "shepherd" and not exists:
            continue
        err = state.get("last_error") if isinstance(state.get("last_error"), dict) else {}
        try:
            waiting = len([x for x in os.listdir(requests) if x.endswith(".json")])
        except OSError:
            waiting = 0
        row = {"last_ok": int(state.get("last_ok") or 0),
               "last_write": int(state.get("last_write") or 0),
               "last_result": str(state.get("last_result") or ""),
               "fails": int(state.get("fails") or 0), "pending": waiting,
               # 被新回合抢占的次数。不是故障,但要看得见:光看 last_ok 分不出
               # 「没跑过」与「每次都被抢占」(2026-09-01 后者藏了一整天)。
               "cancels": int(state.get("cancels") or 0),
               "last_cancel": int(state.get("last_cancel") or 0),
               "last_error": {"ts": int(err.get("ts") or 0),
                              "detail": str(err.get("detail") or "")[:200]} if err else {}}
        engines[name] = row
        last_ok = max(last_ok, row["last_ok"])
        last_write = max(last_write, row["last_write"])
        pending += waiting
        if row["fails"]:
            if name == "shepherd" or "shepherd" not in engines:
                failed.append(name)
            else:
                legacy_failed.append(name)
    return {"last_ok": last_ok, "last_write": last_write, "pending": pending,
            "failed_engines": failed, "legacy_failed_engines": legacy_failed,
            "engines": engines}


def _sot_nominations(report):
    """报告里的「SOT 合入提名」条目。

    sot 是四层里唯一要人审的一层,提名此前只落在 latest.md 里,既不进上下文也不进面板,
    人得先想起来去看那份文件——sot 因此长期只有一条。抽成结构化字段供 Agents 页直接显示。"""
    out, in_sec = [], False
    for line in report.splitlines():
        if line.startswith("- ") and "SOT 合入提名" in line:
            in_sec = True
            continue
        if in_sec:
            if line.startswith("  - "):
                out.append(line[4:].strip())
                continue
            in_sec = False
    return out


def _dream_interval():
    for k in ("MEMORY_ENGINE_DREAM_INTERVAL", "DESKTRON_DREAM_INTERVAL"):
        v = os.environ.get(k)
        if v:
            try:
                return max(300, int(v))
            except ValueError:
                pass
    return DREAM_INTERVAL_DEFAULT


def _project_memory_pending(report):
    """latest.md 的待刷新工作区数。"""
    for line in report.splitlines():
        if line.startswith("- 待刷新："):
            return len([x for x in line.split("：", 1)[1].split("、") if x.strip()])
    return 0


def desk_self_status():
    """比较后端进程启动时间与已加载源码的修改时间，不将前端文件算入。"""
    lib = os.path.dirname(os.path.abspath(__file__))
    started = oscompat.process_started_at(os.getpid())
    if started is None:
        return {"stale_code": False}
    newest = 0.0
    for root, dirs, files in os.walk(lib):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if not f.endswith(".py"):
                continue
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                pass
    # 留 2 秒余量:装机/同步那一刻文件与进程几乎同时,差个零点几秒不算旧
    return {"stale_code": newest > started + 2, "started": int(started), "newest": int(newest)}


def resident_status():
    """常驻引擎进程占了多少内存、离预算还有多远(见 lib/resident.py)。

    这一项必须看得见:闸从「按个数」改成「按内存」之后,它什么时候在收、收了谁,
    不报出来就只能靠事后看进程表猜。2026-08-23 那次 5.73GB 正是因为没人看总量。
    """
    try:
        import resident
        return resident.snapshot()
    except Exception as exc:
        return {"error": str(exc)[:120]}


def agents_status(repo_root):
    try:
        from deskcfg import DATA_HOME
        data_home = DATA_HOME
    except Exception:
        data_home = None
    return {"ts": int(time.time()), "memory": memory_engine_status(repo_root, data_home=data_home),
            "resident": resident_status(),
            "desk": desk_self_status()}
