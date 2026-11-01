"""Codex 工具结果：完成传输不代表执行成功，实时与历史共用同一判据。"""
import json


def result_text(value):
    if isinstance(value, list):
        return "".join(result_text(x) for x in value)
    if isinstance(value, dict):
        if "text" in value:
            return str(value["text"])
        if "content" in value:
            return result_text(value["content"])
        return json.dumps(value, ensure_ascii=False)
    return str(value or "")


def tool_failure(item):
    """仅认结构字段及工具包装器的固定错误头，不搜索任意输出中的 error 字样。"""
    values = [item] + [item[k] for k in ("result", "output") if k in item]
    for value in values:
        if isinstance(value, str):
            if value.startswith("Script completed\n") and "\nOutput:\n" in value:
                value = value.split("\nOutput:\n", 1)[1].strip()
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                pass
        if isinstance(value, dict):
            code = value.get("exit_code", value.get("exitCode"))
            if (value.get("isError") is True or value.get("success") is False
                    or value.get("error") or value.get("status") in ("failed", "declined")
                    or (isinstance(code, int) and code != 0)):
                # stdout/stderr/content 是执行详情，不是错误摘要。把它们复制到
                # failure.message 会使失败步骤在折叠时仍铺开一整段输出。
                reason = result_text(value.get("error"))
                failure = {"kind": "tool_error", "message": (reason or (f"工具执行失败（退出码 {code}）" if code is not None else "工具执行失败"))[:500]}
                if isinstance(code, int) and not isinstance(code, bool):
                    failure["exit_code"] = code
                return failure
        if isinstance(value, dict) and not any(k in value for k in ("text", "content")):
            continue
        text = result_text(value).strip()
        if text.startswith("Script completed\n") and "\nOutput:\n" in text:
            try:
                wrapped = json.loads(text.split("\nOutput:\n", 1)[1].strip())
            except ValueError:
                wrapped = None
            if isinstance(wrapped, dict):
                failure = tool_failure(wrapped)
                if failure:
                    return failure
        if text.startswith("Script failed\n") or text.startswith("Script error:\n"):
            reason = text.split("Script error:\n", 1)[-1].strip()
        elif text.startswith("apply_patch verification failed:"):
            reason = text
        else:
            continue
        kind = "validation" if reason.startswith("apply_patch verification failed:") else "tool_error"
        return {"kind": kind, "message": reason[:500]}
    return None


def result_state(item, status="done"):
    failure = tool_failure(item) if status != "running" else None
    return {"status": "error" if failure else status, **({"failure": failure} if failure else {})}
