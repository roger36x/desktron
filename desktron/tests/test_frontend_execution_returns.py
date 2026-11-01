"""执行返回只读卡片与历史对账，不借用用户输入的身份或操作。"""
from pathlib import Path
import subprocess
import re
from frontend_i18n_js import NODE_PRELUDE


ROOT = Path(__file__).resolve().parents[2]
DETAIL = (ROOT / "frontend/app-chat-detail.js").read_text(encoding='utf-8')
MODEL = (ROOT / "frontend/app-chat-model.js").read_text(encoding='utf-8')
RECONCILE = (ROOT / "frontend/app-chat-reconcile.js").read_text(encoding='utf-8')
CSS = (ROOT / "frontend/style.css").read_text(encoding='utf-8')
CORE = (ROOT / "frontend/app-chat-core.js").read_text(encoding='utf-8')


def test_sync_results_are_filtered_before_creating_return_turns():
    guard = re.search(r"function hsSkipPart\(.*?\n}", CORE, re.S).group(0)
    script = guard + r'''
const assert = require("node:assert/strict");
assert.equal(hsSkipPart({type:"tool",tool:"Bash",state:{status:"done"}}),false);
assert.equal(hsSkipPart({type:"execution-result",call_id:"bash-sync",source:{kind:"script"}}),true);
assert.equal(hsSkipPart({type:"execution-result",task_id:"child:0123456789abcdef"}),true);
assert.equal(hsSkipPart({type:"execution-result",task_id:"b123",delivery:"task_notification"}),false);
assert.equal(hsSkipPart({type:"execution-result",task_id:"a123",delivery:"agent_message"}),false);
assert.equal(hsSkipPart({type:"execution-result",task_id:"b123"}),false);
assert.equal(hsSkipPart({type:"execution-result",task_id:"b123",delivery:"sync"}),true);
'''
    subprocess.run(["node", "-e", NODE_PRELUDE + script], check=True, capture_output=True, text=True, encoding='utf-8')


def fn(name):
    return re.search(r"(?:async )?function " + name + r"\(.*?\n}", MODEL + "\n" + RECONCILE, re.S).group(0)


def test_async_return_opens_new_turn_and_late_old_parts_stay_put():
    script = "\n".join(fn(n) for n in ["hsTurnFor", "hsPart", "hsHistoryTurnStart"]) + r'''
const assert = require("node:assert/strict");
function hsNewTurn(v, user) {
  const t = {parts:new Map(),user:user || "",msgId:null,el:{classList:{add(){}}}};
  v.turns.push(t); return t;
}
const v = {turns:[]};
const original = hsNewTurn(v,"启动后台检查");
const old = hsPart(v,"m1","launch","tool");
assert.equal(old.turn,original);
const notification = hsPart(v,"m1","exec_job","execution-result");
assert.equal(v.turns.length,2);
assert.notEqual(notification.turn,original);
assert.equal(notification.turn.user,"");
const reply = hsPart(v,"m1","reply","text");
assert.equal(reply.turn,notification.turn);
assert.equal(hsPart(v,"m1","launch","tool"),old);
assert.equal(hsPart(v,"m1","exec_job","execution-result"),notification);
assert.equal(v.turns.length,2);
const next = hsPart(v,"m1","exec_second","execution-result");
assert.equal(v.turns.length,3);
assert.equal(hsPart(v,"m1","reply2","text").turn,next.turn);
assert.equal(hsPart(v,"m1","exec_job","execution-result"),notification);
assert.equal(hsPart(v,"m1","reply3","text").turn,next.turn);
const user = hsNewTurn(v,"新的用户问题");
assert.equal(hsPart(v,"m2","answer","text").turn,user);
assert.equal(hsHistoryTurnStart({info:{role:"tool"},parts:[{type:"execution-result"}]}),true);
// 历史中的来源消息与助手消息使用不同 ID，也必须合成同一回合。
const replay = {turns:[]};
const source = hsPart(replay,"source","exec_job","execution-result");
assert.equal(hsPart(replay,"response","answer","text").turn,source.turn);
assert.equal(replay.turns.length,1);
'''
    subprocess.run(["node", "-e", NODE_PRELUDE + script], check=True, capture_output=True, text=True, encoding='utf-8')


def test_return_card_is_not_user_bubble_and_has_no_resend():
    card = DETAIL[DETAIL.index("function hsPaintExecution("):DETAIL.index("function hsPaint(v, part)")]
    assert 'part.el.className = "hs-execution"' in card
    assert "hsUserBubbleText" not in card and "prompt_async" not in card
    assert "contenteditable" not in card and "POST" not in card
    assert 'part.turn.el.insertBefore(part.el, part.turn.holder)' in card
    assert 'part.turn.holder.appendChild(part.el)' not in card
    assert 'label + " · "' in card
    assert 'aria-expanded' in card
    assert 'html: false' in card and '.disable("image")' in card
    assert 'body.textContent = text' in card  # 脚本输出不是 HTML。


def test_long_return_fetches_full_body_only_when_expanded():
    assert 'open && part.textMore && !part._fullPending' in DETAIL
    assert 'got.type !== "execution-result"' in DETAIL
    assert 'DeskI18n.bind(toggle, "exec.readFailed")' in DETAIL
    assert '.hs-execution.expanded .hs-execution-body { max-height: 480px; overflow: auto;' in CSS
    assert '.hs-execution-source { min-width: 0; overflow-wrap: anywhere; }' in CSS


def test_async_bubble_shares_peer_sizing_and_keeps_status_by_title():
    sizing = re.search(r"\.hs-page \.hs-u, \.hs-page \.hs-turn-execution > \.hs-execution \{([^}]+)\}", CSS).group(1)
    assert "width: max-content" in sizing
    assert "max-width: 84%" in sizing
    assert "margin-left: auto" in sizing
    peer = re.search(
        r"\.hs-page \.hs-turn-execution > \.hs-execution, \.hs-page \.hs-u-peer \.hs-bubble,\s*"
        r"\.hs-page \.hs-agv-prompt \.hs-bubble \{([^}]+)\}", CSS
    ).group(1)
    assert "box-sizing: border-box" in peer
    assert "padding: 12px 16px" in peer
    head = re.search(r"\n\.hs-execution-head \{([^}]+)\}", CSS).group(1)
    assert "justify-content: flex-start" in head
    assert "flex-wrap: wrap" in head
    assert "space-between" not in head


def test_continuation_is_not_drawn_as_child_agent_speech():
    assert 'if (ev && ev.presentation === "continuation") return null;' in MODEL
    pour = RECONCILE[RECONCILE.index("function hsPour("):]
    assert pour.index("if (info.aside)") < pour.index('if (info.role === "user")')
    assert 'if (info.role === "tool") continue;' in pour


def test_agent_header_shows_status_without_changing_execution_metadata():
    card = DETAIL[DETAIL.index("function hsPaintExecution("):DETAIL.index("function hsPaint(v, part)")]
    script = card + r'''
const assert = require("node:assert/strict");
global.window = {};
function hsDur() { return "21.5s"; }
const nodes = Object.fromEntries([".hs-execution-source", ".hs-execution-status", ".hs-execution-body", ".hs-execution-file", "button"].map(k => [k, {classList:{toggle(){}}}]));
const turn = {el:{}};
const part = {source:{kind:"agent",name:"写一个冒泡排序"},status:"done",duration_ms:21500,exit_code:0,text:"自检通过",turn,
  el:{parentNode:turn.el,querySelector:s=>nodes[s],classList:{toggle(){}}}};
hsPaintExecution({}, part);
assert.equal(nodes[".hs-execution-source"].textContent,"子代理 · 写一个冒泡排序");
assert.equal(nodes[".hs-execution-status"].hidden,false);
assert.equal(nodes[".hs-execution-status"].textContent,"已完成");
part.status = "error";
hsPaintExecution({}, part);
assert.equal(nodes[".hs-execution-status"].textContent,"失败");
part.status = "cancelled";
hsPaintExecution({}, part);
assert.equal(nodes[".hs-execution-status"].textContent,"已取消");
part.status = "done";
assert.equal(part.status,"done");
assert.equal(part.duration_ms,21500);
assert.equal(part.exit_code,0);
part.source.kind = "script";
hsPaintExecution({}, part);
assert.equal(nodes[".hs-execution-source"].textContent,"后台脚本 · 写一个冒泡排序:");
assert.equal(nodes[".hs-execution-status"].hidden,true);
assert.equal(nodes[".hs-execution-status"].textContent,"");
assert.equal(part.status,"done");
assert.equal(part.duration_ms,21500);
assert.equal(part.exit_code,0);
part.source.kind = "command";
hsPaintExecution({}, part);
assert.equal(nodes[".hs-execution-status"].hidden,false);
assert.equal(nodes[".hs-execution-status"].textContent,"已完成 · 退出码 0 · 21.5s");
'''
    subprocess.run(["node", "-e", NODE_PRELUDE + script], check=True, capture_output=True, text=True, encoding='utf-8')


def test_reconciliation_counts_returns_even_when_text_was_clipped():
    functions = RECONCILE[RECONCILE.index("function hsProcessShape("):RECONCILE.index("function hsViewShape(")]
    script = functions + r'''
const assert = require("node:assert/strict");
const full = [{info:{role:"tool"},parts:[{type:"execution-result",text:"x".repeat(5000)}]}];
const clipped = [{info:{role:"tool"},parts:[{type:"execution-result",text:"x".repeat(2000),textMore:5000}]}];
assert.equal(hsShapeSame(hsMessageShape(full),hsMessageShape(clipped)),true);
assert.equal(hsShapeSame(hsMessageShape(full),hsMessageShape([])),false);
assert.equal(hsMessageShape(full).text,0);
assert.equal(hsMessageShape(full).returns,1);
'''
    subprocess.run(["node", "-e", NODE_PRELUDE + script], check=True, capture_output=True, text=True, encoding='utf-8')


# ---- 返回回填到派出它的那一步(2026-09-08) ----
TOOLS = (ROOT / "frontend/app-chat-tools.js").read_text(encoding='utf-8')


def _fn(src, name):
    m = re.search(r"\nfunction %s\(.*?\n}" % re.escape(name), src, re.S)
    assert m, name
    return m.group(0)


def test_step_row_and_detail_show_return_in_place():
    script = "\n".join([_fn(DETAIL, "hsRet"), _fn(DETAIL, "hsReturnVerb"), _fn(DETAIL, "hsReturnStat"),
                        _fn(DETAIL, "hsReturnDetail"),
                        _fn(DETAIL, "hsToolSig"), _fn(DETAIL, "hsDur"), _fn(TOOLS, "hsEsc"), _fn(TOOLS, "hsClip")]) + r'''
const assert = require("node:assert/strict");
const part = {type:"tool",tool:"Bash",state:{status:"done",output:"Command running in background with ID: j1"}};
const before = hsToolSig(part);
part.returned = {status:"done",exit_code:0,duration_ms:361000,output_file:"/tmp/j1.output",text:"<b>报告</b>"};
assert.notEqual(hsToolSig(part), before, "返回到达要让步骤行重建");
assert.equal(hsReturnVerb(part.returned), "已返回");
assert.equal(hsReturnStat(part.returned), "退出码 0 · 361s");
const html = hsReturnDetail(part);
assert.ok(html.includes("已返回 · 退出码 0 · 361s"));
assert.ok(html.includes("输出文件 /tmp/j1.output"));
assert.ok(html.includes("&lt;b&gt;报告&lt;/b&gt;") && !html.includes("<b>报告</b>"), "报告正文按文本转义");
assert.equal(hsReturnVerb({status:"error"}), "返回失败");
assert.equal(hsReturnVerb({status:"cancelled"}), "已取消");
assert.equal(hsReturnDetail({type:"tool"}), "");
'''
    subprocess.run(["node", "-e", NODE_PRELUDE + script], check=True, capture_output=True, text=True, encoding='utf-8')


def test_paint_tool_writes_return_note_and_full_fetch_covers_returned():
    paint = _fn(DETAIL, "hsPaintTool")
    assert 'hsSetText(nEl, ret ? hsReturnVerb(ret)' in paint
    assert 're_.className = "hs-step-ret"' in paint
    assert 'part.el.classList.toggle("ret", !!ret)' in paint
    assert "!part.returnedMore" in DETAIL and "part.returned = got.returned; delete part.returnedMore;" in DETAIL
    assert ".hs-page .hs-step-ret {" in CSS and ".hs-page .hs-tt-ret {" in CSS
    detail = _fn(DETAIL, "hsStepDetail")
    assert "hsReturnDetail(part)" in detail and "hsStepDetailBase(part)" in detail


def test_reconciliation_counts_returned_text_on_tool_steps():
    functions = RECONCILE[RECONCILE.index("function hsProcessShape("):RECONCILE.index("function hsViewShape(")]
    script = functions + r'''
const assert = require("node:assert/strict");
const live = [{info:{role:"assistant"},parts:[{type:"tool",state:{output:"ok"},returned:{text:"x".repeat(5000)}}]}];
const clipped = [{info:{role:"assistant"},parts:[{type:"tool",state:{output:"ok"},returned:{text:"x".repeat(2000)},returnedMore:5000}]}];
const none = [{info:{role:"assistant"},parts:[{type:"tool",state:{output:"ok"}}]}];
assert.equal(hsShapeSame(hsMessageShape(live),hsMessageShape(clipped)),true);
assert.equal(hsShapeSame(hsMessageShape(live),hsMessageShape(none)),false);
'''
    subprocess.run(["node", "-e", NODE_PRELUDE + script], check=True, capture_output=True, text=True, encoding='utf-8')


def det(name):
    return re.search(r"function " + name + r"\(.*?\n}", DETAIL, re.S).group(0)


def test_暂判的回填不写动词不着色也不进签名():
    """通知先于这一步自己的结果到达时,后端只能暂记一笔(pending)。界面照着画的话,
    一条同步命令在整个执行期间都顶着紫色的「已返回」。"""
    script = "\n".join(det(n) for n in ["hsRet", "hsReturnDetail", "hsToolSig"]) + r'''
const assert = require("node:assert/strict");
const done = {status:"done",text:"ok",exit_code:0};
const step = {state:{status:"running",output:"",input:{}}};

assert.equal(hsRet({...step,returned:{...done,pending:true}}),null);
assert.deepEqual(hsRet({...step,returned:done}),done);
assert.equal(hsRet(step),null);

// 展开区同样不提前写那一段
assert.equal(hsReturnDetail({...step,returned:{...done,pending:true}}),"");

// 签名里也不能出现:暂判落地与撤回都不该引起整行重建
assert.equal(hsToolSig({...step,returned:{...done,pending:true}}),hsToolSig(step));
assert.notEqual(hsToolSig({...step,returned:done}),hsToolSig(step));
'''
    subprocess.run(["node", "-e", NODE_PRELUDE + script], check=True, capture_output=True, text=True, encoding='utf-8')
