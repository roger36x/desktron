"""发布流程等待、断线恢复与人工交接；仅使用临时数据库和模拟宿主。"""
import os
from test_app_control_browser_ac import Base, APPS
from app_control.runtime import AppControlError, AppControlRuntime


class ReleaseWait(Base):
    def begin(self, condition=None):
        return self.rt.act(self.lease, 's1', 'wait', condition or {
            'selector': '#continue', 'state': 'enabled', 'timeout_ms': 1000000})

    def finish(self, dispatched, ok=True, error=''):
        return self.rt.complete(dispatched['id'], ok, {'url': 'https://test/release'}, error,
                                '1:8', dispatched['dispatch_id'])

    def restart(self):
        path = self.rt.db_path
        self.rt.close()
        self.rt = AppControlRuntime(path, apps_provider=lambda: APPS,
                                    clock=lambda: self.now[0], evidence_dir=os.path.join(self.tmp.name, 'ev'))
        self.rt.NOTIFY_DELAY_S = 10 ** 6

    def test_fifteen_minute_wait_has_short_probes_and_fixed_deadline(self):
        op = self.begin()
        first = self.rt.next_operation('browser')
        self.assertLessEqual(first['input']['timeout_ms'], 1000)
        pending = self.finish(first, False, 'wait_timeout')
        self.assertEqual(pending['status'], 'queued')
        self.assertIsNone(self.rt.next_operation('browser'))
        self.now[0] += 900
        self.restart()
        second = self.rt.next_operation('browser')
        self.assertEqual(second['id'], op['id'])
        self.assertEqual(second['wait_deadline'], 2000)
        done = self.finish(second)
        self.assertEqual(done['status'], 'succeeded')
        self.assertTrue(done['output']['condition_met'])

    def test_lost_probe_and_late_receipt_do_not_finish_new_attempt(self):
        self.begin()
        first = self.rt.next_operation('browser')
        self.now[0] += 6
        self.assertIsNone(self.rt.next_operation('browser'))
        self.now[0] += 1
        second = self.rt.next_operation('browser')
        self.assertNotEqual(first['dispatch_id'], second['dispatch_id'])
        self.assertEqual(self.finish(first)['status'], 'running')
        self.assertEqual(self.finish(second)['status'], 'succeeded')

    def test_restart_during_probe_reissues_only_read(self):
        self.begin()
        first = self.rt.next_operation('browser')
        self.restart()
        second = self.rt.next_operation('browser')
        self.assertEqual(second['name'], 'wait')
        self.assertNotEqual(second['dispatch_id'], first['dispatch_id'])
        self.assertEqual(self.finish(first)['status'], 'running')
        self.assertEqual(self.finish(second)['status'], 'succeeded')

    def test_wait_deadline_survives_restarts_and_offline_host(self):
        op = self.begin({'selector': '#done', 'timeout_ms': 10000})
        self.rt.next_operation('browser')
        self.now[0] += 11
        self.restart()
        self.assertIsNone(self.rt.next_operation('browser'))
        self.assertEqual(self.rt.get_operation(op['id'])['error'], 'wait_timeout')

    def test_postcondition_does_not_replay_approved_click(self):
        op = self.rt.act(self.lease, 's1', 'click', {
            'selector': '#publish', 'wait_for': {'selector': '[role=dialog]', 'state': 'visible',
                                               'timeout_ms': 10000}}, idempotency_key='publish-once')
        self.rt.approve(op['id'], True, 'ui_user')
        click = self.rt.next_operation('browser')
        self.assertEqual(click['name'], 'click')
        pending = self.rt.complete(click['id'], True, {'clicked': True}, observation_version='1:8')
        self.assertTrue(pending['output']['action_executed'])
        self.restart()
        probe = self.rt.next_operation('browser')
        self.assertEqual(probe['name'], 'wait')
        self.finish(probe, False, 'wait_timeout')
        self.now[0] += 11
        failed = self.rt.get_operation(op['id'])
        self.assertEqual(failed['error'], 'postcondition_timeout')
        self.assertTrue(failed['output']['action_executed'])
        repeated = self.rt.act(self.lease, 's1', 'click', op['input'], idempotency_key='publish-once')
        self.assertEqual(repeated['id'], op['id'])
        self.assertIsNone(self.rt.next_operation('browser'))

    def test_inflight_write_is_never_replayed_on_restart(self):
        op = self.rt.act(self.lease, 's1', 'click', {'selector': '#publish',
            'wait_for': {'selector': '[role=dialog]'}}, idempotency_key='uncertain')
        self.rt.approve(op['id'], True, 'ui_user')
        self.rt.next_operation('browser')
        self.restart()
        self.assertIsNone(self.rt.next_operation('browser'))
        self.assertEqual(self.rt.get_operation(op['id'])['error'], 'runtime_restarted_during_execution')

    def test_human_intervention_cancels_old_work_and_requires_fresh_inspect(self):
        self.observe()
        op = self.rt.act(self.lease, 's1', 'click', {'selector': '#publish'}, idempotency_key='old')
        self.rt.approve(op['id'], True, 'ui_user')
        self.rt.pause(self.lease)
        self.rt.resume(self.lease)
        self.assertIsNone(self.rt.next_operation('browser'))
        self.assertEqual(self.rt.get_operation(op['id'])['status'], 'cancelled')
        with self.assertRaisesRegex(AppControlError, 'reinspect_required'):
            self.rt.act(self.lease, 's1', 'click', {'selector': '#publish'}, idempotency_key='new')
        self.observe(url='https://test/different-release')
        new = self.rt.act(self.lease, 's1', 'click', {'selector': '#publish'}, idempotency_key='new')
        self.assertEqual(new['status'], 'waiting_user')

    def test_old_inspect_cannot_unlock_handoff(self):
        op = self.rt.inspect(self.lease, 's1')
        self.rt.next_operation('browser')
        self.rt.pause(self.lease)
        self.rt.resume(self.lease)
        self.rt.complete(op['id'], True, {'url': 'https://test/old'}, observation_version='1:7')
        self.assertTrue(self.rt.get_lease(self.lease)['needs_inspect'])

    def test_pause_ends_durable_wait(self):
        op = self.begin()
        probe = self.rt.next_operation('browser')
        self.rt.pause(self.lease)
        self.rt.resume(self.lease)
        self.assertEqual(self.finish(probe)['status'], 'cancelled')
        self.assertEqual(self.rt.get_operation(op['id'])['error'], 'human_intervention_reinspect_required')

    def test_failure_condition_and_resource_change_are_terminal(self):
        for error in ('wait_aborted', 'resource_changed', 'invalid_selector', 'locator_ambiguous'):
            op = self.begin()
            probe = self.rt.next_operation('browser')
            self.assertEqual(self.finish(probe, False, error)['error'], error)
            self.assertEqual(self.rt.get_operation(op['id'])['status'], 'failed')

    def test_new_read_does_not_shorten_long_wait_lease(self):
        self.begin({'selector': '#done', 'timeout_ms': 1800000})
        expires = self.rt.get_lease(self.lease)['expires_at']
        self.rt.inspect(self.lease, 's1')
        self.assertEqual(self.rt.get_lease(self.lease)['expires_at'], expires)

    def test_wait_for_is_validated_before_any_click(self):
        for condition in ({}, {'event': 'not_supported'}, {'state': 'enabled'},
                          {'selector': '#done', 'timeout_ms': 1800001},
                          {'locator': {'nth': 1}}, {'selector': '#done', 'arbitrary_js': 'alert(1)'}):
            with self.assertRaises(AppControlError):
                self.rt.act(self.lease, 's1', 'click', {'selector': '#publish', 'wait_for': condition},
                            idempotency_key='bad')
        self.assertIsNone(self.rt.next_operation('browser'))

    def test_trace_does_not_leak_wait_text(self):
        op = self.begin({'text': 'private-business-content', 'timeout_ms': 20000})
        trace = self.rt.trace(op['id'], 's1')
        self.assertNotIn('private-business-content', str(trace))
        self.assertEqual(trace['output']['phase'], 'waiting')

    def test_wait_blocks_queued_write_but_allows_inspection_between_probes(self):
        waiting = self.begin()
        write = self.rt.act(self.lease, 's1', 'click', {'selector': '#publish'}, idempotency_key='later')
        self.rt.approve(write['id'], True, 'ui_user')
        probe = self.rt.next_operation('browser')
        self.assertEqual(probe['id'], waiting['id'])
        self.finish(probe, False, 'wait_timeout')
        self.assertIsNone(self.rt.next_operation('browser'))
        inspect = self.rt.inspect(self.lease, 's1')
        self.assertEqual(self.rt.next_operation('browser')['id'], inspect['id'])

    def test_lease_expiry_cancels_running_probe(self):
        op = self.begin()
        probe = self.rt.next_operation('browser')
        with self.rt.db:
            self.rt.db.execute('UPDATE leases SET expires_at=? WHERE id=?', (self.now[0], self.lease))
        done = self.finish(probe)
        self.assertEqual(done['status'], 'cancelled')
        self.assertEqual(self.rt.get_operation(op['id'])['error'], 'lease_expired')

    def test_navigation_context_loss_retries_only_the_probe(self):
        self.begin()
        probe = self.rt.next_operation('browser')
        self.assertEqual(self.finish(probe, False, 'wait_context_changed')['status'], 'queued')
        self.now[0] += 2
        self.assertEqual(self.rt.next_operation('browser')['name'], 'wait')

    def test_old_approved_version_is_rejected_after_postcondition(self):
        self.observe()
        first = self.rt.act(self.lease, 's1', 'click', {'selector': '#publish',
            'wait_for': {'selector': '[role=dialog]'}}, '1:7', 'first')
        second = self.rt.act(self.lease, 's1', 'click', {'selector': '#confirm'}, '1:7', 'second')
        self.rt.approve(first['id'], True, 'ui_user')
        self.rt.approve(second['id'], True, 'ui_user')
        self.rt.next_operation('browser')
        self.rt.complete(first['id'], True, {'clicked': True}, observation_version='1:8')
        probe = self.rt.next_operation('browser')
        self.finish(probe)
        self.assertIsNone(self.rt.next_operation('browser'))
        self.assertEqual(self.rt.get_operation(second['id'])['error'], 'stale_observation_version')

    def test_click_completing_after_human_pause_does_not_start_postcondition(self):
        op = self.rt.act(self.lease, 's1', 'click', {'selector': '#publish',
            'wait_for': {'selector': '[role=dialog]'}}, idempotency_key='started')
        self.rt.approve(op['id'], True, 'ui_user')
        self.rt.next_operation('browser')
        self.rt.pause(self.lease)
        self.rt.resume(self.lease)
        done = self.rt.complete(op['id'], True, {'clicked': True}, observation_version='1:8')
        self.assertTrue(done['output']['action_executed'])
        self.assertEqual(done['error'], 'human_intervention_reinspect_required')
        self.assertIsNone(self.rt.next_operation('browser'))
