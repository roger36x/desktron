"""app-server 撤回的读—校验—写—回读协议，不把 RPC 成功等同于撤回成功。"""
import hashlib
import json


def boundary(ids):
    return {"count": len(ids), "tail": ids[-1] if ids else None,
            "sha256": hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest()}


def read_turn_ids(request, thread_id, validate_history=None):
    out = request("thread/read", {"threadId": thread_id, "includeTurns": True})
    thread = out.get("thread") or {}
    turns = thread.get("turns")
    if thread.get("id") != thread_id or not isinstance(turns, list):
        raise RuntimeError("原生历史缺少完整 thread/turns，不能确认撤回边界")
    if out.get("nextCursor") or thread.get("nextCursor") or thread.get("hasMore"):
        raise RuntimeError("原生历史分页未完整读取，不能确认撤回边界")
    if validate_history:
        validate_history(thread)
    if thread.get("historyMode") == "paginated":
        # includeTurns 对分页历史已弃用，不能把其返回值当作完整回合边界。
        turns, cursor, seen = [], None, set()
        while True:
            params = {"threadId": thread_id, "sortDirection": "asc", "limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = request("thread/turns/list", params)
            data = page.get("data")
            if not isinstance(data, list):
                raise RuntimeError("原生历史分页缺少 data，不能确认撤回边界")
            turns.extend(data)
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in seen or not data:
                raise RuntimeError("原生历史分页游标异常，不能确认撤回边界")
            seen.add(cursor)
    ids = [t.get("id") for t in turns]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise RuntimeError("原生回合 ID 缺失或重复，不能确认撤回边界")
    if any(t.get("status") in ("inProgress", "running") for t in turns):
        raise RuntimeError("原生回合仍在执行，等待中断确认")
    return ids


def verified_revert(request, thread_id, target, method, expected=None, checkpoint=None,
                    validate_history=None):
    def save(**patch):
        if checkpoint is not None and checkpoint(**patch) is False:
            raise RuntimeError("撤回事务已换代，停止操作")

    def observe():
        return read_turn_ids(request, thread_id, validate_history)

    ids = observe()
    if expected is not None and boundary(ids) == expected:
        return {"ok": True, "method": "already-reverted", "verified": True, "boundary": expected}
    if not ids or ids[-1] != target:
        return {"ok": False, "conflict": True,
                "error": "目标回合不是历史末尾或已不存在；没有匹配的撤回凭证，停止自动删除"}
    before = boundary(ids[:-1])
    if expected is not None and expected != before:
        return {"ok": False, "conflict": True, "error": "撤回前的历史边界发生变化，停止自动删除"}
    save(phase="applying", expected=before)
    used = "thread/rollback" if method == "rollback" else "thread/revert"
    try:
        if used == "thread/rollback":
            request(used, {"threadId": thread_id, "numTurns": 1})
        else:
            request(used, {"threadId": thread_id, "beforeTurnId": target})
    except Exception as exc:
        msg = str(exc).lower()
        legacy = "thread/revert only supports paginated threads" in msg
        missing = "method" in msg and ("not found" in msg or "unknown" in msg)
        if used != "thread/revert" or not (legacy or missing):
            raise
        # 兼容失败之后重新观察，绝不盲目重放 rollback(1)。
        fresh = observe()
        if boundary(fresh) == before:
            return {"ok": True, "method": "already-reverted", "verified": True, "boundary": before}
        if fresh != ids:
            return {"ok": False, "conflict": True, "error": "兼容撤回前历史已变化，停止自动删除"}
        used = "thread/rollback"
        request(used, {"threadId": thread_id, "numTurns": 1})
    save(phase="verifying")
    if boundary(observe()) != before:
        return {"ok": False, "conflict": True, "error": "撤回 RPC 已返回，但历史回读不匹配；保持发送拦截"}
    return {"ok": True, "method": used, "verified": True, "boundary": before}
