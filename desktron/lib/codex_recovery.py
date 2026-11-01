"""在隔离 HOME 核验撤回，再发布独立副本；原 thread 和原 rollout 始终只读。

恢复意图、固定副本 ID、源摘要及原生撤回边界由调用方持久化。各步骤可重复，
没有边界凭证时不能把目标不存在当作成功。不会复制凭证或启动模型回合。
"""
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from recall_protocol import boundary, read_turn_ids


def _digest(path):
    with open(path, "rb") as f:
        digest = hashlib.sha256()
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
        return digest.hexdigest()


def _snapshot(source, target, source_id, new_id, retain=None):
    """仅规范化独立完整日志。history_base 不是正文，不能当作独立历史复制。"""
    temp = target.with_suffix(".tmp")
    count = 0
    with open(source, encoding="utf-8") as src, open(temp, "w", encoding="utf-8") as dst:
        for i, line in enumerate(src):
            if retain is not None and i >= retain:
                break
            row = json.loads(line)
            if i == 0:
                meta = row.get("payload") or {}
                if row.get("type") != "session_meta" or meta.get("id") != source_id:
                    raise RuntimeError("源日志身份不匹配，停止恢复")
                if meta.get("history_base"):
                    raise RuntimeError("这段历史引用了其他日志分段，暂不支持自动重建；原会话已保留")
                meta["id"] = meta["session_id"] = new_id
            row["ordinal"] = i
            dst.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
        if not count or (retain is not None and count != retain):
            raise RuntimeError("源日志不完整，停止恢复")
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(temp, target)


def recover_recalled_thread(thread_id, target, cwd, env, directory, state, checkpoint, timeout=120):
    import codex_appserver as app
    deadline = time.monotonic() + timeout
    def left():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("副本恢复超时，可重试继续同一笔恢复")
        return remaining
    def save(**patch):
        if checkpoint(**patch) is False:
            raise RuntimeError("恢复事务已变化")
        state.update(patch)
    def connect(home_env):
        conn = app._Connection(cwd=cwd, env=home_env)
        try:
            conn.initialize(overall=left())
            return conn
        except Exception:
            conn.close()
            raise

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    # 在建立任何副本前保留固定身份，HTTP 响应丢失或 Desk 重启不生成第二份。
    if not state.get("thread_id"):
        conn = connect(env)
        try:
            meta = conn.request("thread/read", {"threadId": thread_id, "includeTurns": False},
                                overall=left()).get("thread") or {}
        finally:
            conn.close()
        if meta.get("id") != thread_id or not meta.get("path"):
            raise RuntimeError("无法定位源会话完整日志")
        source = Path(meta["path"]).resolve()
        sessions = Path(env.get("CODEX_HOME") or Path.home()/".codex")/"sessions"
        if not source.is_relative_to(sessions.resolve()):
            raise RuntimeError("源日志不在当前引擎的 sessions 目录")
        save(thread_id=str(uuid.uuid4()), source=str(source), source_sha256=_digest(source),
             created_at=time.time())
    source = Path(state["source"])
    if _digest(source) != state["source_sha256"]:
        raise RuntimeError("源会话在恢复期间发生变化，停止发布副本；原记录已保留")
    new_id = state["thread_id"]
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime(state["created_at"]))
    relative = Path("sessions")/stamp[:10].replace("-", "/")/f"rollout-{stamp}-{new_id}.jsonl"
    isolated = root/"home"
    snapshot = isolated/relative
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if not state.get("snapshot_ready"):
        _snapshot(source, snapshot, thread_id, new_id)
        if _digest(source) != state["source_sha256"]:
            raise RuntimeError("复制期间源日志发生变化，停止恢复")
        save(snapshot_ready=True)
    isolated_env = dict(env, CODEX_HOME=str(isolated))
    # 不复制任何 config/auth/目标调度状态；此连接仅运行历史协议。
    if not state.get("receipt"):
        result = app.revert_thread(None, new_id, target, cwd=cwd, env=isolated_env,
                                   expected=state.get("expected"), timeout=left(),
                                   checkpoint=lambda **patch: save(**patch))
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "隔离副本撤回失败")
        save(receipt=result)
    expected = state["receipt"]["boundary"]
    conn = connect(isolated_env)
    try:
        def request(method, params):
            return conn.request(method, params, overall=left())
        if boundary(read_turn_ids(request, new_id, app._validate_recall_history)) != expected:
            raise RuntimeError("隔离副本历史发生变化，停止发布")
        meta = request("thread/read", {"threadId": new_id, "includeTurns": False})["thread"]
        with open(meta["path"], encoding="utf-8") as f:
            head = json.loads(next(f))["payload"]
        base = head.get("history_base")
        if base:
            if base.get("thread_id") != new_id or not isinstance(base.get("end_ordinal_exclusive"), int):
                raise RuntimeError("撤回后的日志分段边界无法确认")
            retain = base["end_ordinal_exclusive"]
        else:
            retain = None  # legacy rollback 直接更新 snapshot 文件。
    finally:
        conn.close()
    published = Path(env.get("CODEX_HOME") or Path.home()/".codex")/relative
    published.parent.mkdir(parents=True, exist_ok=True)
    if not state.get("published"):
        if published.exists():
            # 写文件与写 checkpoint 之间退出时，只有逐字一致的产物可以复用。
            candidate = root/"publish-check.jsonl"
            _snapshot(snapshot, candidate, new_id, new_id, retain)
            if _digest(candidate) != _digest(published):
                raise RuntimeError("副本路径已有不同内容，停止覆盖")
        else:
            _snapshot(snapshot, published, new_id, new_id, retain)
        save(published=str(published))
    if _digest(source) != state["source_sha256"]:
        raise RuntimeError("发布前源会话发生变化，副本暂不启用")
    conn = connect(env)
    try:
        conn.request("thread/resume", {"threadId": new_id, "excludeTurns": True}, overall=left())
        got = read_turn_ids(lambda m, p: conn.request(m, p, overall=left()), new_id,
                            app._validate_recall_history)
        if boundary(got) != expected:
            raise RuntimeError("发布后的副本边界不匹配，暂不启用")
    finally:
        conn.close()
    return {"ok": True, "verified": True, "thread_id": new_id, "boundary": expected,
            "source_thread_id": thread_id, "source_sha256": state["source_sha256"]}
