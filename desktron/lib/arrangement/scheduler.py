"""轻量时间调度与单轮执行；Desk 是默认身份，已有 Agent 可选。"""
import datetime as dt
import calendar
import re
import threading
import time

from larkbot import context, runner
from larkbot import store as agent_store

from . import store


KINDS = {"manual", "once", "interval", "daily", "weekdays", "weekly", "custom"}
DEFAULT_AGENT_ID = "_desk"
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_STARTED = False
_START_LOCK = threading.Lock()
_RUNNING = set()
_RUN_LOCK = threading.Lock()
_SLOTS = threading.Semaphore(2)


def _desk_log(area, message):
    """deskcfg 依赖 ui_server 的 argv 契约；测试与离线工具里没有时静默退化。"""
    try:
        import deskcfg
        deskcfg.log(area, message)
    except Exception:
        pass


def _schedule_date(value, label):
    value = str(value or "")
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError()
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(label + "需要有效日期 YYYY-MM-DD")


def normalize_trigger(raw):
    trig = dict(raw or {})
    kind = str(trig.get("kind") or "manual")
    if kind not in KINDS:
        raise ValueError("触发方式只支持 manual/once/interval/daily/weekdays/weekly/custom")
    out = {"kind": kind}
    if kind == "interval":
        minutes = str(trig.get("minutes", ""))
        if not minutes.isascii() or not minutes.isdigit() or not 1 <= int(minutes) <= 10080:
            raise ValueError("执行间隔需要是 1~10080 分钟的整数")
        out["minutes"] = int(minutes)
    if kind not in {"manual", "interval"}:
        clock = str(trig.get("time") or "09:00")
        if not _TIME_RE.match(clock):
            raise ValueError("时间需要是 HH:MM")
        out["time"] = clock
    if kind == "once":
        date = str(trig.get("date") or "")
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                raise ValueError()
            dt.date.fromisoformat(date)
        except ValueError:
            raise ValueError("单次安排需要有效日期 YYYY-MM-DD")
        out["date"] = date
    if kind == "weekly":
        try:
            weekday = int(trig.get("weekday"))
        except Exception:
            raise ValueError("每周安排需要星期")
        if weekday < 0 or weekday > 6:
            raise ValueError("星期需要是 0~6")
        out["weekday"] = weekday
    if kind == "custom":
        unit = str(trig.get("unit") or "")
        if unit not in {"day", "week", "month"}:
            raise ValueError("重复单位需要是 day/week/month")
        every = str(trig.get("every", ""))
        if not every.isascii() or not every.isdigit() or not 1 <= int(every) <= 999:
            raise ValueError("重复频率需要是 1~999 的整数")
        start = _schedule_date(trig.get("start_date"), "起始日期")
        out.update(unit=unit, every=int(every), start_date=start.isoformat())
        if unit == "week":
            days = trig.get("weekdays")
            if (not isinstance(days, list) or not days
                    or any(type(day) is not int or not 0 <= day <= 6 for day in days)):
                raise ValueError("每周重复至少选择一天，星期需要是 0~6")
            out["weekdays"] = sorted(set(days))
        if trig.get("end_date"):
            end = _schedule_date(trig["end_date"], "截止日期")
            if end < start:
                raise ValueError("截止日期不能早于起始日期")
            out["end_date"] = end.isoformat()
    return out


def _next_custom(trig, after):
    start = dt.date.fromisoformat(trig["start_date"])
    end = dt.date.fromisoformat(trig["end_date"]) if trig.get("end_date") else dt.date.max
    today = dt.datetime.fromtimestamp(after).date()
    if today > end:
        return 0
    every, unit = trig["every"], trig["unit"]
    clock = dt.time.fromisoformat(trig["time"])
    # 直接跳到当前周期，不逐日扫描，也不以恢复/重启时间重置重复周期。
    if unit == "day":
        period = max(0, (today - start).days // every)
    elif unit == "week":
        anchor = start - dt.timedelta(days=start.weekday())  # 周一为周期起点
        period = max(0, (today - anchor).days // (7 * every))
    else:
        period = max(0, ((today.year - start.year) * 12 + today.month - start.month) // every)
    for index in range(period, period + 3):
        try:
            if unit == "day":
                days = [start + dt.timedelta(days=index * every)]
            elif unit == "week":
                week = anchor + dt.timedelta(weeks=index * every)
                days = [week + dt.timedelta(days=day) for day in trig["weekdays"]]
            else:
                month = start.year * 12 + start.month - 1 + index * every
                year, month = divmod(month, 12)
                month += 1
                # 月末规则：1 月 31 日 → 2 月末 → 3 月 31 日，不累计漂移。
                days = [dt.date(year, month, min(start.day, calendar.monthrange(year, month)[1]))]
        except (OverflowError, ValueError):
            return 0
        for day in days:
            if day < start:
                continue
            if day > end:
                return 0
            candidate = dt.datetime.combine(day, clock).timestamp()
            if candidate > after:
                return int(candidate)
    return 0


def next_at(trigger, after=None):
    trig = normalize_trigger(trigger)
    if trig["kind"] == "manual":
        return 0
    after = float(after if after is not None else time.time())
    if trig["kind"] == "custom":
        return _next_custom(trig, after)
    if trig["kind"] == "interval":
        return int(after) + trig["minutes"] * 60
    if trig["kind"] == "once":
        candidate = dt.datetime.fromisoformat(trig["date"] + "T" + trig["time"]).timestamp()
        return int(candidate) if candidate > after else 0
    # 用本地 naive datetime 再交给 timestamp() 解释。若跨过夏令时边界，它会按候选日
    # 的本地时区规则换算；把 base.tzinfo 直接钉过去只会复制“今天的固定偏移”。
    base = dt.datetime.fromtimestamp(after)
    hour, minute = [int(x) for x in trig["time"].split(":")]
    for offset in range(9):
        day = base.date() + dt.timedelta(days=offset)
        if trig["kind"] == "weekdays" and day.weekday() > 4:
            continue
        if trig["kind"] == "weekly" and day.weekday() != trig["weekday"]:
            continue
        candidate = dt.datetime.combine(day, dt.time(hour, minute))
        if candidate.timestamp() > after + 0.5:
            return int(candidate.timestamp())
    return 0


def declaration(body, existing=None):
    body = dict(body or {})
    old = dict(existing or {})
    instruction = str(body.get("instruction", old.get("instruction") or "")).strip()
    if not instruction:
        raise ValueError("请写清每次要做什么")
    if len(instruction) > 8000:
        raise ValueError("运行指令不能超过 8000 字")
    # 安排本身只需要一个可执行引擎。没有显式 Agent 时使用 Desk 通用身份；飞书 Bot
    # 是可选的上下文模板与触发入口，不能成为创建定时任务的前置条件。
    agent_id = str(body.get("agent_id", old.get("agent_id") or DEFAULT_AGENT_ID)).strip()
    if not agent_id:
        agent_id = DEFAULT_AGENT_ID
    name = str(body.get("name", old.get("name") or "")).strip()
    if not name:
        name = re.split(r"[，。,.\n]", instruction, maxsplit=1)[0].strip()[:40] or "新安排"
    if len(name) > 80:
        raise ValueError("名称不能超过 80 字")
    trigger = normalize_trigger(body.get("trigger", old.get("trigger") or {}))
    enabled = bool(body.get("enabled", old.get("enabled", True)))
    session_id = str(body.get("session_id", old.get("session_id") or "")).strip()
    if len(session_id) > 160:
        raise ValueError("会话标识不能超过 160 字")
    # 修改指令或绑定身份不推迟下一次运行，也不让已领取的单次安排再次入队。
    unchanged = trigger == old.get("trigger") and enabled == old.get("enabled", True)
    due = old["next_at"] if unchanged and "next_at" in old else next_at(trigger)
    if trigger["kind"] == "once" and not unchanged and not due:
        raise ValueError("单次执行时间必须晚于当前时间（按运行端本地时间）")
    if trigger["kind"] == "custom" and enabled and not unchanged and not due:
        raise ValueError("重复规则范围内已没有未来执行时间，请调整日期或星期")
    return {"name": name, "agent_id": agent_id, "instruction": instruction,
            "session_id": session_id,
            "trigger": trigger, "enabled": enabled,
            "next_at": due if enabled else 0}


def public_items(root):
    latest = {}
    for run in store.runs(root):
        aid = run.get("arrangement_id")
        if aid and aid not in latest and not run.get("test"):
            latest[aid] = run
    out = []
    for item in store.list_all(root):
        row = dict(item)
        run = latest.get(row.get("id"))
        if run:
            row["last_run"] = {k: run.get(k) for k in
                               ("id", "status", "started_at", "finished_at", "error", "output", "used")}
            # 清单只承担概览。完整结果由 /runs/<id> 按需读取，避免一次打开“安排”
            # 就把各端最近一次的长输出全部跨隧道搬回来。
            row["last_run"]["output"] = str(row["last_run"].get("output") or "")[:1200]
        out.append(row)
    return out


def resolve_agent(root, aid):
    """把安排的执行身份解析成 runner 配置；默认身份不依赖飞书 Bot 声明。"""
    if not aid or aid == DEFAULT_AGENT_ID:
        try:
            from workbench import _engine_pref
            engine = _engine_pref()
        except Exception:
            engine = "claude"
        return {
            "id": DEFAULT_AGENT_ID,
            "name": "Desktron",
            "engine": engine,
            "cwd": root,
            "enabled": True,
            # 不注入 larkbot.context 里的飞书交互身份；记忆与知识仍按任务检索。
            "context": {"routing": False, "memory": True, "knowledge": True,
                        "history": 0, "memory_max": 20, "knowledge_max": 10},
        }
    return agent_store.load(root, aid)


def _execute(root, run_row, decl, running_key):
    rid = run_row["id"]
    aid = decl.get("agent_id") or ""
    try:
        with _SLOTS:
            store.run_update(root, rid, {"status": "running", "started_at": int(time.time())})
            agent = resolve_agent(root, aid)
            if not agent:
                error, output, used = f"找不到 Agent {aid}", "", {}
            elif not agent.get("enabled", True):
                error, output, used = f"Agent {aid} 已停用", "", {}
            else:
                try:
                    prompt, used = context.build(root, agent, decl.get("instruction") or "",
                                                 "arrangement:" + str(decl.get("id") or rid), history=[])
                    output, error = runner.run(agent.get("engine") or "claude", prompt,
                                               cwd=agent.get("cwd") or root,
                                               model=agent.get("model") or "", log=_desk_log,
                                               performance=(agent.get("runtime") or {}).get(
                                                   "performance") or "fast")
                except Exception as ex:
                    output, error, used = "", str(ex)[:400], {}
            # 超时可能已产生部分 stdout；有输出不代表运行完成，runner 给了 error 就如实失败。
            status = "failed" if error else "succeeded"
            changes = {"status": status, "finished_at": int(time.time()),
                       "output": str(output or "")[:50000], "error": str(error or "")[:1000],
                       "used": used or {}}
            store.run_update(root, rid, changes)
            if decl.get("id"):
                store.update(root, decl["id"], {"last_run_id": rid,
                                                 "last_status": status,
                                                 "last_finished_at": changes["finished_at"]})
            _desk_log("arrangements", f"{decl.get('name') or rid} {status}"
                      + (f":{changes['error'][:120]}" if changes["error"] else ""))
    finally:
        with _RUN_LOCK:
            _RUNNING.discard(running_key)
        import lifecycle
        lifecycle.finish(critical="arrangement")


def start_run(root, decl, test=False):
    key = str(decl.get("id") or "test:" + str(time.time_ns()))
    import self_update
    with self_update.admit_work() as admitted:
        if not admitted:
            return None, "Desk 正在准备强制更新，暂不启动新安排"
        with _RUN_LOCK:
            if not test and key in _RUNNING:
                return None, "这项安排正在运行"
            import lifecycle
            if not lifecycle.begin(critical="arrangement"):
                return None, "Desk 正在重启，暂不启动新安排"
            _RUNNING.add(key)
        try:
            row = store.run_create(root, {
                "arrangement_id": decl.get("id") or "", "arrangement_name": decl.get("name") or "测试安排",
                "agent_id": decl.get("agent_id") or "", "test": bool(test), "status": "queued",
            })
            threading.Thread(target=_execute, args=(root, row, dict(decl), key), daemon=True,
                             name="arrangement-" + row["id"][-8:]).start()
        except Exception:
            with _RUN_LOCK:
                _RUNNING.discard(key)
            lifecycle.finish(critical="arrangement")
            raise
    return row, ""


def has_running():
    with _RUN_LOCK:
        return bool(_RUNNING)


def tick(root, now=None):
    now = int(now if now is not None else time.time())
    import self_update
    with self_update.admit_work() as admitted:
        if not admitted:
            return 0
        claimed = store.claim_due(root, now, next_at)
        # 机器离线跨过截止日期后，不补跑已过期的重复安排。
        today = dt.datetime.fromtimestamp(now).date().isoformat()
        claimed = [item for item in claimed if not (
            item.get("trigger", {}).get("kind") == "custom"
            and item["trigger"].get("end_date")
            and today > item["trigger"]["end_date"])]
        for item in claimed:
            start_run(root, item)
    return len(claimed)


def start_background(root):
    global _STARTED
    with _START_LOCK:
        if _STARTED:
            return False
        _STARTED = True
    store.recover_interrupted(root)

    def loop():
        while True:
            try:
                tick(root)
            except Exception as ex:
                _desk_log("arrangements", "调度轮失败:" + str(ex)[:160])
            time.sleep(15)

    threading.Thread(target=loop, daemon=True, name="arrangements-scheduler").start()
    return True
