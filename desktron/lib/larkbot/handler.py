"""一条飞书消息的完整处理链。

    事件 → 去重 → 路由(route) → 斜杠命令 or 组装(context) → 跑引擎(runner) → 出站(outbound)

出站那一步必经 outbound.send,那里面才有红线闸。本模块任何地方都不直接调 larkapi 发消息。

去重按 event_id:飞书长连接在未收到 ack、或客户端重连时会重推同一个事件。不去重的话
一句话会被回答两遍甚至三遍——而且因为每轮都重新组装上下文,两遍的答案还未必一样。
"""
import hashlib
import json
import os
import re
import threading
import time

from . import context, larkapi, outbound, route, runner, store

#: 已处理的 event_id。进程内即可:重推都发生在秒级,进程重启后飞书那边也不会再推很久以前的。
_seen = {}
_seen_lock = threading.Lock()
SEEN_TTL = 600


def _dedupe(event_id):
    """→ True 表示这条见过、该丢掉。"""
    if not event_id:
        return False
    now = time.time()
    with _seen_lock:
        for k, v in list(_seen.items()):
            if now - v > SEEN_TTL:
                _seen.pop(k, None)
        if event_id in _seen:
            return True
        _seen[event_id] = now
    return False


def _text_of(msg):
    """把飞书的消息内容取成纯文本。非文本类型给一个占位描述——
    模型至少该知道"这里有一张图",而不是收到一段空白。"""
    mt = msg.get("message_type") or msg.get("msg_type") or "text"
    raw = msg.get("content") or ""
    try:
        c = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        c = {}
    if mt == "text":
        return str(c.get("text") or "")
    if mt == "post":
        # 富文本:按段落把 text 节点拼起来,图片折成占位
        out = []
        for para in (((c.get("content") or c.get("zh_cn", {}).get("content")) or [])):
            for node in (para or []):
                if node.get("tag") == "text":
                    out.append(str(node.get("text") or ""))
                elif node.get("tag") == "img":
                    out.append("[图片]")
                elif node.get("tag") == "a":
                    out.append(f"{node.get('text') or ''}({node.get('href') or ''})")
            out.append("\n")
        return "".join(out).strip()
    if mt == "image":
        return "[图片]"
    if mt == "file":
        return f"[文件 {c.get('file_name') or ''}]"
    if mt == "audio":
        return "[语音]"
    return f"[{mt}]"


#: 一条消息最多取几张图。富文本里可以塞很多张,而每张都是一次下载加一次模型读取;
#: 真要看第八张的场景没有见过,超出的在正文里说一声就够。
MAX_IMAGES = 6

#: 落盘图片保留多久。它们躺在工作区里,只增不减迟早变成一堆没人认领的文件。
MEDIA_KEEP_S = 7 * 86400


def _image_keys(msg):
    """这条消息里的图片 key。图片消息一个,富文本里可能有好几个。"""
    mt = msg.get("message_type") or msg.get("msg_type") or "text"
    raw = msg.get("content") or ""
    try:
        c = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return []
    if mt == "image":
        k = str(c.get("image_key") or "")
        return [k] if k else []
    if mt == "post":
        out = []
        for para in (((c.get("content") or c.get("zh_cn", {}).get("content")) or [])):
            for node in (para or []):
                if node.get("tag") == "img":
                    k = str(node.get("image_key") or "")
                    if k:
                        out.append(k)
        return out
    return []


def _img_ext(raw):
    """按内容认扩展名,不信文件名。后缀写错的图,读它的工具多半就不认了。"""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def media_dir(repo_root):
    """图片落在工作区里,不放机器级数据目录:引擎是以工作区为 cwd 跑的,
    读 cwd 之外的绝对路径要过一道权限询问,而这条链上没有人能回答它。"""
    return os.path.join(repo_root, ".workbench", "agents", "_media")


def _media_sweep(d):
    try:
        cut = time.time() - MEDIA_KEEP_S
        for n in os.listdir(d):
            fp = os.path.join(d, n)
            try:
                if os.path.isfile(fp) and os.path.getmtime(fp) < cut:
                    os.remove(fp)
            except OSError:
                pass
    except OSError:
        pass


def _save_images(repo_root, app_id, message_id, keys, domain="", log=None):
    """把图片下载到工作区,→ 本地路径列表。

    取不到的那张跳过并记一行,不让整条消息因此不答:少一张图仍答得出话,整条哑掉不行。
    """
    if not (app_id and message_id and keys):
        return []
    d = media_dir(repo_root)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return []
    _media_sweep(d)
    out = []
    for k in keys[:MAX_IMAGES]:
        # key 是飞书给的外部输入,不让它决定落盘路径:只取哈希当文件名。
        stem = hashlib.sha1(f"{message_id}:{k}".encode()).hexdigest()[:16]
        hit = ""
        for ext in (".png", ".jpg", ".gif", ".webp"):
            fp = os.path.join(d, stem + ext)
            if os.path.exists(fp) and os.path.getsize(fp) > 0:
                hit = fp
                break
        if hit:
            out.append(hit)
            continue
        raw, err = larkapi.download_resource(app_id, message_id, k, domain)
        if err or not raw:
            if log:
                log(f"图片取不到({k[:12]}…):{err or '空内容'}")
            continue
        fp = os.path.join(d, stem + _img_ext(raw))
        try:
            with open(fp, "wb") as f:
                f.write(raw)
        except OSError as ex:
            if log:
                log(f"图片落盘失败:{str(ex)[:80]}")
            continue
        out.append(fp)
    return out


def _attachments(repo_root, agent, msg, log=None):
    """本轮要额外告诉模型的东西:被引用的那条消息,以及两条消息里的图片(已下载的本地路径)。

    → 一段追加到用户发言后面的文本;没有可带的就是空串。

    图片给的是路径而不是内容:各引擎的非交互调用都只收一段文本,但它们都有读文件的工具。
    路径带上,并且明说「用读文件的工具打开」——不说的话模型多半只会把它当一个字符串,
    答「我看不到图」,和没下载是一样的结果。
    """
    app_id = msg.get("_app_id") or agent.get("lark_app_id") or ""
    domain = agent.get("domain") or ""
    lines, shots, wanted = [], [], 0

    pid = str(msg.get("parent_id") or "")
    if pid and app_id:
        parent, err = larkapi.get_message(app_id, pid, domain)
        if err:
            if log:
                log(f"被引用的消息取不到:{err}")
        elif parent:
            ptext = _strip_mentions(_text_of(parent))
            if ptext:
                lines.append("## 对方引用的那条消息\n\n" + ptext)
            pkeys = _image_keys(parent)
            wanted += len(pkeys[:MAX_IMAGES])
            shots += _save_images(repo_root, app_id, pid, pkeys, domain, log)

    keys = _image_keys(msg)
    wanted += len(keys[:MAX_IMAGES])
    shots += _save_images(repo_root, app_id, msg.get("message_id") or "", keys, domain, log)

    # 有图却一张都没取下来:多半是这个应用没开 im:resource。让模型照实说,
    # 而不是回一句「我看不到图」——那句话人无从下手,他会以为是自己没发出去。
    if wanted and not shots:
        lines.append("## 这轮带着的图片\n\n"
                     f"对方发了 {wanted} 张图,但这一侧没能取下来(多半是这个飞书应用"
                     "没有开 im:resource 权限)。照实说明取不到图的原因,不要只答「我看不到图」。")

    if shots:
        lines.append("## 这轮带着的图片\n\n"
                     + "\n".join(f"- {p}" for p in shots)
                     + "\n\n这些是已经下载到本地的图片文件。要看内容就用读文件的工具打开这些路径,"
                       "不要回答「看不到图」。")
    return ("\n\n---\n\n" + "\n\n---\n\n".join(lines)) if lines else ""


_MENTION_RE = re.compile(r"@_user_\d+\s*")


def _strip_mentions(text):
    """去掉消息里的 @ 占位符。飞书把 @ 存成 @_user_1 这样的记号,
    留着会让模型以为对方在提一个叫 _user_1 的人。"""
    return _MENTION_RE.sub("", str(text or "")).strip()


# ---- 斜杠命令。人在群里就地改配置,不碰配置文件。----

def _slash(repo_root, agent, anchor, text):
    """→ (回复文本, 是否已处理)。没命中命令返回 (None, False)。"""
    t = text.strip()
    if not t.startswith("/"):
        return None, False
    cmd, _, arg = t[1:].partition(" ")
    cmd, arg = cmd.strip().lower(), arg.strip()
    aid = agent.get("id") or ""

    if cmd in ("clear", "reset"):
        store.session_clear(repo_root, aid, anchor)
        return "这条会话的历史已清空。声明没动。", True
    if cmd == "help":
        return ("/clear 清空本会话历史\n/status 看当前配置\n"
                "/role <文本> 改系统提示词\n"
                "/cd <路径> 改工作目录\n/engine <claude|codex|pi> 换引擎\n"
                "/model <名字> 引擎用哪个模型(- 回到默认;换引擎时自动清掉)\n"
                "/history <N> 带几轮历史(0=不带)\n/memory on|off\n/knowledge on|off\n"
                "/routing on|off 第 0 段(它在哪儿工作、东西在哪)\n"
                "/strict on|off 把红线的 warn 升为 block\n"
                "\n改配置也可以在 Desk 的代理 → 飞书 Bot 里点「配置」,"
                "系统提示词那种一整段的文字在那儿改更合适。"), True
    if cmd == "status":
        c, o = agent.get("context") or {}, agent.get("outbound") or {}
        s = store.session(repo_root, aid, anchor)
        return (f"agent: {agent.get('name') or aid}\n"
                f"引擎: {agent.get('engine')}"
                + (f" · 模型: {agent.get('model')}" if agent.get("model") else " · 模型: (引擎默认)")
                + f"\n目录: {agent.get('cwd') or '(工作台根)'}\n"
                f"记忆: {'开' if c.get('memory') else '关'} / 知识: {'开' if c.get('knowledge') else '关'}"
                f" / 历史: {c.get('history')} 轮\n"
                f"红线: {'严格' if o.get('strict') else '标准'}\n"
                f"本会话已有 {len(s.get('turns') or [])} 轮"
                + _used_line(repo_root, aid, anchor)), True

    setters = {
        "role":      ("context", "role", lambda v: v),
        # cwd 决定它在哪儿干活,也决定它脚下能看见什么(代码仓与工作目录里的资料)。
        # 记忆与知识不跟着它走,那两段是全工作台检索的。
        "cd":        (None, "cwd", lambda v: os.path.expanduser(v)),
        "engine":    (None, "engine", lambda v: v.lower()),
        # 模型名不校验:名单随引擎版本变,这里核不准;名字错了引擎自己会报,
        # 那句报错会原样回到飞书里。"-" = 清空,回到引擎默认。
        "model":     (None, "model", lambda v: "" if v.strip() in ("-", "默认") else v.strip()),
        "history":   ("context", "history", lambda v: max(0, min(40, int(v)))),
        "routing":   ("context", "routing", lambda v: v.lower() in ("on", "1", "true", "开")),
        "memory":    ("context", "memory", lambda v: v.lower() in ("on", "1", "true", "开")),
        "knowledge": ("context", "knowledge", lambda v: v.lower() in ("on", "1", "true", "开")),
        "strict":    ("outbound", "strict", lambda v: v.lower() in ("on", "1", "true", "开")),
    }
    if cmd not in setters:
        return f"不认识的命令 /{cmd},/help 看可用的。", True
    if not arg:
        return f"/{cmd} 要带参数,/help 看用法。", True
    grp, key, conv = setters[cmd]
    try:
        val = conv(arg)
    except Exception:
        return f"/{cmd} 的参数不对:{arg}", True
    if cmd == "engine" and val not in runner.ENGINES:
        return f"引擎只能是 {'/'.join(sorted(runner.ENGINES))}", True
    if cmd == "cd" and not os.path.isdir(val):
        return f"目录不存在:{val}", True
    decl = dict(agent)
    decl.pop("id", None)
    if grp:
        decl[grp] = dict(decl.get(grp) or {})
        decl[grp][key] = val
    else:
        decl[key] = val
    note = ""
    if cmd == "engine" and val != agent.get("engine") and agent.get("model"):
        # 模型名是引擎专属的(claude 的 opus 对 codex 毫无意义),带着旧值切过去,
        # 表现是引擎报参数错、一条消息都答不了。所以换引擎时强制清掉,并说一声。
        decl["model"] = ""
        note = f"(原模型 {agent.get('model')} 已清掉——模型名是引擎专属的,需要就再 /model)"
    saved, err = store.save(repo_root, aid, decl)
    if err:
        return "改不了:" + err, True
    if cmd == "model" and not val:
        return "model 已清空,回到引擎默认" + note, True
    return f"{cmd} 已改为 {val}" + (" " + note if note else ""), True


_names_cache = {}    # 文件路径 → (名字表, mtime)


def _used_line(repo_root, aid, anchor):
    """上一轮实际读到了什么。配置与用量是两回事:记忆开着而一条也没检索到时,
    实际读到 0 条,但开关仍显示「开」——不把用量摆出来,人看不出问题在哪。"""
    recent = store.used_recent(repo_root, aid, anchor, 1)
    if not recent:
        return "\n\n(还没有对话,没有用量可看)"
    u = recent[0]
    n_m, n_k = len(u.get("memory") or []), len(u.get("knowledge") or [])
    line = (f"\n\n上一轮实际读到:记忆 {n_m} 条 / 知识 {n_k} 条 / 历史 {u.get('history', 0)} 轮")
    if n_m:
        line += "\n  记忆:" + "、".join((u.get("memory") or [])[:6])
    if n_k:
        line += "\n  知识:" + "、".join((u.get("knowledge") or [])[:6])
    if not n_m and not n_k:
        # 这一句是这个功能的重点:开着却读到 0 条,得说清下一步往哪儿看。
        line += "\n  一条都没读到。多半是这句话里没有能检索的词,或者工作台还没沉淀过相关的记忆。"
    return line


def _names_for_redline(repo_root, agent):
    """给红线闸的人名表。

    原打算从 curator 的实体图谱取,但读入口只放行 backref/centrality/check/edges/
    expand/map/neighbors/path,没有"按类型列举"这一项,而 map 在未建簇的实例上直接报错。
    所以改成显式配置:声明里的 outbound.names,加一个可选的外部文件(每行一个名字)。

    显式配置在这里反而更合适——谁该被保护,用户自己最清楚,不该由图谱的完整程度决定。
    """
    names = list((agent.get("outbound") or {}).get("names") or [])
    path = str((agent.get("outbound") or {}).get("names_file") or "").strip()
    if path:
        p = os.path.expanduser(path)
        if not os.path.isabs(p):
            p = os.path.join(repo_root, p)
        try:
            mt = os.path.getmtime(p)
            hit = _names_cache.get(p)
            if hit and hit[1] == mt:
                names += hit[0]
            else:
                with open(p, encoding="utf-8") as f:
                    got = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
                _names_cache[p] = (got, mt)
                names += got
        except OSError:
            pass          # 文件读不到只少一个 warn 级提示,不该让整条出站失败
    return [n for n in names if len(str(n or "").strip()) >= 2][:500]


def _handle(repo_root, event, log=None):
    """处理一条 im.message.receive_v1。→ 结果字典(给 sidecar 看的,也进日志)。

    整个函数不抛异常:它跑在 HTTP 请求线程里,抛出去 sidecar 只会看到 500,
    而真正的原因(哪个 agent、卡在哪一步)就丢了。所有失败都变成带 reason 的返回值。
    """
    started = last = time.monotonic()
    phase, aid = "prepare", ""
    timings = {}

    def _advance(name):
        nonlocal last, phase
        now = time.monotonic()
        timings[phase] = round((now - last) * 1000, 1)
        last, phase = now, name

    def _log(m):
        if log:
            try:
                log("larkbot", m)
            except Exception:
                pass

    try:
        eid = event.get("event_id") or ""
        if _dedupe(eid):
            return {"ok": True, "skipped": "duplicate", "event_id": eid}

        msg = dict(event.get("message") or {})
        msg["_app_id"] = event.get("app_id") or ""
        sender = (event.get("sender") or {})
        msg["sender"] = sender.get("sender_id") or sender.get("id") or {}

        agents = store.list_all(repo_root, enabled_only=True)
        if not agents:
            return {"ok": True, "skipped": "no-agent"}

        # 群形态要先查:它决定"不 @ 要不要理"。私聊不必查,省一次网络往返。
        mode = ""
        _advance("chat_lookup")
        if (msg.get("chat_type") or "group") != "p2p":
            app_id = msg["_app_id"] or (agents[0].get("lark_app_id") or "")
            if app_id and msg.get("chat_id"):
                mode = larkapi.chat_mode(app_id, msg["chat_id"], agents[0].get("domain") or "")

        def _ids(a):
            return [a.get("lark_app_id") or "", (a.get("_bot_open_id") or "")]

        # bot 的 open_id 要向飞书问,问到之后写回声明的运行态字段(下划线开头,不落盘)
        _advance("bot_lookup")
        for a in agents:
            if not a.get("_bot_open_id") and a.get("lark_app_id"):
                info, err = larkapi.bot_info(a["lark_app_id"], a.get("domain") or "")
                a["_bot_open_id"] = info.get("open_id") or ""
                if err:
                    _log(f"{a.get('id')}: {err}")

        _advance("route")
        agent, anchor, kind, why = route.pick(msg, agents, _ids, mode)
        if not agent:
            return {"ok": True, "skipped": why, "event_id": eid}

        text = _strip_mentions(_text_of(msg))
        if not text:
            return {"ok": True, "skipped": "empty-text"}

        # 凭证在这里挡一道,而不是等到出站才发现。没有 secret 就发不出消息,再往下走
        # 只会白跑一轮引擎(几十秒到几分钟),最后仍然是同一个错。早失败,理由也更清楚。
        if not larkapi.app_secret(agent.get("lark_app_id") or ""):
            _log(f"{agent.get('id')}: 未配置 app_secret,这条消息不处理")
            return {"ok": False, "skipped": "no-secret", "agent": agent.get("id")}

        chat_id = msg.get("chat_id") or ""
        mid = msg.get("message_id") or ""
        in_thread = kind in ("real-thread", "topic-chat", "regular-group-thread")
        aid = agent.get("id") or ""

        reply, done = _slash(repo_root, agent, anchor, text)
        if done:
            _advance("send")
            r = outbound.send(repo_root, agent, reply, chat_id, mid, in_thread, anchor,
                              sealed_dir=context.sealed_dir(repo_root))
            return {"ok": r["ok"], "agent": aid, "anchor": anchor, "slash": True, "err": r["err"]}

        # 占位卡片是一次飞书网络往返，与本地组装上下文互不依赖。并行后首屏确认更早，
        # 且不会把这次 RTT 叠加到引擎启动前；进入 runner 前 join，仍保证流式更新有 mid。
        card_box = {"mid": "", "err": ""}
        card_thread = None
        if (agent.get("outbound") or {}).get("card", True):
            def _open_card():
                try:
                    card_box["mid"], card_box["err"] = outbound.open_placeholder(
                        agent, chat_id, mid, in_thread)
                except Exception as ex:
                    card_box["err"] = f"占位卡片发送异常:{str(ex)[:120]}"
            card_thread = threading.Thread(target=_open_card, daemon=True,
                                           name=f"larkbot-card-{aid[:16]}")
            card_thread.start()

        # 被引用的那条消息与两条消息里的图片。放在占位卡片发出之后:两边都是飞书往返,
        # 让它们叠在一起,人看到「思考中」的时刻不因为多取一张图而推迟。
        _advance("attach")
        try:
            attach = _attachments(repo_root, agent, msg, _log)
        except Exception as ex:
            attach = ""
            _log(f"{aid}: 取引用与图片失败:{str(ex)[:120]}")

        # 追加这轮用户发言并取回整条会话:一次读一次写(store.turn_and_used)。
        # 用量在下面 build 之后补记——那时才知道实际读到了什么。
        _advance("context")
        sess = store.turn_and_used(repo_root, aid, anchor, "user", text)
        # 不传 sender_name:飞书给的是 open_id(ou_xxx),对模型没有意义,换成真名要再调一次
        # contact 接口。群里多人对话时这个信息才有价值,单人私聊没有,先不付那次往返。
        prompt, used = context.build(repo_root, agent, text, anchor,
                                     history=(sess.get("turns") or [])[:-1])
        # 接在组装之后,不混进 text:那个值还要去搜记忆(_terms)、还要落进会话历史。
        # 图片路径进了检索词是纯噪音,进了历史则会在文件被清掉之后留下一串死路径。
        prompt += attach

        # 记下这一轮实际读到了什么。人调完一项会立刻再问一句,看的就是这个——
        # 配置说记忆是开的,不等于真读到了东西。
        store.used_append(repo_root, aid, anchor, used, text)

        _advance("placeholder")
        if card_thread:
            card_thread.join()               # 网络调用自身有超时；不留下晚到的孤儿卡片
        card_mid, ph_err = card_box["mid"], card_box["err"]
        if ph_err:
            _log(f"{aid}: {ph_err}")         # 占位发不出去不算失败,退回一次性回复

        _advance("engine")
        th = outbound.Throttle()
        engine_metrics = {}
        out, err = runner.run(agent.get("engine") or "claude", prompt,
                              cwd=agent.get("cwd") or repo_root,
                              model=agent.get("model") or "", log=(log or None),
                              performance=(agent.get("runtime") or {}).get("performance") or "fast",
                              metrics=engine_metrics,
                              on_chunk=((lambda s: outbound.stream_progress(agent, card_mid, s, th))
                                        if card_mid else None))
        # runner 内部的首段/启动/认证是 engine 总耗时无法解释的部分；只记数字，不记正文。
        for key, value in engine_metrics.items():
            timings["engine_" + key] = value
        _advance("send")
        if err and not out:
            r = outbound.send(repo_root, agent, f"跑不动:{err}", chat_id, mid, in_thread, anchor,
                              sealed_dir=context.sealed_dir(repo_root), card_message_id=card_mid)
            return {"ok": False, "agent": aid, "anchor": anchor, "err": err}

        body = out + (f"\n\n---\n(注:{err})" if err else "")
        r = outbound.send(repo_root, agent, body, chat_id, mid, in_thread, anchor,
                          names=_names_for_redline(repo_root, agent),
                          sealed_dir=context.sealed_dir(repo_root),
                          card_message_id=card_mid)
        # 被闸拦下的内容不进历史:它没发出去,对面没看见,记进去下一轮模型会以为说过了
        _advance("persist")
        if not r.get("held"):
            store.session_append(repo_root, aid, anchor, "bot", body)
        _log(f"{aid} {kind} {anchor} 记忆{len(used['memory'])}条 知识{len(used['knowledge'])}条 "
             f"历史{used['history']}轮 红线={r['level']}" + (f" 出站失败:{r['err']}" if r["err"] else ""))
        return {"ok": r["ok"], "agent": aid, "anchor": anchor, "kind": kind,
                "level": r["level"], "held": r.get("held", False), "used": used, "err": r["err"]}
    except Exception as ex:
        _log(f"处理异常:{str(ex)[:300]}")
        return {"ok": False, "err": str(ex)[:300]}
    finally:
        _advance("done")
        timings["total"] = round((time.monotonic() - started) * 1000, 1)
        # 不记录消息正文、prompt 或凭证；失败与提前返回也保留已执行阶段的计时。
        _log(f"timing agent={aid or '-'} ms=" + json.dumps(timings, separators=(",", ":")))


def handle(repo_root, event, log=None):
    """登记飞书触发的 Agent 运行，使强制更新不会在处理中切换版本。"""
    import self_update
    with self_update.tracked_work(user_activity=True, critical="larkbot") as admitted:
        if not admitted:
            return {"ok": False, "skipped": "self-update-draining"}
        return _handle(repo_root, event, log)
