"""目标会话能看到后台隔离问询，普通发送不受阻塞。"""
import json
import os
from pathlib import Path
from test_frontend_recall import fn
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
CORE = (ROOT / "frontend/app-chat-core.js").read_text(encoding="utf-8")
COMPOSER = (ROOT / "frontend/app-chat-composer.js").read_text(encoding="utf-8")
PAGE = (ROOT / "frontend/app-chat-page.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend/style.css").read_text(encoding="utf-8")


class PeerAskStatusFrontend(unittest.TestCase):
    def test_status_is_visible_without_exposing_question_body(self):
        self.assertIn('class="hs-peer hs-col"', PAGE)
        # 提示位于上下文托盘上方；正文仍不暴露问询内容。
        self.assertNotIn("hs-peer-note", PAGE + CSS)
        self.assertNotIn("后台处理，不影响当前会话", PAGE)
        self.assertLess(PAGE.index('class="hs-peer hs-col"'), PAGE.index('class="hs-context hs-col"'))
        self.assertIn('type === "session.peer"', CORE)
        self.assertIn('hasOwnProperty.call(st, "peer")', CORE)
        self.assertIn('.hs-page .hs-peer[hidden] { display: none; }', CSS)
        self.assertNotIn('peer.question', CORE + PAGE)

    def test_prompt_does_not_confirm_or_cancel_peer(self):
        self.assertNotIn('e.code === "peer_ask_active"', COMPOSER)
        self.assertNotIn('confirm_peer_cancel', COMPOSER)
        self.assertNotIn('hsPeerCancelConfirm', COMPOSER + CORE)

    def test_peer_status_does_not_pause_ordinary_queue(self):
        queue = COMPOSER[COMPOSER.index('async function hsQFlush('):COMPOSER.index('async function hsSend(')]
        dispatch = COMPOSER[COMPOSER.index('async function hsDispatch('):COMPOSER.index('  const previousGeneration')]
        script = f"""
const vm=require('vm'), assert=require('assert');
const ctx={{hsQPaint:()=>{{}},hsDraftSave:()=>{{}},
  hsDispatch:async()=>true}};
vm.createContext(ctx);
vm.runInContext({json.dumps(fn('app-chat-composer.js', 'hsQIssue') + queue)},ctx);
(async()=>{{
  const item={{text:'pending',atts:[]}};
  const v={{shepherdMode:false,queue:[item],turnReady:true,peerAsk:{{requestID:'ask'}}}};
  await ctx.hsQFlush(v);
  assert.equal(v.queue.length,0);assert.equal(v.queueIssue,null);
}})().catch(e=>{{console.error(e);process.exitCode=1;}});
"""
        result = subprocess.run(["node", "-e", script], cwd=ROOT,
                                text=True, capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_status_revision_rejects_late_events_and_names_source(self):
        start = CORE.index("function hsPeerSync(")
        end = CORE.index("function hsStateGeneration", start)
        source = CORE[start:end]
        script = f"""
const vm=require('vm'), assert=require('assert');
const ctx={{}};
vm.createContext(ctx);
vm.runInContext({json.dumps(source)},ctx);
const main={{textContent:''}},phase={{textContent:''}};
const el={{hidden:true,querySelector:q=>q==='.hs-peer-main'?main:phase}};
const v={{peerEl:el,peerAsk:null,peerEpoch:'',peerRevision:0}};
ctx.hsPeerSync(v,{{active:true,epoch:'old',revision:2,phase:'running',source:'desktop · 来源会话:'}});
assert.equal(el.hidden,false);
assert.equal(main.textContent,'desktop · 来源会话');
assert.equal(main.title,main.textContent);
assert.equal(phase.textContent,'正在处理');
ctx.hsPeerSync(v,{{active:false,epoch:'old',revision:1}});
assert.equal(el.hidden,false);
ctx.hsPeerSync(v,{{active:false,epoch:'old',revision:0}});
assert.equal(el.hidden,false);
assert.equal(v.peerRevision,2);
ctx.hsPeerSync(v,{{active:false,epoch:'old',revision:3}});
assert.equal(el.hidden,true);
ctx.hsPeerSync(v,{{active:true,epoch:'new',revision:1,phase:'queued',source:'重启后会话'}});
assert.equal(el.hidden,false);
assert.equal(v.peerRevision,1);
assert.equal(phase.textContent,'等待处理');
assert.equal(main.title,'重启后会话');
"""
        result = subprocess.run(["node", "-e", script], cwd=ROOT,
                                text=True, capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
