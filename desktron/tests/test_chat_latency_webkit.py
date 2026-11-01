"""DESK_WEBKIT_TESTS=1 运行；测实际 WebKit 正文布局，非模型首 token 时间。"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc")
                    or os.environ.get("DESK_WEBKIT_TESTS") != "1", reason="需启用 WebKit 验收")
@pytest.mark.desktop
def test_received_text_reaches_real_dom_without_typewriter_delay(tmp_path):
    model = (ROOT / "frontend/app-chat-model.js").read_text()
    detail = (ROOT / "frontend/app-chat-detail.js").read_text()
    core = (ROOT / "frontend/app-core.js").read_text()
    baseline = os.environ.get("DESK_LATENCY_BASELINE", "")
    baseline = "HEAD" if baseline == "1" else baseline

    def fn(name, source):
        return re.search(r"\nfunction " + name + r"\(.*?\n\}", source, re.S).group()

    queue_source = (subprocess.check_output(
        ["git", "show", baseline + ":frontend/app-chat-model.js"], cwd=ROOT, text=True)
        if baseline else model)
    funcs = [fn("hsQueue", queue_source), fn("stripFraming", core)]
    funcs += [fn(n, model) for n in ("hsPaintLive", "hsPaintGap", "hsTailNudge", "hsPaintDone", "hsFlush")]
    funcs += [fn(n, detail) for n in ("hsPaint", "hsLiteHtml", "hsSafeCut", "hsStreamHtml",
              "hsStreamNodeSame", "hsStreamNodes", "hsStreamMark", "hsStreamPatchHead",
              "hsStreamPatchTail", "hsStreamPaint")]
    script = "\n".join(funcs) + r"""
const md=window.markdownit({html:false,linkify:true}), HS_MD_MS=260;
function hsScroll() {} // 只测正文调度/排版，不伪造贴底或网络耗时
window.fixtureStage='script'; window.fixtureFrames=0;
const frame=()=>new Promise(resolve=>requestAnimationFrame(t=>{window.fixtureFrames++;resolve(t)})), rows=[];
const box=document.querySelector('#content');
async function run() {
  window.fixtureStage='first-frame';
  await frame(); await frame();
  window.fixtureStage='cases';
  for (const width of [900,390]) for (const size of [40,1000,20000]) {
    box.style.width=width+'px'; box.replaceChildren();
    const t={holder:box,parts:new Map()}, v={turns:[t]};
    const part={text:'',type:'text',turn:t}; t.parts.set('p',part);
    const text='正文测试'.repeat(Math.ceil(size/4)).slice(0,size);
    const start=performance.now(); let first=null;
    hsQueue(v,part,text);
    while (!part.el || part.el.textContent !== text) {
      await frame();
      if (part.el?.textContent && first===null) first=performance.now()-start;
      if (performance.now()-start>18000) throw new Error('render timeout');
    }
    const rect=part.el.getBoundingClientRect();
    rows.push({width,chars:size,first_ms:first,dom_ms:performance.now()-start,height:rect.height});
    hsFlush(v);
    await frame();
  }
  window.webkit.messageHandlers.result.postMessage({rows});
}
run().catch(e=>window.webkit.messageHandlers.result.postMessage({error:String(e)}));
"""
    vendor = (ROOT / "frontend/vendor/markdown-it.min.js").read_text()
    html = ("<html><meta charset=utf-8><style>body{margin:0;font:16px sans-serif}"
            "#content{overflow-wrap:anywhere}</style><div id=content></div><script>"
            "window.onerror=(m)=>{window.fixtureError=String(m);window.webkit.messageHandlers.result.postMessage({error:String(m)})};"
            "</script><script>"
            + vendor + "</script><script>" + script.replace("</script", "<\\/script") + "</script></html>")
    page = tmp_path / "fixture.html"
    page.write_text(html)
    binary = tmp_path / "fixture"
    subprocess.run(["swiftc", str(Path(__file__).parent / "fixtures/chat_latency_webkit.swift"),
                    "-o", str(binary)], capture_output=True, text=True, check=True, timeout=90)
    result = subprocess.run([str(binary), str(page)], capture_output=True, text=True, timeout=50)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "error" not in data, data
    assert len(data["rows"]) == 6
    for row in data["rows"]:
        assert row["height"] > 0
        if not baseline:
            assert row["dom_ms"] < 150, row
    print(json.dumps(data, ensure_ascii=False))


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc")
                    or os.environ.get("DESK_WEBKIT_TESTS") != "1", reason="需启用 WebKit 验收")
@pytest.mark.desktop
def test_mobile_history_batches_and_async_reasoning_keep_viewport_intent(tmp_path):
    """390px WebKit 下，首屏补历史和长过程全文乱序回填均服从同一视口意图。"""
    overlays = (ROOT / "frontend/app-chat-overlays.js").read_text()
    detail = (ROOT / "frontend/app-chat-detail.js").read_text()

    def fn(name, source=overlays):
        match = re.search(r"\nfunction " + re.escape(name) + r"\(.*?\n\}", source, re.S)
        assert match, name
        return match.group()

    names = ("hsViewport", "hsViewportCancel", "hsViewportWrite", "hsViewportSchedule",
             "hsViewportCommit", "hsViewportChange", "hsSetStick", "hsScrollCommit",
             "hsRestoreViewportAnchor", "hsViewportApplyAnchor", "hsTurnAnchor", "hsTurnAtAnchor",
             "hsBlockPath", "hsBlockAt", "hsViewportAnchor", "hsHoldCapture", "hsHoldAdjust",
             "hsUnstick")
    funcs = [fn(name) for name in names]
    funcs.append(fn("hsThinkFull", detail))
    script = "\n".join(funcs) + r'''
const body=document.querySelector("#chat"), stream=document.querySelector("#stream");
function hsTocSchedule(){} function hsMeasureBubbles(){} function hsRemount(){}
function hsTrimDom(){} function hsResetNarrativeCommit(){}
function hsFollowTarget(el){return el.scrollHeight-el.clientHeight}
const pending=new Map();
function api(url){return new Promise(resolve=>pending.set(url.split("/").pop(),resolve))}
function hsPaint(v,part){part.el.querySelector(".text").textContent=part.text}
let uid=0;
function turn(text, process=false){
  const el=document.createElement("section"); el.className="turn";
  if(process) el.innerHTML='<button class="hs-think-more"></button><div class="text"></div>';
  else el.innerHTML='<div class="text"></div>';
  el.querySelector(".text").textContent=text; stream.appendChild(el);
  return {uid:"t"+(++uid),seq:uid,uat:uid,user:"u",el,parts:new Map()};
}
function gap(){return body.scrollHeight-body.clientHeight-body.scrollTop}
// 无前台图形会话时 WKWebView 会冻结 rAF；读 scrollHeight 是真实 WebKit 的同步布局检查点。
async function settle(){await Promise.resolve();await Promise.resolve();void body.scrollHeight}
async function run(){
  const v={sid:"mobile",body,stream,el:body,turns:[],stick:true,busy:false,lastTop:0};
  // 快速首屏只有尾部四轮；其余历史像 hsPourRest 一样分批向前补。
  for(let i=0;i<4;i++)v.turns.push(turn("尾部回答 "+i+" "+"正文 ".repeat(35)));
  await settle();hsSetStick(v,true);await settle();
  const batchGaps=[];
  for(let batch=0;batch<6;batch++){
    hsViewportChange(v,()=>{
      const made=[];
      for(let i=0;i<5;i++){
        const t=turn("较早历史 "+batch+"-"+i+" "+"内容 ".repeat(45));
        stream.removeChild(t.el);made.push(t);
      }
      const fragment=document.createDocumentFragment();made.forEach(t=>fragment.appendChild(t.el));
      stream.prepend(fragment);v.turns=[...made,...v.turns];
    },false,"prepend");
    await settle();batchGaps.push(gap());
  }
  const parts=[];
  for(let i=0;i<4;i++){
    const t=turn("过程预览 "+i,true);v.turns.push(t);
    const part={id:"r"+i,type:"reasoning",text:"过程预览 "+i,textMore:20000,buf:"",el:t.el,turn:t};
    t.parts.set(part.id,part);parts.push(part);hsThinkFull(v,part);
  }
  hsScrollCommit(v,true);await settle();
  const full="长过程内容用于验证窄屏换行和异步增高。 ".repeat(900),reasoningGaps=[];
  // 乱序回填三块，每次完成真实布局后都必须保持底部。
  for(const i of [2,1,3]){
    pending.get("r"+i)({text:full+i});await settle();reasoningGaps.push(gap());
  }
  // 用户主动上滑后，剩余的更早过程继续增高只能守住阅读锚点，不能抢回底部。
  body.scrollTop=Math.max(0,body.scrollTop-320);hsUnstick(v);
  const held=v.holdAnchor, before=held.el.getBoundingClientRect().top-body.getBoundingClientRect().top;
  pending.get("r0")({text:full+"0"});await settle();
  const after=held.el.getBoundingClientRect().top-body.getBoundingClientRect().top;
  window.webkit.messageHandlers.result.postMessage({width:body.clientWidth,batchGaps,reasoningGaps,
    reading:{stick:v.stick,gap:gap(),offsetDelta:after-before}});
}
run().catch(e=>window.webkit.messageHandlers.result.postMessage({error:String(e),stack:e&&e.stack}));
'''
    html = ("<html><meta charset=utf-8><style>html,body{margin:0}"
            "#chat{width:390px;height:520px;overflow-y:auto;overflow-anchor:none;font:16px/1.5 sans-serif}"
            ".turn{padding:8px 12px;border-bottom:1px solid #ddd}.text{white-space:pre-wrap;overflow-wrap:anywhere}"
            ".hs-think-more{display:block;height:24px}</style>"
            "<div id=chat><main id=stream></main></div><script>"
            "window.onerror=(m,src,line,col,e)=>window.webkit.messageHandlers.result.postMessage("
            "{error:String(m),line,col,stack:e&&e.stack});window.fixtureStage='script';"
            + script.replace("</script", "<\\/script") + "</script></html>")
    page = tmp_path / "mobile-viewport.html"
    page.write_text(html)
    binary = tmp_path / "fixture"
    subprocess.run(["swiftc", str(Path(__file__).parent / "fixtures/chat_latency_webkit.swift"),
                    "-o", str(binary)], capture_output=True, text=True, check=True, timeout=90)
    result = subprocess.run([str(binary), str(page)], capture_output=True, text=True, timeout=50)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "error" not in data, data
    assert data["width"] == 390
    assert max(map(abs, data["batchGaps"])) <= 1, data
    assert max(map(abs, data["reasoningGaps"])) <= 1, data
    assert data["reading"]["stick"] is False, data
    assert data["reading"]["gap"] > 100, data
    assert abs(data["reading"]["offsetDelta"]) <= 1, data
    print(json.dumps(data, ensure_ascii=False))
