"""来源消息与真人输入隔离；实时、落盘与历史重建使用同一关联规则。

2026-09-08 起返回优先回填到派出它的那一步(见文件末尾一组用例);独立来源卡片只在认不出那一步时保留。"""
import json
from unittest.mock import patch

import pytest
from test_claude_task_notification import NOTIF_BODY, _user, _assistant
from test_claude_turn_boundary import Bus, _init, _tool, _text
import engine_adapters as ea
from engine_adapters.execution_returns import from_notification, from_agent_message, append_result, is_async_result, attach_return, find_tool_part


@pytest.fixture
def adapter(tmp_path):
    ad = ea.ClaudeAdapter(str(tmp_path), str(tmp_path), Bus())
    sid = ad.create_session()["id"]
    return ad, sid


def context(sid):
    return {"sid": sid, "msg_id": "msg_a_test", "parts": {}, "order": [], "t0": 0}


def result(call_id, text):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": call_id, "content": text}]}}


def test_claude_parallel_results_match_call_ids_and_ignore_unknown(adapter):
    ad, sid = adapter
    ctx = context(sid)
    ad.handle_event(_init(), ctx)
    ad.handle_event(_tool(0, "Agent"), ctx)
    ad.handle_event(_tool(1, "Bash"), ctx)
    ad.handle_event(result("tu_0", "子代理检查报告"), ctx)
    ad.handle_event(result("tu_1", "普通命令输出"), ctx)
    ad.handle_event(result("missing", "不得覆盖最近一步"), ctx)
    tools = [p for p in ctx["parts"].values() if p["type"] == "tool"]
    assert [p["state"]["output"] for p in tools] == ["子代理检查报告", "普通命令输出"]
    returns = [p for p in ctx["parts"].values() if p["type"] == "execution-result"]
    assert returns == []
    ad.handle_event(result("tu_0", "子代理检查报告"), ctx)
    assert not any(p["type"] == "execution-result" for p in ctx["parts"].values())


def test_notification_does_not_inject_or_finish_user_turn_and_survives_storage(adapter):
    ad, sid = adapter
    ctx = context(sid)
    ad.handle_event(_init(), ctx)
    with patch.object(ad, "prompt_async", side_effect=AssertionError("不允许重投")):
        ad.handle_event(_user("n1", NOTIF_BODY, origin={"kind": "task-notification"}), ctx)
    assert len(ctx["parts"]) == 1
    card = next(iter(ctx["parts"].values()))
    assert card["type"] == "execution-result"
    assert card["source"] == {"kind": "agent", "name": "Explore"}
    assert card["text"] == "改了 frontend/app-chat.js 的三处……"
    snap = ad._read_live_checkpoint(sid)
    assert snap["parts"][0]["role"] == "tool"
    ad._finish_turn(sid, ctx, None)
    saved = ad.messages(sid)
    assert saved[-1]["info"]["role"] != "user"
    assert saved[-1]["parts"][0]["role"] == "tool"


def test_history_keeps_report_without_followup_and_rebuild_is_stable(adapter, tmp_path):
    ad, sid = adapter
    path = tmp_path / "transcript.jsonl"
    rows = [_user("u1", "开始"), _assistant("a1", "正在检查"),
            _user("n1", NOTIF_BODY, origin={"kind": "task-notification"})]
    path.write_text("\n".join(json.dumps(row) for row in rows))
    msgs = ad._group_transcript(str(path), sid, "engine")
    again = ad._group_transcript(str(path), sid, "engine")
    assert msgs == again
    returns = [m for m in msgs if m["info"]["role"] == "tool"]
    assert len(returns) == 1
    assert returns[0]["parts"][0]["text"].startswith("改了")
    assert len([m for m in msgs if ad._turn_start(m)]) == 1


def test_history_duplicate_notifications_keep_one_report(adapter, tmp_path):
    ad, sid = adapter
    path = tmp_path / "transcript.jsonl"
    rows = [_user("n1", NOTIF_BODY, origin={"kind": "task-notification"}),
            _assistant("a1", "我来核实"),
            _user("n2", NOTIF_BODY, origin={"kind": "task-notification"}),
            _assistant("a2", "核实完成")]
    path.write_text("\n".join(json.dumps(row) for row in rows))
    msgs = ad._group_transcript(str(path), sid, "engine")
    assert len([m for m in msgs if m["info"]["role"] == "tool"]) == 1
    assert not any(m["info"]["role"] == "user" for m in msgs)
    assert all(m["info"]["role"] == "assistant" for m in msgs if m["info"].get("aside"))


@pytest.mark.parametrize("tool,inp", [
    ("bash", {"command": "git status --short"}),
    ("bash", {"command": "python3 scripts/check.py"}),
    ("bash", {"command": "npm test"}),
    ("bash", {"command": "bash scripts/test.sh"}),
    ("bash", {"command": "sleep 10", "run_in_background": True}),
    ("Agent", {"description": "检查布局"}),
    ("Agent", {"run_in_background": True}),
    ("TaskOutput", {"task_id": "background-job", "block": True}),
])
@pytest.mark.parametrize("status", ["done", "error", "cancelled"])
def test_tool_completion_never_creates_source_message(adapter, tool, inp, status):
    ad, sid = adapter
    ctx = context(sid)
    p = ad._ensure_part(ctx, "call", "tool")
    p["tool"] = tool
    p["state"] = {"status": status, "input": inp, "output": "结果"}
    ad._part_updated(ctx, p)
    assert list(ctx["parts"].values()) == [p]
    assert p["state"]["output"] == "结果"


def test_duplicate_summary_does_not_replace_full_result():
    full = from_notification(_user("n1", NOTIF_BODY))
    parts = []
    append_result(parts, full)
    short = dict(full, text="完成", content_kind="summary")
    append_result(parts, short)
    assert len(parts) == 1
    assert parts[0]["text"] == full["text"]


def test_authoritative_shorter_report_and_zero_exit_code_replace_old_values():
    full = from_notification(_user("n1", NOTIF_BODY))
    full["exit_code"] = 2
    parts = []
    append_result(parts, full)
    append_result(parts, dict(full, text="已修复", exit_code=0))
    assert parts[0]["text"] == "已修复"
    assert parts[0]["exit_code"] == 0


def test_different_tasks_are_not_merged_by_a_shared_call():
    one = from_notification(_user("n1", NOTIF_BODY))
    two = dict(one, task_id="another-task", text="另一份结果")
    parts, seen = [], {}
    append_result(parts, one, seen)
    append_result(parts, two, seen)
    assert len(parts) == 2


def test_async_script_notification_is_visible_and_output_is_literal():
    event = {"type": "system", "subtype": "task_notification", "task_id": "bash:job1",
             "status": "failed", "result": "<script>alert(1)</script>"}
    tool = {"tool": "Bash", "state": {"input": {"command": "python3 check.py", "run_in_background": True}}}
    card = from_notification(event, tool=tool)
    assert card["source"]["kind"] == "script"
    assert card["status"] == "error"
    assert card["text"] == event["result"]
    assert is_async_result(card)


def test_long_return_is_clipped_only_for_transport(adapter):
    ad, sid = adapter
    card = from_notification(_user("n1", NOTIF_BODY))
    card["text"] = "完整报告" * 4000
    rows = [{"info": {"id": "m", "role": "tool"}, "parts": [card]}]
    clipped = ad._trim_outputs(rows)
    assert len(clipped[0]["parts"][0]["text"]) == ad.LIST_OUTPUT_CAP
    assert clipped[0]["parts"][0]["textMore"] == 16000
    assert len(card["text"]) == 16000
    ad._append_msg(sid, rows[0])
    assert ad.part(sid, card["id"])["text"] == card["text"]


def test_notification_system_and_transcript_share_identity():
    system = {"task_id": "agent:ab948002b9c5bc293", "status": "completed", "summary": "已完成"}
    live = from_notification(system)
    history = from_notification(_user("n1", NOTIF_BODY))
    assert live["id"] == history["id"]
    rows = []
    append_result(rows, live)
    append_result(rows, history)
    assert len(rows) == 1
    assert rows[0]["source"]["name"] == "Explore"


def test_codex_agent_message_is_a_return_not_a_user_prompt():
    message = {"type": "agent_message", "author": "/root/check", "recipient": "/root", "content": [
        {"type": "input_text", "text": "Message Type: FINAL_ANSWER\nTask name: /root\nSender: /root/check\nPayload:\n检查通过"}]}
    card = from_agent_message(message)
    assert card["role"] == "tool" and card["source"]["name"] == "check"
    assert card["text"] == "检查通过"
    assert from_agent_message(dict(message, type="message", role="user")) is None
    message["content"][0]["text"] = "Message Type: NEW_TASK\nTask name: /root/check"
    assert from_agent_message(message) is None


def test_codex_wait_result_stays_in_tool_step(tmp_path):
    ad = ea.CodexAdapter(str(tmp_path), str(tmp_path), Bus())
    sid = ad.create_session()["id"]
    ad.subagents = lambda _: [{"id": "child", "task": "检查布局"}]
    ctx = context(sid)
    item = {"id": "wait1", "type": "collabAgentToolCall", "tool": "wait", "status": "completed",
            "agentsStates": {"child": {"status": "completed", "message": "布局检查通过"}}}
    ad._app_event("item/completed", {"item": item}, ctx)
    ad._app_event("item/completed", {"item": dict(item, id="wait2")}, ctx)
    cards = [p for p in ctx["parts"].values() if p["type"] == "execution-result"]
    assert cards == []
    assert len([p for p in ctx["parts"].values() if p["type"] == "tool"]) == 2


def test_pi_sync_script_history_and_live_do_not_create_return_cards(tmp_path):
    ad = ea.PiAdapter(str(tmp_path), str(tmp_path), Bus())
    sid = ad.create_session()["id"]
    ctx = context(sid)
    args = {"command": "python3 check.py"}
    ad._pi_tool_run(ctx, {"toolCallId": "call1", "toolName": "bash", "args": args})
    ad._pi_tool_done(ctx, {"toolCallId": "call1", "result": {"content": [{"type": "text", "text": "检查通过"}]}})
    assert not any(p["type"] == "execution-result" for p in ctx["parts"].values())
    history = ad._pi_parts([{"type": "toolCall", "id": "call1", "name": "bash", "arguments": args}],
                          "history", sid, {"call1": {"content": [{"type": "text", "text": "检查通过"}]}})
    assert not any(p["type"] == "execution-result" for p in history)
    assert history[0]["state"]["output"] == "检查通过"


def test_old_sync_cards_filtered_without_mutating_stored_messages(adapter):
    ad, sid = adapter
    async_card = from_notification(_user("n1", NOTIF_BODY))
    old_async = dict(async_card)
    old_async.pop("delivery")
    sync = dict(old_async, id="sync", task_id="", call_id="call")
    wait = dict(old_async, id="wait", task_id="child:0123456789abcdef")
    tool = {"id": "call", "type": "tool", "state": {"output": "保留执行记录"}}
    rows = [{"info": {"id": "m", "role": "assistant"}, "parts": [tool, sync, wait, old_async]},
            {"info": {"id": "n", "role": "tool"}, "parts": [sync]}]
    ad._append_msg(sid, rows[0])
    ad._append_msg(sid, rows[1])
    projected = json.loads(b"[" + ad.messages_window_json(sid, 10) + b"]")
    assert len(projected) == 1
    assert projected[0]["parts"] == [tool, old_async]
    assert ad.messages(sid)[0]["parts"] == rows[0]["parts"]
    assert len(rows[0]["parts"]) == 4


@pytest.mark.parametrize("tool", ["Bash", "Agent"])
def test_claude_sync_history_keeps_only_tool_output(adapter, tmp_path, tool):
    ad, sid = adapter
    call = _assistant("a1", "")
    call["message"]["content"] = [{"type": "tool_use", "id": "call1", "name": tool,
                                   "input": {"command": "python3 check.py", "description": "检查"}}]
    path = tmp_path / "sync.jsonl"
    rows = [_user("u1", "检查"), call, result("call1", "检查通过"), _assistant("a2", "完成")]
    path.write_text("\n".join(json.dumps(row) for row in rows))
    parts = [p for m in ad._group_transcript(str(path), sid, "engine") for p in m["parts"]]
    assert not any(p["type"] == "execution-result" for p in parts)
    assert next(p for p in parts if p["type"] == "tool")["state"]["output"] == "检查通过"


# ---- 返回回填到派出它的那一步(2026-09-08) ----
# 此前返回做成独立的来源卡片、另开回合。回合中途送达的通知在记录里只有 attachment 条目,
# 投影认不出,回合一收尾按记录重建卡片就没了;实时那侧又把用户这一轮的回答切到卡下面。
# 现在通知按 tool-use-id(或回执原文里的任务 id)回填到派出它的那一步:步骤行写「已返回 ·
# 退出码 · 耗时」,展开看报告;实时、落盘、投影三条路同一形态。认不出那一步才退回卡片。

def _bg_notif(task="bash:j1", call_id="tu_0", **extra):
    o = {"type": "system", "subtype": "task_notification", "task_id": task, "status": "completed",
         "summary": 'Background command "跑测试" completed (exit code 0)',
         "output_file": "/tmp/tasks/j1.output"}
    if call_id:
        o["tool_use_id"] = call_id
    o.update(extra)
    return o


def test_live_notification_attaches_to_dispatching_step(adapter):
    ad, sid = adapter
    ctx = context(sid)
    ad.handle_event(_init(), ctx)
    ad.handle_event(_tool(0, "Bash"), ctx)
    ad.handle_event(_bg_notif(), ctx)
    parts = list(ctx["parts"].values())
    assert [p["type"] for p in parts] == ["tool"]
    r = parts[0]["returned"]
    assert r["status"] == "done" and r["exit_code"] == 0
    assert r["task_id"] == "j1" and r["output_file"] == "/tmp/tasks/j1.output"
    assert r["source"]["kind"] in {"command", "script"}
    # 重复通知原位更新,不长出第二份
    ad.handle_event(_bg_notif(status="failed"), ctx)
    assert len(ctx["parts"]) == 1
    assert parts[0]["returned"]["status"] == "error"


def test_live_notification_matches_step_by_task_id_in_receipt(adapter):
    ad, sid = adapter
    ctx = context(sid)
    ad.handle_event(_init(), ctx)
    ad.handle_event(_tool(0, "Bash"), ctx)
    ad.handle_event(result("tu_0", "Command running in background with ID: j1. Output is being written to: /tmp/x"), ctx)
    ad.handle_event(_bg_notif(call_id=""), ctx)
    parts = list(ctx["parts"].values())
    assert [p["type"] for p in parts] == ["tool"]
    assert parts[0]["returned"]["task_id"] == "j1"


def test_idle_notification_updates_stored_step_and_publishes(adapter):
    """引擎空闲时交回:派它出去的那一轮早已落盘。改存储里那一块,并按那条消息的 id 发 updated。"""
    ad, sid = adapter
    step = {"id": "p1", "type": "tool", "tool": "Bash", "call_id": "c9",
            "state": {"status": "done", "input": {"command": "python3 x.py", "run_in_background": True},
                      "output": "Command running in background with ID: j9. Output is being written to: /tmp/x"}}
    ad._append_msg(sid, {"info": {"id": "msg_a_old", "role": "assistant", "sessionID": sid, "at": 1}, "parts": [step]})
    ctx = context(sid)
    ad.handle_event(_bg_notif(task="bash:j9", call_id="c9"), ctx)
    assert ctx["parts"] == {}
    saved = ad.messages(sid)[-1]["parts"][0]
    assert saved["returned"]["task_id"] == "j9" and saved["returned"]["exit_code"] == 0
    assert ad.part(sid, "p1")["returned"]["status"] == "done"
    ups = [e["properties"]["part"] for e in ad.bus.events if e.get("type") == "message.part.updated"]
    assert ups and ups[-1]["id"] == "p1" and ups[-1]["messageID"] == "msg_a_old"
    assert ups[-1]["returned"]["task_id"] == "j9"


def _bash_call(uuid, call_id, cmd):
    call = _assistant(uuid, "")
    call["message"]["content"] = [{"type": "tool_use", "id": call_id, "name": "Bash",
                                   "input": {"command": cmd, "description": "跑测试", "run_in_background": True}}]
    return call


BG_NOTIF_BODY = (
    "<task-notification>\n<task-id>j1</task-id>\n<tool-use-id>call1</tool-use-id>\n"
    "<output-file>/tmp/tasks/j1.output</output-file>\n<status>completed</status>\n"
    "<summary>Background command \"跑测试\" completed (exit code 0)</summary>\n</task-notification>"
)


def test_history_midturn_attachment_attaches_to_step_without_source_message(adapter, tmp_path):
    ad, sid = adapter
    path = tmp_path / "t.jsonl"
    rows = [_user("u1", "跑一下"), _bash_call("a1", "call1", "python3 check.py"),
            result("call1", "Command running in background with ID: j1. Output is being written to: /tmp/x"),
            {"type": "attachment", "uuid": "att1", "timestamp": "2026-09-08T06:46:58.486Z",
             "attachment": {"type": "queued_command", "commandMode": "task-notification", "prompt": BG_NOTIF_BODY}},
            _assistant("a2", "跑完了")]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    msgs = ad._group_transcript(str(path), sid, "engine")
    assert msgs == ad._group_transcript(str(path), sid, "engine")
    assert not any(m["info"]["role"] == "tool" for m in msgs)
    assert not any(m["info"].get("aside") for m in msgs), "回合中途的通知没有自己的一轮"
    step = next(p for m in msgs for p in m["parts"] if p["type"] == "tool")
    assert step["returned"]["task_id"] == "j1" and step["returned"]["exit_code"] == 0
    assert step["returned"]["source"]["kind"] == "script"
    assert step["state"]["output"].startswith("Command running")
    assert len([m for m in msgs if ad._turn_start(m)]) == 1


def test_history_idle_notification_attaches_and_keeps_continuation(adapter, tmp_path):
    ad, sid = adapter
    path = tmp_path / "t.jsonl"
    rows = [_user("u1", "跑一下"), _bash_call("a1", "call1", "python3 check.py"),
            result("call1", "Command running in background with ID: j1."), _assistant("a2", "已派出"),
            _user("n1", BG_NOTIF_BODY, origin={"kind": "task-notification"}),
            _assistant("a3", "看了输出,没问题")]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    msgs = ad._group_transcript(str(path), sid, "engine")
    assert not any(m["info"]["role"] == "tool" for m in msgs)
    step = next(p for m in msgs for p in m["parts"] if p["type"] == "tool")
    assert step["returned"]["task_id"] == "j1"
    aside = [m for m in msgs if m["info"].get("aside")]
    assert len(aside) == 1 and aside[0]["parts"][0]["text"] == "看了输出,没问题"


def test_list_clips_returned_text_and_part_keeps_full(adapter):
    ad, sid = adapter
    # 回执里得留着那个句柄:读记录时会按它复核这条回填还算不算数(见 settle_stored)。
    step = {"id": "p1", "type": "tool", "tool": "Agent", "call_id": "c1",
            "state": {"status": "done", "output": "Agent started with ID: t1"},
            "returned": {"status": "done", "text": "报告" * 9000, "task_id": "t1"}}
    ad._append_msg(sid, {"info": {"id": "m", "role": "assistant"}, "parts": [step]})
    rows = ad._trim_outputs(ad.messages(sid))
    p = rows[0]["parts"][0]
    assert len(p["returned"]["text"]) == ad.LIST_OUTPUT_CAP and p["returnedMore"] == 18000
    assert len(ad.part(sid, "p1")["returned"]["text"]) == 18000


def test_exit_code_parsed_from_summary_and_attach_merges():
    card = from_notification(_bg_notif(status="completed", summary='Background command "x" completed (exit code 3)'))
    assert card["exit_code"] == 3
    step = {"type": "tool", "call_id": "tu_0", "state": {}}
    attach_return(step, from_notification({"task_id": "bash:j1", "status": "completed", "summary": "完成",
                                           "result": "完整报告"}))
    attach_return(step, from_notification({"task_id": "bash:j1", "status": "completed", "summary": "完成"}))
    assert step["returned"]["text"] == "完整报告", "摘要不盖报告"
    assert find_tool_part([step], "", "bash:j1") is None
    step["state"]["output"] = "Command running in background with ID: j1. done"
    assert find_tool_part([step], "", "bash:j1") is step
    assert find_tool_part([step], "tu_0", "") is step


# ── 同步命令的通知只是回声 ─────────────────────────────────────────────────
# Claude Code 对前台 Bash 也发一帧 task_notification,正文就是描述文本、不带退出码。
# 照单回填的话,一排从头到尾都是同步的命令会在行上写「已返回」——那个词说的是
# 「它回来了」,而它根本没走开。2026-09-09 复核本机记录:42 条回填里 13 条是这么来的。
#
# 判据取「这一步有没有交出句柄」:后台命令的回执是 ID/task,结果必须另外送回;同步命令的
# 回执就是结果本身。两类在本机 42 条上零交叉。

def _sync_notif(call_id="tu_0", task="bash:j7"):
    """前台命令跑完后引擎发来的那一帧:summary 就是描述文本,没有 exit code。"""
    return {"type": "system", "subtype": "task_notification", "task_id": task,
            "status": "completed", "summary": "跑全套并盘点工作树", "tool_use_id": call_id}


def test_handoff_criterion_separates_sync_from_async():
    from engine_adapters.execution_returns import handed_off
    bg = {"state": {"output": "Command running in background with ID: j1. Output is being…"}}
    mon = {"state": {"output": "Monitor started (task j2, timeout 240000ms). You will…"}}
    sync = {"state": {"output": "FAILED desktron/tests/test_x.py::A::b\n1 failed"}}
    assert handed_off(bg, {"task_id": "bash:j1"})
    assert handed_off(mon, {"task_id": "j2"})
    assert not handed_off(sync, {"task_id": "bash:j7"})
    # 还没有自己结果的那一步在等这条通知,不能算同步
    assert handed_off({"state": {"output": ""}}, {"task_id": "j1"})


def test_live_sync_notification_neither_marks_step_nor_makes_card(adapter):
    ad, sid = adapter
    ctx = context(sid)
    ad.handle_event(_init(), ctx)
    ad.handle_event(_tool(0, "Bash"), ctx)
    ad.handle_event(result("tu_0", "FAILED desktron/tests/test_x.py::A::b\n1 failed"), ctx)
    ad.handle_event(_sync_notif(), ctx)
    parts = list(ctx["parts"].values())
    assert [p["type"] for p in parts] == ["tool"]        # 没长出第二块
    assert "returned" not in parts[0]                    # 行上仍是「已执行」
    assert parts[0]["state"]["output"].startswith("FAILED")


def test_live_background_notification_still_marks_step(adapter):
    """对照组:交了句柄的那一步照旧标「已返回」,这条判据不许把后台任务一起收走。"""
    ad, sid = adapter
    ctx = context(sid)
    ad.handle_event(_init(), ctx)
    ad.handle_event(_tool(0, "Bash"), ctx)
    ad.handle_event(result("tu_0", "Command running in background with ID: j1. Output→/tmp/x"), ctx)
    ad.handle_event(_bg_notif(), ctx)
    parts = list(ctx["parts"].values())
    assert [p["type"] for p in parts] == ["tool"]
    assert parts[0]["returned"]["exit_code"] == 0


def test_stored_sync_step_is_left_alone(adapter):
    """已经落盘的那一轮同样不改:刷新前后必须同形,否则历史里那一行会自己变个说法。"""
    step = {"id": "p1", "type": "tool", "tool": "Bash", "call_id": "c9",
            "state": {"status": "done", "input": {"command": "python3 -m pytest", "timeout": 600000},
                      "output": "8213 行测试输出"}}
    ad, sid = adapter
    ad._append_msg(sid, {"info": {"id": "msg_a_old", "role": "assistant", "sessionID": sid,
                                  "at": 1}, "parts": [step]})
    ctx = context(sid)
    ad.handle_event(_sync_notif(call_id="c9", task="bash:j7"), ctx)
    assert ctx["parts"] == {}                            # 没有另起返回卡
    assert "returned" not in ad.messages(sid)[-1]["parts"][0]
    assert not [e for e in ad.bus.events if e.get("type") == "message.part.updated"]


# ── 通知先于这一步自己的结果到达 ───────────────────────────────────────────
# 上面那条判据只在「这一步已经有自己的输出」时才判得准。实际帧序不保证:2026-09-10 实见
# 前台 Bash 的 task_notification 先于它自己的 tool_result 到达,判断的那一刻输出还是空的,
# 于是走了「还没有自己结果的不算同步」这条放行,行上写出「已返回」并着了紫色。
#
# 那次放行改成暂判(returned.pending),等这一步自己的结果落地再按同一条判据复核一次:
# 里面没有那个句柄就撤回。复核也用在读记录的时候,自愈此前落下的那一批——本机 56 条
# 落盘回填里 21 条会被撤回,全部是 source.kind 为 script 的同步执行(2026-09-10 试算)。

def test_notification_before_result_retracts_when_sync_output_lands(adapter):
    ad, sid = adapter
    ctx = context(sid)
    ad.handle_event(_init(), ctx)
    ad.handle_event(_tool(0, "Bash"), ctx)
    ad.handle_event(_sync_notif(), ctx)                  # 通知先到,此刻输出还是空的
    step = list(ctx["parts"].values())[0]
    assert step["returned"]["pending"] is True           # 只是暂判,不是定论
    ad.handle_event(result("tu_0", "FAILED desktron/tests/test_x.py::A::b\n1 failed"), ctx)
    assert "returned" not in step                        # 结果里没有句柄:撤回
    assert [p["type"] for p in ctx["parts"].values()] == ["tool"]


def test_notification_before_result_stands_for_real_background(adapter):
    """对照组:结果落地后句柄就在里面,回填留下,暂判的标记清掉。"""
    ad, sid = adapter
    ctx = context(sid)
    ad.handle_event(_init(), ctx)
    ad.handle_event(_tool(0, "Bash"), ctx)
    ad.handle_event(_bg_notif(), ctx)
    ad.handle_event(result("tu_0", "Command running in background with ID: j1. Output→/tmp/x"), ctx)
    step = list(ctx["parts"].values())[0]
    assert step["returned"]["exit_code"] == 0
    assert "pending" not in step["returned"]


def test_settle_return_waits_while_output_is_still_empty():
    from engine_adapters.execution_returns import attach_return, settle_return
    step = {"type": "tool", "state": {"output": ""}}
    attach_return(step, {"task_id": "j1", "text": "报告", "status": "done"})
    assert settle_return(step) is False                  # 还没有结果,维持暂判
    assert step["returned"]["pending"] is True
    step["state"]["output"] = "Monitor started (task j1, timeout 240000ms)."
    assert settle_return(step) is True
    assert step["returned"]["text"] == "报告" and "pending" not in step["returned"]


def test_stored_records_heal_on_read(adapter):
    """此前落下的那一批在读记录时按同一条判据撤回,不必迁移文件;真交出去过的那条不动。"""
    ad, sid = adapter
    sync = {"id": "p1", "type": "tool", "tool": "Bash", "call_id": "c9",
            "state": {"status": "done", "output": "8213 行测试输出"},
            "returned": {"task_id": "b9bugs56h", "call_id": "c9", "status": "done",
                         "delivery": "task_notification", "content_kind": "summary",
                         "text": "找那一步的落盘记录", "source": {"kind": "script", "name": "脚本执行"}}}
    bg = {"id": "p2", "type": "tool", "tool": "Bash", "call_id": "c8",
          "state": {"status": "done", "output": "Command running in background with ID: j1."},
          "returned": {"task_id": "bash:j1", "call_id": "c8", "status": "done",
                       "delivery": "task_notification", "text": "跑完了",
                       "source": {"kind": "command", "name": "后台任务"}}}
    ad._append_msg(sid, {"info": {"id": "msg_a_old", "role": "assistant", "sessionID": sid,
                                  "at": 1}, "parts": [sync, bg]})
    parts = ad.messages(sid)[-1]["parts"]
    assert "returned" not in parts[0]
    assert parts[1]["returned"]["text"] == "跑完了"
