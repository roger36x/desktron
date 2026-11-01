import unittest
from test_frontend_recall import RecallUI


class OrdinaryDeliveryUI(unittest.TestCase):
    run_js = RecallUI.run_js

    def test_hot_backend_restart_pauses_ordinary_memory_queue(self):
        self.run_js([('app-chat-core.js', f) for f in ('hsRuntimeConnected',)], r'''
const assert=require('node:assert/strict');let painted=0,saved=0,recovered=0;
let _hsRuntime={boot_id:'old',frontend_rev:'same'},_hsRuntimeReloading=false;
const _hsViews=new Map([['s',{shepherdMode:false,queue:[{text:'queued'}],pendingSend:null}] ]);
function hsQPaint(){painted++} function hsDraftSave(){saved++}
function hsRecoverLiveViews(){recovered++} function refreshTerms(){}
function deskStartupMark(){} const location={reload(){throw Error('must not reload')}};
hsRuntimeConnected({boot_id:'new',frontend_rev:'same'});
const v=_hsViews.get('s');assert.equal(v.queuePaused,false);assert.equal(v.turnReady,false);assert.ok(painted&&saved&&recovered);
''')

    def test_normal_terminal_clears_ordinary_pending_receipt(self):
        self.run_js([('app-chat-core.js', f) for f in ('hsTurnSettled',)], r'''
const assert=require('node:assert/strict');let saved=0;
for(const name of ['hsAskClose','hsFlush','hsFinalize','hsStatus','hsBusy','hsIndex','hsReconcile',
 'hsQPaint','hsSeen','hsGoalSync','hsAgentsSync','hsTasksSync','hsEngineSync','hsCompactOff']) globalThis[name]=()=>{};
function hsStateGeneration(p){return p.generation||0} function hsDraftSave(){saved++}
const sending={text:'hello',requestID:'r'};
const v={turnGeneration:2,busy:true,turns:[],promptDispatch:{id:'r',generation:2,sending},
 sending,pendingSend:{...sending},awaitAsk:false};
hsTurnSettled(v,{generation:2});
assert.equal(v.pendingSend,null);assert.equal(v.sending,null);assert.equal(v.promptDispatch,null);assert.equal(saved,1);
''')

    def test_terminal_generation_jump_clears_pending_receipt(self):
        self.run_js([('app-chat-core.js', f) for f in ('hsTurnSettled',)], r'''
const assert=require('node:assert/strict');
for(const name of ['hsAskClose','hsFlush','hsFinalize','hsStatus','hsBusy','hsIndex','hsReconcile',
 'hsQPaint','hsSeen','hsGoalSync','hsAgentsSync','hsTasksSync','hsEngineSync','hsCompactOff']) globalThis[name]=()=>{};
function hsStateGeneration(p){return p.generation||0} function hsDraftSave(){}
const sending={text:'hello',requestID:'r'};
const v={turnGeneration:1700,busy:true,turns:[],
 promptDispatch:{id:'r',generation:101,sending},sending,pendingSend:{...sending},awaitAsk:false};
hsTurnSettled(v,{generation:1700});
assert.equal(v.pendingSend,null);assert.equal(v.sending,null);assert.equal(v.promptDispatch,null);
''')

    def test_stale_terminal_generation_cannot_reach_settlement(self):
        self.run_js([('app-chat-core.js', f) for f in ('hsStateGeneration','hsStateTake')], r'''
const assert=require('node:assert/strict');
const v={turnGeneration:101,promptDispatch:{id:'current',generation:101}};
assert.equal(hsStateTake(v,{generation:100}),false);
assert.equal(v.promptDispatch.id,'current');assert.equal(v.turnGeneration,101);
''')

    def test_runtime_restart_pauses_ordinary_queue_and_preserves_request_id(self):
        self.run_js([('app-chat-composer.js', f) for f in ('hsDraftWrite','hsDraftLoad','hsQFlush')], r'''
const assert=require('node:assert/strict');let saved,sends=0;
const HS_DRAFT_KEY=s=>s;
let _hsRuntime={boot_id:'old'};
const localStorage={setItem(k,v){saved=v},getItem(){return saved},removeItem(){saved=null}};
function hsInpText(){return ''} function hsDraftAtts(a){return a||[]}
function hsQPaint(){} function hsAttPaint(){} function hsSendSync(){}
async function hsDispatch(){sends++;return true}
const v={sid:'s',shepherdMode:false,queuePaused:false,queue:[{text:'queued',atts:[]}],
 sending:{text:'sent',atts:[],requestID:'prompt_1'}};
hsDraftWrite(v);_hsRuntime={boot_id:'new'};
const restored={sid:'s',shepherdMode:false,turnReady:true,inp:{},atts:[]};hsDraftLoad(restored);
assert.equal(restored.queuePaused,false);assert.equal(restored.turnReady,false);assert.equal(restored.pendingSend.requestID,'prompt_1');
hsQFlush(restored);assert.equal(sends,0);assert.equal(restored.queue.length,1);
''')

    def test_blocked_state_keeps_queue_paused_and_offers_unknown_boundary(self):
        self.run_js([('app-chat-core.js', f) for f in ('hsDeliveryBlocked','hsApply',)], r'''
const assert=require('node:assert/strict');let env=null,saved=0,painted=0,busy=null;
function hsStateTake(){return true} function hsAskStateSync(){} function hsRetrySync(){}
function hsActivitySync(){} function hsRecallStateSync(){} function hsTurnReady(){throw Error('not ready')}
function hsBusy(v,x){busy=x} function hsEnvironment(v,p){env=p}
function hsQPaint(){painted++} function hsDraftSave(){saved++}
const v={queuePaused:false,busy:true};
hsApply(v,'session.state',{state:'blocked',delivery:{request_id:'r',status:'uncertain'}});
assert.equal(v.turnReady,false);assert.equal(v.queuePaused,false);assert.equal(v.turnReady,false);assert.equal(busy,false);
assert.equal(env.action.kind,'delivery-accept-unknown');assert.equal(env.action.requestID,'r');
assert.ok(saved&&painted);
''')

    def test_delivery_conflict_prompt_rejection_shows_unknown_boundary_card(self):
        self.run_js([('app-chat-core.js','hsDeliveryBlocked'),
                     ('app-chat-composer.js','hsDispatch')], r'''
const assert=require('node:assert/strict');let env=null,errors=0,removed=0,saved=0,painted=0;
for(const name of ['hsStatusClear','hsSetStick','hsTouch','hsStatus','hsScroll','hsIndex','hsRename']) globalThis[name]=()=>{};
function hsNewTurn(v){const t={el:{remove(){removed++;}}};v.turns.push(t);return t;}
function hsBusy(v,b){v.busy=b;} function hsQIssue(v,k,msg){v.issue={k,msg};hsQPaint();hsDraftSave();}
function hsQPaint(){painted++;} function hsDraftSave(){saved++;}
function hsEnvironment(v,p){env=p;} function hsErrorNotice(){errors++;}
async function hsStateSync(){throw Error('state sync should not be needed when response carries delivery');}
async function api(){const e=new Error('conflict');e.status=409;e.code='delivery_uncertain';
  e.data={state:'blocked',generation:7,delivery:{request_id:'old',status:'uncertain'}};throw e;}
const v={sid:'s',turns:[],turnGeneration:5,atts:[]};
(async()=>{
  assert.equal(await hsDispatch(v,'again',[]),false);
  assert.equal(errors,0);assert.equal(removed,1);assert.equal(v.turnGeneration,5);
  assert.equal(v.busy,false);assert.equal(v.turnReady,true);assert.equal(v.sending,null);
  assert.equal(env.stage,'delivery');assert.equal(env.action.kind,'delivery-accept-unknown');
  assert.equal(env.action.requestID,'old');assert.equal(v.issue.k,'state');
  assert.ok(saved&&painted);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


if __name__=='__main__':unittest.main()
