"""Execute the delivery lifecycle: recovery may continue only with non-dispatch proof."""
import unittest
from test_frontend_recall import RecallUI as _Harness


class QueueRecovery(unittest.TestCase):
    run_js = _Harness.run_js

    def test_recovery_errors_are_readable_and_do_not_release_unsent_messages(self):
        self.run_js([('app-chat-composer.js', 'hsRecoverServerQueue')], r'''
const assert=require('node:assert/strict');let failure,notice;
const HS_DRAFT_KEY=s=>s;const localStorage={getItem:()=>null};
async function api(){throw failure}
function hsErrorNotice(v,label,stage,details){notice={label,stage,details}}
(async()=>{
 const v={sid:'s',queue:[{text:'keep'}]};
 for(const status of [404,503,409,0]) {
   failure=Object.assign(new Error(status?'/session/s/queue-recovery → '+status:'连接中断'),{status,detail:status===409?'另一页面正在恢复':''});
   assert.equal(await hsRecoverServerQueue(v),false);
   assert.equal(v.queueRecoveryBlocked,true);assert.equal(v.queueRecovering,false);assert.equal(v.queue[0].text,'keep');
   assert.equal(notice.label,'旧待发消息恢复未完成');assert.equal(notice.stage,'delivery');
   assert(!notice.details.join('').includes('/session/'));
   assert(notice.details[0].includes(status?String(status):'连接中断'));
   if(status===409) assert(notice.details[0].includes('另一页面正在恢复'));
 }
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_receipt_proof_and_user_cancellation_are_distinct(self):
        self.run_js([('app-chat-composer.js', 'hsSentOrNot')], r'''
const assert=require('node:assert/strict');
let calls=0, receipt={status:'not_found'}, cancelled={status:'cancelled',retryable:true};
async function api(path,opts={}){calls++;return opts.method ? cancelled : receipt}
(async()=>{
 const v={sid:'s',pendingSend:{requestID:'r'}};
 let proof={};assert.equal(await hsSentOrNot(v,'hello',proof),'not-sent');assert.equal(proof.retryable,true);
 cancelled={status:'cancelled'};proof={};await hsSentOrNot(v,'hello',proof);assert.notEqual(proof.retryable,true);
 cancelled={status:'started'};proof={};assert.equal(await hsSentOrNot(v,'hello',proof),'sent');assert.notEqual(proof.retryable,true);
 receipt={status:'cancelled',reason:'user'};proof={};await hsSentOrNot(v,'hello',proof);assert.notEqual(proof.retryable,true);
 receipt={status:'rejected',reason:'restart_before_store'};cancelled={status:'cancelled',retryable:true};proof={};await hsSentOrNot(v,'hello',proof);assert.equal(proof.retryable,true);
 cancelled={status:'cancelled',retryable:false};proof={};await hsSentOrNot(v,'hello',proof);assert.equal(proof.retryable,false);
 for(const status of ['accepted','preparing','uncertain','started','finished']) {
   receipt={status};proof={};await hsSentOrNot(v,'hello',proof);assert.notEqual(proof.retryable,true);
 }
 calls=0;assert.equal(await hsSentOrNot({sid:'s',shepherdMode:false},'hello'),'unknown');assert.equal(calls,0);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_restart_queue_drains_once_only_after_authoritative_ready(self):
        self.run_js([('app-chat-composer.js',f) for f in ('hsDraftLoad','hsQFlush')]+[
            ('app-chat-core.js','hsTurnReady')], r'''
const assert=require('node:assert/strict');let sends=0,release;
const HS_DRAFT_KEY=s=>s;
const localStorage={getItem(){return JSON.stringify({boot:'old',q:[{t:'queued',a:[{data_b64:'image'}]}]})}};
function hsQPaint(){} function hsDraftSave(){} function hsStateTake(){return true}
function hsStateGeneration(p){return p.generation} function hsTurnSettled(){throw Error('unexpected')}
async function hsDispatch(v,text,atts){sends++;assert.equal(text,'queued');assert.equal(atts[0].data_b64,'image');return new Promise(r=>release=r)}
(async()=>{
 const v={sid:'s',readyGeneration:10,turnReady:true};hsDraftLoad(v);
 await hsQFlush(v);assert.equal(sends,0);assert.equal(v.queuePaused,false);
 hsTurnReady(v,{generation:10});hsTurnReady(v,{generation:10});await hsQFlush(v);
 assert.equal(sends,1);release(true);await new Promise(r=>setImmediate(r));assert.equal(v.queue.length,0);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_peer_status_does_not_pause_automatic_queue(self):
        self.run_js([('app-chat-composer.js','hsQFlush')], r'''
const assert=require('node:assert/strict');let sends=0;
function hsQPaint(){} function hsDraftSave(){}
async function hsDispatch(v,text){sends++;assert.equal(text,'first\n\nsecond');return true}
(async()=>{
 const v={turnReady:true,peerAsk:{requestID:'ask'},queue:[{text:'first'},{text:'second'}]};
 await hsQFlush(v);assert.equal(sends,1);assert.equal(v.queue.length,0);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_retry_is_bounded_and_user_action_survives_state_updates(self):
        self.run_js([('app-chat-composer.js','hsQFlush')], r'''
const assert=require('node:assert/strict');let sends=0;
function hsQPaint(){} function hsDraftSave(){}
async function hsDispatch(){sends++;return true}
(async()=>{
 const v={turnReady:true,queue:[{text:'keep'}]};hsQRetry(v);
 await hsQFlush(v);assert.equal(sends,0);assert.equal(v.queuePaused,false);
 for(let n=0;n<5;n++) hsQRetry(v);
 assert.equal(v.queuePaused,true);hsQIssue(v,'state','waiting');assert.equal(v.queueIssue.kind,'action');
 v.queueRetryAt=0;await hsQFlush(v);assert.equal(sends,0);assert.equal(v.queue.length,1);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_attachment_loss_is_persisted_and_never_autosends_partial_message(self):
        self.run_js([('app-chat-composer.js',f) for f in ('hsDraftWrite','hsDraftLoad','hsQFlush','hsQResume')], r'''
const assert=require('node:assert/strict');let saved,sends=0;
const HS_DRAFT_KEY=s=>s;
const localStorage={setItem(k,v){saved=v},getItem(){return saved},removeItem(){}};
function hsInpText(){return ''} function hsDraftAtts(){return []}
function hsQPaint(){} function hsDraftSave(){}
async function hsDispatch(){sends++;return true}
(async()=>{
 hsDraftWrite({sid:'s',queue:[{text:'needs image',atts:[{data_b64:'large'}]}]});
 const v={sid:'s'};hsDraftLoad(v);assert.equal(v.queue[0].missingAttachments,true);
 v.turnReady=true;await hsQResume(v);await hsQFlush(v);assert.equal(sends,0);assert.equal(v.queueIssue.kind,'edit');
 hsDraftWrite(v);hsDraftLoad(v);assert.equal(v.queue[0].missingAttachments,true);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_receipt_recovery_keeps_new_draft_and_does_not_replay_cancel(self):
        self.run_js([('app-chat-page.js','hsFollowTick')], r'''
const assert=require('node:assert/strict');let mode='unknown',lookups=0,restored=0;
function hsHidden(){return false} function hsSeen(){} function hsQPaint(){} function hsDraftSave(){}
function hsBusy(v,b){v.busy=b} function hsStatusClear(){} function hsErrorNotice(){}
function hsStatus(v,state,label){v.statusLabel=label}
function hsRestoreUnsent(){restored++} function hsStateSync(){throw Error('unexpected sent')}
async function hsSentOrNot(v,t,e){lookups++;e.retryable=mode==='retry';return mode==='unknown'?'unknown':'not-sent'}
(async()=>{
 const v={sid:'s',el:{isConnected:true},inp:{textContent:'new draft'},pendingSend:{requestID:'r',text:'first',atts:[]},queue:[{text:'second'}]};
 await hsFollowTick(v);assert.equal(v.pendingSend.requestID,'r');assert.equal(v.queue.length,1);
 assert.equal(v.statusLabel,'发送中');
 v.busy=true;v.statusLabel='执行中';await hsFollowTick(v);assert.equal(v.statusLabel,'执行中');v.busy=false;
 mode='retry';await hsFollowTick(v);assert.equal(v.pendingSend,null);assert.equal(v.queue[0].text,'first');assert.equal(v.queue[1].text,'second');
 assert.equal(v.inp.textContent,'new draft');assert.equal(v.turnReady,false);assert.equal(restored,0);
 mode='cancelled';v.pendingSend={requestID:'r2',text:'cancelled'};await hsFollowTick(v);
 assert.equal(restored,1);assert.equal(v.queue.length,2);assert.equal(v.queuePaused,true);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_edit_preserves_draft_and_original_queue_position(self):
        self.run_js([('app-chat-composer.js','hsQPop')], r'''
const assert=require('node:assert/strict');let warnings=0;
function hsQPaint(){} function hsDraftSave(){} function hsEnvironment(){warnings++}
function hsInpText(el){return el.textContent} function hsFill(v,t){v.inp.textContent=t} function hsAttPaint(){}
const v={inp:{textContent:'draft'},queue:[{text:'one'},{text:'two'}]};
assert.equal(hsQPop(v),false);assert.equal(v.queue.length,2);assert.equal(warnings,1);
v.inp.textContent='';assert.equal(hsQPop(v),true);assert.equal(v.inp.textContent,'two');
assert.equal(v.queueEditIndex,1);assert.equal(v.queuePaused,true);
''')

    def test_network_failure_never_persists_an_empty_queue_between_states(self):
        self.run_js([('app-chat-composer.js',f) for f in ('hsQFlush','hsDispatch','hsSentOrNot')], r'''
const assert=require('node:assert/strict');let snapshots=[];
for(const n of ['hsQPaint','hsStatusClear','hsSetStick','hsTouch','hsStatus','hsScroll','hsIndex']) globalThis[n]=()=>{};
function hsBusy(v,b){v.busy=b}
function hsNewTurn(v){const t={el:{remove(){}}};v.turns.push(t);return t}
function hsDraftSave(v){
 assert.ok(v.sending || v.queue.length,'must not erase the only durable copy');
 snapshots.push(JSON.parse(JSON.stringify({sending:v.sending,queue:v.queue})));
}
async function api(path,opts={}) {
 if(path.endsWith('/prompt_async')) throw Error('connection lost');
 return path.endsWith('/cancel-unsent') ? {status:'cancelled',retryable:true} : {status:'not_found'};
}
(async()=>{
 const item={text:'keep both',atts:[{data_b64:'image'}]};
 const v={sid:'s',turns:[{uid:'old'}],turnReady:true,queue:[item],turnGeneration:1};
 await hsQFlush(v);
 assert.equal(v.queue[0],item);assert.equal(v.queueRetries,1);assert.equal(v.queuePaused,false);
 assert.equal(snapshots[0].sending.text,'keep both');
 assert.equal(snapshots.at(-1).queue[0].atts[0].data_b64,'image');
})().catch(e=>{console.error(e);process.exitCode=1});
''')
