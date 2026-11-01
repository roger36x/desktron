"""刚建、还没跑过第一回合的 GUI 会话也要出现在侧栏。

侧栏列表原来靠「meta 有登记 + 底层记录读得到」两个条件取休眠会话,而这两件都要等首帧:
引擎到那时才报 session_id,适配器据此登记 meta(engine_adapters._set_engine_sid);
claude 也要到那时才落 transcript。于是在工作流里新建一段会话之后,它既不在 meta 里、
也没有 transcript,侧栏要等聊过一轮并手动刷新才看得见(2026-08-17 用户实见)。

_gui_unstarted 绕开那两个前提,直接读适配器索引——create_session 当场就往里写了
id/title/domain/created。
"""
import os
import sys
import inspect
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
sys.path.insert(0, LIB)

_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import term
finally:
    sys.argv = _argv


class _Ad:
    def __init__(self, index, name="claude"):
        self.index = index
        self.name = name

    def list_sessions(self):
        return list(self.index.values())

    def turn_state(self, _sid):
        return {"state": "ready"}


class GuiUnstarted(unittest.TestCase):
    def setUp(self):
        self._ad, self._eng, self._title = term._adapter, term.GUI_ENGINES, term._custom_title_for
        term.GUI_ENGINES = ("claude", "codex")
        term._custom_title_for = lambda _sid: ""
        self.idx = {"claude": {}, "codex": {}}
        term._adapter = lambda e: _Ad(self.idx[e])

    def tearDown(self):
        term._adapter, term.GUI_ENGINES, term._custom_title_for = self._ad, self._eng, self._title

    def _rec(self, **kw):
        r = {"title": "", "domain": "wf-a", "time": {"created": 1700000000000}}
        r.update(kw)
        return r

    def test_未跑过的会话补进列表(self):
        self.idx["claude"] = {"adp_new": self._rec()}
        got = term._gui_unstarted([], set())
        self.assertEqual([x["id"] for x in got], ["adp_new"])
        self.assertEqual(got[0]["domain"], "wf-a")     # 归属带上,才会落到那个工作流下
        self.assertFalse(got[0]["alive"])

    def test_跑过的不再补(self):
        """有 engine_sid = 已经有稳定 id,上面那圈按 meta + transcript 列过了,重复会出两行。"""
        self.idx["claude"] = {"adp_old": self._rec(engine_sid="uuid-1")}
        self.assertEqual(term._gui_unstarted([], set()), [])

    def test_预分配_uuid_但未启动仍补进列表(self):
        """Pi 会在创建时预分配 UUID；engine_started 才是它有没有发出首轮的判据。"""
        self.idx["claude"] = {"adp_pi": self._rec(
            engine_sid="01a00000-0000-7000-8000-000000000000", engine_started=False)}
        self.assertEqual([x["id"] for x in term._gui_unstarted([], set())], ["adp_pi"])

    def test_已列出的不重复(self):
        self.idx["claude"] = {"adp_new": self._rec()}
        self.assertEqual(term._gui_unstarted([{"id": "adp_new"}], set()), [])

    def test_活着的不重复(self):
        self.idx["claude"] = {"adp_new": self._rec()}
        self.assertEqual(term._gui_unstarted([], {"adp_new"}), [])

    def test_没有标题时退到新会话(self):
        """这一档本来就还没有可取的名字;摆一串 adp_ 开头的十六进制,读的人认不出是哪一段。"""
        self.idx["claude"] = {"adp_new": self._rec()}
        self.assertEqual(term._gui_unstarted([], set())[0]["title"], "新会话")

    def test_有标题时用它(self):
        self.idx["claude"] = {"adp_new": self._rec(title="排查同步")}
        self.assertEqual(term._gui_unstarted([], set())[0]["title"], "排查同步")

    def test_多引擎各自补(self):
        self.idx["claude"] = {"adp_c": self._rec()}
        self.idx["codex"] = {"adp_x": self._rec()}
        got = term._gui_unstarted([], set())
        self.assertEqual({x["engine"] for x in got}, {"claude", "codex"})

    def test_某个引擎取不到索引不连累其余(self):
        def _boom(e):
            if e == "codex":
                raise RuntimeError("引擎不可用")
            return _Ad(self.idx[e])
        term._adapter = _boom
        self.idx["claude"] = {"adp_c": self._rec()}
        self.assertEqual([x["id"] for x in term._gui_unstarted([], set())], ["adp_c"])

    def test_索引里的坏记录跳过(self):
        self.idx["claude"] = {"adp_bad": None, "adp_ok": self._rec()}
        self.assertEqual([x["id"] for x in term._gui_unstarted([], set())], ["adp_ok"])

    def test_创建时刻用作排序键(self):
        self.idx["claude"] = {"adp_new": self._rec()}
        self.assertEqual(term._gui_unstarted([], set())[0]["time"], 1700000000000)

    def test_引擎锚点映射到Desk会话号(self):
        """会话页地址用 adp_，侧栏落盘行用引擎 id；列表必须把两者关联起来。"""
        self.idx["codex"] = {"adp_ui": self._rec(engine_sid="thread-1")}
        self.assertEqual(term._gui_session_id("codex", "thread-1"), "adp_ui")
        self.assertEqual(term._gui_session_id("codex", "thread-missing"), "")

    def test_落盘列表把Desk会话号作为别名返回(self):
        src = inspect.getsource(term._term_persisted_scan)
        self.assertIn('_gui_session_id(eng, sid, adapter_reverse.get(eng))', src)
        self.assertIn('item["session_id"] = session_id', src)

    def test_Shepherd_侧栏只返回公开SID和模式标识(self):
        old = term._shepherd
        try:
            term._shepherd = lambda: _Ad({"s": self._rec(
                id="adp_shepherd", engine="codex", title="牧羊会话")})
            got = term._shepherd_sessions([], set())
        finally:
            term._shepherd = old
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["id"], "adp_shepherd")
        self.assertEqual(got[0]["session_id"], "adp_shepherd")
        self.assertEqual(got[0]["engine"], "codex")
        self.assertTrue(got[0]["shepherd_mode"])
        self.assertNotIn("engine_sid", got[0])
        self.assertNotIn("binding_id", got[0])


if __name__ == "__main__":
    unittest.main()
