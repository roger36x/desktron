"""网页搜索步骤的 URL 必须作为一个完整宾语显示。"""
import json
import os
import re
import shutil
import subprocess
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = open(os.path.join(ROOT, "frontend", "app-chat-tools.js"), encoding="utf-8").read()


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class WebSearchTarget(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        start = SRC.find("function hsGlyphWide(")
        match = re.search(r"\nfunction hsToolObj\(part\)[\s\S]*?\n}\n", SRC)
        assert start >= 0 and match, "工具宾语格式化函数不见了"
        cls.code = SRC[start:match.end()]

    def _obj(self, pattern):
        inp = {"pattern": pattern, "description": "搜索 " + pattern}
        js = ("function hsToolGist(){ return ''; }\n" + self.code
              + "\nconsole.log(JSON.stringify(hsToolObj({tool:'websearch',state:{input:"
              + json.dumps(inp, ensure_ascii=False) + "}})));" )
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_https_从协议开始整体着色(self):
        got = self._obj("https://raw.githubusercontent.com/primer/theme.js")
        self.assertTrue(got["t"].startswith("https://"))
        self.assertFalse(got["pat"])
        self.assertNotIn("搜索 ", got["t"])

    def test_site_操作符也属于完整定位串(self):
        got = self._obj("site:github.com/primer/github-vscode-theme")
        self.assertTrue(got["t"].startswith("site:github.com/"))
        self.assertFalse(got["pat"])

    def test_site_定位符加自然语言时分开渲染(self):
        got = self._obj("site:github.com 中文主题")
        self.assertEqual(got["t"], "site:github.com 中文主题")
        self.assertTrue(got["webq"])
        self.assertFalse(got["pat"])
        self.assertFalse(got["code"])

    def test_普通搜索词仍按检索式灰显(self):
        got = self._obj("GitHub Monaco dark light theme")
        self.assertEqual(got["t"], "GitHub Monaco dark light theme")
        self.assertTrue(got["pat"])


if __name__ == "__main__":
    unittest.main()
