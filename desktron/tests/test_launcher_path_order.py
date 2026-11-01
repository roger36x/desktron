"""Apple Silicon 上 /usr/local/bin 只作兜底，不能遮住系统工具。

/usr/local 是 Intel Homebrew 与旧安装器的位置。系统没有 Rosetta 时，那里的 x86_64 程序
启动即 EBADARCH；Node 的 spawn 不会像 Python 那样换下一个 PATH 候选，而是直接抛错。
2026-10-06 实见：macOS 27 上 /usr/local/bin/git 让 pi-subagents 启动崩溃，Pi 回合全部失败。
"""
import os
import re
import subprocess
import unittest
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[2] / 'desktron/bin/desktron'


def launcher_path(inherited, system):
    source = LAUNCHER.read_text(encoding='utf-8')
    block = re.search(r'^_DESK_LOCAL_HEAD=.*?^export PATH=[^\n]*_DESK_LOCAL_TAIL[^\n]*$',
                      source, re.S | re.M).group(0)
    # 用桩替换 uname，让 Intel 与 Linux 分支也能在任意构建机上验证。
    stub = 'uname() { case "$1" in -s) echo %s;; -m) echo %s;; esac; }\n' % tuple(system.split())
    script = stub + block + '\nprintf %s "$PATH"'
    env = {'HOME': '/home/desk', 'PATH': inherited}
    done = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True,
                          timeout=10, check=True, encoding='utf-8')
    return done.stdout.split(':')


# The bash launcher only runs on macOS/Linux; Windows starts through desk_launcher.py.
# On GitHub Windows runners the stripped-env `bash -c` call exits 1 (run 37659906654).
@unittest.skipIf(os.name == 'nt', 'POSIX bash launcher; Windows uses desk_launcher.py')
class LauncherPathOrder(unittest.TestCase):
    def test_apple_silicon_moves_local_bin_after_system_dirs(self):
        path = launcher_path('/usr/local/bin:/usr/bin:/bin:/usr/local/bin', 'Darwin arm64')
        self.assertEqual(path, ['/home/desk/.local/bin', '/opt/homebrew/bin',
                                '/usr/bin', '/bin', '/usr/local/bin'])

    def test_gui_minimal_path_still_gains_local_bin_as_fallback(self):
        path = launcher_path('/usr/bin:/bin:/usr/sbin:/sbin', 'Darwin arm64')
        self.assertLess(path.index('/usr/bin'), path.index('/usr/local/bin'))
        self.assertEqual(path[-1], '/usr/local/bin')

    def test_intel_mac_and_linux_keep_local_bin_first(self):
        for system in ('Darwin x86_64', 'Linux x86_64'):
            path = launcher_path('/usr/bin:/bin', system)
            self.assertLess(path.index('/usr/local/bin'), path.index('/usr/bin'), system)


if __name__ == '__main__':
    unittest.main()
