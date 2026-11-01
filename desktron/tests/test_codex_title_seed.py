"""引擎侧自动起的标题不能是 Desk 的 seed(2026-08-26 用户实见)。

codex 会拿首条用户消息自动填 state db 的 threads.title,而首条消息现在以 seed 开头——
新会话的名字于是成了「〈系统约定,本次会话持续有效〉 ## 起手…」。切引擎重载后侧栏按
rollout/db 取标题(此前显示的是适配器索引里的正确标题),问题这才显形。

剥 seed 的修复(_desk_seed_strip)当时只做在「rollout 首条用户消息」那条兜底上,
而 db 标题的优先级更高,轮不到它执行;db 里存的又是截断过的(seed 约 3.3k,截断点
远在分隔符之前),剥不出原话,只能整条判废、落回兜底。
"""
import os
import sys
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import term
    import engine_adapters as ea
finally:
    sys.argv = _argv

SEED = ea.CodexAdapter.SEED_HEAD
SEP = ea.CodexAdapter.SEED_SEP


class EngineTitleClean(unittest.TestCase):
    def test_截断的种子标题整条判废(self):
        """db 里那种:以 seed 开头、分隔符已被截掉,剥不出原话——返回空,调用方落回兜底。"""
        self.assertEqual(term._engine_title_clean(SEED + "## 起手\n\n你是 Desktron 中的 Agent"), "")

    def test_完整种子剥出用户原话(self):
        full = SEED + "种子正文" + SEP + "情景" + ea.SCENE_SEP + "真话"
        self.assertEqual(term._engine_title_clean(full), "真话")

    def test_正常改名不受影响(self):
        self.assertEqual(term._engine_title_clean("排查 incident 告警"), "排查 incident 告警")

    def test_空值安全(self):
        self.assertEqual(term._engine_title_clean(""), "")
        self.assertEqual(term._engine_title_clean(None), "")

    def test_两个读取点都过这道(self):
        """db 与 session_index 各是一条路,漏一条的表现完全一样,一起锁。"""
        src = _srcmap.term_src()
        i = src.index("def _session_index_titles")
        j = src.index("def _state_db_titles")
        k = src.index("def _rollout_uuid")
        self.assertIn("_engine_title_clean", src[i:j], "session_index 那条没过闸")
        self.assertIn("_engine_title_clean", src[j:k], "state db 那条没过闸")


if __name__ == "__main__":
    unittest.main()
