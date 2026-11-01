"""记忆领域路由:记忆区树/读/就地编辑、项目关键记忆、备忘录存取(蒸馏喂给记忆引擎)。
拆分自 ui_server._workbench;owns() 按路径认领,
handle(h) 内 h 为 Handler 实例,行为与原 if 链一致。"""
import os, json, urllib.parse
from deskcfg import REPO_ROOT
from memory_store import _memory_tree, _memory_read, _memory_edit, _project_memory


def owns(p, method):
    return p in ("/workbench/memory", "/workbench/memory/edit",
                 "/workbench/project-memory", "/workbench/notepad")


def handle(h, p, q, method):
    if p == "/workbench/memory":
        params = urllib.parse.parse_qs(q)
        name = (params.get("name") or [""])[0]
        if name:
            txt = _memory_read(name, count=True)   # 界面点开单条正文 = 一次访问(口径同 curator show)
            if txt is None:
                return h._send_json({"error": "not found"}, 404)
            data = txt.encode("utf-8")
            h.send_response(200)
            h.send_header("Content-Type", "text/plain; charset=utf-8")
            h.send_header("Content-Length", str(len(data)))
            h.end_headers(); h.wfile.write(data)
            return
        return h._send_json({"tree": _memory_tree()})
    if p == "/workbench/memory/edit" and method == "POST":
        # 记忆区就地编辑(#75 拍板:不做待审,用户编辑 = verified)。body 整体替换,frontmatter 由服务端治理
        ln = int(h.headers.get("Content-Length", 0))
        try: body = json.loads(h.rfile.read(ln) or b"{}")
        except Exception: body = {}
        res = _memory_edit(str(body.get("path") or ""), body.get("body"), by=os.environ.get("USER") or "user")
        return h._send_json(res, 200 if res.get("ok") else 422)
    if p == "/workbench/project-memory":
        # 项目(容器)关键记忆:解析 frontmatter 的可读列表(summary/来源/汇总),供总览与 panel 展示
        params = urllib.parse.parse_qs(q)
        dom = (params.get("domain") or [""])[0]
        return h._send_json({"items": _project_memory(dom)})
    if p == "/workbench/notepad" and method in ("GET", "POST"):
        # 备忘录应用持久化:GET 整份取,POST 整份存(domains/<域>/notepad.json)。
        # 原 localStorage 迁移由前端首载完成;agent 在会话 cwd 直接读写同一份文件。
        import notepad_store
        if method == "GET":
            return h._send_json(notepad_store.load(REPO_ROOT))
        length = int(h.headers.get("Content-Length", 0))
        try:
            jb = json.loads(h.rfile.read(length) or b"{}")
            assert isinstance(jb, dict)
        except Exception:
            return h._send_json({"error": "invalid json"}, 400)
        err = notepad_store.save(REPO_ROOT, jb)
        if err:
            return h._send_json({"error": err}, 422)
        return h._send_json({"ok": True})
    return h._send_json({"error": "unknown"}, 404)
