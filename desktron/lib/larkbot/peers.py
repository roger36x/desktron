"""另一侧的 Desk(本机 ↔ 服务器)。

两侧的 workspace 各自独立,agent 声明与凭证都不跨侧——这一点没变,也不该变:凭证跟着
机器走是对的。变的是**看得见、管得着**:人在本机想知道服务器上那个 bot 在不在线、
想把它停掉,不该为此切一次后端。

聚合在服务端做,不在前端:令牌在 backends.json 里,前端拿不到也不该拿到。前端只说
「操作服务器上的这个」,由本机后端带着票转过去。

**只转 larkbot 那几个端点。** 这不是通用代理——通用代理意味着任何人拿到本机的票就能
借道打另一台,而那张票本来只保证「你能用这一台」。
"""
import json
import os
import urllib.error
import urllib.request

#: 转发标记。带着它进来的请求不再往外聚合——两台互相指着对方时会无限转下去,
#: 而那种配置(各自把对方写进 backends.json)是正常的。
HOP_HEADER = "X-Desk-Peer-Hop"

#: 允许转发的路径前缀。写死,不接受调用方传任意路径。安排与 Bot 声明一样按机器归属，
#: 管理页需要从一侧查看、操作另一侧，但仍只开放这两个明确的管理面。
ALLOW_PATHS = {"/workbench/arrangements"}
ALLOW_PREFIXES = ("/workbench/lark-bot/", "/workbench/arrangements/")

CONF = os.path.expanduser("~/.config/desktron/backends.json")

#: 拉另一侧的超时。短一些:它只是页面上的一段,那边关机或网络不通时不该让整页转圈。
TIMEOUT = 6


def _load():
    try:
        with open(CONF, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def others(me_url="", me_name=""):
    """→ [{name, url, token}]。除自己之外、配了地址的后端。

    两条判据都要:
      **按名字**——backends.json 里的 name 约定为 desktop / server 两个值,
      与 remote_auth.role() 一一对应,是最可靠的一条。
      **按 host:port**——防的是同一台机器在表里出现两次(不同写法),或 name 被人改过。

    只按 host:port 会漏:服务器自己那条写的是它的外部地址,而从它内部看
    自己是 127.0.0.1,字面对不上,结果它把自己当成对端去转发一圈(2026-08-25 实测)。
    """
    out = []
    mine = _hostport(me_url)
    for b in _load():
        if not isinstance(b, dict):
            continue
        url = str(b.get("url") or "").rstrip("/")
        name = str(b.get("name") or "?")
        if not url:
            continue
        if me_name and name == me_name:
            continue
        if mine and _hostport(url) == mine:
            continue
        out.append({"name": name, "url": url, "token": str(b.get("token") or "")})
    return out


def _hostport(url):
    try:
        import urllib.parse
        u = urllib.parse.urlparse(url)
        return (u.hostname or "", u.port or (443 if u.scheme == "https" else 80))
    except Exception:
        return ""


def call(peer, path, method="GET", body=None, timeout=TIMEOUT):
    """打另一侧的一个 larkbot 端点。→ (数据, 错误说明)。

    错误说明是给人看的一句话,不是异常:那一侧关机、不在内网、票过期都很正常,
    页面该显示「连不上」而不是塌掉。
    """
    path_only = path.split("?", 1)[0]
    if path_only not in ALLOW_PATHS and not any(path_only.startswith(prefix) for prefix in ALLOW_PREFIXES):
        return None, f"不允许转发的路径:{path}"
    url = peer["url"].rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header(HOP_HEADER, "1")
    if peer.get("token"):
        req.add_header("Authorization", "Bearer " + peer["token"])
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}"), ""
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "票不对(那一侧的令牌变过?重新部署一次会同步)"
        try:
            return json.loads(e.read().decode() or "{}"), ""
        except Exception:
            return None, f"HTTP {e.code}"
    except Exception as e:
        return None, f"连不上:{str(e)[:100]}"


def find(name, me_url="", me_name=""):
    """按 name 找一个后端。→ (peer, 错误说明)。"""
    for p in others(me_url, me_name):
        if p["name"] == name:
            return p, ""
    return None, f"没有叫 {name} 的后端(在 ~/.config/desktron/backends.json 里配)"


def is_hop(headers):
    """这个请求是不是从另一侧转过来的。是的话不再往外聚合。"""
    try:
        return bool(headers.get(HOP_HEADER))
    except Exception:
        return False
