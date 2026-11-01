"""首次安装的自管 Node 准备与安全边界。"""
import io
import os
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
sys.argv = ["test", "/tmp/setup-runtime-frontend", "0", "0", ROOT]
from provision import setup_runtime  # noqa: E402


class 安装边界(unittest.TestCase):
    def test_解包拒绝路径穿越(self):
        with tempfile.TemporaryDirectory() as root:
            archive = os.path.join(root, "bad.tgz")
            with tarfile.open(archive, "w:gz") as tf:
                info = tarfile.TarInfo("../outside")
                info.size = 1
                tf.addfile(info, io.BytesIO(b"x"))
            with self.assertRaisesRegex(RuntimeError, "不安全"):
                setup_runtime._safe_extract(archive, os.path.join(root, "out"))

    def test_已有合格_node_直接复用(self):
        with mock.patch.object(setup_runtime.shutil, "which", return_value="/usr/bin/node"), \
             mock.patch.object(setup_runtime, "_node_ok", side_effect=lambda path: path == "/usr/bin/node"):
            self.assertEqual(setup_runtime.install_node(), "/usr/bin/node")

    def test_prepare_只接确定性运行时(self):
        launcher = open(os.path.join(ROOT, "desktron", "bin", "desktron"),
                        encoding="utf-8").read()
        self.assertIn('setup_runtime.prepare_sync()["node"]', launcher)
        self.assertIn('prepare_runtime', launcher.split('  prepare)')[1].split('    ;;')[0])
        self.assertIn('prepare_runtime', launcher.split('  install)')[1].split('    ;;')[0])
        self.assertNotIn("setup_agent", launcher)


if __name__ == "__main__":
    unittest.main()
