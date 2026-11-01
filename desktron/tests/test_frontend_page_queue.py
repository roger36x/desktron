"""Execute real page-level queue JS, including hidden/cold views and lost receipts."""
import unittest
from test_frontend_recall import RecallUI as Harness

QUEUE_DOM = r'''
const assert=require('node:assert/strict');
const node=()=>({children:[],dataset:{},classList:{add(){}},setAttribute(){},
 set innerHTML(value){this.children=[]},appendChild(x){this.children.push(x)}});
const document={createElement:node};const box=node();
const click=b=>b.onclick({stopPropagation(){}});
const v={el:{querySelector(){return box}},queue:[],inp:{textContent:'draft'},atts:[{name:'keep'}]};
function hsDraftSave(){} function hsInpText(e){return e.textContent}
function hsFill(v,t){v.inp.textContent=t} function hsAttPaint(){}
'''


class PageQueue(unittest.TestCase):
    run_js = Harness.run_js

    def test_hidden_cold_view_dispatches_without_loading_history(self):
        self.run_js([('app-chat-core.js','hsQueuePump'), ('app-chat-page.js','hsFollowTick'),
                     ('app-chat-composer.js','hsQFlush')], r'''
const assert=require('node:assert/strict');
const document={hidden:false}; let states=0,sends=0,configs=0;
function hsHidden(){return true} function hsQPaint(){} function hsDraftSave(){}
async function hsEngineSync(v){configs++;v.shepherdMode=true}
async function hsStateSync(v){states++;v.busy=false;v.turnReady=true;return {state:'ready'}}
async function hsDispatch(v,text){sends++;assert.equal(text,'one\n\ntwo');return true}
const v={el:{isConnected:true},needLoad:true,needConfig:true,busy:true,queue:[{text:'one'},{text:'two'}]};
const paused={el:{isConnected:true},queuePaused:true,queue:[{text:'edit'}]};
const empty={el:{isConnected:true},queue:[]};
const _hsViews=new Map([['hidden',v],['paused',paused],['empty',empty]]);
(async()=>{await hsQueuePump();await hsQueuePump();
 assert.equal(states,1);assert.equal(sends,1);assert.equal(configs,1);
 assert.equal(v.needLoad,true);assert.equal(paused.queue.length,1);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_failed_running_or_compacting_state_never_releases_queue(self):
        self.run_js([('app-chat-core.js','hsQueuePump'), ('app-chat-page.js','hsFollowTick'),
                     ('app-chat-composer.js','hsQFlush')], r'''
const assert=require('node:assert/strict');const document={hidden:false};
function hsHidden(){return true} async function hsDispatch(){throw Error('unexpected dispatch')}
let state=null;async function hsStateSync(){return state}
const v={el:{isConnected:true},busy:false,turnReady:true,queue:[{text:'keep'}]};
const _hsViews=new Map([['s',v]]);
(async()=>{for(const s of [null,{state:'running'},{state:'blocked'},{state:'ready',compacting:1},
 {state:'ready',compact:{running:true}}]){state=s;await hsQueuePump();assert.equal(v.queue.length,1)}
 document.hidden=true;state={state:'ready'};await hsQueuePump();assert.equal(v.queue.length,1);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_overlapping_pumps_do_not_dispatch_twice(self):
        self.run_js([('app-chat-core.js','hsQueuePump'), ('app-chat-page.js','hsFollowTick'),
                     ('app-chat-composer.js','hsQFlush')], r'''
const assert=require('node:assert/strict');const document={hidden:false};let release,sends=0,reads=0;
function hsHidden(){return true}function hsQPaint(){}function hsDraftSave(){}
function hsStateSync(){reads++;return new Promise(r=>release=r)}
async function hsDispatch(){sends++;return true}
const v={el:{isConnected:true},turnReady:true,queue:[{text:'one'}]};const _hsViews=new Map([['s',v]]);
(async()=>{const first=hsQueuePump();await hsQueuePump();assert.equal(reads,1);
 release({state:'ready'});await first;await hsQueuePump();assert.equal(sends,1);assert.equal(v.queuePolling,false)
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_hidden_pending_receipt_is_checked_before_next_queue(self):
        self.run_js([('app-chat-core.js','hsQueuePump'), ('app-chat-page.js','hsFollowTick')], r'''
const assert=require('node:assert/strict');const document={hidden:false};let proofs=0;
function hsHidden(){return true}async function hsSentOrNot(){proofs++;return 'unknown'}function hsStatus(){}
function hsStateSync(){throw Error('must settle receipt first')}
const v={el:{isConnected:true},pendingSend:{requestID:'r',text:'old'},queue:[{text:'next'}]};
const _hsViews=new Map([['s',v]]);
(async()=>{await hsQueuePump();assert.equal(proofs,1);assert.equal(v.pendingSend.requestID,'r');assert.equal(v.queue.length,1)})()
.catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_reference_download_cannot_send_a_cancelled_queue(self):
        self.run_js([('app-chat-composer.js','hsQFlush')], r'''
const assert=require('node:assert/strict');let release;
function hsAttHydrateOne(){return new Promise(r=>release=r)}function hsDispatch(){throw Error('cancelled')}
const v={turnReady:true,queue:[{text:'x',atts:[{url:'/asset'}]}]};
(async()=>{const send=hsQFlush(v);v.queue=[];release({data_b64:'aA=='});await send;assert.equal(v.qSending,false)})()
.catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_empty_queue_arrow_up_reaches_history(self):
        self.run_js([('app-chat-composer.js','hsQPop'),('app-chat-input.js','hsEditKey')], r'''
const assert=require('node:assert/strict');let history=0;
function hsAtEdge(){return true}function hsHistory(){history++;return true}
assert.equal(hsEditKey({queue:[],inp:{}},{key:'ArrowUp'}),true);assert.equal(history,1);
''')

    def test_editing_only_item_keeps_exit_control_visible(self):
        self.run_js([('app-chat-composer.js',n) for n in ('hsQPaint','hsQResume','hsQFlush')], QUEUE_DOM + r'''
function hsDispatch(){throw Error('draft must not send')}
v.queuePaused=true;v.queueEditIndex=0;v.turnReady=true;
hsQPaint(v);
assert.equal(box.hidden,false);
assert.equal(box.children[0].children[1].textContent,'保留为草稿');
click(box.children[0].children[1]);
assert.equal(v.queuePaused,false);assert.equal(v.queueEditIndex,null);assert.equal(box.hidden,true);
assert.equal(v.inp.textContent,'draft');assert.equal(v.atts[0].name,'keep');
''')

    def test_edit_actions_follow_messages_and_keep_execution_guards(self):
        self.run_js([('app-chat-composer.js',n) for n in ('hsQPaint','hsQResume','hsQFlush')], QUEUE_DOM + r'''
let sends=0;async function hsDispatch(v,text){sends++;assert.equal(text,'remaining');return true}
v.queue=[{text:'remaining'}];v.queuePaused=true;v.queueEditIndex=1;v.turnReady=true;
hsQPaint(v);assert.equal(box.children[0].className,'hs-q');
const action=box.children[1].children[1];assert.equal(action.textContent,'发送其余消息');
v.pendingSend={requestID:'unknown'};click(action);assert.equal(v.queueEditIndex,1);assert.equal(v.queuePaused,true);
v.pendingSend=null;v.interruptRequest=true;click(action);assert.equal(v.queueEditIndex,1);
v.interruptRequest=false;click(action);
assert.equal(sends,1);assert.equal(v.inp.textContent,'draft');assert.equal(v.atts[0].name,'keep');
''')

    def test_attachment_repair_targets_affected_row_without_popping_tail(self):
        self.run_js([('app-chat-composer.js','hsQPaint')], QUEUE_DOM + r'''
v.inp.textContent='';v.atts=[];v.queue=[{text:'image',atts:[{url:'/asset'}]},{text:'tail'}];
v.queuePaused=true;v.queueIssue={kind:'edit',message:'附件取回失败'};
hsQPaint(v);
assert.equal(box.children[0].children[1].textContent,'补充附件');
assert.equal(box.children[2].children.length,1);
click(box.children[0].children[1]);
assert.equal(v.inp.textContent,'image');assert.equal(v.queueEditIndex,0);
assert.equal(v.queue[0].text,'tail');assert.equal(v.queuePaused,true);
''')

    def test_normal_queue_has_no_toolbar_and_recovery_actions_have_context(self):
        self.run_js([('app-chat-composer.js','hsQPaint'),('app-chat-composer.js','hsQResume')], QUEUE_DOM + r'''
let resumed=0,legacy=0;function hsQFlush(){resumed++}function hsLegacyDraft(){legacy++}
v.queue=[{text:'queued'}];hsQPaint(v);assert.equal(box.children.length,1);
v.queuePaused=true;v.queueIssue={kind:'action',message:'网络未恢复'};hsQPaint(v);
assert.equal(box.children[1].children[0].textContent,'网络未恢复');
assert.equal(box.children[1].children[1].textContent,'重试');click(box.children[1].children[1]);
assert.equal(resumed,1);
v.queuePaused=true;v.queueIssue={kind:'legacy',message:'旧待发消息已恢复，请确认后发送'};hsQPaint(v);
assert.equal(box.children[1].children[1].textContent,'确认发送');click(box.children[1].children[1]);
assert.equal(resumed,2);
v.pendingSend={text:'old'};hsQPaint(v);assert.equal(box.children[1].children[1].textContent,'保留为草稿');
click(box.children[1].children[1]);assert.equal(legacy,1);assert.equal(resumed,2);
''')

    def test_recovery_ack_loss_does_not_duplicate_restored_messages(self):
        self.run_js([('app-chat-composer.js',name) for name in
                     ('hsRecoverServerQueue','hsDraftWrite')], r'''
const assert=require('node:assert/strict');const values=new Map();let acks=0,claims=0;
const HS_DRAFT_KEY=s=>'draft:'+s;const navigator={};
const localStorage={getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};
function hsInpText(e){return e.textContent}function hsDraftAtts(a){return a||[]}
function hsDraftSave(v){hsDraftWrite(v)}function hsQPaint(){}function hsAttPaint(){}function hsErrorNotice(){}
async function api(path,opts={}){if(opts.method!=='POST')return {available:true};const b=JSON.parse(opts.body);
 if(b.ack){acks++;if(acks===1)throw Error('lost ack');return {ok:true}}
 claims++;return {token:'token',items:[{id:'q',request_id:'r',text:'recover',atts:[{url:'/blob'}]}]}}
const v={sid:'s',inp:{textContent:'my draft'},queue:[],atts:[]};
(async()=>{assert.equal(await hsRecoverServerQueue(v),false);assert.equal(v.queueRecovery,'token');
 assert.equal(JSON.parse(values.get('draft:s')).recovery,'token');
 assert.equal(await hsRecoverServerQueue(v),true);assert.equal(claims,1);assert.equal(acks,2);
 assert.equal(v.queue.length,1);assert.equal(v.queue[0].atts[0].url,'/blob');
 assert.equal(v.inp.textContent,'my draft');assert.equal(v.queuePaused,true);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_storage_failure_never_acknowledges_or_changes_original_draft(self):
        self.run_js([('app-chat-composer.js',name) for name in
                     ('hsRecoverServerQueue','hsDraftWrite')], r'''
const assert=require('node:assert/strict');const HS_DRAFT_KEY=s=>'draft:'+s;const navigator={};let acks=0;
const localStorage={getItem:k=>k==='desk.queue.recovery-owner'?'browser':null,
 setItem(){throw Error('quota')},removeItem(){}};
function hsInpText(e){return e.textContent}function hsDraftAtts(a){return a||[]}function hsErrorNotice(){}
async function api(path,opts={}){if(opts.method!=='POST')return {available:true};
 if(JSON.parse(opts.body).ack){acks++;return {ok:true}}
 return {token:'token',items:[{id:'q',request_id:'r',text:'recover',atts:[]}]}}
const v={sid:'s',inp:{textContent:'draft'},queue:[{text:'local'}],atts:[]};
(async()=>{assert.equal(await hsRecoverServerQueue(v),false);assert.equal(acks,0);
 assert.equal(v.queue.length,1);assert.equal(v.queue[0].text,'local');assert.equal(v.inp.textContent,'draft');
 assert.equal(v.queueRecoveryBlocked,true);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_cold_configuration_failure_keeps_queue_until_retry_succeeds(self):
        self.run_js([('app-chat-composer.js','hsQFlush')], r'''
const assert=require('node:assert/strict');let ok=false,sends=0;
async function hsEngineSync(v){if(ok)v.shepherdMode=true}
function hsQPaint(){}function hsDraftSave(){}async function hsDispatch(){sends++;return true}
const v={needConfig:true,turnReady:true,queue:[{text:'keep'}]};
(async()=>{await hsQFlush(v);assert.equal(sends,0);assert.equal(v.needConfig,true);
 ok=true;await hsQFlush(v);assert.equal(sends,1);assert.equal(v.queue.length,0)
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_reconnection_retries_migration_before_pending_receipts(self):
        self.run_js([('app-chat-core.js','hsQueuePump')], r'''
const assert=require('node:assert/strict');const document={hidden:false};let recovery=0,ticks=0;
async function hsRecoverServerQueue(v){recovery++;v.queueRecoveryBlocked=false;return true}
async function hsFollowTick(v,queueOnly){assert.equal(v.queueRecoveryBlocked,false);assert.equal(queueOnly,true);ticks++}
const v={el:{isConnected:true},queueRecoveryBlocked:true,queue:[]};const _hsViews=new Map([['s',v]]);
(async()=>{await hsQueuePump();assert.equal(recovery,1);assert.equal(ticks,1);await hsQueuePump();assert.equal(ticks,1)})()
.catch(e=>{console.error(e);process.exitCode=1});
''')
