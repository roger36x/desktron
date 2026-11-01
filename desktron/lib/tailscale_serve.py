"""读取 Windows/macOS 本机 Tailscale 与 Serve 状态，生成 HTTPS 连接链接。

只读：不登录、不修改 Serve 配置。CLI 不存在、未登录或 Serve 未指向 Desk 时，
返回明确的状态码，由设置页给出对应的下一步，不猜测地址。
"""
import json
import os
import shutil
import subprocess
import urllib.parse

_APP_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
_TIMEOUT = 3


def _cli():
    found = shutil.which("tailscale")
    if found:
        return found
    if os.name == "nt":
        # Windows 安装器默认装在 Program Files 下，安装后未重新登录时 PATH 可能还没生效。
        candidate = os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Tailscale", "tailscale.exe")
        return candidate if os.path.isfile(candidate) else ""
    return _APP_CLI if os.access(_APP_CLI, os.X_OK) else ""


def _run_json(cli, *args):
    out = subprocess.run([cli, *args, "--json"], capture_output=True, text=True,
                         encoding='utf-8', timeout=_TIMEOUT, check=False)
    if out.returncode != 0:
        # Native output can contain machine/account details; do not return it.
        raise RuntimeError('Tailscale CLI 状态读取失败（exit %d）。' % out.returncode)
    result = json.loads(out.stdout or "{}")
    if not isinstance(result, dict):
        raise ValueError('Tailscale CLI 返回格式错误。')
    return result


def _proxies_to(proxy, port):
    try:
        u = urllib.parse.urlsplit(str(proxy or ""))
    except ValueError:
        return False
    try:
        return (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")
                and u.port == port and u.username is None and u.password is None
                and u.path in ('', '/') and not u.query and not u.fragment)
    except ValueError:
        return False


def _public_ingress(config):
    if not isinstance(config, dict):
        return False
    if any(value is True for value in (config.get('AllowFunnel') or {}).values()):
        return True
    return any(_public_ingress(value) for value in config.values() if isinstance(value, dict))


def serve_target(serve_json, dns_name, port):
    """Serve 配置中由本机 dns_name 的 HTTPS 根路径转发到 port 的入口；没有返回空串。"""
    web = (serve_json or {}).get("Web") or {}
    tcp = (serve_json or {}).get("TCP") or {}
    for hostport, cfg in web.items():
        host, _, hport = str(hostport).rpartition(":")
        if host.lower() != dns_name.lower():
            continue
        # ipn.ServeConfig: Web routes alone do not establish an HTTPS listener.
        # Secure cookies require TCPPortHandler.HTTPS on this exact port.
        if not hport.isascii() or not hport.isdigit() or not 1 <= int(hport) <= 65535:
            continue
        listener = tcp.get(hport) or {}
        if (listener.get("HTTPS") is not True or listener.get("HTTP")
                or listener.get("TCPForward")):
            continue
        handler = ((cfg or {}).get("Handlers") or {}).get("/") or {}
        if _proxies_to(handler.get("Proxy"), port):
            # A matching Serve route may also be exposed by Funnel. Refuse to
            # advertise a pairing credential while any public ingress is enabled.
            if _public_ingress(serve_json):
                return ""
            return "https://" + host + ("" if hport == "443" else ":" + hport) + "/"
    return ""


def status(port):
    """→ {"state": ..., "address"?: ..., "command"?: ...}

    state: missing（未安装 CLI）、offline（未登录或未运行）、unconfigured（Serve 未指向
    Desk 端口）、ready（可用）、error（读取失败）。"""
    command = f"tailscale serve --bg http://127.0.0.1:{port}"
    cli = _cli()
    if not cli:
        return {"state": "missing", "command": command}
    if os.name == 'nt':
        command = f'& "{cli}" serve --bg http://127.0.0.1:{port}'
    try:
        st = _run_json(cli, "status")
        me = st.get("Self") or {}
        dns = str(me.get("DNSName") or "").rstrip(".")
        if st.get("BackendState") != "Running" or not dns:
            return {"state": "offline", "command": command}
        serve = _run_json(cli, "serve", "status")
        if _public_ingress(serve):
            return {'state': 'public', 'command': ''}
        address = serve_target(serve, dns, port)
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError, TypeError, AttributeError):
        return {"state": "error", "error": "Tailscale 状态读取失败，请在本机检查 CLI。", "command": ''}
    if not address:
        return {"state": "unconfigured", "host": dns, "command": command}
    return {"state": "ready", "address": address}
