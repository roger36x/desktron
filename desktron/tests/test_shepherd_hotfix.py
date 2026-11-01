"""P-1 止血：解卡死 reserved 回合、补隔离原因码。

只解开已经发生的阻塞，不改变正常回合的判定。
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron' / 'lib'))

from shepherd import Conflict, Coordinator, Store
from shepherd.recovery import ORPHAN_RESERVED_SECONDS, orphan_reserved
from shepherd.recovery_controller import RecoveryController


class HotfixFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.coordinator = Coordinator(self.store)
        self.cid = 'conversation_1'
        self.coordinator.create(self.cid)
        self.binding = self.coordinator.bind(
            self.cid, 'claude', 'native_1', native_cwd=self.tmp.name,
            connector_version='claude-v1', binding_id='binding_1',
            ownership_nonce='secret_nonce')

    def acquire(self):
        return self.coordinator.acquire(self.cid, self.binding,
                                        ownership_nonce='secret_nonce')


class CancelReservedWithoutRecovery(HotfixFixture, unittest.TestCase):
    """缺 recovery 块的 reserved 回合以前无法取消，会话被永久挡住。"""

    def test_reserved_round_without_recovery_block_can_be_cancelled(self):
        lease = self.acquire()
        row = self.coordinator.begin_round(
            self.cid, lease['lease_id'], lease['generation'],
            attempt_id='attempt_no_recovery')
        self.assertEqual(self.coordinator.state(self.cid)['inflight']['phase'], 'reserved')
        self.coordinator.cancel_reserved(self.cid, attempt_id=row['attempt_id'])
        state = self.coordinator.state(self.cid)
        self.assertIsNone(state.get('inflight'))
        audit = state['audit_events'][-1]
        self.assertEqual(audit['kind'], 'cancel_before_dispatch')
        self.assertEqual(audit['reason'], 'reserved_without_recovery')

    def test_reserved_round_with_recovery_block_still_cancels(self):
        lease = self.acquire()
        row = self.coordinator.begin_round(
            self.cid, lease['lease_id'], lease['generation'], attempt_id='attempt_normal',
            recovery={'phase': 'reserved', 'boot_id': 'boot_old', 'boundary': {}})
        self.coordinator.cancel_reserved(self.cid, attempt_id=row['attempt_id'])
        state = self.coordinator.state(self.cid)
        self.assertIsNone(state.get('inflight'))
        self.assertEqual(state['audit_events'][-1]['reason'], 'reserved')

    def test_dispatched_round_is_never_cancelled_as_undispatched(self):
        lease = self.acquire()
        row = self.coordinator.begin_round(
            self.cid, lease['lease_id'], lease['generation'], attempt_id='attempt_live',
            recovery={'phase': 'reserved', 'boot_id': 'boot_old', 'boundary': {}})
        self.coordinator.mark_dispatched(
            self.cid, lease['lease_id'], lease['generation'], attempt_id=row['attempt_id'],
            process={'pid': 42, 'pgid': 42, 'start_token': 'live'})
        with self.assertRaisesRegex(Conflict, '派发前取消'):
            self.coordinator.cancel_reserved(self.cid, attempt_id=row['attempt_id'])

    def test_dispatch_evidence_blocks_cancel_even_when_phase_reads_reserved(self):
        """派发痕迹独立成立：哪怕 phase 看起来还是 reserved 也不许取消。"""
        for field, value in (('process', {'pid': 7, 'pgid': 7, 'start_token': 'x'}),
                             ('native_locator', {'session_id': 'native_1'})):
            with self.subTest(field=field):
                turn = {'phase': 'reserved',
                        'recovery': {'phase': 'reserved', 'boot_id': 'boot_x',
                                     'boundary': {}, field: value}}
                self.assertFalse(Coordinator._undispatched(turn))

    def test_undispatched_accepts_reserved_with_or_without_recovery(self):
        self.assertTrue(Coordinator._undispatched({'phase': 'reserved'}))
        self.assertTrue(Coordinator._undispatched(
            {'phase': 'reserved',
             'recovery': {'phase': 'reserved', 'boot_id': 'b', 'boundary': {}}}))
        self.assertFalse(Coordinator._undispatched({'phase': 'dispatched'}))


class BindingStatusReason(HotfixFixture, unittest.TestCase):
    """隔离与退役以前不记原因，反复故障无法归因。"""

    def test_quarantine_records_reason_and_time(self):
        lease = self.acquire()
        target = self.coordinator.bind(
            self.cid, 'pi', 'native_2', native_cwd=self.tmp.name,
            connector_version='pi-v1', binding_id='binding_2',
            ownership_nonce='other_nonce')
        self.coordinator.prepare_switch(
            self.cid, target, ownership_nonce='other_nonce',
            lease_id=lease['lease_id'], generation=lease['generation'])
        self.coordinator.tombstone_for_delete(self.cid)
        binding = self.coordinator.state(self.cid)['bindings'][target]
        self.assertEqual(binding['status'], 'quarantined')
        self.assertEqual(binding['status_reason'],
                         'switch_target_abandoned_by_delete')
        self.assertGreater(binding['status_at'], 0)

    def test_mark_binding_truncates_and_keeps_optional_fields_absent(self):
        binding = {}
        Coordinator._mark_binding(binding, 'quarantined', 'x' * 400)
        self.assertEqual(len(binding['status_reason']), 120)
        self.assertNotIn('status_attempt_id', binding)
        self.assertNotIn('status_detail', binding)
        Coordinator._mark_binding(binding, 'retired', 'why',
                                  attempt_id='attempt_1', detail='d' * 400)
        self.assertEqual(binding['status_attempt_id'], 'attempt_1')
        self.assertEqual(len(binding['status_detail']), 240)

    def test_unspecified_reason_is_still_recorded(self):
        binding = {}
        Coordinator._mark_binding(binding, 'retired', '')
        self.assertEqual(binding['status_reason'], 'unspecified')


class OrphanReservedConstant(unittest.TestCase):
    def test_orphan_window_is_longer_than_a_dispatch_gap(self):
        # begin_round→mark_dispatched 是同进程内的连续动作；窗口必须远大于它，
        # 才不会把正在派发的回合误当成孤儿。
        self.assertGreaterEqual(ORPHAN_RESERVED_SECONDS, 300)


if __name__ == '__main__':
    unittest.main()


class _Service:
    """恢复控制器需要的最小服务：只提供 store、coordinator 和 connector 查表。"""

    def __init__(self, root):
        self.root = root
        self.store = Store(root)
        self.coordinator = Coordinator(self.store)
        self.connectors = {}

    def connector(self, binding):
        return self.connectors[binding['binding_id']]


class OrphanReservedPredicate(unittest.TestCase):
    """判据本身：只认「无 recovery 块 + reserved + 超龄」三者同时成立。"""

    def test_requires_reserved_phase_without_recovery_and_old_enough(self):
        old = time.time() - ORPHAN_RESERVED_SECONDS - 1
        self.assertTrue(orphan_reserved({'phase': 'reserved', 'started_at': old}))

    def test_fresh_reserved_round_is_not_an_orphan(self):
        """begin_round→mark_dispatched 的活窗口不能被判成孤儿。"""
        self.assertFalse(orphan_reserved({'phase': 'reserved', 'started_at': time.time()}))

    def test_round_carrying_a_recovery_block_is_never_an_orphan(self):
        """有 recovery 块就能用 boot_id 围栏，不走这条宽松路径。"""
        old = time.time() - ORPHAN_RESERVED_SECONDS - 1
        self.assertFalse(orphan_reserved(
            {'phase': 'reserved', 'started_at': old,
             'recovery': {'phase': 'reserved', 'boot_id': 'boot_x', 'boundary': {}}}))

    def test_dispatched_round_is_never_an_orphan(self):
        old = time.time() - ORPHAN_RESERVED_SECONDS - 1
        self.assertFalse(orphan_reserved({'phase': 'dispatched', 'started_at': old}))


class OrphanReservedPassesRecoveryGate(unittest.TestCase):
    """准入门与它守卫的动作必须同判据。

    以前 recover_one 能取消孤儿 reserved，但 recover_guarded 的 eligible 只认
    dispatched，于是产品路径永远到不了那个取消分支，回合一直卡住。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = _Service(self.tmp.name)
        self.cid = 'orphan_conversation'
        coordinator = self.service.coordinator
        coordinator.create(self.cid)
        self.binding_id = coordinator.bind(
            self.cid, 'claude', 'native_1', native_cwd=self.tmp.name,
            connector_version='claude-v1', binding_id='binding_1',
            ownership_nonce='secret_nonce')
        self.lease = coordinator.acquire(self.cid, self.binding_id,
                                         ownership_nonce='secret_nonce')

    def _begin_without_recovery(self, *, age):
        """构造迁移进来的那类回合：reserved，且从未写过 recovery 块。"""
        coordinator = self.service.coordinator
        row = coordinator.begin_round(self.cid, self.lease['lease_id'],
                                      self.lease['generation'], attempt_id='attempt_orphan')
        with self.service.store.change(self.cid) as state:
            state['active_turn']['started_at'] = time.time() - age
        return row

    def test_stale_orphan_is_admitted_and_cancelled_through_the_product_path(self):
        self._begin_without_recovery(age=ORPHAN_RESERVED_SECONDS + 60)
        controller = RecoveryController(self.service, boot_id='new_boot', retry_delays=())
        report = controller.recover_guarded(self.cid, trigger='send_before')
        self.assertEqual(report['outcome'], 'cancelled_before_dispatch')
        state = self.service.coordinator.state(self.cid)
        self.assertIsNone(state.get('inflight'))

    def test_cancelled_orphan_leaves_an_audit_reason_rather_than_vanishing(self):
        """解卡不等于掩盖：必须留下为什么被取消。"""
        self._begin_without_recovery(age=ORPHAN_RESERVED_SECONDS + 60)
        controller = RecoveryController(self.service, boot_id='new_boot', retry_delays=())
        controller.recover_guarded(self.cid, trigger='send_before')
        state = self.service.coordinator.state(self.cid)
        audit = state['audit_events'][-1]
        self.assertEqual(audit['kind'], 'cancel_before_dispatch')
        self.assertEqual(audit['reason'], 'reserved_without_recovery')

    def test_fresh_reserved_round_is_still_refused_by_the_gate(self):
        """活窗口不能因为这次放宽而被恢复路径碰到。"""
        self._begin_without_recovery(age=1)
        controller = RecoveryController(self.service, boot_id='new_boot', retry_delays=())
        report = controller.recover_guarded(self.cid, trigger='send_before')
        self.assertEqual(report['outcome'], 'not_applicable')
        self.assertIsNotNone(self.service.coordinator.state(self.cid).get('inflight'))
