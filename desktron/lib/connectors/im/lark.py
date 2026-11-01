"""飞书消息引用预览、深链及当前用户身份缓存。

保留对既有同步材料的只读访问；不再提供 Timeline 批量采集、群标签或关注集。
"""
import datetime
import glob
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse

from connectors.im import IMSpec, ThreadModel, PullMode, Surface, Identity

# ---- 同步池布局与深链(接口面②③) ----

POOL_SUBDIR = "lark"   # 既有本机同步材料的子目录


def pool_dir(sync_root, source):
    """原始条目目录:source ∈ {im, vc, doc}。"""
    return os.path.join(sync_root, POOL_SUBDIR, source)


def chat_link(chat_id):
    """群/单聊深链(飞书客户端 applink)。"""
    return f"https://applink.feishu.cn/client/chat/open?openChatId={chat_id}"


DOC_BASE = os.environ.get("DESKTRON_LARK_DOC_BASE", "https://www.feishu.cn").rstrip("/")


def doc_link(kind, token):
    """文档深链:kind ∈ {docx, wiki, docs}(分享链接原样带的类型)。

    租户域名因组织而异,由 DESKTRON_LARK_DOC_BASE 指定(如 https://<tenant>.feishu.cn)。"""
    return f"{DOC_BASE}/{kind}/{token}"


def doc_link_guess(token):
    """无类型信息时按 token 前缀猜(新式 wiki token 不带 wik 前缀,能配 meta url 就别用这个)。"""
    return doc_link("wiki" if token.startswith("wik") else "docx", token)


DOC_TOKEN_RE = re.compile(r"(docx|wiki|docs)/([A-Za-z0-9]{16,})")   # 消息里贴的飞书文档链接 → (类型, token)
DOC_URL_RE = re.compile(r"https://[\w.-]+/(?:docx|wiki|docs)/[\w-]+")   # 同步 meta 里的真实文档 url


# ---- 消息引用预览 ----------------------------------------------------------
#
# 飞书消息深链没有稳定的一种形状：新链接常带 messageId，旧链接只给 chat + position，
# 话题链接给 thread id，转发链接则只有 token。Desk 回答里引用到其中任一种时，都应该
# 在会话自己的侧栏还原原消息，而不是把人直接送出 Desk。
_MESSAGE_REF_PATHS = ("/client/chat/open", "/client/thread/open", "/client/message/link/open")
_MESSAGE_ID_RE = re.compile(r"(?:^|[^A-Za-z0-9])(om_[A-Za-z0-9_-]+)")


def parse_message_reference(url):
    """识别飞书消息深链并抽取可用于反查的稳定字段；普通群链接不冒充消息引用。"""
    raw = str(url or "").strip()
    if not raw or len(raw) > 8192:
        return None
    try:
        u = urllib.parse.urlparse(raw)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if not any(host == d or host.endswith("." + d)
               for d in ("feishu.cn", "larksuite.com")):
        return None
    path = (u.path or "").rstrip("/")
    if path not in _MESSAGE_REF_PATHS:
        return None
    q0 = urllib.parse.parse_qs(u.query)
    q = {str(k).lower(): str(v[0]) for k, v in q0.items() if v}
    mid = q.get("messageid") or q.get("message_id") or ""
    if not mid:
        m = _MESSAGE_ID_RE.search(raw)
        mid = m.group(1) if m else ""
    chat_id = q.get("openchatid") or q.get("open_chat_id") or q.get("chat_id") or ""
    thread_id = q.get("open_thread_id") or q.get("openthreadid") or ""
    thread_position = q.get("thread_position") or ""
    position = q.get("position") or q.get("message_position") or ""
    token = q.get("token") or ""
    # /client/chat/open?openChatId=... 只是普通会话入口。只有能指向具体位置的才接管。
    if path == "/client/chat/open" and not (mid or position):
        return None
    if path == "/client/thread/open" and not thread_id:
        return None
    if path == "/client/message/link/open" and not token:
        return None
    return {"url": raw, "message_id": mid, "chat_id": chat_id,
            "thread_id": thread_id, "thread_position": thread_position,
            "position": position, "token": token}


def _same_message_link(link, ref):
    """查询参数顺序或大小写变化不影响同一条 token / thread 深链的识别。"""
    other = parse_message_reference(link)
    if not other:
        return False
    if ref.get("token"):
        return other.get("token") == ref["token"]
    if ref.get("thread_id"):
        return (other.get("thread_id") == ref["thread_id"]
                and (not ref.get("thread_position")
                     or other.get("thread_position") == ref["thread_position"]))
    return str(link or "") == ref.get("url")


def _json_envelope(stdout):
    """lark-cli 偶尔在 JSON 前打印进度行，只截取第一个对象；成功以 ok=true 为准。"""
    out = str(stdout or "")
    if "{" not in out:
        return {}
    try:
        obj = json.loads(out[out.index("{"):])
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _messages_in(obj):
    """兼容 shortcut 不同版本的信封层级，找第一份 messages 数组。"""
    if isinstance(obj, dict):
        if isinstance(obj.get("messages"), list):
            return obj["messages"]
        for value in obj.values():
            got = _messages_in(value)
            if got is not None:
                return got
    return None


def _rg_paths(pool, needle, timeout=4):
    """在 4 万余条本地消息里按稳定字段定位少量候选，不逐文件启动 Python 解析。"""
    if not needle or not os.path.isdir(pool):
        return []
    base = ["rg", "-l", "--fixed-strings", needle]
    # 回答引用通常来自近两周。先只扫这些日期前缀，避免每次点击都读完整个 192MB 消息池；
    # 老链接再退回全池。日期只是性能提示，最终匹配仍以 JSON 字段严格复核。
    recent = []
    today = datetime.date.today()
    for off in range(14):
        recent.extend(["--glob", (today - datetime.timedelta(days=off)).isoformat() + "-*.json"])
    try:
        r = subprocess.run(base + recent + [pool], capture_output=True, text=True,
                           timeout=min(timeout, 2))
        if not (r.stdout or "").strip():
            r = subprocess.run(base + ["--glob", "*.json", pool],
                               capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return [p for p in (r.stdout or "").splitlines() if p][:80]


_MESSAGE_REF_PATH_CACHE = {}


def _cached_message(pool, ref):
    cache_key = (pool, ref.get("url") or "")
    cached = _MESSAGE_REF_PATH_CACHE.get(cache_key)
    if cached and os.path.isfile(cached):
        item = _read_json(cached)
        if isinstance(item, dict):
            if ref.get("thread_id") and not _same_message_link(item.get("message_app_link"), ref):
                reply = next((x for x in (item.get("thread_replies") or [])
                              if isinstance(x, dict)
                              and _same_message_link(x.get("message_app_link"), ref)), None)
                if reply is not None:
                    reply = dict(reply)
                    reply["_ref_thread_root"] = item
                    item = reply
            return cached, item
    candidates = []
    mid = ref.get("message_id") or ""
    if mid:
        candidates.extend(glob.glob(os.path.join(pool, "*-" + mid + ".json")))
    # position 在同一群内稳定；先按它缩小候选，再用 chat_id 严格复核。
    if not candidates and ref.get("position"):
        candidates.extend(_rg_paths(pool, '"message_position": "' + ref["position"] + '"'))
    # token / thread 链没有 position，按完整链接反查同步记录。
    if not candidates:
        candidates.extend(_rg_paths(pool, ref.get("token") or ref.get("thread_id") or ref.get("url") or ""))
    seen = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        item = _read_json(path)
        if not isinstance(item, dict):
            continue
        if mid and str(item.get("message_id") or "") != mid:
            continue
        if ref.get("chat_id") and str(item.get("chat_id") or "") != ref["chat_id"]:
            continue
        if ref.get("position") and str(item.get("message_position") or "") != ref["position"]:
            continue
        if (ref.get("token") or ref.get("thread_id")) and not (mid or ref.get("position")):
            if not _same_message_link(item.get("message_app_link"), ref):
                reply = next((x for x in (item.get("thread_replies") or [])
                              if isinstance(x, dict)
                              and _same_message_link(x.get("message_app_link"), ref)), None)
                if reply is None:
                    continue
                reply = dict(reply)
                reply["_ref_thread_root"] = item
                item = reply
        _MESSAGE_REF_PATH_CACHE[cache_key] = path
        return path, item
    return "", None


def _message_view(item, target_id=""):
    sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
    content = str(item.get("content") or "")
    if item.get("deleted"):
        content = "这条消息已撤回"
    return {
        "message_id": str(item.get("message_id") or ""),
        "sender": str(sender.get("name") or "未知成员"),
        "sender_type": str(sender.get("sender_type") or ""),
        "sender_id": str(sender.get("id") or ""),
        "avatar": str(sender.get("avatar") or sender.get("avatar_url") or ""),
        "create_time": str(item.get("create_time") or ""),
        "content": content[:20000],
        "msg_type": str(item.get("msg_type") or "text"),
        "updated": bool(item.get("updated")),
        "deleted": bool(item.get("deleted")),
        "is_self": bool(item.get("is_self")),
        "reactions": item.get("reactions") if isinstance(item.get("reactions"), dict) else None,
        "target": str(item.get("message_id") or "") == target_id,
    }


def _cached_context(pool, path, target, before=3, after=3):
    """按目标消息日期取同群邻近消息；跨午夜时把前后一天也纳入。"""
    root = target.get("_ref_thread_root") if isinstance(target.get("_ref_thread_root"), dict) else target
    replies = root.get("thread_replies") if isinstance(root.get("thread_replies"), list) else []
    if replies:
        target_id = str(target.get("message_id") or "")
        return [_message_view(item, target_id) for item in [root] + replies if isinstance(item, dict)]
    chat_id = str(target.get("chat_id") or "")
    target_id = str(target.get("message_id") or "")
    if not chat_id:
        return [_message_view(target, target_id)]
    day = str(target.get("create_time") or "")[:10]
    try:
        base = datetime.date.fromisoformat(day)
    except ValueError:
        return [_message_view(target, target_id)]
    # 绝大多数上下文都在同一天，只在午夜十分钟内把相邻日期也纳入。先让 rg 按 chat_id
    # 筛路径，再解析这一小撮 JSON；直接打开当天每一条消息在大群上仍要上千次文件读取。
    offsets = [0]
    clock = str(target.get("create_time") or "")[11:16]
    if clock and clock <= "00:10":
        offsets.insert(0, -1)
    if clock and clock >= "23:50":
        offsets.append(1)
    patterns = [(base + datetime.timedelta(days=off)).isoformat() + "-*.json" for off in offsets]
    args = ["rg", "-l", "--fixed-strings", '"chat_id": "' + chat_id + '"']
    for pattern in patterns:
        args.extend(["--glob", pattern])
    try:
        r = subprocess.run(args + [pool], capture_output=True, text=True, timeout=3)
        candidates = [p for p in (r.stdout or "").splitlines() if p]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        candidates = []
    if not candidates:
        candidates = []
        for pattern in patterns:
            candidates.extend(glob.glob(os.path.join(pool, pattern)))
    items = []
    for candidate in candidates:
        item = target if candidate == path else _read_json(candidate)
        if isinstance(item, dict) and str(item.get("chat_id") or "") == chat_id:
            items.append(item)
    def key(item):
        pos = str(item.get("message_position") or "")
        return (str(item.get("create_time") or ""), int(pos) if pos.isdigit() else 0,
                str(item.get("message_id") or ""))
    items.sort(key=key)
    at = next((i for i, item in enumerate(items)
               if str(item.get("message_id") or "") == target_id), -1)
    if at < 0:
        return [_message_view(target, target_id)]
    return [_message_view(item, target_id)
            for item in items[max(0, at - before):at + after + 1]]


def _fetch_message(message_id):
    """本地同步池没有命中时，使用当前用户身份只读拉取一条消息。"""
    try:
        r = subprocess.run(["lark-cli", "im", "+messages-mget", "--message-ids", message_id,
                            "--as", "user", "--format", "json"],
                           capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return None, "未安装 lark-cli"
    except subprocess.TimeoutExpired:
        return None, "飞书消息读取超时"
    data = _json_envelope(r.stdout)
    # ui_server 装有 SIGCHLD 收割器，returncode 偶尔不可信；JSON 成功信封才是契约。
    if data.get("ok") is True:
        messages = _messages_in(data) or []
        return (messages[0], "") if messages else (None, "没有找到这条消息")
    err = _json_envelope(r.stderr).get("error") or {}
    return None, str(err.get("message") or "飞书授权不足或登录已失效")[:240]


def _fetch_thread(thread_id):
    """本地同步池没有命中时，使用当前用户身份只读拉取话题消息。"""
    try:
        r = subprocess.run([
            "lark-cli", "im", "+threads-messages-list", "--thread", thread_id,
            "--order", "asc", "--page-size", "50", "--page-all",
            "--as", "user", "--format", "json",
        ], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return None, "未安装 lark-cli"
    except subprocess.TimeoutExpired:
        return None, "飞书话题读取超时"
    data = _json_envelope(r.stdout)
    if data.get("ok") is True:
        messages = [item for item in (_messages_in(data) or []) if isinstance(item, dict)]
        return (messages, "") if messages else (None, "这个飞书话题中没有可读取的消息")
    err = _json_envelope(r.stderr).get("error") or {}
    return None, str(err.get("message") or "飞书授权不足或登录已失效")[:240]


def message_reference(sync_root, url, cache_dir=""):
    """把一条飞书消息深链解析成侧栏可直接渲染的最小只读视图。"""
    ref = parse_message_reference(url)
    if not ref:
        return {"ok": False, "code": "not_message_reference", "error": "这不是可预览的飞书消息引用"}
    pool = pool_dir(sync_root, "im")
    path, target = _cached_message(pool, ref)
    if target is not None:
        messages = _cached_context(pool, path, target)
        source = "sync"
    elif ref.get("message_id"):
        target, error = _fetch_message(ref["message_id"])
        if target is None:
            return {"ok": False, "code": "message_unavailable", "error": error, "open_url": ref["url"]}
        target_id = str(target.get("message_id") or ref["message_id"])
        raw = [target] + [x for x in (target.get("thread_replies") or []) if isinstance(x, dict)]
        messages = [_message_view(item, target_id) for item in raw]
        source = "api"
    elif ref.get("thread_id"):
        raw, error = _fetch_thread(ref["thread_id"])
        if raw is None:
            return {"ok": False, "code": "message_unavailable", "error": error, "open_url": ref["url"]}
        target = next((item for item in raw
                       if _same_message_link(item.get("message_app_link"), ref)), None)
        if target is None and ref.get("thread_position"):
            target = next((item for item in raw
                           if str(item.get("thread_message_position") or "")
                           == ref["thread_position"]), None)
        # 飞书的 thread_position=-1 表示话题入口，而话题列表接口只返回正文/回复。
        # 此时以第一条可读消息作为侧栏锚点，仍展示完整话题上下文。
        target = target or raw[0]
        target_id = str(target.get("message_id") or "")
        messages = [_message_view(item, target_id) for item in raw if isinstance(item, dict)]
        source = "api"
    else:
        return {"ok": False, "code": "message_unresolved",
                "error": "这个飞书链接没有可解析的消息标识，本地同步记录中也未找到原消息",
                "open_url": ref["url"]}
    chat_id = str((target or {}).get("chat_id") or ref.get("chat_id") or "")
    names = _read_json(os.path.join(cache_dir, "lark-chat-names.json")) if cache_dir else {}
    chat_name = str((names or {}).get(chat_id) or "飞书会话")
    self_id = user_open_id_cached(cache_dir) if cache_dir else ""
    if self_id:
        for message in messages:
            message["is_self"] = str(message.get("sender_id") or "") == self_id
    return {"ok": True, "chat_name": chat_name, "messages": messages,
            "open_url": ref["url"], "source": source}


# ---- 消息预览与身份缓存共用的基础件 ----

def _ts(s):
    """"2026-07-19 20:14" → epoch(本地时区);解析失败返回 0。"""
    try:
        return time.mktime(time.strptime(str(s).strip()[:16], "%Y-%m-%d %H:%M"))
    except Exception:
        return 0


def _read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


_BG_REFRESH_LOCKS = {}
_BG_REFRESH_GUARD = threading.Lock()


def _bg_refresh(key, fn):
    """后台单飞刷新:同 key 只允许一个刷新线程在跑,抢不到锁直接放弃(下次请求再试)。
    请求路径上的慢活(全池扫描/lark-cli 调用)一律走这里,旧值先用不挡请求。"""
    with _BG_REFRESH_GUARD:
        lk = _BG_REFRESH_LOCKS.setdefault(key, threading.Lock())
    if lk.acquire(blocking=False):
        def _run():
            try:
                fn()
            finally:
                lk.release()
        threading.Thread(target=_run, daemon=True, name=f"lark-refresh-{key}").start()


def _self_fetch_write(cp):
    oid = ""
    try:
        import subprocess
        r = subprocess.run(["lark-cli", "auth", "status"], capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            oid = str((((json.loads(r.stdout) or {}).get("identities") or {}).get("user") or {}).get("openId") or "")
    except Exception:
        pass
    if oid:
        with open(cp, "w", encoding="utf-8") as f:
            json.dump({"open_id": oid}, f)


def user_open_id_cached(cache_dir, ttl=6 * 3600):
    """当前用户 open_id(lark-cli auth status),落盘缓存;拿不到给空串。
    有旧值时过期不挡请求:旧值先用,后台单飞重拉;只有从未拿到过才同步调 cli。"""
    os.makedirs(cache_dir, exist_ok=True)
    cp = os.path.join(cache_dir, "lark-self.json")
    old = (_read_json(cp) or {}).get("open_id") or ""
    if old:
        try:
            if time.time() - os.path.getmtime(cp) < ttl:
                return old
        except OSError:
            pass
        _bg_refresh("self", lambda: _self_fetch_write(cp))
        return old
    _self_fetch_write(cp)
    return (_read_json(cp) or {}).get("open_id") or ""


# ---- 描述符:能力声明(契约见 connectors/im/__init__.py) ----

SPEC = IMSpec(
    key="lark",
    name="飞书",
    thread_model=ThreadModel.TOPIC_GROUP,        # 话题群:1 话题 ↔ 1 会话(会话桥 2026-08-17 撤除,
                                                 # 这一项留着描述渠道本身的能力,不是某个消费方的现状)
    pull=PullMode.POLL_CLI,                      # 按消息链接只读查询 lark-cli
    surfaces=frozenset({Surface.CHAT}),           # 仅保留消息引用预览
    identity=Identity.USER,                      # 使用当前用户的飞书访问权限
    impl=__import__("sys").modules[__name__],
)
