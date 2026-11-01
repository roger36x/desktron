"""Desk 展示元数据，不写入引擎历史。调用方持有对应消息文件锁。"""
import hashlib
import json
import os
import threading

from peer_message import MODEL_HEAD, TAIL, split_peer_message


def _parts(messages):
    for message in messages:
        if (message.get("info") or {}).get("role") != "user":
            continue
        for part in message.get("parts") or []:
            text = part.get("text")
            if part.get("type") != "text" or not isinstance(text, str) or TAIL not in text:
                continue
            normalized, legacy_head = split_peer_message(text)
            if not normalized.startswith(MODEL_HEAD + "\n\n"):
                continue
            # 完整正文包含回信会话号；绝不只按第一段、时间或新旧消息 ID 猜来源。
            key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            head = part.get("peer_head") or legacy_head
            yield part, key, head if isinstance(head, str) and head != MODEL_HEAD else ""


def restore(path, messages, previous=()):
    """独立索引跨过投影裁剪/重建；内容相同但来源冲突时不做推断。"""
    candidates = list(_parts([*previous, *messages]))
    if not candidates:
        return messages
    meta_path = path + ".peer-heads.json"
    try:
        with open(meta_path, encoding="utf-8") as f:
            heads = json.load(f)
    except FileNotFoundError:
        heads = {}
    if not isinstance(heads, dict):
        raise ValueError("invalid peer metadata")
    before = dict(heads)
    for _part, key, head in candidates:
        if head:
            if key not in heads:
                heads[key] = head
            elif heads[key] != head:
                heads[key] = ""  # 不同来源有歧义，宁可缺失也不串会话
    if heads != before:
        tmp = f"{meta_path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(heads, f, ensure_ascii=False)
            os.replace(tmp, meta_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    for part, key, head in _parts(messages):
        if not head and heads.get(key):
            part["peer_head"] = heads[key]
    return messages
