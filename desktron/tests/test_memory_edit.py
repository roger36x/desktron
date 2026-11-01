"""记忆就地编辑:路径纪律 + 免闸层直改回退。
2026-08-21 起编辑不再改 confidence——顶档 corroborated 按 source 的独立来源数推导,
改正文不产生新来源。"""
import os
import json
import sys
import subprocess
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "desktron", "lib"))

# 导入只临时提供启动参数，配置模块保留同一实例。文件写入由每个用例的
# _memory_root 补丁隔离；不能删除共享模块，让后续动态导入绕过已有补丁。
with mock.patch.object(sys, "argv", ["test", os.path.join(_ROOT, "frontend"), "0", "0", _ROOT]):
    import memory_store


NOTE = """---
id: memo-abc12345
tier: notes
kind: judgment
confidence: reported
summary: 测试判断
created: 2026-07-20
updated: 2026-07-20
---

旧正文。
"""


class MemoryEditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.patch = mock.patch.object(memory_store, "_memory_root", return_value=self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_import_preserves_shared_modules_and_argv(self):
        # 独立进程检查冷启动和已加载两种情形；只收集模块，不运行其中用例。
        script = r'''
import os, runpy, sys, tempfile
from pathlib import Path
source = Path(sys.argv[1]).resolve()
preload = sys.argv[2] == 'warm'
sys.path.insert(0, str(source.parents[1] / 'lib'))
with tempfile.TemporaryDirectory() as root:
    os.environ['DESKTRON_DATA_HOME'] = os.path.join(root, 'data')
    sys.argv = ['test', os.path.join(root, 'frontend'), '0', '0', root]
    argv = sys.argv
    before = {}
    if preload:
        import deskcfg, memory_store
        before = {name: sys.modules[name] for name in ('deskcfg', 'memory_store')}
    loaded = runpy.run_path(str(source))
    assert sys.argv is argv, '导入测试模块改变了调用方 argv'
    assert loaded['memory_store'] is sys.modules.get('memory_store'), 'memory_store 被摘出缓存'
    assert 'deskcfg' in sys.modules, 'deskcfg 被摘出缓存'
    for name, module in before.items():
        assert sys.modules[name] is module, name + ' 模块身份发生变化'
'''
        for mode in ('cold', 'warm'):
            with self.subTest(mode=mode):
                result = subprocess.run([sys.executable, '-c', script, __file__, mode],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)

    def _write(self, rel, text=NOTE):
        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)
        return full

    def test_notes_fallback_keeps_confidence(self):
        # curator 对 tmp root 无实例会失败 → 免闸层(notes)直改回退
        full = self._write("incident/notes/memo-abc12345.md")
        res = memory_store._memory_edit("incident/notes/memo-abc12345.md", "新正文,已人工确认。", by="tester")
        self.assertTrue(res["ok"], res)
        with open(full, encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("confidence: reported", txt)      # 编辑不升档
        self.assertNotIn("confidence: verified", txt)
        self.assertIn("新正文,已人工确认。", txt)
        self.assertNotIn("旧正文", txt)
        self.assertNotIn("updated: 2026-07-20", txt)   # updated 已刷为今天

    def test_sot_does_not_bypass_gate(self):
        self._write("incident/sot/ops.md")
        res = memory_store._memory_edit("incident/sot/ops.md", "x")
        self.assertFalse(res["ok"])
        self.assertIn("过闸", res["error"])

    def test_sealed_rejected(self):
        self._write("_sealed/red.md")
        res = memory_store._memory_edit("_sealed/red.md", "x")
        self.assertFalse(res["ok"])
        self.assertIn("_sealed", res["error"])

    def test_path_guards(self):
        self.assertFalse(memory_store._memory_edit("../evil.md", "x")["ok"])
        self.assertFalse(memory_store._memory_edit("incident/notes/nope.md", "x")["ok"])
        self.assertFalse(memory_store._memory_edit("incident/notes/not-md.txt", "x")["ok"])

    def test_missing_confidence_line_left_alone(self):
        # 没有 confidence 行也不补:档位由 curator 按 source 推导,直改回退不代它决定
        note = NOTE.replace("confidence: reported\n", "")
        self._write("thinking/notes/memo-x.md", note.replace("memo-abc12345", "memo-x"))
        res = memory_store._memory_edit("thinking/notes/memo-x.md", "正文")
        self.assertTrue(res["ok"], res)
        with open(os.path.join(self.root, "thinking/notes/memo-x.md"), encoding="utf-8") as f:
            self.assertNotIn("confidence:", f.read())

    def test_project_page_uses_title_not_legacy_capsule(self):
        self._write("incident/notes/old.md", NOTE.replace("memo-abc12345", "old"))
        capsule = "项目目的、当前状态、下一步与关键约束。" * 20
        rollup = NOTE.replace("id: memo-abc12345", "id: project-memory-incident")
        rollup = rollup.replace("summary: 测试判断", f"summary: Incident 项目记忆\nrole: rollup\ncapsule: {capsule}")
        self._write("incident/notes/rollup-current.md", rollup)
        items = memory_store._project_memory("incident")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "project-memory-incident")
        self.assertEqual(items[0]["summary"], "Incident 项目记忆")
        self.assertNotIn(capsule, json.dumps(items, ensure_ascii=False))
        self.assertEqual(items[0]["updated"], "2026-07-20")
        json.dumps(items, ensure_ascii=False)  # API 返回体必须可直接 JSON 序列化

    def test_project_page_does_not_fall_back_to_legacy_notes(self):
        self._write("incident/notes/old.md", NOTE.replace("memo-abc12345", "old"))
        self._write("incident/sot/charter.md", NOTE.replace("memo-abc12345", "charter"))
        self.assertEqual(memory_store._project_memory("incident"), [])


if __name__ == "__main__":
    unittest.main()
