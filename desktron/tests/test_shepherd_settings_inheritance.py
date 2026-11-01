"""Changing several Shepherd settings must preserve the full next-session snapshot."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))


@unittest.skipUnless(shutil.which('node'), 'requires Node.js')
class ShepherdSettingsInheritance(unittest.TestCase):
    def run_js(self, body):
        source = (ROOT / 'frontend/app-chat-opts.js').read_text(encoding='utf-8')
        start = source.index('async function hsSetOpt(')
        end = source.index('\n// 档位菜单', start)
        harness = r'''
const assert = require('node:assert/strict');
const state = {opts:{}};
const sessions = {};
const calls = [];
let engineInfo = {opts:{}};
let engineOpts = {};
const curEngine = 'claude';
function deskLog(message) { throw new Error(message); }
function hsOptsPaint(v) { v.opts = v.sessionOpts; }
async function hsEngineSync(v) { hsOptsPaint(v); }
async function api(path, request) {
  const body = JSON.parse(request.body);
  calls.push({path, body});
  if (path === '/workbench/engine') {
    for (const [engine, opts] of Object.entries(body.opts)) {
      state.opts[engine] = {...state.opts[engine], ...opts};
      delete state.opts[engine].desk_role;
    }
    if (body.shepherd_engine) state.shepherd_engine = body.shepherd_engine;
    return {ok:true};
  }
  const sid = decodeURIComponent(path.split('/')[2]);
  sessions[sid] = {...sessions[sid], ...body.opts};
  return {opts:{...sessions[sid]}};
}
function view(engine) {
  const initial = {model:'old-model', effort:'low', autocompact:'200000', desk_role:'role'};
  const sid = engine + '-session';
  sessions[sid] = {...initial};
  state.opts[engine] = {...initial};
  const v = {sid, engine, shepherdMode:true, sessionOpts:{...initial}, opts:{...initial},
    engineSnap:{opts:{[engine]:{...initial}}}};
  return v;
}
'''
        result = subprocess.run(['node', '-e', harness + source[start:end] + '\n' + body],
                                capture_output=True, text=True, timeout=20, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_sequential_model_effort_and_compaction_preserve_next_session_defaults(self):
        self.run_js(r'''
(async () => {
  for (const engine of ['claude', 'codex', 'pi']) {
    const v = view(engine);
    const untouched = {...sessions[v.sid]};
    await hsSetOpt(v, {model:'chosen-model'});
    assert.equal(v.sessionOpts.model, 'chosen-model');
    await hsSetOpt(v, {effort:'high'});
    await hsSetOpt(v, {autocompact:'600000'});
    for (const opts of [v.opts, v.sessionOpts, state.opts[engine], v.engineSnap.opts[engine]]) {
      assert.equal(opts.model, 'chosen-model');
      assert.equal(opts.effort, 'high');
      assert.equal(opts.autocompact, '600000');
    }
    assert.equal(state.opts[engine].desk_role, undefined);
    assert.equal(engineInfo.shepherd_engine, engine);
    assert.equal(engineOpts[engine].model, 'chosen-model');
    assert.equal(untouched.model, 'old-model');
    assert.equal(sessions[v.sid].desk_role, 'role');
    await hsSetOpt(v, {model:'', autocompact:''});
    assert.equal(state.opts[engine].model, '');
    assert.equal(state.opts[engine].autocompact, '');
    assert.equal(state.opts[engine].effort, 'high');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
''')

    def test_saved_frontend_defaults_are_snapshotted_by_new_backend_session(self):
        from engine_adapters.bus import EventBus
        from engine_adapters.shepherd import ShepherdAdapter

        output = self.run_js(r'''
(async () => {
  const v = view('pi');
  await hsSetOpt(v, {model:'example/model'});
  await hsSetOpt(v, {effort:'high'});
  await hsSetOpt(v, {autocompact:'400000'});
  console.log(JSON.stringify(state.opts.pi));
})().catch(error => { console.error(error); process.exitCode = 1; });
''')
        defaults = json.loads(output)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = ShepherdAdapter(str(root / 'data'), str(root / 'workspace'), EventBus())
            adapter.service = mock.Mock()
            adapter.service.create.return_value = {'binding_id':'binding', 'lease':{'generation':1}}
            with mock.patch('workbench._wb_state_read', return_value={'engine_opts':{'pi':defaults}}):
                sid = adapter.create_session(opts={'active_engine':'pi'})['id']
            saved = adapter.saved_engine_options(sid, 'pi')
            self.assertEqual(saved, defaults)
            adapter.update_session_options(sid, {'autocompact':''}, engine='pi')
            options = adapter.session_options(sid, {'model':'other', 'effort':'low',
                                                   'autocompact':'600000'}, engine='pi')
            self.assertEqual(options['model'], 'example/model')
            self.assertEqual(options['effort'], 'high')
            self.assertEqual(options['autocompact'], '')
            restored = ShepherdAdapter(str(root / 'data'), str(root / 'workspace'), EventBus())
            self.assertEqual(restored.saved_engine_options(sid, 'pi'), options)

    def test_new_session_uses_remembered_shepherd_engine_but_honors_explicit_engine(self):
        source = (ROOT / 'frontend/app-chat-opts.js').read_text(encoding='utf-8')
        helper = source[source.index('function hsNewSessionEngine('):source.index('async function hsEngineSync(')]
        page = (ROOT / 'frontend/app-chat-page.js').read_text(encoding='utf-8')
        create = page[page.index('async function newHtmlSession(opts)'):]
        assets = (ROOT / 'frontend/app-assets-apps.js').read_text(encoding='utf-8')
        welcome = assets[assets.index('function _welcomeEngine('):assets.index('function _welcomeHTML(')]
        terminal = assets[assets.index('async function newTerminal('):assets.index('// ---- Store:')]
        script = r'''
const assert = require('node:assert/strict');
const curEngine = 'claude', shepherdMode = true, shepherdTrial = false;
let engineInfo = {shepherd_engine:'pi'};
const requests = [];
function claimMainNavigation() { return 1; }
function curDomainId() { return 'workflow'; }
function alert(message) { throw Error(message); }
async function openHtmlSession() {}
async function api(path, request) {
  requests.push(JSON.parse(request.body)); return {id:'new-session'};
}
'''
        script += helper + create + welcome + terminal + r'''
(async () => {
  await newHtmlSession();
  assert.equal(requests[0].engine, 'pi');
  assert.equal(requests[0].shepherd_mode, true);
  await newHtmlSession({engine:'codex'});
  assert.equal(requests[1].engine, 'codex');
  await newHtmlSession({shepherd_mode:false});
  assert.equal(requests[2].engine, 'claude');
  await newTerminal(curEngine, 'workflow');
  assert.equal(requests[3].engine, 'pi');
  assert.equal(_welcomeEngine(), 'pi');
  engineInfo.shepherd_engine = 'invalid';
  assert.equal(hsNewSessionEngine(true), 'claude');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=20, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_late_settings_response_does_not_replace_another_engine_view(self):
        self.run_js(r'''
(async () => {
  const v = view('pi');
  let release;
  const original = api;
  api = async (path, request) => {
    const out = await original(path, request);
    if (path.endsWith('/settings')) await new Promise(resolve => { release = resolve; });
    return out;
  };
  const pending = hsSetOpt(v, {model:'pi-choice', effort:'high'});
  await Promise.resolve();
  v.engine = 'claude';
  v.sessionOpts = v.opts = {model:'opus', effort:'max'};
  release();
  await pending;
  assert.deepEqual(v.sessionOpts, {model:'opus', effort:'max'});
  assert.equal(v.shepherdEngineState.pi.opts.model, 'pi-choice');
  assert.equal(state.opts.pi.model, 'pi-choice');
})().catch(error => { console.error(error); process.exitCode = 1; });
''')


if __name__ == '__main__':
    unittest.main()
