"""会话名称写入服务。

HTTP、CLI 与后续其他入口都必须经过这里，不能直接改 custom-titles.json：这里同时维护
公开／原生 ID 别名、适配器内的原生名称同步状态，并在成功落盘后发布刷新事件。
"""
from term import _term_set_title


def session_identity(owner, sid):
    """返回适配器公开 ID，以及本会话所有已知 ID。"""
    index = getattr(owner, "index", {})
    if isinstance(index, dict):
        for key, row in list(index.items()):
            if isinstance(row, dict) and (key == sid or row.get("engine_sid") == sid):
                native = row.get("engine_sid")
                return key, tuple(dict.fromkeys(
                    [sid, key] + ([native] if isinstance(native, str) and native else [])))
    return sid, (sid,)


def rename_session(owner, sid, title):
    """原子写入 Desk 名称，并把原生引擎名称同步标记为待发送。"""
    own_sid, identities = session_identity(owner, sid)
    index = getattr(owner, "index", {})
    row = index.get(own_sid) if isinstance(index, dict) else None
    native = row.get("engine_sid") if isinstance(row, dict) else None
    normalized = title.strip()[:80]
    _term_set_title(native or own_sid, normalized, aliases=identities)
    if owner is not None:
        with owner._lock:
            entry = owner.index.get(own_sid)
            if entry is not None:
                entry.setdefault("desk_name_fallback", {
                    "title": entry.get("title") or "新会话",
                    "title_user": bool(entry.get("title_user"))})
                entry["name_sync"] = {"state": "pending", "title": normalized}
                owner._save_index()
        owner.bus.publish({"type": "session.updated", "properties": {"sessionID": own_sid}})
    return {"ok": True, "session_id": own_sid, "title": normalized}
