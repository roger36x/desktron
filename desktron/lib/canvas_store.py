"""Desk 画布应用(Excalidraw)的文件持久化:一张画布一个文件,存 .workbench/canvases/。

原先画布存在浏览器 IndexedDB 里,agent 读不到、清缓存即丢、换机器带不走;改落盘后
三处都解开,备忘等地方也能直接引用导出的 SVG。

每张画布两个文件:
- <id>.excalidraw:画布本体(JSON,与 Excalidraw 官方 .excalidraw 同形,另带 name/created/updated)
- <id>.svg:保存时前端顺带导出的静态图,供备忘、文档引用(/workbench/view?raw=1&path=... 取)

id 由前端生成,形如 c-<base36>-<rand>;这里只认 [\\w-]+,防路径穿越。
"""
import json
import os
import re
import time

_ID_RE = re.compile(r"^[\w-]{1,64}$")


def _dir(repo_root):
    return os.path.join(repo_root, ".workbench", "canvases")


def _path(repo_root, cid, ext=".excalidraw"):
    if not _ID_RE.match(cid or ""):
        return None
    return os.path.join(_dir(repo_root), cid + ext)


def svg_rel(cid):
    """备忘/文档里引用这张画布导出图的工作台相对路径。"""
    return ".workbench/canvases/%s.svg" % cid


def load(repo_root, cid):
    p = _path(repo_root, cid)
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        assert isinstance(d, dict)
    except Exception:
        return None
    d["id"] = cid
    d.setdefault("elements", [])
    d.setdefault("files", {})
    d.setdefault("appState", {})
    return d


def listing(repo_root):
    """画布清单(不含元素本体),按更新时间倒序。"""
    out = []
    d = _dir(repo_root)
    try:
        names = os.listdir(d)
    except Exception:
        return out
    for n in sorted(names):
        if not n.endswith(".excalidraw"):
            continue
        rec = load(repo_root, n[:-len(".excalidraw")])
        if not rec:
            continue
        out.append({"id": rec["id"], "name": rec.get("name") or "未命名画布",
                    "updated": rec.get("updated") or 0, "created": rec.get("created") or 0,
                    "count": len(rec.get("elements") or [])})
    out.sort(key=lambda x: x["updated"], reverse=True)
    return out


def save(repo_root, cid, rec, svg=None):
    """整张落盘。rec 缺字段按已存的补,svg 给了就同步导出图。返回错误串,'' 为成功。"""
    p = _path(repo_root, cid)
    if not p:
        return "画布 id 非法"
    if not isinstance(rec, dict) or not isinstance(rec.get("elements"), list):
        return "画布结构非法(需要 {elements:[...]})"
    old = load(repo_root, cid) or {}
    doc = {
        "type": "excalidraw", "version": 2, "source": "desktron",
        "id": cid,
        "name": rec.get("name") or old.get("name") or "未命名画布",
        "created": old.get("created") or rec.get("created") or int(time.time() * 1000),
        "updated": int(time.time() * 1000),
        "elements": rec["elements"],
        "appState": rec.get("appState") if isinstance(rec.get("appState"), dict) else {},
        "files": rec.get("files") if isinstance(rec.get("files"), dict) else {},
    }
    try:
        os.makedirs(_dir(repo_root), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(tmp, p)   # 原子替换:防写一半掉电留下半份 JSON
    except Exception as e:
        return "写入失败:%s" % str(e)[:100]
    if isinstance(svg, str) and svg.strip():
        sp = _path(repo_root, cid, ".svg")
        try:
            tmp = sp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(svg)
            os.replace(tmp, sp)
        except Exception:
            pass   # 导出图是副产物,写失败不连累画布本体
    return ""


def remove(repo_root, cid):
    for ext in (".excalidraw", ".svg"):
        p = _path(repo_root, cid, ext)
        if not p:
            return "画布 id 非法"
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except Exception as e:
            return "删除失败:%s" % str(e)[:100]
    return ""
