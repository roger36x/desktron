"""模型可见的上下文标题应说明内容，不使用阶段黑话。"""
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

old_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import domains
    import engine_adapters as ea
    import tasks
    from grounding import builder as grounding_builder
    from engine_adapters.bus import SCENE_HEAD, SCENE_SEP
    from shepherd.claude_codec import BASH_CWD_PROMPT
    from shepherd.history_prompt import history_prompt
finally:
    sys.argv = old_argv


class 上下文命名(unittest.TestCase):
    def test_表达要求直接执行而非自我解释(self):
        with open(os.path.join(ROOT, "prompts", "style.md"), encoding="utf-8") as f:
            style = f.read()
        self.assertIn("不先复述任务、声明计划", style)
        self.assertIn("工具调用前只写必要的动作目的", style)
        self.assertIn("不解释自己", style)

    def test_系统约定说明工作环境(self):
        with open(os.path.join(ROOT, "common", "orientation.md"), encoding="utf-8") as f:
            orientation = f.read()
        self.assertTrue(orientation.startswith("## 工作环境\n"))
        self.assertIn("记忆只保留三部分", orientation)
        for retired in ("人物关系和依赖只查图谱", "记忆分为四层", "`pinned`"):
            self.assertNotIn(retired, orientation)
        self.assertNotIn("起手", orientation)

    def test_当前上下文不再注入旧知识入口(self):
        with tempfile.TemporaryDirectory() as root:
            history = os.path.join(root, "sync", "INDEX.md")
            os.makedirs(os.path.dirname(history))
            with open(history, "w", encoding="utf-8") as f:
                f.write("# 历史索引\n")
            os.makedirs(os.path.join(root, "domains", "desk-iter", "imports", "old-kb"))
            os.makedirs(os.path.join(root, ".memory", "graph"))
            os.makedirs(os.path.join(root, "knowledge"))
            os.makedirs(os.path.join(root, "orgknowledge"))
            with open(os.path.join(root, ".memory", "graph", "entities.yaml"), "w", encoding="utf-8") as f:
                f.write("entities:\n  - id: old\nedges: []\n")
            with open(os.path.join(root, "knowledge", "INDEX.md"), "w", encoding="utf-8") as f:
                f.write("# 旧缓存\n")
            with mock.patch.object(domains, "REPO_ROOT", root), \
                 mock.patch.object(domains, "_domain_dir", return_value=os.path.join(root, "domains", "desk-iter")), \
                 mock.patch.object(domains, "_repo_items", return_value=[]), \
                 mock.patch.object(domains.os.path, "expanduser", return_value=history), \
                 mock.patch("grounding.documents.navigation", return_value="### Desk 资料\n\n测试"):
                text = domains._grounding_index("desk-iter", "static")
        self.assertTrue(text.startswith("## 当前上下文\n"))
        self.assertIn("### 历史资料", text)
        for retired in ("### 知识入口", "关系图谱", "跨用户知识库", "old-kb",
                        "DeepWiki", "官方知识库", "orgknowledge"):
            self.assertNotIn(retired, text)
        self.assertNotIn("起手", text)

    def test_历史索引不存在时不注入入口(self):
        missing = os.path.join(ROOT, "definitely-missing-sync-index")
        with mock.patch.object(domains, "REPO_ROOT", ROOT), \
             mock.patch.object(domains.os.path, "expanduser", return_value=missing):
            text = domains._grounding_index("desk-iter", "static")
        self.assertNotIn("### 历史资料", text)

    def test_memory_skill_只说明三部分记忆(self):
        path = os.path.join(ROOT, ".claude", "skills", "memory", "SKILL.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("工作流记忆只包含三类内容", text)
        self.assertIn("--tier sealed", text)
        for retired in ("pinned", "--tier fact", "graph neighbors"):
            self.assertNotIn(retired, text)

    def test_原生技能简介简短且不带旧概念(self):
        retired = ("kb-extract", "knowledge", "knowledge-ingest")
        for name in retired:
            self.assertFalse(os.path.isfile(os.path.join(ROOT, ".claude", "skills", name, "SKILL.md")))
        banned = ("recap", "sealed", "journal", "compact", "curator", "kind=fact",
                  "graph", "agentic", "non-agentic", "Provider", "MCP", "cookie", "SSO")
        base = os.path.join(ROOT, ".claude", "skills")
        for name in os.listdir(base):
            path = os.path.join(base, name, "SKILL.md")
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                frontmatter = f.read(4096).split("---", 2)[1]
            line = next(row for row in frontmatter.splitlines() if row.startswith("description:"))
            description = line.split(":", 1)[1].strip().strip('"')
            self.assertLessEqual(len(description), 180, name)
            for word in banned:
                self.assertNotIn(word, description, name)

    def test_各引擎提示词都不枚举技能(self):
        for engine in ("claude", "shepherd:claude", "pi", "codex"):
            with mock.patch.object(domains, "REPO_ROOT", ROOT):
                text = grounding_builder.constant(ROOT, "desk-iter", engine)
            self.assertNotIn("## 可用技能", text, engine)
            self.assertNotIn("全局已安装技能", text, engine)

    def test_模型可见正文不含实现行话(self):
        class Connector:
            store_root = "/tmp/history"
            binding = {"conversation_id": "conversation"}

        files = ("prompts/style.md", "common/orientation.md", "common/principles.md", "AGENTS.md")
        texts = [open(os.path.join(ROOT, path), encoding="utf-8").read() for path in files]
        with tempfile.TemporaryDirectory() as task_root:
            task_path = os.path.join(task_root, "TASKS.md")
            with open(task_path, "w", encoding="utf-8") as f:
                f.write("# 待办\n\n- [ ] 示例任务   <!-- t:sample -->\n")
            texts.append(tasks.agents_tasks_segment(task_path, task_root))
        texts += [
            grounding_builder.constant(ROOT, "desk-iter", "pi"),
            ea.ClaudeAdapter.CLAUDE_DELTA, ea.ClaudeAdapter.INTENT_RULE,
            ea.CodexAdapter.CHANNEL_RULE, ea.CodexAdapter.INTENT_RULE, ea.CODEX_DELTA,
            ea.PiAdapter.PI_DELTA,
            BASH_CWD_PROMPT, history_prompt(Connector()),
        ]
        retired = ("裸引擎", "recap", "准入", "任务账本", "mailbox", "revision",
                   "heredoc", "schema", "可达性", "兜底", "主路径", "可观测",
                   "终态", "operation", "child 身份", "grounding", "Binding",
                   "agent loop", "notification turn", "bounded", "异步", "快照")
        for text in texts:
            for word in retired:
                self.assertNotIn(word, text, word)

    def test_新分隔文本不含阶段黑话(self):
        self.assertEqual(SCENE_HEAD, "## 当前上下文")
        self.assertIn("工作区上下文", SCENE_SEP)
        self.assertNotIn("起手", SCENE_SEP)

    def test_首轮不列记忆数量和最近_id(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, ".memory"))
            with open(os.path.join(root, ".memory", "INDEX.md"), "w", encoding="utf-8") as f:
                f.write("## notes\n\n- [irrelevant-note](desk-iter/notes/x.md)(judgment · reported)\n\n"
                        "## sealed\n\n- [private-person](_sealed/people/x.md)(judgment · reported)\n")
            with mock.patch.object(domains, "REPO_ROOT", root):
                text = domains._grounding_index("desk-iter", "scene")
        self.assertNotIn("irrelevant-note", text)
        self.assertNotIn("private-person", text)
        self.assertNotIn("最近", text)
        self.assertNotIn("记忆导航", text)


if __name__ == "__main__":
    unittest.main()
