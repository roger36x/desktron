import json
import signal
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import systemd_restart as restart


class SystemdRestart(unittest.TestCase):
    def test_identity_mismatch_never_signals_and_writes_rejection(self):
        with tempfile.TemporaryDirectory() as root:
            result=str(Path(root)/'result.json')
            with mock.patch.object(restart,'probe',return_value={'boot_id':'new','pid':42}), \
                 mock.patch.object(restart,'start_token',return_value='same'), \
                 mock.patch.object(restart.os,'kill') as kill:
                self.assertEqual(restart.stop('old',42,'same',result_path=result,operation_id='op'),3)
            kill.assert_not_called();self.assertEqual(json.loads(Path(result).read_text())['state'],'rejected')

    def test_pid_reuse_never_signals(self):
        with mock.patch.object(restart,'probe',return_value={'boot_id':'boot','pid':42}), \
             mock.patch.object(restart,'start_token',return_value='new'), \
             mock.patch.object(restart.os,'kill') as kill:
            self.assertEqual(restart.stop('boot',42,'old'),3)
        kill.assert_not_called()

    def test_matching_process_gets_term_then_bounded_kill_and_receipt(self):
        tokens=iter(['same','same','same'])
        with tempfile.TemporaryDirectory() as root:
            result=str(Path(root)/'result.json')
            with mock.patch.object(restart,'probe',return_value={'boot_id':'boot','pid':42}), \
                 mock.patch.object(restart,'start_token',side_effect=lambda _pid:next(tokens)), \
                 mock.patch.object(restart.os,'kill') as kill, \
                 mock.patch.object(restart.time,'monotonic',side_effect=[0,5]), \
                 mock.patch.object(restart.time,'sleep'):
                self.assertEqual(restart.stop('boot',42,'same',timeout=1,
                    result_path=result,operation_id='op'),0)
            self.assertEqual(json.loads(Path(result).read_text())['state'],'old_stopped')
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(42,signal.SIGTERM),(42,signal.SIGKILL)])


if __name__=='__main__':unittest.main()
