"""单层过程展开的行为回归；真实浏览器/CSS 覆盖见 test_mobile_chat_budget。"""
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from frontend_i18n_js import NODE_PRELUDE

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "\n".join((ROOT / "frontend" / name).read_text(encoding='utf-8') for name in (
    "app-chat-detail.js", "app-chat-tools.js", "app-chat-model.js"))


def function(name):
    return re.search(r"\nfunction " + name + r"\(.*?\n}", SOURCE, re.S).group()


FIXTURE = r'''
const assert=require("node:assert/strict");
class El {
  constructor(){
    this.children=[]; this.nodes={}; this._html="";
    const set=new Set();
    this.classList={add:(...xs)=>xs.forEach(x=>set.add(x)),remove:x=>set.delete(x),
      contains:x=>set.has(x),toggle(x,on){const yes=on===undefined?!set.has(x):on;
        if(yes)set.add(x);else set.delete(x);return yes;}};
  }
  set innerHTML(s){this._html=s;
    for(const m of s.matchAll(/class="([^"]+)"/g))for(const c of m[1].split(" "))this.nodes["."+c]=new El();}
  get innerHTML(){return this._html;}
  get firstChild(){return this.children[0]||null;}
  get childNodes(){return this.children;}
  appendChild(el){if(el.parentNode)el.parentNode.children=el.parentNode.children.filter(x=>x!==el);
    this.children.push(el);el.parentNode=this;return el;}
  append(...els){els.forEach(el=>this.appendChild(el));}
  insertBefore(el){return this.appendChild(el);}
  contains(el){return this===el||this.children.some(c=>c.contains?.(el));}
  querySelector(s){return this.nodes[s]||null;}
  replaceChildren(){this.children=[];this._html="";}
}
const document={createElement:()=>new El()},HS_ICONS={chev:""};
const saved=new Set(),renders=[],requests=[],changes=[];let clears=0,nudges=0,timers=0;
function hsProcOpenSet(){return saved;}function hsProcOpenKey(){return "turn";}
function hsProcOpenSave(t,on){if(on)saved.add("turn");else saved.delete("turn");}
function hsViewportChange(v,fn,replace,why){changes.push(why);fn();}
function hsSetText(el,text){el.textContent=text;}function hsDur(){return "1s";}
function hsLinkify(){}function hsCodePaths(){}
function stripFraming(x){return String(x||"");}
const md={render(x){renders.push(x);return `<p>${x}</p>`;}};
function hsStreamHtml(part,source){return {head:"",tail:source===undefined?part.text:source};}
function hsStreamPaint(part,root,pieces){root._html=pieces.head+pieces.tail;
  root.children=[{}];part.streamRoot=root;part.streamMark={parentNode:root};}
function hsPaintTool(v,part){if(!part.el){part.el=new El();hsProcHost(part.turn).appendChild(part.el);}}
function hsTailNudge(){nudges++;}
function clearTimeout(){clears++;}function setTimeout(){timers++;return 1;}
function api(){return new Promise(resolve=>requests.push(resolve));}
const frames=[];function requestAnimationFrame(fn){frames.push(fn);return frames.length;}
function hsScroll(){}
function makeTurn(){const v={sid:"test"},t={view:v,holder:new El(),parts:new Map()};
  hsProcBox(t);return t;}
function add(t,id,type,text,extra={}){const p={id,type,text,done:true,turn:t,...extra};
  t.parts.set(id,p);hsPaint(t.view,p);return p;}
function toggle(t){t.procEl.querySelector(".hs-proc-bar").onclick();}
function body(p){return p.el.querySelector(".hs-think-body");}
'''


def run(code):
    if not shutil.which("node"):
        pytest.skip("需要 node")
    names = ("hsProcBox", "hsProcRepaint", "hsProcHost", "hsPaint", "hsThinkFull",
             "hsTextAttach", "hsNarrativeStamp", "hsNarrativePieces", "hsResetNarrativeCommit",
             "hsPaintLive", "hsPaintGap", "hsQueue", "hsOpenNarrative")
    script = FIXTURE + "\n".join(function(n) for n in names)
    result = subprocess.run(["node", "-e", NODE_PRELUDE + script + "\n(async()=>{\n" + code
                             + "\n})().catch(e=>{console.error(e);process.exit(1)});"],
                            capture_output=True, text=True, timeout=20, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_outer_toggle_reveals_both_narratives_in_order_and_releases_them():
    run(r'''
const t=makeTurn(),think=add(t,"r","reasoning","原生思考");
const tool=add(t,"tool","tool","");
const commentary=add(t,"c","reasoning","工具后的阶段性说明");
assert.equal(renders.length,0);
assert.equal(body(think).firstChild,null);
const host=t.procEl.querySelector(".hs-proc-body");
assert.deepEqual(host.children,[think.el,tool.el,commentary.el]);
toggle(t);
assert.ok(body(think).innerHTML.includes("原生思考"));
assert.ok(body(commentary).innerHTML.includes("阶段性说明"));
assert.equal(think.el.classList.contains("open"),false); // 无需内层按钮
assert.deepEqual(host.children,[think.el,tool.el,commentary.el]);
const before=renders.length;
for(let i=0;i<100;i++)hsPaint(t.view,think);
assert.equal(renders.length,before); // 定稿未变，不重复 markdown 解析
think.paintT=1;
toggle(t);
assert.equal(body(think).firstChild,null);
assert.equal(body(commentary).firstChild,null);
assert.equal(think.streamRoot,null);
assert.equal(think.mdCommittedHtml,null);
assert.equal(think.paintT,0);
assert.equal(clears,1);
think.done=false;
for(let i=0;i<100;i++){think.text+="新增";hsPaintLive(t.view,think);}
assert.equal(renders.length,before);
assert.equal(timers,0);assert.equal(nudges,0);
toggle(t);
assert.ok(body(think).innerHTML.includes("新增"));
assert.equal(host.children[1],tool.el);
''')


def test_saved_open_state_and_new_stream_parts_need_no_extra_click():
    run(r'''
saved.add("turn");const t=makeTurn();
const think=add(t,"r","reasoning","历史思考");
assert.ok(body(think).innerHTML.includes("历史思考"));
const commentary=add(t,"c","reasoning","新阶段",{done:false});
assert.ok(body(commentary).innerHTML.includes("新阶段"));
commentary.text+="的后续";hsPaintLive(t.view,commentary);
assert.ok(body(commentary).innerHTML.includes("新阶段的后续"));
const answer=add(t,"answer","text","最终回答");
assert.equal(answer.el.parentNode,t.holder);
assert.ok(!t.procEl.querySelector(".hs-proc-body").children.includes(answer.el));
''')


def test_full_history_loaded_on_outer_open_and_late_reply_stays_folded():
    run(r'''
const t=makeTurn(),p=add(t,"r","reasoning","预览",{textMore:100});
assert.equal(requests.length,0);toggle(t);
assert.equal(requests.length,1);hsPaint(t.view,p);
assert.equal(requests.length,1); // 合并并发读取
const parsed=renders.length;toggle(t);
requests.shift()({text:"预览和完整历史"});
await new Promise(resolve=>setImmediate(resolve));
assert.equal(p.text,"预览和完整历史");assert.equal(p.textMore,undefined);
assert.equal(changes.at(-1),"reasoning-full");
assert.equal(body(p).firstChild,null);assert.equal(renders.length,parsed);
toggle(t);assert.ok(body(p).innerHTML.includes("完整历史"));
assert.equal(requests.length,0);
''')


def test_failed_full_read_has_explicit_retry_without_per_frame_requests():
    run(r'''
const t=makeTurn(),p=add(t,"r","reasoning","预览",{textMore:100});toggle(t);
requests.shift()(null);
await new Promise(resolve=>setImmediate(resolve));
const more=p.el.querySelector(".hs-think-more");
assert.equal(more.hidden,false);assert.equal(more.disabled,false);
assert.ok(more.textContent.includes("仅显示部分"));
for(let i=0;i<100;i++)hsPaint(t.view,p);
assert.equal(requests.length,0);
more.onclick();assert.equal(requests.length,1);
requests.shift()({text:"完整过程"});
await new Promise(resolve=>setImmediate(resolve));
assert.equal(more.hidden,true);assert.ok(body(p).innerHTML.includes("完整过程"));
assert.equal(changes.at(-1),"reasoning-full");
''')


def test_full_reply_does_not_duplicate_delta_waiting_for_animation_frame():
    run(r'''
const t=makeTurn(),p=add(t,"r","reasoning","预览",{textMore:100});toggle(t);
hsQueue(t.view,p,"新增量");
assert.equal(p.text,"预览");assert.equal(p.buf,"新增量");
requests.shift()({text:"完整历史和新增量"});
await new Promise(resolve=>setImmediate(resolve));
assert.equal(p.text,"预览");assert.equal(p.buf,"新增量");
frames.shift()();
assert.equal(p.text,"预览新增量");assert.equal(p.buf,"");
assert.equal(requests.length,0);
assert.ok(p.el.querySelector(".hs-think-more").textContent.includes("重试"));
''')


def test_old_full_reply_does_not_overwrite_new_stream_or_final_answer():
    run(r'''
for(const changedType of [false,true]){
  saved.clear();const t=makeTurn(),p=add(t,"r","reasoning","旧预览",{textMore:100});
  toggle(t);
  if(changedType)p.type="text";else p.text="新的实时文字";
  requests.shift()({text:"迟到全文"});
  await new Promise(resolve=>setImmediate(resolve));
  assert.notEqual(p.text,"迟到全文");assert.equal(p._fullPending,false);
}
''')
