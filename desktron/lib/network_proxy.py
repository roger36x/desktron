"""Local Windows network policy, frozen at launch; saving never restarts Desk.

No credentials, PAC evaluation, model requests or silent direct fallback.
Absent configuration preserves the previous environment and browser policy.
"""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import urllib.parse
import urllib.request
import uuid

import durable_json

LOOPBACK = ['localhost', '127.0.0.1', '::1']
PROXY_KEYS = {'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}
_TEST_LOCK = threading.Lock()


def config_path():
    return Path.home() / '.config' / 'desktron' / 'network.json'


def _host(value):
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError('代理地址必须是主机名或 IP，不是订阅链接或完整 URL。')
    value = value.strip().strip('[]').lower()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', value):
        raise ValueError('代理地址仅支持主机名或 IP；不接受用户名、密码或 URL 参数。')
    if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-')
           for label in value.split('.')):
        raise ValueError('代理主机名格式错误。')
    return value


def normalize(value):
    if not isinstance(value, dict) or set(value) - {'mode', 'host', 'port', 'bypass'}:
        raise ValueError('网络代理配置格式错误。')
    mode = value.get('mode')
    if mode not in ('system', 'direct', 'manual'):
        raise ValueError('请选择跟随系统、直连或手动配置。')
    bypass = value.get('bypass', LOOPBACK)
    if isinstance(bypass, str):
        bypass = [part.strip() for part in bypass.split(',') if part.strip()]
    if not isinstance(bypass, list) or len(bypass) > 64:
        raise ValueError('绕过地址最多 64 个，以逗号分隔。')
    normalized = list(LOOPBACK)
    for part in bypass:
        if not isinstance(part, str):
            raise ValueError('绕过地址格式错误。')
        part = part.strip().lower()
        suffix = part.startswith('.')
        part = ('.' if suffix else '') + _host(part.lstrip('.') if suffix else part)
        if part not in normalized:
            normalized.append(part)
    if len(normalized) > 64:
        raise ValueError('包含本机地址在内，绕过地址最多 64 个。')
    result = {'mode': mode, 'host': '', 'port': 0, 'bypass': normalized}
    if mode == 'manual':
        result['host'] = _host(value.get('host', ''))
        port = value.get('port')
        if isinstance(port, str) and port.isascii() and port.isdecimal():
            port = int(port)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('代理端口必须为 1–65535。')
        result['port'] = port
    return result


def read_config():
    raw = durable_json.read_value(config_path())
    if not raw:
        if config_path().exists():
            raise ValueError('网络配置为空，原文件已保留；请修复后再启动。')
        return {**normalize({'mode': 'system'}), 'revision': 'legacy', 'configured': False}
    try:
        if raw.get('schema') != 1 or not re.fullmatch(r'[0-9a-f]{32}', raw.get('revision', '')):
            raise ValueError()
        result = normalize({k: raw[k] for k in ('mode', 'host', 'port', 'bypass')})
    except (ValueError, KeyError, TypeError):
        raise ValueError('网络配置损坏，原文件已保留；请修复后再启动。') from None
    return {**result, 'revision': raw['revision'], 'configured': True}


def _proxy_url(host, port):
    return 'http://' + ('[' + host + ']' if ':' in host else host) + ':' + str(port)


def _endpoint(value):
    try:
        parsed = urllib.parse.urlsplit(value if '://' in value else 'http://' + value)
        if (parsed.scheme != 'http' or parsed.username is not None or parsed.password is not None
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError()
        port = parsed.port if parsed.port is not None else 80
        if not 1 <= port <= 65535:
            raise ValueError()
        return _proxy_url(_host(parsed.hostname), port)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('系统代理不是受支持的无认证 HTTP 代理；请改用手动配置。') from None


def system_proxies():
    if sys.platform != 'win32':
        raise ValueError('统一网络代理目前仅支持 Windows。')
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
            def query(name, default=None):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    return default
            automatic = bool(query('AutoConfigURL', '') or query('AutoDetect', 0))
            if not automatic:
                try:
                    with winreg.OpenKey(key, 'Connections') as connections:
                        connection = winreg.QueryValueEx(connections, 'DefaultConnectionSettings')[0]
                    if isinstance(connection, bytes) and len(connection) >= 12:
                        # WININET connection flags: AUTO_PROXY_URL=4, AUTO_DETECT=8.
                        automatic = bool(int.from_bytes(connection[8:12], 'little') & 12)
                except FileNotFoundError:
                    pass
            if automatic:
                raise ValueError('系统使用 PAC 或自动发现，无法统一到引擎；请填写 HTTP 代理地址和端口。')
            if not query('ProxyEnable', 0):
                return {}, []
            raw = str(query('ProxyServer', ''))
            bypass = str(query('ProxyOverride', ''))
    except FileNotFoundError:
        return {}, []
    proxies = {}
    if '=' not in raw:
        proxies = {scheme: _endpoint(raw) for scheme in ('http', 'https')}
    else:
        for part in raw.split(';'):
            scheme, sep, address = part.partition('=')
            if sep and scheme in ('http', 'https'):
                proxies[scheme] = _endpoint(address)
        if not proxies:
            raise ValueError('系统仅设置了 SOCKS 代理；请提供 HTTP 或混合端口。')
    bypasses = []
    for part in bypass.split(';'):
        part = part.strip()
        if part == '<local>':
            raise ValueError('系统绕过规则包含 <local>，引擎不支持等价匹配；请使用手动配置。')
        if part:
            bypasses.append('.' + part[2:] if part.startswith('*.') else part)
    # Only equivalent host/suffix rules are accepted; never silently discard a rule.
    return proxies, normalize({'mode': 'system', 'bypass': bypasses})['bypass']


def resolve(config):
    if config['mode'] == 'manual':
        return {s: _proxy_url(config['host'], config['port']) for s in ('http', 'https')}, config['bypass']
    if config['mode'] == 'direct':
        return {}, ['*']
    proxies, bypass = system_proxies()
    return proxies, list(dict.fromkeys(LOOPBACK + bypass + config['bypass']))


def resolution_id(proxies, bypass):
    return hashlib.sha256(json.dumps([proxies, bypass], sort_keys=True).encode()).hexdigest()


def browser_policy():
    config = read_config()
    if not config['configured']:
        return {'args': [], 'revision': 'legacy', 'resolution': 'legacy'}
    proxies, bypass = resolve(config)
    if proxies:
        rules = []
        for host in bypass:
            if host.startswith('.'):
                rules.extend([host[1:], '*' + host])
            else:
                rules.append('[' + host + ']' if ':' in host else host)
        args = ['--proxy-server=' + ';'.join(s + '=' + url for s, url in sorted(proxies.items())),
                '--proxy-bypass-list=' + ';'.join(rules)]
    else:
        args = ['--no-proxy-server']
    return {'args': args, 'revision': config['revision'], 'resolution': resolution_id(proxies, bypass)}


def apply_environment(environ):
    """Only at process launch, after reading legacy env; not on a settings write."""
    config = read_config()
    if not config['configured']:
        return
    proxies, bypass = resolve(config)  # Validate before mutating; failures cannot mean direct.
    for key in list(environ):
        if key.lower() in PROXY_KEYS:
            environ.pop(key)
    for scheme, url in proxies.items():
        environ[scheme.upper() + '_PROXY'] = url
        environ[scheme + '_proxy'] = url
    environ['NO_PROXY'] = environ['no_proxy'] = ','.join(bypass)
    environ['DESKTRON_NETWORK_MODE'] = config['mode']
    environ['DESKTRON_NETWORK_REVISION'] = config['revision']
    environ['DESKTRON_NETWORK_RESOLUTION'] = resolution_id(proxies, bypass)


def status():
    config = read_config()
    active = os.environ.get('DESKTRON_NETWORK_REVISION', 'legacy')
    browser = os.environ.get('DESKTRON_BROWSER_NETWORK_REVISION', '')
    supported = sys.platform == 'win32'
    error = ''
    try:
        proxies, bypass = resolve(config) if config['configured'] or supported else ({}, [])
        system_state = 'fixed_http_proxy' if proxies else 'no_fixed_proxy'
    except ValueError as exc:
        error, system_state = str(exc), 'unsupported'
    resolution = resolution_id(proxies, bypass) if not error and config['configured'] else 'legacy'
    pending = config['configured'] and (active != config['revision'] or browser != config['revision']
               or os.environ.get('DESKTRON_NETWORK_RESOLUTION') != resolution
               or os.environ.get('DESKTRON_BROWSER_NETWORK_RESOLUTION') != resolution)
    return {'config': config, 'supported': supported, 'resolution_error': error,
            'system_state': system_state, 'engine_revision': active,
            'browser_revision': browser, 'browser_capable': bool(browser),
            'pending': pending,
            'webpage_test': 'not_run', 'model_test': 'not_run'}


def save(value, expected_revision):
    if sys.platform != 'win32':
        raise ValueError('统一网络代理目前仅支持 Windows；不会改变其他平台的网络配置。')
    config = normalize(value)
    resolve(config)  # Reject unsupported OS proxy/PAC before writing.
    path = config_path()
    snapshot = durable_json.read(path)
    if snapshot.get('revision', 'legacy') != expected_revision:
        raise durable_json.ConflictError('网络设置已被其他操作更新，请重新读取后再保存。')
    snapshot.clear()
    snapshot.update(config, schema=1, revision=uuid.uuid4().hex)
    # The unique revision is a CAS field; concurrent writes cannot merge policies.
    durable_json.write(path, snapshot)
    os.chmod(path, 0o600)
    return status()


def test_connection(value):
    """Explicit user action: only TCP to the specified proxy; never website/model traffic."""
    config = normalize(value)
    proxies, _ = resolve(config)
    if not proxies:
        return {'proxy_tcp': 'not_applicable', 'webpage': 'not_run', 'model': 'not_run',
                'message': '没有固定代理端口可测试；这不代表网页或模型可用。'}
    if not _TEST_LOCK.acquire(blocking=False):
        raise ValueError('已有代理连接测试，请等待结果。')
    result = {'proxy_tcp': 'failed', 'webpage': 'not_run', 'model': 'not_run',
              'message': '代理 TCP 连接失败，请检查客户端、地址与端口。'}
    finished = threading.Event()
    def worker():
        try:
            for url in set(proxies.values()):
                target = urllib.parse.urlsplit(url)
                with socket.create_connection((target.hostname, target.port), timeout=2):
                    pass
            result.update(proxy_tcp='passed', message='代理 TCP 端口可连接；尚未验证 HTTP 转发、网页或模型。')
        except OSError:
            pass
        finally:
            finished.set()
            _TEST_LOCK.release()
    threading.Thread(target=worker, daemon=True).start()
    if not finished.wait(5):
        return {'proxy_tcp': 'timeout', 'webpage': 'not_run', 'model': 'not_run',
                'message': '代理连接测试超时；未切换到直连。'}
    return result


if __name__ == '__main__':
    if sys.argv[1:] != ['--native']:
        sys.exit(2)
    try:
        print(json.dumps(browser_policy()))
    except Exception:
        # Do not echo configuration, system proxy URLs or native exception details.
        print('网络代理配置无法读取或不受支持；未回退直连。', file=sys.stderr)
        sys.exit(1)
