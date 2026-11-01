"""工作流制品:文档、页面、目录型证据包、预览链接这类做出来给人看的东西,不含代码。

一处集中:$WORKSPACE/domains/<工作流>/artifacts/。不落在工作流引用的真实代码仓里——
指定了 dir 的工作流(如 Desk 迭代)cwd 就是那个仓,制品写进去会混进它的版本历史。

清单不靠登记维持:目录里有什么就是什么,每个直接子项算一件。目录型制品常是实验包、
验证包或静态页面目录,里面的散件不单独成行。要 agent 记得调一次登记命令才出现的清单,漏一次就永远
缺一件;而写文件这一步本来就要做,拿它当登记动作,产出即登记才是真的。

index.jsonl 只补目录说不了的那部分:外部链接(预览页、飞书文档这类根本不是本地文件的
产出)、标题与说明、隐藏。它只追加,修正写新条目、后写的覆盖先写的,与仓里其它记录
同一套规矩(见 AGENTS.md「修正不改历史」)。
"""
import json
import os
import re
import time

from deskcfg import REPO_ROOT

INDEX_NAME = "index.jsonl"
KINDS = ("doc", "demo", "dir", "page", "image", "data", "link", "other")

# 后缀到类别:只用来给列表分组和挑图标,认不出就是 other,不影响它出现在清单里。
_EXT_KIND = {
    ".md": "doc", ".markdown": "doc", ".txt": "doc", ".pdf": "doc", ".docx": "doc",
    ".html": "page", ".htm": "page",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".svg": "image", ".excalidraw": "image",
    ".csv": "data", ".json": "data", ".jsonl": "data", ".tsv": "data", ".xlsx": "data",
}


def artifacts_dir(domain, create=False):
    """制品目录的绝对路径;domain 非法时返回空串。

    复合键 workspace:sub 只用于终端归属,制品与 cwd 一样落在冒号前的工作流上。
    """
    ws = str(domain or "").split(":")[0] or "_desktop"
    if not re.fullmatch(r"[\w-]+", ws):
        return ""
    path = os.path.join(REPO_ROOT, "domains", ws, "artifacts")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def index_path(domain):
    d = artifacts_dir(domain)
    return os.path.join(d, INDEX_NAME) if d else ""


def _dir_entry(full):
    """目录型静态 demo 的入口。只认约定入口，不猜任意一个 HTML。"""
    for name in ("index.html", "index.htm"):
        if os.path.isfile(os.path.join(full, name)):
            return name
    return ""


def _kind_of(name, isdir, entry=""):
    if isdir:
        return "demo" if entry else "dir"
    return _EXT_KIND.get(os.path.splitext(name)[1].lower(), "other")


def _title_of(name, isdir):
    return name if isdir else (os.path.splitext(name)[0] or name)


def read_ops(domain):
    """index.jsonl 的原始条目,按写入顺序。坏行跳过——一行写坏不该让整份清单消失。"""
    path = index_path(domain)
    out = []
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        return []
    return out


def _append(domain, row):
    path = index_path(domain)
    if not path:
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    row = dict(row)
    row.setdefault("at", int(time.time() * 1000))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _fold(ops):
    """把追加流折成当前态:links(外部链接) / meta(对某一件的说明) / hidden(收起的)。"""
    links, meta, hidden = {}, {}, set()
    for row in ops:
        op = str(row.get("op") or "link")
        ref = str(row.get("ref") or row.get("url") or "")
        if not ref:
            continue
        if op == "hide":
            hidden.add(ref)
        elif op == "show":
            hidden.discard(ref)
        elif op == "link":
            links[ref] = {**links.get(ref, {}), **row, "ref": ref}
        elif op == "meta":
            meta[ref] = {**meta.get(ref, {}),
                         **{k: v for k, v in row.items() if v not in ("", None)}}
    return links, meta, hidden


def _scan(domain):
    """目录里的直接子项。索引文件本身与点开头的隐藏项不算制品。"""
    root = artifacts_dir(domain)
    out = []
    if not root or not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        if name.startswith(".") or name == INDEX_NAME:
            continue
        full = os.path.join(root, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        isdir = os.path.isdir(full)
        entry = _dir_entry(full) if isdir else ""
        rel = os.path.join("artifacts", name)
        out.append({"ref": name, "kind": _kind_of(name, isdir, entry), "title": _title_of(name, isdir),
                    "note": "", "at": int(st.st_mtime * 1000),
                    "size": 0 if isdir else int(st.st_size), "external": False,
                    "path": rel, "entry": os.path.join(rel, entry) if entry else ""})
    return out


def list_items(domain):
    """一个工作流的制品清单,新的在前。文件来自目录,链接来自索引,两者同列一张表——
    「这个工作流做出过什么」不该分两个地方拼。"""
    links, meta, hidden = _fold(read_ops(domain))
    items = _scan(domain)
    for ref, row in links.items():
        items.append({"ref": ref, "kind": str(row.get("kind") or "link"),
                      "title": str(row.get("title") or ref), "note": str(row.get("note") or ""),
                      "at": int(row.get("at") or 0), "size": 0, "external": True,
                      "path": "", "entry": "", "session": str(row.get("session") or "")})
    for item in items:
        over = meta.get(item["ref"])
        if over:
            for key in ("title", "note", "kind"):
                if over.get(key):
                    item[key] = over[key]
    items = [x for x in items if x["ref"] not in hidden]
    items.sort(key=lambda x: -(x.get("at") or 0))
    return items


def add_link(domain, url, title="", kind="link", note="", session="", at=0):
    """登记一件不是本地文件的产出:预览链接、飞书文档、妙搭应用。

    at 是这件产出的时间(毫秒),默认写入时刻。补登记历史产出时要传它的真实时间——
    清单按时间倒序,否则一批几个月前的旧链接会齐刷刷压在今天写的文档上面。
    """
    url = str(url or "").strip()
    if not url or not re.match(r"https?://", url):
        return None
    row = {"op": "link", "ref": url, "url": url,
           "title": str(title or url)[:200], "kind": kind if kind in KINDS else "link",
           "note": str(note or "")[:500], "session": str(session or "")}
    if at:
        row["at"] = int(at)
    return _append(domain, row)


def annotate(domain, ref, title="", note="", kind=""):
    """给某一件补标题或说明。文件名常常说不清它是什么,但改名会断掉别处的引用。"""
    ref = str(ref or "").strip()
    if not ref:
        return None
    row = {"op": "meta", "ref": ref}
    if title:
        row["title"] = str(title)[:200]
    if note:
        row["note"] = str(note)[:500]
    if kind in KINDS:
        row["kind"] = kind
    if len(row) == 2:
        return None
    return _append(domain, row)


def hide(domain, ref, on=True):
    """从清单里收起,不删文件。删文件是另一回事,由人自己决定。"""
    ref = str(ref or "").strip()
    if not ref:
        return None
    return _append(domain, {"op": "hide" if on else "show", "ref": ref})
