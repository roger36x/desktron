"""AGENTS.md 给索引、不给内容:记忆索引行的描述,以及任务备忘的提示,都只留一句。

curator 的 INDEX.md 不限制描述长度,实测单条最长 861 字、中位 180 字,58 条压进 AGENTS.md
就是一万六千字——索引被写成了摘要。这一段的说明本来就要求「按需 read 全文、别全读」,
每条留一句提示足够判断要不要拉全文。只截描述,id 与路径原样保留,否则 read 与 show 都会断。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

_argv = sys.argv
sys.argv = ["x", "frontend", "7999", "7998", os.path.dirname(__file__)]   # deskcfg 在 import 时读 argv
try:
    from domains import INDEX_DESC_LIMIT, _cap_index_desc  # noqa: E402
finally:
    sys.argv = _argv


class CapIndexDesc(unittest.TestCase):
    def test_短描述原样保留(self):
        line = "- [abc](incident/notes/abc.md)(judgment · reported) — 一句话"
        self.assertEqual(_cap_index_desc(line), line)

    def test_超长描述截断并加省略号(self):
        line = "- [abc](incident/notes/abc.md)(judgment · reported) — " + "长" * 500
        out = _cap_index_desc(line)
        self.assertTrue(out.endswith("…"))
        self.assertEqual(out[out.index(" — ") + 3:], "长" * INDEX_DESC_LIMIT + "…")

    def test_id_与路径不受影响(self):
        line = "- [abc-def](incident/notes/abc-def.md)(judgment · reported) — " + "长" * 500
        out = _cap_index_desc(line)
        self.assertTrue(out.startswith("- [abc-def](incident/notes/abc-def.md)(judgment · reported)"))

    def test_没有描述的行不动(self):
        line = "- [abc](incident/notes/abc.md)(judgment · reported)"
        self.assertEqual(_cap_index_desc(line), line)

    def test_截断处不留尾随空格(self):
        line = "- [abc](x.md)(a) — " + ("词 " * 100)
        out = _cap_index_desc(line)
        self.assertNotIn(" …", out)


class MemoHint(unittest.TestCase):
    """任务备忘存在备忘录应用自己的库里,起手段只给一句提示与 note id,全文按 id 现拉。"""

    def setUp(self):
        import tasks
        self.tasks = tasks
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, ".workbench"))

    def tearDown(self):
        self.tmp.cleanup()

    def _note(self, content):
        with open(os.path.join(self.root, ".workbench", "notepad.json"), "w", encoding="utf-8") as f:
            json.dump({"notes": [{"id": "n1", "content": content}]}, f, ensure_ascii=False)

    def test_长备忘只留一句提示(self):
        self._note("标题\n" + "正" * 500)
        hint, size = self.tasks._memo_hint(self.root, "n1")
        self.assertTrue(hint.endswith("…"))
        self.assertLessEqual(len(hint), self.tasks.MEMO_HINT_LIMIT + 1)
        self.assertGreater(size, 500)          # 字数照实报,供判断值不值得拉

    def test_提示里不重复任务标题(self):
        self._note("SRE 核心目标及分工\nGSRE 整体目标：20min 止损")
        hint, _ = self.tasks._memo_hint(self.root, "n1", "SRE 核心目标及分工")
        self.assertFalse(hint.startswith("SRE 核心目标及分工"))
        self.assertTrue(hint.startswith("GSRE"))

    def test_取不到备忘返回空(self):
        self.assertEqual(self.tasks._memo_hint(self.root, "不存在"), ("", 0))

    def test_段里不内联备忘全文(self):
        body = "标题\n" + "机密正文" * 100      # 全文 400 余字
        self._note(body)
        tp = os.path.join(self.root, "TASKS.md")
        with open(tp, "w", encoding="utf-8") as f:
            f.write("# 待办\n\n- [ ] 一条任务   <!-- t:a1 note:n1 -->\n")
        seg = self.tasks.agents_tasks_segment(tp, self.root)
        memo_lines = [l for l in seg.splitlines() if l.startswith("  备忘 ")]
        self.assertEqual(len(memo_lines), 1)
        self.assertIn("note:n1", memo_lines[0])
        # 备忘行 = 前缀 + 一句提示,与全文长度无关(旧形态会随全文一起涨到 900 字)
        self.assertLess(len(memo_lines[0]), self.tasks.MEMO_HINT_LIMIT + 40)
        self.assertNotIn("  > ", seg)              # 旧的整篇缩进引用形态已废

    def test_起手段要求跨会话验收后收口原任务(self):
        tp = os.path.join(self.root, "TASKS.md")
        with open(tp, "w", encoding="utf-8") as f:
            f.write("# 待办\n\n- [~] 待发布任务   <!-- t:a1 -->\n")
        seg = self.tasks.agents_tasks_segment(tp, self.root)
        self.assertIn("发送最终答复前再次核对", seg)
        self.assertIn("其他会话留下的部署、重启或验收", seg)
        self.assertIn("沿用原任务 id 更新或关闭", seg)


class ProjectMemoryContext(unittest.TestCase):
    def test_scene_injects_entire_body_and_metadata_but_not_capsule(self):
        import domains
        from curator.note import write_note
        from memory_engine import project_memory

        with tempfile.TemporaryDirectory() as root:
            path = project_memory.model_path(os.path.join(root, ".memory"), "risk")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            capsule = "这是面向下一次会话的短摘要。" * 20
            body = "\n\n".join(
                f"## {heading}\n\n直接注入的详细正文" + "完整内容。" * 90
                for heading in project_memory.HEADINGS
            )
            write_note(path, {
                "id": "project-memory-risk", "tier": "notes", "kind": "judgment",
                "confidence": "reported", "summary": "Risk 项目记忆", "role": "rollup",
                "capsule": capsule, "created": "2026-08-31", "updated": "2026-10-16",
            }, body)
            with mock.patch.object(domains, "REPO_ROOT", root):
                out = domains._grounding_index("risk", "scene")
            self.assertNotIn(capsule, out)
            self.assertIn(body, out)
            self.assertEqual(out.count(body), 1)
            self.assertGreater(len(body), 1200)  # 不沿用旧摘要的读取截断。
            self.assertIn("show --id project-memory-risk", out)
            self.assertIn("更新时间：2026-10-16", out)
            self.assertIn("不代表实时状态、当前指令或操作授权", out)
            with mock.patch.object(domains, "REPO_ROOT", root):
                self.assertEqual(out, domains._grounding_index("risk", "scene"))
                self.assertNotIn(body, domains._grounding_index("risk", "nav"))


if __name__ == "__main__":
    unittest.main()
