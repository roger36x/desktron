import tempfile
import threading

import pytest

from engine_adapters.delivery import DeliveryConflict, DeliveryReceipts


def test_duplicate_claim_is_idempotent_and_payload_is_not_stored():
    with tempfile.TemporaryDirectory() as root:
        receipts = DeliveryReceipts(root, 'boot_a')
        results = []
        def claim(): results.append(receipts.claim('adp_s', 'request_1', {'text':'private body'})[1])
        threads = [threading.Thread(target=claim) for _ in range(6)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        assert sum(results) == 1
        assert 'private body' not in open(receipts.path('adp_s'), encoding='utf-8').read()
        with pytest.raises(DeliveryConflict):
            receipts.claim('adp_s', 'request_1', {'text':'different'})


def test_restart_rejects_unstored_and_stored_but_fences_dispatched():
    with tempfile.TemporaryDirectory() as root:
        old = DeliveryReceipts(root, 'boot_old')
        old.claim('accepted', 'r1', {})
        old.claim('stored', 'r2', {})
        old.stored('stored', 'r2', message_id='msg_u_1')
        old.claim('sent', 'r3', {})
        old.stored('sent', 'r3', message_id='msg_u_2')
        old.dispatched('sent', 'r3', engine_session_id='native_1')
        new = DeliveryReceipts(root, 'boot_new')
        assert new.recover('accepted')
        assert new.recover('stored')
        assert new.recover('sent')
        assert new.get('accepted', 'r1')['reason'] == 'restart_before_store'
        assert new.get('stored', 'r2')['status'] == 'cleanup_required'
        assert new.get('stored', 'r2')['reason'] == 'restart_before_dispatch_cleanup'
        assert new.get('sent', 'r3')['status'] == 'uncertain'
        with pytest.raises(DeliveryConflict) as error:
            new.claim('sent', 'r4', {})
        assert error.value.code == 'delivery_uncertain'


def test_terminal_or_explicit_unknown_resolution_releases_next_request():
    with tempfile.TemporaryDirectory() as root:
        receipts = DeliveryReceipts(root, 'boot')
        receipts.claim('done', 'r1', {})
        receipts.stored('done', 'r1', message_id='msg_u')
        receipts.dispatched('done', 'r1')
        receipts.finish('done', 'r1', reason='native_terminal', outcome='completed')
        assert receipts.claim('done', 'r2', {})[1]

        receipts.claim('unknown', 'r1', {})
        receipts.stored('unknown', 'r1', message_id='msg_u')
        receipts.dispatched('unknown', 'r1')
        restarted = DeliveryReceipts(root, 'boot_new')
        restarted.recover('unknown')
        restarted.resolve_uncertain('unknown', 'r1')
        assert restarted.claim('unknown', 'r2', {})[1]


def test_cleanup_required_cannot_be_finished_by_normal_turn_path():
    with tempfile.TemporaryDirectory() as root:
        receipts=DeliveryReceipts(root,'boot')
        receipts.claim('adp_s','r1',{})
        receipts.storing('adp_s','r1',message_id='m1')
        receipts.require_cleanup('adp_s','r1')
        with pytest.raises(DeliveryConflict):
            receipts.finish('adp_s','r1',reason='failed_before_dispatch',outcome='failed')
        assert receipts.get('adp_s','r1')['status']=='cleanup_required'


def test_terminal_request_identity_remains_idempotent():
    with tempfile.TemporaryDirectory() as root:
        receipts=DeliveryReceipts(root,'boot')
        receipts.claim('adp_s','old',{'text':'once'})
        receipts.stored('adp_s','old',message_id='m-old')
        receipts.finish('adp_s','old',reason='done')
        got,fresh=receipts.claim('adp_s','old',{'text':'once'})
        assert not fresh and got['status']=='finished'
        with pytest.raises(DeliveryConflict):
            receipts.claim('adp_s','old',{'text':'different'})


def test_native_identity_can_be_attached_after_dispatch_without_changing_stage():
    with tempfile.TemporaryDirectory() as root:
        receipts = DeliveryReceipts(root, 'boot')
        receipts.claim('adp_s', 'r1', {})
        receipts.stored('adp_s', 'r1', message_id='msg_u')
        receipts.dispatched('adp_s', 'r1')
        row = receipts.bind_native('adp_s', 'r1', engine_session_id='thread_1', native_turn_id='turn_1')
        assert row['status'] == 'dispatched'
        assert row['engine_session_id'] == 'thread_1'
        assert row['native_turn_id'] == 'turn_1'


def test_retry_proof_is_only_returned_by_the_cancellation_that_won():
    with tempfile.TemporaryDirectory() as root:
        receipts = DeliveryReceipts(root, 'boot')
        first = receipts.cancel_unsent('s', 'r')
        assert first['status'] == 'cancelled' and first['retryable'] is True
        assert receipts.cancel_unsent('s', 'r')['retryable'] is False
        assert 'retryable' not in receipts.get('s', 'r')
        assert receipts.claim('s', 'r', {'text':'late'})[1] is False
        receipts.claim('s2', 'r2', {})
        receipts.stored('s2', 'r2', message_id='m')
        receipts.dispatched('s2', 'r2')
        assert receipts.cancel_unsent('s2', 'r2')['retryable'] is False


def test_user_cancel_after_restart_or_projection_cleanup_never_authorizes_retry():
    with tempfile.TemporaryDirectory() as root:
        receipts = DeliveryReceipts(root, 'old')
        receipts.claim('s', 'r', {})
        restarted = DeliveryReceipts(root, 'new')
        restarted.recover('s')
        assert restarted.get('s', 'r')['reason'] == 'restart_before_store'
        assert restarted.cancel_unsent('s', 'r')['status'] == 'cancelled'
        assert restarted.get('s', 'r')['reason'] == 'cancelled_after_recovery'
        receipts.claim('s2', 'r2', {})
        receipts.stored('s2', 'r2', message_id='m')
        receipts.cancel_unsent('s2', 'r2')
        receipts.finish_cleanup('s2', 'r2')
        assert receipts.get('s2', 'r2')['reason'] == 'cancelled_before_dispatch'


def test_cancel_during_restart_cleanup_removes_retry_permission():
    with tempfile.TemporaryDirectory() as root:
        old = DeliveryReceipts(root, 'old')
        old.claim('s', 'r', {})
        old.stored('s', 'r', message_id='m')
        new = DeliveryReceipts(root, 'new')
        new.recover('s')
        new.cancel_unsent('s', 'r')
        new.finish_cleanup('s', 'r')
        assert new.get('s', 'r')['reason'] == 'cancelled_before_dispatch'


def test_recovered_retry_proof_can_only_be_consumed_once():
    with tempfile.TemporaryDirectory() as root:
        old = DeliveryReceipts(root, 'old')
        old.claim('s', 'r', {})
        new = DeliveryReceipts(root, 'new')
        new.recover('s')
        results = []
        def claim(): results.append(new.cancel_unsent('s', 'r').get('retryable'))
        threads = [threading.Thread(target=claim) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        assert results.count(True) == 1
