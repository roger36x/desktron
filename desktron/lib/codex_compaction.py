"""Manual maintenance of an exclusive, projected-history app-server thread."""
import time
from shepherd.execution_budget import ExecutionDeadline


def compact_projected(conn, thread_id, *, on_notification, on_submitting,
                      on_started, on_completed, on_working_context,
                      cancelled, timeout, deadline):
    from codex_appserver import AppServerError, AppServerTimeout
    terminal = None
    native_turn = ''
    compact_signal = ''
    turn_status = ''
    native_error = None
    budget = ExecutionDeadline(timeout, max(.001, deadline - time.monotonic()), operation='compact')

    def left():
        remaining = budget.hard_deadline - time.monotonic()
        if remaining <= 0:
            raise AppServerError('原生手动压缩超时，未收到完成凭据')
        return remaining

    def receive(method, params):
        nonlocal terminal, native_turn, compact_signal, turn_status, native_error
        # This is a new private thread: all accepted maintenance evidence must
        # explicitly belong to it, never to another thread or an earlier turn.
        if params.get('threadId') != thread_id:
            return
        turn = params.get('turn') or {}
        if method == 'turn/started':
            if native_turn and turn.get('id') != native_turn:
                raise AppServerError('手动压缩出现了不匹配的原生回合')
            native_turn = turn.get('id') or ''
            if native_turn and on_started:
                on_started(thread_id, native_turn)
        if method == 'turn/completed' and native_turn and turn.get('id') == native_turn:
            turn_status = turn.get('status') or ''
            if turn.get('status') in ('failed', 'interrupted'):
                terminal = {'status':turn['status'], 'signal':method, 'threadId':thread_id,
                            'turnId':native_turn, 'error':turn.get('error') or native_error}
        if method == 'error' and not params.get('willRetry') and not params.get('will_retry'):
            if not native_turn:
                raise AppServerError('原生手动压缩失败：' + str(params.get('error') or '未返回成功凭据'))
            if params.get('turnId') in (None, native_turn):
                # Request errors precede turn/completed. Keep reading so the
                # native failed terminal can settle the operation normally.
                native_error = params.get('error')
        item = params.get('item') or {}
        if (method == 'thread/compacted' or
                (method == 'item/completed' and item.get('type') == 'contextCompaction')):
            if not native_turn or params.get('turnId') in (None, native_turn):
                compact_signal = method
        if compact_signal and (not native_turn or turn_status == 'completed'):
            terminal = {'status':'completed', 'signal':compact_signal, 'threadId':thread_id,
                        'turnId':native_turn}
        if on_notification:
            on_notification(method, params)

    left()
    if cancelled and cancelled():
        raise AppServerError('手动压缩在派发前已取消')
    if on_submitting and on_submitting(thread_id) is False:
        raise AppServerError('手动压缩未派发')
    # The RPC reply means accepted, not compacted. Wait for native evidence.
    conn.request('thread/compact/start', {'threadId':thread_id}, receive,
                 timeout=min(30,left()), overall=left())
    cancel_at = None
    while terminal is None:
        if cancelled and cancelled():
            cancel_at = cancel_at or time.monotonic()
            if time.monotonic() - cancel_at >= 3:
                raise AppServerError('手动压缩已取消，原生终态未确认')
        # Native compaction does not promise incremental output. Its accepted
        # operation is bounded by the shared absolute budget, not token silence.
        remaining = left()
        try:
            msg = conn.read(min(1, remaining))
        except AppServerTimeout:
            continue
        if msg.get('method') and 'id' in msg:
            conn.handle_server_request(msg)
        elif msg.get('method'):
            receive(msg['method'], msg.get('params') or {})
    if terminal['status'] == 'completed':
        thread = conn.request('thread/read', {'threadId':thread_id,'includeTurns':False},
                              overall=min(10,left())).get('thread') or {}
        if thread.get('id') != thread_id or not thread.get('path'):
            raise AppServerError('手动压缩完成但缺少匹配的工作上下文')
        # Finish the writer before parsing the authoritative replacement history.
        conn.close()
        on_working_context(thread_id, thread['path'])
    on_completed(terminal)
    return thread_id
