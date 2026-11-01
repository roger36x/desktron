"""基座路由:开发令牌 / 外链打开 / HTTP 代理 / UI 状态 / 文件浏览与查看器 / 页面渲染。
拆分自 ui_server._workbench;owns() 按路径认领,
handle(h) 内 h 为 Handler 实例,行为与原 if 链一致。"""
import os, re, json, subprocess, urllib.parse, hmac, stat
from deskcfg import _DEV, DATA_HOME, _dev_token
import http_proxy
from workbench import _wb_state_patch, _wb_state_read, _wb_state_write
from assets import (_fs_file, _fs_git_file, _fs_git_root, _fs_ls, _fs_search, _fs_write_file, _wb_html, _wb_file,
                    _wb_view, _wb_view_write, _VIEW_PAGE, _fs_grep)



def _chrome_bin():
    """解析本机 Chrome 可执行路径(用于 Desk 专属 chrome-profile 开页);找不到返回 None。"""
    import oscompat
    for c in oscompat.chrome_candidates():
        if os.path.exists(c):
            return c
    return None


def owns(p, method):
    return p in ("/dev/token", "/workbench/open-external", "/workbench/http-proxy",
                 "/workbench/state", "/workbench/fs", "/workbench/view",
                 "/workbench/page", "/workbench/page_meta", "/workbench/file",
                 "/workbench/backends")


def handle(h, p, q, method):
    import hmac, stat
    if p == "/workbench/backends" and method == "GET":
        # 开源版只有本机这一份后端；保留接口形状，前端据 role 选默认视图。
        import remote_auth as _ra
        return h._send_json({"items": [], "role": _ra.role()})
    if p == "/dev/token" and method == "GET":
        # 开发态前端自动重载用:返回前端文件 mtime;非开发态 dev=False,前端不轮询
        return h._send_json({"dev": True, "token": _dev_token()} if _DEV else {"dev": False})
    if p == "/workbench/open-external" and method == "POST":
        # 在浏览器打开外链——禁止内嵌(X-Frame-Options/CSP frame-ancestors)的站点
        # (google、github 等)在 iframe 抽屉里只会空白,唯一出路是转浏览器。
        # profile: "system"(默认,系统默认浏览器) | "desk"(Desk 专属 chrome-profile,
        # 带内网 SSO 登录态与扩展,适合打开需登录的内网页)。
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
            url = body.get("url", "")
            profile = body.get("profile", "system")
            assert isinstance(url, str) and re.match(r"^https?://", url)
        except Exception:
            return h._send_json({"error": "需要 http(s) url"}, 400)
        try:
            if profile == "desk":
                chrome = _chrome_bin()
                if not chrome:
                    return h._send_json({"error": "未找到 Chrome 可执行文件,无法用 Desk profile 打开"}, 500)
                desk_profile = os.path.join(os.path.dirname(DATA_HOME), "chrome-profile")
                subprocess.Popen([chrome, "--user-data-dir=" + desk_profile, url])
            else:
                import oscompat
                oscompat.open_url(url)   # 系统默认浏览器
        except Exception as e:
            return h._send_json({"error": str(e)}, 500)
        return h._send_json({"ok": True, "profile": profile})
    if p == "/workbench/http-proxy" and method == "POST":
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
            assert isinstance(body, dict)
        except Exception:
            return h._send_json({"ok": False, "error": "invalid json"}, 400)
        status, res = http_proxy.handle_proxy_request(body)
        return h._send_json(res, status)
    if p == "/workbench/state/patch" and method == "POST":
        length = int(h.headers.get("Content-Length", 0))
        try:
            data = json.loads(h.rfile.read(length) or b"{}")
            assert isinstance(data, dict)
        except Exception:
            return h._send_json({"error": "invalid json"}, 400)
        _wb_state_patch(data)
        return h._send_json({"ok": True})
    if p == "/workbench/state":
        if method == "GET":
            return h._send_json(_wb_state_read())
        length = int(h.headers.get("Content-Length", 0))
        try:
            data = json.loads(h.rfile.read(length) or b"{}")
            assert isinstance(data, dict)
        except Exception:
            return h._send_json({"error": "invalid json"}, 400)
        # 兼容短暂发布过的前端：它把局部更新 POST 到旧路径；传统完整保存始终是 PUT。
        if method == "POST":
            _wb_state_patch(data)
        else:
            _wb_state_write(data)
        return h._send_json({"ok": True})
    if p == "/workbench/fs" and method == "GET":
        # coding 项目文件浏览(右抽屉用):懒列一层(无 path 列根)或读单个文件
        params = urllib.parse.parse_qs(q)
        root_id = (params.get("root") or [""])[0]
        rel = (params.get("path") or [""])[0]
        if (params.get("gitroot") or [""])[0] == "1":
            # 这份文件所属的仓根,连同它相对仓根的路径。从会话点开文件时用它当 IDE 的根,
            # 而不是文件所在的那一层。
            return h._send_json(_fs_git_root(root_id, rel))
        if (params.get("git") or [""])[0]:
            # 这份文件在某个 git 版本里的样子。差异视图比的就是它与当前缓冲区——
            # 「相对上次提交我改了什么」,而不是「缓冲区与磁盘差在哪」(后者没改就是空的)。
            rev = (params.get("git") or ["HEAD"])[0] or "HEAD"
            txt, note = _fs_git_file(root_id, rel, rev)
            if txt is None:
                return h._send_json({"error": "not found"}, 404)
            return h._send_json({"text": txt, "note": note, "rev": rev})
        if (params.get("file") or [""])[0] == "1":
            txt = _fs_file(root_id, rel)
            if txt is None:
                return h._send_json({"error": "not found"}, 404)
            return h._send_text(txt)
        grep = (params.get("grep") or [""])[0]
        if grep:   # 按内容递归搜:文件名搜不到的东西(某个函数、某句报错)只能翻正文
            r = _fs_grep(root_id, grep)
            if r is None:
                return h._send_json({"error": "not a project dir"}, 404)
            return h._send_json(r)
        kw = (params.get("q") or [""])[0]
        if kw:   # 按文件名递归搜(懒加载树只过滤已展开行等于搜不到,必须走磁盘)
            r = _fs_search(root_id, kw)
            if r is None:
                return h._send_json({"error": "not a project dir"}, 404)
            return h._send_json(r)
        items = _fs_ls(root_id, rel)
        if items is None:
            return h._send_json({"error": "not a project dir"}, 404)
        return h._send_json({"items": items, "path": rel})
    if p == "/workbench/fs" and method == "POST":
        # 文件浏览器就地编辑保存(取代原先"退出去开 nvim"那条写入口)
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
            root_id = body.get("root") or ""
            rel = body.get("path") or ""
            text = body["text"]
            assert isinstance(text, str)
        except Exception:
            return h._send_json({"error": "需要 {root, path, text}"}, 400)
        if not _fs_write_file(root_id, rel, text):
            return h._send_json({"error": "写入失败"}, 404)
        return h._send_json({"ok": True})
    if p == "/workbench/view" and method == "POST":
        # 查看器里就地编辑保存(md/纯文本);路径解析与 GET 同源,不额外放开可写范围
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
            rel = body.get("path") or ""
            text = body["text"]
            assert isinstance(text, str)
        except Exception:
            return h._send_json({"error": "需要 {path, text}"}, 400)
        if not _wb_view_write(rel, text, body.get("base") or None):
            return h._send_json({"error": "写入失败(路径越界、不存在或不是可编辑的文本文件)"}, 404)
        return h._send_json({"ok": True})
    if p in ("/workbench/page", "/workbench/page_meta"):
        params = urllib.parse.parse_qs(q)
        html, source = _wb_html((params.get("id") or [""])[0])
        if html is None:
            return h._send_json({"error": "bad id"}, 400)
        if p == "/workbench/page_meta":
            return h._send_json({"source": source})
        data = html.encode("utf-8")
        h.send_response(200)
        h.send_header("Content-Type", "text/html; charset=utf-8")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
        return
    if p == "/workbench/file":
        params = urllib.parse.parse_qs(q)
        content = _wb_file((params.get("path") or [""])[0])
        if content is None:
            return h._send_json({"error": "not found"}, 404)
        data = content.encode("utf-8")
        h.send_response(200)
        h.send_header("Content-Type", "text/plain; charset=utf-8")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
        return
    if p == "/workbench/view":
        # 悬浮框内嵌:工作区内文件/目录渲染成可浏览页(终端里点路径走这里)
        params = urllib.parse.parse_qs(q)
        rel = (params.get("path") or [""])[0]
        raw = (params.get("raw") or [""])[0] in ("1", "true")
        base = (params.get("base") or [""])[0] or None
        root_id = (params.get("root") or [""])[0] or None
        download = (params.get("download") or [""])[0] in ("1", "true")
        res = (_wb_view(rel, raw, base, root_id, download=True) if download
               else _wb_view(rel, raw, base, root_id))
        if not res:
            body = _VIEW_PAGE.format(body='<div class=crumb>无法在工作区内查看此路径(可能在工作区外或不存在)。</div>')
            data = body.encode("utf-8")
            h.send_response(404)
            h.send_header("Content-Type", "text/html; charset=utf-8")
            h.send_header("Content-Length", str(len(data)))
            h.end_headers(); h.wfile.write(data)
            return
        ctype, data = res
        h.send_response(200)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(data)))
        if download:
            name = os.path.basename(urllib.parse.unquote(rel)) or "download"
            h.send_header("Content-Disposition", "attachment; filename*=UTF-8''"
                          + urllib.parse.quote(name, safe=""))
        h.end_headers(); h.wfile.write(data)
        return
    return h._send_json({"error": "unknown"}, 404)
