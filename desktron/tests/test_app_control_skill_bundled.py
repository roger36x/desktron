"""App Control 的使用说明必须与运行时一起随 Desk 发行。"""
import os
import sys
import unittest
from unittest import mock


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SKILL = os.path.join(ROOT, ".claude", "skills", "app-control", "SKILL.md")


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class AppControl随发行走(unittest.TestCase):
    def test_skill在仓库内(self):
        self.assertTrue(os.path.isfile(SKILL), "运行时在、Skill 不在时，新用户不会知道如何使用")

    def test_gitignore放行(self):
        lines = _read(".gitignore").splitlines()
        self.assertIn(".claude/skills/*", lines)
        self.assertIn("!.claude/skills/app-control/", lines)

    def test_不被发行包排除(self):
        excludes = _read("scripts/payload-excludes.txt").splitlines()
        self.assertNotIn("/.claude", excludes)
        self.assertNotIn(".claude/skills", excludes)

    def test_codex起手索引能枚举(self):
        libp = os.path.join(ROOT, "desktron", "lib")
        if libp not in sys.path:
            sys.path.insert(0, libp)
        old_argv = sys.argv
        sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
        try:
            import domains
        finally:
            sys.argv = old_argv
        with mock.patch.object(domains, "REPO_ROOT", ROOT):
            self.assertIn("`app-control`", domains._skill_index())


if __name__ == "__main__":
    unittest.main()
