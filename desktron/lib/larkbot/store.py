"""agent 声明与会话映射的持久化。

两份东西,分开放,因为寿命不同:
  .workbench/agents/<id>.json   声明。人写的,要能手改、能进版本控制、能带走。
  .workbench/agents/_state.json 运行态(锚点 → 会话)。机器写的,删了不心疼,重建即可。

声明里绝不出现 app_secret——凭证只在 ~/.config/desktron/env(见 larkapi.app_secret)。
这是红线,不是风格问题:.workbench 会随工作区一起被打包、被同步、被别人看到。
"""
import json
import os
import re
import threading
import time

#: bot 的代号。人自己起的短名,同时也是声明的文件名。面向用户一律叫「代号」不叫「id」——
#: 飞书那边的 App ID 也是个 id,两处都叫 id 时会填反。
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")

#: 飞书的 App ID。开放平台给的形如 cli_ 加一串十六进制。
#: 校验它不是为了挑剔格式,是为了拦下「把 App Secret 填进了这一栏」——
#: 那一栏会落进声明文件,而声明是要随工作区打包同步的。
APP_ID_RE = re.compile(r"^cli_[A-Za-z0-9]+$")

#: 声明的默认值。新建时不写进文件,读的时候补——这样默认值改了,老 agent 跟着变,
#: 而不是各自冻在创建那一刻的样子。
DEFAULTS = {
    "name": "",
    "lark_app_id": "",
    "domain": "feishu",
    "allowed_users": [],          # 空 = 不限制
    "allowed_chats": [],
    "engine": "claude",
    #: 引擎之下的第二根轴(参考 botmux 的 cliId × model):同一个引擎可以跑不同模型。
    #: 空 = 引擎自己的默认。模型名是引擎专属的,所以换引擎时要一并清掉(见 handler)。
    "model": "",
    #: 它在哪儿干活。空 = 工作台根(Desk 进程的工作目录)。记忆与知识是全局检索的,
    #: 与这一项无关;跟着它走的是脚下的东西——代码仓与工作目录里的资料。
    "cwd": "",
    "enabled": True,
    #: 应用头像的 CDN 链接。自检(ping)成功时从 bot/v3/info 顺手取回,展示用的事实
    #: 记录,不是配置——想换头像去开放平台改,下次自检就跟过来。
    "avatar": "",
    #: 这份声明是在哪台机器上建的(remote_auth.desk_id())。不是配置项,是事实记录:
    #: 本机与服务器的 workspace 各自独立,sync-home 同步的是 home/(代码)不是运行态,
    #: 所以声明本来就不跨侧——在哪一侧配就在哪一侧跑。
    #: 记它是为了认出「被人整个拷过来的那一份」:两处同时对一个飞书 app 建长连接时
    #: 飞书会把消息推给两边、bot 答两遍,而这个现象很难反推到「另一台也在跑」。
    #: 只提示不拦——拷过来接着用是正当需求,拦了反而挡路。
    "created_on": "",
    "runtime": {
        # fast 隔离交互式客户端的插件/MCP/会话配置并降低推理档，适合消息型 Bot；
        # balanced 保留引擎原配置，供确实依赖自定义插件或高推理强度的 Agent 使用。
        "performance": "fast",
    },
    "context": {
        "routing": True,          # 第 0 段:它在哪儿、东西在哪(context._routing)
        "role": "",               # 系统提示词
        "memory": True,           # sot + notes,全工作台检索
        "knowledge": True,        # SOT 层的 kind=fact
        "history": 12,            # 本锚点保留几轮;0 = 不带历史
        "memory_max": 20,         # 记忆最多取几条
        "knowledge_max": 10,
    },
    "outbound": {
        "strict": False,          # warn 是否升为 block
        "compliance": True,       # 合规词检查
        "sealed": True,           # 封存指纹比对
        "card": True,             # 长回答用卡片流式刷新
        "names": [],              # 要保护的人名(与评价词同现才命中,warn 级)
        "names_file": "",         # 名字表文件,每行一个;相对路径按工作台根解析
    },
}


def dir_of(repo_root):
    return os.path.join(repo_root, ".workbench", "agents")


def _path(repo_root, aid):
    return os.path.join(dir_of(repo_root), f"{aid}.json")


def _merge(base, over):
    """浅层递归合并:只对 dict 递归,list 整体覆盖。
    list 不合并是有意的——allowed_users 配成空表意思是「不限制」,
    若与默认值合并,人就再也删不掉自己加过的条目。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _strip_defaults(d, base):
    """去掉与默认值相同的字段。落盘时用。

    不这么做的话,save 会把整份默认值一起写进文件,此后改 DEFAULTS 对存量 agent 就没用了——
    它们各自冻在创建那一刻的样子,而人以为改了。斜杠命令那条路尤其明显:它拿 load() 的结果
    (已补齐默认值)直接回存,一次 /history 就把当时的全部默认值固化下来。
    """
    out = {}
    for k, v in (d or {}).items():
        b = base.get(k) if isinstance(base, dict) else None
        if isinstance(v, dict) and isinstance(b, dict):
            sub = _strip_defaults(v, b)
            if sub:
                out[k] = sub
        elif v != b:
            out[k] = v
    return out


def load(repo_root, aid):
    """→ 声明(补齐默认值);不存在返回 None。"""
    try:
        with open(_path(repo_root, aid), encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return None
    except Exception:
        return None
    d = _merge(DEFAULTS, d)
    d["id"] = aid
    return d


def list_all(repo_root, enabled_only=False):
    out = []
    try:
        names = sorted(os.listdir(dir_of(repo_root)))
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        a = load(repo_root, fn[:-5])
        if a and (not enabled_only or a.get("enabled")):
            out.append(a)
    return out


def save(repo_root, aid, decl):
    """→ (声明, err)。代号与凭证的校验都在这里,不指望调用方守规矩。"""
    if not ID_RE.match(str(aid or "")):
        return None, "代号只能是小写字母数字与 _-,2~40 位"
    if str(aid).startswith("cli_"):
        # 代号与飞书的 App ID 挨着问,填反过(2026-08-25)。反过来那次连带把 Secret
        # 填进了 App ID 那一栏——所以这两条都拦,不只提示。
        return None, "这看着是飞书的 App ID。代号是你自己起的短名,App ID 填在 lark_app_id"
    app_id = str((decl or {}).get("lark_app_id") or "").strip()
    if app_id and not APP_ID_RE.match(app_id):
        # 拦的主要是「把 Secret 填进了 App ID 那一栏」:Secret 一旦落进声明,
        # 就随 .workbench 被打包、同步、被别人看到,而它本该只在 ~/.config/desktron/env。
        return None, ("App ID 得是 cli_ 开头(开放平台凭证页上那个)。"
                      "填进来的这串不像 App ID——如果它是 App Secret,不要写在这里,"
                      "建完之后单独填,它只进 ~/.config/desktron/env")
    bad = [k for k in (decl or {}) if "secret" in k.lower() or "token" in k.lower()]
    if bad:
        # 不是洁癖:.workbench 会随工作区被打包同步,凭证进去就出不来了
        return None, f"声明里不能出现凭证字段({', '.join(bad)});secret 放 ~/.config/desktron/env"
    d = _strip_defaults(_merge(DEFAULTS, decl or {}), DEFAULTS)
    d.pop("id", None)
    d["updated"] = int(time.time())
    if not d.get("created_on"):
        try:
            import remote_auth
            d["created_on"] = remote_auth.desk_id()
        except Exception:
            pass          # 认不出这台机器只是少一条提示,不该让存不下去
    os.makedirs(dir_of(repo_root), exist_ok=True)
    p = _path(repo_root, aid)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    d["id"] = aid
    return d, ""


def delete(repo_root, aid):
    try:
        os.remove(_path(repo_root, aid))
        return True
    except OSError:
        return False


# ---- 运行态:锚点 → 会话 ----

def _state_path(repo_root):
    return os.path.join(dir_of(repo_root), "_state.json")


#: 状态文件的进程内缓存:(内容, 落盘时的 (mtime_ns, size))。
#:
#: 一轮消息里 session_append / session / used_append 各自 load+save 一遍,同一份 JSON
#: 反复解析与序列化;会话与用量长起来后这一项随文件大小线性变慢(2026-08-27 复核)。
#: 缓存按 stat 校验:文件被别的进程改过(跨侧转发、手工编辑)就重读,不会读到陈旧的。
_STATE = {}
_STATE_LOCK = threading.Lock()


def _stat_key(p):
    try:
        st = os.stat(p)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def state_load(repo_root):
    p = _state_path(repo_root)
    with _STATE_LOCK:
        hit = _STATE.get(p)
        key = _stat_key(p)
        if hit and hit[1] == key and key is not None:
            return hit[0]
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            d = d if isinstance(d, dict) else {}
        except Exception:
            d = {}
        _STATE[p] = (d, key)
        return d


def state_save(repo_root, d):
    os.makedirs(dir_of(repo_root), exist_ok=True)
    p = _state_path(repo_root)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        # indent=2 去掉:这个文件是机器写机器读的,缩进只让它大一半、序列化慢一截。
        # 要人读时 python -m json.tool 一下即可。
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, p)
    with _STATE_LOCK:
        _STATE[p] = (d, _stat_key(p))


#: 一条会话记多少轮历史。超出的丢掉——这是磁盘上的上限,
#: 实际带进上下文的还要受 context.history 二次裁剪。
KEEP_TURNS = 40


def session(repo_root, aid, anchor):
    """取某个锚点的会话记录(没有则空)。结构:{turns:[{role,text,ts}], created}。"""
    st = state_load(repo_root)
    return (st.get("sessions") or {}).get(f"{aid}|{anchor}") or {"turns": [], "created": 0}


def turn_and_used(repo_root, aid, anchor, role, text, used=None, question=""):
    """追加一轮 + 记这一轮的用量,一次读、一次写。→ 这条会话(含刚追加的那轮)。

    此前 session_append / session / used_append 各自 load+save,一轮消息里同一份 JSON
    读写六次。合成一次不只是省 IO——三次写之间若有别的线程插入,后写的会覆盖前面的。
    """
    st = state_load(repo_root)
    k = f"{aid}|{anchor}"
    now = int(time.time())
    ss = st.setdefault("sessions", {})
    sess = ss.setdefault(k, {"turns": [], "created": now})
    sess["turns"].append({"role": role, "text": str(text or "")[:8000], "ts": now})
    sess["turns"] = sess["turns"][-KEEP_TURNS:]
    if used is not None:
        u = st.setdefault("used", {})
        arr = u.setdefault(k, [])
        arr.append({"q": str(question or "")[:120], "ts": now,
                    "memory": (used or {}).get("memory") or [],
                    "knowledge": (used or {}).get("knowledge") or [],
                    "history": (used or {}).get("history") or 0,
                    "role": bool((used or {}).get("role"))})
        u[k] = arr[-KEEP_USED:]
    state_save(repo_root, st)
    return sess


def session_append(repo_root, aid, anchor, role, text):
    """追加一轮。role 是 "user" 或 "bot"。"""
    st = state_load(repo_root)
    ss = st.setdefault("sessions", {})
    k = f"{aid}|{anchor}"
    s = ss.setdefault(k, {"turns": [], "created": int(time.time())})
    s["turns"].append({"role": role, "text": str(text or "")[:8000], "ts": int(time.time())})
    s["turns"] = s["turns"][-KEEP_TURNS:]
    state_save(repo_root, st)
    return s


def session_clear(repo_root, aid, anchor):
    """/clear 用。清空这条会话的历史,声明不动。"""
    st = state_load(repo_root)
    ss = st.setdefault("sessions", {})
    ss.pop(f"{aid}|{anchor}", None)
    state_save(repo_root, st)


#: 每条会话留几轮的上下文用量。留得住最近几轮就够——人调完一项会立刻再问一句,
#: 看的就是那一两轮。留多了只是让文件变大。
KEEP_USED = 5


def used_append(repo_root, aid, anchor, used, question=""):
    """记下这一轮实际读到了什么。

    这是「调得对不对」的唯一依据。配置说记忆是开的,不等于真读到了东西——检索词
    一个都没命中时实际是 0 条,而 /status 只会显示「记忆: 开」,人看不出问题在哪
    (2026-08-25 用户问「怎么优化 agent」时发现这个缺口)。
    """
    st = state_load(repo_root)
    u = st.setdefault("used", {})
    k = f"{aid}|{anchor}"
    arr = u.setdefault(k, [])
    arr.append({"q": str(question or "")[:120], "ts": int(time.time()),
                "memory": (used or {}).get("memory") or [],
                "knowledge": (used or {}).get("knowledge") or [],
                "history": (used or {}).get("history") or 0,
                "role": bool((used or {}).get("role"))})
    u[k] = arr[-KEEP_USED:]
    state_save(repo_root, st)


def used_recent(repo_root, aid, anchor="", n=KEEP_USED):
    """→ 最近几轮的用量。anchor 为空则跨会话取最近的几轮。"""
    u = (state_load(repo_root).get("used") or {})
    if anchor:
        return (u.get(f"{aid}|{anchor}") or [])[-n:][::-1]
    out = []
    for k, arr in u.items():
        if k.split("|", 1)[0] == aid:
            out += arr
    return sorted(out, key=lambda x: x.get("ts") or 0, reverse=True)[:n]


def activity(repo_root):
    """→ {aid: {last, week, q, mem, kn}}:卡片「最近」块的数据,一次遍历状态文件。

    last = 最后一轮的时间戳;week = 近 7 天答了几条(按 bot 角色的 turns 数——
    turns 每锚点有保留上限,重度使用时这个数会低估,展示端措辞按「不少于」理解);
    q/mem/kn = 最后一轮的问句摘要与实际读到的条数(取自 used)。
    """
    st = state_load(repo_root)
    out = {}
    week_ago = time.time() - 7 * 86400
    for k, s in (st.get("sessions") or {}).items():
        aid = k.split("|", 1)[0]
        a = out.setdefault(aid, {"last": 0, "week": 0, "q": "", "mem": 0, "kn": 0})
        for t in (s.get("turns") or []):
            ts = int(t.get("ts") or 0)
            a["last"] = max(a["last"], ts)
            if t.get("role") == "bot" and ts > week_ago:
                a["week"] += 1
    for k, arr in (st.get("used") or {}).items():
        aid = k.split("|", 1)[0]
        a = out.setdefault(aid, {"last": 0, "week": 0, "q": "", "mem": 0, "kn": 0})
        for u in arr:
            if int(u.get("ts") or 0) >= a.get("_uts", 0):
                a["_uts"] = int(u.get("ts") or 0)
                a["q"] = str(u.get("q") or "")
                a["mem"] = len(u.get("memory") or [])
                a["kn"] = len(u.get("knowledge") or [])
    for a in out.values():
        a.pop("_uts", None)
    return out


def held_path(repo_root):
    """被红线拦下的内容落这里,供 Desk 里查看。不进飞书、不进日志正文。"""
    return os.path.join(dir_of(repo_root), "_held.json")


def hold(repo_root, aid, anchor, text, hits):
    """记一条被拦的出站。只留最近 50 条——它是给人当场看的,不是审计账本。"""
    p = held_path(repo_root)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        items = d.get("items") if isinstance(d, dict) else None
    except Exception:
        items = None
    items = items if isinstance(items, list) else []
    items.append({"agent": aid, "anchor": anchor, "text": str(text or "")[:20000],
                  "hits": hits, "ts": int(time.time())})
    os.makedirs(dir_of(repo_root), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"items": items[-50:]}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
