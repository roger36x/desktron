"""表达规则只保留每回合注入的 prompts/style.md。"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class StylePromptBundled(unittest.TestCase):
    def test_style在注入路径上(self):
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "prompts", "style.md")))
        src = _read("desktron/lib/engine_adapters/cli_store.py")
        self.assertIn('"prompts", "style.md"', src)

    def test_prompts在装配清单里(self):
        src = _read("desktron/bin/desktron")
        match = re.search(r"for asset in ([^;]+); do", src)
        self.assertIsNotNone(match)
        self.assertIn("prompts", match.group(1).split())

    def test_style是唯一写作规则源(self):
        self.assertFalse(os.path.exists(os.path.join(
            ROOT, ".claude", "skills", "writing-voice")))
        self.assertNotIn("writing-voice", _read("prompts/style.md"))
        self.assertNotIn("!.claude/skills/writing-voice/", _read(".gitignore").splitlines())

    def test_style不被发行包排除(self):
        excludes = _read("scripts/payload-excludes.txt").splitlines()
        self.assertNotIn("/prompts", excludes)


if __name__ == "__main__":
    unittest.main()
