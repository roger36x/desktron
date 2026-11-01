"""飞书引用应在会话独立侧栏预览，不进入浏览器 App。"""
import os
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
CORE = open(os.path.join(ROOT, "frontend", "app-core.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
PAGES = open(os.path.join(ROOT, "frontend", "app-pages.js"), encoding="utf-8").read()
SWIFT = open(os.path.join(ROOT, "desktop", "native", "main.swift"), encoding="utf-8").read()


class LarkMessageReferencePanel(unittest.TestCase):
    def test_message_link_is_intercepted_before_generic_page_open(self):
        route = CORE[CORE.index('if (/^https?:\\/\\//.test(href))'):]
        self.assertLess(route.index("hsLarkRefOpenFromLink"), route.index("openPage(href"))

    def test_panel_is_separate_from_review_and_apps(self):
        for token in ('class="hs-lr"', "function hsLarkRefOpen", "function hsLarkRefClose",
                      "function hsLarkRefWire"):
            self.assertIn(token, CHAT)
        self.assertIn("/workbench/lark-message?url=", CHAT)
        start = CHAT.index("async function hsLarkRefOpen")
        end = CHAT.index("function hsLarkRefClose", start)
        self.assertNotIn("showAppSide", CHAT[start:end])

    def test_review_and_lark_reference_are_mutually_exclusive(self):
        self.assertIn("if (v.larkRef) hsLarkRefClose(v);", CHAT)
        self.assertIn("if (v.rv) hsRvClose(v);", CHAT)

    def test_panel_has_loading_empty_error_and_mobile_states(self):
        for token in ("hs-lr-loading", "hs-lr-empty", "没有取到原消息", "在飞书中打开"):
            self.assertIn(token, CHAT + CSS)
        self.assertIn(".hs-page.lr-on", CSS)
        self.assertIn(".hs-page .hs-lr-back", CSS)

    def test_preview_uses_feishu_bubbles_reactions_and_light_theme(self):
        for token in ("hsLarkRefReactionItems", "hs-lr-bubble", "hs-lr-reactions",
                      "HS_LARK_REACTION_EMOJI"):
            self.assertIn(token, CHAT + CSS)
        light = CSS[CSS.index('html[data-theme="light"] .hs-lr'):]
        self.assertIn("--lr-canvas: #fff", light)
        self.assertIn("--lr-bubble: #f2f3f5", light)
        self.assertIn("--lr-bubble-own: #cfe1ff", light)
        self.assertNotIn("border-bottom", CSS[CSS.index(".hs-lr-msg {"):CSS.index(".hs-lr-avatar {")])

    def test_original_page_is_a_mode_of_the_same_panel(self):
        for token in ('data-mode="preview"', 'data-mode="original"',
                      "function hsLarkRefMode", 'nativeMode: "lark-message-reference"'):
            self.assertIn(token, CHAT)
        self.assertIn('s.startsWith("lark-ref:")', PAGES)
        self.assertNotIn("showAppSide", CHAT[CHAT.index("function hsLarkRefMode"):CHAT.index("function hsLarkRefLoading")])

    def test_web_context_requires_an_explicit_user_action(self):
        self.assertIn("模型默认不可见", CHAT)
        self.assertIn("加入下一条消息", CHAT)
        self.assertIn('type: "context", context_kind: "lark_web"', CHAT)
        self.assertIn('case "larkContext"', SWIFT)
        self.assertIn("message.frameInfo.isMainFrame", SWIFT)
        self.assertIn(".desktron-focused-chat-pane", SWIFT)


if __name__ == "__main__":
    unittest.main()
