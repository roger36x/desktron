"""Desk 角色定义只保留引擎无关正身目录 `.desk/agents`。"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))


class RepoLayout(unittest.TestCase):
    def test_正身是普通文件(self):
        p = os.path.join(ROOT, ".desk", "agents", "ops.md")
        self.assertTrue(os.path.isfile(p) and not os.path.islink(p), p)

    def test_装配清单带上正身目录(self):
        src = open(os.path.join(ROOT, "desktron", "bin", "desktron"), encoding="utf-8").read()
        import re
        m = re.search(r"for asset in ([^;]+); do", src)
        self.assertIsNotNone(m)
        self.assertIn(".desk", m.group(1).split(), "工作区拿不到 .desk,正身就断了")
        self.assertNotIn(".opencode", m.group(1).split())

    def test_工作流运行目录不进入提交和发行物(self):
        ignored = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read().splitlines()
        payload = open(os.path.join(ROOT, "scripts", "payload-excludes.txt"),
                       encoding="utf-8").read().splitlines()
        self.assertIn("/domains/", ignored)
        self.assertIn("/domains", payload)


class AdapterReads(unittest.TestCase):
    def _ad(self, ws):
        import engine_adapters as ea
        ad = ea.CliAdapter.__new__(ea.CliAdapter)
        ad.workspace = ws
        return ad

    def test_优先读正身目录(self):
        with tempfile.TemporaryDirectory() as ws:
            os.makedirs(os.path.join(ws, ".desk", "agents"))
            open(os.path.join(ws, ".desk", "agents", "x.md"), "w", encoding='utf-8').write("正身")
            ad = self._ad(ws)
            self.assertEqual(ad.agent_body("x"), "正身")
            self.assertEqual([a["name"] for a in ad.agents()], ["x"])


if __name__ == "__main__":
    unittest.main()
