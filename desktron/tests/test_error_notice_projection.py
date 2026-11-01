"""Existing notice projection owns priority; native facts own execution state."""
import unittest
from unittest import mock

import test_native_lifecycle_projection as fixtures
from test_engine_adapters_gui import ea
from error_notice import prepare, project


class NoticeProjection(unittest.TestCase):
    def test_turn_error_replaces_non_actionable_warning_and_restores_it_on_clear(self):
        row={}
        project(row, prepare('sid', {'stage':'compatibility','state':'warning','label':'limited'}))
        visible=project(row, prepare('sid', {'stage':'turn','state':'error','label':'failed'}, 2))
        self.assertEqual(visible['stage'], 'turn')
        self.assertEqual(len(row['environment_issues']), 2)
        visible=project(row, prepare('sid', {'stage':'turn','state':'ready'}, 2))
        self.assertEqual(visible['stage'], 'compatibility')

    def test_recovery_action_stays_visible_without_erasing_turn_failure(self):
        row={}
        project(row, prepare('sid', {'stage':'recovery','state':'warning','label':'verify'}))
        visible=project(row, prepare('sid', {'stage':'turn','state':'error','label':'failed'}, 3))
        self.assertEqual(visible['stage'], 'recovery')
        self.assertIn('turn', row['environment_issues'])
        visible=project(row, prepare('sid', {'stage':'recovery','state':'ready'}))
        self.assertEqual(visible['stage'], 'turn')


class LegacyCompactionNotice(unittest.TestCase):
    @staticmethod
    def old_compaction(*, observed=10, engine='pi'):
        return prepare('sid', {'stage':'permission', 'state':'error',
            'label':'Pi 权限闸异常', 'engine':engine, 'provider':'openai',
            'observedAt':observed, 'startedAt':observed,
            'error':{'source':'engine', 'code':'PERMISSION_EXTENSION_ERROR',
                     'message':'Compaction summary failed: WebSocket error'}})

    @staticmethod
    def recovered(*, through=20, engine='pi'):
        return prepare('sid', {'stage':'compaction', 'state':'ready',
            'label':'上下文压缩已恢复', 'engine':engine, 'provider':'example-gw',
            'resolvedThrough':through})

    def test_success_clears_old_misclassified_compaction_error(self):
        row={'selected_engine':'pi', 'engine_opts':{'pi':{'model':'example-gw/model'}}}
        project(row, self.old_compaction())
        visible=project(row, self.recovered())
        self.assertNotIn('permission',row['environment_issues'])
        self.assertEqual(visible['state'],'ready')

    def test_success_preserves_real_permission_and_later_or_other_engine_errors(self):
        real=prepare('sid', {'stage':'permission', 'state':'error',
            'label':'permission failed', 'engine':'pi', 'observedAt':10,
            'error':{'source':'engine', 'code':'PERMISSION_EXTENSION_ERROR',
                     'message':'tool gate failed'}})
        unknown=self.old_compaction();unknown.pop('engine',None)
        for issue in (real, self.old_compaction(observed=30),
                      self.old_compaction(engine='claude'), unknown):
            with self.subTest(issue=issue):
                row={}
                project(row,issue)
                project(row,self.recovered())
                self.assertIn('permission',row['environment_issues'])


class NativeNoticeProjection(unittest.TestCase):
    setUp=fixtures.NativeRetryProjection.setUp
    make=fixtures.NativeRetryProjection.make

    def test_info_does_not_publish_warning_or_clear_auth_error(self):
        ad,ctx,bus=self.make(ea.PiAdapter)
        ad.index[ctx['sid']]={}
        ad._env_publish(ctx['sid'], {'stage':'auth','state':'error','label':'login'})
        before=ad.session_environment(ctx['sid'])
        bus.events.clear()
        with mock.patch.object(ad, '_trace') as trace:
            ad.handle_event({'type':'extension_ui_request','method':'notify',
                             'notifyType':'info','message':'informational text'}, ctx)
        self.assertFalse(bus.events)
        self.assertEqual(ad.session_environment(ctx['sid']), before)
        self.assertNotIn('informational text', str(trace.call_args))

    def test_warning_is_turn_scoped_and_error_is_still_settled_by_native(self):
        ad,ctx,bus=self.make(ea.PiAdapter)
        ad.handle_event({'type':'extension_ui_request','method':'notify',
                         'notifyType':'warning','message':'limited capability'}, ctx)
        self.assertEqual(bus.events[-1]['properties']['scope'], 'turn')
        self.assertNotIn('engine_error', ctx)
        ad.handle_event({'type':'extension_ui_request','method':'notify',
                         'notifyType':'unrecognized','message':'new native notice'}, ctx)
        self.assertEqual(bus.events[-1]['properties']['state'], 'warning')
        ad.handle_event({'type':'extension_ui_request','method':'notify',
                         'notifyType':'error','message':'request failed'}, ctx)
        self.assertEqual(ctx['_request_failure']['message'], 'request failed')
        self.assertNotIn('engine_error', ctx)

    def test_cancelled_compaction_is_not_a_failure_but_independent_error_survives(self):
        for extra,expected in (({'aborted':True}, 'cancelled'),
                               ({'status':'declined'}, 'noop'),
                               ({'aborted':True,'willRetry':True}, 'retrying'),
                               ({'aborted':True,'error':'storage failed'}, 'failed')):
            with self.subTest(extra=extra):
                ad,ctx,bus=self.make(ea.PiAdapter)
                ad.handle_event({'type':'compaction_start','reason':'manual'},ctx)
                ad.handle_event({'type':'compaction_end','reason':'manual', **extra},ctx)
                frames=[e['properties'] for e in bus.events if e['type']=='session.compact']
                self.assertEqual(frames[-1]['result'],expected)
                self.assertEqual(bool(frames[-1]['error']),expected=='failed')
                self.assertNotIn('engine_error',ctx)

    def test_error_event_reuses_backend_selected_notice(self):
        ad,ctx,bus=self.make(ea.PiAdapter)
        ad.index[ctx['sid']]={}
        ad._env_publish(ctx['sid'], {'stage':'auth','state':'error','label':'login',
                                    'action':{'kind':'terminal','label':'登录'}})
        ad._turn_error_environment(ctx['sid'], {'message':'request failed'})
        ad._publish_turn_error(ctx['sid'],ctx,{'message':'request failed'})
        self.assertEqual(bus.events[-1]['properties']['environment'],
                         ad.session_environment(ctx['sid']))
        self.assertEqual(bus.events[-1]['properties']['environment']['stage'],'auth')


class AuthLifecycle(unittest.TestCase):
    def auth(self, engine='claude', provider='claude', started=10):
        return prepare('s', {'stage':'auth', 'state':'error', 'label':'expired',
            'engine':engine, 'provider':provider, 'startedAt':started})

    def test_switch_filters_without_deleting_other_source(self):
        from error_notice import visible
        row={'shepherd_mode':True, 'selected_engine':'claude'}
        project(row, self.auth())
        row.update(selected_engine='pi', engine_opts={'pi':{'model':'example-gw/gpt'}})
        self.assertIsNone(visible(row))
        self.assertIn('auth:claude:claude', row['environment_issues'])
        project(row, self.auth('pi','example-gw'))
        self.assertEqual(visible(row)['provider'],'example-gw')
        row['engine_opts']['pi']['model']='another/model'
        self.assertIsNone(visible(row))
        row['selected_engine']='claude'
        self.assertEqual(visible(row)['engine'],'claude')

    def test_recovery_preserves_other_provider_and_fences_late_failure(self):
        from error_notice import visible
        row={'engine':'pi', 'opts':{'model':'a/model'}}
        project(row,self.auth('pi','a'))
        project(row,self.auth('pi','b'))
        recovered=prepare('s',{'stage':'auth','state':'ready','engine':'pi','provider':'a','resolvedThrough':20})
        project(row,recovered)
        self.assertNotIn('auth:pi:a',row['environment_issues'])
        self.assertIn('auth:pi:b',row['environment_issues'])
        self.assertIsNone(visible(row))
        project(row,self.auth('pi','a',15))
        self.assertNotIn('auth:pi:a',row['environment_issues'])
        project(row,self.auth('pi','a',30))
        self.assertEqual(visible(row)['startedAt'],30)
        project(row,recovered)
        self.assertEqual(visible(row)['startedAt'],30)

    def test_unknown_legacy_shepherd_auth_is_not_attributed_to_pi(self):
        from error_notice import visible
        old={'stage':'auth','state':'error','label':'expired','action':{'kind':'terminal'}}
        row={'shepherd_mode':True,'selected_engine':'pi','environment':old}
        self.assertIsNone(visible(row))
        project(row,prepare('s',{'state':'ready','stage':'environment','clear':True}))
        self.assertEqual(row['environment_issues']['auth'],old)
        self.assertIsNone(visible(row))

    def test_clear_does_not_resurrect_old_projection(self):
        from error_notice import visible
        row={'engine':'claude'}
        project(row,self.auth())
        project(row,prepare('s',{'stage':'auth','state':'ready','engine':'claude','provider':'claude','resolvedThrough':20}))
        self.assertIsNone(visible(row))
        self.assertNotIn('environment',row)


class AuthAdapterLifecycle(unittest.TestCase):
    setUp = fixtures.NativeRetryProjection.setUp
    make = fixtures.NativeRetryProjection.make

    def test_auth_resolves_across_sessions_but_not_provider_or_newer_failure(self):
        ad,ctx,bus=self.make(ea.PiAdapter)
        for sid,provider,started in [('one','a',10),('two','a',10),('other','b',10),('new','a',30)]:
            ad.index[sid]={'opts':{'model':provider+'/model'}}
            ad._env_publish(sid,{'stage':'auth','state':'error','label':'expired',
                'engine':'pi','provider':provider,'startedAt':started})
        ad.resolve_auth({'engine':'pi','provider':'a'},20)
        self.assertIsNone(ad.session_environment('one'))
        self.assertIsNone(ad.session_environment('two'))
        self.assertEqual(ad.session_environment('other')['provider'],'b')
        self.assertEqual(ad.session_environment('new')['startedAt'],30)
        # Persisted resolution survives reloading and rejects an in-flight old error.
        ad2=ea.PiAdapter(self.tmp.name,self.tmp.name,bus)
        ad2._env_publish('one',{'stage':'auth','state':'error','label':'late',
            'engine':'pi','provider':'a','startedAt':15})
        self.assertIsNone(ad2.session_environment('one'))

    def test_bound_worker_tags_its_producer_not_selected_next_engine(self):
        ad,ctx,bus=self.make(ea.ClaudeAdapter)
        ad.index[ctx['sid']]={'shepherd_mode':True,'selected_engine':'pi',
                             'engine_opts':{'pi':{'model':'example-gw/model'}}}
        ctx.update(producer_engine='claude',opts={'model':'opus'})
        ad._ctx_env_publish(ctx,{'stage':'auth','state':'error','label':'expired'})
        issue=ad.index[ctx['sid']]['environment_issues']['auth:claude:claude']
        self.assertEqual(issue['engine'],'claude')
        self.assertIsNone(ad.session_environment(ctx['sid']))
        self.assertTrue(bus.events[-1]['properties']['clear'])

    def test_failed_or_unknown_login_probe_does_not_resolve(self):
        import engine
        ad,ctx,bus=self.make(ea.ClaudeAdapter)
        ad.index[ctx['sid']]={}
        ad._env_publish(ctx['sid'],{'stage':'auth','state':'error','label':'expired'})
        with mock.patch.object(engine,'_adapters',{'claude':ad}), \
             mock.patch.object(engine,'_shepherd_adapter',None), \
             mock.patch.object(engine,'_shepherd_trial_adapter',None), \
             mock.patch('settings.invalidate_auth_probe_caches'), \
             mock.patch('claude_auth.subscription_status') as probe:
            for status in (False,None):
                probe.return_value=status
                self.assertFalse(engine.refresh_auth_notices('claude')['ok'])
                self.assertIsNotNone(ad.session_environment(ctx['sid']))
            probe.return_value=True
            self.assertTrue(engine.refresh_auth_notices('claude')['ok'])
            self.assertIsNone(ad.session_environment(ctx['sid']))


    def test_successful_native_turn_clears_auth_but_interrupt_does_not(self):
        for status in ('completed','interrupted'):
            with self.subTest(status=status):
                ad,ctx,bus=self.make(ea.ClaudeAdapter)
                ad.index[ctx['sid']]={'time':{}}
                ad._env_publish(ctx['sid'],{'stage':'auth','state':'error','label':'expired','startedAt':1})
                ctx['native_terminal']={'source':'result','status':status}
                ad._bg_publish=mock.Mock();ad._recap_arm=mock.Mock()
                ad._finish_turn(ctx['sid'],ctx,None)
                self.assertEqual(ad.session_environment(ctx['sid']) is None,status=='completed')

    def test_lifecycle_closed_failure_still_projects_auth(self):
        ad,ctx,bus=self.make(ea.ClaudeAdapter)
        ad.index[ctx['sid']]={}
        ctx['_lifecycle_closed']=True
        ad._turn_error_environment(ctx['sid'],{'code':'AUTH','message':'expired'})
        self.assertEqual(ad.session_environment(ctx['sid'])['engine'],'claude')
        self.assertEqual(ad.session_environment(ctx['sid'])['stage'],'auth')

    def test_clearing_no_prior_issue_also_fences_late_failure(self):
        ad,ctx,bus=self.make(ea.ClaudeAdapter)
        ad.index[ctx['sid']]={}
        ad.resolve_auth({'engine':'claude','provider':'claude'},20)
        ad._env_publish(ctx['sid'],{'stage':'auth','state':'error','label':'late','startedAt':10})
        self.assertIsNone(ad.session_environment(ctx['sid']))


    def test_environment_snapshot_pairs_revision_with_content(self):
        ad,ctx,bus=self.make(ea.ClaudeAdapter)
        ad.index[ctx['sid']]={}
        ad._env_publish(ctx['sid'],{'stage':'auth','state':'error','label':'expired','startedAt':10})
        snapshot=ad.environment_snapshot(ctx['sid'])
        ad.resolve_auth({'engine':'claude','provider':'claude'},20)
        current=ad.environment_snapshot(ctx['sid'])
        self.assertEqual(snapshot['revision'],snapshot['environment']['issueRevision'])
        self.assertGreater(current['revision'],snapshot['revision'])
        self.assertIsNone(current['environment'])
        self.assertEqual(snapshot['environment']['state'],'error')


    def test_direct_worker_auth_notice_uses_its_engine(self):
        ad,ctx,bus=self.make(ea.ClaudeAdapter)
        ad.index[ctx['sid']]={'shepherd_mode':True,'selected_engine':'pi'}
        ad._env_publish(ctx['sid'],{'stage':'auth','state':'error','label':'expired'})
        self.assertIn('auth:claude:claude',ad.index[ctx['sid']]['environment_issues'])
        self.assertIsNone(ad.session_environment(ctx['sid']))
