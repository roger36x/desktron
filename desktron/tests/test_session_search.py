import json
import os
import sys
import tempfile
import threading
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
_argv = list(sys.argv)
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7765", "0", ROOT]
import session_search
from session_search import SessionSearch, history_around
sys.argv = _argv


class FakeAdapter:
    name = "fake"

    def __init__(self, root):
        self.root = root
        self._lock = threading.Lock()
        self.index = {"adp_test": {"title": "侧栏设计", "domain": "desk-iter",
                                    "time": {"updated": 123}}}
        self.history_args = None

    def _msg_path(self, sid):
        return os.path.join(self.root, sid + ".jsonl")

    def search_source_path(self, sid, record):
        return self._msg_path(sid)

    def search_documents(self, sid, record, path):
        return session_search.projection_documents(path)

    def history(self, sid, have, limit):
        self.history_args = (sid, have, limit)
        return [{"info": {"id": "msg_u_target", "role": "user"},
                 "parts": [{"type": "text", "text": "目标消息"}]}]

    def messages(self, sid):
        return []

    def _history_window(self, messages, have, limit):
        return []


class SessionSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = FakeAdapter(self.tmp.name)
        rows = [
            {"info": {"id": "msg_u_1", "role": "user", "at": 1},
             "parts": [{"type": "text", "text": "讨论右侧边栏的定位"}]},
            {"info": {"id": "msg_a_2", "role": "assistant", "at": 2},
             "parts": [{"type": "text", "text": "适合作为上下文面板"},
                       {"type": "tool", "state": {"output": "secret-tool-output"}}]},
            {"info": {"id": "msg_u_3", "role": "user", "at": 3},
             "parts": [{"type": "text", "text": "下一轮"}]},
        ]
        with open(self.adapter._msg_path("adp_test"), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.search = SessionSearch(os.path.join(self.tmp.name, "search.sqlite3"), engine="fake")
        snapshot = self.search.snapshot(self.adapter)
        self.search.total = len(snapshot)
        self.search._sync(snapshot)

    def tearDown(self):
        self.tmp.cleanup()

    def test_中文子串命中并返回稳定消息锚点(self):
        result = self.search.query("右侧边栏")
        self.assertEqual(1, len(result["results"]))
        row = result["results"][0]
        self.assertEqual("adp_test", row["session_id"])
        self.assertEqual("msg_u_1", row["hits"][0]["message_id"])
        self.assertEqual(1, row["hits"][0]["turn_from_end"])
        self.assertIn("右侧边栏", row["hits"][0]["snippet"])

    def test_工具输出不进入正文索引(self):
        self.assertEqual([], self.search.query("secret-tool-output")["results"])

    def test_锚点窗口只取目标附近并报告后续回合数(self):
        result = history_around(self.adapter, "adp_test", 9, before=6, after=6)
        self.assertEqual(3, result["newer"])
        self.assertEqual(("adp_test", 3, 13), self.adapter.history_args)
        self.assertEqual("msg_u_target", result["messages"][0]["info"]["id"])

    def test_新增回合后用消息标识校正偏移(self):
        history_around(self.adapter, "adp_test", 0, before=0, after=0,
                       message_id="msg_u_1")
        self.assertEqual(("adp_test", 1, 1), self.adapter.history_args)

    def test_相同会话号在不同引擎索引中严格隔离(self):
        other = FakeAdapter(self.tmp.name)
        other.name = "other"
        other_path = other._msg_path("adp_test")
        with open(other_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"info": {"id": "other_u", "role": "user", "at": 9},
                                 "parts": [{"type": "text", "text": "另一引擎独占内容"}]},
                                ensure_ascii=False) + "\n")
        search = SessionSearch(os.path.join(self.tmp.name, "other.sqlite3"), engine="other")
        snapshot = search.snapshot(other)
        search.total = len(snapshot)
        search._sync(snapshot)
        self.assertEqual([], self.search.query("另一引擎独占内容")["results"])
        self.assertEqual(1, len(search.query("另一引擎独占内容")["results"]))

    def test_索引器不再按引擎名分派解析器(self):
        with open(session_search.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn('adapter.name == "claude"', source)
        self.assertNotIn('adapter.name == "codex"', source)
        self.assertIn("adapter.search_source_path", source)
        self.assertIn("adapter.search_documents", source)


if __name__ == "__main__":
    unittest.main()


class SortAndExpandTest(unittest.TestCase):
    """结果排序与「展开更多命中」。

    卡片上写着「搜索到 37 处相关内容」,能看的却只有两条,而且那一行点不动
    (2026-09-04 用户实见)。每段会话多留几条,展开就不必再打一趟接口——经隧道
    一个往返一两秒,而这些片段本来就在这次结果里。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = FakeAdapter(self.tmp.name)
        # 两段会话:旧那段命中多(相关度靠前),新那段只有一条但时间最新。
        self.adapter.index = {
            "adp_old": {"title": "旧会话", "domain": "d", "time": {"updated": 100}},
            "adp_new": {"title": "新会话", "domain": "d", "time": {"updated": 200}},
        }
        def rows(n, base):
            return [{"info": {"id": "m%d" % i, "role": "user", "at": base + i},
                     "parts": [{"type": "text", "text": "锚点 %d" % i}]} for i in range(n)]
        for sid, n, base in (("adp_old", 6, 1000), ("adp_new", 1, 9000)):
            with open(self.adapter._msg_path(sid), "w", encoding="utf-8") as fh:
                for row in rows(n, base):
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.search = SessionSearch(os.path.join(self.tmp.name, "s.sqlite3"), engine="fake")
        snap = self.search.snapshot(self.adapter)
        self.search.total = len(snap)
        self.search._sync(snap)

    def tearDown(self):
        self.tmp.cleanup()

    def _by(self, sort):
        return {g["title"]: g for g in self.search.query("锚点", sort=sort)["results"]}

    def test_每段留够展开用的命中(self):
        """只留两条的话,「搜索到 6 处」那一行摊开来仍然只有两条,等于没展开。"""
        g = self._by("")["旧会话"]
        self.assertEqual(g["count"], 6)
        self.assertGreater(len(g["hits"]), 2)
        self.assertLessEqual(len(g["hits"]), SessionSearch.HITS_KEEP)

    def test_按时间排把最新的那段提前(self):
        order = [g["title"] for g in self.search.query("锚点", sort="time")["results"]]
        self.assertEqual(order[0], "新会话", "最新的命中没排到前面:%s" % order)

    def test_按时间排时卡内也按时间(self):
        """卡片之间按时间、卡片之内按相关度的话,读起来是两套秩序。"""
        hits = self._by("time")["旧会话"]["hits"]
        ats = [h["at"] for h in hits]
        self.assertEqual(ats, sorted(ats, reverse=True))

    def test_默认仍按相关度(self):
        """不传 sort 时的次序不能变——它是默认那一档。"""
        self.assertEqual([g["title"] for g in self.search.query("锚点")["results"]],
                         [g["title"] for g in self.search.query("锚点", sort="")["results"]])

    def test_内部字段不外泄(self):
        for g in self.search.query("锚点", sort="time")["results"]:
            self.assertNotIn("score", g)
            self.assertNotIn("newest", g, "排序用的中间量不该发给前端")
