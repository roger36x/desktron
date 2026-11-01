"""执行前端实际函数，覆盖优先级、断线恢复和失败动作。"""
import unittest
from test_frontend_recall import fn
import subprocess
from frontend_i18n_js import NODE_PRELUDE


class ProgressUI(unittest.TestCase):
    def js(self, functions, script):
        source='\n'.join(fn(*x) for x in functions)
        out=subprocess.run(['node','-e',NODE_PRELUDE + source+'\n'+script],capture_output=True,text=True,timeout=10, encoding='utf-8')
        self.assertEqual(out.returncode,0,out.stderr)

    def test_running_state_moves_busy_send_to_engine_wait_without_resending(self):
        self.js([('app-chat-core.js','hsApply')],r'''
const assert=require('node:assert/strict');let writes=0;
function hsStateTake(){return true}function hsAskStateSync(){}
function hsBusy(v,b){v.busy=b}function hsStatus(v,state,label){v.statusState=state;v.statusLabel=label;writes++}
const v={busy:true,statusLabel:'发送中',pendingSend:{requestID:'r'}};
hsApply(v,'session.state',{state:'running'});
assert.equal(v.statusLabel,'处理中');assert.equal(v.pendingSend.requestID,'r');
v.statusLabel='生成中';hsApply(v,'session.state',{state:'running'});
assert.equal(v.statusLabel,'生成中');assert.equal(writes,1);
''')

    def test_http_acceptance_leaves_send_label_but_preserves_durable_stub(self):
        self.js([('app-chat-composer.js','hsDispatch')],r'''
const assert=require('node:assert/strict');let posts=0,early=false;
for(const n of ['hsQPaint','hsStatusClear','hsSetStick','hsTouch','hsScroll','hsDraftSave'])globalThis[n]=()=>{};
function hsBusy(v,b){v.busy=b}function hsStatus(v,s,l){v.statusLabel=l}
function hsNewTurn(v){const t={el:{remove(){}}};v.turns.push(t);return t}
async function api(){posts++;if(early)current.statusLabel='生成中';return {}}
let current;
(async()=>{
 current={sid:'s',turns:[{}],turnGeneration:1};
 assert.equal(await hsDispatch(current,'fixture',[]),true);
 assert.equal(current.statusLabel,'处理中');
 assert.ok(current.pendingSend.requestID);assert.equal(current.pendingSend.text,'fixture');
 early=true;current={sid:'s',turns:[{}],turnGeneration:1};
 await hsDispatch(current,'fixture',[]);assert.equal(current.statusLabel,'生成中');assert.equal(posts,2);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_stall_hint_is_only_one_sentence_and_clears_on_recovery(self):
        self.js([('app-chat-model.js','hsActivityPaint')],r'''
const assert=require('node:assert/strict');
let box=null;
const document={createElement(tag){return {tag,children:[],append(...nodes){this.children.push(...nodes)},remove(){box=null}}}};
const v={statusState:'working',stallSec:121,statusEl:{querySelector(){return box},appendChild(el){box=el}}};
hsActivityPaint(v);
assert.deepEqual(box.children.map(el=>el.tag),['span']);
assert.equal(box.children[0].textContent,'长时间无新进展，可继续等待或中断');
const first=box;hsActivityPaint(v);assert.equal(box,first);
v.activity={running_tools:2};box=null;hsActivityPaint(v);
assert.equal(box.children[0].textContent,'2 个工具仍在执行，长时间无新进展；可继续等待或中断');
v.activity={phase:'first_response'};box=null;hsActivityPaint(v);
assert.equal(box.children[0].textContent,'模型尚未开始回复，可继续等待或中断');
v.stallSec=0;hsActivityPaint(v);assert.equal(box,null);
for(const state of [{awaitAsk:true},{compactT0:1},{statusState:'done'}]){
  Object.assign(v,{statusState:'working',stallSec:121,awaitAsk:false,compactT0:0},state);
  hsActivityPaint(v);assert.equal(box,null);
}
''')

    def test_generic_failure_exit_code_is_compact_and_updates_signature(self):
        self.js([('app-chat-detail.js','hsRet'),('app-chat-detail.js','hsToolFailureExit'),('app-chat-detail.js','hsToolSig')],r'''
const assert=require('node:assert/strict');
const part={state:{status:'error',failure:{message:'工具执行失败（退出码 1）'}}};
assert.equal(hsToolFailureExit(part.state),'（退出码 1）');
const before=hsToolSig(part);
part.state.failure.message='工具执行失败 (退出码 -9)';
assert.equal(hsToolFailureExit(part.state),'（退出码 -9）');
assert.notEqual(hsToolSig(part),before);
part.state.failure.message='权限不足（退出码 1）';
assert.equal(hsToolFailureExit(part.state),'');
part.state.status='done';assert.equal(hsToolFailureExit(part.state),'');
''')

    def test_tool_preview_merges_without_losing_input_and_cannot_reopen_terminal(self):
        self.js([('app-chat-core.js','hsToolProgressMerge'),
                 ('app-chat-core.js','hsToolProgressQuiet')],r'''
const assert=require('node:assert/strict');
const folded={contains(name){return false;}},part={el:{classList:folded},
  state:{status:'running',input:{task:'inspect'},output:'old'}};
const q={progressOnly:true,state:{status:'running',output:'new',outputMore:9000,
  progressRevision:7,progressTotal:11000}};
assert.equal(hsToolProgressMerge(part,q,'running'),true);
assert.deepEqual(part.state.input,{task:'inspect'});
assert.equal(part.state.output,'new');assert.equal(part.progress.revision,7);
assert.equal(hsToolProgressQuiet(q,part,'running'),true);
part.el.classList.contains=()=>true;
assert.equal(hsToolProgressQuiet(q,part,'running'),false);
part.state={status:'done',input:{task:'inspect'},output:'final'};
assert.equal(hsToolProgressMerge(part,q,'done'),false);
assert.equal(part.state.status,'done');assert.equal(part.state.output,'final');
''')

    def test_provider_wait_is_delayed_neutral_and_tool_safe(self):
        self.js([('app-chat-model.js','hsActivitySync'),
                 ('app-chat-model.js','hsStatusPaint')],r'''
const assert=require('node:assert/strict');
let label='',tail='',klass='';function hsActivityPaint(){}function hsEllipsis(x){return x;}
function hsStTxt(t,s,x){label=s;tail=x||'';}function hsClock(ms){return Math.round(ms/1000)+'s'}
const statusEl={querySelector(){return null},set className(value){klass=value},get className(){return klass}};
const v={statusEl,statusState:'working',statusLabel:'处理中',statusT0:Date.now()-5000,
  awaitAsk:false,compactT0:0,retry:null,providerQueue:null,stallSec:0,activity:null};
hsActivitySync(v,{kind:'working',seconds:30});hsStatusPaint(v);
assert.equal(label,'处理中');
hsActivitySync(v,{kind:'provider_wait',seconds:59});hsStatusPaint(v);
assert.equal(label,'处理中');
hsActivitySync(v,{kind:'provider_wait',seconds:60});hsStatusPaint(v);
assert.equal(label,'等待模型进展');assert.equal(tail,' 60s');
assert.equal(klass,'hs-status st-working');
hsActivitySync(v,{kind:'provider_wait',seconds:120,running_tools:1});hsStatusPaint(v);
assert.equal(v.providerWait,null);
hsActivitySync(v,{kind:'provider_wait',seconds:900});hsStatusPaint(v);
assert.equal(v.providerWait,null);assert.equal(v.stallSec,900);
assert.equal(label,'长时间无新进展');assert.equal(klass,'hs-status st-warn');
''')

    def test_approval_beats_stall_and_retry(self):
        self.js([('app-chat-model.js','hsStatusPaint')],r'''
const assert=require('node:assert/strict');
let label='';function hsActivityPaint(){} function hsStTxt(t,s){label=s;}
const v={statusEl:{querySelector(){return null}},statusState:'working',awaitAsk:true,
  ask:{permission:true},stallSec:200,retry:{at:Date.now(),n:2}};
hsStatusPaint(v);assert.equal(label,'等待你批准…');
v.ask={};hsStatusPaint(v);assert.equal(label,'等待你回答…');
v.awaitAsk=false;v.retry=null;
function hsClock(){return '3m'}
hsStatusPaint(v);assert.equal(label,'长时间无新进展');
v.activity={kind:'no_progress',phase:'first_response'};
hsStatusPaint(v);assert.equal(label,'模型尚未开始回复，可取消');
''')

    def test_execution_review_status_is_live_and_refreshable(self):
        self.js([('app-chat-core.js','hsExecutionReviewSync'),
                 ('app-chat-model.js','hsStatusPaint')],r'''
const assert=require('node:assert/strict');let label='',klass='';
function hsActivityPaint(){} function hsStTxt(t,s){label=s;} function hsClock(){return '1h'}
const statusEl={querySelector(){return null},set className(v){klass=v},get className(){return klass}};
const v={statusEl,statusState:'working',statusLabel:'处理中',statusT0:Date.now(),
  awaitAsk:false,compactT0:0,retry:null,providerQueue:null,stallSec:0};
hsExecutionReviewSync(v,{status:'dispatching',review_sequence:1});
assert.equal(label,'正在复核长任务');assert.equal(klass,'hs-status st-working');
hsExecutionReviewSync(v,{status:'running',review_sequence:1,next_review_at:Date.now()/1000+3600});
assert.equal(label,'长任务已续租');
hsExecutionReviewSync(v,{status:'absolute_limit',review_sequence:12});
assert.equal(label,'已达到 12 小时执行上限');assert.equal(klass,'hs-status st-warn');
hsExecutionReviewSync(v,null);assert.equal(v.executionReview,null);
''')

    def test_snapshot_removes_expired_approval_and_advances_next(self):
        self.js([('app-chat-core.js','hsAskStateSync')],r'''
const assert=require('node:assert/strict');const HS_WAIT_LABELS=['等待你批准…'];
let closed=0, opened=[];
function hsAskClose(v){closed++;v.ask=null;} function hsStatus(v,s,label){v.statusLabel=label;}
function hsAsk(v,p){opened.push(p.id);v.ask={id:p.id};}
const v={ask:{id:'old'},awaitAsk:true,statusLabel:'等待你批准…'};
hsAskStateSync(v,{});assert.equal(closed,0);
hsAskStateSync(v,{asks:[{id:'old'}]});assert.equal(closed,0);
hsAskStateSync(v,{asks:[{id:'next',tool:'Bash'}]});assert.deepEqual(opened,['next']);
assert.equal(v.awaitAsk,true);
hsAskStateSync(v,{asks:[]});assert.equal(v.awaitAsk,false);assert.equal(v.ask,null);
assert.equal(v.statusLabel,'处理中');
''')

    def test_refresh_clears_approval_before_applying_ready(self):
        self.js([('app-chat-core.js','hsApply')],r'''
const assert=require('node:assert/strict');let ready=0;
function hsStateTake(){return true;} function hsAskStateSync(v,p){v.awaitAsk=false;}
function hsActivitySync(){} function hsTurnReady(v){assert.equal(v.awaitAsk,false);ready++;}
const v={awaitAsk:true};hsApply(v,'session.state',{state:'ready',asks:[],activity:{kind:'idle'}});
assert.equal(ready,1);
''')

    def test_late_stall_does_not_override_new_turn(self):
        self.js([('app-chat-core.js','hsApply')],r'''
const assert=require('node:assert/strict');
function hsStateTake(v,p){return p.generation>=v.turnGeneration;}
function hsActivitySync(){throw Error('stale event');}function hsStatusPaint(){throw Error('stale render');}
const v={busy:true,turnGeneration:2};hsApply(v,'session.stalled',{generation:1,seconds:200,activity:{kind:'no_progress'}});
''')

    def test_failed_step_label_and_recovery(self):
        self.js([('app-chat-tools.js','hsToolOutcome')],r'''
const assert=require('node:assert/strict');function hsToolVerb(t,r){return r?'执行中':'已执行'};
const p={tool:'bash',state:{status:'error',failure:{kind:'validation'}}};
assert.equal(hsToolOutcome(p,false),'校验失败');
p.state.failure.kind='tool_error';assert.equal(hsToolOutcome(p,false),'执行失败');
p.state={status:'done'};assert.equal(hsToolOutcome(p,false),'已执行');
''')

    def test_chat_text_cannot_approve_permission(self):
        self.js([('app-chat-overlays.js','hsAskSend')],r'''
let calls=0;function api(){calls++;throw Error('must not approve')}
(async()=>{await hsAskSend({ask:{id:'p',permission:true}}, {}, 'continue');
if(calls)throw Error('sent approval');})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_failed_approval_request_keeps_retry_and_does_not_show_allowed(self):
        self.js([('app-chat-overlays.js','hsAsk')],r'''
const assert=require('node:assert/strict');
const fields={};const button={disabled:false};let actionsRemoved=0;
const actions={querySelectorAll(){return [button]},remove(){actionsRemoved++}};
const el={querySelector(sel){if(sel==='.hs-acts')return actions;return fields[sel] ||= {};}};
const document={createElement(){return el}};
const HS_ICONS={lock:'lock',unlock:'unlock'};
function hsScroll(){}function hsToolArg(){return 'command'}function hsStatus(){}
function hsProcHost(t){return t.holder}function hsProcPlace(t,e){t.holder.insertBefore(e)}
function hsProcReveal(){}
async function api(){return {ok:false}}async function hsStateSync(){}
const v={sid:'s',turns:[{holder:{insertBefore(){}},parts:new Map()}]};
hsAsk(v,{id:'approval',tool:'Bash',reason:'Allow?'});
(async()=>{await fields['.hs-pill'].onclick();assert.equal(actionsRemoved,0);
assert.equal(v.awaitAsk,true);assert.equal(v.ask.submitting,false);assert.equal(button.disabled,false);
assert.match(fields['.hs-t2'].textContent,/审批提交失败/);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_diagnostic_stop_keeps_even_unstarted_message(self):
        self.js([('app-chat-composer.js','hsInterrupt')],r'''
const assert=require('node:assert/strict');let body;
async function api(url,p){body=JSON.parse(p.body);return {}}
for(const name of ['hsFlush','hsFinalize','hsStatus','hsIndex','hsScheduleBusyVerify'])globalThis[name]=()=>{};
const HS_INTERRUPT_VERIFY_MS=1;const turn={parts:new Map(),user:'keep'};
const v={sid:'s',busy:true,turns:[turn],queue:[]};
(async()=>{await hsInterrupt(v,true);assert.equal(body.recall,false);assert.equal(v.turns[0],turn)})()
.catch(e=>{console.error(e);process.exitCode=1});
''')


if __name__=='__main__':unittest.main()
