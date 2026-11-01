"""休眠会话列表要认得出每一个 GUI 引擎。

`_term_persisted` 按引擎名写了一串 if/elif,末尾是 `else: continue`——名单里没有的引擎,
会话在 meta 里有记录也会被整条丢掉。这个坑接新引擎时曾反复出现；
2026-08-17 第一版接 pi 时也踩过,表现都是「开过的会话切一下引擎就没了」。

所以这里不逐个引擎写用例,而是拿 GUI_ENGINES 当基准:每加一家引擎,PATCH 表先失败,
提醒作者补上取记录的那一支,再由同一套断言验它真的出现在列表里。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import term  # noqa: E402
    from engine import GUI_ENGINES  # noqa: E402
finally:
    sys.argv = _argv


def _row(sid, title="标题"):
    return {"id": sid, "title": title, "time": 1786949222000}


#: 有引擎侧落盘记录、需要工作台去扫的引擎。
TRANSCRIPT_ENGINES = ("claude", "codex", "pi")

#: 每个引擎从哪儿取会话记录。加引擎时补一行——底下那条用例会先告诉你缺了谁。
PATCH = {
    "claude": ("_claude_session_by_id", lambda sid: _row(sid, "claude 的会话")),
    "codex": ("_codex_sessions", lambda eng: [_row("s-codex", "codex 的会话")]),
    "pi": ("_adapter", lambda eng: type("Ad", (), {"index": {
        "adp-pi": {"engine_sid": "s-pi", "title": "pi 的会话",
                   "time": {"updated": 1786949222000}}}})()),
}


class PersistedPerEngine(unittest.TestCase):
    def setUp(self):
        self.stack = []
        for fn, ret in (("_live_map", lambda: {}),
                        ("_gui_live_sids", lambda: set()),
                        ("_gui_resident_sids", lambda: set()),
                        ("_cursor_sessions", lambda: []),
                        ("_custom_title_for", lambda *a: ""),
                        ("_codex_list_domain", lambda dom, cwd, fallback: dom)):
            p = mock.patch.object(term, fn, ret)
            p.start()
            self.stack.append(p)

    def tearDown(self):
        for p in reversed(self.stack):
            p.stop()

    def test_名单与有落盘记录的引擎同步(self):
        """加了引擎却没在这里补一行,说明 _term_persisted 那串分支多半也漏了它。

        当前 GUI 引擎都有可扫的记录或适配器索引,两张表应完全一致。"""
        self.assertEqual(set(PATCH), set(TRANSCRIPT_ENGINES),
                         "PATCH 表要与 TRANSCRIPT_ENGINES 同步:%s" % (set(TRANSCRIPT_ENGINES) ^ set(PATCH)))
        self.assertEqual(set(TRANSCRIPT_ENGINES), set(GUI_ENGINES))

    def test_每个引擎的休眠会话都列得出来(self):
        for eng in TRANSCRIPT_ENGINES:
            with self.subTest(engine=eng):
                sid = "s-" + eng
                fn, ret = PATCH[eng]
                with mock.patch.object(term, "_term_meta_read",
                                       lambda: {sid: {"engine": eng, "domain": "desk-iter"}}), \
                        mock.patch.object(term, fn, ret):
                    rows = term._term_persisted()
                got = [r for r in rows if r["id"] == sid]
                self.assertEqual(len(got), 1, f"{eng} 的休眠会话没出现在列表里")
                self.assertEqual(got[0]["engine"], eng)
                self.assertEqual(got[0]["domain"], "desk-iter")   # 归域信 meta
if __name__ == "__main__":
    unittest.main()
