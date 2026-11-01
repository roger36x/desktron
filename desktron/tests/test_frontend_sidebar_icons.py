"""左栏顶级入口统一使用可缓存、可着色的 PNG 图标。"""
import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INDEX = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "frontend/style.css").read_text(encoding="utf-8")
LICENSE = (ROOT / "frontend/vendor/ICONS-LICENSE.txt").read_text(encoding="utf-8")


def _button(button_id):
    match = re.search(rf'<button[^>]+id="{button_id}"[^>]*>(.*?)</button>', INDEX, re.S)
    assert match, f"missing #{button_id}"
    return match.group(1)


def test_top_level_sidebar_icons_use_packaged_original_masks():
    assert 'class="fx-ic fx-ic-arrangements"' in _button("fx-tasks")
    assert 'class="fx-ic fx-ic-chat"' in _button("fx-new")
    assert 'class="fx-ic fx-ic-apps"' in _button("fx-apps")
    assert 'class="settings-icon"' in _button("settings-btn")
    for button_id in ("fx-tasks", "fx-new", "fx-apps", "settings-btn"):
        assert "<svg" not in _button(button_id)
    for name in ("sidebar-chat.png", "sidebar-apps.png", "sidebar-settings.png"):
        data = (ROOT / "frontend/icons" / name).read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        assert f'/icons/{name}' in CSS


def test_sidebar_icon_sources_are_original_with_historical_attribution():
    assert "Desktron 原创圆润界面图标" in LICENSE
    assert "设置使用V6加长齿形" in LICENSE
    assert "Flaticon条目仅记录替换前素材" in LICENSE
    for source in ("chat_8800042", "apps_8917404", "settings_2099058"):
        assert source in LICENSE
    assert LICENSE.count("作者 Graphics Plazza,风格 Basic Outline") >= 5
    assert "作者 Graphics Plazza,风格 Black Outline" in LICENSE
    assert "作者 Freepik" in LICENSE
    settings = ROOT / "frontend/icons/sidebar-settings.png"
    assert hashlib.sha256(settings.read_bytes()).hexdigest() == (
        "e7e9e5858bd9e23ca07f97f40cc2085491eb1a1fbe09ab5ac9a8a8f4ffb8730f"
    )


def test_css_icon_masks_do_not_use_fake_fixed_versions():
    assert not re.search(r'url\("/icons/[^"?]+\.png\?v=\d+', CSS)
    for name in (
        "bell.png", "shepherd.png", "task.png", "current-session-task.png",
        "arrangement.png", "status.png", "status-alert.png", "status-login.png",
        "sidebar-chat.png", "sidebar-apps.png", "sidebar-settings.png",
    ):
        assert f'/icons/{name}' in CSS
