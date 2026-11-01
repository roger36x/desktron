"""Embedded Codex-like terminal redraw regressions."""
import os
import re
import unittest
import _srcmap


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class FrontendEmbedRedrawTest(unittest.TestCase):
    def _embed_body(self, relative_path, function_name):
        source = _srcmap.frontend_src(os.path.basename(relative_path))   # 2026-08-27 分拆,按名路由
        match = re.search(
            rf"async function {function_name}\(.*?\n  }}\n",
            source,
            re.S,
        )
        self.assertIsNotNone(match)
        return match.group(0)

    def test_workflow_embed_requests_codex_like_redraw_after_layout_settles(self):
        body = self._embed_body("frontend/app-assets.js", "embedTerminal")

        # codex 家族的 fork 同样适用 TUI 重画这套契约:判据按家族,不写死引擎名
        self.assertIn("const isCodexLike = engineIsCodexLike(engine)", body)
        self.assertIn("sendResize(true)", body)
        self.assertIn("payload.redraw = true", body)
        self.assertIn("document.fonts.ready.then", body)
        self.assertIn("settleCodexTui();", body)
        self.assertIn("redrawCodexPromptWhenReady();", body)
        self.assertIn("buffer.cursorY", body)
        self.assertIn("promptRedrawDone = true", body)

    def test_shared_embed_has_same_redraw_contract(self):
        body = self._embed_body("frontend/term-embed.js", "embed")

        # 共享模块不依赖主 app 全局,家族名单在模块内自带一份(CODEX_LIKE)
        self.assertIn("const isCodexLike = CODEX_LIKE.includes(engine)", body)
        self.assertIn("sendResize(true)", body)
        self.assertIn("payload.redraw = true", body)
        self.assertIn("clearTimeout(settleTimer)", body)
        self.assertIn("redrawCodexPromptWhenReady();", body)
        self.assertIn("promptRedrawDone = true", body)


if __name__ == "__main__":
    unittest.main()
