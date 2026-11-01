import unittest
from test_frontend_recall import RecallUI


class DeliveryReceipts(unittest.TestCase):
    run_js = RecallUI.run_js

    def test_not_found_is_cancelled_before_late_post_not_inferred_from_text(self):
        self.run_js([('app-chat-composer.js','hsSentOrNot')], r'''
const assert=require('node:assert/strict');const calls=[];
async function api(path,opts={}){calls.push([path,opts.method]);return {status:opts.method==='POST'?'cancelled':'not_found'}}
(async()=>{
 const got=await hsSentOrNot({sid:'s',shepherdMode:true,pendingSend:{requestID:'r'}},'same old text');
 assert.equal(got,'not-sent');assert.deepEqual(calls,[['/session/s/request/r',undefined],['/session/s/request/r/cancel-unsent','POST']]);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_late_started_response_is_sent_and_unknown_never_guessed(self):
        self.run_js([('app-chat-composer.js','hsSentOrNot')], r'''
const assert=require('node:assert/strict');let result='not_found';let count=0;
async function api(path,opts={}){count++;return {status:opts.method==='POST'?'started':result}}
(async()=>{
 const v={sid:'s',shepherdMode:true,pendingSend:{requestID:'r'}};
 assert.equal(await hsSentOrNot(v,'text'),'sent');assert.equal(count,2);
 result='accepted';assert.equal(await hsSentOrNot(v,'text'),'unknown');
 result='uncertain';assert.equal(await hsSentOrNot(v,'text'),'sent');
 count=0;assert.equal(await hsSentOrNot({sid:'s',shepherdMode:true},'text'),'unknown');assert.equal(count,0);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_draft_keeps_request_id_and_reboot_pauses_queue(self):
        self.run_js([('app-chat-composer.js', f) for f in ('hsDraftWrite','hsDraftLoad')], r'''
const assert=require('node:assert/strict');let saved;let _hsRuntime={boot_id:'old'};
const HS_DRAFT_KEY=s=>s;
const localStorage={setItem(k,v){saved=v},getItem(){return saved},removeItem(){saved=null}};
function hsInpText(){return ''} function hsDraftAtts(a){return a||[]}
function hsQPaint(){} function hsAttPaint(){}
const v={sid:'s',shepherdMode:true,queue:[{text:'queued'}],sending:{text:'sent',requestID:'r'}};
hsDraftWrite(v);_hsRuntime={boot_id:'new'};
const restored={sid:'s'};hsDraftLoad(restored);
assert.equal(restored.pendingSend.requestID,'r');assert.equal(restored.queuePaused,false);assert.equal(restored.turnReady,false);
assert.equal(restored.queue[0].text,'queued');
''')
