"""探活版本标识：正式发行与开发态 payload 都不能返回空值或混淆语义。"""
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))

old_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import ui_remote_gate  # noqa: E402
finally:
    sys.argv = old_argv


class RuntimeRev(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.expand = mock.patch.object(
            ui_remote_gate.os.path,
            "expanduser",
            side_effect=lambda path: os.path.join(self.tmp.name, os.path.basename(path)),
        )
        self.expand.start()
        self.addCleanup(self.expand.stop)

    def write(self, name, value):
        with open(os.path.join(self.tmp.name, name), "w", encoding="utf-8") as f:
            f.write(value)

    def test_正式发行提交号优先(self):
        self.write(".update-rev", "a" * 40 + "\n")
        self.write(".payload-md5", "b" * 32 + "\n")
        self.assertEqual(ui_remote_gate._runtime_rev(), "a" * 40)

    def test_开发同步回退到带前缀的_payload_指纹(self):
        self.write(".payload-md5", "ABCDEF0123456789ABCDEF0123456789\n")
        self.assertEqual(
            ui_remote_gate._runtime_rev(),
            "payload:abcdef0123456789abcdef0123456789",
        )

    def test_损坏的指纹不冒充版本(self):
        self.write(".payload-md5", "not-a-fingerprint\n")
        self.assertEqual(ui_remote_gate._runtime_rev(), "")


if __name__ == "__main__":
    unittest.main()
