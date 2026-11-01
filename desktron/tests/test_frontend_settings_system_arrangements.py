"""设置页内的系统安排详情导航回归。"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SETTINGS = (ROOT / "frontend/app-settings-core.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend/style.css").read_text(encoding="utf-8")


def test_system_arrangement_details_stay_inside_settings_content():
    block = SETTINGS[
        SETTINGS.index("function _paintSystemArrangementsSettings"):
        SETTINGS.index("async function renderTaskStatsSection")
    ]
    assert 'card.innerHTML = \'<div class="sm-system-arrangement-detail"></div>\'' in block
    assert "_arrSysRenderResource(body, active, back, repaint" in block
    assert 'bodyClass: "sm-system-arrangement-detail arrangements-page arr-sys-resource-page"' in block
    assert '_arrSheet(item.name, "系统安排")' not in block
    assert "_arrCloseSheet(modal)" not in block


def test_memory_resource_uses_the_inline_detail_route():
    block = SETTINGS[
        SETTINGS.index("function _paintSystemArrangementsSettings"):
        SETTINGS.index("async function renderTaskStatsSection")
    ]
    assert 'item.kind === "memory"' in block
    assert 'item.kind === "lark"' not in block
    assert "_paintSystemArrangementsSettings(section, status, item.kind)" in block
    assert '_paintSystemArrangementsSettings(section, status, "")' in block
    assert 'backLabel: DeskI18n.tx("系统安排")' in block


def test_inline_detail_uses_the_settings_page_surface():
    assert '.settings-center-live .sm-system-arrangement-detail-open > .sm-title' in CSS
    assert '.settings-center-live .sm-system-arrangement-detail-open > .sm-card' in CSS
    assert "background: transparent" in CSS[
        CSS.index('.settings-center-live .sm-system-arrangement-detail-open > .sm-card'):
        CSS.index('.settings-center-live .sm-system-arrangement-detail-open > .sm-card') + 260
    ]
