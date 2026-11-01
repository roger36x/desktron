"""会话标题扫描游标必须完整跨重启落盘。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import term  # noqa: E402
finally:
    sys.argv = _argv


class Flush(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {name: getattr(term, name) for name in (
            "_TCACHE_PATH", "_TITLE_CACHE", "_CT_CACHE", "_RT_CACHE", "_tcache_dirty",
            "_tcache_last", "_tcache_timer")}
        term._TCACHE_PATH = os.path.join(self.tmp.name, "title-cache.json")
        term._TITLE_CACHE = {}
        term._CT_CACHE = {}
        term._RT_CACHE = {"codex": {}}
        term._tcache_dirty = 0
        term._tcache_last = 0.0
        term._tcache_timer = None

    def tearDown(self):
        timer = term._tcache_timer
        if timer:
            timer.cancel()
        for name, value in self.saved.items():
            setattr(term, name, value)
        self.tmp.cleanup()

    def test_一批变化完整写入同一份快照(self):
        paths = []
        for i in range(3):
            path = os.path.join(self.tmp.name, "%d.jsonl" % i)
            with open(path, "w", encoding="utf-8") as f:
                f.write("x" * (i + 1))
            paths.append(path)
            term._CT_CACHE[path] = (i + 1, "标题%d" % i)
            term._tcache_dirty += 1
        self.assertTrue(term._tcache_flush())
        with open(term._TCACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(set(data["custom"]), set(paths))
        self.assertEqual(term._tcache_dirty, 0)

    def test_触碰只排一次延迟落盘(self):
        made = []

        class FakeTimer:
            daemon = False
            def __init__(self, delay, fn):
                made.append((delay, fn)); self.cancelled = False
            def start(self): pass
            def cancel(self): self.cancelled = True

        with mock.patch.object(term.threading, "Timer", FakeTimer):
            term._tcache_touch()
            term._tcache_touch()
        self.assertEqual(len(made), 1, "一条标题变化起一个 timer 会退化成高频写盘")
        self.assertGreaterEqual(made[0][0], 5, "延迟太短会跟着侧栏轮询频繁写盘")

    def test_rollout_cursor_persists_and_legacy_bad_key_cannot_block_flush(self):
        path = os.path.join(self.tmp.name, "rollout.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}\n")
        term._RT_CACHE["codex"][path] = {
            "identity": [1, 2], "offset": 3, "first_user": "默认名",
            "thread_name": "", "complete": False, "skip_line": False}
        # 旧实现曾把 tuple key 写进 _TITLE_CACHE，不能因一条脏数据放弃整批游标。
        term._TITLE_CACHE[("rollout-title", path, "codex")] = (1, 2, 3, "old")
        term._tcache_dirty = 1
        self.assertTrue(term._tcache_flush())
        term._RT_CACHE = {"codex": {}}
        term._tcache_load()
        self.assertEqual(term._RT_CACHE["codex"][path]["offset"], 3)
        self.assertEqual(term._RT_CACHE["codex"][path]["first_user"], "默认名")

    def test_进程退出前注册最终_flush(self):
        with open(os.path.join(ROOT, "desktron", "lib", "term_titles.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("atexit.register(_tcache_flush)", src)


if __name__ == "__main__":
    unittest.main()
