"""上下文四段组装(docs/agent-hosting-design.md「上下文四段组装」)。

这是与 botmux 的分野:它把消息直接喂给 CLI、让引擎自己攒历史,`/compact`、`/context`、
`/clear` 都是字面透传下去的——它有意不碰这一层。我们碰,因为要控制的正是「模型看得见什么」。

四段,按顺序拼:
    1 角色   声明里的系统提示词
    2 记忆   notes 与 sot 中的 judgment，按消息内容检索并标明确认状态
    3 知识   sot 中的领域事实(kind=fact),按消息内容检索
    4 历史   本锚点的近 N 轮原话,超出按轮次裁,不做摘要压缩

第 4 段不压缩是有意的:摘要引入失真,而这一段的价值恰恰是原话。要省地方就少留几轮,
不要把十轮揉成一段"大意如此"——那样出错时没人能从上下文里看出错在哪。

第 2、3 段一律全局检索,不按工作流筛。工作流是 Desk 里给人分域的方式,而群里问什么
取决于那个群在聊什么,与 Desk 怎么分域无关——按工作流筛只会让它检索不到本该检索到的。
bot 是工作台这一层的代理,不属于任何一个工作流。

**_sealed 层从不进入这里。** 这是红线的第一道闸,也是最重要的一道:没进去的东西不会漏出去。
下面 TIERS 是白名单不是黑名单——白名单漏写一层顶多少些上下文,黑名单漏写一层就是泄露。
"""
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys

#: 允许进入上下文的记忆层。白名单。journal 太碎(流水,一天几十条),不进。
TIERS = ("sot", "notes")

#: 从不进入的层。列在这里只为让人一眼看见,实际拦截靠上面的白名单。
NEVER = ("_sealed",)

# 停用词。中文按字拆(那一串本身没有空格,用 split 会把整串当成一个元素,
# 结果单个字一个也不在表里——2026-08-24 就是这么错过一次),英文按空格拆。
_STOP_CN = "的了是在和与对我你他她它们这那有个及其为以到从被把并且但而或就都也很更最不没吧呢啊么吗请帮看下说做谁什么怎样如何"
_STOP_EN = ("a an the is are was were of to in on at for and or not do does did "
            "can could would should what how why when where who")
_STOP = set(_STOP_CN) | set(_STOP_EN.split())


def _trim(w):
    """剥掉中文段首尾的停用词。切出来的是连续汉字段,「上的」这种前后挂着虚词的
    很常见,剥完只剩一个字就该丢——单字词在 curator 里是子串匹配,会捞回一大片。"""
    while w and w[0] in _STOP:
        w = w[1:]
    while w and w[-1] in _STOP:
        w = w[:-1]
    return w


#: 一个检索词命中超过这么多条,就当它没有区分度,整个丢掉。
#: 实测「上的」在本工作台命中 11 条、另一个词 1 条——前者拉回来的全是噪音,
#: 而上下文里塞十条不相关的记忆比少一条相关的更糟:模型会去解释它们。
TERM_TOO_BROAD = 8


def _terms(text, k=6):
    """从消息里抽检索词。机械抽取,不接 LLM——这一步要快(每条消息都跑)且可预期。
    中文按 2 字以上的连续汉字段切,英文按单词切,去停用词,长的优先。"""
    t = str(text or "")
    words = re.findall(r"[一-鿿]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}", t)
    seen, out = set(), []
    for w in sorted(words, key=len, reverse=True):
        lw = w.lower()
        if lw in seen:
            continue
        # 中文没有分词,这里切出来的是连续汉字段,一段可能含好几个词。整段查表
        # 对「的了是在和与」这种全停用词的段无效(整段不在表里),所以中文再按单字判一次:
        # 每个字都是停用词就丢掉。英文按整词判,那一侧本来就是词。
        if lw in _STOP or (w[0] >= "一" and all(c in _STOP for c in w)):
            continue
        if w[0] >= "一":
            w = _trim(w)
            if len(w) < 2:
                continue
        seen.add(lw)
        out.append(w)
        if len(out) >= k:
            break
    return out


def _mem_read(repo_root):
    """mem_read.py 的路径。走它而不是直接读文件:直接开文件拿不到反向引用与冲突提示,
    也不计访问次数(那份计数是判断「哪些记忆真的在被用」的唯一依据)。"""
    p = os.path.join(repo_root, ".claude", "skills", "memory", "scripts", "mem_read.py")
    return p if os.path.isfile(p) else ""


_LINE = re.compile(r"^(\S+)\s+\((.*?)\)\s+(\S+)\s*$")


def _parse(out):
    """解析 curator search 的文本输出:
        <id>  (匹配位置)  <相对路径>
            <描述>
    没有 --json,只能解析文本。格式不合的行跳过——宁可少取几条,不要把报错文字
    当成记忆塞进上下文里。"""
    items, cur = [], None
    for ln in str(out or "").splitlines():
        if not ln.strip():
            continue
        if ln.startswith("    ") and cur:
            cur["desc"] = ln.strip()
            continue
        m = _LINE.match(ln)
        if m:
            cur = {"id": m.group(1), "where": m.group(2), "path": m.group(3), "desc": ""}
            items.append(cur)
        else:
            cur = None
    return items


def _search(repo_root, term, tier, timeout=12):
    mr = _mem_read(repo_root)
    if not mr:
        return []
    try:
        r = subprocess.run([sys.executable, mr, "search", term, "--tier", tier],
                           capture_output=True, text=True, timeout=timeout, cwd=repo_root)
    except Exception:
        return []
    return _parse(r.stdout)


def _body(repo_root, path, limit=1200):
    """取一条记忆的正文。路径由 curator 给出,这里只做一次归属校验——
    防的是路径里带 .. 跳出 .memory,以及任何指向 _sealed 的路径。
    后者理论上不会发生(白名单只查 sot/notes),但这一层的代价是一个 startswith,
    而漏一次的代价不可逆,所以留着。"""
    root = os.path.realpath(os.path.join(repo_root, ".memory"))
    p = os.path.realpath(os.path.join(root, path))
    if not p.startswith(root + os.sep):
        return ""
    rel = os.path.relpath(p, root)
    if rel.split(os.sep)[0] in NEVER or os.sep + "_sealed" + os.sep in os.sep + rel + os.sep:
        return ""
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()[:limit]
    except OSError:
        return ""


def _fetch(repo_root, terms, tiers):
    """一次取齐「词 × 层」结果。返回值可被记忆与知识两种视图共同裁剪。

    旧实现让 memory 查 sot+notes、knowledge 再查一遍 sot；两组线程池同时扫同一批
    文件，既多 50% 子进程，也会争磁盘。这里把共同的 sot 合成一次，但归并顺序不变。
    """
    if not terms or not tiers:
        return {}
    jobs = [(ti, term, tier) for ti, term in enumerate(terms) for tier in tiers]
    got = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
        futs = {ex.submit(_search, repo_root, term, tier): (ti, tier)
                for ti, term, tier in jobs}
        for f in concurrent.futures.as_completed(futs):
            try:
                got[futs[f]] = f.result()
            except Exception:
                got[futs[f]] = []
    return got


def _memory_meta(repo_root, path):
    """Read only classification metadata from an already authorized search hit."""
    root = os.path.realpath(os.path.join(repo_root, ".memory"))
    target = os.path.realpath(os.path.join(root, path))
    if not target.startswith(root + os.sep):
        return {}
    try:
        from curator.note import read_note
        return read_note(target)[0]
    except Exception:
        return {}


def _select(terms, tiers, cap, got, accept=None):
    """从已取回的结果按「词顺序 → 层顺序」去重裁剪。"""
    hit, order = {}, []
    for ti in range(len(terms)):
        for tier in tiers:
            items = got.get((ti, tier)) or []
            if len(items) > TERM_TOO_BROAD:
                continue
            for raw in items:
                if raw["id"] in hit or accept is not None and not accept(raw, tier):
                    continue
                it = dict(raw)          # 同一原始结果会被两个视图使用，不能原地改 tier
                it["tier"] = tier
                hit[it["id"]] = it
                order.append(it)
    return order[:cap]


def _pick(repo_root, text, tiers, cap):
    """按消息内容检索若干层,合并去重,取前 cap 条。

    (词 × 层)并发跑。每次 _search 是一个 mem_read 子进程,实测单次 1~1.9 秒(528 条库),
    串行下「6 词层」要 5.9 秒——那是人在飞书里干等的时间,而其中大部分是 curator 在
    各自扫盘,彼此无关。并发之后墙钟收敛到最慢的那一次(2026-08-27 实测)。

    并发不改语义:结果按「词的原顺序 → 层的原顺序」归并,与串行时一致——
    顺序是相关性的粗排(长词优先,见 _terms),乱序会让 cap 截出来的那几条变随机。
    早停(order >= cap*3)在并发下不再省时间,去掉:任务已经一起发出去了。
    """
    terms = _terms(text)
    if not terms:
        return []
    return _select(terms, tiers, cap, _fetch(repo_root, terms, tiers))


def _routing(repo_root, agent):
    """第 0 段:它在哪儿工作、东西在哪、什么时候去看。

    没有这一段时,bot 对「我是谁」唯一的信息来自引擎默认与 cwd 里的 AGENTS.md,
    于是把自己当 coding agent(2026-08-27 用户在飞书里实测,codex 自称编程代理)。

    只说入口与判据,不放正文:内联的成本随内容线性涨,路由是常数。
    只列真的在的东西:写了不存在的路径,模型会去读、读不到、然后开始猜,比不写更糟。
    """
    cwd = agent.get("cwd") or repo_root
    lines = [
        "## 你在哪儿工作",
        "",
        "你是 Desktron 工作台的 AI 代理,用户通过飞书与你交互。"
        "你不在终端里给人写代码——代码与文件是你查证问题的手段,不是交付物。",
        "回话按飞书的读法来:先说结论,必要再展开;不超过 300 字,除非用户明确要求"
        "(如「详细分析」)。",
        "",
        f"- 名称:{agent.get('name') or agent.get('id') or 'bot'}",
        f"- 工作目录:{cwd}",
    ]
    _cli_desc = {"lark-cli": "飞书:文档、群、日历"}
    clis = [c for c in _cli_desc if shutil.which(c)]
    if clis:
        lines.append("- 常用 CLI:" + "、".join(f"{c}({_cli_desc[c]})" for c in clis))
    mr = _mem_read(repo_root)
    if mr:
        lines.append(f"- 本工作台的记忆:经 {os.path.relpath(mr, repo_root)} 读,"
                     "不要直接开文件。这是工作台沉淀的判断与结论。")
    lines += ["", "红线内容不要发到飞书:封存的内容、任何凭证、对具名个人的评价。"]
    return "\n".join(lines)


def build(repo_root, agent, text, anchor, history=None, sender_name=""):
    """→ (拼好的 prompt, 用了什么的清单)。清单给 Desk 显示,让人看得见这一轮喂了什么。

    history 由调用方从 store.session 取,不在这里读文件——组装要能脱开磁盘单测。
    """
    cfg = agent.get("context") or {}
    parts, used = [], {"memory": [], "knowledge": [], "history": 0, "role": False}

    # 第 0 段排在角色之前:role 是「你是谁」,它先得知道「你在哪儿」。
    # 默认开;关掉的场景是「这个 bot 只答对话本身,不查任何东西」,那时它是纯噪音。
    if cfg.get("routing", True):
        parts.append(_routing(repo_root, agent))

    role = str(cfg.get("role") or "").strip()
    if role:
        parts.append(role)
        used["role"] = True

    # 一次取齐后分成两个视图。knowledge 是 sot 的子集，不能再起一批重复的 sot 搜索。
    _pre = {"m": [], "k": []}
    if cfg.get("memory") or cfg.get("knowledge"):
        terms = _terms(text)
        tiers = TIERS if cfg.get("memory") else ("sot",)
        fetched = _fetch(repo_root, terms, tiers)
        if cfg.get("memory"):
            def memory_item(raw, tier):
                return tier == "notes" or _memory_meta(repo_root, raw["path"]).get("kind", "judgment") == "judgment"
            _pre["m"] = _select(terms, TIERS, int(cfg.get("memory_max") or 20), fetched, memory_item)
        if cfg.get("knowledge"):
            def knowledge_item(raw, _tier):
                return _memory_meta(repo_root, raw["path"]).get("kind") == "fact"
            _pre["k"] = _select(terms, ("sot",), int(cfg.get("knowledge_max") or 10), fetched, knowledge_item)

    if cfg.get("memory"):
        got = _pre["m"]
        lines = []
        for it in got:
            b = _body(repo_root, it["path"], 600)
            if b:
                meta = _memory_meta(repo_root, it["path"])
                label = ("pinned" if meta.get("user_confirmed") is True else
                         "legacy-unconfirmed" if it["tier"] == "sot" else it["tier"])
                lines.append(f"[{label}/{it['id']}] {it['desc']}\n{b.strip()}")
                used["memory"].append(it["id"])
        if lines:
            parts.append("## 已知(来自本工作台的记忆)\n\n" + "\n\n".join(lines))

    if cfg.get("knowledge"):
        got = _pre["k"]
        lines = []
        for it in got:
            if it["id"] in used["memory"]:
                continue          # 记忆那段已经带过,不重复占地方
            b = _body(repo_root, it["path"], 800)
            if b:
                lines.append(f"[{it['id']}] {it['desc']}\n{b.strip()}")
                used["knowledge"].append(it["id"])
        if lines:
            parts.append("## 领域事实\n\n" + "\n\n".join(lines))

    n = int(cfg.get("history") or 0)
    turns = (history or [])[-n:] if n > 0 else []
    if turns:
        lines = [("对方" if t.get("role") == "user" else "你") + ": " + str(t.get("text") or "")
                 for t in turns]
        parts.append("## 之前说过的(本会话原话,未压缩)\n\n" + "\n".join(lines))
        used["history"] = len(turns)

    who = f"{sender_name} 说: " if sender_name else ""
    parts.append("## 现在\n\n" + who + str(text or ""))
    return "\n\n---\n\n".join(parts), used


def sealed_dir(repo_root):
    """封存目录,给 redline 用。这里只负责指出它在哪——本模块自己从不读它。"""
    return os.path.join(repo_root, ".memory", "_sealed")
