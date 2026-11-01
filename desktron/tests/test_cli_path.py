"""按 PATH 找命令时跳过无法直接启动的残缺安装(Shepherd「无法确认协议能力」的根因)。"""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktron/lib"))
import cli_path


class Which(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.broken = Path(self.tmp.name, "broken"); self.broken.mkdir()
        self.good = Path(self.tmp.name, "good"); self.good.mkdir()

    def _tool(self, directory, body):
        path = directory / ("claude.exe" if os.name == 'nt' else "claude")
        path.write_bytes(body)
        path.chmod(0o755)
        return str(path)

    def test_skips_executable_text_without_shebang(self):
        # npm 跳过原生可选依赖时留下的 bin/claude.exe 就是这种文件。
        self._tool(self.broken, b'echo "Error: claude native binary not installed." >&2\nexit 1\n')
        good = self._tool(self.good, b'MZ' + b'\0' * 16 if os.name == 'nt' else b"#!/bin/sh\necho ok\n")
        search = os.pathsep.join([str(self.broken), str(self.good)])
        self.assertEqual(cli_path.which("claude", path=search), good)

    def test_accepts_native_binary_header(self):
        good = self._tool(self.good, (b'MZ' if os.name == 'nt' else b"\xcf\xfa\xed\xfe") + b"\0" * 16)
        self.assertEqual(cli_path.which("claude", path=str(self.good)), good)

    def test_returns_none_when_only_broken_candidates(self):
        self._tool(self.broken, b"exit 1\n")
        self.assertIsNone(cli_path.which("claude", path=str(self.broken)))

    def test_shepherd_and_memory_use_the_checked_lookup(self):
        for rel in ("shepherd/claude_connector.py", "shepherd/unified_runners.py",
                    "memory_engine/project_memory.py"):
            source = (ROOT / "desktron/lib" / rel).read_text(encoding="utf-8")
            self.assertNotIn("shutil.which('claude')", source, rel)
            self.assertNotIn('shutil.which("claude")', source, rel)
            self.assertIn("cli_path.which(", source, rel)


if __name__ == "__main__":
    unittest.main()
