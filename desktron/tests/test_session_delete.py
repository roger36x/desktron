"""永久删除由后端单端点清适配器索引、终端 meta 与引擎原生记录。

删除失败时保留前端行与本地记录，避免刷新后复活且失去重试入口。
"""
import os
import re
import sys
import unittest
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _srcmap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", "/tmp/frontend", "7999", "7998", "/tmp/workspace"]
try:
    from session import api as session_api
finally:
    sys.argv = _argv

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _read(rel):
    import os as _os
    base = _os.path.basename(rel)
    if base in ("app-chat.js", "app-panel.js", "app-rail.js", "app-assets.js", "app-settings.js"):
        return _srcmap.frontend_src(base)   # 2026-08-27 分拆,拼接整体等价
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class DeleteClearsBothSides(unittest.TestCase):
    @staticmethod
    def _handler(owner):
        return SimpleNamespace(
            _adapter_owning=lambda sid: owner,
            _send_json=lambda body, code=200: (body, code),
        )

    def test_侧栏永久删除只发统一端点(self):
        js = _read("frontend/app-panel.js")
        fn = re.search(r"async function closeTermSession\(s, dormant\)[\s\S]*?\n}\n", js)
        self.assertIsNotNone(fn, "closeTermSession 不见了")
        body = fn.group(0)
        self.assertIn("/workbench/terminals/", body)
        self.assertNotIn("/session/", body)
        self.assertIn("alertLike", body)
        self.assertLess(body.index("return;", body.index("catch")), body.index("disposeTermPane"))

    def test_另一处删除入口同样只发统一端点(self):
        js = _read("frontend/app-assets.js")
        i = js.index('删除会话「${s.title || s.id}」？会清除其记录')
        seg = js[i:i + 1400]
        self.assertIn("/workbench/terminals/", seg)
        self.assertNotIn("/session/${encodeURIComponent(s.id)}", seg)
        self.assertIn("alertLike", seg)
        self.assertLess(seg.index("return;", seg.index("catch")), seg.index("row.remove()"))

    def test_后端永久删除同时清适配器和本地记录(self):
        owner = mock.Mock()
        h = self._handler(owner)
        with mock.patch.object(session_api, "_term_kill") as kill:
            result = session_api.handle(h, "/workbench/terminals/engine-1", "", "DELETE")
        self.assertEqual(({"ok": True}, 200), result)
        owner.delete_session.assert_called_once_with("engine-1")
        kill.assert_called_once_with("engine-1", keep=False)

    def test_关闭只停进程不清适配器(self):
        owner = mock.Mock()
        h = self._handler(owner)
        with mock.patch.object(session_api, "_term_kill") as kill:
            result = session_api.handle(h, "/workbench/terminals/adp_1", "keep=1", "DELETE")
        self.assertEqual(({"ok": True}, 200), result)
        owner.delete_session.assert_not_called()
        kill.assert_called_once_with("adp_1", keep=True)

    def test_忙碌冲突保留本地记录供重试(self):
        class Busy(RuntimeError):
            status = 409
            code = "session_busy"

        owner = mock.Mock()
        owner.delete_session.side_effect = Busy("会话仍在运行")
        h = self._handler(owner)
        with mock.patch.object(session_api, "_term_kill") as kill:
            result = session_api.handle(h, "/workbench/terminals/adp_1", "", "DELETE")
        self.assertEqual(409, result[1])
        self.assertEqual("session_busy", result[0]["code"])
        kill.assert_not_called()

    def test_压缩中删除不释放进程或清理记录(self):
        owner = mock.Mock(index={"adp_1": {"engine_sid": "native-1"}})
        owner.turn_state.return_value = {"state": "ready", "compacting": 100}
        with mock.patch.object(session_api, "_term_kill") as kill:
            body, status = session_api.handle(self._handler(owner),
                "/workbench/terminals/native-1", "", "DELETE")
        self.assertEqual(status, 409)
        owner.turn_state.assert_called_once_with("adp_1")
        owner.delete_session.assert_not_called()
        kill.assert_not_called()

    def test_关闭适配器失败时保留可见记录(self):
        owner = mock.Mock(index={"adp_1": {"engine_sid": "native-1"}})
        owner.abort.side_effect = RuntimeError("interrupt failed")
        with mock.patch.object(session_api, "_term_kill") as kill:
            body, status = session_api.handle(self._handler(owner),
                "/workbench/terminals/native-1", "keep=1", "DELETE")
        self.assertEqual(status, 500)
        owner.abort.assert_called_once_with("adp_1", recall=False)
        kill.assert_not_called()

    def test_pi_原生存储删除后刷新不再出现(self):
        from engines.stores import PiSessionStore
        import engines
        native = "11111111-2222-4333-8444-555555555555"
        for requested in ("adp_pi", native):
            with self.subTest(requested=requested), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / ("2026-09-11_" + native + ".jsonl")
                path.write_text(json.dumps({"type": "session", "id": native, "cwd": folder}) + "\n")
                store = PiSessionStore()
                owner = mock.Mock(index={"adp_pi": {"engine_sid": native}})
                owner.name = "pi"
                owner.turn_state.return_value = {"state": "ready"}
                owner.delete_session.side_effect = lambda sid: owner.index.pop(sid)
                with mock.patch.dict(os.environ, {"PI_CODING_AGENT_SESSION_DIR": folder}), \
                     mock.patch.object(engines.get("pi"), "store", store), \
                     mock.patch.object(session_api, "_term_kill"):
                    self.assertEqual(len(store.list_all()), 1)
                    body, status = session_api.handle(self._handler(owner),
                        "/workbench/terminals/" + requested, "", "DELETE")
                    self.assertEqual(status, 200, body)
                    self.assertFalse(path.exists())
                    self.assertEqual(store.list_all(), [])
                    owner.delete_session.assert_called_once_with("adp_pi")


class EmptyShellSession(unittest.TestCase):
    """建了没跑过首轮的会话(adp_ 号,没有 engine_sid)不是引擎侧锚点。

    TUI 退场前它的害处是被送去 resume:引擎起来了、却在自己的记录里找不到这个 id,
    停在选择器上报「No sessions match」(2026-08-17 用户实见)。现在没有 resume 这一步了,
    但两类 id 仍要分开走:adp_ 直接打开工作台自己的会话,其余先认领(/session/adopt)。
    """

    def test_两类_id_分开走(self):
        js = _read("frontend/app-assets.js")
        fn = re.search(r"async function attachTerminal\(sessionId, engine, opts\)[\s\S]*?\n}\n", js)
        self.assertIsNotNone(fn)
        seg = fn.group(0)
        i = seg.index("/^adp_/.test(sid)")
        self.assertIn("openHtmlSession(sid", seg[i:i + 120], "adp_ 直接开,不走认领")
        self.assertLess(i, seg.index("termToHtml(sid"), "认领是另一类 id 的路,排在后面")

    def test_resume_不再发起终端(self):
        """resumeSession 走的是同一个 attachTerminal,不再自己 POST 一个 resume 终端。"""
        js = _read("frontend/app-assets.js")
        fn = re.search(r"async function resumeSession\(sid, domainId, engine\)[\s\S]*?\n}\n", js)
        self.assertIsNotNone(fn, "resumeSession 不见了")
        body = fn.group(0)
        self.assertNotIn("resume: sid", body)
        self.assertIn('startsWith("adp_")', body)


if __name__ == "__main__":
    unittest.main()
