"""远端访问的共享令牌鉴权。

本机运行时默认只绑 127.0.0.1。旧远程令牌沿用原有绑定策略；Tailscale
专用入口即使生成令牌，默认仍只绑环回，不因此隐式开放局域网。
所有非本机入口均先配置鉴权再开放，避免名义有鉴权而实际未生效。

旧令牌落 SECRETS(~/.config/desktron/env 的 DESKTRON_REMOTE_TOKEN)；设置页生成的
Tailscale 专用令牌落 ~/.config/desktron/tailscale.json，两者均不进仓库。
校验三种来源,按顺序:Authorization: Bearer、Cookie、查询参数 ?token=。
查询参数只为「手机上点一次书签」准备,命中后服务端种 HttpOnly Cookie,
后续请求与 WebSocket 握手都靠 Cookie,令牌不再出现在地址栏与日志里。

环回地址一律放行:本机的壳与 Desk 自己的组件不带令牌,也不该被自己的鉴权挡住。
例外是 DESKTRON_SERVE_PORT 指定的 Tailscale Serve 专用端口:那里的环回来源是转发代理,
每个请求都要验票(见 serve_port)。
"""
import hmac
import ipaddress
import os

TOKEN_ENV = "DESKTRON_REMOTE_TOKEN"
COOKIE = "cd_remote"
LOOPBACK = ("127.0.0.1", "::1", "localhost")

# 探活:不验票的一条路径。客户端切到某一端之前要先分清「那边没装/没起」与「装了但令牌不对」——
# 这两种的下一步完全不同(前者装,后者配令牌)。若探活也验票,两种都回 401,分不出来。
# 回的内容只够认出「这是一份 Desk、是哪一份、什么版本」,不含主机名、用户名与任何配置。
PROBE_PATH = "/desk-probe"


_RUNTIME_TOKEN = None


def load_runtime_token():
    """Freeze the effective credential before any listener admits requests."""
    global _RUNTIME_TOKEN
    if _RUNTIME_TOKEN is not None:
        raise RuntimeError('远程访问运行令牌已固定，变更须重启。')
    _RUNTIME_TOKEN = token()


def token():
    """运行令牌；尚未启动 HTTP 服务的调用方读取已配置值。"""
    if _RUNTIME_TOKEN is not None:
        return _RUNTIME_TOKEN
    v = os.environ.get(TOKEN_ENV, "").strip()
    if v:
        return v
    try:
        from credstore import _secrets_read
        v = str(_secrets_read().get(TOKEN_ENV, "")).strip()
    except Exception:
        v = ""
    if v:
        return v
    import tailscale_settings
    config = tailscale_settings.runtime_config()
    if config['enabled']:
        return config['token']
    import lan_settings
    lan = lan_settings.runtime_config()
    return lan['token'] if lan['enabled'] else ""


ROLE_ENV = "DESKTRON_ROLE"


def role():
    """这一份 Desk 是本机那份还是服务器上常驻的那份。默认 local;常驻服务器的
    服务单元里设 DESKTRON_ROLE=remote。

    不按「请求是不是从环回来的」判:经 SSH 隧道访问服务器时页面地址也是 127.0.0.1,
    来源看不出对面是谁。也不按「有没有配令牌」判:令牌说的是这一份对外开不开,
    与它是哪一份无关。"""
    v = os.environ.get(ROLE_ENV, "").strip().lower()
    if not v:
        try:
            from credstore import _secrets_read
            v = str(_secrets_read().get(ROLE_ENV, "")).strip().lower()
        except Exception:
            v = ""
    return "remote" if v == "remote" else "local"


DESK_ID_ENV = "DESKTRON_DESK_ID"


def desk_id():
    """这份 Desk 的稳定身份,跨重启不变,识别的是「哪一台机器」而不是 role() 那种
    「本机/常驻」的类别值。用于按机器区分来源——同一台机器上可能换着跑不同 Agent 身份。

    落 SECRETS,首次调用时生成并写回,此后只读,跟 token()/role() 读同一份文件。
    这里是懒生成:第一次读到空时当场补上,不依赖某条安装脚本。

    两个进程同时首次调用理论上会各生成一个值、后写的那个赢,这份小概率的先跑竞态
    可以接受——用途是粗粒度限流,不是身份鉴权,不值得为此加文件锁。
    """
    v = os.environ.get(DESK_ID_ENV, "").strip()
    if v:
        return v
    try:
        from credstore import _secrets_read, _secrets_write
        v = str(_secrets_read().get(DESK_ID_ENV, "")).strip()
        if v:
            return v
        import secrets
        v = "desk_" + secrets.token_urlsafe(16)
        _secrets_write({DESK_ID_ENV: v})
        return v
    except Exception:
        return ""


BIND_ENV = "DESKTRON_REMOTE_BIND"


def lan_interface():
    """lan:IPv4/前缀限定一个局域网地址与来源网段，不开放其他网卡。"""
    want = os.environ.get(BIND_ENV, "").strip().lower()
    if not want:
        import lan_settings
        config = lan_settings.runtime_config()
        if config['enabled']:
            want = 'lan:' + config['interface']
    if not want.startswith("lan:"):
        return None
    try:
        interface = ipaddress.IPv4Interface(want[4:])
    except ValueError:
        raise ValueError("局域网监听须使用 lan:IPv4/前缀") from None
    private = (ipaddress.IPv4Network("10.0.0.0/8"),
               ipaddress.IPv4Network("172.16.0.0/12"),
               ipaddress.IPv4Network("192.168.0.0/16"))
    if not any(interface.network.subnet_of(net) for net in private):
        raise ValueError("局域网监听与来源网段须限定在私有 IPv4 地址内")
    if not token():
        raise ValueError("局域网监听必须配置访问令牌")
    return interface


def client_allowed(client_host):
    """局域网模式只接受环回与指定网段，且不信任转发来源头。"""
    interface = lan_interface()
    if interface is None:
        return True
    try:
        address = ipaddress.ip_address(str(client_host).split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback or address in interface.network


def bind_host():
    """监听地址。默认与鉴权同步:配了令牌才对外绑,没配只绑环回。
    DESKTRON_REMOTE_BIND 可单独覆盖——loopback 用于「只经 SSH 隧道进来、但仍然验票」的形态
    :鉴权照常生效,监听面不放开。lan:IPv4/前缀保留环回，另开受限局域网监听。"""
    want = os.environ.get(BIND_ENV, "").strip().lower()
    if lan_interface() is not None:
        return "127.0.0.1"  # 原生壳及本机 CLI 继续使用原来的环回入口。
    if want in ("loopback", "127.0.0.1", "local"):
        return "127.0.0.1"
    if want in ("all", "0.0.0.0", "any"):
        if not token():
            raise ValueError("全网卡监听必须配置访问令牌")
        return "0.0.0.0"
    # A damaged managed policy must not fall back to legacy token => all NICs.
    import lan_settings
    if lan_settings.runtime_config().get('failed'):
        return '127.0.0.1'
    # Pairing for Serve must never implicitly open the primary port to the LAN.
    import tailscale_settings
    config = tailscale_settings.runtime_config()
    if config.get('failed') or config['enabled'] or tailscale_settings.legacy_port():
        return "127.0.0.1"
    return "0.0.0.0" if token() else "127.0.0.1"


SERVE_PORT_ENV = "DESKTRON_SERVE_PORT"


def serve_port(ui_port=None):
    """Tailscale Serve 专用入口的环回端口；未配置返回 None。

    Serve 在本机终止 HTTPS 后从环回地址转发，来源看起来与原生壳相同。
    因此这个端口单独监听，并且不享受环回放行：每个请求都必须带令牌。"""
    import tailscale_settings
    config = tailscale_settings.runtime_config()
    if config.get('failed'):
        return None  # A corrupt optional policy cannot expose a legacy Serve listener.
    raw = os.environ.get(SERVE_PORT_ENV, "").strip()
    if not raw:
        try:
            from credstore import _secrets_read
            raw = str(_secrets_read().get(SERVE_PORT_ENV, "")).strip()
        except Exception:
            raw = ""
    if not raw:
        if not config['enabled']:
            return None
        raw = str(config['port'])
    try:
        port = int(raw)
    except ValueError:
        raise ValueError("Tailscale 入口端口须为整数") from None
    if not 1024 <= port <= 65535:
        raise ValueError("Tailscale 入口端口须在 1024–65535 之间")
    if ui_port is not None and port == int(ui_port):
        raise ValueError("Tailscale 入口端口不能与本机入口端口相同")
    if not token():
        raise ValueError("Tailscale 入口必须配置访问令牌")
    return port


def _cookie_of(header, name):
    for part in str(header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return ""


def check(client_host, headers, path, proxied=False):
    """→ (是否放行, 是否需要补种 Cookie)。令牌未配 = 不设防,恒放行。

    proxied=True 表示请求来自 Tailscale Serve 专用端口：环回来源不代表本机，
    未配置令牌时一律拒绝，不回退成无鉴权。"""
    host = str(client_host or "").split("%")[0]
    if proxied:
        if host not in LOOPBACK:
            return False, False
    elif not client_allowed(client_host):
        return False, False
    if str(path or "").split("?", 1)[0] == PROBE_PATH:
        return True, False    # 探活豁免,与环回放行同层(理由见 PROBE_PATH)
    tok = token()
    if not tok:
        return (False, False) if proxied else (True, False)
    if not proxied and host in LOOPBACK:
        return True, False
    auth = str(headers.get("Authorization") or "")
    if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), tok):
        return True, False
    if hmac.compare_digest(_cookie_of(headers.get("Cookie"), COOKIE), tok):
        return True, False
    q = path.split("?", 1)[1] if "?" in path else ""
    for kv in q.split("&"):
        k, _, v = kv.partition("=")
        if k == "token":
            import urllib.parse
            if hmac.compare_digest(urllib.parse.unquote(v), tok):
                return True, True    # 首次带参:放行并种 Cookie,让后续请求不再暴露令牌
            break
    return False, False


def cookie_header(secure=False):
    """种 Cookie 的响应头值。HttpOnly 挡 JS 读取;https 下再加 Secure。"""
    v = f"{COOKIE}={token()}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000"
    return v + ("; Secure" if secure else "")
