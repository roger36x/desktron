"""Desk 自控制入口只暴露显式登记的 navigation surface。"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "frontend/app-desk-control.js").read_text(encoding="utf-8")
BUNDLE = (ROOT / "desktron/lib/frontend_bundle.py").read_text(encoding="utf-8")


class DeskAppControlFrontend(unittest.TestCase):
    def test_module_is_bundled_without_editing_main_document_loader(self):
        self.assertIn('"app-desk-control.js"', BUNDLE)

    def test_sensitive_control_planes_are_protected(self):
        for selector in ("#panel-list", "#main-stage", "#settings-modal",
                         "#settings-btn", ".hs-sit-approvals", ".hs-approval"):
            self.assertIn(selector, JS)
        self.assertIn("data-app-control-protected", JS)
        self.assertIn("data-app-control-control-plane", JS)
        nav = (ROOT / "frontend/app-rail-nav.js").read_text(encoding="utf-8")
        self.assertIn('modal.setAttribute("data-app-control-protected", "true")', nav)
        self.assertIn("截图需逐次确认", JS)
        self.assertIn("隐藏会话正文、设置和审批内容", JS)

    def test_only_allowlisted_navigation_items_are_exposed(self):
        for item in ("toggle_panel", "arrangements", "apps"):
            self.assertIn(item, JS)
        self.assertNotIn("agents", JS)
        self.assertNotIn('["#tn-back", "back"', JS)
        self.assertNotIn('["#tn-fwd", "forward"', JS)
        self.assertNotIn("#panel-list .", JS)
        self.assertNotIn("#main-stage .", JS)
        self.assertIn("window.__deskAgentCommand", JS)
        self.assertIn('openStore("app")', JS)
        self.assertNotIn('openWorkshopPage(', JS)
        self.assertNotIn("settings-btn?.click", JS)

    def test_control_uses_switch_semantics_and_accessible_state_labels(self):
        # 2026-09-07 起控件是 .sm-switch 开关而不是文字按钮:状态用 on/off + aria-checked 表达,
        # 文字状态落在标题下的说明行(role=status),详见 test_frontend_desk_control_row.py。
        self.assertIn("const stateLabels", JS)
        self.assertIn('button.setAttribute("role", "switch")', JS)
        self.assertIn('button.setAttribute("aria-checked", on ? "true" : "false")', JS)
        self.assertIn('button.setAttribute("aria-label", button.title)', JS)
        self.assertIn('info.setAttribute("role", "status")', JS)
        self.assertIn('info.setAttribute("aria-live", "polite")', JS)

    def test_control_is_mounted_only_in_debug_settings(self):
        core = (ROOT / "frontend/app-settings-core.js").read_text(encoding="utf-8")
        self.assertIn('window.mountDeskControlSettings?.(gPref)', core)
        self.assertIn('row.dataset.settingsView = "debug"', JS)
        self.assertIn('host.appendChild(row); render();', JS)
        self.assertIn('button.className = "sm-switch"', JS)
        self.assertNotIn('#foot-backend', JS)
        self.assertNotIn('#top-nav', JS)
        self.assertNotIn('desk-cursor-icon', JS)
        self.assertIn('if (!bridge) return;', JS)

    def test_delegation_is_bound_to_current_chat_and_revoked_on_change(self):
        self.assertIn('startsWith("chat:")', JS)
        self.assertIn('action: "resourceChanged"', JS)
        self.assertIn('data-app-control-protected', JS)
        self.assertNotIn("lastChatPage", JS)
        self.assertIn("pendingSession", JS)
        self.assertIn("delegatedSession || pendingSession", JS)
        self.assertIn('state === "pending"', JS)
        self.assertIn("event.session_id", JS)
        self.assertIn("eventSession !== currentSession()", JS)
        self.assertIn("commandSession !== delegatedSession", JS)
        self.assertIn("commandSession !== currentSession()", JS)
        self.assertIn('state !== "delegated"', JS)
        self.assertIn("interactionEpoch += 1", JS)
        self.assertIn("Number(command?.interaction_epoch) !== interactionEpoch", JS)
        self.assertLess(JS.index("interactionEpoch += 1"), JS.index("window.__deskAgentCommand"))

    def test_human_input_excludes_control_plane_before_advancing_epoch(self):
        self.assertIn("event.isTrusted", JS)
        self.assertIn("[data-app-control-control-plane],.hs-approval,.hs-sit-approvals", JS)
        self.assertIn('state === "delegated"', JS)
        self.assertIn('state = "paused"', JS)


if __name__ == "__main__":
    unittest.main()
