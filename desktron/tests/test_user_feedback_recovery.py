"""用户反馈对应的前后端恢复入口回归。"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SETUP = (ROOT / "frontend/app-settings-setup.js").read_text(encoding="utf-8")


def test_setup_keeps_fresh_auth_link_retry_without_duplicate_side_summary():
    assert "本机概况" not in SETUP
    assert 'class="setup-side-panel"' not in SETUP
    assert 'data-setup-action="retry-auth"' in SETUP
    assert "back._setupAuthCommand" in SETUP
