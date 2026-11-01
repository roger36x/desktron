"""A UI selection cannot cancel a durable post-fence catch-up."""
from types import SimpleNamespace
import unittest
from unittest import mock

import test_shepherd_product as fixtures
from shepherd import Conflict


def scenario(case):
    ad=case.adapter
    sid=ad.create_session('workflow',opts={'active_engine':'codex'})['id']
    state={'pending_switch':{'target_binding_id':'pi'},'bindings':{'pi':{'engine':'pi'}}}
    ad.service.coordinator=SimpleNamespace(state=lambda _sid:state)
    active=ad.service.active
    def current(sid):
        if state['pending_switch']:
            return {'state':state,'binding':None,'lease':None}
        return active(sid)
    ad.service.active=current
    switch=ad.service.switch
    calls=[]
    def resume(sid,engine,cwd,cancelled=None,selection_epoch=None):
        if state['pending_switch']:
            state['pending_switch']=None
        calls.append(engine)
        return switch(sid,engine,cwd,cancelled=cancelled)
    ad.service.switch=resume
    return ad,sid,calls


class PendingSelection(unittest.TestCase):
    setUp=fixtures.ShepherdFacade.setUp

    def test_pi_child_result_cannot_apply_a_pending_engine_selection(self):
        ad=self.adapter
        sid=ad.create_session('workflow', opts={'active_engine':'pi'})['id']
        with mock.patch.object(ad, '_schedule_engine_prepare'):
            ad.switch_engine(sid, 'claude')
        ad._request_local.rid='pi-child-pi_fixture'
        with mock.patch.object(ad.requests, 'cancelled', return_value=False), \
             mock.patch.object(ad, '_apply_requested_engine') as switch:
            with self.assertRaisesRegex(Conflict, 'Pi 子代理结果'):
                ad._before_turn_start(sid)
        switch.assert_not_called()
        self.assertEqual(ad.session_engine(sid),'pi')
        self.assertEqual(ad.selected_engine(sid),'claude')

    def test_pi_child_result_runs_when_original_engine_is_still_pi(self):
        ad=self.adapter
        sid=ad.create_session('workflow', opts={'active_engine':'pi'})['id']
        ad._request_local.rid='pi-child-pi_fixture'
        with mock.patch.object(ad.requests, 'cancelled', return_value=False):
            ad._before_turn_start(sid)
        self.assertEqual(ad.session_engine(sid),'pi')

    def test_prepare_failure_visible_after_refresh_and_reselect_clears_it(self):
        ad = self.adapter
        sid = ad.create_session('workflow', opts={'active_engine':'pi'})['id']
        with mock.patch.object(ad, '_schedule_engine_prepare'):
            ad.switch_engine(sid, 'claude')
        ad._selection_epochs[sid] = 3
        with mock.patch.object(ad.service, 'switch', side_effect=Conflict('历史格式不兼容')):
            ad._prepare_selected_engine(sid, 3)
        self.assertEqual(ad.session_environment(sid)['stage'], 'shepherd-switch')
        self.assertEqual(ad.session_environment(sid)['details'], ['历史格式不兼容'])
        self.assertEqual(ad.session_engine(sid), 'pi')
        ad.switch_engine(sid, 'pi')
        self.assertIsNone(ad.session_environment(sid))
        self.assertNotIn('next_engine', ad.index[sid])

    def test_late_failed_prepare_cannot_replace_newer_selection_notice(self):
        ad = self.adapter
        sid = ad.create_session('workflow', opts={'active_engine':'pi'})['id']
        with mock.patch.object(ad, '_schedule_engine_prepare'):
            ad.switch_engine(sid, 'claude')
        ad._selection_epochs[sid] = 3
        def fail(_sid):
            ad.switch_engine(sid, 'pi')
            raise Conflict('obsolete failure')
        with mock.patch.object(ad, '_apply_requested_engine', side_effect=fail):
            ad._prepare_selected_engine(sid, 3)
        self.assertIsNone(ad.session_environment(sid))

    def test_selecting_an_engine_does_not_clear_unrelated_warning(self):
        ad = self.adapter
        sid = ad.create_session('workflow', opts={'active_engine':'pi'})['id']
        ad._env_publish(sid, {'state':'warning','stage':'recovery','label':'needs verification'})
        ad.switch_engine(sid, 'pi')
        self.assertEqual(ad.session_environment(sid)['stage'], 'recovery')

    def test_switch_issue_uses_shared_projection_and_clears_while_hidden(self):
        ad = self.adapter
        sid = ad.create_session('workflow', opts={'active_engine':'pi'})['id']
        ad._env_publish(sid, {'state':'error', 'stage':'auth', 'label':'login required',
                             'action':{'kind':'terminal','label':'登录'}})
        ad._switch_notice(sid, error='target unavailable')
        self.assertEqual(ad.session_environment(sid)['stage'], 'auth')
        issues=ad.index[sid]['environment_issues']
        self.assertIn('shepherd-switch', issues)
        revision=ad.index[sid]['environment_revision']
        ad._switch_notice(sid)
        self.assertNotIn('shepherd-switch', ad.index[sid]['environment_issues'])
        self.assertEqual(ad.session_environment(sid)['stage'], 'auth')
        self.assertGreater(ad.index[sid]['environment_revision'], revision)

    def test_recovery_compatibility_stays_in_audit_and_clears_legacy_warning(self):
        ad = self.adapter
        sid = ad.create_session('workflow', opts={'active_engine':'pi'})['id']
        report = {'conversation_id':sid, 'outcome':'settled_incomplete',
            'compatibility':[{'engine':'pi','action':'ignored_internal_entry',
                              'kind':'compaction','count':2}]}
        with mock.patch.object(ad.bus, 'publish', wraps=ad.bus.publish) as publish:
            ad._record_recovery(report)
        self.assertIsNone(ad.session_environment(sid))
        self.assertFalse([call for call in publish.call_args_list if call.args
                          and call.args[0].get('type') == 'session.environment'])

        ad._env_publish(sid, {'state':'warning','stage':'shepherd_compatibility',
            'label':'legacy compatibility warning','details':['metadata only']})
        with mock.patch.object(ad.bus, 'publish', wraps=ad.bus.publish) as publish:
            ad._record_recovery(report)
        self.assertIsNone(ad.session_environment(sid))
        published = [call.args[0] for call in publish.call_args_list
                     if call.args and call.args[0].get('type') == 'session.environment']
        cleared = published[-1]['properties']
        self.assertEqual({key:cleared.get(key) for key in ('sessionID','state','stage')}, {
            'sessionID':sid, 'state':'ready', 'stage':'shepherd_compatibility'})
        self.assertFalse([event for event in published
                          if event['properties'].get('state') == 'warning'])

        ad._env_publish(sid, {'state':'warning','stage':'shepherd_compatibility',
            'label':'legacy compatibility warning'})
        ad._env_publish(sid, {'state':'error','stage':'auth','label':'login required'})
        ad._record_recovery(report)
        self.assertEqual(ad.session_environment(sid)['stage'], 'auth')
        self.assertNotIn('shepherd_compatibility', ad.index[sid].get('environment_issues', {}))

    def test_ready_recovery_report_clears_stale_recovery_issue(self):
        ad = self.adapter
        sid = ad.create_session('workflow', opts={'active_engine':'pi'})['id']
        state = {'active_turn':{'phase':'settled','recovery_state':'source_staged'}}
        ad.service.coordinator = SimpleNamespace(state=lambda _sid: state)
        ad._env_publish(sid, {'state':'warning','stage':'recovery',
                              'label':'needs verification'})

        ad._record_recovery({'conversation_id':sid, 'state':'ready',
                             'outcome':'not_applicable'})

        self.assertIsNone(ad.session_environment(sid))
        self.assertNotIn('recovery', ad.index[sid].get('environment_issues', {}))

    def test_ready_report_cannot_clear_an_active_recovery_fence(self):
        ad = self.adapter
        sid = ad.create_session('workflow', opts={'active_engine':'pi'})['id']
        state = {'active_turn':{'phase':'settled','recovery_state':'blocked'}}
        ad.service.coordinator = SimpleNamespace(state=lambda _sid: state)
        ad._env_publish(sid, {'state':'warning','stage':'recovery',
                              'label':'needs verification'})

        ad._record_recovery({'conversation_id':sid, 'state':'ready',
                             'outcome':'not_applicable'})

        self.assertEqual(ad.session_environment(sid)['stage'], 'recovery')

    def test_explicit_reselection_retargets_pending_without_activating_stale_target(self):
        ad,sid,calls=scenario(self)
        with mock.patch.object(ad,'_schedule_engine_prepare'):
            result=ad.switch_engine(sid,'codex')
        self.assertTrue(result['preparing'])
        ad._before_turn_start(sid)
        self.assertEqual(calls,['codex'])
        self.assertEqual(ad.service.active(sid)['binding']['engine'],'codex')
        self.assertNotIn('next_engine',ad.index[sid])


    def test_selection_after_recovery_fence_retargets_instead_of_rebinding_old_engine(self):
        # Incomplete Claude turn fenced a same-engine rebind; the user then
        # picked Pi. A failing Claude rebind must not make Pi unreachable.
        ad,sid,calls=scenario(self)
        state=ad.service.coordinator.state(sid)
        state['pending_switch']={'target_binding_id':'claude2','recovery_turn_id':sid+':1',
                                 'source_binding_id':'claude1'}
        state['bindings']={'claude2':{'engine':'claude'}}
        ad.index[sid]['next_engine']='pi'
        with mock.patch.object(ad.service,'recover_incomplete',create=True,
                               side_effect=Conflict('Shepherd 公共投影 Hash 不匹配')) as rebind:
            ad._before_turn_start(sid)
        rebind.assert_not_called()
        self.assertEqual(calls,['pi'])

    def test_unchanged_selection_still_resumes_recovery_rebind(self):
        ad,sid,calls=scenario(self)
        state=ad.service.coordinator.state(sid)
        state['pending_switch']={'target_binding_id':'claude2','recovery_turn_id':sid+':1',
                                 'source_binding_id':'claude1'}
        state['bindings']={'claude2':{'engine':'claude'}}
        ad.index[sid]['next_engine']='claude'
        with mock.patch.object(ad.service,'recover_incomplete',create=True,
                               side_effect=Conflict('rebind failed')) as rebind:
            with self.assertRaisesRegex(Conflict,'rebind failed'):
                ad._before_turn_start(sid)
        rebind.assert_called_once_with(sid,'claude1')
        self.assertEqual(calls,[])

    def test_restart_missing_ui_next_engine_still_resumes_durable_switch(self):
        ad,sid,calls=scenario(self)
        ad.index[sid]['selected_engine']='codex'
        ad._before_turn_start(sid)
        self.assertEqual(calls,['pi','codex'])


    def test_pending_failure_rejects_before_public_message_write(self):
        ad,sid,_=scenario(self)
        with mock.patch.object(ad.service,'switch',side_effect=Conflict('source hash mismatch')), \
             mock.patch.object(ad,'_run') as run:
            with self.assertRaisesRegex(Conflict,'source hash mismatch'):
                ad._run_serial(sid,'must not persist',{},[])
        run.assert_not_called()
        self.assertIsNone(ad.service.active(sid)['lease'])
