"""异步通知是工具来源的内容部件，不是用户发言，也不是再次投递给引擎的提示。

原始引擎日志不改写；实时快照和历史投影共用这里的来源、状态与关联规则。
同步工具结果（含脚本、子代理、主动 wait）只回填执行步骤，不生成来源卡片。
"""
import hashlib
import re


TYPE = "execution-result"
_SCRIPT = re.compile(
    r"(?:^|[\n;|&])\s*(?:(?:/[^\s]+/)?(?:python[\d.]*|node|npm|pnpm|yarn|pytest|bash|sh|zsh|ruby|make|cargo)"
    r"(?:\s|$)|go\s+test(?:\s|$)|(?:\./|/)[^\s;|&]+\.(?:sh|py|js|mjs)(?:\s|$))")


def _text(value):
    if isinstance(value, list):
        return "\n".join(str(x.get("text") or "") for x in value if isinstance(x, dict))
    return str(value or "")


def result_part(*, call_id="", task_id="", kind="command", name="", text="", status="done",
                at=0, output_file="", exit_code=None, duration_ms=0, content_kind="report", delivery=""):
    key = str(task_id or call_id)
    if not key:
        return None  # 没有关联证据时不编造一条来源消息。
    status = {"completed": "done", "succeeded": "done", "failed": "error",
              "killed": "cancelled", "stopped": "cancelled"}.get(status, status)
    return {"id": "exec_" + hashlib.sha256(key.encode()).hexdigest()[:24],
            "type": TYPE, "role": "tool", "delivery": delivery,
            "call_id": str(call_id), "task_id": str(task_id),
            "source": {"kind": kind, "name": str(name or {"agent": "子任务", "script": "脚本执行",
                                                         "command": "后台任务"}[kind])},
            "status": status, "text": _text(text), "content_kind": content_kind, "at": at,
            "output_file": str(output_file), "exit_code": exit_code, "duration_ms": duration_ms}


def is_async_result(part):
    """旧投影只过滤展示，不删日志：同步结果无 task_id，wait 结果有专用哈希后缀。"""
    if part.get("delivery"):
        return part["delivery"] in {"task_notification", "agent_message"}
    task_id = str(part.get("task_id") or "")
    return bool(task_id) and not re.search(r":[0-9a-f]{16}$", task_id)


def from_agent_message(item):
    """Codex 原始 agent_message 有独立类型；绝不按普通 user 正文猜身份。"""
    if item.get("type") != "agent_message":
        return None
    body = _text(item.get("content"))
    match = re.match(r"Message Type: (FINAL_ANSWER|MESSAGE)\s*\n", body)
    if not match:
        return None  # NEW_TASK 是派发，不是返回。
    head, sep, content = body.partition("\nPayload:")
    if not sep:
        return None
    sender = re.search(r"^Sender:\s*(.+)$", head, re.M)
    task = re.search(r"^Task name:\s*(.+)$", head, re.M)
    identity = item.get("author") or (sender.group(1).strip() if sender else "")
    if not identity:
        return None
    name = sender.group(1).strip() if sender else task.group(1).strip() if task else "子任务"
    key = item.get("id") or hashlib.sha256((identity + body).encode()).hexdigest()[:24]
    return result_part(task_id=str(key), kind="agent", name=name.rsplit("/", 1)[-1], delivery="agent_message",
                       text=content.lstrip(), status="done" if match.group(1) == "FINAL_ANSWER" else "returned")


def from_notification(obj, agent=None, tool=None):
    """结构化通知或 transcript 中的 task-notification；不读取它指定的任意文件。"""
    body = _text((obj.get("message") or {}).get("content"))
    def field(tag):
        match = re.search(r"<" + tag + r">([\s\S]*?)</" + tag + r">", body)
        return match.group(1).strip() if match else ""
    task_id = re.sub(r"^(?:agent|bash):", "", str(obj.get("task_id") or field("task-id")))
    call_id = obj.get("tool_use_id") or field("tool-use-id")
    summary = obj.get("summary") or field("summary")
    # 只有 ID/状态的预告不是报告；等待带正文或输出位置的通知再呈现。
    if not (summary or obj.get("result") or field("result") or obj.get("output_file") or field("output-file")):
        return None
    inp = (tool or {}).get("state", {}).get("input") or {}
    kind = "agent" if agent or str((tool or {}).get("tool") or "").lower() in {"task", "agent"} else "command"
    match = re.search(r'Agent\s+"([^"]+)"', summary)
    if match:
        kind = "agent"
    name = (agent or {}).get("task") or inp.get("description") or (match.group(1) if match else "")
    if not name and kind != "agent":
        name = inp.get("command") or summary
    if kind != "agent" and _SCRIPT.search(str(inp.get("command") or "")):
        kind = "script"
    # 退出码只写在摘要那句话里:Background command "…" completed (exit code 0)。
    exit_code = obj.get("exit_code")
    if exit_code is None:
        m_exit = re.search(r"\(exit code (-?\d+)\)", summary or "")
        exit_code = int(m_exit.group(1)) if m_exit else None
    return result_part(call_id=call_id, task_id=task_id, kind=kind, name=str(name or "")[:160],
                       delivery="task_notification", exit_code=exit_code,
                       text=obj.get("result") or field("result") or summary,
                       status=obj.get("status") or field("status") or "done",
                       output_file=obj.get("output_file") or field("output-file"),
                       duration_ms=(agent or {}).get("ms") or 0,
                       content_kind="report" if obj.get("result") or field("result") else "summary")


# 回填到派出它的那一步时,返回记录里留这些字段;id/type/role 是独立卡片才需要的身份。
RETURN_KEYS = ("status", "text", "content_kind", "at", "output_file", "exit_code", "duration_ms",
               "task_id", "call_id", "delivery", "source")


def find_tool_part(parts, call_id="", task_id=""):
    """派出这次后台任务的那一步。先按调用 id 认(通知里的 tool-use-id 与工具块的 call_id 是
    同一个),认不出再按任务 id 在回执原文里找——Bash 后台命令的回执写着
    「Command running in background with ID: <id>」,通知只带这个 id 时靠它对上。"""
    call_id = str(call_id or "")
    task_id = re.sub(r"^(?:agent|bash):", "", str(task_id or ""))
    rows = [p for p in (parts or []) if isinstance(p, dict) and p.get("type") == "tool"]
    if call_id:
        for p in rows:
            if str(p.get("call_id") or "") == call_id:
                return p
    if task_id:
        pat = re.compile(r"(?<![\w-])" + re.escape(task_id) + r"(?![\w-])")
        for p in reversed(rows):
            if pat.search(str((p.get("state") or {}).get("output") or "")[:2000]):
                return p
    return None


def handed_off(step, result):
    """这一步是不是把活交出去了——决定这条通知算不算「交回」。

    真异步的回执交出的是一个句柄:Bash 后台是「Command running in background with
    ID: <id>」,Monitor 是「Monitor started (task <id>…)」,前台命令跑超时被转后台也写成
    同样的形态。句柄交出去之后结果必须另外送回,那条通知才是这一步的结局,行上写
    「已返回」才是准的。

    同步命令的回执就是结果本身,里面不会出现那个 id。Claude Code 对前台 Bash 也发一帧
    task_notification(正文就是描述文本,没有退出码),照单回填的话,一排从头到尾都是同步的
    命令会全部显示成「已返回」——本机 42 条回填里有 13 条是这么来的(2026-09-09 复核)。

    还没有自己结果的那一步先按「交出去了」记,但那只是暂判:通知与这一步自己的 tool_result
    谁先到并不固定,前台 Bash 实见通知先到(2026-09-10),此刻输出还是空的,同步也会被记成
    交回。暂判的回填带 pending,等这一步自己的结果落地,由 settle_return 复核一次。
    """
    task_id = re.sub(r"^(?:agent|bash):", "", str((result or {}).get("task_id") or ""))
    out = str(((step or {}).get("state") or {}).get("output") or "")
    if not out.strip() or not task_id:
        return True
    return _has_handle(out, task_id)


def _has_handle(out, task_id):
    return re.search(r"(?<![\w-])" + re.escape(task_id) + r"(?![\w-])", out[:2000]) is not None


def _own_output(step):
    return str(((step or {}).get("state") or {}).get("output") or "")


def settle_return(step):
    """这一步自己的结果落地后,复核当时那次暂判。

    通知先于 tool_result 到达时,handed_off 只能按「还没有自己的结果」放行。等结果真的
    来了,如果里面没有那个句柄,说明这一步是同步跑完的,通知不过是回声:撤掉回填,行上
    不写「已返回」,也不着色。句柄在里面则是真交出去过,清掉 pending 即可。

    返回 True 表示 step 变了,调用方需要再发一次 part.updated。
    """
    ret = (step or {}).get("returned")
    if not isinstance(ret, dict) or not ret.get("pending"):
        return False
    out = _own_output(step)
    if not out.strip():
        return False    # 还是空的,等下一次
    task_id = re.sub(r"^(?:agent|bash):", "", str(ret.get("task_id") or ""))
    if task_id and not _has_handle(out, task_id):
        step.pop("returned", None)
    else:
        ret.pop("pending", None)
    return True


def settle_stored(step):
    """读记录时复核一次回填:同步跑完的那一步不该带着「已返回」。

    2026-09-10 之前落盘的记录里有这一类——通知先于这一步自己的 tool_result 到达,当时只能
    暂判为交出去了,后来没有人复核。判据与 handed_off 同一条:自己的输出里有没有那个句柄。
    只删这条回填,不动别的字段。"""
    ret = (step or {}).get("returned")
    if not isinstance(ret, dict) or not ret.get("task_id"):
        return False
    if handed_off(step, ret):
        return ret.pop("pending", None) is not None
    step.pop("returned", None)
    return True


def attach_return(step, result):
    """把返回记在派出它的那一步上(step["returned"]),不另起消息。重复通知原位更新,
    摘要不盖报告,空值不盖已有值——与 append_result 同一套合并规则。"""
    if not step or not result:
        return None
    new = {k: result.get(k) for k in RETURN_KEYS if k in result}
    old = step.get("returned")
    if isinstance(old, dict) and old:
        merged = dict(old)
        for k, v in new.items():
            if v is None or (k in {"at", "duration_ms", "call_id", "task_id", "output_file"} and not v and old.get(k)):
                continue
            merged[k] = v
        if old.get("content_kind") == "report" and new.get("content_kind") == "summary":
            merged["text"] = old.get("text", "")
            merged["content_kind"] = "report"
        src = old.get("source") or {}
        if (src.get("kind") in {"agent", "script"} and (new.get("source") or {}).get("kind") == "command"):
            merged["source"] = src
        step["returned"] = merged
    else:
        step["returned"] = new
    # 决定的时候这一步还没有自己的结果:记成暂判,等结果落地由 settle_return 复核。
    if _own_output(step).strip():
        step["returned"].pop("pending", None)
    else:
        step["returned"]["pending"] = True
    return step


def append_result(parts, result, seen=None):
    """重复完成帧原位更新；call/task 两种标识只用于同一条返回的对账。"""
    if not result:
        return None
    keys = [k + ":" + result[k] for k in ("call_id", "task_id") if result.get(k)]
    seen = seen if seen is not None else {}
    def same(candidate):
        if candidate.get("task_id") and result.get("task_id") and candidate["task_id"] != result["task_id"]:
            return False
        return any(candidate.get(k) and candidate.get(k) == result.get(k) for k in ("call_id", "task_id"))
    old = next((seen[k] for k in keys if k in seen and same(seen[k])), None)
    if old is None:
        old = next((p for p in parts if p.get("type") == TYPE and same(p)), None)
    if old is not None:
        ident = old["id"]
        # 摘要不能盖掉报告；真正的报告更新即使更短也应采用，不能按字数猜权威性。
        content = old.get("text", "")
        content_kind = old.get("content_kind")
        source = old.get("source") or {}
        old.update({k: v for k, v in result.items() if v is not None
                    and not (k in {"at", "duration_ms", "call_id", "task_id", "output_file"}
                             and not v and old.get(k))})
        if (source.get("kind") in {"agent", "script"} and old.get("source", {}).get("kind") == "command"
                or source.get("kind") == "agent" and old.get("source", {}).get("name") == "子任务"):
            old["source"] = source
        old["id"] = ident
        if content_kind == "report" and result.get("content_kind") == "summary":
            old["text"] = content
            old["content_kind"] = content_kind
    else:
        old = dict(result)
        parts.append(old)
    for key in keys:
        seen[key] = old
    return old
