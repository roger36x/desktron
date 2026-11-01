"""PTY redraw regression tests."""
import os
import sys
import unittest
from unittest import mock


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

# 两条串跑防护:
# 1) 别的测试(test_term_branch/test_session_handoff)会在 sys.modules 里留 termpty 桩,先清掉换真模块;
# 2) 真 termpty 导入时会装全局 SIGCHLD 收割器,在共享 pytest 进程里会抢走后续测试
#    subprocess.run 的 waitpid(worktree_ops 的 git 命令结果被污染),导入期按住 signal.signal。
import signal as _signal  # noqa: E402
from unittest import mock as _mock  # noqa: E402

sys.modules.pop("termpty", None)
with _mock.patch.object(_signal, "signal"):
    import termpty  # noqa: E402


class PtyRedrawTest(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'POSIX waitpid/WNOHANG；Windows ConPTY 使用读线程')
    def test_reaper_only_waits_for_registered_pty_children(self):
        sessions = {
            "one": {"pid": 101},
            "two": {"pid": 202},
        }
        with mock.patch.object(termpty, "_PTY", sessions), \
             mock.patch.object(termpty, "_EXIT", {}), \
             mock.patch.object(termpty.os, "waitpid", side_effect=[(101, 0), (0, 0)]) as waitpid:
            termpty._reap_children()
            self.assertEqual(termpty._EXIT, {101: 0})
        self.assertEqual(waitpid.call_args_list, [
            mock.call(101, os.WNOHANG),
            mock.call(202, os.WNOHANG),
        ])
        self.assertNotIn(mock.call(-1, os.WNOHANG), waitpid.call_args_list)

    def test_redraw_jiggles_one_row_then_restores_geometry(self):
        with mock.patch.object(termpty, "pty_resize") as resize, \
             mock.patch.object(termpty.time, "sleep") as sleep:
            termpty.pty_redraw("session", 120, 40)

        self.assertEqual(resize.call_args_list, [mock.call("session", 120, 39), mock.call("session", 120, 40)])
        sleep.assert_called_once_with(0.06)

    def test_redraw_never_uses_zero_rows(self):
        with mock.patch.object(termpty, "pty_resize") as resize, \
             mock.patch.object(termpty.time, "sleep"):
            termpty.pty_redraw("session", 80, 1)

        self.assertEqual(resize.call_args_list, [mock.call("session", 80, 1), mock.call("session", 80, 1)])


if __name__ == "__main__":
    unittest.main()
