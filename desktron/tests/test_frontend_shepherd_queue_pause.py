"""Actual JS: stop, late ready, refresh and repeated resume must not lose a queue."""
import unittest
from test_frontend_recall import RecallUI


class ShepherdQueuePause(unittest.TestCase):
    run_js = RecallUI.run_js

    def test_confirmed_stop_drains_only_after_ready(self):
        self.run_js([('app-chat-composer.js', f) for f in ('hsInterrupt','hsQFlush','hsQResume')], r'''
const assert = require('node:assert/strict');
let aborts=0, sends=0, saved=0, resolveAbort;
function api(){aborts++;return new Promise(r=>resolveAbort=r)}
function hsDraftSave(){saved++}
for(const n of ['hsQPaint','hsFlush','hsFinalize','hsStatus','hsIndex','hsScheduleBusyVerify']) globalThis[n]=()=>{};
const HS_INTERRUPT_VERIFY_MS=1;
async function hsDispatch(v,text,atts){sends++;assert.equal(text,'queued');assert.equal(atts.length,1);return true}
const v={sid:'s',shepherdMode:true,busy:true,turnReady:false,
  turns:[{parts:new Map([['x',{}]])}],queue:[{text:'queued',atts:[{name:'image.png'}]}]};
(async()=>{
  const stopping=hsInterrupt(v); await hsInterrupt(v);
  assert.equal(aborts,1);assert.notEqual(v.queuePaused,true);
  v.busy=false;v.turnReady=true;await hsQFlush(v);await hsQResume(v);
  assert.equal(sends,0);assert.equal(v.queue.length,1);
  resolveAbort({});await stopping;
  assert.equal(sends,1);assert.equal(v.queue.length,0);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_abort_without_receipt_pauses_and_keeps_queue(self):
        self.run_js([('app-chat-composer.js', f) for f in ('hsInterrupt','hsQFlush')], r'''
const assert=require('node:assert/strict');let sends=0,painted=0,saved=0;
async function api(){throw Error('offline')}
function hsQPaint(){painted++} function hsDraftSave(){saved++}
for(const n of ['hsFlush','hsFinalize','hsStatus','hsIndex','hsScheduleBusyVerify']) globalThis[n]=()=>{};
async function hsDispatch(){sends++;return true}
const v={sid:'s',shepherdMode:true,busy:true,turnReady:false,
  turns:[{parts:new Map([['x',{}]])}],queue:[{text:'queued',atts:[]}]};
(async()=>{
  assert.equal(await hsInterrupt(v),false);
  assert.equal(v.queuePaused,false);assert.equal(v.turnReady,false);assert.ok(painted);assert.ok(saved);
  v.busy=false;v.turnReady=true;await hsQFlush(v);
  assert.equal(sends,1);assert.equal(v.queue.length,0);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_normal_engine_still_drains_and_failed_shepherd_send_keeps_content(self):
        self.run_js([('app-chat-composer.js', f) for f in ('hsQFlush','hsQResume')], r'''
const assert=require('node:assert/strict');let sends=0,ok=true;
function hsQPaint(){} function hsDraftSave(){}
async function hsDispatch(){sends++;return ok}
(async()=>{
  const normal={shepherdMode:false,queuePaused:true,queue:[{text:'normal'}],turnReady:true};
  await hsQFlush(normal);assert.equal(sends,0);assert.equal(normal.queue.length,1);
  await hsQResume(normal);assert.equal(sends,1);assert.equal(normal.queue.length,0);
  const v={shepherdMode:true,queuePaused:true,queue:[{text:'safe',atts:[{name:'image'}]}],turnReady:true};
  ok=false;await hsQResume(v);assert.equal(v.queue.length,1);assert.equal(v.queue[0].atts[0].name,'image');
  assert.equal(v.queuePaused,true);await hsQFlush(v);assert.equal(sends,2);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_unknown_delivery_blocks_send_without_touching_new_draft(self):
        self.run_js([('app-chat-composer.js', f) for f in ('hsSend','hsQFlush','hsQResume')], r'''
const assert=require('node:assert/strict');let notices=0;
function hsEnvironment(){notices++}
function hsDispatch(){throw Error('must not send')}
const v={shepherdMode:true,pendingSend:{text:'old'},queuePaused:true,
  queue:[{text:'queued'}],inp:{textContent:'new draft'},atts:[{name:'image'}],turnReady:true};
(async()=>{await hsSend(v);await hsQFlush(v);await hsQResume(v);
  assert.equal(notices,1);assert.equal(v.queue.length,1);assert.equal(v.queuePaused,true);
  assert.equal(v.inp.textContent,'new draft');assert.equal(v.atts.length,1);
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_paused_queue_survives_refresh_with_attachments(self):
        self.run_js([('app-chat-composer.js', f) for f in ('hsDraftWrite','hsDraftLoad','hsQFlush')], r'''
const assert=require('node:assert/strict');let saved;
const HS_DRAFT_KEY=s=>s;
const localStorage={setItem(k,v){saved=v},getItem(){return saved},removeItem(){saved=null}};
function hsInpText(){return ''} function hsDraftAtts(a){return a||[]}
function hsQPaint(){} function hsAttPaint(){}
function hsDispatch(){throw Error('must not send after refresh')}
const v={sid:'s',shepherdMode:true,queuePaused:true,queue:[{text:'queued',atts:[{name:'image'}]}]};
hsDraftWrite(v);
const restored={sid:'s',turnReady:true};hsDraftLoad(restored);
assert.equal(restored.queuePaused,true);assert.equal(restored.queue[0].atts[0].name,'image');
hsDraftWrite(restored);assert.equal(JSON.parse(saved).qp,true);
hsQFlush(restored);
''')
