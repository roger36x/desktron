"""Historical session handoff regression tests.

Run: python3 -m unittest desktron/tests/test_session_handoff.py
"""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock


_BASE = tempfile.mkdtemp(prefix="session-handoff-test-")
_REPO_ROOT = os.path.join(_BASE, "repo_root")
_FRONTEND = os.path.join(_BASE, "frontend")
_DATA = os.path.join(_BASE, "data")
for _d in (_REPO_ROOT, _FRONTEND, _DATA):
    os.makedirs(_d, exist_ok=True)

os.environ["DESKTRON_DATA_HOME"] = _DATA
sys.argv = ["test", _FRONTEND, "0", "0", _REPO_ROOT]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

_termpty = types.ModuleType("termpty")
_termpty.pty_open = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("not used"))
_termpty.pty_kill = lambda *a, **k: None
_termpty.pty_input = lambda *a, **k: None
sys.modules["termpty"] = _termpty

# Other tests may leave a stubbed domains module in sys.modules during unittest discover.
if "domains" in sys.modules and not hasattr(sys.modules["domains"], "_domain_for_cwd"):
    del sys.modules["domains"]

import term  # noqa: E402


class CursorHandoffPrompt(unittest.TestCase):
    def test_cursor_transcript_becomes_clean_handoff_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            sid = "11111111-2222-4333-8444-555555555555"
            fp = os.path.join(td, f"{sid}.jsonl")
            rows = [
                {
                    "role": "user",
                    "message": {
                        "content": "# AGENTS.md instructions for /repo\n\nnoise"
                    },
                },
                {
                    "role": "user",
                    "message": {
                        "content": "<user_query>\n继续实现个人应用开发项目\n</user_query>"
                    },
                },
                {
                    "role": "assistant",
                    "message": {"content": "已经创建了页面，还差会话恢复能力。"},
                },
            ]
            with open(fp, "w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

            with mock.patch.object(term, "_cursor_path", return_value=fp):
                prompt = term._session_handoff_prompt("cursor-agent", sid, "codex")

        self.assertIn("source_engine: cursor-agent", prompt)
        self.assertIn("target_engine: codex", prompt)
        self.assertIn("用户: 继续实现个人应用开发项目", prompt)
        self.assertIn("助手: 已经创建了页面，还差会话恢复能力。", prompt)
        self.assertNotIn("# AGENTS.md", prompt)

    def test_claude_transcript_becomes_handoff_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            sid = "22222222-3333-4444-8555-666666666666"
            fp = os.path.join(td, f"{sid}.jsonl")
            rows = [
                {"type": "summary", "summary": "旧标题"},
                {"type": "user", "message": {"content": "<system-reminder>noise</system-reminder>"}},
                {"type": "user", "message": {"content": "继续修 MR comment"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "已经定位到三个问题。"}]}},
            ]
            with open(fp, "w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

            with mock.patch.object(term, "_workspace_dirs", return_value=[td]), \
                 mock.patch.object(term, "_claude_path", return_value=fp):
                prompt = term._session_handoff_prompt("claude", sid, "codex")

        self.assertIn("source_engine: claude", prompt)
        self.assertIn("用户: 继续修 MR comment", prompt)
        self.assertIn("助手: 已经定位到三个问题。", prompt)
        self.assertNotIn("system-reminder", prompt)

    def test_codex_rollout_becomes_handoff_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            sid = "33333333-4444-4555-8666-777777777777"
            fp = os.path.join(td, f"rollout-test-{sid}.jsonl")
            rows = [
                {"type": "session_meta", "payload": {"id": sid, "cwd": td}},
                {"type": "response_item", "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "# AGENTS.md instructions\nnoise"}],
                }},
                {"type": "response_item", "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "修复 Lark 注入问题"}],
                }},
                {"type": "response_item", "payload": {
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "我会改成 dataset 事件绑定。"}],
                }},
            ]
            with open(fp, "w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

            with mock.patch.object(term, "_codex_rollout_path", return_value=fp):
                prompt = term._session_handoff_prompt("codex", sid, "claude")
                same_engine = term._session_handoff_prompt("codex", sid, "codex")

        self.assertIn("source_engine: codex", prompt)
        self.assertIn("用户: 修复 Lark 注入问题", prompt)
        self.assertIn("助手: 我会改成 dataset 事件绑定。", prompt)
        self.assertNotIn("# AGENTS.md", prompt)
        self.assertIn("source_engine: codex", same_engine)
        self.assertIn("target_engine: codex", same_engine)
        self.assertIn("不恢复原线程的新会话", same_engine)
        self.assertIn("不要沿用源会话的 Desk 业务角色", same_engine)


class ResumePickerTitles(unittest.TestCase):
    def test_dir_sessions_uses_custom_title_when_present(self):
        class Store:
            def list_by_cwd(self, wd):
                return [{"id": "sid-1", "title": "native title", "time": 123}]

        spec = types.SimpleNamespace(store=Store())
        fake_engines = types.ModuleType("engines")
        fake_engines.get = lambda engine: spec

        with mock.patch.dict(sys.modules, {"engines": fake_engines}), \
             mock.patch.object(term, "_custom_titles", return_value={"sid-1": "renamed title"}):
            sessions = term._dir_sessions("cursor-agent", "/tmp/project")

        self.assertEqual(sessions[0]["title"], "renamed title")

    def test_dir_sessions_uses_custom_title_for_cursor_meta_supplement(self):
        class Store:
            def list_by_cwd(self, wd):
                return []

        spec = types.SimpleNamespace(store=Store())
        fake_engines = types.ModuleType("engines")
        fake_engines.get = lambda engine: spec
        native = {"id": "sid-2", "title": "native cursor", "time": 456}

        with mock.patch.dict(sys.modules, {"engines": fake_engines}), \
             mock.patch.object(term, "_domain_for_cwd", return_value="dom"), \
             mock.patch.object(term, "_term_meta_read", return_value={"sid-2": {"engine": "cursor-agent", "domain": "dom"}}), \
             mock.patch.object(term, "_cursor_session_by_id", return_value=native), \
             mock.patch.object(term, "_custom_titles", return_value={"sid-2": "renamed cursor"}):
            sessions = term._dir_sessions("cursor-agent", "/tmp/project")

        self.assertEqual(sessions[0]["title"], "renamed cursor")

    def test_dir_sessions_excludes_task_runs_from_resume_picker(self):
        class Store:
            def list_by_cwd(self, wd):
                return [
                    {"id": "task-sid", "title": "app task", "time": 456},
                    {"id": "normal-sid", "title": "normal", "time": 123},
                ]

        spec = types.SimpleNamespace(store=Store())
        fake_engines = types.ModuleType("engines")
        fake_engines.get = lambda engine: spec

        with mock.patch.dict(sys.modules, {"engines": fake_engines}), \
             mock.patch.object(term, "_term_meta_read", return_value={
                 "task-sid": {"engine": "codex", "domain": "dw-c-bp-sre-thinking", "task": True}
             }):
            sessions = term._dir_sessions("codex", "/tmp/project")

        self.assertEqual([s["id"] for s in sessions], ["normal-sid"])


class ResumeExplicitDomain(unittest.TestCase):
    # test_codex_resume_keeps_explicit_target_domain 随 TUI 撤:它构造一个假描述符走
    # _term_open,验证「显式传 domain 时不被 rollout 的旧 cwd 反写回原域」。GUI 这条路
    # 归属只在 meta 与适配器索引里,不存在从落盘 cwd 反推这一步。


    def test_gui_codex_busy只看适配器存活状态(self):
        sid = "019f3b62-48e2-7872-a014-4372580b8e75"
        stored = {"id": sid, "cwd": "/work", "title": "codex", "time": 123}

        def sessions(engine):
            return [stored] if engine == "codex" else []

        with term._TERM_OBS_LOCK:
            old_state = dict(term._TERM_STATE)
            term._TERM_STATE.clear()
        try:
            with mock.patch.object(term, "_term_meta_read", return_value={
                    sid: {"domain": "desk-iter", "engine": "codex", "task": False}
                 }), \
                 mock.patch.object(term, "_codex_sessions", side_effect=sessions), \
                 mock.patch.object(term, "_gui_live_sids", return_value={sid}), \
                 mock.patch.object(term, "_domain_for_cwd", return_value="desk-iter"), \
                 mock.patch.object(term, "_git_branch_for_cwd", return_value=""):
                rows = term._term_persisted()
        finally:
            with term._TERM_OBS_LOCK:
                term._TERM_STATE.clear()
                term._TERM_STATE.update(old_state)

        self.assertTrue(rows[0]["busy"])

    def test_live_codex_list_keeps_isolated_session_domain(self):
        sid = "019f3b62-48e2-7872-a014-4372580b8e75"
        sess = {"id": sid, "engine": "codex", "domain": "c-bp-sre:proj-sre-tea-event",
                "title": "codex", "created": 123, "task": False, "codex_uuid": None}

        with term._TERMS_LOCK:
            old_terms = dict(term._TERMS)
            term._TERMS.clear()
            term._TERMS[sid] = sess
        try:
            with mock.patch.object(term, "_live_map", return_value={sid: {"last_out": 0}}), \
                 mock.patch.object(term, "_codex_sessions", return_value=[{"id": sid, "cwd": "/old/project", "title": "old title"}]), \
                 mock.patch.object(term, "_domain_for_cwd", return_value="proj-sre-tea-event"), \
                 mock.patch.object(term, "_git_branch_for_cwd", return_value=""):
                rows = term._term_list()
        finally:
            with term._TERMS_LOCK:
                term._TERMS.clear()
                term._TERMS.update(old_terms)

        self.assertEqual(rows[0]["domain"], "c-bp-sre:proj-sre-tea-event")


if __name__ == "__main__":
    unittest.main()
