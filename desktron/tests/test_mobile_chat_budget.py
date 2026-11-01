"""手机聊天页的 DOM 与隐藏页预算。真实 WebKit 用 DESK_WEBKIT_TESTS=1 启用。"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGE = (ROOT / "frontend/app-chat-page.js").read_text(encoding="utf-8")
DETAIL = (ROOT / "frontend/app-chat-detail.js").read_text(encoding="utf-8")
CORE = (ROOT / "frontend/app-core.js").read_text(encoding="utf-8")


def source_function(source, name):
    hit = re.search(r"\n(?:async )?function " + re.escape(name) + r"\(.*?\n\}", source, re.S)
    assert hit, name
    return hit.group()


def function(name):
    return source_function(PAGE, name)


def declarations():
    names = ("HS_WARM_MAX", "HS_PHONE_WARM_MAX", "HS_DOM_TURNS", "HS_PHONE_DOM_TURNS",
             "HS_PHONE_HIDDEN_DOM_TURNS")
    return "\n".join(re.search(r"^const " + name + r" = .*?;$", PAGE, re.M).group()
                     for name in names)


def budget_source():
    return declarations() + "\n" + "\n".join(function(name) for name in (
        "hsEvictCold", "hsTrimDom"))


def test_hidden_pages_defer_config_until_shown():
    build = function("hsBuild")
    shown = function("hsShown")
    assert "if (!v.ephemeral && hsHidden(v)) v.needConfig = true" in build
    assert "else v.configReady = hsEngineSync(v)" in build
    assert "v.configReady = hsEngineSync(v)" in shown
    composer = (ROOT / "frontend/app-chat-composer.js").read_text(encoding="utf-8")
    assert "if (v.configReady) await v.configReady" in source_function(composer, "hsSend")
    assert "if (v.configReady) await v.configReady" in source_function(composer, "hsDispatch")


def test_history_batches_are_trimmed_after_pour():
    assert "hsTrimDom(v);" in function("hsPrepend")
    reconcile = (ROOT / "frontend/app-chat-reconcile.js").read_text(encoding="utf-8")
    assert "if (!v.pouring) hsTrimDom(v)" in source_function(reconcile, "hsPour")
    remount = function("hsRemount")
    assert "if (v.mobileTrimmed)" in remount
    assert "hsMore(v)" in remount
    overlays = (ROOT / "frontend/app-chat-overlays.js").read_text(encoding="utf-8")
    assert 'if (!was && typeof hsTrimDom === "function") hsTrimDom(v)' in source_function(overlays, "hsSetStick")


def test_reopening_trimmed_phone_page_does_not_fetch_older_history():
    """重新点开会话只恢复现有 25 回合；不能把旧阅读状态当成一次向上翻页。"""
    shown = function("hsShown")
    assert "v.domTrimmed && !v.mobileTrimmed && !v.stick" in shown
    assert "else if (v.mobileTrimmed" not in shown
    # 明确的向上操作仍经 hsUnstick -> hsRemount -> hsMore 补回已释放历史。
    overlays = (ROOT / "frontend/app-chat-overlays.js").read_text(encoding="utf-8")
    assert "hsRemount(v);" in source_function(overlays, "hsUnstick")
    assert "if (v.mobileTrimmed)" in function("hsRemount")
    assert "hsMore(v)" in function("hsRemount")


@pytest.mark.skipif(not shutil.which("node"), reason="需要 node 跑会话露面函数")
def test_shown_runtime_only_remounts_locally_retained_turns():
    script = function("hsShown") + r'''
const assert=require("node:assert/strict"); let _hsWarmClock=0;
function hsSeen(){} function hsSituationSync(){} function hsViewportChange(v,fn){fn();}
function hsEvictColdSoon(){} let remounts=0; function hsRemount(){remounts++;}
function view(mobileTrimmed){return {warmAt:0,needConfig:false,needMeta:false,needLoad:false,
  agentLogOwed:false,domTrimmed:true,mobileTrimmed,stick:false,pendRest:null};}
const phone=view(true);hsShown(phone);assert.equal(remounts,0);
const desktop=view(false);hsShown(desktop);assert.equal(remounts,1);
'''
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_late_updates_for_released_turns_do_not_create_orphan_turns():
    core = (ROOT / "frontend/app-chat-core.js").read_text(encoding="utf-8")
    apply = source_function(core, "hsApply")
    assert "info.id && v.mobileDroppedMids" in apply  # user/assistant 共用的早期闸
    assert apply.count("v.mobileDroppedMids") >= 3
    assert apply.count("if (v.needLoad) return") >= 3
    reconcile = (ROOT / "frontend/app-chat-reconcile.js").read_text(encoding="utf-8")
    pour = source_function(reconcile, "hsPour")
    assert "v.mobileDroppedMids.delete(info.id)" in pour
    trim = function("hsTrimDom")
    assert "v.findHits = []; v.findAt = 0; hsFindPaint(v)" in trim
    assert "hsFindRun(v, findQ, false)" in trim
    evict = function("hsEvictCold")
    assert "v.asideOf = new Map()" in evict
    assert "v.mobileDroppedMids = new Set()" in evict
    assert "v.mobileTrimmed = false" in evict
    assert "v.historyLoadEpoch = (v.historyLoadEpoch || 0) + 1" in evict
    assert "hsHistoryLoading(v, false)" in evict


@pytest.mark.skipif(not shutil.which("node"), reason="需要 node 跑前端预算函数")
def test_phone_keeps_three_warm_pages_and_25_attached_turns_without_dropping_index():
    script = budget_source() + r'''
const assert=require("node:assert/strict");
let phone=true; function hsPhoneInput(){return phone;}
function hsHidden(v){return !!v.hidden;}
function hsFlush(){} function hsIndex(){} function hsViewportChange(v,fn){fn();}
function hsHistoryLoading(){}
const _hsViews=new Map();
function view(i){return {ephemeral:false,needLoad:false,warmAt:i,hidden:i<5,busy:false,
  el:{isConnected:true},stream:{innerHTML:"full"},turns:[{}],roles:new Map(),toc:{innerHTML:"x"}};}
for(let i=0;i<6;i++)_hsViews.set(String(i),view(i));
const keep=_hsViews.get("5"); hsEvictCold(keep);
assert.deepEqual([..._hsViews].filter(([,v])=>!v.needLoad).map(([id])=>id),["3","4","5"],
  "最近三个手机会话应保持热页，第四个起按 LRU 回收");
phone=false;
for(const v of _hsViews.values()){v.needLoad=false;v.turns=[{}];v.stream.innerHTML="full";}
hsEvictCold(keep);
assert.deepEqual([..._hsViews].filter(([,v])=>!v.needLoad).map(([id])=>id),["1","2","3","4","5"],
  "桌面端热页预算必须保持五个");
phone=true;
const turns=Array.from({length:40},()=>{const el={parentNode:{},remove(){this.parentNode=null;}};return {el};});
const v={pouring:false,stick:true,turns}; hsTrimDom(v);
assert.equal(v.turns.length,40);
assert.equal(v.mobileTrimmed,undefined);
assert.equal(v.domTrimmed,true);
assert.equal(v.turns.filter(t=>t.el.parentNode).length,25);
// 隐藏后连同前台已摘下的节点一起释放，不能漏掉 detached DOM。
v.hidden=true; hsTrimDom(v);
assert.equal(v.turns.length,25);
assert.equal(v.turns.filter(t=>t.el.parentNode).length,25);
assert.equal(v.mobileTrimmed,true);
assert.equal(turns[0].el,null);
const hiddenTurns=Array.from({length:40},()=>{const el={parentNode:{},remove(){this.parentNode=null;}};return {el,parts:new Map()};});
const hiddenView={pouring:false,stick:false,hidden:true,busy:true,phoneHistoryOpen:true,turns:hiddenTurns};
hsTrimDom(hiddenView);
assert.equal(hiddenView.turns.length,25);
assert.equal(hiddenView.turns.filter(t=>t.el.parentNode).length,25);
assert.equal(hiddenView.phoneHistoryOpen,false);
// 运行中的后台会话切回仍有25轮，贴底时无需等下一次对账补刻度。
hiddenView.hidden=false; hiddenView.stick=true; hsTrimDom(hiddenView);
assert.equal(hiddenView.turns.length,25);
phone=false;
const desktop=Array.from({length:41},()=>{const el={parentNode:{},remove(){this.parentNode=null;}};return {el};});
hsTrimDom({pouring:false,stick:true,turns:desktop});
assert.equal(desktop.filter(t=>t.el.parentNode).length,40);
'''
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="需要 node 跑前端预算函数")
def test_phone_reading_window_survives_live_updates_but_releases_when_hidden():
    script = budget_source() + function("hsRemount") + function("hsMore") + r'''
const assert=require("node:assert/strict");
function hsPhoneInput(){return true;} function hsHidden(v){return !!v.hidden;}
function hsViewportChange(v,fn){fn();} function hsIndex(){} function hsFlush(){}
function hsHistoryLoading(){}
function hsTocFade(){} const _hsViews=new Map();
function turn(){return {el:{parentNode:{},remove(){this.parentNode=null;}},parts:new Map()};}
const v={turns:Array.from({length:25},turn),stick:true,pouring:false,el:{isConnected:true}};
// 即使起初不足25轮、没有已摘节点，阅读意图也要保留。
hsRemount(v); assert.equal(v.phoneHistoryOpen,true);
v.turns.push(turn());hsTrimDom(v);assert.equal(v.turns.length,26);
assert.equal(v.turns.filter(t=>t.el.parentNode).length,26);
// 分页同样在插入历史前设置保护标记，不能在回底期间收到响应又裁掉。
v.phoneHistoryOpen=false;
let calls=0;
async function api(){calls++;return [{}];}
function hsPrepend(view,msgs){
  assert.equal(view.phoneHistoryOpen,true);
  view.turns.unshift(...Array.from({length:30},turn));hsTrimDom(view);
}
(async()=>{
  await hsMore(v);assert.equal(calls,1);assert.equal(v.turns.length,56);
  assert.equal(v.turns.filter(t=>t.el.parentNode).length,56);
  v.hidden=true;v.busy=true;v.warmAt=0;
  const keep={el:{isConnected:true},warmAt:4,turns:[turn()]};
  _hsViews.set('old',v);_hsViews.set('keep',keep);
  _hsViews.set('warm2',{el:{isConnected:true},warmAt:3,turns:[turn()]});
  _hsViews.set('warm3',{el:{isConnected:true},warmAt:2,turns:[turn()]});
  hsEvictCold(keep);
  assert.equal(v.turns.length,25);assert.equal(v.phoneHistoryOpen,false);
  assert.equal(v.turns.filter(t=>t.el.parentNode).length,25);
  v.hidden=false;hsTrimDom(v);assert.equal(v.turns.length,25);
  v.hidden=true;v.busy=false;v.phoneHistoryOpen=true;
  v.stream={innerHTML:'body'};v.toc={innerHTML:'ticks'};
  hsEvictCold(keep);
  assert.equal(v.needLoad,true);assert.equal(v.phoneHistoryOpen,false);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    for name in ("hsLoad", "hsLoadAnchor"):
        assert "v.phoneHistoryOpen = false" in function(name)


@pytest.mark.skipif(not shutil.which("node"), reason="需要 node 跑手机冷页揭示函数")
def test_phone_cold_history_stays_hidden_until_final_batch_then_reveals_once():
    reconcile = (ROOT / "frontend/app-chat-reconcile.js").read_text(encoding="utf-8")
    reveal = function("hsHistoryReveal")
    loading = function("hsHistoryLoading")
    fast = source_function(reconcile, "hsPourFast")
    rest = source_function(reconcile, "hsPourRest")
    assert 'v.el.classList.toggle("hs-history-loading", !!on)' in loading
    assert 'v.body.setAttribute("aria-busy", "true")' in loading
    assert 'status.hidden = !on' in loading
    assert "if (!cut) { hsPour(v, list); hsHistoryReveal(v); return; }" in fast
    assert "else { hsHistoryReveal(v); hsReconcileAfterLoad(v); }" in rest
    assert "!v.historyReady" in reveal
    load = function("hsLoad")
    assert load.count("hsHistoryReveal(v)") == 1
    assert 'await apprPoll()' in load
    assert load.index('await apprPoll()') < load.index("apprRenderView(v)") < load.index("hsHistoryReveal(v)")
    assert "if (!v.historyLoading) { hsSetStick(v, true); hsPin(v); }" in load
    overlays = (ROOT / "frontend/app-chat-overlays.js").read_text(encoding="utf-8")
    viewport_change = source_function(overlays, "hsViewportChange")
    assert "epoch === s.epoch && !v.historyRevealing" in viewport_change
    assert reveal.index("v.historyRevealing = true") < reveal.index("hsSetStick(v, true)")
    assert reveal.index("hsSetStick(v, true)") < reveal.index("v.historyRevealing = false")
    script = loading + reveal + r'''
const assert=require("node:assert/strict");
let classes=new Set(["hs-history-loading"]), measures=0, indices=0, commits=0;
function hsPhoneInput(){return true;}
function hsMeasureBubbles(){measures++;} function hsIndex(){indices++;}
function hsSetStick(v,on){v.stick=on;if(on)commits++;}
const v={historyLoading:true,historyReady:true,historyLoadEpoch:1,pendRest:[{}],
  el:{classList:{toggle(n,on){on?classes.add(n):classes.delete(n);}},querySelector(){return null;}},
  body:{setAttribute(){},removeAttribute(){}}};
hsHistoryReveal(v);assert.equal(v.historyLoading,true,"unfinished history was exposed");
v.pendRest=null;hsHistoryReveal(v);
assert.equal(v.historyLoading,false);assert.equal(classes.has("hs-history-loading"),false);
assert.deepEqual([measures,indices,commits],[1,1,1]);
hsHistoryReveal(v);assert.deepEqual([measures,indices,commits],[1,1,1],"history revealed more than once");
'''
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    build = function("hsBuild")
    assert 'class="hs-history-status" role="status" aria-live="polite"' in build
    css = (ROOT / "frontend/style.css").read_text(encoding="utf-8")
    assert ".hs-page.hs-history-loading .hs-stream { visibility: hidden; }" in css
    assert ".hs-page.hs-history-loading .hs-body { pointer-events: none; }" in css


@pytest.mark.skipif(not shutil.which("node"), reason="需要 node 跑视口控制函数")
def test_reveal_with_sse_overflow_still_commits_bottom_once():
    overlays = (ROOT / "frontend/app-chat-overlays.js").read_text(encoding="utf-8")
    funcs = "\n".join(source_function(overlays, name) for name in ("hsViewportChange", "hsSetStick"))
    script = funcs + r'''
const assert=require("node:assert/strict");let commits=0;
function hsViewport(v){return v.viewport;} function hsViewportAnchor(){return null;}
function hsMeasureBubbles(){} function hsRestoreViewportAnchor(){} function hsViewportCommit(){}
function hsViewportCancel(){} function hsScrollCommit(){commits++;} function hsViewportSchedule(){}
function hsTrimDom(v){hsViewportChange(v,()=>{},false,"trim-sse");}
const v={viewport:{depth:0,epoch:0},historyRevealing:true,stick:false,
  body:{classList:{toggle(){}}},down:{classList:{toggle(){}}},busy:false};
hsSetStick(v,true);assert.equal(commits,1);
'''
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc")
                    or os.environ.get("DESK_WEBKIT_TESTS") != "1", reason="需启用 WebKit 验收")
@pytest.mark.desktop
def test_webkit_cold_history_is_hidden_until_one_final_bottom_commit(tmp_path):
    script = function("hsHistoryLoading") + function("hsHistoryReveal") + r'''
const root=document.querySelector("#root"),body=document.querySelector("#body"),stream=document.querySelector("#stream");
let commits=0,measures=0,indices=0;
function hsPhoneInput(){return true;} function hsMeasureBubbles(){measures++;} function hsIndex(){indices++;}
function hsSetStick(v,on){v.stick=on;if(on){commits++;v.body.scrollTop=v.body.scrollHeight;}}
const v={el:root,body,stream,historyReady:false,pendRest:[1],stick:false};
hsHistoryLoading(v,true);
const hidden=[];
for(let batch=0;batch<5;batch++){
  for(let i=0;i<5;i++){
    const row=document.createElement("article");row.textContent=("历史正文 "+batch+" ").repeat(40);stream.prepend(row);
  }
  void body.scrollHeight;hidden.push(getComputedStyle(stream).visibility);
  hsHistoryReveal(v);
}
v.pendRest=null;v.historyReady=true;hsHistoryReveal(v);
const gap=body.scrollHeight-body.clientHeight-body.scrollTop;
window.webkit.messageHandlers.result.postMessage({hidden,visible:getComputedStyle(stream).visibility,
  blocked:getComputedStyle(body).pointerEvents,busy:body.hasAttribute("aria-busy"),commits,measures,indices,gap,width:body.clientWidth});
'''
    html = ("<html><meta charset=utf-8><style>" + (ROOT / "frontend/style.css").read_text(encoding="utf-8")
            + "#root,#body{width:390px!important;max-width:390px!important}#body{height:520px;overflow-y:auto}article{padding:12px}</style>"
            + '<section id="root" class="hs-page"><div class="hs-wrap"><div id="body" class="hs-body">'
            + '<main id="stream" class="hs-stream"></main></div></div></section><script>'
            + script.replace("</script", "<\\/script") + "</script></html>")
    page = tmp_path / "cold-history.html"
    page.write_text(html, encoding="utf-8")
    binary = tmp_path / "cold-history"
    subprocess.run(["swiftc", str(Path(__file__).parent / "fixtures/chat_latency_webkit.swift"),
                    "-o", str(binary)], capture_output=True, text=True, check=True, timeout=90)
    result = subprocess.run([str(binary), str(page)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["width"] == 390
    assert data["hidden"] == ["hidden"] * 5
    assert data["visible"] == "visible"
    assert data["blocked"] == "auto"
    assert data["busy"] is False
    assert data["commits"] == data["measures"] == data["indices"] == 1
    assert abs(data["gap"]) <= 1


@pytest.mark.skipif(not shutil.which("node"), reason="需要 node 跑手机历史恢复函数")
def test_phone_history_restore_can_retry_after_request_failure():
    script = function("hsRemount") + function("hsMore") + r'''
const assert=require("node:assert/strict");
function hsPhoneInput(){return true;} function hsHidden(){return false;}
function hsTocFade(){} let attempts=0, prepends=0;
async function api(){attempts++;if(attempts===1)throw new Error("offline");return [{info:{role:"user"}}];}
function hsPrepend(){prepends++;}
const v={sid:"s",turns:Array.from({length:25},()=>({})),mobileTrimmed:true,
  moreBusy:false,moreDone:false,busy:false,pendRest:null,historyNewer:0};
(async()=>{
  hsRemount(v);await new Promise(resolve=>setImmediate(resolve));
  assert.equal(attempts,1);assert.equal(v.mobileTrimmed,true);assert.equal(v.moreBusy,false);
  hsRemount(v);await new Promise(resolve=>setImmediate(resolve));
  assert.equal(attempts,2);assert.equal(prepends,1);assert.equal(v.mobileTrimmed,false);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc")
                    or os.environ.get("DESK_WEBKIT_TESTS") != "1", reason="需启用 WebKit 验收")
@pytest.mark.desktop
def test_390px_webkit_detaches_old_turns_and_hidden_pages(tmp_path):
    overlays = (ROOT / "frontend/app-chat-overlays.js").read_text(encoding="utf-8")
    script = budget_source() + function("hsRemount") + source_function(overlays, "hsIndex") + r'''
hsPhoneInput=()=>true;
function hsHidden(v){return !!v.hidden;}
function hsFlush(){} function hsTocSync(){} function hsTocFade(){} function hsHistoryLoading(){}
function hsViewportChange(v,fn){fn();}
const _hsViews=new Map();
const root=document.querySelector("#root");
function page(i){
  const el=document.createElement("section"); el.className="page"; root.appendChild(el);
  const stream=document.createElement("div"); el.appendChild(stream);
  const turns=[];
  for(let n=0;n<40;n++){
    const turn=document.createElement("article"); turn.className="turn";
    for(let k=0;k<40;k++){const tool=document.createElement("div");tool.textContent="tool "+k;turn.appendChild(tool);}
    stream.appendChild(turn); turns.push({el:turn,user:'turn '+n});
  }
  const toc=document.createElement('div');el.appendChild(toc);
  return {ephemeral:false,needLoad:false,warmAt:i,hidden:i<5,busy:false,stick:true,el,stream,turns,
    roles:new Map(),toc};
}
for(let i=0;i<6;i++)_hsViews.set(String(i),page(i));
const keep=_hsViews.get("5"), start=performance.now();
hsEvictCold(keep); hsTrimDom(keep); hsIndex(keep);
const original=keep.turns.map(t=>t.el), ticks=[...keep.toc.children];
let stable=true;
for(let i=0;i<4;i++){
  keep.stick=false;hsRemount(keep);
  stable &&= keep.stream.children.length===40 && original.every((el,n)=>keep.stream.children[n]===el);
  keep.stick=true;hsTrimDom(keep);
  stable &&= keep.stream.children.length===40 && keep.toc.children.length===40
    && ticks.every((el,n)=>keep.toc.children[n]===el) && !keep.mobileTrimmed;
}
const elapsed=performance.now()-start;
const warm=[..._hsViews.values()].filter(v=>!v.needLoad).length;
const turns=keep.turns.filter(t=>t.el.parentNode).length, retained=keep.turns.length;
window.webkit.messageHandlers.result.postMessage({warm,turns,retained,stable,elapsed,nodes:document.querySelectorAll("*").length,width:root.getBoundingClientRect().width});
'''
    html = ("<html><meta charset=utf-8><style>body{margin:0}#root{width:390px}.turn{margin:4px}</style>"
            "<div id=root></div><script>" + script.replace("</script", "<\\/script") + "</script></html>")
    page = tmp_path / "mobile-budget.html"
    page.write_text(html, encoding="utf-8")
    binary = tmp_path / "mobile-budget"
    subprocess.run(["swiftc", str(Path(__file__).parent / "fixtures/chat_latency_webkit.swift"),
                    "-o", str(binary)], capture_output=True, text=True, check=True, timeout=90)
    result = subprocess.run([str(binary), str(page)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    print(json.dumps(data, ensure_ascii=False))
    assert data["width"] == 390
    assert data["warm"] == 3
    assert data["turns"] == 40
    assert data["retained"] == 40
    assert data["stable"] is True
    # 当前页保留完整 40 轮，另外两个热页各保留最近 25 轮；其余冷页只留空壳。
    assert data["nodes"] < 5000
    assert data["elapsed"] < 150, data


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc")
                    or os.environ.get("DESK_WEBKIT_TESTS") != "1", reason="需启用 WebKit 验收")
@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.desktop
def test_webkit_outer_process_toggle_restores_narratives_with_bounded_work(tmp_path, width):
    funcs = [source_function(CORE, "stripFraming")]
    funcs += [source_function(DETAIL, name) for name in (
        "hsLiteHtml", "hsSafeCut", "hsStreamHtml", "hsNarrativeStamp", "hsNarrativePieces",
        "hsStreamNodeSame", "hsStreamNodes", "hsStreamMark", "hsStreamPatchHead", "hsStreamPatchTail",
        "hsStreamPaint", "hsPaint", "hsProcHost", "hsTextAttach")]
    tools = (ROOT / "frontend/app-chat-tools.js").read_text(encoding="utf-8")
    funcs += [source_function(tools, name) for name in ("hsProcBox", "hsProcRepaint")]
    model = (ROOT / "frontend/app-chat-model.js").read_text(encoding="utf-8")
    funcs += [source_function(model, name) for name in (
        "hsOpenNarrative", "hsPaintLive", "hsPaintGap", "hsTailNudge")]
    script = "\n".join(funcs) + r'''
const renderer=window.markdownit({html:false,linkify:true});
let renders=0,chars=0; const md={render(text){renders++;chars+=text.length;return renderer.render(text);}};
const HS_MD_MS=260, HS_ICONS={chev:""};
function hsSetText(el,text){el.textContent=text;} function hsDur(ms){return String(ms);}
function hsThinkFull(){} function hsLinkify(){} function hsCodePaths(){}
function hsProcOpenSet(){return new Set();} function hsProcOpenKey(){return "turn";}
function hsProcOpenSave(){} function hsViewportChange(v,fn){fn();}
(async()=>{
const host=document.querySelector("#root"),v={};
const text=("## 标题\n\n- 很长的思考条目一\n- 很长的思考条目二\n\n").repeat(2500);
const turn={holder:host,view:v,parts:new Map()}, part={type:"reasoning",text,done:true,turn};
turn.parts.set("r",part);
const start=performance.now(); hsPaint(v,part); const collapsedMs=performance.now()-start;
const body=part.el.querySelector(".hs-think-body"), collapsedNodes=body.querySelectorAll("*").length;
const tool=document.createElement("div");tool.textContent="工具步骤";hsProcHost(turn).appendChild(tool);
const commentary={type:"reasoning",text:"阶段性说明",done:true,turn};turn.parts.set("c",commentary);hsPaint(v,commentary);
const bar=turn.procEl.querySelector(".hs-proc-bar");
bar.click();
const openNodes=body.querySelectorAll("*").length, openedRenders=renders;
const cssVisible=getComputedStyle(body).display!=="none" && getComputedStyle(hsProcHost(turn)).display!=="none";
const innerHidden=getComputedStyle(part.el.querySelector(".hs-think-bar")).display==="none";
const order=[...hsProcHost(turn).children].map(el=>el===part.el?"reasoning":el===tool?"tool":"commentary");
const first=body.firstChild, beforeChars=chars;
for(let i=0;i<12;i++){
  hsOpenNarrative(part);part.text+="\n\n后续阶段 "+i;part.settled=true;hsPaint(v,part);
}
const incrementalChars=chars-beforeChars, stablePrefix=body.firstChild===first;
part.done=true;hsPaint(v,part);
const finalRenders=renders;for(let i=0;i<100;i++)hsPaint(v,part);
const repeatedRenders=renders-finalRenders;
const expected=document.createElement("div");expected.innerHTML=renderer.render(part.text.trim()).trim();
const finalMatches=body.innerHTML.replace("<!--hs-live-tail-->","")===expected.innerHTML;
bar.click();
const releasedNodes=body.querySelectorAll("*").length, foldedRenders=renders;
for(let i=0;i<100;i++){hsOpenNarrative(part);part.text+="新字";hsPaintLive(v,part);}
const hiddenRenders=renders-foldedRenders;
bar.click();
const reopened=body.textContent.endsWith("新字".repeat(100));
// 真实长文本节流：窗口内先直写尾巴，随即收起应撤销已排队的补绘。
part.paintAt=performance.now();part.text+="节流尾巴";hsPaintLive(v,part);
const nudged=body.textContent.endsWith("节流尾巴"),queued=!!part.paintT;
bar.click();const beforeWait=renders;
await new Promise(resolve=>setTimeout(resolve,450));
const quietAfterFold=renders===beforeWait && !part.paintT && !body.firstChild;
bar.click();const tailRestored=body.textContent.endsWith("节流尾巴");
window.webkit.messageHandlers.result.postMessage({collapsedMs,collapsedNodes,openNodes,openedRenders,
  releasedNodes,width:host.getBoundingClientRect().width,cssVisible,innerHidden,order,incrementalChars,
  stablePrefix,repeatedRenders,hiddenRenders,finalMatches,reopened,nudged,queued,quietAfterFold,tailRestored,
  commentaryVisible:commentary.el.textContent.includes("阶段性说明")});
})().catch(e=>window.webkit.messageHandlers.result.postMessage({error:String(e)}));
'''
    vendor = (ROOT / "frontend/vendor/markdown-it.min.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend/style.css").read_text(encoding="utf-8")
    html = ("<html><meta charset=utf-8><style>" + css
            + f"\nbody{{margin:0}}#root{{width:{width}px}}</style><div id=root class=hs-page></div>"
            + "<script>" + vendor + "</script><script>" + script.replace("</script", "<\\/script")
            + "</script></html>")
    page = tmp_path / "reasoning-budget.html"
    page.write_text(html, encoding="utf-8")
    binary = tmp_path / "reasoning-budget"
    subprocess.run(["swiftc", str(Path(__file__).parent / "fixtures/chat_latency_webkit.swift"),
                    "-o", str(binary)], capture_output=True, text=True, check=True, timeout=90)
    result = subprocess.run([str(binary), str(page)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    print(json.dumps(data, ensure_ascii=False))
    assert data["width"] == width
    assert data["collapsedNodes"] == 0
    assert data["openedRenders"] == 2
    assert data["openNodes"] > 1000
    assert data["releasedNodes"] == 0
    assert data["cssVisible"] and data["innerHidden"]
    assert data["order"] == ["reasoning", "tool", "commentary"]
    assert data["incrementalChars"] < 200  # 7万余字稳定前缀不因12次续写重复解析
    assert data["stablePrefix"] and data["finalMatches"]
    assert data["repeatedRenders"] == data["hiddenRenders"] == 0
    assert data["reopened"] and data["commentaryVisible"]
    assert data["nudged"] and data["queued"] and data["quietAfterFold"] and data["tailRestored"]
    assert data["collapsedMs"] < 20, data


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc")
                    or os.environ.get("DESK_WEBKIT_TESTS") != "1", reason="需启用 WebKit 验收")
@pytest.mark.desktop
def test_390px_webkit_folded_subagent_progress_keeps_dom_still(tmp_path):
    quiet = source_function((ROOT / "frontend/app-chat-core.js").read_text(encoding="utf-8"),
                            "hsToolProgressQuiet")
    script = quiet + r'''
const root=document.querySelector("#root"), row=document.createElement("div");
row.className="hs-step";row.innerHTML='<div class="hs-step-h">子代理运行中</div>';root.appendChild(row);
const part={el:row,state:{status:"running",input:{task:"inspect"},output:""}};
let rebuilds=0;const first=row.firstElementChild,start=performance.now();
for(let i=0;i<1000;i++){
  const q={progressOnly:true,state:{status:"running",output:("progress "+i+" ").repeat(160)}};
  part.state=q.state;
  if(!hsToolProgressQuiet(q,part,"running")){rebuilds++;row.innerHTML='<div class="hs-step-h">重建</div>';}
}
const elapsed=performance.now()-start;
row.classList.add("open");
const openChanged=!hsToolProgressQuiet({progressOnly:true,state:{status:"running"}},part,"running");
const terminalChanged=!hsToolProgressQuiet({progressOnly:false,state:{status:"done"}},part,"running");
window.webkit.messageHandlers.result.postMessage({elapsed,rebuilds,firstStable:first===row.firstElementChild,
  openChanged,terminalChanged,nodes:root.querySelectorAll("*").length,width:root.getBoundingClientRect().width});
'''
    html = ("<html><meta charset=utf-8><style>body{margin:0}#root{width:390px}</style><div id=root></div>"
            "<script>" + script.replace("</script", "<\\/script") + "</script></html>")
    page = tmp_path / "folded-subagent.html"
    page.write_text(html, encoding="utf-8")
    binary = tmp_path / "folded-subagent"
    subprocess.run(["swiftc", str(Path(__file__).parent / "fixtures/chat_latency_webkit.swift"),
                    "-o", str(binary)], capture_output=True, text=True, check=True, timeout=90)
    result = subprocess.run([str(binary), str(page)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    print(json.dumps(data, ensure_ascii=False))
    assert data["width"] == 390
    assert data["rebuilds"] == 0
    assert data["firstStable"] is True
    assert data["openChanged"] is True
    assert data["terminalChanged"] is True
    assert data["nodes"] == 2
    assert data["elapsed"] < 50, data


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc")
                    or os.environ.get("DESK_WEBKIT_TESTS") != "1", reason="需启用 WebKit 验收")
@pytest.mark.desktop
def test_390px_webkit_tool_boundaries_only_parse_new_markdown(tmp_path):
    funcs = [source_function(CORE, "stripFraming")]
    funcs += [source_function(DETAIL, name) for name in (
        "hsLiteHtml", "hsSafeCut", "hsStreamHtml", "hsNarrativeStamp", "hsNarrativePieces",
        "hsStreamNodeSame", "hsStreamNodes", "hsStreamMark", "hsStreamPatchHead", "hsStreamPatchTail",
        "hsStreamPaint")]
    script = "\n".join(funcs) + r'''
const renderer=window.markdownit({html:false,linkify:true});
let parsedChars=0, renders=0;
const md={render(text){parsedChars+=text.length;renders++;return renderer.render(text);}};
const HS_MD_MS=260, root=document.querySelector("#root"), part={text:("段落 **稳定前缀**。\n\n").repeat(3000)};
let pieces=hsNarrativePieces(part,true), start=performance.now();
hsStreamPaint(part,root,pieces);
const first=root.firstElementChild, initial=part.text.length;
for(let i=0;i<12;i++){
  part.text += "\n\n## 新片段 "+i+"\n\n"+("新增内容。\n\n").repeat(100);
  part.done=false;part.settled=false;part.mdAt=0;
  hsStreamPaint(part,root,hsNarrativePieces(part,false));
  part.settled=true;
  hsStreamPaint(part,root,hsNarrativePieces(part,true,false));
}
part.done=true;
hsStreamPaint(part,root,hsNarrativePieces(part,true,true));
const expected=document.createElement("div");expected.innerHTML=renderer.render(part.text.trim()).trim();
const actual=root.innerHTML.replace("<!--hs-live-tail-->","");
const canonical=actual===expected.innerHTML,perfParsedChars=parsedChars;
// 工具边界故意落在列表、围栏、引用定义中；实时可分段，终态 DOM 必须回到完整 Markdown。
const complexRoot=document.createElement("div"),complex={text:"- one\n",done:false};
hsStreamPaint(complex,complexRoot,hsNarrativePieces(complex,true,false));
complex.text+="- two\n\n```js\nconst x=1;\n";
hsStreamPaint(complex,complexRoot,hsNarrativePieces(complex,true,false));
complex.text+="const y=2;\n```\n\n[link][id]\n\n[id]: https://example.com";complex.done=true;
hsStreamPaint(complex,complexRoot,hsNarrativePieces(complex,true,true));
const complexExpected=document.createElement("div");complexExpected.innerHTML=renderer.render(complex.text.trim()).trim();
const complexCanonical=complexRoot.innerHTML.replace("<!--hs-live-tail-->","")===complexExpected.innerHTML;
const elapsed=performance.now()-start;
window.webkit.messageHandlers.result.postMessage({elapsed,parsedChars:perfParsedChars,renders,initial,final:part.text.length,
  canonical,complexCanonical,firstStable:first===root.firstElementChild,nodes:root.querySelectorAll("*").length,
  width:root.getBoundingClientRect().width});
'''
    vendor = (ROOT / "frontend/vendor/markdown-it.min.js").read_text(encoding="utf-8")
    html = ("<html><meta charset=utf-8><style>body{margin:0}#root{width:390px}</style><div id=root></div>"
            "<script>" + vendor + "</script><script>" + script.replace("</script", "<\\/script")
            + "</script></html>")
    page = tmp_path / "markdown-boundaries.html"
    page.write_text(html, encoding="utf-8")
    binary = tmp_path / "markdown-boundaries"
    subprocess.run(["swiftc", str(Path(__file__).parent / "fixtures/chat_latency_webkit.swift"),
                    "-o", str(binary)], capture_output=True, text=True, check=True, timeout=90)
    result = subprocess.run([str(binary), str(page)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    print(json.dumps(data, ensure_ascii=False))
    assert data["width"] == 390
    assert data["firstStable"] is True
    assert data["canonical"] is True
    assert data["complexCanonical"] is True
    # 初始正文、各新增片段的活动/定稿、最终全文各有界一次；不再是 12 次全文。
    growth = data["final"] - data["initial"]
    assert data["parsedChars"] < 2.1 * data["initial"] + 3.2 * growth, data
    assert data["elapsed"] < 500, data
