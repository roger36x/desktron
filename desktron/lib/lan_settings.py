"""Opt-in RFC1918 listener configuration; saving never opens a socket or firewall.

The server captures policy once before starting listeners. Desired settings can
change on disk without widening the active source network or removing its token.
Legacy DESKTRON_REMOTE_BIND remains authoritative, including explicit loopback.
"""
import ipaddress
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import json
import uuid

import durable_json

_RUNTIME = None
_PRIVATE = tuple(ipaddress.IPv4Network(net) for net in
                 ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


class SaveOutcomeUnknown(RuntimeError):
    """Read current state before attempting another write."""


def config_path():
    return Path.home() / '.config' / 'desktron' / 'lan.json'


def parse_interface(value):
    try:
        if not isinstance(value, str) or '/' not in value:
            raise ValueError()
        interface = ipaddress.IPv4Interface(value)
        if not any(interface.network.subnet_of(net) for net in _PRIVATE):
            raise ValueError()
        if (interface.network.prefixlen < 31
                and interface.ip in (interface.network.network_address, interface.network.broadcast_address)):
            raise ValueError()
        return interface
    except (ValueError, TypeError):
        raise ValueError('局域网地址须为本机私有 IPv4/前缀，例如 192.168.1.10/24。') from None


def read_config():
    try:
        raw = durable_json.read_value(config_path())
        if not raw and not config_path().exists():
            return {'enabled': False, 'interface': '', 'revision': 'new', 'token': ''}
        if (raw.get('schema') != 1 or type(raw.get('enabled')) is not bool
                or not isinstance(raw.get('interface'), str)
                or not re.fullmatch(r'[0-9a-f]{32}', raw.get('revision', ''))
                or not isinstance(raw.get('token'), str)
                or (raw['token'] and not re.fullmatch(r'[A-Za-z0-9_-]{43}', raw['token']))
                or (raw['enabled'] and (not raw['token'] or not raw['interface']))):
            raise ValueError()
        if raw['interface']:
            parse_interface(raw['interface'])
        return raw
    except (ValueError, TypeError, KeyError):
        raise ValueError('局域网配置损坏，原文件已保留；请修复后再继续。') from None


def load_runtime():
    """Call once before the primary server admits requests, never from save()."""
    global _RUNTIME
    if _RUNTIME is not None:
        raise RuntimeError('局域网运行策略已固定，变更须重启。')
    try:
        _RUNTIME = read_config()
    except (OSError, ValueError):
        _RUNTIME = {'enabled': False, 'interface': '', 'token': '', 'failed': True}
        raise


def runtime_config():
    return dict(_RUNTIME or {'enabled': False, 'interface': '', 'token': ''})


def legacy_bind():
    return os.environ.get('DESKTRON_REMOTE_BIND', '').strip().lower()


def candidates():
    """Local interface addresses only; no hostnames, account or adapter names."""
    try:
        if os.name == 'nt':
            command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                       'Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | '
                       'Where-Object { $_.AddressState -eq "Preferred" } | '
                       'Select-Object IPAddress,PrefixLength | ConvertTo-Json -Compress']
            result = subprocess.run(command, capture_output=True, encoding='utf-8', timeout=8)
            if result.returncode:
                raise OSError()
            rows = json.loads(result.stdout or '[]')
            if isinstance(rows, dict):
                rows = [rows]
            values = [str(row['IPAddress']) + '/' + str(row['PrefixLength']) for row in rows]
        elif __import__('sys').platform == 'darwin':
            result = subprocess.run(['/sbin/ifconfig'], capture_output=True, encoding='utf-8', timeout=5)
            if result.returncode:
                raise OSError()
            values = []
            for address, mask in re.findall(r'\binet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-f]+)', result.stdout):
                netmask = str(ipaddress.IPv4Address(int(mask, 16)))
                values.append(str(ipaddress.IPv4Interface(address + '/' + netmask)))
        else:
            result = subprocess.run(['ip', '-j', '-4', 'address', 'show', 'up'],
                                    capture_output=True, encoding='utf-8', timeout=5)
            if result.returncode:
                raise OSError()
            values = [str(addr['local']) + '/' + str(addr['prefixlen'])
                      for row in json.loads(result.stdout or '[]') for addr in row.get('addr_info', [])
                      if addr.get('family') == 'inet']
        found = set()
        for value in values:
            try:
                found.add(str(parse_interface(value)))
            except ValueError:
                pass
        return sorted(found), ''
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        return [], '本机网卡读取失败，请手动填写私有 IPv4/前缀并核对网络。'


def status(active_interface=''):
    config = read_config()
    legacy = legacy_bind()
    target = legacy[4:] if legacy.startswith('lan:') else '' if legacy else (
        config['interface'] if config['enabled'] else '')
    available, error = candidates()
    # The listener is bound once at startup; a DHCP change leaves it on an
    # address this machine no longer owns. Unknown detection is not "missing".
    active_ip = (active_interface or '').split('/', 1)[0]
    missing = bool(active_ip and not error
                   and active_ip not in {value.split('/', 1)[0] for value in available})
    return {'enabled': bool(target), 'interface': target or config['interface'],
            'revision': config['revision'], 'managed': not bool(legacy),
            'active_interface': active_interface or '',
            'active_missing': missing,
            'restart_required': (active_interface or '') != target,
            'candidates': available, 'detection_error': error}


def _require_local(interface):
    # A UDP bind validates ownership without connecting, listening or sending data.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind((str(interface.ip), 0))
    except OSError:
        raise ValueError('此 IPv4 地址不属于当前可用网卡；请刷新并选择本机地址。') from None


def save(value, expected_revision, ui_port=7765, active_interface=''):
    if (not isinstance(value, dict) or set(value) != {'enabled', 'interface'}
            or type(value.get('enabled')) is not bool or not isinstance(value.get('interface'), str)):
        raise ValueError('局域网设置须包含启用状态和私有 IPv4/前缀。')
    if legacy_bind():
        raise ValueError('局域网监听由 DESKTRON_REMOTE_BIND 管理；请先移除旧配置再使用此设置。')
    interface = str(parse_interface(value['interface'])) if value['interface'] else ''
    if value['enabled']:
        if not interface:
            raise ValueError('请先选择本机私有 IPv4/前缀。')
        _require_local(parse_interface(interface))
        import remote_auth
        if remote_auth.bind_host() != '127.0.0.1':
            raise ValueError('主入口已开放全网卡；请先调整已有监听策略，再启用受限局域网入口。')
    current = read_config()
    snapshot = durable_json.read(config_path())
    if (expected_revision != current['revision']
            or snapshot.get('revision', 'new') != current['revision']):
        raise durable_json.ConflictError('局域网设置已更新，请刷新后再保存。')
    snapshot.update(schema=1, enabled=value['enabled'], interface=interface,
                    revision=uuid.uuid4().hex,
                    token=current['token'] or (secrets.token_urlsafe(32) if value['enabled'] else ''))
    try:
        durable_json.write(config_path(), snapshot)
        return status(active_interface)
    except durable_json.ConflictError:
        raise
    except (OSError, ValueError):
        raise SaveOutcomeUnknown('保存结果未知，请刷新状态核对后再操作。') from None
