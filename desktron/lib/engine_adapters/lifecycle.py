"""Native lifecycle projection; never schedules, retries or settles a turn.

Retry is an operation inside a Desk turn, not its terminal outcome. Native
adapters supply structured facts; messages are display-only. The existing
turn generation and context identity fence late callbacks.
"""
import copy
import time


def _current(adapter, ctx):
    if ctx.get("_lifecycle_closed"):
        return False
    live = adapter._live.get(ctx["sid"])
    return live is None or live is ctx


def retry_view(adapter, ctx):
    retry = ctx.get("_maintenance_retry") or ctx.get("retry")
    return adapter._retry_view(retry) if isinstance(retry, dict) else None


def _publish_retry(adapter, ctx):
    adapter._checkpoint_live(ctx, force=True)
    retry = retry_view(adapter, ctx)
    adapter.bus.publish({"type": "session.retry", "properties": {
        **(retry or {}), "sessionID": ctx["sid"],
        "generation": int(ctx.get("seq") or 0),
        "messageID": ctx.get("msg_id"), "active": retry is not None}})


def retry_update(adapter, ctx, retry, *, scope=None):
    """Publish the same snapshot used by /state and the live checkpoint."""
    if not _current(adapter, ctx):
        return False
    scope = scope or ("compaction" if ctx.get("compact_t0") else "request")
    key = "retry" if scope == "request" else "_maintenance_retry"
    ctx[key] = dict(copy.deepcopy(retry), scope=scope)
    ctx["_line_at"] = time.time()
    _publish_retry(adapter, ctx)
    return True


def retry_clear(adapter, ctx, *, scope="request"):
    """Native progress/end clears waiting, without declaring success."""
    key = "retry" if scope == "request" else "_maintenance_retry"
    if not _current(adapter, ctx) or ctx.pop(key, None) is None:
        return False
    _publish_retry(adapter, ctx)
    return True


def snapshot(adapter, ctx):
    """One read model for live, reconnect and checkpoints; not a second ledger."""
    ctx = ctx or {}
    out = {"generation": int(ctx.get("seq") or 0)}
    retry = retry_view(adapter, ctx)
    if retry:
        out["retry"] = retry
    if ctx.get("compact_t0"):
        out["compacting"] = ctx["compact_t0"]
    if ctx.get("native_queue"):
        out["native_queue"] = copy.deepcopy(ctx["native_queue"])
    if ctx.get("sid"):
        out["activity"] = adapter.turn_activity(ctx["sid"], ctx)
    return out


def compact_update(adapter, ctx, properties):
    if not _current(adapter, ctx):
        return False
    props = dict(copy.deepcopy(properties), sessionID=ctx["sid"],
                 generation=int(ctx.get("seq") or 0), messageID=ctx.get("msg_id"))
    if props.get("running"):
        ctx["compact_t0"] = props.get("started") or ctx.get("compact_t0") or int(time.time()*1000)
        props["started"] = ctx["compact_t0"]
    else:
        ctx["compact_t0"] = 0
        retry_clear(adapter, ctx, scope="compaction")
    if props.get("result") == "retrying":
        ctx["_native_recovery"] = "compaction"
    else:
        ctx.pop("_native_recovery", None)
    record_compact = getattr(adapter, "_record_compact", None)
    if record_compact is not None:
        record_compact(ctx, props)
    adapter._compact_state[ctx["sid"]] = copy.deepcopy(props)
    adapter._checkpoint_live(ctx, force=True)
    adapter.bus.publish({"type": "session.compact", "properties": props})
    return True


def request_failed(ctx, failure):
    """An attempt failed; only the native loop knows whether recovery follows."""
    ctx["_request_failure"] = copy.deepcopy(failure)


def request_recovered(ctx):
    ctx.pop("_request_failure", None)


def commit_request_failure(ctx):
    """Called at native settlement or Desk closure, never at a retry event.

    A process/protocol/permission failure already recorded by Desk takes
    precedence. This does not start work or declare a successful terminal.
    """
    failure = ctx.pop("_request_failure", None)
    if (not failure or ctx.get("aborted") or ctx.get("engine_error")
            or ctx.get("engine_failure")):
        return None
    ctx["engine_failure"] = copy.deepcopy(failure)
    ctx["engine_error"] = failure["message"]
    return failure
