"""红线出站闸(docs/agent-hosting-design.md「红线出站」)。

这是整套东西里唯一不可省的一块,也是我们不复用 botmux 的理由:它的 hook 排在飞书 API
返回成功之后、且不读 hook 的返回值,只能旁观不能拦截。闸必须与出口在同一个进程里,
而且要在调飞书之前。

两道:
  第一道在取的时候——curator 的 _sealed 层从不进入上下文组装(见 context.py)。
  第二道在这里——防的是模型从别处(代码、文档、工具返回、别人贴的消息)读到红线内容后转述。

分两级,因为这两类的代价不对称:
  block  凭证与封存原文。漏一次不可逆(token 发出去就得换),宁可错拦。
  warn   具名个人的判断、合规敏感词。错拦的代价是人要多点一下,错放的代价是难收回,
         但这一类的模式识别本身不可靠,一律 block 会让它没法用。所以默认放行 + 留痕,
         由 agent 声明里的 strict 决定是否升级为 block。

拦下不静默:返回值带着被拦的原文与命中项,调用方负责在 Desk 里显示「有内容未发送」。
原文只留在本机,不进飞书、不进日志正文(日志只记命中的类别与位置)。
"""
import hashlib
import os
import re

# ---- 凭证。这一类判得准:它们有固定前缀或固定形状,不像自然语言那样模糊。----
_CRED = [
    ("aws-key",      re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
    ("slack-token",  re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("openai-key",   re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("lark-secret",  re.compile(r"\b(?:cli_[a-z0-9]{16,}|app_secret[\"'\s:=]+[A-Za-z0-9]{20,})", re.I)),
    ("private-key",  re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt",          re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    # 赋值形态的兜底:KEY=xxx / "token": "xxx"。要求右侧足够长且不像占位符,
    # 否则 `TOKEN=<your-token>` 这种文档示例会被误拦——那恰恰是该发出去的内容。
    ("assigned-secret", re.compile(
        r"\b(?:api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*[\"']?"
        r"(?!(?:\$|<|\{|your|xxx|placeholder|changeme|\.\.\.|例如|占位))"
        r"([A-Za-z0-9/+_=-]{16,})", re.I)),
]

#: 合规敏感词。命中不等于违规,只是「这段话涉及了不该随手往外发的领域」。
#: 故意写得少而具体:词表一长就全是噪音,人会开始无视提示,那时这道闸等于没有。
_COMPLIANCE = [
    "用户原始数据", "个人身份信息", "pii",
    "内部估值", "未公开财报", "裁员名单", "绩效结果",
]

#: 判断词。与人名同现才算命中——单独出现的「不行」「靠谱」是正常表达。
_JUDGE = [
    "不行", "不靠谱", "能力差", "水平低", "拉胯", "混日子", "该换掉", "该走人",
    "绩效差", "背锅", "甩锅", "不称职", "扶不上", "没救",
]


#: 滑窗长度(字符)。取 24:短了会误命中——二十来个汉字的句子在两处独立出现的概率很低,
#: 但八九个字的("这个方案我觉得可以")随处都是。长了则一句改写就漏。
SHINGLE = 24

#: 指纹条数上限。封存通常只有几百 KB,但它是用户自己的目录、大小不由我们控制,
#: 不设上限的话一份被误放进去的日志能把这里撑到几百万条。超了就截断并在日志里说,
#: 不静默降级——降级了的闸比没有闸更危险。
MAX_PRINTS = 400_000


def _shingles(text, n=SHINGLE):
    """把文本切成逐字符滑动的等长窗口。逐字符(步长 1)是为了两边对齐:
    出站那侧也这么切,任一窗口相同即命中,与前后缀、截断位置无关。"""
    s = " ".join(str(text or "").split())
    for i in range(0, max(0, len(s) - n + 1)):
        yield s[i:i + n]


def _sealed_prints(sealed_dir, log=None):
    """封存内容的指纹集合:滑窗 md5,不存原文。

    存指纹不存原文,是因为闸自己也不该把封存内容捧在手里传来传去;滑窗而非整行,
    是因为整行比对一个前缀就绕过了——而转述几乎必然带前缀(「他说过:」「文档里写的是」),
    那样这道闸等于不存在。改写过的内容仍抓不到,那不是这道闸的目标:
    第一道闸「不取」才是主力(见 context.py),这道只兜「原样搬运」。
    """
    out = {}
    if not sealed_dir or not os.path.isdir(sealed_dir):
        return out
    full = False
    for root, _dirs, files in os.walk(sealed_dir):
        for fn in files:
            if full or not fn.endswith((".md", ".txt", ".json", ".yaml", ".yml")):
                continue
            try:
                with open(os.path.join(root, fn), encoding="utf-8", errors="replace") as f:
                    body = f.read()
            except OSError:
                continue
            for w in _shingles(body):
                out[hashlib.md5(w.encode()).hexdigest()] = fn
                if len(out) >= MAX_PRINTS:
                    full = True
                    if log:
                        log("larkbot", f"封存指纹达上限 {MAX_PRINTS},{fn} 之后未纳入")
                    break
    return out


def check(text, sealed_dir="", names=(), strict=False, compliance=True):
    """→ {"ok":bool, "level":"pass|warn|block", "hits":[{kind,detail,at}], "text":原文}

    names:具名个人的名字表(工号、花名、姓名)。由调用方从 curator 的实体图谱取,
    这里不自己去查——闸要能脱离 curator 单测。
    strict:把 warn 级升为 block。
    """
    t = str(text or "")
    hits = []
    for kind, rx in _CRED:
        m = rx.search(t)
        if m:
            # detail 只留类别与位置,不回显匹配到的串——日志里躺着一份等于没拦
            hits.append({"kind": kind, "detail": "凭证形态", "at": m.start(), "level": "block"})

    if sealed_dir:
        prints = _sealed_prints(sealed_dir)
        if prints:
            for i, w in enumerate(_shingles(t)):
                h = hashlib.md5(w.encode()).hexdigest()
                if h in prints:
                    hits.append({"kind": "sealed", "detail": f"命中封存({prints[h]})",
                                 "at": i, "level": "block"})
                    break

    low = t.lower()
    if compliance:
        for w in _COMPLIANCE:
            i = low.find(w.lower())
            # 英文缩写(如 pii)要求词边界,否则长单词里的片段也会误命中
            if i >= 0 and (not w.isascii() or re.search(r"\b" + re.escape(w) + r"\b", low)):
                hits.append({"kind": "compliance", "detail": f"敏感领域「{w}」", "at": i, "level": "warn"})
                break

    for n in (names or ()):
        n = str(n or "").strip()
        if len(n) < 2 or n not in t:
            continue
        for j in _JUDGE:
            if j in t:
                hits.append({"kind": "person-judgment", "detail": f"「{n}」+ 评价",
                             "at": t.find(n), "level": "warn"})
                break
        else:
            continue
        break

    if not hits:
        return {"ok": True, "level": "pass", "hits": [], "text": t}
    lv = "block" if any(h["level"] == "block" for h in hits) else ("block" if strict else "warn")
    return {"ok": lv != "block", "level": lv, "hits": hits, "text": t}


def notice(res):
    """被拦时发给飞书的替代文本。不带原文,也不说命中了什么具体内容——
    那些只在 Desk 里给本人看。对面看到的只是「这里有东西,去 Desk 看」。"""
    kinds = sorted({h["kind"] for h in res.get("hits", [])})
    return "内容未发送:命中本机红线(" + "、".join(kinds) + ")。请在 Desk 里查看原文。"
