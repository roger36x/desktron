"""会话自动命名:模型给的名字单独存一层,永不与用户改的名混在一起。

为什么单独一个文件而不是写进 .workbench/custom-titles.json:那份是「用户手改」层,
优先级最高,清空即回落。自动名写进去会有两个后果——覆盖掉用户改的名,以及用户清空
自己的标题时连带把自动名一起清掉,分不清清的是哪一个。所以自动名自成一层,
排在「用户手改」与「引擎侧 /rename」之下、引擎派生标题之上(合并见 term_titles._display_title)。

这个模块被两侧引用:写在引擎适配器的 recap 支线里(engine_adapters/recap.py),
读在会话列表里(term_titles.py)。两侧都不 import 对方,只依赖 deskcfg 的根路径。
"""
import json
import os
import threading
import time

from deskcfg import REPO_ROOT

WB_AUTO_TITLES = os.path.join(REPO_ROOT, ".workbench", "auto-titles.json")
WB_CUSTOM_TITLES = os.path.join(REPO_ROOT, ".workbench", "custom-titles.json")
WB_AUTO_TITLE_STATE = os.path.join(REPO_ROOT, ".workbench", "auto-title-state.json")

# 只在会话刚开头命名。名字反复变比名字不准更难用:侧栏那一列是靠形状记住的,
# 每回合重算一次等于每回合把用户的定位打乱一次。拿到就定,之后不再改。
AUTOTITLE_MAX_TURNS = 2
AUTOTITLE_MAX_ATTEMPTS = 3
AUTOTITLE_MAX_CHARS = 24

_LOCK = threading.RLock()
_CACHE = {"t": 0.0, "v": {}}
_CACHE_TTL = 5.0

# 支线里跟 recap 一起要的那一行。放在正文之前,单独一行,便于原样切掉——
# 让模型在结尾追加会与「不写过程叙述」的要求打架,实测它更容易把标题写成一句总结。
AUTOTITLE_PROMPT = (
    "先另起一行写「标题: xxx」给这段会话命名:12 字以内,概括在做什么,"
    "不带标点结尾,不含引号。然后空一行,再按下面的要求写工作日志。\n\n"
)
_TITLE_MARKS = ("标题:", "标题：", "Title:", "title:")


def _entry_title(val):
    """两种落盘形状都认:{"title":..,"at":..} 是当前格式,裸字符串留给手工改过的文件。"""
    if isinstance(val, dict):
        val = val.get("title")
    return val.strip() if isinstance(val, str) and val.strip() else ""


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def auto_titles():
    """{sid: 标题};5s 轻缓存,与 term 的自定义标题同一量级(侧栏 1.5s 一轮)。"""
    now = time.time()
    if now - _CACHE["t"] < _CACHE_TTL:
        return _CACHE["v"]
    with _LOCK:
        if now - _CACHE["t"] < _CACHE_TTL:
            return _CACHE["v"]
        out = {}
        for sid, val in _read_json(WB_AUTO_TITLES).items():
            name = _entry_title(val)
            if name:
                out[str(sid)] = name
        _CACHE["t"], _CACHE["v"] = now, out
        return out


def auto_title_for(*ids):
    """按候选 id 顺序取自动标题(命中即返回);与 _custom_title_for 同签名,便于并排调用。"""
    titles = auto_titles()
    for i in ids:
        if i and titles.get(str(i)):
            return titles[str(i)]
    return None


def user_renamed(*ids):
    """用户在 Desk 侧手改过标题。引擎侧 /rename 不在这里判(要读各家落盘格式),
    由 term_titles._engine_renamed 在合并标题时兜住。"""
    custom = _read_json(WB_CUSTOM_TITLES)
    return any(i and str(custom.get(str(i)) or "").strip() for i in ids)


def _write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _retry_state(cur, keys):
    rows = [cur.get(k) for k in keys if isinstance(cur.get(k), dict)]
    def number(value):
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0
    return {
        "pending": any(bool(row.get("pending")) for row in rows),
        "attempts": max([number(row.get("attempts")) for row in rows] or [0]),
        "started_at": min([number(row.get("started_at")) for row in rows
                           if number(row.get("started_at")) > 0] or [0]),
    }


def _remember_pending(keys, state=None):
    """把刚进入命名窗口的会话标成待处理；后续回合不再依赖回合数猜它是否该重试。"""
    with _LOCK:
        cur = _read_json(WB_AUTO_TITLE_STATE)
        retry = state or _retry_state(cur, keys)
        now = int(time.time() * 1000)
        row = {"pending": True, "attempts": int(retry.get("attempts") or 0),
               "started_at": int(retry.get("started_at") or now)}
        for key in keys:
            cur[key] = row
        try:
            _write_json(WB_AUTO_TITLE_STATE, cur)
            return True
        except Exception:
            return False


def want_autotitle(sid, turns, *ids):
    """这段会话现在该不该向支线要一个名字。

    初次触发仍只发生在会话开头；一旦进入过命名窗口，就保持待处理状态，直到拿到标题
    或真实生成次数达到上限。新回合取消支线不会消耗次数，也不会把待处理状态抹掉。

    用户手改与已有自动标题仍是最前面的硬闸。尤其不能因为存在 pending 就绕过手改判断。
    用户手改的判定放在这里而不只放在写入前:支线本身要花一次生成,
    对已经有名字的会话连问都不该问。"""
    if not sid or turns < 1:
        return False
    keys = list(dict.fromkeys(str(i) for i in ((sid,) + tuple(ids)) if i))
    if user_renamed(*keys):
        with _LOCK:
            try:
                _drop_retry_state(keys)
            except Exception:
                pass
        return False
    if auto_title_for(*keys):
        return False
    with _LOCK:
        state = _retry_state(_read_json(WB_AUTO_TITLE_STATE), keys)
    if state["attempts"] >= AUTOTITLE_MAX_ATTEMPTS:
        return False
    if state["pending"]:
        return True
    if turns > AUTOTITLE_MAX_TURNS:
        return False
    return _remember_pending(keys, state)


def note_autotitle_miss(sid, *aliases):
    """记录一次真实生成却没给出可解析标题的结果；取消或未发起的支线不调用这里。"""
    keys = list(dict.fromkeys(str(i) for i in ((sid,) + aliases) if i))
    if not keys:
        return False
    with _LOCK:
        if user_renamed(*keys) or auto_title_for(*keys):
            try:
                _drop_retry_state(keys)
            except Exception:
                pass
            return False
        cur = _read_json(WB_AUTO_TITLE_STATE)
        state = _retry_state(cur, keys)
        attempts = min(AUTOTITLE_MAX_ATTEMPTS, state["attempts"] + 1)
        row = {"pending": attempts < AUTOTITLE_MAX_ATTEMPTS, "attempts": attempts,
               "started_at": state["started_at"] or int(time.time() * 1000),
               "last_miss_at": int(time.time() * 1000)}
        for key in keys:
            cur[key] = row
        try:
            _write_json(WB_AUTO_TITLE_STATE, cur)
            return attempts < AUTOTITLE_MAX_ATTEMPTS
        except Exception:
            return False


def _drop_retry_state(keys):
    cur = _read_json(WB_AUTO_TITLE_STATE)
    changed = any(k in cur for k in keys)
    if not changed:
        return False
    for key in keys:
        cur.pop(key, None)
    _write_json(WB_AUTO_TITLE_STATE, cur)
    return True


def split_autotitle(text):
    """从支线正文里切出「标题: xxx」那一行,返回 (标题, 去掉该行的正文)。

    模型偶尔会把标题写在开头却不另起一行,或在前面多一个空行;只认前三行,
    认不出就当它没写——宁可这次不命名,也不要把日志正文的首句当成标题。"""
    raw = str(text or "")
    lines = raw.split("\n")
    for i, line in enumerate(lines[:3]):
        s = line.strip()
        for mark in _TITLE_MARKS:
            if s.startswith(mark):
                title = s[len(mark):].strip().strip('"“”「」\'')
                rest = "\n".join(lines[:i] + lines[i + 1:]).strip()
                return title[:AUTOTITLE_MAX_CHARS], rest
    return "", raw.strip()


def set_auto_title(sid, title, *aliases):
    """落盘一条自动标题。已有记录不覆盖；拿到名字后结束持久化重试状态。

    同一段会话在两侧有两个 id:适配器自己的 adp_ id 与引擎侧 uuid。侧栏休眠行按引擎 uuid
    列,会话页按 adp_ id 寻址,取标题的那一侧未必知道另一个。所以两个键都写同一个值,
    任一侧查得到即可,不必在读取处再做一次映射。"""
    title = str(title or "").strip()[:AUTOTITLE_MAX_CHARS]
    if not sid or not title:
        return False
    keys = [str(k) for k in ((sid,) + aliases) if k]
    with _LOCK:
        # 支线生成需要几十秒。发起时没人改名，不代表返回时仍没人改；落盘前必须再判一次，
        # 否则模型的迟到结果仍会写进自动标题层，违背“人改后不再改”。
        if user_renamed(*keys):
            try:
                _drop_retry_state(keys)
            except Exception:
                pass
            return False
        cur = _read_json(WB_AUTO_TITLES)
        if any(_entry_title(cur.get(k)) for k in keys):
            return False
        for k in keys:
            cur[k] = {"title": title, "at": int(time.time() * 1000)}
        try:
            os.makedirs(os.path.dirname(WB_AUTO_TITLES), exist_ok=True)
            tmp = WB_AUTO_TITLES + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cur, fh, ensure_ascii=False)
            os.replace(tmp, WB_AUTO_TITLES)
        except Exception:
            return False
        try:
            _drop_retry_state(keys)
        except Exception:
            pass
        _CACHE["t"] = 0.0   # 失效缓存,下次读重载
        return True


def drop_auto_title(*ids):
    """删掉自动标题(会话删除时清理,或用户要求回落);两个 id 都传进来一并清。"""
    keys = [str(i) for i in ids if i]
    with _LOCK:
        cur = _read_json(WB_AUTO_TITLES)
        title_changed = any(k in cur for k in keys)
        for k in keys:
            cur.pop(k, None)
        try:
            if title_changed:
                _write_json(WB_AUTO_TITLES, cur)
            retry_changed = _drop_retry_state(keys)
        except Exception:
            return False
        _CACHE["t"] = 0.0
        return title_changed or retry_changed
