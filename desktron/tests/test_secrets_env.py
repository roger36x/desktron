"""凭证热加载不能破坏 Desk 已选定的运行时环境。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))

import credstore


class 凭证环境边界(unittest.TestCase):
    def test_旧版_path_不会覆盖运行时(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "env")
            with open(path, "w", encoding="utf-8") as f:
                f.write('PATH="$HOME/opt/node-current/bin:$PATH"\n')
                f.write("OPENAI_API_KEY=secret\n")
            base = {"PATH": "/managed/node:/usr/bin", "HOME": "/real-home"}
            with mock.patch.object(credstore, "SECRETS_PATH", path):
                got = credstore.secrets_env(base)
        self.assertEqual(got["PATH"], "/managed/node:/usr/bin")
        self.assertEqual(got["HOME"], "/real-home")
        self.assertEqual(got["OPENAI_API_KEY"], "secret")

    def test_desk_身份字段不能被凭证覆盖(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("DESKTRON_RUNTIME_PATH=/old/node\n")
                f.write("DESKTRON_SESSION_ID=wrong\n")
                f.write("ANTHROPIC_BASE_URL=https://example.test\n")
            base = {"DESKTRON_RUNTIME_PATH": "/managed/node",
                    "DESKTRON_SESSION_ID": "right"}
            with mock.patch.object(credstore, "SECRETS_PATH", path):
                got = credstore.secrets_env(base)
        self.assertEqual(got["DESKTRON_RUNTIME_PATH"], "/managed/node")
        self.assertEqual(got["DESKTRON_SESSION_ID"], "right")
        self.assertEqual(got["ANTHROPIC_BASE_URL"], "https://example.test")

    def test_运行时与动态链接环境不能由凭证注入(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("PYTHONPATH=/tmp/wrong-python\n")
                f.write("VIRTUAL_ENV=/tmp/wrong-venv\n")
                f.write("LD_PRELOAD=/tmp/inject.so\n")
                f.write("DYLD_INSERT_LIBRARIES=/tmp/inject.dylib\n")
                f.write("DESKTRON_UNKNOWN=wrong\n")
                f.write("OPENAI_BASE_URL=https://example.test/v1\n")
            base = {"PYTHONPATH": "/managed/python", "DESKTRON_UNKNOWN": "right"}
            with mock.patch.object(credstore, "SECRETS_PATH", path):
                got = credstore.secrets_env(base)
        self.assertEqual(got["PYTHONPATH"], "/managed/python")
        self.assertEqual(got["DESKTRON_UNKNOWN"], "right")
        self.assertNotIn("VIRTUAL_ENV", got)
        self.assertNotIn("LD_PRELOAD", got)
        self.assertNotIn("DYLD_INSERT_LIBRARIES", got)
        self.assertEqual(got["OPENAI_BASE_URL"], "https://example.test/v1")


if __name__ == "__main__":
    unittest.main()
