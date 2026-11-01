"""Desk 备忘录应用(notepad)的文件持久化:全局单店 .workbench/notepad.json。
原先按工作流各存一份,程序坞打开的备忘录看不到任务备忘(实例错位);改全局一份,
文件夹 = 工作流名(任务备忘落对应文件夹),备忘录 App/工作流页/agent 三处同一份数据。
结构与前端 localStorage 同形:{notes:[{id,content,updated,folder?,deleted?,deletedAt?}],
folders:[文件夹名], activeId}。任务备忘 id 存任务行注记 note:。
agent 读写路径:$REPO_ROOT/.workbench/notepad.json(工作台根,任一会话可达)。"""
import datetime as _dt
import json
import os

TASK_TEMPLATE = "\n\n做什么:\n\n进展:\n\n数据:\n\n结论:\n"


def _path(repo_root):
    return os.path.join(repo_root, ".workbench", "notepad.json")


def load(repo_root):
    empty = {"notes": [], "folders": [], "activeId": None}
    try:
        with open(_path(repo_root), encoding="utf-8") as f:
            d = json.load(f)
        assert isinstance(d, dict) and isinstance(d.get("notes"), list)
        return {"notes": d["notes"],
                "folders": d.get("folders") if isinstance(d.get("folders"), list) else [],
                "activeId": d.get("activeId")}
    except Exception:
        return empty


def save(repo_root, doc):
    """整份落盘(前端/任务备忘共用)。返回错误串,'' 为成功。
    防空覆盖:空 notes 盖非空存储必须显式带 allowEmpty(用户真删空才带)——
    多开的空实例整份回写曾经清掉过用户数据(2026-07-15),这里是最后一道闸。"""
    if not (isinstance(doc, dict) and isinstance(doc.get("notes"), list)):
        return "文档结构非法(需要 {notes:[...]})"
    if not doc["notes"] and not doc.get("allowEmpty"):
        cur = load(repo_root)
        if cur["notes"]:
            return "拒绝空覆盖:存储中尚有 %d 篇备忘(删空需显式确认)" % len(cur["notes"])
    clean = {"notes": doc["notes"],
             "folders": doc.get("folders") if isinstance(doc.get("folders"), list) else [],
             "activeId": doc.get("activeId")}
    p = _path(repo_root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False)
    os.replace(tmp, p)
    return ""


def get_note(repo_root, note_id):
    if not note_id:
        return None
    for n in load(repo_root)["notes"]:
        if n.get("id") == note_id and not n.get("deleted"):
            return n
    return None


def set_note_content(repo_root, note_id, content):
    """整体更新一篇备忘的正文(工作流页就地编辑走这里),刷新 updated。返回错误串。"""
    doc = load(repo_root)
    for n in doc["notes"]:
        if n.get("id") == note_id and not n.get("deleted"):
            n["content"] = str(content or "")
            n["updated"] = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
            return save(repo_root, doc)
    return "备忘不存在(可能已在备忘录里删除)"


def rename_folder(repo_root, old, new):
    """工作流改名时同步改备忘文件夹(文件夹名 = 工作流名是散备忘归属的匹配键,不改会断蒸馏)。
    幂等;new 已存在则合并进去。返回迁移的备忘数。"""
    old, new = str(old or "").strip(), str(new or "").strip()
    if not old or not new or old == new:
        return 0
    doc = load(repo_root)
    if old not in doc["folders"] and not any(n.get("folder") == old for n in doc["notes"]):
        return 0
    doc["folders"] = [f for f in doc["folders"] if f != old]
    if new not in doc["folders"]:
        doc["folders"].append(new)
    moved = 0
    for n in doc["notes"]:
        if n.get("folder") == old:
            n["folder"] = new
            moved += 1
    err = save(repo_root, doc)
    if err:
        raise RuntimeError(err)
    return moved


def ensure_task_note(repo_root, folder, title):
    """给任务建一篇备忘(文件夹 = 工作流名,首行 = 任务标题 + 模板),返回备忘 id。"""
    doc = load(repo_root)
    folder = str(folder or "任务").strip() or "任务"
    if folder not in doc["folders"]:
        doc["folders"].append(folder)
    nid = "%08x" % int.from_bytes(os.urandom(4), "big")
    note = {"id": nid,
            "content": str(title or "任务备忘").strip() + TASK_TEMPLATE,
            "updated": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "folder": folder}
    doc["notes"].insert(0, note)
    doc["activeId"] = nid
    err = save(repo_root, doc)
    if err:
        raise RuntimeError(err)
    return nid
