"""Shepherd Pi session-scoped compaction threshold.

Pi 0.85.1 compacts when context > contextWindow - reserveTokens and re-reads its
settings at every check. Shepherd's launcher owns a private in-memory settings
manager, so each turn restates the session threshold through a `desk_compaction`
control frame and the resident host changes behaviour without a restart.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / 'desktron' / 'lib'
sys.path.insert(0, str(LIB))
_argv = sys.argv
sys.argv = ['ui_server.py', str(ROOT / 'frontend'), '7999', '7998', str(ROOT)]
try:
    import engine_adapters as ea
    from shepherd.pi_compaction import (PI_COMPACTION_MIN_HEADROOM, annotate_pi_catalog,
                                        normalize_pi_autocompact, pi_autocompact_choices,
                                        pi_compaction_frame, pi_compaction_threshold)
    from shepherd.pi_connector import pi_sdk_root
finally:
    sys.argv = _argv


class _Bus:
    def publish(self, *a, **k):
        pass


def _worker(cls):
    return cls(tempfile.mkdtemp(), str(ROOT), _Bus())


class Thresholds(unittest.TestCase):
    def test_only_declared_choices_survive(self):
        self.assertEqual(normalize_pi_autocompact('400000'), '400000')
        self.assertEqual(normalize_pi_autocompact(' 400000 '), '400000')
        for bad in ('', None, '123', 400001, 'auto'):
            self.assertEqual(normalize_pi_autocompact(bad), '', bad)
        self.assertEqual(pi_compaction_threshold({'autocompact': '200000'}), 200000)
        self.assertEqual(pi_compaction_threshold({'autocompact': '999'}), 0)
        self.assertEqual(pi_compaction_threshold(None), 0)
        self.assertEqual(pi_autocompact_choices()[0], {'id': '', 'label': '自动'})
        self.assertEqual(pi_autocompact_choices(128000), [{'id':'', 'label':'自动'}])
        self.assertEqual(pi_autocompact_choices(272000), [
            {'id':'', 'label':'自动'}, {'id':'200000', 'label':'20 万'}])
        self.assertEqual([row['id'] for row in pi_autocompact_choices(800000)],
                         ['', '200000', '400000', '600000'])


class ShepherdOptions(unittest.TestCase):
    def test_pi_threshold_is_kept_by_shepherd_but_not_native_pi(self):
        pi = _worker(ea.PiAdapter)
        self.assertNotIn('autocompact', pi.normalize_options({'autocompact': '400000'}))
        value = ea.ShepherdAdapter._engine_options('pi', pi, {'autocompact': '400000'})
        self.assertEqual(value['autocompact'], '400000')
        self.assertNotIn('autocompact',
                         ea.ShepherdAdapter._engine_options('pi', pi, {'autocompact': '1'}))
        self.assertEqual(
            ea.ShepherdAdapter._engine_options('pi', pi, {'autocompact': ''})['autocompact'], '')

    def test_other_engines_keep_their_own_normalization(self):
        codex = _worker(ea.CodexAdapter)
        self.assertEqual(ea.ShepherdAdapter._engine_options('codex', codex, {'autocompact': '400000'}),
                         codex.normalize_options({'autocompact': '400000'}))

    def test_catalog_clips_tiers_to_each_model_window(self):
        base = {'ui': {'modeKey': 'mode'}, 'models': [
            {'id': 'deepseek/v4-pro', 'contextWindow': 1000000},
            {'id': 'openai/spark', 'contextWindow': 128000},
            {'id': 'openai/sol', 'contextWindow': 272000},
            {'id': 'example-gw/sol:dev', 'contextWindow': 240000, 'family': 'example-gw/sol',
             'variants': [{'id': 'example-gw/sol:dev', 'key': 'standard', 'contextWindow': 240000},
                          {'id': 'example-gw/sol:max', 'key': 'max', 'contextWindow': 936000}]},
        ]}
        pi = ea.ShepherdAdapter._engine_catalog('pi', base)
        long_model, spark, openai, family = pi['models']
        self.assertEqual(long_model['autocompacts'], pi_autocompact_choices(1000000))
        self.assertEqual(spark['autocompacts'], [{'id':'', 'label':'自动'}])
        self.assertEqual(openai['autocompacts'], [
            {'id':'', 'label':'自动'}, {'id':'200000', 'label':'20 万'}])
        self.assertEqual(family['autocompacts'], [{'id':'', 'label':'自动'}])
        standard, maximum = family['variants']
        self.assertEqual(standard['autocompacts'], [{'id':'', 'label':'自动'}])
        self.assertEqual(maximum['autocompacts'], pi_autocompact_choices(936000))
        self.assertNotIn('autocompacts', pi['ui'])
        self.assertEqual(pi['ui']['modeKey'], 'mode')
        self.assertNotIn('autocompacts', base['models'][0])
        self.assertIs(ea.ShepherdAdapter._engine_catalog('claude', base), base)

    def test_manual_tier_boundary_is_inclusive(self):
        boundary = 200000 + PI_COMPACTION_MIN_HEADROOM
        rows = annotate_pi_catalog({'models': [
            {'id': 'a', 'contextWindow': boundary},
            {'id': 'b', 'contextWindow': boundary - 1},
            {'id': 'c', 'contextWindow': None}]})['models']
        self.assertEqual([x['id'] for x in rows[0]['autocompacts']], ['', '200000'])
        self.assertEqual(rows[1]['autocompacts'], [{'id':'', 'label':'自动'}])
        self.assertNotIn('autocompacts', rows[2])


class RunnerSendsThreshold(unittest.TestCase):
    def test_control_frame_precedes_prompt_and_skips_manual_compact(self):
        source = (LIB / 'shepherd' / 'unified_runners.py').read_text(encoding='utf-8')
        pi = source.split('def _pi(', 1)[1].split('\n    def ', 1)[0]
        frame = pi.index("process.send(pi_compaction_frame(self.options))")
        prompt = pi.index("self._timed('pi.prompt_send', process.send,")
        self.assertLess(frame, prompt)
        self.assertIn("if prompt!='/compact':", pi[:frame])
        self.assertIn("if kind=='desk_compaction': continue", pi)

    def test_frame_carries_window_gate(self):
        self.assertEqual(pi_compaction_frame({'autocompact': '400000'}),
                         {'type': 'desk_compaction', 'threshold': 400000,
                          'minHeadroom': PI_COMPACTION_MIN_HEADROOM})
        self.assertEqual(pi_compaction_frame({})['threshold'], 0)


def _pi_model():
    try:
        providers = json.loads((Path.home() / '.pi/agent/models.json').read_text()).get('providers') or {}
    except (OSError, ValueError):
        return ''
    for provider, row in providers.items():
        for model in row.get('models') or []:
            if (model.get('contextWindow') or 0) >= 800000 and (model.get('maxTokens') or 0) > 0:
                return f"{provider}/{model['id']}"
    return ''


@unittest.skipUnless(pi_sdk_root() and _pi_model(), 'pinned Pi SDK and a large-window Pi model required')
class LauncherControlFrame(unittest.TestCase):
    def test_threshold_applies_in_order_restores_and_passes_other_lines(self):
        folder = tempfile.mkdtemp()
        boot = Path(folder) / 'boot.json'
        boot.write_text(json.dumps({'id': 'threshold-test', 'messages': [], 'entries': None,
                                    'compact': False}))
        proc = subprocess.Popen(
            ['node', str(LIB / 'shepherd' / 'pi_memory_rpc.mjs'), pi_sdk_root(), str(boot),
             '--mode', 'rpc', '--offline', '--no-extensions', '--no-tools', '--model', _pi_model()],
            cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        rows = []
        threading.Thread(target=lambda: [rows.append(json.loads(line)) for line in proc.stdout
                                         if line.startswith('{')], daemon=True).start()

        def send(*frames):
            proc.stdin.write(''.join(json.dumps(f, ensure_ascii=False) + '\n' for f in frames))
            proc.stdin.flush()

        def wait_for(count):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and len(rows) < count:
                time.sleep(0.05)
            self.assertGreaterEqual(len(rows), count, proc.stderr.read() if proc.poll() is not None else rows)

        try:
            wait_for(1)
            from shepherd.phase_timing import HOST_STAGES
            self.assertEqual(set(rows[0].pop('startup_timing')), HOST_STAGES)
            self.assertEqual(rows[0], {'type':'protocol_ready','contract':'desk-pi-execute-v1',
                                       'sessionId':'threshold-test'})
            send(pi_compaction_frame({'autocompact': '400000'}), {'id': 'a', 'type': 'get_state'})
            wait_for(3)
            send(pi_compaction_frame({}),
                 {'id': 'b', 'type': 'mentions "desk_compaction" but is not one'})
            wait_for(5)
            send({**pi_compaction_frame({'autocompact': '200000'}), 'minHeadroom': 2000000})
            wait_for(6)
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
            proc.stdout.close(); proc.stderr.close()
        applied, state, native, other, small = rows[1:6]
        self.assertEqual((small['type'], small['status']), ('desk_compaction', 'above_window'))
        self.assertEqual(small['reserveTokens'], native['reserveTokens'])
        self.assertEqual((applied['type'], applied['status']), ('desk_compaction', 'applied'))
        self.assertEqual(applied['reserveTokens'], applied['contextWindow'] - 400000)
        self.assertEqual((state['type'], state['id']), ('response', 'a'))
        self.assertEqual((native['status'], native['threshold']), ('native', 0))
        self.assertLess(native['reserveTokens'], applied['reserveTokens'])
        self.assertEqual((other['type'], other['id'], other['success']), ('response', 'b', False))
        self.assertEqual(proc.returncode, 0)


class FrontendReadsRouteTiers(unittest.TestCase):
    def test_dormant_saved_tier_paints_native_auto_as_effective(self):
        src = (ROOT / 'frontend' / 'app-chat-opts.js').read_text(encoding='utf-8')
        import re
        fn = re.search(r"function hsPickAutocompact\(.*?\n\}", src, re.S).group(0)
        label = re.search(r"function hsOptLabel\(.*?\n\}", src, re.S).group(0)
        from frontend_i18n_js import NODE_PRELUDE
        script = NODE_PRELUDE + label + "\n" + fn + r"""
const assert = require('node:assert/strict');
let items = null;
function hsMenuAnchor() {}
function hsAutocompacts() { return [{id:'',label:'自动'},{id:'200000',label:'20 万'}]; }
function openCtxMenu(_x,_y,rows) { items=rows; }
function hsSetOpt() {}
const v={opts:{autocompact:'600000'}};
const anchor={getBoundingClientRect:()=>({left:0,top:0})};
hsPickAutocompact(v,anchor);
assert.equal(items.find(x=>x.checked).label,'自动');
assert.equal(v.opts.autocompact,'600000');
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_route_tiers_win_and_short_routes_hide_menu(self):
        src = (ROOT / 'frontend' / 'app-chat-opts.js').read_text(encoding='utf-8')
        import re
        fns = [re.search(r"function " + name + r"\(.*?\n\}", src, re.S).group(0)
               for name in ('hsModels', 'hsModel', 'hsModelRoute', 'hsAutocompacts')]
        script = '\n'.join(fns) + r"""
const assert = require('node:assert/strict');
const tiers = [{id:''},{id:'200000'}];
function hsUI() { return {autocompacts: null}; }
const catalog = {ui:{}, models:[
  {id:'long', autocompacts: tiers},
  {id:'sol:dev', family:'sol', defaultVariant:'standard', variants:[
    {id:'sol:dev', key:'standard'}, {id:'sol:max', key:'max', autocompacts: tiers}]},
  {id:'short'}]};
const view = (model) => ({catalog, opts:{model}, realModel:''});
assert.equal(hsAutocompacts(view('long')).length, 2);
assert.equal(hsAutocompacts(view('sol:max')).length, 2);
assert.equal(hsAutocompacts(view('sol:dev')).length, 0);
assert.equal(hsAutocompacts(view('short')).length, 0);
const codex = {catalog:{ui:{autocompacts: tiers}, models:[{id:'gpt'}]}, opts:{model:'gpt'}, realModel:''};
assert.equal(hsAutocompacts(codex).length, 2);
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
