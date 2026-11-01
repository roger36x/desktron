"""工作区级任务清单 TASKS.md 的解析与行级读写(- [ ] 待办 / [~] 进行中 / [x] 完成)。
纯文本 ↔ 字段的叶子模块,不依赖 workbench/domains(避免循环 import)。
清单落 domains/<工作区>/TASKS.md,即会话 cwd:agent 不走 API,直接 Edit/cat 这份纯文本;
面板清单 UI 与 AGENTS.md 起手摘要经本模块读写同一份文件。跨会话/跨引擎的接续靠文件常驻,
不依赖任何 agent 在场——这是设计底线(任务管理器可纯 UI 独立使用)。
行级更新按「行号 + 现标题」双校验:agent 与面板并行改同一份文件时,行号漂移即报冲突,
由调用方重拉;只重写目标行,任务行之外的手写内容(标题/散文)逐字保留。
任务行语法沿用容器备忘录时代的约定(container_project.py,已退役),存量文件无需迁移。
v2 注记(最小子集,当前契约 desk-docs/reference/task-format.md):行尾 HTML 注释藏机器字段
`<!-- c:2026-07-14T17:05 d:... -->`(c=创建时间,d=闭环时间),渲染端不显示,存量无注记行照常解析;
看板(今日/本周/趋势/平均完成时长)全部由 c/d 派生,无人工填报。
"""
import datetime as _dt
import os
import re

TASKS_MD_NAME = "TASKS.md"
TASK_STATE_CHAR = {"todo": " ", "doing": "~", "done": "x"}
CHAR_TASK_STATE = {" ": "todo", "~": "doing", "x": "done"}
TASK_LINE_RE = re.compile(r"^-\s*\[([ x~])\]\s*(.*)$")


def _clean_title(s):
    return str(s or "").replace("\n", " ").strip()


ANNOT_RE = re.compile(r"\s*<!--\s*([^>]*?)\s*-->\s*$")


def _now_ts():
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M")


def _parse_annot(text):
    out = {}
    for tok in str(text or "").split():
        if ":" in tok:
            k, v = tok.split(":", 1)
            if k and v:
                out[k] = v
    return out


def _split_task_line(raw):
    """任务行 → (状态字符, 纯标题, 注记 dict);非任务行返回 None。标题不含行尾注记。"""
    m = TASK_LINE_RE.match(str(raw).rstrip())
    if not m:
        return None
    rest = m.group(2)
    annot = {}
    am = ANNOT_RE.search(rest)
    if am:
        annot = _parse_annot(am.group(1))
        rest = rest[: am.start()]
    return m.group(1), rest.strip(), annot


def _fmt_task_line(state_char, title, annot):
    tail = " ".join(f"{k}:{v}" for k, v in annot.items() if v)
    return f"- [{state_char}] {title}" + (f"   <!-- {tail} -->" if tail else "")


def _ts_date(v):
    try:
        return _dt.date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def _ts_dt(v):
    try:
        return _dt.datetime.strptime(str(v)[:16], "%Y-%m-%dT%H:%M")
    except Exception:
        return None


def tasks_md_path(domain, repo_root=None):
    """工作区任务清单的绝对路径:domains/<工作区>/TASKS.md;容器自定义子区(容器:子区)→
    domains/<容器>/<子区>/TASKS.md。恒落 $REPO_ROOT/domains 下——dir 指向外部真实仓的工作区
    (coding 项目 / Desk 开发)也不例外:清单是工作台自己的常驻资产,不写进真实仓。
    id 非法返回 None。repo_root 覆盖语义同 container_project.project_md_path。"""
    parts = str(domain or "").split(":")
    if not (1 <= len(parts) <= 2) or not all(re.fullmatch(r"[\w-]+", p or "") for p in parts):
        return None
    if repo_root is None:
        import deskcfg
        repo_root = deskcfg.REPO_ROOT
    return os.path.join(repo_root, "domains", *parts, TASKS_MD_NAME)


def read_tasks(path):
    """TASKS.md → 行级条目 [{line,title,state}](line 从 0 起,更新时定位用)。
    只认任务行,其余行跳过。文件缺失=空清单。"""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return []
    items = []
    for i, raw in enumerate(lines):
        parsed = _split_task_line(raw)
        if parsed:
            ch, title, annot = parsed
            it = {"line": i, "title": title, "state": CHAR_TASK_STATE.get(ch, "todo")}
            # due 与 c/d 不同:那两个是「发生过什么」由系统写,due 是「打算什么时候前做完」由人设。
            for k in ("t", "c", "d", "due", "flow", "fcur", "fdone", "note", "nl"):
                if annot.get(k):
                    it[k] = annot[k]
            items.append(it)
    return items


def tasks_summary(path):
    """{done, doing, total, items, stats}:面板角标、清单 UI、工作流页看板共用。
    stats 全部由 c/d 注记派生(无注记的存量行不进今日/本周/趋势,在办计数不受影响)。"""
    items = read_tasks(path)
    today = _dt.date.today()
    week_start = today - _dt.timedelta(days=today.weekday())
    prev_week_start = week_start - _dt.timedelta(days=7)
    tomorrow = today + _dt.timedelta(days=1)
    created = [(_ts_date(t.get("c")), t) for t in items]
    done_d = [(_ts_date(t.get("d")), t) for t in items if t["state"] == "done"]
    today_new = sum(1 for d, _ in created if d == today)
    today_touched = {id(t) for d, t in created if d == today} | {id(t) for d, t in done_d if d == today}
    week_touched = ({id(t) for d, t in created if d and week_start <= d <= today}
                    | {id(t) for d, t in done_d if d and week_start <= d <= today})
    week_done = sum(1 for d, _ in done_d if d and week_start <= d <= today)
    prev_week_done = sum(1 for d, _ in done_d if d and prev_week_start <= d < week_start)
    trend = [sum(1 for d, _ in done_d if d == today - _dt.timedelta(days=i)) for i in range(13, -1, -1)]

    def _avg_hours(lo, hi):
        spans = []
        for t in items:
            if t["state"] != "done":
                continue
            dd = _ts_date(t.get("d"))
            if not (dd and lo <= dd < hi):
                continue
            c1, d1 = _ts_dt(t.get("c")), _ts_dt(t.get("d"))
            if c1 and d1 and d1 >= c1:
                spans.append((d1 - c1).total_seconds() / 3600)
        return round(sum(spans) / len(spans), 1) if spans else None

    return {"done": sum(1 for t in items if t["state"] == "done"),
            "doing": sum(1 for t in items if t["state"] == "doing"),
            "total": len(items), "items": items,
            "stats": {"today_new": today_new, "today_touched": len(today_touched),
                      "week_touched": len(week_touched), "week_done": week_done,
                      "prev_week_done": prev_week_done,
                      "avg_hours": _avg_hours(week_start, tomorrow),
                      "avg_hours_prev": _avg_hours(prev_week_start, week_start),
                      "trend14": trend, "trend_total": sum(trend)}}


def _read_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()
    except Exception:
        return []


def _write_lines(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    os.replace(tmp, path)


def add_task(path, title):
    """末尾追加一条待办。成功返回 '',失败返回错误串(调用方直接透传给前端)。"""
    title = _clean_title(title)
    if not title:
        return "标题为空"
    lines = _read_lines(path)
    lines.append(_fmt_task_line(" ", title, {"c": _now_ts(), "t": _gen_tid(_existing_tids(lines))}))
    _write_lines(path, lines)
    return ""


def update_task(path, line, expect, state=None, title=None, delete=False, flow=None, fcur=None,
                fdone=None, nl=None):
    """行级更新/删除:line 定位 + expect(现标题)校验——agent 并行改过文件导致行号漂移时报冲突,
    调用方重拉全量,不盲改。成功返回 ''。
    flow=任务流节点串(URL 编码,「|」分隔;传空串=删除任务流),fcur=当前节点下标;都藏行尾注记。
    nl=节点↔任务关联表(`阶段.节点=任务 id`,逗号分隔),坐标与 fdone 同一套。"""
    lines = _read_lines(path)
    if not isinstance(line, int) or line < 0 or line >= len(lines):
        return "清单已变(行号越界),请刷新"
    parsed = _split_task_line(lines[line])
    if not parsed or parsed[1] != _clean_title(expect):
        return "清单已变(该行已被改动),请刷新"
    cur_ch, cur_title, annot = parsed
    if delete:
        del lines[line]
    else:
        ch = TASK_STATE_CHAR[state] if state in TASK_STATE_CHAR else cur_ch
        new_title = _clean_title(title) if title is not None else cur_title
        if not new_title:
            return "标题为空"
        if ch == "x" and cur_ch != "x":
            annot["d"] = _now_ts()      # 闭环时刻:看板完成趋势/MTTR 口径的数据源
        elif ch != "x" and cur_ch == "x":
            annot.pop("d", None)        # 勾回待办:闭环时间作废
        if flow is not None:
            f = str(flow).strip()
            if f and " " not in f:
                annot["flow"] = f
            else:
                annot.pop("flow", None)
                annot.pop("fcur", None)
            annot.pop("fdone", None)   # 节点集变了,旧进度下标作废
            annot.pop("nl", None)      # 同理:节点坐标变了,旧关联作废
        if fcur is not None and annot.get("flow"):
            try:
                annot["fcur"] = str(max(0, int(fcur)))
            except Exception:
                pass
        if fdone is not None and annot.get("flow"):
            v = str(fdone).strip()
            if v and " " not in v:
                annot["fdone"] = v      # 已完成节点集(逗号分隔的 阶段.节点 下标)
            else:
                annot.pop("fdone", None)
        if nl is not None and annot.get("flow"):
            v = str(nl).strip()
            if v and " " not in v:
                annot["nl"] = v         # 节点↔任务关联(阶段.节点=任务 id,逗号分隔)
            else:
                annot.pop("nl", None)
            annot.pop("fcur", None)     # 新口径接管,旧阶段游标退役
        lines[line] = _fmt_task_line(ch, new_title, annot)
    _write_lines(path, lines)
    return ""


def _existing_tids(lines):
    out = set()
    for ln in lines:
        pp = _split_task_line(ln)
        if pp and pp[2].get("t"):
            out.add(pp[2]["t"])
    return out


def ensure_ids(path):
    """给清单里所有还没有 t: 注记的任务补一个稳定 id,一次写回。返回补了几条。

    任务 id 原本只在「节点关联到某条任务」时才现取,于是绝大多数任务没有 id,
    界面上只能退回行号——而行号随增删漂移,交给 agent 之后未必还指着同一条。
    统一成人人都有 id 之后,行号就只是定位手段,不再出现在任何对外的说法里。
    """
    lines = _read_lines(path)
    used = _existing_tids(lines)
    n = 0
    for i, raw in enumerate(lines):
        parsed = _split_task_line(raw)
        if not parsed:
            continue
        ch, title, annot = parsed
        if annot.get("t"):
            continue
        annot["t"] = _gen_tid(used)
        used.add(annot["t"])
        lines[i] = _fmt_task_line(ch, title, annot)
        n += 1
    if n:
        _write_lines(path, lines)
    return n


def _gen_tid(existing):
    while True:
        tid = "%04x" % int.from_bytes(os.urandom(2), "big")
        if tid not in existing:
            return tid


def task_ensure_id(path, line, expect, create=True):
    """行校验后取任务稳定 id(t: 注记)。create=True 且缺 id 时生成短随机 id 写回;
    create=False 只读(无 id 返回空串,不动文件)。返回 (tid, err)。"""
    lines = _read_lines(path)
    if not isinstance(line, int) or line < 0 or line >= len(lines):
        return "", "清单已变(行号越界),请刷新"
    parsed = _split_task_line(lines[line])
    if not parsed or parsed[1] != _clean_title(expect):
        return "", "清单已变(该行已被改动),请刷新"
    ch, title, annot = parsed
    if annot.get("t"):
        return annot["t"], ""
    if not create:
        return "", ""
    tid = _gen_tid(_existing_tids(lines))
    annot["t"] = tid
    lines[line] = _fmt_task_line(ch, title, annot)
    _write_lines(path, lines)
    return tid, ""


def task_annot_set(path, line, expect, key, value):
    """行校验后设/清一个白名单注记(备忘绑定 note:、截止 due:)。value 空=删。成功返回 ''。"""
    if key not in ("note", "due"):
        return "不支持的注记"
    v = str(value or "").strip()
    if " " in v:
        return "注记值不能含空格"
    # due 只收 YYYY-MM-DD:它要参与排序与「今天到期」的比较,格式散了这两件事就没法做。
    # c:/d: 带时刻是因为它们记的是发生时间点,截止日到天就够。
    if key == "due" and v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return "截止日格式应为 YYYY-MM-DD"
    lines = _read_lines(path)
    if not isinstance(line, int) or line < 0 or line >= len(lines):
        return "清单已变(行号越界),请刷新"
    parsed = _split_task_line(lines[line])
    if not parsed or parsed[1] != _clean_title(expect):
        return "清单已变(该行已被改动),请刷新"
    ch, title, annot = parsed
    if v:
        annot[key] = v
    else:
        annot.pop(key, None)
    lines[line] = _fmt_task_line(ch, title, annot)
    _write_lines(path, lines)
    return ""


def task_line_annot(path, line, expect):
    """行校验后取注记 dict。返回 (annot, err)。"""
    lines = _read_lines(path)
    if not isinstance(line, int) or line < 0 or line >= len(lines):
        return {}, "清单已变(行号越界),请刷新"
    parsed = _split_task_line(lines[line])
    if not parsed or parsed[1] != _clean_title(expect):
        return {}, "清单已变(该行已被改动),请刷新"
    return parsed[2], ""


def _notes_dir(path):
    """任务备忘目录:TASKS.md 同级 tasknotes/(每任务一份 <tid>.md,贴图落 assets/)。"""
    return os.path.join(os.path.dirname(path), "tasknotes")


def task_note_read(path, tid):
    if not re.fullmatch(r"[0-9a-f]{4,8}", tid or ""):
        return ""
    try:
        with open(os.path.join(_notes_dir(path), tid + ".md"), encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def task_note_write(path, tid, text):
    if not re.fullmatch(r"[0-9a-f]{4,8}", tid or ""):
        return "id 非法"
    d = _notes_dir(path)
    os.makedirs(d, exist_ok=True)
    tgt = os.path.join(d, tid + ".md")
    tmp = tgt + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(text or ""))
    os.replace(tmp, tgt)
    return ""


def task_note_asset(path, tid, name, data):
    """备忘贴图/附件落盘:tasknotes/assets/<tid>-<ts>-<净名>。返回 (md 相对引用路径, err)。"""
    if not re.fullmatch(r"[0-9a-f]{4,8}", tid or ""):
        return "", "id 非法"
    safe = re.sub(r"[^\w.-]", "_", os.path.basename(str(name or "img.png")))[:80] or "img.png"
    fn = f"{tid}-{_dt.datetime.now().strftime('%H%M%S%f')[:10]}-{safe}"
    d = os.path.join(_notes_dir(path), "assets")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, fn), "wb") as f:
        f.write(data)
    return "assets/" + fn, ""


_MEMO_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)\s]*\)")


MEMO_HINT_LIMIT = 60   # 备忘索引行的提示长度:够判断这条要不要拉全文即可


def _memo_hint(repo_root, note_id, title="", limit=MEMO_HINT_LIMIT):
    """备忘的索引条目:返回 (一句提示, 全文字数)。取不到返回 ("", 0)。

    AGENTS.md 是索引不是内容。备忘存在备忘录应用自己的库(.workbench/notepad.json),
    整篇推进起手上下文等于把应用数据搬进每一次会话——八条未完成任务曾合计占 4939 字,
    而其中大多数与本次会话要做的那条无关。这里只给足以判断的一句,全文按 note id 现拉。
    备忘首行通常就是任务标题(面板新建时带过去的),提示里去掉它,不在索引里重复上一行。"""
    body = _memo_digest(repo_root, note_id, limit=100000)
    if not body:
        return "", 0
    flat = " ".join(body.split())
    t = " ".join(str(title or "").split())
    if t and flat.startswith(t):
        flat = flat[len(t):].lstrip(" ：:—-")
    return (flat if len(flat) <= limit else flat[:limit].rstrip() + "…"), len(body)


def _memo_digest(repo_root, note_id, limit=900):
    """任务备忘正文摘要(起手注入用):图片标记折成 [图]、压连续空行、超长截断。取不到返回 ''。"""
    if not (repo_root and note_id):
        return ""
    try:
        import notepad_store
        n = notepad_store.get_note(repo_root, note_id)
    except Exception:
        return ""
    if not n:
        return ""
    body = _MEMO_IMG_RE.sub("[图]", str(n.get("content") or ""))
    out, blank = [], False
    for ln in (x.rstrip() for x in body.split("\n")):
        if not ln.strip():
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(ln)
    body = "\n".join(out).strip()
    if len(body) > limit:
        body = body[:limit].rstrip() + "…(截断,全文在面板备忘)"
    return body


def agents_tasks_segment(path, repo_root=None):
    """AGENTS.md 的「当前待办」段:未完成 + 进行中列标题(带备忘快照),已完成只报数量。
    清单空/文件缺失返回 ''(不给每个工作区都塞一段空约定;第一条任务落盘后,下次会话起手即带)。
    repo_root 给了才内联备忘:任务行注记 note: 指向全局备忘录(.workbench/notepad.json)里的纪要,
    是用户在工作流页写的任务上下文(结论/计划/排除项),会话起手看不到它就等于盲推任务。"""
    s = tasks_summary(path)
    if not s["total"]:
        return ""
    out = ["## 当前任务",
           "",
           "> 界面与 `TASKS.md` 使用同一份清单。先用 `desk-task list --all` 查重；"
           "同一项工作沿用原任务 id，不为检查、测试或同步另建任务。"
           "用 `desk-task --task-id <id> claim|progress|complete` 更新，不要直接修改 `TASKS.md`。",
           "",
           "> 只有完成验收后才能关闭任务；待部署或待验收时写明下一步。"
           "发送最终答复前再次核对本回合实际推进的任务。"
           "若本会话补完了其他会话留下的部署、重启或验收，仍须沿用原任务 id 更新或关闭。",
           "",
           "> 需要写完成条件、依据或任务格式时，先读 `desk-docs/reference/task-format.md`，"
           "不要猜测隐藏字段。备忘带有 note id 时，处理任务前读取 `.workbench/notepad.json` 中对应内容；"
           "备忘只读，修改请在界面中完成。",
           ""]
    for t in s["items"]:
        if t["state"] != "done":
            out.append(f"- [{TASK_STATE_CHAR[t['state']]}] {t['title']}" +
                       (f" (任务 id:{t['t']})" if t.get('t') else ""))
            hint, size = _memo_hint(repo_root, t.get("note"), t["title"]) if repo_root else ("", 0)
            if hint:
                out.append(f"  备忘 note:{t.get('note')}({size} 字):{hint}")
    if s["done"]:
        out.append("")
        out.append(f"(另有 {s['done']} 项已完成,不展开,见 TASKS.md)")
    return "\n".join(out)
