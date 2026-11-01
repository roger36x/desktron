"""Shared Workbench HTTP proxy.

The proxy accepts a named upstream instead of arbitrary URLs.  This keeps the
browser-side API reusable while preserving a narrow server-side forwarding
surface.
"""
import base64
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


REFRESH_SKEW_SECONDS = 300
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


CANONICAL_REQUEST_HEADERS = {
    "accept": "Accept",
    "content-type": "Content-Type",
}


# 具名上游登记表。每条形如:
#   "name": {
#       "base_url": "https://api.example.com",
#       "allowed_methods": {"GET"},
#       "allowed_path": re.compile(r"^/v1/[A-Za-z0-9_./-]+$"),
#       "default_headers": {"Accept": "application/json"},
#       "allowed_request_headers": {"accept", "content-type"},
#       # 可选:用本机命令换取 JWT,令牌按 exp 缓存,401 时强制刷新一次。
#       "auth": {"type": "command-jwt", "command": ["my-cli", "token"], "header": "Authorization"},
#       "timeout": 25,
#   }
# 默认不登记任何上游;代理只转发到这里明确列出的地址。
UPSTREAMS = {}


_TOKEN_CACHE = {}
_TOKEN_LOCK = threading.Lock()


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def clear_token_cache():
    with _TOKEN_LOCK:
        _TOKEN_CACHE.clear()


def _error(status, message):
    return status, {"ok": False, "error": message}


def _now():
    return time.time()


def _base64url_decode(part):
    pad = "=" * ((4 - len(part) % 4) % 4)
    return base64.urlsafe_b64decode((part + pad).encode("ascii"))


def decode_jwt_exp(token):
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = json.loads(_base64url_decode(parts[1]).decode("utf-8"))
        exp = payload.get("exp")
        return int(exp) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


def fetch_command_jwt(auth):
    """运行上游声明的命令,取其标准输出作为令牌。命令由登记表写死,不接受请求方传入。"""
    command = auth.get("command")
    if not isinstance(command, (list, tuple)) or not command or not all(isinstance(x, str) for x in command):
        raise RuntimeError("auth command is not configured")
    proc = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "token command failed").strip()
        raise RuntimeError(msg[:500])
    token = (proc.stdout or "").strip()
    if not token:
        raise RuntimeError("token command returned empty token")
    return token


def _auth_cache_key(auth):
    return auth["type"] + ":" + " ".join(auth.get("command") or ())


def _get_cached_token(auth, token_fetcher, now):
    key = _auth_cache_key(auth)
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(key)
        if cached and cached.get("exp") and cached["exp"] - now() > REFRESH_SKEW_SECONDS:
            return cached["token"], "cache"
    token = token_fetcher(auth)
    exp = decode_jwt_exp(token)
    with _TOKEN_LOCK:
        if exp:
            _TOKEN_CACHE[key] = {"token": token, "exp": exp}
        else:
            _TOKEN_CACHE.pop(key, None)
    return token, "fetched"


def _clear_auth(auth):
    with _TOKEN_LOCK:
        _TOKEN_CACHE.pop(_auth_cache_key(auth), None)


def _validate_payload(payload):
    if not isinstance(payload, dict):
        return None, None, _error(400, "invalid json body")
    upstream_name = str(payload.get("upstream") or "").strip()
    upstream = UPSTREAMS.get(upstream_name)
    if not upstream:
        return None, None, _error(400, "unknown upstream")
    method = str(payload.get("method") or "GET").upper()
    if method not in upstream["allowed_methods"]:
        return None, None, _error(405, "method not allowed")
    path = str(payload.get("path") or "")
    if not path.startswith("/") or "://" in path or "?" in path or "#" in path or ".." in path:
        return None, None, _error(400, "bad path")
    if not upstream["allowed_path"].match(path):
        return None, None, _error(403, "path not allowed")
    return upstream, method, None


def _query_string(query):
    if query is None:
        return ""
    if isinstance(query, str):
        return query.lstrip("?")
    if isinstance(query, dict):
        clean = {}
        for k, v in query.items():
            if v is None:
                continue
            clean[str(k)] = v
        return urllib.parse.urlencode(clean, doseq=True)
    if isinstance(query, list):
        return urllib.parse.urlencode(query, doseq=True)
    raise ValueError("query must be object, list, or string")


def _target_url(upstream, path, query):
    parsed = urllib.parse.urlparse(upstream["base_url"])
    qs = _query_string(query)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, qs, ""))


def _request_body(payload, headers):
    if "body" not in payload or payload.get("body") is None:
        return None
    body = payload.get("body")
    if isinstance(body, str):
        return body.encode("utf-8")
    if isinstance(body, (dict, list)):
        headers.setdefault("Content-Type", "application/json")
        return json.dumps(body, ensure_ascii=False).encode("utf-8")
    raise ValueError("body must be string, object, list, or null")


def _headers(upstream, payload, auth_header=None, token=None):
    headers = dict(upstream.get("default_headers") or {})
    extra = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
    allowed = upstream.get("allowed_request_headers") or set()
    for k, v in extra.items():
        name = str(k).lower()
        if name in allowed and v is not None:
            headers[CANONICAL_REQUEST_HEADERS.get(name, str(k))] = str(v)
    if auth_header and token:
        headers[auth_header] = token
    return headers


def _read_response(resp):
    try:
        data = resp.read(MAX_RESPONSE_BYTES + 1)
    except TypeError:
        data = resp.read()
    if len(data) > MAX_RESPONSE_BYTES:
        raise RuntimeError("upstream response is too large")
    text = data.decode("utf-8", "replace")
    ctype = resp.headers.get("Content-Type", "")
    body = text
    if "json" in ctype.lower() or text[:1] in ("{", "["):
        try:
            body = json.loads(text)
        except Exception:
            body = text
    status = getattr(resp, "status", None) or getattr(resp, "code", 0) or 0
    return int(status), body, ctype


def _open_once(payload, upstream, method, opener, token_fetcher, now, force_refresh=False):
    auth = upstream.get("auth") or {}
    token, token_source = None, "none"
    if auth.get("type") == "command-jwt":
        if force_refresh:
            _clear_auth(auth)
        token, token_source = _get_cached_token(auth, token_fetcher, now)
    headers = _headers(upstream, payload, auth.get("header"), token)
    data = _request_body(payload, headers)
    req = urllib.request.Request(
        _target_url(upstream, payload["path"], payload.get("query")),
        data=data,
        method=method,
    )
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with opener.open(req, timeout=int(upstream.get("timeout") or 25)) as resp:
            status, body, ctype = _read_response(resp)
            return status, body, ctype, req, token_source
    except urllib.error.HTTPError as err:
        status, body, ctype = _read_response(err)
        return status, body, ctype, req, token_source


def handle_proxy_request(payload, opener=None, token_fetcher=None, now=None):
    upstream, method, err = _validate_payload(payload)
    if err:
        return err
    now = now or _now
    token_fetcher = token_fetcher or fetch_command_jwt
    opener = opener or urllib.request.build_opener(NoRedirectHandler())
    started = now()
    try:
        status, body, ctype, req, token_source = _open_once(payload, upstream, method, opener, token_fetcher, now)
        retry = ""
        if status == 401 and upstream.get("auth"):
            status, body, ctype, req, token_source = _open_once(
                payload, upstream, method, opener, token_fetcher, now, force_refresh=True)
            retry = "refreshed-token-after-401"
    except ValueError as exc:
        return _error(400, str(exc))
    except Exception as exc:
        return _error(502, str(exc))
    response = {
        "ok": 200 <= status < 300,
        "status": status,
        "headers": {"content-type": ctype},
        "body": body,
    }
    if payload.get("debug"):
        response["debug"] = {
            "upstream": str(payload.get("upstream") or ""),
            "method": method,
            "target_url": req.full_url,
            "auth": upstream.get("auth", {}).get("type", "") if upstream.get("auth") else "none",
            "token_source": token_source,
            "elapsed_ms": int(max(0, now() - started) * 1000),
        }
        if retry:
            response["debug"]["retry"] = retry
    return status, response
