"""Shepherd Mode is a separate engine-menu mode, not a fifth execution engine."""
from contextlib import nullcontext
from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
import pytest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))
UI_SERVER = (ROOT / 'desktron/lib/ui_server.py').read_text(encoding='utf-8')

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


class BackendContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        argv = sys.argv
        sys.argv = ['ui_server.py', str(ROOT/'frontend'), '7999', '7998', str(ROOT)]
        try:
            from system import api as system_api
            import ui_contract
            import self_update
            cls.system_api, cls.ui_contract, cls.self_update = system_api, ui_contract, self_update
        finally:
            sys.argv = argv

    @classmethod
    def handler(cls, body=None, contract=False, path='/session'):
        raw = json.dumps(body or {}).encode()
        if contract:
            h = object.__new__(cls.ui_contract.UiContractMixin)
            h.path = path
        else:
            h = mock.Mock()
        h.headers = {'Content-Length': str(len(raw))}; h.rfile = BytesIO(raw)
        h._send_json = lambda obj, code=200: (obj, code)
        return h

    def test_engine_endpoint_reads_and_updates_separate_boolean(self):
        state = {'engine':'claude','shepherd_mode':False,'engine_opts':{}}
        h = self.handler({'shepherd_mode':True})
        with mock.patch.object(self.system_api, '_wb_state_read', side_effect=lambda: dict(state)), \
             mock.patch.object(self.system_api, '_wb_state_patch', side_effect=lambda patch: state.update(patch)), \
             mock.patch.object(self.system_api, '_engine_pref', return_value='claude'), \
             mock.patch.object(self.system_api.engine_specs, 'ENGINES', {}), \
             mock.patch.object(self.system_api.engine_specs, 'manifest', return_value={}):
            got = self.system_api.handle(h, '/workbench/engine', '', 'PUT')
        self.assertEqual(got, ({'ok':True,'engine':'claude','shepherd_mode':True,'shepherd_trial':False}, 200))
        self.assertTrue(state['shepherd_mode'])

    def test_put_dispatches_session_contract_routes_through_lifecycle_gate(self):
        start = UI_SERVER.index('    def do_PUT(self):')
        end = UI_SERVER.index('    def do_DELETE(self):', start)
        route = UI_SERVER[start:end]
        mutation = UI_SERVER[UI_SERVER.index('    def _local_mutation'):
                             UI_SERVER.index('    def do_POST(self):')]
        self.assertIn('return self._local_mutation("PUT")', route)
        self.assertIn('if self._is_contract():', mutation)
        self.assertIn('return self._adapter_route(method)', mutation)

    def test_shepherd_conversation_engine_endpoint_is_separate(self):
        h = self.handler({'engine':'pi'}, contract=True, path='/session/session/engine')
        adapter = mock.Mock(); adapter.name = 'shepherd'
        adapter.switch_engine.return_value = {'ok':True,'session_id':'s','engine':'pi'}
        with mock.patch.object(self.ui_contract.UiContractMixin, '_adapter_owning', return_value=adapter):
            got = self.ui_contract.UiContractMixin._adapter_route(h, 'PUT')
        self.assertEqual(got, ({'ok':True,'session_id':'s','engine':'pi'}, 200))
        adapter.switch_engine.assert_called_once_with('session', 'pi')

    def test_fork_route_returns_shepherd_session_and_rejects_busy_source(self):
        from shepherd.store import Conflict
        adapter = mock.Mock()
        adapter.fork_session.return_value = {'id': 'child', 'engine': 'claude', 'shepherd_mode': True}
        with mock.patch.object(self.ui_contract.UiContractMixin, '_adapter_owning', return_value=adapter):
            result, code = self.ui_contract.UiContractMixin._adapter_route(
                self.handler({'msg_id': 'reply'}, contract=True, path='/session/source/fork'), 'POST')
            self.assertEqual(200, code)
            self.assertTrue(result['shepherd_mode'])
            adapter.fork_session.assert_called_once_with('source', 'reply')
            adapter.fork_session.side_effect = Conflict('当前回合尚未结束')
            result, code = self.ui_contract.UiContractMixin._adapter_route(
                self.handler({'msg_id': 'reply'}, contract=True, path='/session/source/fork'), 'POST')
            self.assertEqual(409, code)
            self.assertEqual('fork_unavailable', result['code'])

    def test_fork_route_does_not_start_during_update_or_with_invalid_anchor(self):
        adapter = mock.Mock()
        with mock.patch.object(self.ui_contract.UiContractMixin, '_adapter_owning', return_value=adapter):
            result, code = self.ui_contract.UiContractMixin._adapter_route(
                self.handler({'msg_id': []}, contract=True, path='/session/source/fork'), 'POST')
            self.assertEqual(400, code)
            with mock.patch.object(self.self_update, 'tracked_work', return_value=nullcontext(False)):
                result, code = self.ui_contract.UiContractMixin._adapter_route(
                    self.handler({'msg_id': 'reply'}, contract=True, path='/session/source/fork'), 'POST')
            self.assertEqual(409, code)
            adapter.fork_session.assert_not_called()

    def test_shepherd_search_merges_formal_and_legacy_trial_stores(self):
        h = self.handler(contract=True,
                         path='/session/search?q=needle&engine=shepherd&sort=time')
        formal, trial = mock.Mock(), mock.Mock()
        formal.search_sessions.return_value = {'results':[{'session_id':'formal','count':1,
            'hits':[{'at':10}]}], 'indexing':True, 'progress':{'done':1,'total':2}}
        trial.search_sessions.return_value = {'results':[{'session_id':'trial','count':1,
            'hits':[{'at':20}]}], 'indexing':False, 'progress':{'done':0,'total':0}}
        with mock.patch.object(self.ui_contract, '_shepherd', return_value=formal), \
             mock.patch.object(self.ui_contract, '_shepherd_trial', return_value=trial):
            got, code = self.ui_contract.UiContractMixin._adapter_route(h, 'GET')
        self.assertEqual(code, 200)
        self.assertEqual(['trial','formal'], [row['session_id'] for row in got['results']])
        self.assertTrue(got['indexing']);self.assertEqual({'done':1,'total':2},got['progress'])
        formal.search_sessions.assert_called_once_with('needle', sort='time')
        trial.search_sessions.assert_called_once_with('needle', sort='time')

    def test_shepherd_search_anchor_accepts_selected_execution_engine(self):
        h = self.handler(contract=True,
                         path='/session/session/around?engine=pi&turn_from_end=2')
        adapter = mock.Mock(); adapter.name = 'shepherd'
        adapter.history.return_value = []
        with mock.patch.object(self.ui_contract.UiContractMixin, '_adapter_owning', return_value=adapter), \
             mock.patch.object(self.ui_contract, 'history_around', return_value={
                 'messages':[], 'newer':2}) as around:
            got = self.ui_contract.UiContractMixin._adapter_route(h, 'GET')
        self.assertEqual(got, ({'messages':[], 'newer':2, 'target':''}, 200))
        around.assert_called_once_with(adapter, 'session', 2, 6, 6, '')

    def test_shepherd_compact_route_returns_async_acceptance(self):
        h = self.handler({'operation_id':'compact_mobile'}, contract=True,
                         path='/session/session/compact')
        adapter = mock.Mock(); adapter.name = 'shepherd'
        adapter.compact_session_async.return_value = {
            'ok':True,'accepted':True,'operation_id':'compact_mobile','result':'running'}
        with mock.patch.object(self.ui_contract.UiContractMixin, '_adapter_owning',
                               return_value=adapter):
            got = self.ui_contract.UiContractMixin._adapter_route(h, 'POST')
        self.assertEqual(got[1], 202)
        adapter.compact_session_async.assert_called_once_with('session','compact_mobile')
        adapter.compact_session.assert_not_called()

    def test_shepherd_compact_status_route_is_operation_scoped(self):
        h = self.handler(contract=True, path='/session/session/compact/compact_mobile')
        adapter = mock.Mock(); adapter.name = 'shepherd'
        adapter.compact_status.return_value = {
            'ok':True,'running':False,'operation_id':'compact_mobile','result':'success'}
        with mock.patch.object(self.ui_contract.UiContractMixin, '_adapter_owning',
                               return_value=adapter):
            got = self.ui_contract.UiContractMixin._adapter_route(h, 'GET')
        self.assertEqual(got[1], 200)
        adapter.compact_status.assert_called_once_with('session','compact_mobile')

    def test_shepherd_reconciliation_requires_current_attempt(self):
        h = self.handler({'action':'omit_and_continue','attempt_id':'attempt_7'},
                         contract=True, path='/session/session/reconcile')
        adapter = mock.Mock(); adapter.name = 'shepherd'
        adapter.reconcile_end.return_value = {'ok':True,'session_id':'session'}
        with mock.patch.object(self.ui_contract.UiContractMixin, '_adapter_owning', return_value=adapter):
            got = self.ui_contract.UiContractMixin._adapter_route(h, 'POST')
        self.assertEqual(got, ({'ok':True,'session_id':'session'}, 200))
        adapter.reconcile_end.assert_called_once_with('session', 'attempt_7')

    def test_shepherd_reconciliation_rejects_stale_or_missing_action(self):
        h = self.handler({'action':'omit_and_continue'},
                         contract=True, path='/session/session/reconcile')
        adapter = mock.Mock(); adapter.name = 'shepherd'
        with mock.patch.object(self.ui_contract.UiContractMixin, '_adapter_owning', return_value=adapter):
            got = self.ui_contract.UiContractMixin._adapter_route(h, 'POST')
        self.assertEqual(got[1], 409)
        adapter.reconcile_end.assert_not_called()

    def test_shepherd_settings_are_scoped_to_explicit_selected_engine(self):
        h = self.handler({'opts':{'model':'opus','effort':'max'},'engine':'claude'},
                         contract=True, path='/session/session/settings')
        adapter = mock.Mock(); adapter.name = 'shepherd'
        adapter.execution_name.return_value = 'pi'
        adapter.update_session_options.return_value = {'model':'opus','effort':'max'}
        with mock.patch.object(self.ui_contract.UiContractMixin, '_adapter_owning', return_value=adapter), \
             mock.patch.object(self.ui_contract, '_wb_state_read', return_value={
                 'engine_opts':{'claude':{'mode':'acceptEdits'},'pi':{'mode':'default'}}}):
            got = self.ui_contract.UiContractMixin._adapter_route(h, 'POST')
        self.assertEqual(got[1], 200)
        adapter.update_session_options.assert_called_once_with(
            'session', {'model':'opus','effort':'max'}, {'mode':'acceptEdits'}, engine='claude')

    def test_config_exposes_resolved_command_manifest_for_shepherd_session(self):
        class Adapter:
            name = 'shepherd'
            trial = True
            recall_supported = True
            rewind_supported = False
            fork_supported = False
            compact_supported = True
            search_supported = False
            streams_text = True
            asks_permission = True

            def execution_name(self, _sid): return 'pi'
            def session_environment(self, _sid): return None
            def compact_available(self, _sid): return True
            def slash_commands_for_session(self, _sid):
                return [{'n':'skill:private','d':'must stay native'}]
            def config_label(self, _sid): return 'Pi Coding'
            def session_options(self, _sid, legacy): return dict(legacy)
            def bg_tasks(self, _sid): return {'running':0,'started':[]}
            def options_for_session(self, _sid): return {'models':[]}
            def execution_status(self, _sid): return {'producer_engine':'pi'}
            def trial_policy(self): return {'execute':True}

        h = self.handler(contract=True, path='/config?session=adp_trial_fixture')
        adapter = Adapter()
        with mock.patch.object(self.ui_contract, '_adapter', return_value=adapter), \
             mock.patch.object(self.ui_contract, '_engine_pref', return_value='pi'), \
             mock.patch.object(self.ui_contract.UiContractMixin, '_adapter_owning', return_value=adapter), \
             mock.patch.object(self.ui_contract, '_wb_state_read', return_value={'engine_opts':{}}), \
             mock.patch.object(self.ui_contract, '_engine_update', return_value={}):
            got, code = self.ui_contract.UiContractMixin._adapter_route(h, 'GET')
        self.assertEqual(code, 200)
        manifest = got['command_manifest']
        self.assertEqual((manifest['version'], manifest['engine'], manifest['shepherd']), (1,'pi',True))
        self.assertIn('/compact', [row['name'] for row in manifest['commands']])
        self.assertNotIn('/skill:private', [row['name'] for row in manifest['commands']])
        self.assertEqual(got['slash'], [{'n':'skill:private','d':'must stay native'}])

    def test_shepherd_default_engine_is_persisted_without_changing_native_desk(self):
        state = {'engine':'claude', 'shepherd_mode':True, 'engine_opts':{}}
        with mock.patch.object(self.system_api, '_wb_state_read', side_effect=lambda: dict(state)), \
             mock.patch.object(self.system_api, '_wb_state_patch', side_effect=lambda patch: state.update(patch)), \
             mock.patch.object(self.system_api, '_engine_pref', return_value='claude'), \
             mock.patch.object(self.system_api.engine_specs, 'ENGINES', {}), \
             mock.patch.object(self.system_api.engine_specs, 'manifest', return_value={}):
            result = self.system_api.handle(self.handler({'shepherd_engine':'pi'}),
                                            '/workbench/engine', '', 'POST')
            self.assertEqual(result[1], 200)
            self.assertEqual(state['engine'], 'claude')
            self.assertEqual(state['shepherd_engine'], 'pi')
            result = self.system_api.handle(self.handler(), '/workbench/engine', '', 'GET')
            self.assertEqual(result[0]['shepherd_engine'], 'pi')
            for invalid in ('shell', '', None, []):
                result = self.system_api.handle(self.handler({'shepherd_engine':invalid}),
                                                '/workbench/engine', '', 'POST')
                self.assertEqual(result[1], 400)
            self.assertEqual(state['shepherd_engine'], 'pi')

    def test_invalid_mode_type_is_rejected(self):
        h = self.handler({'shepherd_mode':'yes'})
        with mock.patch.object(self.system_api, '_wb_state_read', return_value={}):
            self.assertEqual(self.system_api.handle(h, '/workbench/engine', '', 'PUT')[1], 400)

    def test_active_shepherd_conversation_can_exit_desk_mode(self):
        source = (ROOT / 'frontend/app-events.js').read_text(encoding='utf-8')
        self.assertIn('shepherd_mode:!selected', source)
        self.assertNotIn('Shepherd Conversation 已启用；模式不能在会话中途关闭', source)

    def test_session_creation_persists_mode_without_changing_engine(self):
        h = self.handler({'engine':'codex','domain':'workflow','shepherd_mode':True}, contract=True)
        adapter = mock.Mock(); adapter.create_session.return_value = {'id':'s'}
        with mock.patch.object(self.ui_contract, '_shepherd', return_value=adapter) as select, \
             mock.patch.object(self.self_update, 'admit_work', return_value=nullcontext(True)):
            got = self.ui_contract.UiContractMixin._adapter_route(h, 'POST')
        self.assertEqual(got, ({'id':'s'}, 200)); select.assert_called_once_with()
        adapter.create_session.assert_called_once_with('workflow', task=False,
            opts={'shepherd_mode':True,'active_engine':'codex'})


@unittest.skipUnless(sync_playwright, 'requires Playwright WebKit')
@pytest.mark.desktop
class MenuWebKit(unittest.TestCase):
    def setUp(self):
        self.pw = sync_playwright().start(); self.browser = self.pw.webkit.launch()
        self.page = self.browser.new_page(viewport={'width': 390, 'height': 844})
        self.calls = []
        self.page.set_content('<style>#engine-pick{position:fixed;left:20px;bottom:20px}</style>'
                              '<button id="engine-pick"></button><div id="engine-menu" hidden></div>')
        self.page.add_style_tag(content=(ROOT / 'frontend/style.css').read_text(encoding='utf-8'))
        source = (ROOT / 'frontend/app-events.js').read_text(encoding='utf-8')
        start = source.index('async function applyEnginePick')
        end = source.index('\nasync function initEnginePick', start) if '\nasync function initEnginePick' in source[start+1:] else -1
        # applyEnginePick is declared after initEnginePick; extract by brace counting.
        i = source.index('async function applyEnginePick')
        brace = source.index('{', i); depth = 0; finish = None
        for pos in range(brace, len(source)):
            if source[pos] == '{': depth += 1
            elif source[pos] == '}':
                depth -= 1
                if depth == 0: finish = pos + 1; break
        function = source[i:finish]
        function = function.replace('} catch { setStatus(false); }',
            '} catch (error) { window.__enginePickError = String(error && (error.stack || error)); setStatus(false); }')
        function = function.replace('location.reload();',
            'window.__reloaded = true; applyEnginePick({...e, shepherd_mode:shepherdMode, shepherd_trial:shepherdTrial});')
        self.page.add_script_tag(content='''
const $=s=>document.querySelector(s); const ICONS={}; const RP_CHECK='●';
const __store = new Map(); Object.defineProperty(window, 'localStorage', {value:{
  clear(){__store.clear()}, getItem(k){return __store.has(k)?__store.get(k):null},
  setItem(k,v){__store.set(k,String(v))}, removeItem(k){__store.delete(k)} }});
const BRAND_ICONS={claude:'<svg></svg>',codex:'<svg></svg>',shepherd:'<span class="brand-shepherd img-ic" role="img" aria-label="Shepherd"></span>'};
const ENGINE_DESC={claude:'Claude',codex:'Codex'},ENGINE_ICON={},ENGINE_ACCENT={claude:'gray'};
let curEngine='claude',engineManifest={},engineInfo=null,engineOpts={},shepherdMode=false,shepherdTrial=false;
let calls=[];function setStatus(){}function escapeHtml(s){return String(s)}
function _showWatermark(){}function rememberEngineView(){}function deskStartupMark(){}
let _termWatermark=null;async function api(path,opts){calls.push({path,body:JSON.parse(opts.body)});return {ok:true,engine:'claude',...JSON.parse(opts.body)}}
localStorage.clear();
''' + function)
        self.page.evaluate('applyEnginePick({engine:"claude",engines:{claude:{label:"Claude Code"},codex:{label:"Codex"}},available:{claude:true,codex:true},manifest:{},opts:{},shepherd_mode:false})')
        error = self.page.evaluate('window.__enginePickError || ""')
        self.assertFalse(error, error)

    def tearDown(self):
        self.browser.close(); self.pw.stop()

    def test_mode_is_below_separator_and_does_not_switch_engine(self):
        self.page.locator('#engine-pick').click()
        self.assertEqual(self.page.locator('#engine-menu .fm-head').all_text_contents(),
                         ['独立引擎', '联合引擎'])
        titles = self.page.locator('#engine-menu .fm-title').all_text_contents()
        self.assertEqual(titles, ['Claude Code','Codex','Shepherd'])
        self.assertEqual(self.page.locator('#engine-menu [role="separator"]').count(), 1)
        self.assertEqual(self.page.locator('#engine-menu [role="separator"] + .fm-head + .fm-shepherd').count(), 1)
        self.assertEqual(self.page.locator('#engine-menu .fm-shepherd .brand-shepherd.img-ic').count(), 1)
        self.page.locator('#engine-menu .fm-shepherd').first.click()
        self.assertEqual(self.page.evaluate('calls'), [{'path':'/workbench/engine','body':{
            'shepherd_mode':True,'shepherd_trial':False}}])
        self.assertEqual(self.page.evaluate('curEngine'), 'claude')
        self.assertTrue(self.page.evaluate('shepherdMode'))
        self.assertEqual(self.page.locator('#engine-pick .brand-shepherd.img-ic').count(), 1)
        self.assertEqual(self.page.locator('#engine-pick').text_content().strip(), 'Shepherd')
        self.page.locator('#engine-pick').click()
        self.assertEqual(self.page.locator('#engine-menu .fm-row.on').count(), 1)
        self.assertTrue(self.page.locator('#engine-menu .fm-shepherd').first.evaluate('(el) => el.classList.contains("on")'))
        self.assertEqual(self.page.locator('#engine-menu .fm-current').count(), 0)
        self.assertFalse(self.page.locator('#engine-menu .fm-row').filter(has_text='Claude Code').evaluate('(el) => el.classList.contains("on")'))

    def test_shepherd普通引擎与模式互斥(self):
        self.page.evaluate('''
          shepherdMode = true;
          hsSituationCurrentView = () => ({ephemeral:false, shepherdMode:true,
            sid:'shepherd-session', engine:'codex'});
        ''')
        self.page.locator('#engine-pick').click()
        self.page.locator('#engine-menu .fm-row').filter(has_text='Codex').click()
        self.assertEqual(self.page.evaluate('calls'), [
            {'path':'/workbench/engine','body':{'engine':'codex','shepherd_mode':False}}])

    def test_shepherd_conversation_can_exit_to_ordinary_desk(self):
        self.page.evaluate('''
          shepherdMode = true;
          hsSituationCurrentView = () => ({ephemeral:false, shepherdMode:true,
            sid:'shepherd-session', engine:'codex'});
        ''')
        self.page.locator('#engine-pick').click()
        self.page.locator('#engine-menu .fm-shepherd').first.click()
        self.assertEqual(self.page.evaluate('calls'), [
            {'path':'/workbench/engine','body':{'shepherd_mode':False,'shepherd_trial':False}}])
        self.assertFalse(self.page.evaluate('shepherdMode'))

    def test_engine_and_shepherd_icons_share_one_visual_grid(self):
        self.page.locator('#engine-pick').click()
        rows = self.page.locator('#engine-menu .fm-row')
        for index in range(3):
            icon = rows.nth(index).locator('.fm-icon').bounding_box()
            title = rows.nth(index).locator('.fm-title').bounding_box()
            self.assertAlmostEqual(icon['height'], 20, delta=.1)
            self.assertAlmostEqual(icon['y'] + icon['height'] / 2,
                                   title['y'] + title['height'] / 2, delta=.6)
        sheep = rows.nth(2).locator('.brand-shepherd.img-ic').bounding_box()
        self.assertLessEqual(sheep['width'], 16)
        self.assertLessEqual(sheep['height'], 16)

    def test_trial_label_and_separate_entry_are_removed(self):
        self.page.locator('#engine-pick').click()
        self.assertEqual(self.page.locator('#engine-menu .fm-shepherd').count(), 1)
        self.assertNotIn('试用', self.page.locator('#engine-menu').text_content())

    def test_narrow_menu_stays_inside_viewport(self):
        self.page.locator('#engine-pick').click()
        box = self.page.locator('#engine-menu').bounding_box()
        self.assertLessEqual(box['x'] + box['width'], 390)


if __name__ == '__main__': unittest.main()
