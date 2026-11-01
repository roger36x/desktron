"""Local opt-in for the authenticated Tailscale loopback entry.

The private token stays in the user's configuration, never API responses.
Saving does not start listeners, change Tailscale Serve or restart Desk.
Legacy environment/credential-file settings remain authoritative.
"""
import os
from pathlib import Path
import re
import secrets
import uuid

import durable_json

_RUNTIME = None


class SaveOutcomeUnknown(RuntimeError):
    """A save may have committed; read the current revision before any retry."""


def config_path():
    return Path.home() / '.config' / 'desktron' / 'tailscale.json'


def read_config():
    try:
        raw = durable_json.read_value(config_path())
        if not raw and not config_path().exists():
            return {'enabled': False, 'port': 7766, 'revision': 'new', 'token': ''}
        if (raw.get('schema') != 1 or type(raw.get('enabled')) is not bool
                or type(raw.get('port')) is not int or not 1024 <= raw['port'] <= 65535
                or not re.fullmatch(r'[0-9a-f]{32}', raw.get('revision', ''))
                or not isinstance(raw.get('token'), str)
                or (raw['token'] and not re.fullmatch(r'[A-Za-z0-9_-]{43}', raw['token']))
                or (raw['enabled'] and not raw['token'])):
            raise ValueError()
        return raw
    except (ValueError, TypeError, KeyError):
        raise ValueError('Tailscale 配置损坏，原文件已保留；请修复后再继续。') from None


def load_runtime():
    """Freeze the optional listener policy; startup logs failures and stays local."""
    global _RUNTIME
    if _RUNTIME is not None:
        raise RuntimeError('Tailscale 运行策略已固定，变更须重启。')
    try:
        _RUNTIME = read_config()
    except (OSError, ValueError):
        _RUNTIME = {'enabled': False, 'port': 0, 'token': '', 'failed': True}
        raise


def runtime_config():
    # Before HTTP startup, standalone setup callers still validate desired state.
    return dict(_RUNTIME) if _RUNTIME is not None else read_config()


def legacy_port():
    from credstore import _secrets_read
    raw = str(os.environ.get('DESKTRON_SERVE_PORT', '')).strip()
    return raw or str(_secrets_read().get('DESKTRON_SERVE_PORT', '')).strip()


def status(active_port=0):
    config = read_config()
    legacy = legacy_port()
    target = int(legacy) if legacy.isascii() and legacy.isdecimal() else config['port'] if config['enabled'] else 0
    return {'enabled': bool(legacy) or config['enabled'], 'port': target or config['port'],
            'revision': config['revision'], 'managed': not bool(legacy),
            'active_port': active_port or 0, 'restart_required': (active_port or 0) != target}


def save(value, expected_revision, ui_port=7765, active_port=0):
    if (not isinstance(value, dict) or set(value) != {'enabled', 'port'}
            or type(value.get('enabled')) is not bool or type(value.get('port')) is not int
            or not 1024 <= value['port'] <= 65535 or value['port'] == ui_port):
        raise ValueError('Tailscale 端口须为 1024–65535，且不能与 Desk 本机端口相同。')
    if legacy_port():
        raise ValueError('Tailscale 端口由环境变量或旧配置管理；请先移除 DESKTRON_SERVE_PORT 再使用此设置。')
    if value['enabled']:
        import remote_auth
        if remote_auth.bind_host() != '127.0.0.1':
            raise ValueError('请先将 DESKTRON_REMOTE_BIND 设为 loopback；此设置不会改变已有远程监听策略。')
    current = read_config()
    snapshot = durable_json.read(config_path())
    if (expected_revision != current['revision']
            or snapshot.get('revision', 'new') != current['revision']):
        raise durable_json.ConflictError('Tailscale 设置已更新，请刷新后再保存。')
    snapshot.update(schema=1, enabled=value['enabled'], port=value['port'],
                    revision=uuid.uuid4().hex,
                    token=current['token'] or (secrets.token_urlsafe(32) if value['enabled'] else ''))
    # A unique revision is the CAS field; never merge concurrent policies/tokens.
    try:
        durable_json.write(config_path(), snapshot)
        return status(active_port)
    except durable_json.ConflictError:
        raise
    except (OSError, ValueError):
        # replace may already have committed before fsync/readback failed.
        raise SaveOutcomeUnknown('保存结果未知，请刷新状态核对后再操作。') from None
