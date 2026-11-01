"""Frontend history recovery and no-role handoff regression tests."""
import os
import re
import unittest
import _srcmap


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
APP_ASSETS = os.path.join(ROOT, "frontend", "app-assets.js")


class FrontendSessionRecovery(unittest.TestCase):
    def setUp(self):
        self.js = _srcmap.assets_src()

    def test_resume_picker_lists_only_current_engine(self):
        # 现行设计:跨引擎交接场景退役,恢复列表只扫当前引擎(混引擎列表徒增认知)
        body = re.search(r"async function openResumePicker\(.*?\n}\n// resume", self.js, re.S).group(0)

        self.assertIn("const engines = [curEngine]", body)
        self.assertNotIn("TERMINAL_ENGINES", body)

    def test_resume_session_accepts_source_engine(self):
        """被点的那段会话未必属于当前偏好的引擎:引擎要一路带到认领请求里,
        否则后端拿错读法去找记录,认领失败、页面一片空白。"""
        self.assertRegex(self.js, r"async function resumeSession\(sid, domainId, engine\)")
        body = re.search(r"async function resumeSession\(.*?\n}\n", self.js, re.S).group(0)
        self.assertIn("attachTerminal(sid, engine || curEngine", body)

    def test_sidebar_title_is_authoritative_for_chat_header(self):
        panel = _srcmap.frontend_src("app-panel.js")
        assets = _srcmap.frontend_src("app-assets.js")
        chat = _srcmap.frontend_src("app-chat.js")

        self.assertIn("function _termListedTitle", panel)
        self.assertIn("_syncOpenChatTitles();", panel)
        attach = re.search(r"async function attachTerminal\(.*?\n}\n", assets, re.S).group(0)
        self.assertIn("_termListedTitle(sid)", attach)
        self.assertIn("openHtmlSession(sid, listedTitle, true, opts || {})", attach)
        adopt = re.search(r"async function termToHtml\(.*?\n}\n\nasync function openHtmlSession", chat, re.S).group(0)
        self.assertIn("listedTitle || r.title || \"对话\"", adopt)

    def test_history_rows_offer_no_role_continuation(self):
        self.assertIn("function openResumeChoice", self.js)
        picker = re.search(r"async function openResumePicker\(.*?\n}\n// resume", self.js, re.S).group(0)

        self.assertIn("openResumeChoice(s, domId)", picker)
        self.assertIn("无角色接续（新会话）", self.js)
        self.assertIn("恢复原会话", self.js)

    def test_running_session_directly_opens_but_history_uses_choice(self):
        picker = re.search(r"async function openResumePicker\(.*?\n}\n// resume", self.js, re.S).group(0)

        self.assertIn("if (isLive && s.engine === curEngine) resumeSession(s.id, domId, s.engine)", picker)
        self.assertIn("else openResumeChoice(s, domId)", picker)

    def test_handoff_shows_progress_and_renames_new_session(self):
        self.assertIn("function showHandoffProgress", self.js)
        self.assertIn("读取旧会话", self.js)
        self.assertIn("整理旧会话摘要", self.js)
        self.assertIn("创建无角色会话", self.js)
        self.assertIn("无角色接续：", self.js)
        self.assertIn("/workbench/terminals/" , self.js)

    def test_handoff_primary_is_limited_to_supported_sources(self):
        self.assertIn("function canHandoffSource", self.js)
        # 能交接的来源 = 后端读得懂其记录的引擎:claude 与 codex 家族
        self.assertIn('engine === "claude" || engineIsCodexLike(engine)', self.js)
        choice = re.search(r"function openResumeChoice\(.*?\n}\n\nfunction cancelHandoffProgress", self.js, re.S).group(0)

        self.assertIn("暂不支持读取该引擎的历史", choice)
        self.assertIn("if (canHandoff)", choice)

    def test_handoff_progress_has_cancel_and_does_not_hijack_after_navigation(self):
        self.assertIn("function cancelHandoffProgress", self.js)
        self.assertIn("handoff-cancel", self.js)
        self.assertIn("_handoffRun", self.js)
        self.assertIn("if (run !== _handoffRun) return", self.js)

    def test_handoff_uses_recovery_timeouts(self):
        body = re.search(r"async function continueSessionInCurrentEngine\(.*?\n}\n\nasync function resumeSession", self.js, re.S).group(0)

        # 交接那一步(蒸馏源会话)仍是一次长请求;建会话已改走 newHtmlSessionWithPrompt,
        # 超时由它自己那条路管,这里不再各写一个数。
        self.assertIn("timeout: 45000", body)

    def test_handoff_does_not_pull_user_back_from_other_views(self):
        body = re.search(r"async function continueSessionInCurrentEngine\(.*?\n}\n\nasync function resumeSession", self.js, re.S).group(0)

        self.assertIn("!($(\"#terminal\")?.hidden)", body)

    def test_handoff_progress_has_loading_animation(self):
        css_path = os.path.join(ROOT, "frontend", "style.css")
        with open(css_path, encoding="utf-8") as f:
            css = f.read()

        self.assertIn("aria-live", self.js)
        self.assertIn("@keyframes handoff-pulse", css)
        self.assertIn("@keyframes handoff-sweep", css)
        self.assertIn("animation: handoff-pulse", css)
        self.assertIn("animation: handoff-sweep", css)

    def test_handoff_creates_a_gui_session(self):
        """无角色接续必须新建 GUI 会话并把整理后的 seed prompt 作为首句发出。"""
        body = re.search(r"async function continueSessionInCurrentEngine\(.*?\n}\n\nasync function resumeSession", self.js, re.S).group(0)
        self.assertIn('newHtmlSessionWithPrompt({ domain: domainId, desk_role: "" }, handoff.prompt)', body)
        self.assertNotIn('api("/workbench/terminals"', body)
        self.assertNotIn("fork_session", body)
        self.assertNotIn("resumeSession(", body)

    def test_empty_terminal_view_clears_handoff_progress_watermark(self):
        # 按函数体本身取,不按「后面紧跟哪个函数」取:后者会被任何一次在它下面新增函数
        # 弄失配(2026-08-10 实见:新增 _termWakeOn 之后这条直接报 NoneType)。
        watermark = re.search(r"function _showWatermark\(.*?\n}\n", self.js, re.S).group(0)
        sync = re.search(r"async function syncTermView\(.*?\n}\n\nasync function openAsset", self.js, re.S).group(0)

        self.assertIn("function _isHandoffProgressVisible", self.js)
        self.assertIn('_termWatermark.className = "term-pane term-watermark"', watermark)
        self.assertIn("_isHandoffProgressVisible()", sync)
        self.assertIn("clearTerminalView(); renderSessionPanel();", sync)

    def test_resume_不再先把进程拉起来(self):
        """引擎侧锚点直接认领进 GUI(/session/adopt),不再 POST 一个 resume 终端。
        原来那一步是为了让 TUI 有 PTY 可附着,而 GUI 会话的进程是按回合起的。"""
        body = re.search(r"async function resumeSession\(.*?\n}\n", self.js, re.S).group(0)
        self.assertNotIn('api("/workbench/terminals"', body)
        self.assertNotIn("resume: sid", body)
        self.assertIn('/reopen`, { method: "POST" }', body)
        self.assertIn("attachTerminal(sid, engine || curEngine", body)

    def test_handoff_progress_is_bound_to_target_domain(self):
        progress = re.search(r"function showHandoffProgress\(.*?\n}\n\nfunction showHandoffError", self.js, re.S).group(0)
        body = re.search(r"async function continueSessionInCurrentEngine\(.*?\n}\n\nasync function resumeSession", self.js, re.S).group(0)

        self.assertIn("let _handoffDomain", self.js)
        self.assertIn("_handoffDomain && curDomainId() !== _handoffDomain", progress)
        self.assertIn("_handoffDomain = domainId", body)
        self.assertIn("curDomainId() === domainId", body)

    def test_resume_picker_reports_engine_scan_failures(self):
        picker = re.search(r"async function openResumePicker\(.*?\n}\n// resume", self.js, re.S).group(0)

        self.assertIn("scanErrors", picker)
        self.assertIn("扫描失败", picker)
        self.assertIn("timeout: 30000", picker)

    def test_resume_picker_includes_terminal_list_fallback(self):
        picker = re.search(r"async function openResumePicker\(.*?\n}\n// resume", self.js, re.S).group(0)

        self.assertIn("fallbackTerms", picker)
        self.assertIn("source: 'terminals'", picker)

    def test_chat_resume_picker_excludes_workflow_sessions(self):
        picker = re.search(r"async function openResumePicker\(.*?\n}\n// resume", self.js, re.S).group(0)

        self.assertIn("const chatExcluded = new Set()", picker)
        self.assertIn("(t.refs || []).length", picker)
        self.assertIn("chatExcluded.has(s.engine + ':' + id)", picker)

    def test_resume_picker_keeps_source_domain_but_targets_current_domain(self):
        picker = re.search(r"async function openResumePicker\(.*?\n}\n// resume", self.js, re.S).group(0)
        choice = re.search(r"function openResumeChoice\(.*?\n}\n\nfunction cancelHandoffProgress", self.js, re.S).group(0)

        self.assertRegex(self.js, r"async function openResumePicker\(anchor, domainId, sourceDomainIds\)")
        self.assertIn("scanDomains", picker)
        self.assertIn("sourceDomain: scanDomain", picker)
        self.assertIn("s.sourceDomain !== domId", picker)
        self.assertIn("const sourceSid = s.resume_id || s.id", choice)
        self.assertIn("continueSessionInCurrentEngine(s.engine, sourceSid, domId", choice)
        self.assertIn("resumeSession(sourceSid, s.sourceDomain || domId, s.engine)", choice)

    def test_resume_picker_terminal_fallback_overrides_scanned_title(self):
        picker = re.search(r"async function openResumePicker\(.*?\n}\n// resume", self.js, re.S).group(0)

        self.assertIn("const seen = new Map()", picker)
        self.assertIn("const hit = keys.find((k) => k && seen.has(k))", picker)
        self.assertIn("existing.title = s.title", picker)
        self.assertIn("existing.source = s.source", picker)


if __name__ == "__main__":
    unittest.main()
