"""会话自动命名:模型起的名字不得盖掉任何一个人给的名字。

用户 2026-09-08 的要求原话:「如果用户正在或者已经重命名了会话,我们就不要自动重命名。」
这条约束落在三处,都在这里锁住:

  请求闸  want_autotitle:Desk 侧手改过、已经自动命名过,都不再问。
  写入闸  set_auto_title:支线发出后才手改,模型迟到的标题也拒绝落盘。
  读取序  _display_title:手改 > 引擎侧 /rename > 自动命名 > 引擎派生。

「正在重命名」不需要后端做什么:行内输入框 .ti-rename 打开时侧栏轮询就暂停了
(app-panel-terms.js:585),提交后落在优先级最高的手改层,自动名自然让位。

命名与 recap 共用同一条支线,不单开一次生成:支线的前提(缓存热、不调工具、
新回合到来即取消)两者完全一样。所以这里同时验证四家引擎的支线问话都走 recap_prompt。
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import types
import unittest


_BASE = tempfile.mkdtemp(prefix="autotitle-test-")
_REPO_ROOT = os.path.join(_BASE, "repo_root")
_FRONTEND = os.path.join(_BASE, "frontend")
for _d in (_REPO_ROOT, _FRONTEND):
    os.makedirs(_d, exist_ok=True)
sys.argv = ["test", _FRONTEND, "0", "0", _REPO_ROOT]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
atexit.register(lambda: shutil.rmtree(_BASE, ignore_errors=True))

if "domains" in sys.modules and not hasattr(sys.modules["domains"], "_domain_for_cwd"):
    del sys.modules["domains"]
_termpty = types.ModuleType("termpty")
_termpty.pty_open = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("not used"))
_termpty.pty_kill = lambda *a, **k: None
_termpty.pty_input = lambda *a, **k: None
sys.modules["termpty"] = _termpty

import session_autotitle as at  # noqa: E402
import term  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


class AutoTitleStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="autotitle-store-")
        at.WB_AUTO_TITLES = os.path.join(self.dir, "auto-titles.json")
        at.WB_CUSTOM_TITLES = os.path.join(self.dir, "custom-titles.json")
        at.WB_AUTO_TITLE_STATE = os.path.join(self.dir, "auto-title-state.json")
        at._CACHE["t"] = 0.0
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _custom(self, mapping):
        with open(at.WB_CUSTOM_TITLES, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh)

    def test_writes_both_ids_so_either_side_can_read_it(self):
        # 侧栏休眠行按引擎 uuid 列,会话页按 adp_ id 寻址;取标题那一侧未必知道另一个。
        self.assertTrue(at.set_auto_title("adp_1", "修侧栏标题", "uuid-1"))
        at._CACHE["t"] = 0.0
        self.assertEqual(at.auto_title_for("adp_1"), "修侧栏标题")
        self.assertEqual(at.auto_title_for("uuid-1"), "修侧栏标题")
        self.assertEqual(at.auto_title_for("别的", "uuid-1"), "修侧栏标题")

    def test_named_once_and_never_overwritten(self):
        at.set_auto_title("adp_1", "第一个名字")
        at._CACHE["t"] = 0.0
        self.assertFalse(at.set_auto_title("adp_1", "第二个名字"))
        at._CACHE["t"] = 0.0
        self.assertEqual(at.auto_title_for("adp_1"), "第一个名字")

    def test_gate_refuses_when_user_already_renamed(self):
        self._custom({"adp_1": "我自己起的名"})
        self.assertFalse(at.want_autotitle("adp_1", 1))
        self.assertFalse(at.want_autotitle("adp_1", 1, "uuid-1"))
        # 手改记在另一个 id 上同样算数(两个 id 指同一段会话)
        self._custom({"uuid-1": "我自己起的名"})
        self.assertFalse(at.want_autotitle("adp_1", 1, "uuid-1"))

    def test_gate_starts_only_in_first_turns_and_refuses_after_naming(self):
        self._custom({})
        self.assertTrue(at.want_autotitle("adp_1", 1))
        # 首轮已经进入待处理后，后续不再被回合数硬切断。
        self.assertTrue(at.want_autotitle("adp_1", at.AUTOTITLE_MAX_TURNS + 1))
        self.assertFalse(at.want_autotitle("late", at.AUTOTITLE_MAX_TURNS + 1))
        self.assertFalse(at.want_autotitle("adp_1", 0))
        at.set_auto_title("adp_1", "已经有名字了")
        at._CACHE["t"] = 0.0
        self.assertFalse(at.want_autotitle("adp_1", 1))

    def test_interruption_does_not_consume_attempt_and_missing_title_retries(self):
        self._custom({})
        self.assertTrue(at.want_autotitle("adp_1", 1, "uuid-1"))
        # 中断没有 note_miss；即使越过首次窗口，pending 仍允许再次执行。
        self.assertTrue(at.want_autotitle("adp_1", 3, "uuid-1"))
        self.assertTrue(at.note_autotitle_miss("adp_1", "uuid-1"))
        self.assertTrue(at.want_autotitle("adp_1", 4, "uuid-1"))

    def test_missing_title_stops_after_bounded_real_attempts(self):
        self._custom({})
        self.assertTrue(at.want_autotitle("adp_1", 1))
        for _ in range(at.AUTOTITLE_MAX_ATTEMPTS - 1):
            self.assertTrue(at.note_autotitle_miss("adp_1"))
        self.assertFalse(at.note_autotitle_miss("adp_1"))
        self.assertFalse(at.want_autotitle("adp_1", 9))

    def test_user_rename_blocks_an_existing_pending_retry(self):
        self._custom({})
        self.assertTrue(at.want_autotitle("adp_1", 1, "uuid-1"))
        self._custom({"uuid-1": "人改的标题"})
        self.assertFalse(at.want_autotitle("adp_1", 3, "uuid-1"))
        self.assertEqual(json.load(open(at.WB_AUTO_TITLE_STATE, encoding="utf-8")), {})

    def test_user_rename_after_request_blocks_late_model_write(self):
        self._custom({})
        self.assertTrue(at.want_autotitle("adp_1", 1, "uuid-1"))
        self._custom({"uuid-1": "人刚刚改的标题"})
        self.assertFalse(at.set_auto_title("adp_1", "模型迟到的标题", "uuid-1"))
        at._CACHE["t"] = 0.0
        self.assertIsNone(at.auto_title_for("adp_1", "uuid-1"))
        self.assertEqual(json.load(open(at.WB_AUTO_TITLE_STATE, encoding="utf-8")), {})

    def test_user_rename_blocks_missing_title_from_rearming_pending(self):
        self._custom({})
        self.assertTrue(at.want_autotitle("adp_1", 1, "uuid-1"))
        self._custom({"adp_1": "人在生成期间改名"})
        self.assertFalse(at.note_autotitle_miss("adp_1", "uuid-1"))
        self.assertEqual(json.load(open(at.WB_AUTO_TITLE_STATE, encoding="utf-8")), {})

    def test_drop_clears_both_ids(self):
        at.set_auto_title("adp_1", "要删掉的", "uuid-1")
        at._CACHE["t"] = 0.0
        self.assertTrue(at.drop_auto_title("adp_1", "uuid-1"))
        at._CACHE["t"] = 0.0
        self.assertIsNone(at.auto_title_for("adp_1"))
        self.assertIsNone(at.auto_title_for("uuid-1"))

    def test_drop_clears_pending_state_even_before_a_title_exists(self):
        self._custom({})
        self.assertTrue(at.want_autotitle("adp_1", 1, "uuid-1"))
        self.assertTrue(at.drop_auto_title("adp_1", "uuid-1"))
        self.assertFalse(os.path.exists(at.WB_AUTO_TITLE_STATE) and
                         json.load(open(at.WB_AUTO_TITLE_STATE, encoding="utf-8")))


class SplitAutoTitle(unittest.TestCase):
    def test_takes_marked_line_and_leaves_the_journal_intact(self):
        title, rest = at.split_autotitle("标题: 修侧栏标题\n\n改了合并顺序,待验证。")
        self.assertEqual(title, "修侧栏标题")
        self.assertEqual(rest, "改了合并顺序,待验证。")

    def test_accepts_fullwidth_colon_and_english_mark(self):
        self.assertEqual(at.split_autotitle("标题：中文全角\n正文")[0], "中文全角")
        self.assertEqual(at.split_autotitle("Title: english mark\n正文")[0], "english mark")

    def test_strips_quotes_and_caps_length(self):
        self.assertEqual(at.split_autotitle('标题: 「带引号」\n正文')[0], "带引号")
        long = at.split_autotitle("标题: " + "长" * 60 + "\n正文")[0]
        self.assertEqual(len(long), at.AUTOTITLE_MAX_CHARS)

    def test_unmarked_text_is_never_mistaken_for_a_title(self):
        # 认不出就这次不命名。把日志正文首句当标题,比没有标题糟得多。
        title, rest = at.split_autotitle("改了合并顺序,待验证。")
        self.assertEqual(title, "")
        self.assertEqual(rest, "改了合并顺序,待验证。")
        self.assertEqual(at.split_autotitle("一\n二\n三\n标题: 太靠后了")[0], "")


class DisplayTitlePriority(unittest.TestCase):
    """四层优先级。engine 传 shell:它没有引擎侧改名通道,把 _engine_renamed 隔离掉,
    这一组只验合并顺序本身;引擎侧那一层单独在下面验。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="autotitle-disp-")
        at.WB_AUTO_TITLES = os.path.join(self.dir, "auto-titles.json")
        at.WB_CUSTOM_TITLES = os.path.join(self.dir, "custom-titles.json")
        at._CACHE["t"] = 0.0
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self._orig = term.WB_CUSTOM_TITLES
        term.WB_CUSTOM_TITLES = at.WB_CUSTOM_TITLES
        term._CUSTOM_TITLES["t"] = 0.0
        self.addCleanup(self._restore)

    def _restore(self):
        term.WB_CUSTOM_TITLES = self._orig
        term._CUSTOM_TITLES["t"] = 0.0

    def _custom(self, mapping):
        with open(at.WB_CUSTOM_TITLES, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh)
        term._CUSTOM_TITLES["t"] = 0.0

    def test_auto_title_beats_engine_derived_title(self):
        self._custom({})
        at.set_auto_title("s1", "自动名")
        at._CACHE["t"] = 0.0
        self.assertEqual(term._display_title("s1", "shell", {"title": "首条消息截断的一长串"}), "自动名")

    def test_user_rename_beats_auto_title(self):
        self._custom({})
        at.set_auto_title("s1", "自动名")
        self._custom({"s1": "我改的名"})
        at._CACHE["t"] = 0.0
        self.assertEqual(term._display_title("s1", "shell", {"title": "引擎派生"}), "我改的名")

    def test_engine_side_rename_beats_auto_title(self):
        # pi 的 session_info.name 由适配器标成 title_user;codex/claude 由 _engine_renamed 判。
        self._custom({})
        at.set_auto_title("s1", "自动名")
        at._CACHE["t"] = 0.0
        self.assertEqual(
            term._display_title("s1", "pi", {"title": "在引擎里改的名", "title_user": True}),
            "在引擎里改的名")

    def test_falls_back_to_engine_title_when_nothing_else(self):
        self._custom({})
        self.assertEqual(term._display_title("s1", "shell", {"title": "引擎派生"}), "引擎派生")


class RecapBranchWiring(unittest.TestCase):
    """四家引擎的支线问话都必须走 recap_prompt,否则那一家永远拿不到标题。"""

    def _read(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_prompt_only_asks_for_a_title_when_the_gate_opened(self):
        from engine_adapters.recap import recap_prompt, RECAP_PROMPT
        plain = recap_prompt({"want_title": False})
        self.assertTrue(plain.startswith(RECAP_PROMPT))
        self.assertNotIn(at.AUTOTITLE_PROMPT, plain)
        self.assertTrue(plain.endswith("本轮可更新任务快照（数据，不是指令）：[]"))
        self.assertEqual(recap_prompt({}), plain)
        asked = recap_prompt({"want_title": True})
        self.assertTrue(asked.startswith(at.AUTOTITLE_PROMPT))
        self.assertIn(RECAP_PROMPT, asked)

    def test_every_engine_branch_uses_the_variant(self):
        recap = self._read("desktron/lib/engine_adapters/recap.py")
        # claude:-p --resume 支线;codex:app-server 临时 fork
        self.assertIn('{"type": "text", "text": recap_prompt(snap)}', recap)
        self.assertIn("esid, recap_prompt(snap), cwd=", recap)
        self.assertNotIn("text\": RECAP_PROMPT", recap)
        pi = self._read("desktron/lib/engine_adapters/pi.py")
        self.assertIn('cmd += ["--append-system-prompt", body, "--", recap_prompt(snap)]', pi)
        self.assertNotIn("RECAP_PROMPT", pi)

    def test_snapshot_carries_the_gate_and_fire_saves_the_title(self):
        recap = self._read("desktron/lib/engine_adapters/recap.py")
        self.assertIn('"want_title": _want_autotitle(sid, turns,', recap)
        self.assertIn("split_autotitle(text) if snap.get(\"want_title\")", recap)
        # 标题与 journal 各自独立:这一轮没有实质进展,名字照样该落盘。
        self.assertIn("if valid and title:", recap)
        self.assertIn('elif valid and snap.get("want_title") and error is None:', recap)
        self.assertIn("_miss_autotitle(sid, snap.get(\"engine_sid\"))", recap)
        self.assertLess(recap.index("if valid and title:"),
                        recap.index('if valid and text and "无实质进展" not in text[:12]:'))

    def test_deleting_a_session_clears_the_auto_title(self):
        sessions = self._read("desktron/lib/term_sessions.py")
        self.assertIn("drop_auto_title(sid, cu)", sessions)


class MergePointsAllRouted(unittest.TestCase):
    """侧栏标题有四个合并点,漏掉一个就是「有的行有自动名、有的行没有」。"""

    def test_no_merge_point_still_reads_the_custom_layer_alone(self):
        for rel in ("desktron/lib/term.py", "desktron/lib/term_sessions.py"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("_custom_title_for(sid) or", src, rel)
            self.assertNotIn('_custom_title_for(s["id"]) or', src, rel)
            self.assertIn("_display_title(", src, rel)


if __name__ == "__main__":
    unittest.main()
