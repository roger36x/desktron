"""The native notification bridge has fixed content and cannot become a web capability."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_notification_bridge_is_main_frame_local_and_fixed_kind_only():
    source = (ROOT / 'desktop/native/main.swift').read_text(encoding='utf-8')
    block = source[source.index('if message.name == "desktopNotification"'):
                   source.index('if message.name == "approvalControl"')]
    assert 'message.frameInfo.isMainFrame' in block
    assert 'message.webView === web' in block
    assert 'origin.protocol == "http"' in block
    assert 'origin.host == "127.0.0.1"' in block
    assert 'origin.port == PORT' in block
    delivery = source[source.index('private func deliverDesktopNotification'):
                      source.index('// ---- 内置浏览器')]
    for kind in ('update_succeeded', 'update_failed'):
        assert kind in delivery
    assert 'body["title"]' not in block and 'body["body"]' not in block
    assert 'notificationHistoryKey' in delivery and 'pendingNotificationIDs' in delivery
    assert 'guard granted else {' in delivery
    assert delivery.index('pendingNotificationIDs.remove(eventID)', delivery.index('center.add(request)')) > delivery.index('center.add(request)')


def test_native_build_links_user_notifications_framework():
    launcher = (ROOT / 'desktron/bin/desktron').read_text(encoding='utf-8')
    assert '-framework UserNotifications' in launcher


def test_frontend_uses_stable_completion_identity_not_message_content():
    source = (ROOT / 'frontend/app-settings-sections.js').read_text(encoding='utf-8')
    assert '`update:${st.completion_id}:${st.completion_outcome}`' in source
    assert 'desktopNotification.postMessage' in source
