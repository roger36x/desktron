import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
LIB = os.path.join(ROOT, 'desktron', 'lib')
sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ['ui_server.py', os.path.join(ROOT, 'frontend'), '7999', '7998', ROOT]
try:
    import engine_adapters as ea
finally:
    sys.argv = _argv


class Bus:
    def publish(self, _ev):
        pass


class Proc:
    def __init__(self):
        self.killed = False
    def kill(self):
        self.killed = True


class TurnWatchLongTool(unittest.TestCase):
    def make_adapter(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return ea.PiAdapter(tmp.name, tmp.name, Bus())

    def test_turn_watch_defers_kill_while_tool_is_running(self):
        proc = Proc()
        calls = {'defer': 0}
        def should_defer():
            calls['defer'] += 1
            return calls['defer'] == 1
        class Done:
            def wait(self, _step):
                return False
        watch = ea._TurnWatch(30, proc.kill, should_defer=should_defer)
        watch.done = Done()
        watch.at = 0
        with mock.patch('engine_adapters.bus.time.time', side_effect=[31, 31, 62]):
            watch._loop()
        self.assertEqual(calls['defer'], 2)
        self.assertTrue(proc.killed)

    def test_turn_activity_keeps_running_tool_observable_during_no_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            ad = ea.PiAdapter(tmp, tmp, Bus())
            sid = ad.create_session()['id']
            ctx = {'sid': sid, '_line_at': time.time() - ad.STALL_SECONDS - 5,
                   'parts': {'tool_1': {'type': 'tool', 'state': {'status': 'running'}}}}
            activity = ad.turn_activity(sid, ctx)
            self.assertEqual(activity['kind'], 'no_progress')
            self.assertEqual(activity['running_tools'], 1)

    def test_turn_watch_records_structured_watchdog_timeout_before_kill(self):
        ad = self.make_adapter()
        proc = Proc()
        ctx = {'parts': {}}
        captured = {}
        class Watch:
            def __init__(self, timeout, on_dead, should_defer=None):
                captured['timeout'] = timeout
                captured['on_dead'] = on_dead
                captured['should_defer'] = should_defer
            def start(self):
                return self
        with mock.patch('engine_adapters.cli_turn._TurnWatch', Watch):
            ad._turn_watch(ctx, proc)
        self.assertFalse(captured['should_defer']())
        captured['on_dead']()
        self.assertTrue(proc.killed)
        self.assertEqual(ctx['engine_failure']['code'], 'WATCHDOG_TIMEOUT')
        self.assertNotIn('exit', ctx['engine_failure']['message'].lower())

    def test_startup_warning_is_not_appended_to_transport_failure(self):
        ad = self.make_adapter()
        ctx = {}
        warning = "Warning: No project session found with id 'ordinary'; creating a new session with that id."
        self.assertEqual(ad._process_error_tail(ctx, warning + "\nreal failure"), 'real failure')
        self.assertEqual(ctx['process_warnings'], [warning])
        self.assertEqual(ad._process_error_tail({}, warning), '')


if __name__ == '__main__':
    unittest.main()
