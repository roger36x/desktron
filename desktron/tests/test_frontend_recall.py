"""执行实际前端函数，验证撤回阻塞不会吞草稿、重复发请求或遮住其他警告。"""
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]


def fn(file, name):
    source = (ROOT/"frontend"/file).read_text(encoding='utf-8')
    match = re.search(r"(?:async )?function " + name + r"\([^\n]*\) \{.*?\n\}", source, re.S)
    assert match, name
    return match.group(0)


class RecallUI(unittest.TestCase):
    def run_js(self, functions, script):
        selected = list(functions)
        names = {name for _, name in selected}
        for name in ('hsQIssue', 'hsQRetry', 'hsQEdit'):
            if name not in names:
                selected.append(('app-chat-composer.js', name))
        code = '\n'.join(fn(*args) for args in selected) + '\n' + script
        # stdin avoids Windows' command-line size limit as shared helpers grow.
        result = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pending_blocks_send_and_queue_without_touching_drafts(self):
        self.run_js([("app-chat-composer.js", f) for f in ("hsSend", "hsQFlush", "hsDispatch")], r'''
const assert = require('node:assert/strict');
let panels = 0;
function hsRecallPanel() { panels++; }
const v = {recallState: {phase:'blocked'}, inp:{textContent:'edited message'},
  atts:[{name:'image.png'}], queue:[{text:'queued'}]};
(async () => {
  await hsSend(v); await hsQFlush(v);
  assert.equal(await hsDispatch(v, 'edited message', v.atts), false);
  assert.equal(v.inp.textContent, 'edited message');
  assert.equal(v.atts.length, 1); assert.equal(v.queue.length, 1);
  assert.equal(panels, 2);
})().catch(e => { console.error(e); process.exitCode=1; });
''')

    def test_repeated_escape_does_not_send_second_abort(self):
        self.run_js([("app-chat-composer.js", "hsInterrupt")], r'''
const assert = require('node:assert/strict');
let calls = 0, release;
function api() { calls++; return new Promise(resolve => {release=resolve;}); }
for (const name of ['hsRecallStateSync','hsIndex','hsFill','hsBusy','hsDraftSave']) globalThis[name]=()=>{};
const turn = {parts:new Map(), user:'draft', el:{remove(){}}};
const v = {sid:'s',busy:true,turns:[turn],queue:[]};
function hsStateGeneration() {return 1;}
(async () => {
  const first = hsInterrupt(v); await hsInterrupt(v);
  assert.equal(calls,1); release({text:'draft',generation:1}); await first;
  assert.equal(v.interruptRequest,false); assert.equal(v.turns.length,0);
})().catch(e => { console.error(e); process.exitCode=1; });
''')

    def test_structured_rejection_removes_temporary_turn_without_guessing_delivery(self):
        self.run_js([("app-chat-composer.js", "hsDispatch")], r'''
const assert = require('node:assert/strict');
let removed=0, guess=0;
const old={id:'old'}; const v={sid:'s',turns:[old],turnGeneration:42,atts:[]};
for (const name of ['hsStatusClear','hsSetStick','hsTouch','hsStatus','hsScroll','hsDraftSave',
                    'hsIndex','hsRecallPanel','hsQPaint']) globalThis[name]=()=>{};
function hsBusy(v,b) {v.busy=b;}
function hsNewTurn(v) {const t={el:{remove(){removed++;}}};v.turns.push(t);return t;}
function hsRecallStateSync(v,p) {v.recallState=p.recall;}
function hsSentOrNot() {guess++; throw new Error('must not guess a rejected request');}
async function api() {const e=new Error('pending');e.code='recall_pending';
  e.data={recall:{id:'op',phase:'blocked'}};throw e;}
(async()=>{
  assert.equal(await hsDispatch(v,'same text as an older message',[]),false);
  assert.equal(guess,0);assert.equal(removed,1);assert.equal(v.turnGeneration,42);
  assert.deepEqual(v.turns,[old]);assert.equal(v.sending,null);assert.equal(v.busy,false);
  assert.equal(v.recallState.phase,'blocked');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_recall_completion_does_not_hide_other_warning(self):
        self.run_js([("app-chat-core.js", "hsRecallStateSync")], r'''
const assert = require('node:assert/strict');
let hidden=0, shown=0;
function hsEnvironment() {shown++;}
function hsEnvironmentHide() {hidden++;}
const v={envEl:{querySelector(){return null;}},envStage:'recall'};
hsRecallStateSync(v,{recall:{phase:'blocked'}}); assert.equal(shown,1);
v.envStage='mcp'; hsRecallStateSync(v,{recall:{phase:'done'}}); assert.equal(hidden,0);
v.recallShown=true; v.envStage='recall'; hsRecallStateSync(v,{}); assert.equal(hidden,1);
''')

    def test_pending_recall_shows_automatic_progress_without_strategy_buttons(self):
        self.run_js([
            ("app-chat-core.js", "hsRecallStateSync"),
        ], r'''
const assert = require('node:assert/strict');
let called=0, env=null;
function hsEnvironment(v,p){env=p;}
function hsEnvironmentHide(){}
function hsRecallPanel(){called++;}
function hsRecallOpen(){}
const v={envEl:{querySelector(){return null;}},envStage:'',recallResolving:false};
hsRecallStateSync(v,{recall:{id:'op',phase:'retryable',execution_state:'stopped'}});
assert.equal(env.label,'正在自动恢复撤回');
assert.equal(env.action,null);
assert.equal(called,0);
''')

    def test_failed_auto_recovery_exposes_only_retry_not_internal_choices(self):
        self.run_js([
            ("app-chat-core.js", "hsRecallStateSync"),
        ], r'''
const assert = require('node:assert/strict');
let env=null;
function hsEnvironment(v,p){env=p;}
function hsEnvironmentHide(){}
function hsRecallPanel(){}
function hsRecallOpen(){}
const v={envEl:{querySelector(){return null;}},envStage:'',recallResolving:false};
hsRecallStateSync(v,{recall:{id:'op',phase:'retryable',execution_state:'stopped',
  auto_failed:true,error:'offline'}});
assert.equal(env.label,'自动恢复未完成');
assert.equal(env.action.label,'再次尝试');
assert.equal(env.action.kind,'recall');
''')

    def test_inflight_recall_waits_without_duplicate_auto_request(self):
        self.run_js([
            ("app-chat-core.js", "hsRecallStateSync"),
        ], r'''
const assert = require('node:assert/strict');
let called=0;
function hsEnvironment(){}
function hsEnvironmentHide(){}
function hsRecallPanel(){called++;}
function hsRecallOpen(){}
const v={envEl:{querySelector(){return null;}},envStage:'',recallResolving:false};
hsRecallStateSync(v,{recall:{id:'op',phase:'applying',execution_state:'stopped'}});
assert.equal(called,0);
''')

    def test_strategy_button_styles_are_removed(self):
        css = (ROOT/"frontend"/"style.css").read_text(encoding='utf-8')
        self.assertNotIn(".hs-recall-actions", css)


class RecallAttachmentBytes(unittest.TestCase):
    """撤回重建过的回合时，图片只有 /session/.../asset 引用，放回输入框前要取回字节。"""

    run_js = RecallUI.run_js

    HELPERS = [("app-chat-composer.js", f) for f in
               ("hsBlobB64", "hsAttHydrateOne", "hsAttRestore", "hsInterrupt", "hsSend")]
    PRELUDE = r'''
const assert = require('node:assert/strict');
const HS_ATT_MAX = 12 * 1024 * 1024, HS_ATT_FETCH_MS = 1000;
globalThis.FileReader = class { readAsDataURL(b) { this.result = 'data:x;base64,' + b._b64; this.onload(); } };
const envs = [];
function hsEnvironment(_v, p) { envs.push(p); }
for (const name of ['hsRecallStateSync','hsIndex','hsFill','hsBusy','hsDraftSave','hsAttPaint','hsRecallPanel']) globalThis[name]=()=>{};
function hsStateGeneration() { return 1; }
'''

    def test_reference_only_image_is_restored_with_bytes(self):
        self.run_js(self.HELPERS, self.PRELUDE + r'''
globalThis.fetch = async (url) => ({ ok: url.endsWith('/asset/1789_abc123_shot.png'),
  blob: async () => ({ size: 3, type: 'image/png', _b64: 'QUJD' }) });
function api() { return Promise.resolve({ text: 'draft', generation: 1 }); }
const turn = { parts: new Map(), user: 'draft', el: { remove(){} },
  atts: [{ type: 'image', media_type: 'image/png', url: '/session/s/asset/1789_abc123_shot.png' }] };
const v = { sid: 's', busy: true, turns: [turn], queue: [] };
(async () => {
  await hsInterrupt(v);
  await v.attRestoring;
  assert.equal(v.atts.length, 1);
  assert.equal(v.atts[0].data_b64, 'QUJD');
  assert.equal(v.atts[0].name, 'shot.png');
  assert.equal(envs.length, 0);
})().catch(e => { console.error(e); process.exitCode=1; });
''')

    def test_unfetchable_image_warns_and_blocks_send(self):
        self.run_js(self.HELPERS, self.PRELUDE + r'''
globalThis.fetch = async () => ({ ok: false });
let dispatched = 0;
function hsDispatch() { dispatched++; return Promise.resolve(true); }
function hsInpText(inp) { return inp.textContent; }
const v = { sid: 's', inp: { textContent: 'hello' }, queue: [],
  atts: [{ type: 'image', url: '/session/s/asset/gone.png' }] };
(async () => {
  await hsSend(v);
  assert.equal(dispatched, 0);
  assert.equal(v.inp.textContent, 'hello');
  assert.equal(envs.length, 1);
  assert.equal(envs[0].stage, 'attachment');
})().catch(e => { console.error(e); process.exitCode=1; });
''')


if __name__ == '__main__':
    unittest.main()
