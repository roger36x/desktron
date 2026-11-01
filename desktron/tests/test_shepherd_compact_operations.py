import tempfile

from shepherd.compact_operations import CompactOperations, CompactOperationConflict


def test_claim_is_durable_and_idempotent_across_boots():
    with tempfile.TemporaryDirectory() as root:
        first=CompactOperations(root,'boot-a')
        row,fresh=first.claim('adp_one','compact_1',before_seq=7)
        assert fresh and row['running'] and row['before_seq']==7
        again=CompactOperations(root,'boot-b')
        same,fresh=again.claim('adp_one','compact_1',before_seq=99)
        assert not fresh and same['before_seq']==7 and same['running']
        try:
            again.claim('adp_one','compact_2',before_seq=7)
        except CompactOperationConflict:
            pass
        else:
            raise AssertionError('a second compaction crossed the durable fence')


def test_cancel_unaccepted_fences_late_post():
    with tempfile.TemporaryDirectory() as root:
        ops=CompactOperations(root,'boot-a')
        cancelled=ops.cancel_unaccepted('adp_one','compact_late')
        assert cancelled['result']=='not_started' and not cancelled['running']
        same,fresh=ops.claim('adp_one','compact_late',before_seq=2)
        assert not fresh and same['result']=='not_started'


def test_verifying_survives_finish_failure_for_later_recovery():
    with tempfile.TemporaryDirectory() as root:
        ops=CompactOperations(root,'boot-a')
        ops.claim('adp_one','compact_verify',before_seq=2)
        row=ops.verifying('adp_one','compact_verify')
        assert row['running'] and row['status']=='verifying'
        again=CompactOperations(root,'boot-b').get('adp_one','compact_verify')
        assert again['running'] and again['status']=='verifying'


def test_terminal_identity_survives_and_never_reopens():
    with tempfile.TemporaryDirectory() as root:
        ops=CompactOperations(root,'boot-a')
        ops.claim('adp_one','compact_1',before_seq=2)
        done=ops.finish('adp_one','compact_1',result='success')
        assert not done['running'] and done['result']=='success'
        same,fresh=CompactOperations(root,'boot-b').claim(
            'adp_one','compact_1',before_seq=100)
        assert not fresh and same['result']=='success'
