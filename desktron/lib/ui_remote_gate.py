# 由 ui_server.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)
from ui_static import _asset_token
import deskcfg     # 模块形式引入:后台线程日志统一走 deskcfg.log("<模块>", msg),悬浮窗按 [模块] 过滤
import http.server, socketserver, urllib.request, urllib.error, urllib.parse
import sys, os, signal, atexit, time, socket, subprocess, json, re, queue, struct, gzip, io, stat
import runtime_identity
import html


def _runtime_rev():
    """当前运行副本的版本标识。

    正式发行安装会写提交号；开发同步没有可准确代表脏工作树的提交号，
    但会写 payload 内容指纹。后者显式加前缀，避免把工作树快照误报成 Git 提交。
    """
    # generation 内的标记与代码一同生成、不可变。进程从自身文件位置读取，避免
    # canonical home 切换后把旧进程误报成新版本。
    try:
        product_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(product_root, ".payload-rev")) as f:
            rev = f.read().strip().lower()
        if re.fullmatch(r"payload:[0-9a-f]{64}", rev):
            return rev
    except Exception:
        pass
    try:
        with open(os.path.expanduser("~/.local/share/desktron/.update-rev")) as f:
            rev = f.read().strip()
        if rev:
            return rev[:40]
    except Exception:
        pass
    try:
        with open(os.path.expanduser("~/.local/share/desktron/.payload-md5")) as f:
            fingerprint = f.read().strip().lower()
        if re.fullmatch(r"[0-9a-f]{32}", fingerprint):
            return "payload:" + fingerprint
    except Exception:
        pass
    return ""


# 版本表示「当前进程实际加载的代码」，不能每次探活再读磁盘。部署把新 payload 换到
# home 后，如果旧服务尚未重启，动态读取会把旧进程误报成新版本，--restart never
# 因而可能被当作已生效。进程启动时固定一次，重启后才随新版本变化。
_BOOT_RUNTIME_REV = _runtime_rev()


class UiRemoteGateMixin:

    # 添加到手机主屏幕时，系统取图标可能不携带浏览器 Cookie。
    # 只公开这三份产品图标的读取，不扩展到其他静态资源或写方法。
    _PUBLIC_HOME_ICONS = frozenset(("/icon-180.png", "/icon-192.png", "/icon-512.png"))

    def _gate(self):
        """远端共享令牌:未配令牌时恒放行(本机形态不变),配了则拦非环回的无票请求。
        首次带 ?token= 的放行请求补种 Cookie,之后地址栏与日志里不再出现令牌。"""
        import remote_auth
        proxied = self._serve_proxied()
        if (self.command in ("GET", "HEAD")
                and self.path.split("?", 1)[0] in self._PUBLIC_HOME_ICONS
                and (self.client_address[0] in remote_auth.LOOPBACK if proxied
                     else remote_auth.client_allowed(self.client_address[0]))):
            self._set_remote_cookie = False
            return True
        ok, set_cookie = remote_auth.check(self.client_address[0], self.headers, self.path,
                                           proxied=proxied)
        if not ok:
            self.send_response(401)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("需要访问令牌\n".encode())
            return False
        self._set_remote_cookie = set_cookie
        if set_cookie and self.command == "GET":
            # 先种 Cookie 再跳到干净地址，避免令牌被页面资源的 Referer 带走。
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.urlencode([(k, v) for k, v in
                urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if k != "token"])
            target = (parsed.path or "/") + ("?" + query if query else "")
            if not target.startswith("/") or target.startswith("//") or "\\" in target:
                target = "/"
            self.send_response(303)
            self.send_header("Location", target)
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        return True

    def _serve_proxied(self):
        """Tailscale Serve 专用端口：环回来源是转发代理，不能当作本机。"""
        return bool(getattr(self.server, "serve_proxy", False))

    def _lan_local_request(self):
        import remote_auth
        if self._serve_proxied():
            return False
        port = self.server.server_address[1]
        hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
        origin = self.headers.get("Origin")
        return (self.client_address[0] in remote_auth.LOOPBACK
                and self.server.server_address[0] == "127.0.0.1"
                and str(self.headers.get("Host") or "").lower() in hosts
                and (not origin or origin in tuple("http://" + h for h in hosts)))

    def _lan_access_status(self):
        """仅环回同源请求取得凭据；手机只能读取是否已开放。"""
        import remote_auth
        local = self._lan_local_request()
        address = getattr(self.server, "lan_address", "")
        tok = remote_auth.token()
        enabled = bool(address and tok)
        state = {"local": local, "enabled": enabled}
        if local and enabled:
            port = self.server.server_address[1]
            state["address"] = f"http://{address}:{port}/"
            state["url"] = state["address"] + "?token=" + urllib.parse.quote(tok, safe="")
        serve_port = getattr(self.server, "serve_port", 0)
        if local:
            # A damaged desired-state file must not hide pairing links that are
            # already active; report it beside them and keep the original file.
            import tailscale_settings
            import lan_settings
            for key, read in (('tailscale_config', lambda: tailscale_settings.status(serve_port)),
                              ('lan_config', lambda: lan_settings.status(
                                  getattr(self.server, 'lan_interface', '')))):
                try:
                    state[key] = read()
                except (OSError, ValueError) as exc:
                    state[key + '_error'] = (str(exc) if isinstance(exc, ValueError)
                                             else '连接设置读取失败，请检查配置文件权限后刷新。')
        if serve_port:
            # 只告诉本机设置页 Tailscale 入口的状态与链接；手机端只知道入口已开。
            serve = {"enabled": True}
            if local:
                import tailscale_serve
                serve.update(tailscale_serve.status(serve_port))
                if serve.get("address") and tok:
                    serve["url"] = serve["address"] + "?token=" + urllib.parse.quote(tok, safe="")
            state["tailscale"] = serve
        body = json.dumps(state).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _lan_access(self):
        """仅本机环回入口展示连接链接，不把令牌写入日志或产品文件。"""
        import remote_auth
        port = self.server.server_address[1]
        if not self._lan_local_request():
            return self.send_error(403, "local access only")
        address = getattr(self.server, "lan_address", "")
        if address and remote_auth.token():
            url = html.escape(f"http://{address}:{port}/?token="
                              + urllib.parse.quote(remote_auth.token(), safe=""), quote=True)
            content = ("<h1>手机连接 Desk</h1><p>手机与 Mac 连接同一可信 Wi-Fi，"
                       "复制下面的完整链接到手机浏览器。首次打开后自动保存访问凭据。</p>"
                       f'<input readonly aria-label="手机连接链接" value="{url}">'
                       f'<p><a href="{url}" rel="noreferrer">在本机验证链接</a></p>'
                       "<p>链接含访问凭据，请勿公开。此入口使用 HTTP。"
                       "关闭入口请让 Desk 停用局域网监听；更换网络后须重新配置。</p>")
        else:
            content = "<h1>局域网入口未开放</h1><p>请核对监听配置与后端日志。</p>"
        body = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>手机连接 Desk</title><style>body{max-width:700px;margin:48px auto;'
                'padding:0 24px;font:16px/1.7 system-ui}input{box-sizing:border-box;'
                'width:100%;padding:12px;font:inherit}a{color:#2463c5}</style>'
                + content + '</html>').encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _probe(self):
        """探活(不验票,见 remote_auth.PROBE_PATH):回一份足以认出「这是谁」的最小事实。
        正式发行返回提交号；开发同步返回带 payload: 前缀的内容指纹。"""
        import remote_auth
        return self._send_json({"desk": True, "role": remote_auth.role(),
                                "rev": _BOOT_RUNTIME_REV,
                                "rev_scope": "process", **runtime_identity.facts()})
