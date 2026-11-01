"""飞书开放平台的出站面(REST,纯标准库 urllib)。

出站不走长连接:发消息是普通 HTTP,没有理由绕到 Node 那一侧去。这么划有个更要紧的
后果——红线闸(redline.py)与出口在同一个进程、同一个调用栈里,Node 那层碰不到出站,
也就没有绕过闸的路径。分工的理由在这里,不只是省事。

凭证只从本机 SECRETS(~/.config/desktron/env)读,不进仓库、不落 .workbench。
agent 声明里只写 app_id(它不是秘密),secret 按 app_id 拼出键名去 SECRETS 找。
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

#: 域名。国内 feishu,海外 larksuite。按 agent 声明里的 domain 选,默认 feishu。
DOMAINS = {
    "feishu": "https://open.feishu.cn",
    "larksuite": "https://open.larksuite.com",
}
DEFAULT_DOMAIN = "feishu"

#: token 的键名规则。一个 app 一份:同一台机器上可能同时跑几个 bot。
SECRET_KEY_FMT = "LARK_APP_SECRET__{app_id}"

_tok_lock = threading.Lock()
_tok_cache = {}          # app_id → (token, 到期时间戳)

def _open(req, timeout):
    """按 urllib 的代理与 NO_PROXY 配置发请求。

    不在失败后自动换路重发，避免发消息等写请求重复执行。
    """
    return urllib.request.urlopen(req, timeout=timeout)


def base(domain=""):
    return DOMAINS.get(domain or DEFAULT_DOMAIN, DOMAINS[DEFAULT_DOMAIN])


def app_secret(app_id):
    """从本机 SECRETS 取 app_secret;没有返回空串(调用方据此报「未配置凭证」)。"""
    try:
        from credstore import _secrets_read
        d = _secrets_read()
    except Exception:
        return ""
    return str(d.get(SECRET_KEY_FMT.format(app_id=app_id), "")).strip()


def _post(url, payload, token="", timeout=20, method="POST"):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with _open(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        # 飞书把业务错误也放在 body 里,HTTP 码未必是 200——不读 body 就丢了 code 与 msg,
        # 而排查全靠那个 code(比如 99991663 = token 过期,230002 = 机器人不在群里)
        try:
            return json.loads(e.read().decode() or "{}")
        except Exception:
            return {"code": -1, "msg": f"HTTP {e.code}"}
    except Exception as e:
        return {"code": -1, "msg": str(e)[:200]}


def _get(url, token="", timeout=20):
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with _open(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode() or "{}")
        except Exception:
            return {"code": -1, "msg": f"HTTP {e.code}"}
    except Exception as e:
        return {"code": -1, "msg": str(e)[:200]}


def tenant_token(app_id, domain="", force=False):
    """tenant_access_token,带进程内缓存。

    官方给的有效期是 7200 秒,这里按 6600 秒过期——提前 10 分钟换,免得一条消息
    正好卡在边界上拿到一个下一秒失效的 token。飞书那边重复申请会返回同一个,不怕多调。
    """
    with _tok_lock:
        hit = _tok_cache.get(app_id)
        if hit and not force and hit[1] > time.time():
            return hit[0], ""
    sec = app_secret(app_id)
    if not sec:
        return "", f"未配置 app_secret(在 ~/.config/desktron/env 加 {SECRET_KEY_FMT.format(app_id=app_id)})"
    r = _post(base(domain) + "/open-apis/auth/v3/tenant_access_token/internal",
              {"app_id": app_id, "app_secret": sec})
    tok = r.get("tenant_access_token") or ""
    if not tok:
        return "", f"取 token 失败:code={r.get('code')} {str(r.get('msg'))[:100]}"
    with _tok_lock:
        _tok_cache[app_id] = (tok, time.time() + min(int(r.get("expire") or 7200), 7200) - 600)
    return tok, ""


def _call(app_id, domain, path, payload=None, method="POST"):
    """带一次 token 重取的调用。99991663/99991661 = token 失效,换一张再来一次。
    只重一次:再失败就是凭证或权限的问题,重试解决不了,反而把日志刷满。"""
    for attempt in (0, 1):
        tok, err = tenant_token(app_id, domain, force=bool(attempt))
        if err:
            return {"code": -1, "msg": err}
        url = base(domain) + path
        r = _post(url, payload, tok, method=method) if method != "GET" else _get(url, tok)
        if r.get("code") in (99991663, 99991661) and attempt == 0:
            continue
        return r
    return {"code": -1, "msg": "unreachable"}


# ---- 消息 ----

def send_text(app_id, chat_id, text, domain=""):
    return _call(app_id, domain, "/open-apis/im/v1/messages?receive_id_type=chat_id",
                 {"receive_id": chat_id, "msg_type": "text",
                  "content": json.dumps({"text": text}, ensure_ascii=False)})


def reply_text(app_id, message_id, text, domain="", in_thread=False):
    """回到某条消息下。in_thread=True 时回进它所在的话题(飞书的 reply_in_thread)。"""
    return _call(app_id, domain,
                 f"/open-apis/im/v1/messages/{urllib.parse.quote(message_id)}/reply",
                 {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False),
                  "reply_in_thread": bool(in_thread)})


def send_card(app_id, chat_id, card, domain=""):
    return _call(app_id, domain, "/open-apis/im/v1/messages?receive_id_type=chat_id",
                 {"receive_id": chat_id, "msg_type": "interactive",
                  "content": json.dumps(card, ensure_ascii=False)})


def reply_card(app_id, message_id, card, domain="", in_thread=False):
    return _call(app_id, domain,
                 f"/open-apis/im/v1/messages/{urllib.parse.quote(message_id)}/reply",
                 {"msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False),
                  "reply_in_thread": bool(in_thread)})


def update_card(app_id, message_id, card, domain=""):
    """原地改一条已发出的卡片。长回答的流式刷新靠它:先发一张「思考中」,
    边生成边 PATCH,人不用干等一分钟看不到任何动静。
    飞书对同一条消息的更新有频率限制,调用方自己节流(见 outbound.py 的 MIN_GAP)。"""
    return _call(app_id, domain,
                 f"/open-apis/im/v1/messages/{urllib.parse.quote(message_id)}",
                 {"content": json.dumps(card, ensure_ascii=False)}, method="PATCH")


def get_message(app_id, message_id, domain=""):
    """按 id 取一条消息的完整内容。→ (消息 dict, err)。

    引用回复要用它:事件里只有 parent_id,被引用那条的正文与图片一概不带。不取回来的话,
    人在飞书里明明是「指着这张图问」,模型收到的却是一句没有指代对象的话
    (2026-09-10 用户实见:回复一条带图的消息问「你能看到这张图吗」,答「看不到」)。

    返回的字段名对齐事件里的 message:正文在 body.content,这里抄一份到 content,
    好让 _text_of 那一路两种来源同一套读法。
    """
    r = _call(app_id, domain,
              f"/open-apis/im/v1/messages/{urllib.parse.quote(message_id)}", method="GET")
    items = ((r.get("data") or {}).get("items") or [])
    if not items:
        return {}, f"取消息失败:code={r.get('code')} {str(r.get('msg'))[:100]}"
    it = dict(items[0] or {})
    body = it.get("body") or {}
    if not it.get("content") and body.get("content"):
        it["content"] = body.get("content")
    return it, ""


def download_resource(app_id, message_id, file_key, domain="", kind="image", timeout=30):
    """下载消息里的图片或文件,→ (bytes, err)。要开 im:resource 权限。

    不走 _call:那条路把响应当 JSON 解,而这里回来的是二进制。token 失效重取一次的
    做法与它一致。
    """
    for attempt in (0, 1):
        tok, err = tenant_token(app_id, domain, force=bool(attempt))
        if err:
            return b"", err
        url = (base(domain)
               + f"/open-apis/im/v1/messages/{urllib.parse.quote(message_id)}"
               + f"/resources/{urllib.parse.quote(file_key)}"
               + f"?type={urllib.parse.quote(kind)}")
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", "Bearer " + tok)
        try:
            with _open(req, timeout=timeout) as r:
                return r.read(), ""
        except urllib.error.HTTPError as e:
            try:
                d = json.loads(e.read().decode() or "{}")
            except Exception:
                d = {"code": -1, "msg": f"HTTP {e.code}"}
            if d.get("code") in (99991663, 99991661) and attempt == 0:
                continue
            return b"", f"下载失败:code={d.get('code')} {str(d.get('msg'))[:100]}"
        except Exception as e:
            return b"", str(e)[:200]
    return b"", "unreachable"


# ---- 身份与群 ----

def bot_info(app_id, domain=""):
    """→ ({open_id, app_id, name}, err)。@ 判定要拿它比对,启动时取一次即可。"""
    r = _call(app_id, domain, "/open-apis/bot/v3/info", method="GET")
    b = r.get("bot") or {}
    if not b:
        return {}, f"取 bot 信息失败:code={r.get('code')} {str(r.get('msg'))[:100]}"
    return {"open_id": b.get("open_id") or "", "app_id": app_id,
            "name": b.get("app_name") or "",
            # 开放平台上传的应用头像(公开 CDN 链接,非凭证)。面板卡片用它,
            # 让 Desk 里的 bot 与飞书里看到的是同一张脸。
            "avatar": b.get("avatar_url") or ""}, ""


_chat_mode_cache = {}    # (app_id, chat_id) → (mode, 到期)


def chat_mode(app_id, chat_id, domain="", ttl=300, force=False):
    """群的形态:"group" 普通群 / "topic" 话题群。缓存 5 分钟。

    这个值决定「不 @ 要不要理」,判错的后果两个方向都难受:话题群当普通群 = 每句都得 @,
    普通群当话题群 = 别人聊天它一直插嘴。所以 ttl 不敢太长,群形态是能被人随手改的。
    """
    k = (app_id, chat_id)
    hit = _chat_mode_cache.get(k)
    if hit and not force and hit[1] > time.time():
        return hit[0]
    r = _call(app_id, domain, f"/open-apis/im/v1/chats/{urllib.parse.quote(chat_id)}", method="GET")
    mode = ((r.get("data") or {}).get("chat_mode") or "") if "data" in r else (r.get("chat_mode") or "")
    mode = str(mode or "group")
    _chat_mode_cache[k] = (mode, time.time() + ttl)
    return mode


def ping(app_id, domain=""):
    """凭证与网络的连通性自检。→ (ok, 说明)。装完与改完配置各跑一次。"""
    tok, err = tenant_token(app_id, domain, force=True)
    if err:
        return False, err
    info, err2 = bot_info(app_id, domain)
    if err2:
        return False, err2
    return True, f"{info.get('name') or app_id} 就绪"
