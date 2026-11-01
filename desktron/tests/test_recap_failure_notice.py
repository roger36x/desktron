"""Repeated recap failures surface in the session strip instead of only in a state file."""
from pathlib import Path
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'lib'))
ROOT = Path(__file__).resolve().parents[2]
_argv = sys.argv
sys.argv = ['ui_server.py', str(ROOT/'frontend'), '7999', '7998', str(ROOT)]
try:
    from engine_adapters import recap as recap_mod
    from engine_adapters.recap import RecapMixin
finally:
    sys.argv = _argv


class _Adapter(RecapMixin):
    name = 'shepherd'

    def __init__(self, root):
        self.dir = root
        self._recap_lock = threading.RLock()


class RecapFailureNotice(unittest.TestCase):
    def test_notice_appears_at_threshold_and_clears_on_success(self):
        with tempfile.TemporaryDirectory() as root:
            ad = _Adapter(root)
            self.assertIsNone(ad.recap_notice())
            ad._recap_record(False, error=RuntimeError('Claude SessionStore 批次过大'))
            self.assertIsNone(ad.recap_notice())
            ad._recap_record(False, error=RuntimeError('Claude SessionStore 批次过大'))
            frame = ad.recap_notice()
            self.assertEqual(frame['state'], 'warning')
            self.assertEqual(frame['stage'], 'recap')
            self.assertIn(str(recap_mod.RECAP_ALERT_FAILS), frame['label'])
            self.assertTrue(any('批次过大' in line for line in frame['details']))
            # Being pre-empted by a new turn is not a failure and must not clear or raise it.
            ad._recap_record(False, cancelled=True)
            self.assertIsNotNone(ad.recap_notice())
            ad._recap_record(True, wrote=True)
            self.assertIsNone(ad.recap_notice())

    def test_corrupt_state_does_not_raise(self):
        with tempfile.TemporaryDirectory() as root:
            ad = _Adapter(root)
            for body in ('not json', '[]', json.dumps({'fails': 'x'})):
                with open(os.path.join(root, 'recap-state.json'), 'w', encoding='utf-8') as f:
                    f.write(body)
                self.assertIsNone(ad.recap_notice())

    def test_config_and_frontend_wiring(self):
        contract = (ROOT/'desktron/lib/ui_contract.py').read_text(encoding='utf-8')
        self.assertIn('ad.recap_notice()', contract)
        core = (ROOT/'frontend/app-chat-core.js').read_text(encoding='utf-8')
        # The strip must clear itself once /config no longer reports the failure.
        self.assertIn("['auth', 'recap'].includes(v.envCurrent.stage)", core)


if __name__ == '__main__':
    unittest.main()
