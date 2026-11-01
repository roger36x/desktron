"""多标签与审批续办；仅临时 Runtime、模拟宿主与本地 JS。"""
import json
import re
import subprocess
from pathlib import Path

from test_app_control_browser_ac import Base, load_cli
from app_control.runtime import AppControlError


class ClosedLoop(Base):
    def guarded_observe(self, version='1:7'):
        op = self.rt.inspect(self.lease, 's1')
        return self.run_op(op, output={
            'schema': 'desk.browser.document/v1', 'observation_version': version,
            'url': 'https://test/form', 'nodes': [],
            'approval_guard': {'token': 'host-only-proof', 'version': version}}, version=version)

    def click(self, key='once'):
        return self.rt.act(self.lease, 's1', 'click', {'selector': '#go'}, '1:7', key)

    def test_paused_yield_is_owned_idempotent_and_does_not_resume(self):
        self.rt.pause(self.lease, 'tab_switched')
        for sid in ('other', ''):
            with self.assertRaises(AppControlError) as ctx:
                self.rt.yield_lease(self.lease, sid)
            self.assertEqual(ctx.exception.code, 403)
        ended = self.rt.yield_lease(self.lease, 's1')
        self.assertEqual((ended['status'], ended['revoked_reason']), ('revoked', 'agent_yield'))
        self.assertEqual(self.rt.yield_lease(self.lease, 's1'), ended)
        self.assertIsNone(self.rt.next_operation('browser'))

    def test_human_pause_cannot_be_activated_by_agent(self):
        self.rt.pause(self.lease, 'tab_switched')
        with self.assertRaisesRegex(AppControlError, '切回原标签') as ctx:
            self.rt.act(self.lease, 's1', 'activate', {})
        self.assertNotIn('片刻会自动', str(ctx.exception))
        self.assertIn('yield', str(ctx.exception))
        with self.assertRaises(AppControlError) as other:
            self.rt.inspect(self.lease, 'other')
        self.assertEqual(other.exception.code, 403)

    def test_tab_switch_upgrades_idle_pause_without_downgrading(self):
        self.rt.pause(self.lease, 'user_interaction')
        self.rt.pause(self.lease, 'tab_switched')
        self.assertEqual(self.rt.get_lease(self.lease)['revoked_reason'], 'tab_switched')
        self.rt.pause(self.lease, 'user_interaction')
        self.assertEqual(self.rt.get_lease(self.lease)['revoked_reason'], 'tab_switched')

    def test_delayed_approval_revalidates_same_operation_once(self):
        self.guarded_observe()
        op = self.click()
        self.now[0] += 115
        self.guarded_observe('1:1689')
        self.rt.approve(op['id'], True, 'ui_user')
        dispatched = self.rt.next_operation('browser')
        self.assertEqual(dispatched['id'], op['id'])
        self.assertEqual(dispatched['expected_version'], '1:7')
        self.assertEqual(dispatched['input']['_approval_guard']['version'], '1:7')
        self.assertIsNone(self.rt.next_operation('browser'))
        completed = self.rt.complete(op['id'], True, {'approval_revalidated': True,
            'action_executed': True}, observation_version='1:1690')
        self.assertEqual(completed['status'], 'succeeded')
        # 同一幂等键不得因旧版本重放。
        self.assertEqual(self.click()['id'], op['id'])
        self.assertNotIn('_approval_guard', str(self.rt.trace(op['id'], 's1')))

    def test_guard_cannot_be_supplied_by_agent(self):
        with self.assertRaises(AppControlError):
            self.rt.act(self.lease, 's1', 'click', {
                'selector': '#go', '_approval_guard': {'token': 'forged'}}, idempotency_key='forged')

    def test_old_host_unconfirmed_result_is_not_replayed(self):
        self.guarded_observe()
        op = self.click()
        self.rt.approve(op['id'], True)
        self.rt.next_operation('browser')
        done = self.rt.complete(op['id'], True, {'clicked': True})
        self.assertEqual(done['error'], 'approval_revalidation_unconfirmed')
        self.assertIsNone(self.rt.next_operation('browser'))

    def test_target_change_is_terminal_and_requires_new_approval(self):
        self.guarded_observe()
        op = self.click()
        self.rt.approve(op['id'], True)
        self.rt.next_operation('browser')
        done = self.rt.complete(op['id'], False, {'action_executed': False}, 'approval_target_changed')
        self.assertFalse(done['output']['action_executed'])
        self.assertEqual(done['output']['next_step'], 'inspect_then_request_new_approval')
        self.assertIsNone(self.rt.next_operation('browser'))
        with self.assertRaises(AppControlError):
            self.rt.approve(op['id'], True)

    def test_reject_pause_expiry_never_dispatch_guarded_action(self):
        for mode in ('reject', 'pause', 'expire'):
            self.guarded_observe()
            op = self.click(mode)
            if mode == 'reject':
                self.rt.approve(op['id'], False)
            elif mode == 'pause':
                self.rt.pause(self.lease)
                self.rt.resume(self.lease)
            else:
                self.now[0] += 4000
            self.assertIsNone(self.rt.next_operation('browser'))
            with self.assertRaises(AppControlError):
                self.rt.approve(op['id'], True)

    def test_guarded_postcondition_never_revalidates_or_replays_write(self):
        self.guarded_observe()
        op = self.rt.act(self.lease, 's1', 'click', {'selector': '#go',
            'wait_for': {'selector': '#done'}}, '1:7', 'wait-once')
        self.rt.approve(op['id'], True)
        self.rt.next_operation('browser')
        self.rt.complete(op['id'], True, {'approval_revalidated': True, 'action_executed': True})
        probe = self.rt.next_operation('browser')
        self.assertEqual(probe['name'], 'wait')
        self.assertNotIn('_approval_guard', probe['input'])
        done = self.rt.complete(op['id'], True, {}, dispatch_id=probe['dispatch_id'])
        self.assertEqual(done['status'], 'succeeded')


def test_agent_and_human_tab_paths_execute_separately():
    src = (Path(__file__).resolve().parents[2] / 'frontend/app-browser.js').read_text(encoding='utf-8')
    names = ('browserActivateTab', 'browserAgentStepAside', 'browserAgentStepBack',
             'browserCloseTab', 'openBrowserApp')
    functions = '\n'.join(re.search(r'\nfunction ' + name + r'\(.*?\n}\n', src, re.S)[0] for name in names)
    commands = src[src.index('window.__browserAgentOpen ='):src.index('window.__browserControlEvent =')]
    js = '''
      const assert = require('assert'); const window = {};
      let browserTabs = [], browserActiveId = '', overlayOpen = false, pActive = null;
      const browserControlState = new Map(), events = [], NATIVE_BROWSER = true;
      const page = {};
      function browserActiveTab(){return browserTabs.find(x=>x.id===browserActiveId)}
      function browserEnsurePage(show){if(show)showOverlay();return page} function browserPage(){return page}
      function browserRenderTabs(){} function browserRenderNav(){} function persistBrowserTabs(){}
      let shown=0, synced=0, _browserShownNative='';
      function showOverlay(){shown++} function browserSyncNative(){synced++}
      function closePage(){events.push({action:'closePage'})} function browserFocusUrl(){}
      function nb(event){events.push(event)}
    ''' + functions + commands + '''
      // 没有用户标签时也只在后台建页；用户主动打开应用后才选中首张。
      window.__browserAgentOpen({id:'A',url:'https://test/a'});
      window.__browserAgentOpen({id:'B',url:'https://test/b'});
      assert(window.__browserAgentCommand({id:'A',action:'activate'}));
      assert.equal(browserActiveId,''); assert.equal(shown,0); assert.equal(synced,0);
      openBrowserApp(true);
      assert.equal(browserActiveId,'A');
      browserTabs.push({id:'U'}); browserActivateTab('U');
      const initialShown=shown, initialSynced=synced;
      window.__browserAgentOpen({id:'C',url:'https://test/c'});
      assert(window.__browserAgentCommand({id:'B',action:'activate'}));
      assert(window.__browserAgentCommand({id:'C',action:'close'}));
      assert.equal(browserActiveId,'U'); assert.equal(shown,initialShown); assert.equal(synced,initialSynced);
      assert.equal(events.filter(x=>x.action==='agentPause'||x.action==='agentResume').length,0);
      browserActivateTab('A'); browserActivateTab('B');
      assert(!events.some(x=>x.id==='A' && x.action==='agentPause'));
      // 真人真正操作后仍暂停；离开时升级原因，Agent 不能自行解锁。
      browserControlState.set('A',{status:'paused'});
      assert.equal(window.__browserAgentCommand({id:'A',action:'activate'}),false);
      assert.equal(window.__browserAgentOpen({id:'A',url:'https://test/a'}),false);
      assert.equal(browserActiveId,'B');
      browserActivateTab('A');
      assert(events.some(x=>x.id==='A' && x.action==='agentResume'));
      browserActivateTab('B');
      assert(events.some(x=>x.id==='A' && x.action==='agentPause'));
      // 关闭恰好可见的 Agent 页只切到邻页，不打开面板或恢复被暂停的邻页。
      const lastShown=shown, resumes=events.filter(x=>x.action==='agentResume').length;
      assert(window.__browserAgentCommand({id:'B',action:'close'}));
      assert.equal(browserActiveId,'U'); assert.equal(shown,lastShown);
      assert.equal(events.filter(x=>x.action==='agentResume').length,resumes);
    '''
    subprocess.run(['node', '-e', js], check=True, capture_output=True, text=True, encoding='utf-8')


def test_cli_keeps_same_operation_across_approval_wait_and_stops_on_reject():
    cli = load_cli()
    from unittest.mock import patch
    for end in ('succeeded', 'rejected', 'cancelled', 'failed'):
        with patch.object(cli, 'wait', side_effect=[
                {'id': 'same-op', 'status': 'waiting_user'},
                {'id': 'same-op', 'status': 'running'},
                {'id': 'same-op', 'status': end}]) as waiting:
            with patch.object(cli.time, 'monotonic', side_effect=[0, 1, 56, 116]):
                done = cli.wait_terminal({'id': 'same-op', 'status': 'waiting_user'}, 900)
        assert done['status'] == end
        assert waiting.call_count == 3
        assert all(call.args[0]['id'] == 'same-op' and call.args[1] <= 55 for call in waiting.call_args_list)
