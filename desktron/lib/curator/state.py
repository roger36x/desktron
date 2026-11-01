"""运行态 sidecar:访问计数 + 生命周期事件。

放 `.curator/`(gitignored,不上远端)。高频写(访问)与跨 band 的事件不进 frontmatter,
避免污染 git 内桶的 diff。curator 只存数据 + 提供接口;强制 agent 走读接口由 harness 侧约束。

- access.json:{id: {count, last_access}}。show 等读接口每次 +1。
- events.jsonl:追加式生命周期事件(created / promoted / verified / removed ...)。
"""

from __future__ import annotations

import json
import os

from .util import atomic_write, today

STATE_DIR = ".curator"


def resolve_dir(root: str) -> str:
    """读取侧解析 sidecar 目录。

    2026-08-24 之前这里还兼容旧名 `.kbkit/`(2026-08-13 引擎由 kbkit 更名 curator 时的遗留),
    带一次性迁移与只读盘回落。2026-08-25 撤除:本机全部实例已迁完,且此后只有单一提交者、
    不存在别的安装会带着旧目录进来。迁移期加的埋点(state_dir_migrated)一并撤掉——它要观测的
    对象已经不存在。历史实现见 archive/kbkit-compat-removal-20260825-213511/state.py。
    """
    return os.path.join(root, STATE_DIR)


def _dir(inst) -> str:
    return resolve_dir(inst.root)


def _access_path(inst) -> str:
    return os.path.join(_dir(inst), "access.json")


def _events_path(inst) -> str:
    return os.path.join(_dir(inst), "events.jsonl")


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return default


# 读取方(caller)口径:只有 task(为回答用户任务而读)计入 access 热度;
# engine-internal(提取引擎候选收集/自检等机器自读)与 audit(审计读)不计数、只落 trace 事件。
# 热度是 GC/promote 的信号,机器自读计入即自污染(信号与噪声混)。
ACCESS_CALLERS = ("task", "engine-internal", "audit")


def record_access(inst, note_id: str, caller: str = "task") -> None:
    if not note_id:
        return
    if caller not in ACCESS_CALLERS:
        caller = "task"
    if caller != "task":
        record_event(inst, note_id, "accessed", caller=caller)
        return
    data = _load_json(_access_path(inst), {})
    cur = data.get(note_id) or {"count": 0}
    cur["count"] = int(cur.get("count", 0)) + 1
    cur["last_access"] = today()
    data[note_id] = cur
    atomic_write(_access_path(inst), json.dumps(data, ensure_ascii=False, indent=0))


def read_access(inst, note_id: str = None):
    data = _load_json(_access_path(inst), {})
    return data.get(note_id) if note_id else data


def record_event(inst, note_id: str, event: str, **details) -> None:
    if not note_id:
        return
    os.makedirs(_dir(inst), exist_ok=True)
    rec = {"id": note_id, "event": event, "date": today()}
    rec.update({k: v for k, v in details.items() if v is not None})
    with open(_events_path(inst), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_events(inst, note_id: str = None) -> list:
    path = _events_path(inst)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if note_id is None or rec.get("id") == note_id:
                out.append(rec)
    return out
