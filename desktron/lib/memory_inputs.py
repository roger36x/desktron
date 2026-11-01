"""记忆引擎的 Desk 侧输入适配器。

候选检测(机械,免模型):各工作流任务行的 note: 注记 + 工作流文件夹下的散备忘,
指向全局备忘录(.workbench/notepad.json);内容哈希与上次吸收记录比对,有变化且编辑停当
(updated 距今超 STABLE_S)才进候选,不蒸写到一半的备忘。

本模块不运行 agent,只保留两件 Desk 侧的事:候选检测(notepad / TASKS 是 Desk 数据,
引擎不认识)与项目模型吸收后的 hash 记账,经 Host 的 memos /
memos_done 注入给引擎。

env:DESKTRON_PROJECT_MEMORY_MEMOS=off 关(候选恒空,等于关掉备忘这路输入)。
"""
import hashlib
import json
import os
import re
import time

STABLE_S = 30 * 60     # 编辑停当线:备忘 updated 距今超过它才蒸
# STABLE_S 之外的频控、单轮上限与熔断归统一记忆引擎，本模块不再重复定义。

_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)\s]*\)")


def _env(name, default=""):
    return os.environ.get("DESKTRON_" + name, default)


def enabled():
    return _env("PROJECT_MEMORY_MEMOS").lower() not in ("off", "0", "false")


def _state_path(repo_root):
    # sidecar 目录 2026-08-13 随引擎更名从 .kbkit/ 改到 .curator/,经引擎的解析函数拿
    # (它负责一次性迁移与旧目录回落);引擎不可用时退回硬编码新名。
    try:
        from memory_engine.layout import state_dir
        d = state_dir(os.path.join(repo_root, ".memory"), "dream")
    except Exception:
        d = os.path.join(repo_root, ".memory", ".curator", "dream")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "project-memory-inputs.json")


def _load_state(p):
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_state(p, st):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _note_ts(n):
    """updated ISO → epoch;解析不了按 0(视为很旧,可吸收)。"""
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(n.get("updated") or "").replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def collect_candidates(repo_root):
    """{ws: [{nid, task, content}]}:有变化且停当的备忘。ws=工作流域 id(记忆键同源)。"""
    from workbench import _wb_domains_read
    import notepad_store
    import tasks as _tasks
    st = _load_state(_state_path(repo_root))
    hashes = st.get("hashes") or {}
    doc = notepad_store.load(repo_root)
    by_id = {n.get("id"): n for n in doc["notes"] if not n.get("deleted")}
    by_folder = {}
    for n in doc["notes"]:
        if not n.get("deleted") and n.get("folder"):
            by_folder.setdefault(n["folder"], []).append(n)
    now = time.time()
    out = {}
    for d in _wb_domains_read():
        if d.get("kind") in ("project", "container") or d.get("dir"):
            continue   # 只做工作流:项目不产任务备忘,容器聚合读时已有 rollup
        ws = d.get("id")
        seen, cands = set(), []
        # 任务备忘(note: 注记)带任务标题;同工作流文件夹下的散备忘也收
        try:
            items = _tasks.read_tasks(os.path.join(repo_root, "domains", ws, _tasks.TASKS_MD_NAME))
        except Exception:
            items = []
        pool = [(t.get("note"), t.get("title")) for t in items if t.get("note")]
        pool += [(n.get("id"), "") for n in by_folder.get(d.get("name") or "", [])]
        for nid, task in pool:
            n = by_id.get(nid)
            if not n or nid in seen:
                continue
            seen.add(nid)
            body = str(n.get("content") or "").strip()
            if len(body) < 40:
                continue   # 空壳/纯标题备忘,没有可吸收内容
            h = hashlib.sha1(body.encode()).hexdigest()[:16]
            if hashes.get(nid) == h:
                continue   # 上次吸收后的内容没变
            if now - _note_ts(n) < STABLE_S:
                continue   # 还在编辑,等停当
            cands.append({"nid": nid, "task": task or "", "content": body, "hash": h})
        if cands:
            out[ws] = cands
    return out


def mark_done(repo_root, ws, memos):
    """项目模型吸收一批备忘后回调:记下内容 hash,下轮不重复输入。"""
    sp = _state_path(repo_root)
    st = _load_state(sp)
    hashes = st.get("hashes") or {}
    for m in memos or []:
        if m.get("nid") and m.get("hash"):
            hashes[m["nid"]] = m["hash"]
    st["hashes"] = hashes
    st.setdefault("done", {})[ws] = int(time.time())
    _save_state(sp, st)
