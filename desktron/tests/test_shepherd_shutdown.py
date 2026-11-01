import signal
import unittest
from unittest import mock

import engine


class Shutdown(unittest.TestCase):
    def test_parent_watchdog_does_not_interrupt_existing_cleanup(self):
        for cleaning in (False, True):
            with mock.patch.object(engine,'_cleanup_started',cleaning), \
                 mock.patch.dict(engine.os.environ, {}, clear=True), \
                 mock.patch.object(engine.os,'getppid',return_value=100), \
                 mock.patch.object(engine.oscompat,'watch_parent') as watch, \
                 mock.patch.object(engine.oscompat,'watch_stop_event'), \
                 mock.patch.object(engine.oscompat,'request_self_shutdown') as shutdown:
                engine._parent_death_watchdog()
                watch.call_args.args[1]()
                if cleaning: shutdown.assert_not_called()
                else: shutdown.assert_called_once_with()

    def test_native_backend_rejects_already_dead_host(self):
        with mock.patch.dict(engine.os.environ, {
                'DESKTRON_NATIVE_LAUNCH':'1', 'DESKTRON_NATIVE_HOST_PID':'100'}, clear=True), \
             mock.patch.object(engine.os, 'getppid', return_value=1), \
             mock.patch('threading.Thread') as thread:
            with self.assertRaisesRegex(RuntimeError, '孤儿后端'):
                engine._parent_death_watchdog()
        thread.assert_not_called()

    def test_cleanup_stops_existing_shepherd_without_creating_an_adapter(self):
        adapter=mock.Mock()
        with mock.patch.object(engine,'_cleanup_started',False), \
             mock.patch.object(engine,'_shepherd_adapter',adapter), \
             mock.patch.object(engine,'_shepherd') as create, \
             mock.patch('termpty.kill_all'), mock.patch('larkbot.runner.cancel_all'):
            engine._cleanup(); engine._cleanup()
        adapter.shutdown.assert_called_once()
        self.assertGreater(adapter.shutdown.call_args.kwargs['timeout'], 0)
        self.assertLessEqual(adapter.shutdown.call_args.kwargs['timeout'], 3.5)
        create.assert_not_called()
