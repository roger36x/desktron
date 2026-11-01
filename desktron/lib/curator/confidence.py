"""confidence 三档,全部由 `source` 自动推导,没有人工入口。

- reported(默认):source 里没有可核引用。
- documented:恰好一个独立来源。出处在,但没有第二方印证。
- corroborated:两个及以上独立来源。同一件事在互不相同的地方各说了一遍。

2026-08-21 由 verified 改为 corroborated。旧的 verified 要用户逐条签名并绑正文 hash,
线上跑到 405 条记忆时签名数为 0——这道工序没人做,顶档等于不存在。改判据为「引用了几个
独立来源」之后,顶档随写入自动产生,不再依赖人的动作。

独立来源怎么算(按下列顺序抽取,抽到的文本即消费掉,不再参与后面的匹配):

1. URL —— 键取 host + 文档 token。同一篇飞书文档的两个锚点归一为一个来源。
2. UUID —— 会话档案一类。一份档案是一个来源,不因为 uuid 里有多段十六进制就算多个。
3. `文件.扩展名:行号` —— 键取文件路径,行号不计。
4. commit hash —— 7–40 位十六进制且必须含字母,借此排开纯数字的文档 id。
5. `《书名》` —— 专著。书比链接更稳定,算一个来源。

同一本书的多种写法会先归并再计数:书名号里的译名与原名(`《系统之美》/《Thinking in
Systems》`)算一本;指向售书页的链接(amazon / 豆瓣 / goodreads)是这本书的另一种写法,
只要 source 里已有书名号就不另计。否则一条书摘笔记会因为「书名 + 购书链接」自升顶档,
而它自始至终只有一个来源。

书名号之外的文献(英文论文标题、`et al.`、`(年份)`)靠 rules.yaml 的 source_patterns 兜底,
命中只算一个来源:那条通道认的是「这里有一处引用」,不承担数几处。

它量的是引用面,不是真正的独立性:同一场会议里拿到的两个链接会被算成两个来源。要更严
的判据得看内容是否互相印证,那需要语义比对,不在这一层做。
"""

from __future__ import annotations

import re

_URL = re.compile(r'https?://[^\s,;、）)"\'\]]+')
_UUID = re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.I)
_FILELINE = re.compile(r'\b([\w./-]+\.[A-Za-z]{1,8}):\d+')
# 前瞻要求整段十六进制里至少有一个字母,把 ISBN、豆瓣 id 这类纯数字挡在外面。
_COMMIT = re.compile(r'\b(?=[0-9a-f]{7,40}\b)(?=[0-9]*[a-f])[0-9a-f]{7,40}\b')
_DOCTOK = re.compile(r'/(?:docx|wiki|sheets|base|slides|docs|articles)/([A-Za-z0-9]{6,})')
_BOOK = re.compile(r'《([^》]{2,80})》')
# 译名/原名并列:《A》/《B》、《A》（《B》)。相邻且只隔分隔符的,是同一本书的两种写法。
_BOOK_PAIR = re.compile(r'《[^》]{2,80}》\s*[/／(（]\s*《[^》]{2,80}》')
_BOOKSHOP = re.compile(r'^(?:www\.)?(?:amazon\.|book\.douban\.|douban\.|goodreads\.)', re.I)


def source_keys(src, source_patterns=None) -> set:
    """把 source 串拆成一组去重后的独立来源键。空 source 或无可核引用时返回空集。"""
    if not isinstance(src, str):
        return set()
    src = src.strip()
    if not src:
        return set()

    keys, rest = set(), src
    for m in _URL.finditer(rest):
        u = m.group(0)
        host = re.sub(r'^https?://', '', u).split('/')[0]
        toks = _DOCTOK.findall(u)
        if toks:
            keys.add(f"url:{host}/{toks[-1]}")
        else:
            path = re.sub(r'^https?://[^/]+', '', u).rstrip('/')
            keys.add(f"url:{host}{path}")
    rest = _URL.sub(' ', rest)

    for m in _UUID.finditer(rest):
        keys.add("uuid:" + m.group(0).lower())
    rest = _UUID.sub(' ', rest)

    for m in _FILELINE.finditer(rest):
        keys.add("file:" + m.group(1))
    rest = _FILELINE.sub(' ', rest)

    for m in _COMMIT.finditer(rest):
        keys.add("commit:" + m.group(0)[:7])

    pair_spans = [m.span() for m in _BOOK_PAIR.finditer(rest)]
    for m in _BOOK.finditer(rest):
        # 并列译名只留第一个,后一个落在同一 pair 区间内,跳过。
        if any(a < m.start() < b and m.start() > a + 1 for a, b in pair_spans):
            continue
        keys.add("book:" + m.group(1).strip())

    if any(k.startswith("book:") for k in keys):
        keys = {k for k in keys
                if not (k.startswith("url:") and _BOOKSHOP.match(k[4:]))}

    if keys:
        return keys
    # 内建抽取没认出来,再看实例自定的 source_patterns(rules.yaml)。命中只算一个来源:
    # 这条通道是给 deepwiki 缓存那类特殊形态留的兜底,不承担计数。
    for p in source_patterns or []:
        try:
            if re.search(p, src):
                return {"pattern:" + p}
        except re.error:
            continue
    return set()


def derive(fm: dict, source_patterns=None) -> str:
    """按独立来源数推导 confidence。0 → reported,1 → documented,≥2 → corroborated。"""
    n = len(source_keys(fm.get("source"), source_patterns))
    if n == 0:
        return "reported"
    return "documented" if n == 1 else "corroborated"
