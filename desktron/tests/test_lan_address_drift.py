"""The settings page must not offer a QR code for an address the machine lost."""
from pathlib import Path

import pytest
import lan_settings as settings
import remote_auth

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'config_path', lambda: tmp_path / 'lan.json')
    monkeypatch.delenv(remote_auth.BIND_ENV, raising=False)


def _status(monkeypatch, found, error='', active='192.168.1.10/24'):
    monkeypatch.setattr(settings, 'candidates', lambda: (found, error))
    return settings.status(active)


def test_listener_on_lost_address_is_reported(monkeypatch):
    assert _status(monkeypatch, ['192.168.1.20/24'])['active_missing'] is True


def test_listener_on_owned_address_is_not_reported(monkeypatch):
    # Only the IP matters; a different detected prefix is not a lost address.
    assert _status(monkeypatch, ['192.168.1.10/16'])['active_missing'] is False


def test_unknown_detection_or_inactive_listener_is_not_reported(monkeypatch):
    assert _status(monkeypatch, [], error='本机网卡读取失败')['active_missing'] is False
    assert _status(monkeypatch, ['192.168.1.20/24'], active='')['active_missing'] is False


def test_legacy_bind_still_reports_lost_address(monkeypatch):
    monkeypatch.setenv(remote_auth.BIND_ENV, 'lan:192.168.1.10/24')
    state = _status(monkeypatch, ['192.168.1.20/24'])
    assert state['managed'] is False and state['active_missing'] is True


def test_settings_page_withholds_qr_and_explains_lost_address():
    source = (ROOT / 'frontend/app-settings-core.js').read_text(encoding='utf-8')
    assert 'const lanStale = Boolean(state.lan_config?.active_missing);' in source
    assert 'if (state.enabled && !lanStale) entries.push(' in source
    assert 'feedback.textContent = config.active_missing' in source
    english = (ROOT / 'frontend/app-i18n-en.js').read_text(encoding='utf-8')
    for key in ('本机地址已变化，局域网入口仍在监听 {address}，手机无法连接。请更新地址并重启 Desk。',
                '局域网入口监听的地址已不属于本机，二维码已停用。请按下方提示更新地址并重启 Desk。'):
        assert '"' + key + '"' in english
