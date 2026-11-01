"""Update ownership boundaries: prepare, ready, release, durable handoff, admission."""
import contextlib
import json
import os
import runpy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import lifecycle
import runtime_identity
import self_update as update
import self_update_apply as apply
import test_self_update as fixtures


class UpdateOwnership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    @contextlib.contextmanager
    def pending(self, outcome='idle'):
        identity = dict(boot_id='boot', host_instance_id='host', launch_generation='gen',
                        pid=os.getpid(), host_pid=os.getppid())
        state = dict(state='verifying', pending_protocol=2, operation_id='operation', apply_pid=0,
                     pending_outcome=outcome, pending_rev='payload', pending_build=2,
                     pending_host_rev='host-rev', pending_host_build=3,
                     pending_identity=identity, acceptance_unknown=True)
        with mock.patch.object(update, '_base_dir', return_value=str(self.base)), \
             mock.patch.object(update, '_current', return_value=('payload', 2, False)), \
             mock.patch.object(update, '_DRAINING', False), \
             mock.patch.object(runtime_identity, 'facts', return_value=identity), \
             mock.patch.dict(os.environ, {'DESKTRON_HOST_REV':'host-rev', 'DESKTRON_HOST_BUILD':'3'}), \
             mock.patch.object(lifecycle, '_control_root', return_value=str(self.base / 'control')):
            update._save_state(state)
            yield state, identity

    def test_v2_pending_cannot_trigger_old_rollback_readers_version_only_commit(self):
        save = mock.Mock(side_effect=lambda state: state)
        old = runpy.run_path(str(Path(__file__).parent / 'fixtures/update_reader_v1.py'),
                            init_globals={'_safe_int':update._safe_int, '_save_state':save})
        reader = old['_reconcile_pending_commit']
        with self.pending('rolled_back') as (state, _):
            got = reader(dict(state), 'payload', 2, 'host-rev', 3)
            self.assertTrue(got['acceptance_unknown'])
            self.assertEqual(got['state'], 'verifying'); save.assert_not_called()
            # Positive control proves the frozen old reader reproduces the unsafe v1 shortcut.
            got = reader({**state, 'state':'accepted_pending_commit'}, 'payload', 2, 'host-rev', 3)
            self.assertFalse(got['acceptance_unknown']); save.assert_called_once()

    def test_version_match_without_release_receipt_never_opens_admission(self):
        with self.pending():
            self.assertEqual(update.status()['state'], 'verifying')
            with update.admit_work() as admitted:
                self.assertFalse(admitted)

    def test_commit_requires_exact_runtime_and_operation_and_dead_helper(self):
        for mismatch in ('boot_id', 'host_instance_id', 'launch_generation', 'pid', 'host_pid',
                         'operation', 'helper_alive', 'lock_held'):
            with self.subTest(mismatch=mismatch), self.pending() as (state, identity), contextlib.ExitStack() as stack:
                apply.commit_handoff(update._state_path(), state)
                if mismatch in identity:
                    actual = dict(identity); actual[mismatch] = 'different'
                    stack.enter_context(mock.patch.object(runtime_identity, 'facts', return_value=actual))
                elif mismatch == 'operation':
                    apply.commit_handoff(update._state_path(), {**state, 'operation_id':'old-operation'})
                elif mismatch == 'helper_alive':
                    stack.enter_context(mock.patch.object(update, '_pid_alive', return_value=True))
                else:
                    stack.enter_context(mock.patch.object(lifecycle, 'try_update_lock', return_value=None))
                got = update.status()
                self.assertEqual(got['state'], 'verifying')
                self.assertTrue(got['acceptance_unknown'])

    def test_post_release_crash_reconciles_only_exact_accepted_boot(self):
        for outcome in ('idle', 'rolled_back'):
            with self.subTest(outcome=outcome), self.pending(outcome) as (state, _):
                apply.commit_handoff(update._state_path(), state)
                got = update.status()
                self.assertEqual(got['state'], outcome)
                self.assertFalse(got['acceptance_unknown'])
                self.assertEqual(update._load_state()['state'], outcome)

    def test_status_does_not_overwrite_newer_state_under_lock(self):
        with self.pending() as (state, _):
            apply.commit_handoff(update._state_path(), state)
            snapshot = update._load_state()
            update._save_state({**snapshot, 'operation_id':'new-op'})
            got = update._reconcile_pending_commit(snapshot, 'payload', 2, 'host-rev', 3)
            self.assertEqual(got['operation_id'], 'new-op')
            self.assertTrue(got['acceptance_unknown'])

    def test_unknown_state_survives_successful_and_failed_checks(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(fixtures.manifest()).encode()
        with mock.patch.object(update, '_base_dir', return_value=str(self.base)), \
             mock.patch.object(update, '_current', return_value=('dev', 0, True)), \
             mock.patch.object(update, '_channel_base', return_value='https://test.invalid'):
            update._save_state(dict(state='rollback_failed', acceptance_unknown=True,
                                    operation_id='old-op', error='evidence'))
            before = Path(update._state_path()).read_bytes()
            for result in (response, OSError('offline')):
                with mock.patch.object(update.urllib.request, 'urlopen', side_effect=[result]):
                    update.check_now(download=True)
                self.assertEqual(Path(update._state_path()).read_bytes(), before)

    def test_unknown_transaction_cannot_be_overwritten_by_development_request(self):
        stage = self.base / 'updates/dev-host.123456/Desktron.app'
        binary = stage / 'Contents/MacOS/desktron'; binary.parent.mkdir(parents=True)
        binary.write_text('fixture', encoding='utf-8')
        resources = stage / 'Contents/Resources'; resources.mkdir()
        (resources / 'release-rev').write_text('new', encoding='utf-8'); (resources / 'release-build').write_text('2', encoding='utf-8')
        with mock.patch.object(update, '_base_dir', return_value=str(self.base)), \
             mock.patch.object(update, '_current', return_value=('dev', 0, True)), \
             mock.patch.object(update.sys, 'platform', 'darwin'), \
             mock.patch.object(runtime_identity, 'facts', return_value={'supervisor':'native'}), \
             mock.patch.object(update, '_request_native_apply') as request:
            update._save_state(dict(state='rollback_failed', acceptance_unknown=True, operation_id='old'))
            before = Path(update._state_path()).read_bytes()
            self.assertFalse(update.request_development_app('dev-host.123456', 'new')['ok'])
            request.assert_not_called()
            self.assertEqual(Path(update._state_path()).read_bytes(), before)

    def test_prepare_failures_do_not_authorize_exit_or_enter_rollback(self):
        for failure in ('current_contract', 'staged_contract', 'sleep_inhibitor'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(dir=self.base) as td:
                base, path, home, app = fixtures.SafeExtraction()._release_fixture(td)
                state = json.loads(path.read_text(encoding='utf-8'))
                state.update(operation_id='op', previous_boot_id='boot', previous_host_instance_id='host',
                             previous_launch_generation='gen', previous_host_rev='old', previous_host_build=1)
                path.write_text(json.dumps(state), encoding='utf-8')
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(apply.os.path, 'expanduser', return_value=str(app)))
                    for name in ('require_native_parent', 'prepare_managed_pi_extensions', 'release_sleep_inhibitor'):
                        stack.enter_context(mock.patch.object(apply, name))
                    stack.enter_context(mock.patch.object(apply, 'endpoint', return_value={'local':True, 'enabled':False}))
                    stack.enter_context(mock.patch.object(
                        apply, 'verify_stable_app',
                        return_value=fixtures.SafeExtraction.STABLE_REQUIREMENT))
                    stack.enter_context(mock.patch.object(
                        apply, 'installed_requirement_or_legacy',
                        return_value=fixtures.SafeExtraction.STABLE_REQUIREMENT))
                    current = stack.enter_context(mock.patch.object(apply, 'verify_current_native_contract'))
                    staged = stack.enter_context(mock.patch.object(apply, 'verify_staged_launch_contract'))
                    inhibitor = stack.enter_context(mock.patch.object(apply, 'acquire_sleep_inhibitor', return_value=None))
                    stack.enter_context(mock.patch.object(apply, 'require_sleep_inhibitor'))
                    {'current_contract':current, 'staged_contract':staged, 'sleep_inhibitor':inhibitor}[failure].side_effect = RuntimeError(failure)
                    ready = stack.enter_context(mock.patch.object(apply, 'signal_native_ready'))
                    replace = stack.enter_context(mock.patch.object(apply, 'replace_dir'))
                    rollback = stack.enter_context(mock.patch.object(apply, 'recover_native_host'))
                    self.assertEqual(apply.apply(str(path), native_host_pid=4321, operation_id='op'), 1)
                    ready.assert_not_called(); replace.assert_not_called(); rollback.assert_not_called()
                saved = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(saved['state'], 'error'); self.assertFalse(saved['acceptance_unknown'])
                self.assertTrue((app / 'old').exists()); self.assertTrue((home / 'old').exists())

    def test_helper_digest_mismatch_rejected_before_any_state_write(self):
        _, path, _, _ = fixtures.SafeExtraction()._release_fixture(str(self.base))
        state = json.loads(path.read_text(encoding='utf-8')); state.update(operation_id='op', helper_sha256='wrong')
        path.write_text(json.dumps(state), encoding='utf-8'); before = path.read_bytes()
        with mock.patch.object(apply, 'require_native_parent'), mock.patch.object(apply, 'signal_native_ready') as ready:
            with self.assertRaisesRegex(ValueError, '摘要不匹配'):
                apply.apply(str(path), native_host_pid=4321, operation_id='op')
            ready.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_rollback_handoff_persistence_failure_never_stops_released_host(self):
        process = mock.Mock()
        with mock.patch.object(apply, 'wait_backend_down', return_value=True), \
             mock.patch.object(apply, 'launch_app_exact', return_value=process) as launch, \
             mock.patch.object(apply, 'probe', return_value=(True, 'healthy')), \
             mock.patch.object(apply, 'release_exact') as release, \
             mock.patch.object(apply, 'stop_exact') as stop:
            def fail(_): raise apply.HandoffPersistenceError('disk full after release')
            with self.assertRaises(apply.HandoffPersistenceError):
                apply.recover_native_host('/app', '/log', expect_rev='old', expect_host_rev='old',
                    previous_identity={}, expect_remote_wss=False, after_release=fail)
            launch.assert_called_once(); release.assert_called_once(); stop.assert_not_called()

    def test_release_visible_but_unconfirmed_never_reports_rollback_success(self):
        process = mock.Mock()
        with mock.patch.object(apply, 'wait_backend_down', return_value=True), \
             mock.patch.object(apply, 'launch_app_exact', return_value=process) as launch, \
             mock.patch.object(apply, 'probe', return_value=(True, 'healthy')), \
             mock.patch.object(apply, 'release_exact', side_effect=OSError('marker visible; ack failed')), \
             mock.patch.object(apply, 'stop_exact', side_effect=RuntimeError('leader identity lost')) as stop:
            ok, _ = apply.recover_native_host('/app', '/log', expect_rev='old', expect_host_rev='old',
                                             previous_identity={}, expect_remote_wss=False)
            self.assertFalse(ok); launch.assert_called_once(); stop.assert_called_once()

    def test_failed_terminal_write_preserves_pending_snapshot_and_receipt(self):
        with self.pending() as (state, _):
            apply.commit_handoff(update._state_path(), state)
            before = dict(state)
            with mock.patch.object(apply, 'atomic_json', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    apply.state_patch(update._state_path(), state, state='idle',
                                      pending_identity={}, pending_outcome='', operation_id='')
            self.assertEqual(state, before)
            self.assertEqual(update.status()['state'], 'idle')

    def test_state_snapshot_survives_file_and_parent_fsync_failure(self):
        for failed_call in ((1,) if os.name == 'nt' else (1, 2)):
            with self.subTest(fsync_call=failed_call), self.pending() as (state, _):
                before = dict(state)
                calls = [None] * (failed_call - 1) + [OSError('durability failure')]
                with mock.patch.object(apply.os, 'fsync', side_effect=calls):
                    with self.assertRaises(OSError):
                        apply.state_patch(update._state_path(), state, state='idle',
                                          pending_identity={}, pending_outcome='', operation_id='')
                self.assertEqual(state, before)
                # The handler can restore its complete pending record even if replace succeeded.
                apply.state_patch(update._state_path(), state, error='persist failed')
                self.assertEqual(update._load_state()['pending_identity'], before['pending_identity'])
                self.assertTrue(update._load_state()['acceptance_unknown'])

    def test_incompatible_copied_helper_is_rejected_before_host_submission(self):
        state = dict(state='staged', staged_rev='candidate')
        facts = dict(boot_id='boot', host_instance_id='host', launch_generation='gen')
        with mock.patch.object(update, '_base_dir', return_value=str(self.base)), \
             mock.patch.object(update, '_current', return_value=('old', 1, False)), \
             mock.patch.object(update, '_DRAINING', False), \
             mock.patch.object(runtime_identity, 'facts', return_value=facts), \
             mock.patch.dict(os.environ, {'DESKTRON_HOST_REV':'host', 'DESKTRON_HOST_BUILD':'1'}), \
             mock.patch.object(lifecycle, 'prepare_restart', return_value={'ok':True}), \
             mock.patch.object(lifecycle, 'reopen') as reopen, \
             mock.patch.object(lifecycle, 'submit_host_update') as submit, \
             mock.patch.object(update.subprocess, 'run', return_value=mock.Mock(stdout='{"helper_contract":1}')):
            update._save_state(state)
            result = update._request_native_apply(state, automatic=False, mandatory=False)
            self.assertFalse(result['ok'])
            submit.assert_not_called(); reopen.assert_called_once()
            self.assertEqual(update._load_state()['state'], 'staged')
            self.assertFalse(update._DRAINING)

    def test_watcher_does_not_reopen_while_timed_out_helper_is_alive(self):
        operation = 'operation'; facts = dict(host_instance_id='host', launch_generation='gen', boot_id='boot', pid=42)
        with mock.patch.object(update, '_base_dir', return_value=str(self.base)), \
             mock.patch.object(update, '_DRAINING', True), \
             mock.patch.object(runtime_identity, 'facts', return_value=facts), \
             mock.patch.object(lifecycle, 'control_root', return_value=str(self.base)), \
             mock.patch.object(lifecycle, 'reopen') as reopen, \
             mock.patch.object(update, '_pid_alive', return_value=True), \
             mock.patch.object(update.time, 'sleep'), \
             mock.patch.object(update.time, 'monotonic', side_effect=[0, 0, 941]):
            update._save_state(dict(state='applying', operation_id=operation, apply_pid=123))
            (self.base / ('update-response-' + operation + '.json')).write_text(json.dumps(dict(
                operation_id=operation, state='failed', host_instance_id='host', request_launch_generation='gen',
                request_boot_id='boot', request_pid=42)), encoding='utf-8')
            update._watch_native_apply(operation)
            reopen.assert_not_called(); self.assertTrue(update._DRAINING)
            self.assertEqual(update._load_state()['state'], 'applying')

    def test_descendant_exit_between_enumeration_and_birth_read_is_not_live_uncertainty(self):
        process = mock.Mock(pid=100, _desk_descendant_receipts={})
        with mock.patch.object(apply, '_process_parent_rows', side_effect=[[(200, 100)], []]), \
             mock.patch.object(apply, 'process_start_token', return_value=''), \
             mock.patch.object(apply, 'alive', return_value=True):
            # A zombie may answer kill(0), but is excluded by a fresh successful ps snapshot.
            self.assertEqual(apply.observe_transaction_descendants(process), {})

    def test_session_query_race_distinguishes_exited_from_unknown_live_process(self):
        snapshot = mock.Mock(returncode=0, stdout='200 100 S sleep\n')
        with mock.patch.object(apply.subprocess, 'run', return_value=snapshot), \
             mock.patch.object(apply.os, 'getsid', side_effect=ProcessLookupError):
            with mock.patch.object(apply, '_process_parent_rows', return_value=[]):
                self.assertEqual(apply.process_group_members(100), [])
            with mock.patch.object(apply, '_process_parent_rows', return_value=[(200, 100)]):
                with self.assertRaisesRegex(RuntimeError, '进程会话'):
                    apply.process_group_members(100)

    def test_unreadable_birth_identity_of_live_descendant_still_fails_closed(self):
        process = mock.Mock(pid=100, _desk_descendant_receipts={})
        with mock.patch.object(apply, '_process_parent_rows', return_value=[(200, 100)]), \
             mock.patch.object(apply, 'process_start_token', return_value=''), \
             mock.patch.object(apply, 'alive', return_value=True):
            with self.assertRaisesRegex(RuntimeError, '出生身份'):
                apply.observe_transaction_descendants(process)
            with self.assertRaisesRegex(RuntimeError, '仍存活'):
                apply.process_matches_token(200, 'saved-birth')

    @contextlib.contextmanager
    def late_contract_receipt(self, *, child_visible=True, child_tokens=None):
        """Publish the Swift receipt just after the first descendant snapshot."""
        app = self.base / 'candidate.app'
        resources = app / 'Contents/Resources'; resources.mkdir(parents=True)
        (resources / 'update-launch-contract').write_text('1', encoding='utf-8')
        receipt = None
        process = mock.Mock(pid=100, _desk_host_pid=200, _desk_pgid=100,
                            _desk_descendant_receipts={})
        process.poll.return_value = None
        observe = apply.observe_transaction_descendants
        tokens = iter(child_tokens) if child_tokens is not None else None

        def launch(*args, **kwargs):
            nonlocal receipt
            receipt = Path(kwargs['contract_receipt'])
            return process

        def rows():
            return [(200, 100)] + ([(300, 200)] if receipt.exists() and child_visible else [])

        def token(pid):
            return next(tokens, 'replacement') if pid == 300 and tokens is not None else 'birth-%d' % pid

        def observe_then_publish(current):
            result = observe(current)
            receipt.write_text(json.dumps(dict(contract=1, host_pid=200, pid=300)), encoding='utf-8')
            return result

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(apply, 'launch_app_exact', side_effect=launch))
            stack.enter_context(mock.patch.object(apply, '_process_parent_rows', side_effect=rows))
            stack.enter_context(mock.patch.object(apply, 'process_start_token', side_effect=token))
            stack.enter_context(mock.patch.object(apply, 'alive', return_value=True))
            observed = stack.enter_context(mock.patch.object(
                apply, 'observe_transaction_descendants', side_effect=observe_then_publish))
            stack.enter_context(mock.patch.object(apply, 'native_process_checks',
                                                 return_value=dict(native_parent=True, process_group=True)))
            stack.enter_context(mock.patch.object(apply, 'owned_group_leader', return_value=True))
            stopped = stack.enter_context(mock.patch.object(apply, 'stop_exact'))
            yield str(app), process, observed, stopped

    def test_contract_receipt_after_snapshot_reobserves_exact_child_before_accepting(self):
        with self.late_contract_receipt() as (app, process, observed, stopped):
            apply.verify_staged_launch_contract(app, str(self.base / 'host.log'))
            self.assertEqual(process._desk_descendant_receipts[300], 'birth-300')
            self.assertGreaterEqual(observed.call_count, 2)
            stopped.assert_called_once_with(process, timeout=2)

    def test_contract_receipt_cannot_authorize_an_unobserved_pid(self):
        with self.late_contract_receipt(child_visible=False) as (app, process, _, stopped):
            with self.assertRaisesRegex(RuntimeError, '契约自检失败'):
                apply.verify_staged_launch_contract(app, str(self.base / 'host.log'))
            self.assertNotIn(300, process._desk_descendant_receipts)
            stopped.assert_called_once_with(process, timeout=2)

    def test_contract_receipt_cannot_authorize_reused_child_identity(self):
        with self.late_contract_receipt(child_tokens=['original', 'replacement']) as (app, process, _, stopped):
            with self.assertRaisesRegex(RuntimeError, '契约自检失败'):
                apply.verify_staged_launch_contract(app, str(self.base / 'host.log'))
            self.assertNotIn(300, process._desk_descendant_receipts)
            stopped.assert_called_once_with(process, timeout=2)

    def test_contract_receipt_unreadable_live_child_birth_still_fails_closed(self):
        with self.late_contract_receipt(child_tokens=['']) as (app, process, _, stopped):
            with self.assertRaisesRegex(RuntimeError, '出生身份'):
                apply.verify_staged_launch_contract(app, str(self.base / 'host.log'))
            stopped.assert_called_once_with(process, timeout=2)

    def test_current_preflight_binds_existing_identity_not_new_generation(self):
        state = dict(previous_boot_id='boot', previous_host_instance_id='host', previous_launch_generation='gen',
                     previous_host_rev='old', previous_host_build=1)
        with mock.patch.object(apply, 'probe', return_value=(True, 'healthy')) as probe:
            apply.verify_current_native_contract(state, 4321, 'payload', False, lambda: None)
        self.assertEqual(probe.call_args.kwargs['expected_identity']['boot_id'], 'boot')
        self.assertEqual(probe.call_args.kwargs['expected_native_pid'], 4321)
        self.assertNotIn('previous_identity', probe.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
