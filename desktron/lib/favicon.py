"""站点图标的解析:抓一次首页,把它声明的 <link rel="icon"> 找出来。

为什么需要这一层:标签条上那枚图标,只按 `<来源>/favicon.ico` 去拼是拿不全的。
有的站点根路径返回 204 空响应,而它真正的图标声明在 HTML 里、
指向另一个域的 CDN。浏览器解析 HTML 才拿得到,我们够不到那段 HTML——原生嵌入那侧
不回传图标,同源策略又挡着 iframe 的文档。所以由这里代抓一次。

只返回地址,不代理图片:图标多半在公共 CDN 上,让浏览器自己取、自己缓存。
抓不到就说抓不到,由前端回落到通用标记,不编一个地址出来。
"""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

_TTL = 24 * 3600          # 同一个来源一天内不重抓
_NEG_TTL = 10 * 60        # 抓不到的十分钟内也不重试:多半是这个站就没有
_TIMEOUT = 6
_MAX_HTML = 200_000       # 只看头部:图标声明在 <head> 里,整页读下来没必要

_cache = {}               # 来源 → (时刻, 地址或空串)
_lock = threading.Lock()
_loaded = False


def _cache_path():
    """落盘位置。原来只有进程内缓存,Desk 一重启就全忘:浏览器标签恢复时每个站点都要
    重新出外网抓一次首页,2026-09-05 实测开机 20 秒内六次、合计 12 秒——正是页面取首屏
    数据的那一段。图标一天才变一次,按来源落盘,重启后直接命中。"""
    try:
        from deskcfg import DATA_HOME
    except Exception:
        DATA_HOME = os.environ.get("DESKTRON_DATA_HOME",
                                   os.path.expanduser("~/.local/share/desktron/data"))
    return os.path.join(DATA_HOME, "cache", "favicon.json")


def _load_once():
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            raw = json.load(f)
        for origin, row in (raw or {}).items():
            if isinstance(row, list) and len(row) == 2:
                _cache.setdefault(origin, (float(row[0]), str(row[1] or "")))
    except Exception:
        pass


def _save():
    """写整份。条目数以站点计,几十条,原子替换比追加省事。失败不影响内存里那份。"""
    path = _cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: [v[0], v[1]] for k, v in _cache.items()}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass

# rel 里含 icon 的都算(icon / shortcut icon / apple-touch-icon)。属性顺序不固定,
# 所以先抓整条 link 标签,再从里面分别取 rel 与 href。
_LINK = re.compile(r"<link\b[^>]*>", re.I)
_ATTR = re.compile(r'\b(rel|href)\s*=\s*["\']([^"\']*)["\']', re.I)


def _origin(url):
    try:
        u = urllib.parse.urlsplit(url)
        return f"{u.scheme}://{u.netloc}" if u.scheme in ("http", "https") and u.netloc else ""
    except ValueError:
        return ""


def _pick(html, base):
    """HTML 里声明的图标地址。多条时取排在最后的那条——站点惯例是把更大的尺寸写在后面。"""
    best = ""
    for tag in _LINK.findall(html):
        attrs = {k.lower(): v for k, v in _ATTR.findall(tag)}
        rel = (attrs.get("rel") or "").lower()
        href = (attrs.get("href") or "").strip()
        if href and "icon" in rel:
            best = urllib.parse.urljoin(base, href)
    return best


def resolve(url):
    """{"href": 地址} 或 {"href": ""}。地址为空表示这个站点没有可用的声明。"""
    origin = _origin(url)
    if not origin:
        return {"href": ""}
    now = time.time()
    with _lock:
        _load_once()
        hit = _cache.get(origin)
        if hit and now - hit[0] < (_TTL if hit[1] else _NEG_TTL):
            return {"href": hit[1]}
    href = ""
    try:
        req = urllib.request.Request(origin + "/", headers={
            "User-Agent": "desktron/1.0",
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "html" in ctype or not ctype:
                # 跟随重定向之后的最终地址才是相对路径的基准(内网站点常整页跳到 SSO)
                html = r.read(_MAX_HTML).decode("utf-8", "ignore")
                href = _pick(html, r.geturl())
    except Exception:
        href = ""
    with _lock:
        _cache[origin] = (now, href)
        _save()
    return {"href": href}
