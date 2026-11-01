"""term branch metadata regression tests.

Run: python3 -m unittest desktron/tests/test_term_branch.py
"""
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


_BASE = tempfile.mkdtemp(prefix="term-branch-test-")
_REPO_ROOT = os.path.join(_BASE, "repo_root")
_FRONTEND = os.path.join(_BASE, "frontend")
_DATA = os.path.join(_BASE, "data")
for _d in (_REPO_ROOT, _FRONTEND, _DATA):
    os.makedirs(_d, exist_ok=True)

sys.argv = ["test", _FRONTEND, "0", "0", _REPO_ROOT]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
atexit.register(lambda: shutil.rmtree(_BASE, ignore_errors=True))

# Other tests may leave a stubbed domains module in sys.modules during unittest discover.
if "domains" in sys.modules and not hasattr(sys.modules["domains"], "_domain_for_cwd"):
    del sys.modules["domains"]

_termpty = types.ModuleType("termpty")
_termpty.pty_open = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("not used"))
_termpty.pty_kill = lambda *a, **k: None
_termpty.pty_input = lambda *a, **k: None
sys.modules["termpty"] = _termpty

import term  # noqa: E402


class BranchForCwd(unittest.TestCase):
    def tearDown(self):
        shutil.rmtree(getattr(self, "repo", ""), ignore_errors=True)

    def test_returns_current_git_branch_for_cwd(self):
        self.repo = tempfile.mkdtemp(prefix="term-git-repo-")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "checkout", "-q", "-b", "feat/branch-label"], check=True)

        self.assertEqual(term._git_branch_for_cwd(self.repo), "feat/branch-label")

    def test_returns_empty_for_non_git_cwd(self):
        self.repo = tempfile.mkdtemp(prefix="term-non-git-")

        self.assertEqual(term._git_branch_for_cwd(self.repo), "")

    def test_branch_label_only_for_isolated_sessions(self):
        # 现行语义:分支徽标只标「隔离会话」(cwd 不在域主目录);主 checkout 会话不标(全体同分支是噪音)
        self.repo = tempfile.mkdtemp(prefix="term-git-repo-")
        base = os.path.join(self.repo, "base")
        wt = os.path.join(self.repo, "wt")
        for d in (base, wt):
            os.makedirs(d)
            subprocess.run(["git", "init", "-q", d], check=True)
        subprocess.run(["git", "-C", wt, "checkout", "-q", "-b", "feat/live-row"], check=True)
        sess = {"id": "t-branch", "engine": "shell", "domain": "repo"}
        with mock.patch.object(term, "_domain_dir", return_value=base):
            self.assertEqual(term._session_branch_label(sess, "repo", {"cwd": wt}), "feat/live-row")
            self.assertEqual(term._session_branch_label(sess, "repo", {"cwd": base}), "")   # 主目录:不标
            self.assertEqual(term._session_branch_label(sess, "repo", None), "")            # 无落盘 cwd → 主目录

    def test_term_list_omits_branch_for_main_checkout_session(self):
        self.repo = tempfile.mkdtemp(prefix="term-git-repo-")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "checkout", "-q", "-b", "feat/live-row"], check=True)
        sess = {"id": "t-branch", "engine": "shell", "domain": "repo",
                "title": "shell", "created": 123, "task": False, "codex_uuid": None}
        with term._TERMS_LOCK:
            old_terms = dict(term._TERMS)
            term._TERMS.clear()
            term._TERMS[sess["id"]] = sess
        try:
            with mock.patch.object(term, "_live_map", return_value={"t-branch": {"last_out": 0}}), \
                 mock.patch.object(term, "_domain_dir", return_value=self.repo):
                rows = term._term_list()
        finally:
            with term._TERMS_LOCK:
                term._TERMS.clear()
                term._TERMS.update(old_terms)

        self.assertNotIn("branch", rows[0])   # 主 checkout 上的会话:有意不标


if __name__ == "__main__":
    unittest.main()
