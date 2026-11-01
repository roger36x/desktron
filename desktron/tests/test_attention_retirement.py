"""文件活动热区已随活动驱动的记忆晋升架构一并退役。"""
import json
import os
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class AttentionRetirementContract(unittest.TestCase):
    def _read(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_生产者与派生器保持删除(self):
        self.assertFalse(os.path.exists(os.path.join(
            ROOT, ".claude", "skills", "memory", "scripts", "mem_attention_hook.py")))
        self.assertFalse(os.path.exists(os.path.join(
            ROOT, "desktron", "lib", "engines", "usage.py")))
        with open(os.path.join(ROOT, ".claude", "settings.json"), encoding="utf-8") as f:
            settings = json.load(f)
        self.assertNotIn("attention", json.dumps(settings).lower())

    def test_运行时与grounding不再接线(self):
        rels = (
            "desktron/lib/domains.py",
            "desktron/lib/grounding/builder.py",
            "desktron/lib/engine_adapters/cli_turn.py",
            "desktron/lib/engines/base.py",
            "desktron/lib/engines/codex.py",
        )
        source = "\n".join(self._read(rel) for rel in rels)
        for retired in ("_attention_render", "DESKTRON_ATTENTION_DIR", "usage_derive"):
            self.assertNotIn(retired, source)

    def test_旧数据继续被隔离(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            self.assertIn(".attention/", f.read())
        with open(os.path.join(ROOT, "scripts", "payload-excludes.txt"), encoding="utf-8") as f:
            self.assertIn(".attention", f.read())


if __name__ == "__main__":
    unittest.main()
