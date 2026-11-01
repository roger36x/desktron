#!/usr/bin/env python3
"""专项/工作区 id 迁移工具(独立、手动触发)。

容器 id 不跟改名走:展示名可以随时改,id 是磁盘目录、.memory 目录、会话映射的稳定主键,平时不动。
需要把 id 换成语义化新值时(比如 c-test 建项目时随口起的名字,后来想换成 c-ai-native),用本脚本一次性迁移:
domains/<id> 目录、.memory/<id> 目录、domains.json 的 id 字段与其它容器 subareas 里指向旧 id 的 ref、
wbState(state.json)里 sessions/hidden/containerOrder 对旧 id 的引用。

不做 UI 入口,不处理"正在跑的会话"——执行前请自行确认 cwd 落在旧目录下的会话已关闭(目录改名后它们会失效)。

用法: python3 migrate_domain_id.py <旧id> <新id> [repo_root]
"""
import json
import os
import re
import sys


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def migrate(old_id, new_id, repo_root):
    if not re.fullmatch(r"[a-z0-9_-]+", new_id or ""):
        return {"ok": False, "warnings": [f"新 id 非法(仅 a-z0-9_-):{new_id}"], "moved": []}
    domains_json = os.path.join(repo_root, ".workbench", "domains.json")
    doms = _load_json(domains_json, [])
    entry = next((d for d in doms if d.get("id") == old_id), None)
    if entry is None:
        return {"ok": False, "warnings": [f"未知 id:{old_id}(domains.json 里找不到)"], "moved": []}
    if any(d.get("id") == new_id for d in doms):
        return {"ok": False, "warnings": [f"新 id 已存在:{new_id}"], "moved": []}

    moved = []
    old_dir = os.path.join(repo_root, "domains", old_id)
    new_dir = os.path.join(repo_root, "domains", new_id)
    if os.path.isdir(old_dir):
        os.rename(old_dir, new_dir)
        moved.append(f"domains/{old_id} -> domains/{new_id}")

    old_mem = os.path.join(repo_root, ".memory", old_id)
    new_mem = os.path.join(repo_root, ".memory", new_id)
    if os.path.isdir(old_mem):
        os.rename(old_mem, new_mem)
        moved.append(f".memory/{old_id} -> .memory/{new_id}")

    entry["id"] = new_id
    # 其它容器的子区若引用旧 id(ref/id 列),一并改写(子区清单就存在 domains.json 里)
    refs_changed = False
    for d in doms:
        if d.get("kind") != "container":
            continue
        for s in (d.get("subareas") or []):
            if s.get("ref") == old_id:
                s["ref"] = new_id
                if s.get("id") == old_id:
                    s["id"] = new_id
                refs_changed = True
    _save_json(domains_json, doms)
    moved.append("domains.json id 字段")
    if refs_changed:
        moved.append("domains.json 里其它容器的子区 ref 引用")

    state_path = os.path.join(repo_root, ".workbench", "state.json")
    st = _load_json(state_path, {})
    if isinstance(st, dict):
        touched = False
        sessions = st.get("sessions")
        if isinstance(sessions, dict):
            new_sessions = {}
            for sid, dom in sessions.items():
                if dom == old_id:
                    new_sessions[sid] = new_id
                    touched = True
                elif isinstance(dom, str) and dom.startswith(old_id + ":"):
                    new_sessions[sid] = new_id + dom[len(old_id):]
                    touched = True
                else:
                    new_sessions[sid] = dom
            st["sessions"] = new_sessions
        for key in ("hidden", "containerOrder"):
            lst = st.get(key)
            if isinstance(lst, list) and old_id in lst:
                st[key] = [new_id if x == old_id else x for x in lst]
                touched = True
        if touched:
            _save_json(state_path, st)
            moved.append("state.json 里的 sessions/hidden/containerOrder 引用")

    return {"ok": True, "warnings": [], "moved": moved}


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        print("用法: migrate_domain_id.py <旧id> <新id> [repo_root]", file=sys.stderr)
        sys.exit(2)
    old_id, new_id = sys.argv[1], sys.argv[2]
    repo_root = sys.argv[3] if len(sys.argv) == 4 else os.environ.get("DESKTRON_WORKSPACE", ".")
    print(f"即将把 {old_id} 迁移为 {new_id}(repo_root={repo_root})。")
    print("正在跑的、cwd 落在旧目录下的会话进程会因目录消失而失效——请确认已关闭。")
    result = migrate(old_id, new_id, repo_root)
    if not result["ok"]:
        print("迁移失败:", "; ".join(result["warnings"]), file=sys.stderr)
        sys.exit(1)
    print("已迁移:")
    for m in result["moved"]:
        print(" -", m)
