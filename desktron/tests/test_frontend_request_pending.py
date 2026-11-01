"""request_pending 409：服务端明确未接收，消息留在队首，等后端 ready 后自动重发。

2026-10-12 实见：Shepherd 回合结束后 ready 比回执收口早 0.06～0.3 秒，排队消息在这段
间隙里发出，被 409 拒绝后界面显示“已停止”，需要人工重发。
"""
import unittest
from test_frontend_recall import RecallUI

STUBS = r'''
const assert=require('node:assert/strict');let errors=0,removed=0,stateSyncs=0,posts=0,stopped=0;
for(const name of ['hsStatusClear','hsSetStick','hsTouch','hsScroll','hsIndex','hsRename','hsQPaint',
 'hsDraftSave','hsGoalSync','hsAttHydrateOne','hsTurnSettled']) globalThis[name]=()=>{};
function hsStatus(v,k){if(k==='error')stopped++;}
function hsNewTurn(v){const t={el:{remove(){removed++;}}};v.turns.push(t);return t;}
function hsBusy(v,b){v.busy=b;} function hsQIssue(v,k,msg){v.queueIssue=k?{kind:k,msg}:null;}
function hsErrorNotice(){errors++;} function hsEnvironment(){}
function hsStateTake(){return true} function hsStateGeneration(p){return p.generation||0}
async function hsSentOrNot(){throw Error('request_pending must not probe sent-or-not');}
let pending=true;
async function api(){posts++;if(pending){const e=new Error('busy');e.status=409;e.code='request_pending';throw e;}}
async function hsStateSync(v){stateSyncs++;}
const wait=ms=>new Promise(r=>setTimeout(r,ms));
'''


class RequestPendingRetry(unittest.TestCase):
    run_js = RecallUI.run_js
    FNS = [('app-chat-composer.js', f) for f in ('hsQPendingRetry', 'hsQFlush', 'hsDispatch')] + \
          [('app-chat-core.js', 'hsTurnReady')]

    def test_queued_send_is_kept_and_resent_after_ready(self):
        self.run_js(self.FNS, STUBS + r'''
const v={sid:'s',turns:[],turnGeneration:5,atts:[],busy:false,turnReady:true,
 queue:[{text:'later',atts:[]}]};
(async()=>{
  await hsQFlush(v);
  assert.equal(posts,1);assert.equal(removed,1);assert.equal(errors,0);assert.equal(stopped,0);
  assert.deepEqual(v.queue.map(q=>q.text),['later']);
  assert.equal(v.turnReady,false);assert.equal(v.busy,false);assert.equal(v.turnGeneration,5);
  assert.equal(v.queueIssue.kind,'retry');
  // 收口完成：后端补发的 ready 到达（可能早于 300ms 等待期），定时核对后完成重发。
  pending=false;hsTurnReady(v,{generation:5});
  await wait(450);
  assert.equal(posts,2);assert.equal(v.queue.length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_direct_send_moves_message_into_queue(self):
        self.run_js(self.FNS, STUBS + r'''
const v={sid:'s',turns:[],turnGeneration:5,atts:[],busy:false,turnReady:true,queue:[]};
(async()=>{
  assert.equal(await hsDispatch(v,'now',[]),true);
  assert.deepEqual(v.queue.map(q=>q.text),['now']);
  assert.equal(errors,0);assert.equal(stopped,0);assert.equal(v.sending,null);
  await wait(450);assert.equal(stateSyncs,1);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


if __name__ == '__main__':
    unittest.main()
