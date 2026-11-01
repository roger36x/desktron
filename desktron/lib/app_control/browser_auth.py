"""审批只认已登录浏览器的 Cookie；不沿用环回、Bearer 或 Agent 能力放行。

不按请求提供的 URL 查询凭据，不把宿主令牌发给网页。
"""
import hmac
import urllib.parse

from .runtime import AppControlError


def _origin(value):
    try:
        u = urllib.parse.urlsplit(value)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
            return None
        return u.scheme, u.hostname.lower(), u.port or (443 if u.scheme == "https" else 80)
    except (TypeError, ValueError):
        return None


def require_browser(h):
    import remote_auth
    headers = h.headers
    fail = AppControlError("审批身份验证失败，请从已登录的 Desk 页面重新打开", 403)
    if headers.get("X-Desk-Approval-UI") != "1":
        raise fail
    # 新浏览器的 Fetch Metadata 和旧 WebView 的 Origin/Referer 均须同源。
    site = str(headers.get("Sec-Fetch-Site") or "")
    if site and site != "same-origin":
        raise fail
    source = _origin(str(headers.get("Origin") or headers.get("Referer") or ""))
    if not source or source != _origin(source[0] + "://" + str(headers.get("Host") or "")):
        raise fail
    cookie = remote_auth._cookie_of(headers.get("Cookie"), remote_auth.COOKIE)
    if not cookie:
        raise fail
    own = remote_auth.token()
    if not own or not hmac.compare_digest(cookie, own):
        raise fail
