from pathlib import Path

from _srcmap import settings_src


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


def test_usage_view_is_registered_and_bundled():
    settings = settings_src()
    bundle = (ROOT / "desktron" / "lib" / "frontend_bundle.py").read_text(encoding='utf-8')
    boot = (ROOT / "desktron" / "lib" / "ui_boot.py").read_text(encoding='utf-8')

    assert '{ id: "usage", title: "Token 用量" }' in settings
    assert '_routeSettingsSection(usage, "usage")' in settings
    assert '"app-settings-usage.js"' in bundle
    assert "from usage import api as usage_api" in boot
    assert boot.count("_WB_API =") == 1
    route_table = boot.split("_WB_API =", 1)[1].split("\n\n", 1)[0]
    assert "usage_api" in route_table


def test_usage_view_supports_filters_drilldown_and_local_only_copy():
    source = (FRONTEND / "app-settings-usage.js").read_text(encoding='utf-8')

    assert 'api("/workbench/usage?days=365"' in source
    assert 'data-days="30"' in source
    assert 'data-days="90"' in source
    assert 'data-days="365"' in source
    assert 'state.workflow = button.dataset.id' in source
    assert 'openDetail(row)' in source
    assert '导出 CSV' not in source
    assert 'function exportCsv()' not in source
    assert '可信统计只包含新版逐回合 Provider usage' in source
    assert '旧记录作为历史参考单列' in source
    assert '不扫描聊天正文' in source
    assert 'item.recorded_total' in source
    assert 'class="tu-day unavailable"' in source
    assert '数据始于' in source


def test_usage_styles_have_desktop_and_mobile_layouts():
    styles = (FRONTEND / "style.css").read_text(encoding='utf-8')

    source = (FRONTEND / "app-settings-usage.js").read_text(encoding='utf-8')

    assert '<div class="tu-heat-grid"><div class="tu-months"></div>' in source
    assert '<span class="tu-legend">少 ' + '<i></i>' * 5 + ' 多</span>' in source
    assert 'const chartStart = state.data.range.start' in source
    assert 'const selectedStart = cutoff()' in source
    assert 'if (day < selectedStart)' in source
    assert 'class="tu-day history"' in source
    assert 'grid.style.setProperty("--weeks", weeks); grid.dataset.range = state.range' in source
    assert 'seenMonths = new Set(); let lastMonthColumn = -2' in source
    assert 'if (column <= lastMonthColumn + 1) months.pop()' in source
    assert 'let heatRangeAtRight = 0' in source
    assert 'if (heatRangeAtRight !== state.range)' in source
    assert 'scroll.scrollLeft = Math.max(0, scroll.scrollWidth - scroll.clientWidth)' in source
    assert ".tu-heat-grid { --tu-cell: 10px; --tu-gap: 3px;" in styles
    assert '.tu-heat-grid[data-range="365"]' not in styles
    assert '.tu-heat-grid[data-range="30"] .tu-months' not in styles
    assert 'margin-left: auto' not in styles[styles.index('.tu-heat-body'):styles.index('.tu-weekdays')]
    assert ".tu-day.unavailable { pointer-events: none; }" in styles
    assert ".tu-day.history { opacity: .28; pointer-events: none; }" in styles
    assert ".tu-weekdays { position: sticky; left: 0;" in styles
    assert '.tu-heat-grid { --tu-cell: 10px; --tu-gap: 3px; }' in styles
    assert ".tu-heat-scroll {" in styles and "overflow-x: auto" in styles
    assert "-webkit-overflow-scrolling: touch" in styles
    assert ".tu-drawer" in styles
    assert ".tu-controls select { height: 24px; max-width: 108px; padding: 0 20px 0 6px;" in styles
    assert "font-size: 9px; }" in styles
    assert "@media (max-width: 600px)" in styles
    assert ".tu-summary" in styles


def test_heatmap_follows_github_contribution_graph():
    styles = (FRONTEND / "style.css").read_text(encoding='utf-8')
    source = (FRONTEND / "app-settings-usage.js").read_text(encoding='utf-8')
    for color in ("#151b23", "#033a16", "#196c2e", "#2ea043", "#56d364",
                  "#eff2f5", "#aceebb", "#4ac26b", "#2da44e", "#116329"):
        assert color in styles
    assert 'const cuts = [quart(.25), quart(.5), quart(.75)]' in source
    assert '<span>周一</span>' in source and '<span>周五</span>' in source
    assert '.tu-day[data-level="5"]' not in styles
