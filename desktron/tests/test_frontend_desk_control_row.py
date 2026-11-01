"""设置·调试视图里「Desk 导航控制」与「调试」两节同一版式:标题行 + 右侧开关。

2026-09-07 用户实报:调试是「图标 + 文字 + 开关」一行,紧接着一张卡片里却是
「标题 / 状态 / 长说明 / 矩形按钮」四段,两个相邻的开关式功能长成两种控件。
要求改成跟调试一样,包括那个开关。

改法:
  - app-desk-control.js 不再往调试的卡片里塞 .sm-desk-control 行,而是自建一个
    .sm-section(标题行 .sm-title = .sm-title-ic 图标 + 文字 + .sm-switch),挂在调试之后,
    data-settings-view=debug 让它跟着同一个视图显隐;说明与状态合成一行 .sm-hint。
  - 调试的 .sm-card 空了,由既有的 .sm-card:empty { display:none } 收掉,不留空框。
  - 开关语义:delegated / paused → on;idle / error → off;pending → disabled。
    点击动作不变(idle/error → delegate,delegated/paused → takeover),仍经 bridge.postMessage。
  - 图标用新增的 ICONS.sidebar(窗框 + 竖线,与顶栏 #tn-side 同形):这一节交出去的正是侧栏导航。

WebKit 探针实测(1280×800,设置中心 debug 视图,源码直接路由进运行页):
  调试标题行   left 453  top 257  w 558  h 22   16px / 620   开关 977,258  34×20
  导航控制标题 left 453  top 307  w 558  h 22   16px / 620   开关 977,308  34×20
  调试卡片 display none;说明行 43px 高落在标题下 11px 处。
  开关三态:点击 → posted delegate、pending 禁用;delegated 事件 → on/aria-checked=true;
  再点 → posted takeover;revoked 事件 → off。
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "frontend/app-desk-control.js").read_text(encoding="utf-8")
CORE = (ROOT / "frontend/app-settings-core.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend/style.css").read_text(encoding="utf-8")
ICONS = (ROOT / "frontend/app-core.js").read_text(encoding="utf-8")


class DeskControlRowMatchesDebugRow(unittest.TestCase):
    def test_section_mirrors_debug_title_row(self):
        self.assertIn('row.className = "sm-section sm-desk-control"', JS)
        self.assertIn('title.className = "sm-title"', JS)
        self.assertIn('icon.className = "sm-title-ic"', JS)
        self.assertIn('title.append(icon, document.createTextNode("Desk 导航控制"), button)', JS)
        self.assertIn('button.className = "sm-switch"', JS)
        # 开关靠右:调试那枚是 JS 内联 margin-left:auto,这里由 CSS 承担,效果一致。
        self.assertIn(".sm-desk-control > .sm-title .sm-switch { margin-left: auto; }", CSS)

    def test_section_follows_debug_and_shares_its_view(self):
        self.assertIn('row.dataset.settingsView = "debug"', JS)
        self.assertLess(CORE.index("gPref.appendChild(dbg);"),
                        CORE.index("window.mountDeskControlSettings?.(gPref)"))
        # 空卡片靠既有规则隐藏,不再需要往调试卡片里塞东西。
        self.assertIn(".sm-card:empty { display: none; }", CSS)

    def test_switch_state_mapping(self):
        self.assertIn('const on = state === "delegated" || state === "paused";', JS)
        self.assertIn('button.classList.toggle("on", on);', JS)
        self.assertIn('button.disabled = !session || state === "pending";', JS)
        self.assertIn(".sm-desk-control .sm-switch:disabled { opacity: .45; cursor: default; }", CSS)

    def test_status_and_description_share_one_hint_line(self):
        self.assertIn('info.className = "sm-hint"', JS)
        self.assertIn("const description = ", JS)
        self.assertIn('info.textContent = `${!session ? "请先打开一个会话" : stateLabels[state] || stateLabels.idle}。${description}`', JS)
        self.assertIn(".settings-center-live .sm-desk-control > .sm-hint { margin-bottom: 0; }", CSS)

    def test_old_card_layout_is_gone(self):
        for stale in ("const buttonLabels", "sm-link-btn", "aria-pressed", "buttonLabels[state]"):
            self.assertNotIn(stale, JS, stale)
        for stale in (".sm-desk-control strong", ".sm-desk-control p {",
                      ".sm-desk-control button", ".sm-desk-control > div"):
            self.assertNotIn(stale, CSS, stale)

    def test_sidebar_icon_keeps_reviewed_window_and_divider(self):
        import json
        import xml.etree.ElementTree as ET
        m = re.search(r'^  sidebar: (".*"),$', ICONS, re.M)
        self.assertIsNotNone(m)
        root = ET.fromstring(json.loads(m.group(1)))
        ns = {'s': 'http://www.w3.org/2000/svg'}
        self.assertEqual(root.attrib['viewBox'], '0 0 32 32')
        rect = root.find('.//s:rect', ns)
        self.assertEqual((rect.attrib['width'], rect.attrib['height'], rect.attrib['rx']), ('25', '22', '5'))
        self.assertEqual(root.find('.//s:path', ns).attrib['d'], 'M12 5.8v20.4')
        self.assertIn("ICONS.sidebar", JS)


if __name__ == "__main__":
    unittest.main()
