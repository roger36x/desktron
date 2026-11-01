# 由 ui_server.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)
from deskcfg import FRONTEND, UI_PORT, DATA_HOME
from frontend_bundle import bundle_data
import datetime, email.utils, threading, hashlib
import urllib.parse
import sys, os, signal, atexit, time, socket, subprocess, json, re, queue, struct, gzip, io, stat


_FRONTEND_REV = {"signature": None, "token": ""}


def _frontend_tree_signature(root):
    """整棵可交付前端的轻量签名；任一文件变化都必须改变，不取“最大值”。"""
    rows = []
    try:
        for base, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules")]
            for name in names:
                if not name.endswith((".js", ".css", ".html")):
                    continue
                path = os.path.join(base, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                rows.append((rel, st.st_mtime_ns, st.st_size))
    except OSError:
        pass
    return tuple(sorted(rows))


def _frontend_token():
    """前端版本令牌：对全部可交付 js/css/html 的文件签名做短哈希。

    旧实现取最大的秒级 mtime。只要另一个未改文件时间更新，较早文件（例如恢复链所在的
    app-chat-core.js）随后变化也不会推动最大值；服务重启后的 SSE 握手便误判“前端没变”，
    手机继续运行旧代码。这里改成整棵树签名，页面、嵌套 APP 与 vendor 都覆盖；签名未变时
    直接复用，握手不反复读文件正文。"""
    signature = _frontend_tree_signature(FRONTEND)
    if signature and signature == _FRONTEND_REV["signature"]:
        return _FRONTEND_REV["token"]
    if not signature:
        return str(int(time.time()))
    token = hashlib.blake2b(repr(signature).encode("utf-8"), digest_size=8).hexdigest()
    _FRONTEND_REV.update(signature=signature, token=token)
    return token


# 每个交付资源自己的指纹,按内容算(短哈希),用 (mtime_ns, size) 做缓存键避免重复读。
# 普通文件各算各的；拆分源码由 frontend_bundle 按逻辑组交付,组内任一片变化才换该组 URL。
#
# 原来 19 个静态文件共用一个「全前端最新 mtime」:动一个文件,其余 18 个的缓存键一起变,
# 客户端把 1.7MB 全量重下。手机经隧道时每个请求固定一秒多(与大小无关),这就是二十几秒
# (2026-08-24 实测冷启 20 个请求、顺序取 31.7s;而那天我同步了六七次,每次都让缓存全废)。
#
# 按内容而不是 mtime:rsync 重写一份内容相同的文件、或者改完又改回来,指纹都不变。
# 合包把请求数守回拆分前预算；代价是组内改一片会重下该组,符合「往返比字节贵」的实测。
_ASSET_FP = {}


def _asset_token(relpath):
    """静态文件的缓存指纹。取不到就回落到全局那个令牌,不至于让页面引用不到东西。"""
    bundled = bundle_data(FRONTEND, relpath)
    if bundled:
        key, body, _mtime = bundled
        cache_key = "bundle:" + "/" + relpath.lstrip("/")
        hit = _ASSET_FP.get(cache_key)
        if hit and hit[0] == key:
            return hit[1]
        # 切换缓存命名空间：旧入口曾将错误内容按对端指纹缓存一年。
        fp = hashlib.blake2b(b"desk-assets-v2\0" + body, digest_size=6).hexdigest()
        _ASSET_FP[cache_key] = (key, fp)
        return fp
    full = os.path.join(FRONTEND, relpath.lstrip("/"))
    try:
        st = os.stat(full)
        key = (st.st_mtime_ns, st.st_size)
        hit = _ASSET_FP.get(full)
        if hit and hit[0] == key:
            return hit[1]
        with open(full, "rb") as f:
            fp = hashlib.blake2b(b"desk-assets-v2\0" + f.read(), digest_size=6).hexdigest()
        _ASSET_FP[full] = (key, fp)
        return fp
    except OSError:
        return _frontend_token()


BUILD_TOKEN = _frontend_token()


# 这些前缀转发给引擎;其余按静态文件从 frontend/ 取
# 打不上 ?v= 指纹的静态资源按扩展名认(见 end_headers 里那一支)。
_ASSET_CACHEABLE = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|ico|woff2?|ttf|otf)$", re.I)


class UiStaticMixin:

    def _asset_version_matches(self):
        path, _, query = self.path.partition("?")
        versions = urllib.parse.parse_qs(query, keep_blank_values=True).get("v", [])
        return len(versions) == 1 and versions[0] == _asset_token(path)

    # ---- 静态资源按需 gzip ----
    # 手机首屏要拉十来个资源、约 2 MB 原始字节,而 SimpleHTTPRequestHandler 不压缩。
    # 这些全是 JS/CSS,gzip 通常能压掉七成，经隧道时收益尤其明显。
    # 压好的结果按 mtime 缓存,改了文件才重压;太大的不压(内存里留一份不划算,
    # 而且那种多半是本来就该走缓存的一次性资源)。
    _GZ_EXT = (".js", ".css", ".html", ".json", ".svg", ".map", ".txt")
    _GZ_MAX = 8 << 20
    _gz_cache = {}

    def send_head(self):
        p = self.path.split("?", 1)[0]
        bundled = bundle_data(FRONTEND, p)
        if bundled:
            signature, body, mtime = bundled
            enc = None
            if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                cache_key = "bundle:" + p
                hit = self._gz_cache.get(cache_key)
                if not hit or hit[0] != signature:
                    hit = (signature, gzip.compress(body, 6))
                    self._gz_cache[cache_key] = hit
                body, enc = hit[1], "gzip"
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            if enc:
                self.send_header("Content-Encoding", enc)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Last-Modified", self.date_time_string(mtime))
            self.end_headers()
            return io.BytesIO(body)
        if (not p.endswith(self._GZ_EXT)
                or "gzip" not in (self.headers.get("Accept-Encoding") or "")):
            return super().send_head()
        fp = self.translate_path(self.path)
        try:
            st = os.stat(fp)
        except OSError:
            return super().send_head()
        if not stat.S_ISREG(st.st_mode) or st.st_size > self._GZ_MAX:
            return super().send_head()
        # 条件请求:这条 gzip 专用支线原来不认 If-Modified-Since——stdlib 那份判断长在
        # super().send_head() 里,而这里从不落到那一支。/vendor/ 改成 no-cache 之后,
        # 客户端「问一下变没变」还是照样拿到整份 200,等于白改(2026-08-24 实见)。
        ims = self.headers.get("If-Modified-Since")
        if ims and "If-None-Match" not in self.headers:
            try:
                ims_dt = email.utils.parsedate_to_datetime(ims)
                if ims_dt.tzinfo is None:
                    ims_dt = ims_dt.replace(tzinfo=datetime.timezone.utc)
                mtime_dt = datetime.datetime.fromtimestamp(int(st.st_mtime), datetime.timezone.utc)
                if mtime_dt <= ims_dt:
                    self.send_response(304)
                    self.end_headers()
                    return None
            except (TypeError, IndexError, OverflowError, ValueError):
                pass
        hit = self._gz_cache.get(fp)
        if not hit or hit[0] != st.st_mtime_ns:
            try:
                with open(fp, "rb") as f:
                    blob = gzip.compress(f.read(), 6)
            except OSError:
                return super().send_head()
            if len(self._gz_cache) > 256:      # 上限,别让它无限长
                self._gz_cache.clear()
            self._gz_cache[fp] = hit = (st.st_mtime_ns, blob)
        blob = hit[1]
        self.send_response(200)
        self.send_header("Content-Type", self.guess_type(fp))
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Last-Modified", self.date_time_string(st.st_mtime))
        self.end_headers()
        return io.BytesIO(blob)

    def handle_one_request(self):
        """客户端提前挂断不是服务端错误,静默收场。

        页面重载、请求超时被前端取消、SSE 断连,都会让写响应时抛 BrokenPipe/ConnectionReset。
        默认行为是把整条 traceback 交给 socketserver.handle_error 打出来——一次页面重载能灌
        一百多行进日志环(2026-08-10 实见:五个并发 /config 同时中招),真正要看的东西
        (比如回合异常收尾的那句原话)就被冲掉了。日志的价值在于信噪比。"""
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def send_response(self, code, message=None):
        self._status_code = int(code)      # end_headers 里要按它决定能不能进缓存分档
        return super().send_response(code, message)

    def send_response_only(self, code, message=None):
        self._status_code = int(code)
        return super().send_response_only(code, message)

    def end_headers(self):
        # 前端静态资源的缓存分两种。带 ?v=<令牌> 的(index.html 注入过指纹的那些 js/css)
        # 可以长缓存:文件一改令牌就变,URL 跟着变,不存在拿到旧文件的可能。
        # 其余一律禁缓存——WKWebView 的 NSURLCache 跨重启持久,只发 Last-Modified
        # 会让它按启发式缓存旧文件,改动不生效。
        # 这一条对切引擎尤其值钱:那是一次整页重载,而前端有一兆 js/css,
        # 每次都重下重解析。带指纹之后重载直接命中缓存。
        # 只有真发出了内容才谈得上长缓存。401/403/404/5xx 一律不进缓存分档——
        # 带指纹的 URL 上一版连 401 都盖着 immutable 一年:手机经隧道进来,Cookie 一过期
        # 就撞一次 401,那个失败被永久缓存住,该资源在指纹变化之前再也取不回来,
        # 表现是「页面某块莫名坏掉、刷新也不好」(2026-09-02 排查手机端切引擎时发现)。
        status_ok = getattr(self, "_status_code", 200) < 300
        if not self._is_workbench() and not self._is_contract() and not status_ok:
            self.send_header("Cache-Control", "no-store")
        elif not self._is_workbench() and not self._is_contract():
            if "v=" in (self.path.split("?", 1)[1] if "?" in self.path else ""):
                # v 是调用方给的，不代表当前文件就是那一版。过期/错配请求不可长缓存。
                self.send_header("Cache-Control", "public, max-age=31536000, immutable"
                                 if self._asset_version_matches() else "no-store")
            elif _ASSET_CACHEABLE.search(self.path.split("?", 1)[0]):
                # 打不上 ?v= 的那类静态资源:图标散落在应用清单与 CSS 里、字体由 @font-face 引,
                # 都不经 index.html 注入指纹,于是落到下面那一支被判 no-store,每次刷新整份重下
                # ——2026-08-26 实测两个应用图标 31KB、各花 0.5~0.8 秒,而且挡在 load 事件前面。
                # 它们几乎不变,给 no-cache 就够:允许存,但每次问一下,没改过是一个 304 空身。
                # 与 /vendor/ 那一支同一个道理,只是它们按扩展名认。
                self.send_header("Cache-Control", "no-cache")
            elif self.path.split("?", 1)[0].startswith("/vendor/"):
                # 动态加载的 vendor 资源不一定能在 index.html 注入 ?v=。给 no-cache，
                # 配合条件请求，未改动时返回 304，不重复传输整份资源。
                self.send_header("Cache-Control", "no-cache")
            else:
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
        # 首次凭 ?token= 放行的响应补种 Cookie:之后浏览器自动带,地址栏与访问日志里不再有令牌。
        # 这一段原本是类里第二个 end_headers,而 Python 后定义的会覆盖前面的——上面那段缓存
        # 分档因此是死代码,实测两种 URL 都不带任何 Cache-Control(2026-08-10)。
        # 那比改之前更糟:连原来的 no-store 也没了,WKWebView 会按启发式自行缓存旧文件。
        # 一个类里只留一个 end_headers。
        if getattr(self, "_set_remote_cookie", False):
            self._set_remote_cookie = False
            import remote_auth
            self.send_header("Set-Cookie", remote_auth.cookie_header(
                secure=bool(getattr(self.server, "serve_proxy", False))))
        # HTTP/1.1 的兜底:没声明长度、又不是分块或协议升级的响应,客户端无从判断何时结束。
        # 这类响应(SSE 流、少数手写的)就把连接关掉,用 EOF 作结束标志。
        # 304/204 例外:这两种status line 本身就定义了「没有正文」,空行一到就是结尾,
        # 不存在「不知道何时算完」——按老规则把它们也关掉连接的话,vendor 的条件请求
        # (send_head 里那个 304)每问一次就得重连一次,反而抵消了 no-cache 换来的省流量。
        buf = b"".join(getattr(self, "_headers_buffer", None) or [])
        status_line = buf.split(b"\r\n", 1)[0]
        no_body_status = b" 304 " in status_line or b" 204 " in status_line
        buf_l = buf.lower()
        if (not no_body_status and b"content-length:" not in buf_l
                and b"transfer-encoding:" not in buf_l and b"upgrade:" not in buf_l):
            self.close_connection = True
            self.send_header("Connection", "close")
        super().end_headers()

    def _sse_prime(self):
        """防 WKWebView/Safari 对流式响应整段缓冲:关 Nagle + 先灌 2KB 注释 padding,
        促浏览器进入增量派发模式(否则首批小事件被攒在网络层,表现为'半天不出第一个字')。"""
        try:
            import socket as _s
            self.connection.setsockopt(_s.IPPROTO_TCP, _s.TCP_NODELAY, 1)
        except Exception:
            pass
        try:
            self.wfile.write(b":" + b" " * 2048 + b"\n\n")
            self.wfile.flush()
        except Exception:
            pass

    # 超过这个大小才压。小响应压了不划算:省下的字节还不够一次系统调用,
    # 而 CPU 是实打实花的。1KB 这一档往下,压缩比再高也就省几百字节。
    JSON_GZIP_MIN = 1024

    def _send_json(self, obj, code=200):
        """JSON 响应也压缩。

        原来只有静态文件与首页压,API 一律原样发——而最大的那一份恰恰是 API:
        一段会话的消息 741KB,gzip 之后 205KB(2026-08-25 实测,省 72%)。
        经隧道时这一份要四秒多,是「刷新到会话可用」里最大的一块。

        JSON 压缩比很高(重复的键名、大量空白与 ASCII),而这里的响应又多是结构化数据,
        所以收益比静态文件那一侧还大。
        """
        self._send_json_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), code)

    def _send_json_bytes(self, data, code=200):
        """已经是 JSON 字节的那一路也走这里。

        会话消息那条自己拼字节直发(见 messages_window_json 那一段的说明),而它正是最大的
        一份。两处各写一套发送,压缩这类事迟早只加在其中一边——所以压缩落在这个公用函数上,
        两条路都经过它。
        """
        enc = None
        if (len(data) >= self.JSON_GZIP_MIN
                and "gzip" in (self.headers.get("Accept-Encoding") or "")):
            try:
                data, enc = gzip.compress(data, 6), "gzip"
            except Exception:
                enc = None      # 压不动就原样发,不能让一次压缩失败把响应也带走
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # 动态接口不能交给 Safari/WKWebView 启发式缓存。模型目录、任务和会话状态在
        # 同一个 URL 下会变化；缺少这一头时，手机刷新乃至服务重启后仍可能显示旧 JSON。
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        if enc:
            self.send_header("Content-Encoding", enc)
            # 缓存与代理要知道同一个地址会因 Accept-Encoding 给出不同的字节。
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text, ctype="text/plain; charset=utf-8", code=200):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
