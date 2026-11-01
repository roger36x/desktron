"""Execute production JS: cancelling compaction must not report success/error."""
import subprocess
import unittest

from test_frontend_recall import fn


class ErrorProjectionUI(unittest.TestCase):
    def js(self, functions, script):
        source='\n'.join(fn(*f) for f in functions)
        result=subprocess.run(['node','-e',source+'\n'+script],capture_output=True,text=True,timeout=20, encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_auth_source_filter_and_authoritative_empty_snapshot(self):
        self.js([('app-chat-core.js','hsEnvironmentRelevant'),
                 ('app-chat-core.js','hsEnvironmentSnapshot')], r'''
const assert=require('node:assert/strict');let hidden=0,shown=0;
function hsEnvironmentHide(){hidden++;} function hsEnvironment(){shown++;}
const v={engine:'pi',shepherdMode:true,sessionOpts:{model:'example-gw/gpt'},
 envRevision:4,envCurrent:{issueRevision:4,state:'error'}};
assert.equal(hsEnvironmentRelevant(v,{stage:'auth',engine:'claude',provider:'claude'}),false);
assert.equal(hsEnvironmentRelevant(v,{stage:'auth'}),false);
assert.equal(hsEnvironmentRelevant(v,{stage:'auth',engine:'pi',provider:'another'}),false);
assert.equal(hsEnvironmentRelevant(v,{stage:'auth',engine:'pi',provider:'example-gw'}),true);
assert.equal(hsEnvironmentRelevant(v,{stage:'recovery'}),true);
hsEnvironmentSnapshot(v,null,3);assert.equal(hidden,0);
hsEnvironmentSnapshot(v,null,5);assert.equal(hidden,1);assert.equal(v.envCurrent,null);
v.envCurrent={stage:'auth',state:'warning'};hsEnvironmentSnapshot(v,null,5);assert.equal(hidden,2);assert.equal(v.envCurrent,null);
''')

    def test_login_action_uses_error_source(self):
        self.js([('app-chat-page.js','hsLoginShell')], r'''
const assert=require('node:assert/strict');let called;
const HS_AUTH_CMD={claude:{login:'claude auth login'},pi:{login:'pi'}};
function openLoginTUI(...args){called=args;}
hsLoginShell({engine:'pi'},'login',{engine:'claude',provider:'claude'});
assert.equal(called[0],'claude auth login');assert.equal(called[2].engine,'claude');
''')

    def test_old_source_session_error_cannot_bypass_environment_filter(self):
        self.js([('app-chat-core.js','hsEnvironmentRelevant'),
                 ('app-chat-core.js','hsTurnErrorNotice')], r'''
const assert=require('node:assert/strict');let shown=0;
function hsEnvironment(){shown++;} function hsErrorNotice(){shown++;} function hsRetrySummary(){return '';}
const v={engine:'pi',sessionOpts:{model:'a/model'}};
hsTurnErrorNotice(v,{engine:'claude',provider:'claude',error:{code:'AUTH',message:'expired'}});
hsTurnErrorNotice(v,{engine:'pi',provider:'b',error:{code:'AUTH',message:'expired'}});
assert.equal(shown,0);
hsTurnErrorNotice(v,{engine:'pi',provider:'a',error:{code:'AUTH',message:'expired'}});
assert.equal(shown,1);
''')

    def test_login_close_rechecks_source_then_refreshes_open_views(self):
        self.js([('app-settings-sections.js','openLoginTUI')], r'''
const assert=require('node:assert/strict');let close,calls=[],synced=0;
const _hsViews=new Map([['s',{}]]);
async function api(path,opts){calls.push([path,JSON.parse(opts.body)]);return {id:'terminal',ok:true};}
async function _authTuiModal(_id,_title,_tag,callback){close=callback;}
function hsEngineSync(){synced++;}
(async()=>{
 await openLoginTUI('pi','Pi',{engine:'pi',provider:'anthropic'});
 assert.equal(calls.length,1);assert.equal(synced,0);
 assert.deepEqual(calls[0][1],{init:'pi '});
 await close();assert.equal(calls[1][0],'/workbench/auth-refresh');
 assert.deepEqual(calls[1][1],{engine:'pi',provider:'anthropic'});assert.equal(synced,1);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_delivery_recovery_action_survives_late_turn_error(self):
        self.js([('app-chat-core.js','hsTurnErrorNotice')], r'''
const assert=require('node:assert/strict');let shown;
function hsEnvironment(_v,p){shown=p;_v.envCurrent=p;}
function hsErrorNotice(){throw Error('ordinary error must not replace delivery recovery');}
function hsRetrySummary(){return '';}
const recovery={state:'warning',stage:'delivery',label:'上一轮可能已经执行，已暂停后续消息',
  details:['请先核对'],action:{kind:'delivery-accept-unknown',label:'保留未知边界并继续',requestID:'r'}};
const v={sid:'s',envCurrent:recovery};
hsTurnErrorNotice(v,{generation:5,error:{message:'No project session found...\nCommand exited with code -9'}});
assert.equal(shown.stage,'delivery');assert.equal(shown.action.kind,'delivery-accept-unknown');
assert.ok(shown.details.some(x=>x.includes('Command exited')));
''')

    def test_backend_priority_is_respected_and_legacy_warning_cannot_hide_failure(self):
        self.js([('app-chat-core.js','hsTurnErrorNotice')], r'''
const assert=require('node:assert/strict');
let shown;
function hsEnvironment(_v,p){shown=p;}
function hsErrorNotice(_v,msg){shown=msg;}
function hsRetrySummary(){return '';}
const v={envStage:'compatibility',envEl:{hidden:false,classList:{contains:()=>true}}};
hsTurnErrorNotice(v,{error:{message:'request failed'}});
assert.equal(shown,'request failed');
const environment={state:'error',stage:'auth',label:'login',issueRevision:3};
hsTurnErrorNotice(v,{environment,error:{message:'request failed'}});
assert.equal(shown,environment);
''')

    def test_compact_events_do_not_draw_success_boundary_for_cancel_or_noop(self):
        self.js([('app-chat-core.js','hsApply')], r'''
const assert=require('node:assert/strict');
let errors=0,lines=0,status='';
function hsTouch(){} function deskLog(){} function hsCompactOff(){}
function hsStatus(_v,_state,label){status=label;}
function hsErrorNotice(){errors++;} function hsCompactRow(){lines++;} function hsScroll(){}
for(const result of ['cancelled','noop','failed','success']) {
  errors=0;lines=0;
  hsApply({sid:'s',cmpLive:true,busy:false},'session.compact',{running:false,result,trigger:'manual'});
  assert.equal(errors,result==='failed'?1:0);
  assert.equal(lines,result==='success'?1:0);
  if(result==='cancelled')assert.equal(status,'压缩已取消');
  if(result==='noop')assert.equal(status,'无需压缩');
}
''')

    def test_manual_operation_ignores_ordinary_idle_until_matching_terminal(self):
        self.js([('app-chat-core.js','hsApply')], r'''
const assert=require('node:assert/strict');
let terminal=0,settled=0;
function hsStateTake(){return true;} function hsCompactTerminal(v,p){terminal++;v.manualCompact=false;v.busy=false;v.turnReady=true;return true;}
function hsAskStateSync(){} function hsRecallStateSync(){} function hsActivitySync(){} function hsRetrySync(){}
function hsTurnSettled(){settled++;} function hsTurnReady(){settled++;} function hsTouch(){} function deskLog(){}
function hsCompactRow(){} function hsScroll(){} function hsCompactOff(){} function hsStatus(){}
function hsErrorNotice(){} function hsFlush(){} function hsCompactAnchor(){return null;} function hsCompactOn(){}
const v={sid:'s',manualCompact:true,manualCompactID:'op',cmpLive:false,busy:true,turnReady:false};
hsApply(v,'session.idle',{contract:1,state:'ready'});
hsApply(v,'session.state',{state:'ready'});
assert.equal(settled,0);assert.equal(v.busy,true);assert.equal(v.turnReady,false);
hsApply(v,'session.compact',{running:false,result:'success',trigger:'manual',operation_id:'op'});
assert.equal(terminal,1);assert.equal(v.busy,false);assert.equal(v.turnReady,true);
''')

    def test_compact_http_response_preserves_cancelled_and_noop(self):
        self.js([('app-chat-core.js','hsCompactTerminal'),('app-chat-input.js','hsCompact')], r'''
const assert=require('node:assert/strict');
let reply,status='',errors=0;
async function api(){return reply;}
function hsErrorNotice(){errors++;} function hsStatusClear(){} function hsNewTurn(){return {};}
function hsBusy(v,b){v.busy=b;} function hsSetStick(){} function hsTouch(){}
function hsStatus(_v,_state,label){status=label;} function hsScroll(){} function hsCompactOn(){}
function hsCompactOff(){} function hsReconcile(){} function hsQFlush(){}
(async()=>{
for (const [result,label] of [['cancelled','压缩已取消'],['noop','无需压缩'],['success','已压缩']]) {
  reply={ok:true,result};
  const v={sid:'s',busy:false,turnReady:true,caps:{compact:true},turns:[{}]};
  await hsCompact(v);
  assert.equal(status,label);assert.equal(errors,0);
  assert.equal(v.busy,false);assert.equal(v.manualCompact,false);
}
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_shepherd_compact_transport_loss_waits_for_durable_terminal(self):
        self.js([('app-chat-core.js','hsCompactTerminal'),('app-chat-input.js','hsCompactPoll'),('app-chat-input.js','hsCompact')], r'''
const assert=require('node:assert/strict');
let errors=0,status='',syncs=0,timers=[];
async function api(){syncs++;throw new Error('Load failed');}
function setTimeout(fn){timers.push(fn);return timers.length;}
function hsErrorNotice(){errors++;} function hsStatusClear(){} function hsNewTurn(){return {};}
function hsBusy(v,b){v.busy=b;} function hsSetStick(){} function hsTouch(){}
function hsStatus(_v,_state,label){status=label;} function hsScroll(){} function hsCompactOn(){}
function hsCompactOff(){} function hsReconcile(){} function hsQFlush(){}
(async()=>{
 const v={sid:'s',shepherdMode:true,busy:false,turnReady:true,caps:{compact:true},turns:[{}]};
 await hsCompact(v);
 assert.equal(errors,0);assert.equal(status,'压缩连接中断，正在核对结果');
 assert.equal(v.busy,true);assert.equal(v.turnReady,false);assert.equal(v.manualCompact,true);
 assert.equal(timers.length,1);await timers[0]();assert.equal(syncs,2);
 assert.equal(timers.length,2); // unresolved: schedules the next durable state probe
 const current=v.manualCompactID;
 assert.equal(hsCompactTerminal(v,{operation_id:'old',result:'cancelled'}),false);
 assert.equal(v.manualCompactID,current);assert.equal(v.busy,true);
 assert.equal(hsCompactTerminal(v,{operation_id:current,result:'success'}),true);
 assert.equal(v.busy,false);assert.equal(v.turnReady,true);assert.equal(v.manualCompact,false);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_not_found_is_tombstoned_before_local_unlock(self):
        self.js([('app-chat-core.js','hsCompactTerminal'),('app-chat-input.js','hsCompactPoll')], r'''
const assert=require('node:assert/strict');
let calls=[],removed=0,indexed=0,labelSeen='';
async function api(path,opts){calls.push([path,opts&&opts.method]);return opts
 ? {ok:false,running:false,result:'not_started',operation_id:'op'}
 : {running:false,result:'not_found',operation_id:'op'};}
function hsCompactOff(){} function hsBusy(v,b){v.busy=b;} function hsStatus(_v,_s,label){labelSeen=label;}
function hsReconcile(){} function hsQFlush(){} function hsIndex(){indexed++;} function clearTimeout(){}
(async()=>{
 const turn={el:{remove(){removed++;}}};
 const v={sid:'s',manualCompact:true,manualCompactID:'op',manualCompactTurn:turn,
          turns:[turn],busy:true,turnReady:false};
 await hsCompactPoll(v);
 assert.deepEqual(calls,[['/session/s/compact/op',undefined],['/session/s/compact/op/cancel-unaccepted','POST']]);
 assert.equal(removed,1);assert.equal(indexed,1);assert.equal(v.turns.length,0);
 assert.equal(v.manualCompact,false);assert.equal(v.busy,false);assert.equal(v.turnReady,true);
 assert.equal(labelSeen,'压缩未开始，请重试');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_shepherd_compact_acceptance_polls_when_sse_is_lost(self):
        self.js([('app-chat-core.js','hsCompactTerminal'),('app-chat-input.js','hsCompactPoll'),('app-chat-input.js','hsCompact')], r'''
const assert=require('node:assert/strict');
let syncs=0,timers=[],acceptedID='';
async function api(path,opts){
 syncs++;
 if(opts&&opts.method==='POST'){
  acceptedID=JSON.parse(opts.body).operation_id;
  return {ok:true,accepted:true,result:'running',operation_id:acceptedID};
 }
 return {ok:true,running:true,result:'running',operation_id:acceptedID};
}
function setTimeout(fn){timers.push(fn);return timers.length;}
function hsErrorNotice(){throw new Error('must not fail');} function hsStatusClear(){}
function hsNewTurn(){return {};} function hsBusy(v,b){v.busy=b;} function hsSetStick(){}
function hsTouch(){} function hsStatus(){} function hsScroll(){} function hsCompactOn(){}
function hsCompactOff(){} function hsReconcile(){} function hsQFlush(){}
(async()=>{
 const v={sid:'s',shepherdMode:true,busy:false,turnReady:true,caps:{compact:true},turns:[{}]};
 await hsCompact(v);
 assert.equal(syncs,2);assert.equal(timers.length,1);
 assert.equal(v.busy,true);assert.equal(v.manualCompact,true);assert.equal(v.cmpLive,true);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')
