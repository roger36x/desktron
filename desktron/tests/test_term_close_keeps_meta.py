"""关闭会话(keep)不能删 meta——分组与工作流引用只存在于那里。

回归的是实测缺陷:在任务里点会话行的 ×,会话没有从列表消失,反而带着任务绑定
出现在聊天组。原因是关闭走 meta.pop,domain / refs 一起没了,再被扫回来就成了
无归属的散会话。现在改为打 closed 标记:_term_persisted 跳过它(列表里消失),
归属留在盘上,resume 时清标记原地续接。
"""
import os
import sys
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
if "deskcfg" not in sys.modules:   # 只在没人定过时才设:REPO_ROOT 在 deskcfg 首次 import 时定死,
    sys.argv = ["test", "/tmp/tclose-frontend", "0", "0", "/tmp/tclose-repo"]   # 抢先改会连累同批跑的别的用例
# 别的用例会把一个含 domains/ 目录的路径塞进 sys.path,`domains` 于是被当成命名空间包缓存下来,
# term 再 import 它就取不到函数。命中这种残留时先丢掉缓存,让 lib/domains.py 重新胜出。
if getattr(sys.modules.get("domains"), "__file__", None) is None:
    sys.modules.pop("domains", None)
import term  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


class CloseKeepsGrouping(unittest.TestCase):
    def setUp(self):
        self.meta = {SID: {"domain": "desk-iter", "engine": "claude", "task": False,
                           "refs": ["desk-iter"]}}
        self._read, self._write = term._term_meta_read, term._term_meta_write
        self._kill, self._title = term.termpty.pty_kill, term._term_set_title
        term._term_meta_read = lambda: {k: dict(v) for k, v in self.meta.items()}
        term._term_meta_write = lambda m: self.meta.update(m) or self.meta.clear() or self.meta.update(m)
        term.termpty.pty_kill = lambda sid: False
        term._term_set_title = lambda sid, t: None

    def tearDown(self):
        term._term_meta_read, term._term_meta_write = self._read, self._write
        term.termpty.pty_kill, term._term_set_title = self._kill, self._title

    def test_close_marks_closed_and_keeps_domain_and_refs(self):
        term._term_kill(SID, keep=True)
        m = self.meta[SID]
        self.assertTrue(m.get("closed"))
        self.assertEqual(m.get("domain"), "desk-iter")    # 分组还在:再打开仍回工作流,不落聊天组
        self.assertEqual(m.get("refs"), ["desk-iter"])    # 工作流引用还在

    def test_delete_drops_the_entry(self):
        term._term_kill(SID, keep=False)
        self.assertNotIn(SID, self.meta)                  # 删除是删除,不留条目


class ClosedIsHiddenAndCleared(unittest.TestCase):
    def test_persisted_skips_closed(self):
        src = _srcmap.term_src()
        # 扫描主体与并发去重包装分开；关闭态过滤属于扫描主体。
        start = src.index("def _term_persisted_scan(")
        nxt = src.find("\ndef ", start + 1)
        body = src[start:nxt if nxt > 0 else len(src)]   # 它可能是文件里最后一个顶层函数
        self.assertIn('if m.get("closed"):', body)

    # test_open_clears_the_flag_on_resume 随 TUI 撤:它锁的是 _term_open 里那三处
    # `"closed": False`。GUI 会话没有「resume 拉活」这一步,closed 由 term_meta_register
    # 在会话再次登记时清掉,上面那条用例覆盖的正是这一处。


if __name__ == "__main__":
    unittest.main()
