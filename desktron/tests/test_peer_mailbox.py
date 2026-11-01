"""跨会话问询 mailbox：独立于两边会话锁的持久结果通道。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import peer_mailbox


class Mailbox(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.d.cleanup()

    def test_request_lifecycle_and_atomic_claim(self):
        row = peer_mailbox.create("from", "to", self.d.name)
        rid = row["id"]
        self.assertEqual(row["status"], "queued")
        _row, accepted = peer_mailbox.accept(rid, "to", self.d.name)
        self.assertTrue(accepted)
        _row, accepted_again = peer_mailbox.accept(rid, "to", self.d.name)
        self.assertFalse(accepted_again)
        peer_mailbox.update(rid, "running", data_home=self.d.name)
        peer_mailbox.update(rid, "replied", reply="done", data_home=self.d.name)
        got, claimed = peer_mailbox.claim_result(rid, "consumed", self.d.name)
        self.assertTrue(claimed)
        self.assertEqual((got["status"], got["result_status"], got["reply"]),
                         ("consumed", "replied", "done"))
        _got, claimed_again = peer_mailbox.claim_result(rid, "delivering", self.d.name)
        self.assertFalse(claimed_again)

    def test_late_reply_replaces_caller_timeout(self):
        row = peer_mailbox.create("from", "to", self.d.name)
        rid = row["id"]
        peer_mailbox.update(rid, "timeout", data_home=self.d.name)
        peer_mailbox.update(rid, "replied", reply="late", data_home=self.d.name)
        self.assertEqual(peer_mailbox.get(rid, self.d.name)["reply"], "late")

    def test_claimed_result_cannot_be_reopened_by_duplicate_finish(self):
        row = peer_mailbox.create("from", "to", self.d.name)
        rid = row["id"]
        peer_mailbox.update(rid, "replied", reply="first", data_home=self.d.name)
        peer_mailbox.claim_result(rid, "consumed", self.d.name)
        peer_mailbox.update(rid, "failed", error="late duplicate", data_home=self.d.name)
        got = peer_mailbox.get(rid, self.d.name)
        self.assertEqual((got["status"], got["reply"]), ("consumed", "first"))

    def test_first_terminal_failure_reason_is_not_overwritten_by_worker_unwind(self):
        row = peer_mailbox.create("from", "to", self.d.name)
        rid = row["id"]
        peer_mailbox.update(rid, "failed", error="用户新消息已取消", data_home=self.d.name)
        peer_mailbox.update(rid, "failed", error="generic worker error", data_home=self.d.name)
        self.assertEqual(peer_mailbox.get(rid, self.d.name)["error"], "用户新消息已取消")

    def test_failed_result_can_be_claimed_and_delivery_retried(self):
        row = peer_mailbox.create("from", "to", self.d.name)
        rid = row["id"]
        peer_mailbox.update(rid, "failed", error="boom", data_home=self.d.name)
        got, claimed = peer_mailbox.claim_result(rid, "delivering", self.d.name)
        self.assertTrue(claimed)
        self.assertEqual(got["result_status"], "failed")
        peer_mailbox.release_delivery(rid, "offline", self.d.name)
        self.assertEqual(peer_mailbox.get(rid, self.d.name)["status"], "failed")

    def test_startup_recovery_fails_accepted_sidecars_and_releases_old_delivery(self):
        accepted = peer_mailbox.create("from", "to", self.d.name)
        peer_mailbox.accept(accepted["id"], "to", self.d.name)
        peer_mailbox.update(accepted["id"], "running", data_home=self.d.name)
        delivering = peer_mailbox.create("from", "to", self.d.name)
        peer_mailbox.update(delivering["id"], "replied", reply="late", data_home=self.d.name)
        peer_mailbox.claim_result(delivering["id"], "delivering", self.d.name)

        self.assertEqual(peer_mailbox.recover_abandoned(self.d.name), 2)
        failed = peer_mailbox.get(accepted["id"], self.d.name)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("重新发起", failed["error"])
        released = peer_mailbox.get(delivering["id"], self.d.name)
        self.assertEqual((released["status"], released["reply"]), ("replied", "late"))

    def test_invalid_id_never_escapes_mailbox(self):
        with self.assertRaises(ValueError):
            peer_mailbox.update("../outside", "running", data_home=self.d.name)

    def test_request_cannot_be_dispatched_to_another_session(self):
        row = peer_mailbox.create("from", "to", self.d.name)
        _got, accepted = peer_mailbox.accept(row["id"], "other", self.d.name)
        self.assertFalse(accepted)

    def test_terminal_request_cannot_be_dispatched_late(self):
        row = peer_mailbox.create("from", "to", self.d.name)
        peer_mailbox.update(row["id"], "failed", error="send failed",
                            data_home=self.d.name)
        _got, accepted = peer_mailbox.accept(row["id"], "to", self.d.name)
        self.assertFalse(accepted)


if __name__ == "__main__":
    unittest.main()
