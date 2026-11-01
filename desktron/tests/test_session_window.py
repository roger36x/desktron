"""铺几个回合只有一个口径。

搬迁(import_session)按 SESSION_WINDOW 重写存储,取消息(GET /session/<id>/message)按同一个数
截断。此前两处不一样——一个 25、一个 50:存储里回合多于 25 的会话打开时先铺出 50 个,空闲时
的对账走一次 adopt 把存储重写成 25,刻度那一列跟着缩回去。用户看到的是「点开一个会话,偶尔
加载出超过默认条数的历史」(2026-08-16)。本机实测有 9 段会话存了 26~57 个回合,都会撞上。

更早的回合并没有丢:往上拉到顶按 30 个一批经 /history 现取(见 CliAdapter.history)。
"""
import json
import os
import re
import sys
import tempfile
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv

ADAPTERS = _srcmap.engine_adapters_src()
UI_SERVER = _srcmap.ui_server_src()


class OneWindow(unittest.TestCase):
    def test_常量存在且是小数(self):
        self.assertTrue(hasattr(ea, "SESSION_WINDOW"))
        self.assertGreater(ea.SESSION_WINDOW, 0)
        self.assertLessEqual(ea.SESSION_WINDOW, 60)

    def test_搬迁的默认上限取这个常量(self):
        import inspect
        for cls in (ea.ClaudeAdapter, ea.CodexAdapter):
            sig = inspect.signature(cls.import_session)
            self.assertEqual(sig.parameters["limit"].default, ea.SESSION_WINDOW,
                             "%s.import_session 的默认上限脱钩了" % cls.__name__)

    def test_取消息也用这个常量_没有写死的数(self):
        """2026-08-20 起截窗口本身挪进了 messages_window_json(那条路要按文件戳缓存,
        截完再缓存才有意义),路由只负责把常量传进去。"""
        m = re.search(r'seg\[3\] == "message" and method == "GET":[\s\S]+?\n            return\n',
                      UI_SERVER)
        self.assertIsNotNone(m)
        seg = m.group(0)
        self.assertIn("messages_window_json(seg[2], SESSION_WINDOW)", seg)
        self.assertNotIn("> 50", seg)
        self.assertIn("from engine_adapters import SESSION_WINDOW", UI_SERVER)

    def test_截窗口的实现只有一处(self):
        """路由里不重复截取；适配器统一委托 turn-offset 索引按窗口读取。"""
        self.assertNotIn("starts[-SESSION_WINDOW:]", UI_SERVER)
        self.assertIn("self._turn_index(sid).raw_window(limit=window)", ADAPTERS)

    def test_按回合切窗_不按消息条数(self):
        """一个回合含一条发言与若干条助手消息,按消息数切会把回合从中间劈开。"""
        msgs = []
        for i in range(ea.SESSION_WINDOW + 8):
            msgs.append({"info": {"role": "user", "id": "u%d" % i},
                         "parts": [{"type": "text", "text": "问 %d" % i}]})
            msgs.append({"info": {"role": "assistant", "id": "a%d" % i},
                         "parts": [{"type": "text", "text": "答 %d" % i}]})
        starts = [i for i, m in enumerate(msgs) if ea.ClaudeAdapter._turn_start(m)]
        self.assertEqual(len(starts), ea.SESSION_WINDOW + 8)
        # 驱动真的那份实现,不在用例里照抄一遍算法——照抄的话实现改了这里也不会红
        import json, tempfile
        with tempfile.TemporaryDirectory() as d:
            ad = ea.ClaudeAdapter(d, d, type("B", (), {"publish": lambda *a: None})())
            sid = ad.create_session()["id"]
            with open(ad._msg_path(sid), "w", encoding="utf-8") as f:
                for m in msgs:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
            out = json.loads(b"[" + ad.messages_window_json(sid, ea.SESSION_WINDOW) + b"]")
        kept = [m for m in out if ea.ClaudeAdapter._turn_start(m)]
        self.assertEqual(len(kept), ea.SESSION_WINDOW)
        self.assertEqual((out[0].get("info") or {}).get("role"), "user")   # 从一条发言开始,不从半截


if __name__ == "__main__":
    unittest.main()
