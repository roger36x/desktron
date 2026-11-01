"""resume 的 cwd 必须跟着 transcript 落盘位置走(不是跟着归属域)。

回归的是「导入会话」引入的缺陷:claude / cursor 的 transcript 目录名是 cwd 的编码,
会话导入工作流后归属变了、落盘没动,若按域目录 resume 就报
「No conversation found with session ID」。
"""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
sys.argv = ["test", "/tmp/trcwd-frontend", "0", "0", "/tmp/trcwd-repo"]
import term  # noqa: E402


class TranscriptCwd(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="trcwd-home-")
        self.repo = "/Users/somebody/work/proj-x"     # 会话真正落盘的目录
        self.domain = "/Users/somebody/workspace/domains/wf-a"   # 导入后的归属域目录
        self._dirs = term._workspace_dirs
        term._workspace_dirs = lambda: [self.domain, self.repo]
        self._exp = os.path.expanduser
        os.path.expanduser = lambda p: p.replace("~", self.home, 1) if p.startswith("~") else self._exp(p)

    def tearDown(self):
        os.path.expanduser = self._exp
        term._workspace_dirs = self._dirs
        shutil.rmtree(self.home, ignore_errors=True)

    def _put_claude(self, sid, wd):
        d = os.path.join(self.home, ".claude", "projects", term._claude_proj_enc(wd))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, sid + ".jsonl"), "w", encoding="utf-8") as f:
            f.write("{}\n")

    def test_finds_dir_holding_transcript(self):
        self._put_claude("s-1", self.repo)
        self.assertEqual(term._transcript_cwd("claude", "s-1"), self.repo)   # 不是归属域

    def test_empty_when_absent(self):
        self.assertEqual(term._transcript_cwd("claude", "missing"), "")      # 调用方回落域目录

    def test_rollout_engines_are_cwd_independent(self):
        # codex 的 rollout 是全局 glob,与 cwd 无关,不该被本机制干预
        self._put_claude("s-2", self.repo)
        self.assertEqual(term._transcript_cwd("codex", "s-2"), "")

    # test_wired_into_term_open 随 TUI 撤(2026-08-20):它锁的是 _term_open 里
    # 「resume 的 cwd 跟着 transcript 落盘位置走」那两行。GUI 这条路不 resume 进程,
    # 记录由适配器按 id 全局定位(ClaudeAdapter._transcript_path),不再依赖 cwd 编码。
    # _transcript_cwd 本身还在用(会话列表标来源),上面那几条仍然锁着它。


if __name__ == "__main__":
    unittest.main()
