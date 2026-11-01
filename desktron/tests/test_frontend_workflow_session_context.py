"""工作流会话点击必须把当前工作流带到终端 WebSocket。"""
import os
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class FrontendWorkflowSessionContext(unittest.TestCase):
    def _read(self, path):
        base = os.path.basename(path)
        if base in ("app-chat.js", "app-panel.js", "app-rail.js", "app-assets.js", "app-settings.js"):
            return _srcmap.frontend_src(base)   # 2026-08-27 分拆,拼接整体等价
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            return f.read()

    def test_term_row_passes_rendering_workflow(self):
        src = self._read("frontend/app-panel.js")
        start = src.index("function termRow(")
        body = src[start:src.index("async function closeTermSession", start)]
        self.assertIn('attachTerminal(s.id, s.engine, { domain: domId, cwd: s.cwd || "" })', body)
        self.assertIn('navPush({ kind: "sess", id: s.id, domain: domId })', body)

    def test_websocket_carries_context_domain(self):
        """终端 WS 仍带 context_domain(shell / 登录这类工具终端也经它附着)。

        原来还锁了主区 pane 的那个调用点(`termWsUrl(sessionId, engine, term, pane.contextDomain)`),
        那套 pane 随 TUI 退场撤了(2026-08-20);引擎会话的工作流上下文改由后端按会话取
        (CliAdapter.session_cwd),不再从这条 WS 的查询串上带。"""
        src = self._read("frontend/app-assets.js")
        start = src.index("function termWsUrl(")
        body = src[start:src.index("function wireTermInput", start)]
        self.assertIn("context_domain=${encodeURIComponent(contextDomain)}", body)


if __name__ == "__main__":
    unittest.main()
