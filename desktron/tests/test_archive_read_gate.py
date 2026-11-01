"""adapter 归档回读闸:四个宿主闸 + 判定单一源的回归。

回读请求快照会把上一次的完整模型输入连同本次读取记录带进下一次请求,逐轮自我放大
(实测单会话 270 份、63MB)。防护此前只写在 Pi 扩展里,换引擎就落空,所以这里按各宿主
真实的 payload 形态跑真脚本,而不是直接调判定函数。

Run: python3 -m pytest desktron/tests/test_archive_read_gate.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktron" / "lib"))
from memory_engine import gate  # noqa: E402

SCRIPTS = ROOT / ".claude" / "skills"
KN_READ = SCRIPTS / "knowledge" / "scripts" / "kn_read_hook.py"
BASH_GATE = SCRIPTS / "memory" / "scripts" / "claude_bash_gate.py"
CODEX_GATE = SCRIPTS / "memory" / "scripts" / "codex_gate.py"
CURSOR_GATE = SCRIPTS / "memory" / "scripts" / "cursor_gate.py"


def run_gate(script, payload):
    p = subprocess.run([sys.executable, str(script)], input=json.dumps(payload),
                       capture_output=True, text=True, encoding='utf-8')
    return p


def claude_denied(p):
    """claude 闸:deny 走 stdout JSON,exit 0。"""
    if not p.stdout.strip():
        return False
    out = json.loads(p.stdout)
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def codex_denied(p):
    """codex 闸:deny 走 stderr,exit 2。"""
    return p.returncode == 2


def cursor_denied(p):
    if not p.stdout.strip():
        return False
    return json.loads(p.stdout.splitlines()[0]).get("permission") == "deny"


class ArchiveReadGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="archive-gate-")
        ctx = Path(cls.base) / "data" / "adapter" / "coordinated" / "context" / "s1"
        (ctx / "request-contexts").mkdir(parents=True)
        (ctx / "tool-results").mkdir(parents=True)
        cls.snapshot = ctx / "request-contexts" / "live-request-context-ab12.txt"
        cls.snapshot.write_text("x" * 1000, encoding="utf-8")
        # 超阈值归档:整份读回才拦,定位读放行
        cls.big = ctx / "tool-results" / "r-big.txt"
        cls.big.write_text("y" * (gate.ARCHIVE_BULK_BYTES + 1), encoding="utf-8")
        cls.small = ctx / "tool-results" / "r-small.txt"
        cls.small.write_text("y" * 16, encoding="utf-8")
        # 指向快照目录的软链别名(此前 `cat alias/*.txt` 从这里漏过)
        cls.alias = Path(cls.base) / "alias"
        os.symlink(ctx / "request-contexts", cls.alias)
        # 同名业务目录:不带 adapter 段,不该被误伤
        cls.innocent = Path(cls.base) / "repo" / "src" / "request-contexts"
        cls.innocent.mkdir(parents=True)
        (cls.innocent / "fixture.txt").write_text("z", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    # ── claude Read ──────────────────────────────────────────────────────
    def _read(self, path, **ti):
        return run_gate(KN_READ, {"tool_name": "Read", "cwd": self.base,
                                  "tool_input": {"file_path": str(path), **ti}})

    def test_claude_read_拒绝请求快照(self):
        self.assertTrue(claude_denied(self._read(self.snapshot)))

    def test_claude_read_拒绝整份读回大归档(self):
        self.assertTrue(claude_denied(self._read(self.big)))

    def test_claude_read_放行定位读(self):
        self.assertFalse(claude_denied(self._read(self.big, offset=100, limit=20)))

    def test_claude_read_放行小归档(self):
        self.assertFalse(claude_denied(self._read(self.small)))

    def test_claude_read_不误伤同名业务目录(self):
        self.assertFalse(claude_denied(self._read(self.innocent / "fixture.txt")))

    # ── claude Bash ──────────────────────────────────────────────────────
    def _bash(self, cmd):
        return run_gate(BASH_GATE, {"tool_name": "Bash", "cwd": self.base,
                                    "tool_input": {"command": cmd}})

    def test_claude_bash_拒绝直读快照(self):
        self.assertTrue(claude_denied(self._bash(f"cat {self.snapshot}")))

    def test_claude_bash_拒绝截断读快照(self):
        # 快照任何读法都拒:head 少读几行同样把输入带回上下文
        self.assertTrue(claude_denied(self._bash(f"head -50 {self.snapshot}")))

    def test_claude_bash_拒绝裸通配快照(self):
        self.assertTrue(claude_denied(
            self._bash("cat live-request-context-*")))

    def test_claude_bash_拒绝软链别名通配(self):
        self.assertTrue(claude_denied(self._bash(f"cat {self.alias}/*.txt")))

    def test_claude_bash_拒绝重定向读入快照(self):
        self.assertTrue(claude_denied(self._bash(f"wc -l < {self.snapshot}")))

    def test_claude_bash_拒绝整份读回大归档(self):
        self.assertTrue(claude_denied(self._bash(f"cat {self.big.as_posix()}")))

    def test_claude_bash_放行截断读归档(self):
        self.assertFalse(claude_denied(self._bash(f"head -50 {self.big}")))

    def test_claude_bash_放行源码搜索(self):
        # 搜索词里出现目录名不是读取行为,不能误拦
        self.assertFalse(claude_denied(
            self._bash("rg --max-columns 200 -n 'request-contexts' src")))

    def test_claude_bash_放行归档内定位检索(self):
        self.assertFalse(claude_denied(self._bash(f"grep -n 'y' {self.big}")))

    def test_codex_read_file_放行定位读(self):
        p = run_gate(CODEX_GATE, {"tool_name": "read_file", "cwd": self.base,
                                  "tool_input": {"file_path": str(self.big),
                                                 "offset": 10, "limit": 5}})
        self.assertFalse(codex_denied(p))

    def test_codex_bash_拒绝快照通配(self):
        p = run_gate(CODEX_GATE, {"tool_name": "Bash", "cwd": self.base,
                                  "tool_input": {"command": f"cat {self.alias}/*.txt"}})
        self.assertTrue(codex_denied(p))

    def test_codex_bash_放行源码搜索(self):
        p = run_gate(CODEX_GATE, {"tool_name": "Bash", "cwd": self.base,
                                  "tool_input": {"command": "rg -n 'request-contexts' src"}})
        self.assertFalse(codex_denied(p))

    # ── cursor ───────────────────────────────────────────────────────────
    def test_cursor_read_拒绝快照(self):
        p = run_gate(CURSOR_GATE, {"hook_event_name": "beforeReadFile",
                                   "file_path": str(self.snapshot),
                                   "workspace_roots": [self.base]})
        self.assertTrue(cursor_denied(p))

    def test_cursor_shell_拒绝整份读回大归档(self):
        p = run_gate(CURSOR_GATE, {"hook_event_name": "beforeShellExecution",
                                   "command": f"cat {self.big.as_posix()}", "cwd": self.base})
        self.assertTrue(cursor_denied(p))

    def test_cursor_shell_命令落在tool_input时同样拦截(self):
        p = run_gate(CURSOR_GATE, {"hook_event_name": "beforeShellExecution",
                                   "tool_input": {"command": f"cat {self.snapshot}",
                                                  "cwd": self.base}})
        self.assertTrue(cursor_denied(p))

    def test_cursor_shell_放行源码搜索(self):
        p = run_gate(CURSOR_GATE, {"hook_event_name": "beforeShellExecution",
                                   "command": "rg -n 'request-contexts' src",
                                   "cwd": self.base})
        self.assertFalse(cursor_denied(p))


class 判定单一源(unittest.TestCase):
    def test_规则限定在adapter目录下(self):
        self.assertTrue(gate.snapshot_blocked("/x/adapter/coordinated/context/s1/request-contexts/a.txt"))
        self.assertFalse(gate.snapshot_blocked("/repo/src/request-contexts/a.txt"))

    def test_快照文件名不依赖目录(self):
        # 文件被挪走或软链到别处,仍按文件名认出来
        self.assertTrue(gate.snapshot_blocked("/tmp/live-request-context-9f.txt"))


if __name__ == "__main__":
    unittest.main()
