# 由 ui_server.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)
from app import api as app_api
from app_control import api as app_control_api
from core import api as core_api
from deskcfg import FRONTEND, UI_PORT, DATA_HOME
from feed import api as feed_api
from larkbot import api as larkbot_api
from arrangement import api as arrangement_api
from memory import api as memory_api
from provision import api as provision_api
from remote import api as remote_api
from session import api as session_api
from skill import api as skill_api
from system import api as system_api
from task import api as task_api
from usage import api as usage_api
from ui_static import _asset_token
from workflow import api as workflow_api
import deskcfg     # 模块形式引入:后台线程日志统一走 deskcfg.log("<模块>", msg),悬浮窗按 [模块] 过滤
import http.server, socketserver, urllib.request, urllib.error, urllib.parse
import sys, os, signal, atexit, time, socket, subprocess, json, re, queue, struct, gzip, io, stat


class UiBootMixin:

    _WB_API = (task_api, usage_api, workflow_api, session_api, feed_api, remote_api, app_control_api, app_api,
               skill_api, memory_api, provision_api, system_api,
               larkbot_api, arrangement_api, core_api)

    # 开机要的那几样一次给全。经隧道时每个请求的固定开销是一秒多(与响应大小无关,
    # 2026-08-24 实测 2.8KB 与 433KB 一样慢),所以贵的是往返次数,不是字节。
    # 前端原来串行取七样、后来并行成一波;合成一次之后连那一波也省了。
    #
    # 不重写取数逻辑:转调各域已有的处理器,把它们本要写回客户端的 JSON 截下来
    # (见 _Capture)。那些端点以后怎么改,这里跟着变,不会有第二份实现慢慢漂走。
    # terminals 不放这里:它冷启会扫引擎记录,实测可达 1.8s。前端先用 cd_terms 画旧列表,
    # 后台预热与正常轮询再对账；把它内联会让整份 index.html 的首字节一起等扫描结束。
    # 2026-09-03 手机链路盘点:热启动 DCL 那一波还有 6 个 API 各自往返,服务端每个 0.6~2ms,
    # 贵的全在路上——经隧道每个 2 个 RTT 约 240ms 起,六个挤一条管道互相排队,最慢的到 590ms。
    # 能顺路捎的都捎上:apps-rev 进清单,任务快照在 _boot_data 末尾另加。
    # terminals 仍不进来:落盘会话的冷扫要 1.5 秒,会拖住整份 index 的首字节。
    # 元组里不写注释:测试用 `BOOT_PARTS = \((.*?)\)` 截这一段,注释里的括号会让它提前收口。
    BOOT_PARTS = ("/workbench.json", "/workbench/domains", "/workbench/hidden",
                  "/workbench/apps", "/workbench/state", "/workbench/backends",
                  "/workbench/engine", "/workbench/runtime",
                  "/workbench/apps-rev")

    class _Capture:
        """代理真 handler,只把 _send_json 截下来。其余属性(headers、path…)照转。"""

        def __init__(self, real):
            object.__setattr__(self, "_real", real)
            object.__setattr__(self, "result", None)
            object.__setattr__(self, "code", 0)

        def _send_json(self, obj, code=200):
            object.__setattr__(self, "result", obj)
            object.__setattr__(self, "code", code)

        def __getattr__(self, k):
            return getattr(object.__getattribute__(self, "_real"), k)

        def __setattr__(self, k, v):
            setattr(object.__getattribute__(self, "_real"), k, v)

    def _capture_get(self, sub, tag="boot"):
        """转调某条 GET 路由,把它本要写回客户端的那份 JSON 截下来;取不到给 None。

        合并端点都从这里拿数,取数逻辑因此只有一份:那些端点以后怎么改,合并的这几条
        跟着变,不会有第二份实现慢慢漂走。
        """
        cap = self._Capture(self)
        try:
            owner = next((m for m in self._WB_API if m.owns(sub, "GET")), None)
            if owner is None:
                return None
            owner.handle(cap, sub, "", "GET")
        except Exception as e:
            deskcfg.log(tag, "%s 取不到:%s" % (sub, str(e)[:80]))
            return None
        return cap.result if cap.code in (0, 200) else None

    def _boot(self):
        self._send_json(self._boot_data())

    def _boot_data(self):
        out = {}
        for sub in self.BOOT_PARTS:
            if any(m.owns(sub, "GET") for m in self._WB_API):
                out[sub] = self._capture_get(sub)     # 一样取不到不拖垮整份:前端按缺项回落
                continue
            # 不归任何路由的那种:workbench.json 是 frontend 下的静态文件,平时由静态处理器
            # 直接发。扇出只问模块的话它永远是空的,而前端正是拿它判断这份开机数据可不可用
            # (2026-08-25 重启后实测:七项里就它是空的)。
            try:
                with open(os.path.join(FRONTEND, sub.lstrip("/")), encoding="utf-8") as f:
                    out[sub] = json.load(f)
            except Exception as e:
                out[sub] = None
                deskcfg.log("boot", "%s 取不到:%s" % (sub, str(e)[:80]))
        # 任务快照:全部工作流 + 工作台根(_desktop)。前端开机那一波原来要发一条合并请求
        # 再单独取一次 _desktop;服务端算这份只要一两毫秒,捎上就少两个往返。
        # 前端按 key 消费一次即删(app-tasks.js),之后的刷新照旧走网络——不是时间缓存,
        # 只是把「开机第一眼」那份顺路带过去;变更仍由 task.updated 推送失效。
        try:
            doms = out.get("/workbench/domains")
            ids = [d.get("id") for d in doms if isinstance(d, dict)] if isinstance(doms, list) \
                else list(doms.keys()) if isinstance(doms, dict) else []
            out["/workbench/tasks"] = self._tasks_map([x for x in ids if x] + ["_desktop"])
        except Exception as e:
            deskcfg.log("boot", "任务快照取不到:%s" % str(e)[:80])
        return out

    # 一次把几个工作流的任务给全。前端原来一个工作流打一个请求,而侧栏折叠计数、任务页、
    # 设置页统计各遍历一遍,同一时刻就是十来个(2026-08-25 实测 11 个,去重后仍有 5 个)。
    # 服务端这头一个只要 1~3 毫秒、响应最大 4.8KB,贵的全在路上:经隧道时握手中位 384ms,
    # 四分之一的新连接还要多付整整一秒。合成一条,五次往返变一次。
    TASKS_BUNDLE_MAX = 64      # 只是防滥用的上限,正常就几个

    def _tasks_map(self, ids):
        """{工作流 id: 那一份 /workbench/domains/<id>/tasks 的响应}。合并端点与开机内联共用。"""
        out = {}
        for did in [x for x in ids if x][:self.TASKS_BUNDLE_MAX]:
            out[did] = self._capture_get(
                "/workbench/domains/%s/tasks" % urllib.parse.quote(did, safe=""), "tasks")
        return out

    def _tasks_bundle(self, q):
        params = urllib.parse.parse_qs(q)
        if (params.get("scope") or [""])[0] == "all":
            try:
                import task_ledger
                doms = self._capture_get("/workbench/domains", "tasks") or []
                ids = [x.get("id") for x in doms if isinstance(x, dict) and x.get("id")]
                try:
                    ids += [x for x in os.listdir(os.path.join(deskcfg.REPO_ROOT, "domains"))
                            if os.path.isdir(os.path.join(deskcfg.REPO_ROOT, "domains", x))]
                except OSError:
                    pass
                ids = list(dict.fromkeys(ids + ["_desktop"]))
                return self._send_json(task_ledger.aggregate(
                    DATA_HOME, ids[:self.TASKS_BUNDLE_MAX], deskcfg.REPO_ROOT))
            except Exception as e:
                deskcfg.log("tasks", "全局聚合失败:%s" % str(e)[:120])
                return self._send_json({"error": "任务聚合失败"}, 500)
        ids = params.get("ids", [""])[0]
        self._send_json(self._tasks_map(ids.split(",")))

    def _workbench(self, method):
        p, _, q = self.path.partition("?")
        if p == "/workbench/boot" and method == "GET":
            return self._boot()
        if p == "/workbench/tasks" and method == "GET":
            return self._tasks_bundle(q)
        for mod in self._WB_API:
            if mod.owns(p, method):
                return mod.handle(self, p, q, method)
        self._send_json({"error": "unknown"}, 404)

    def _is_workbench(self):
        return self.path.split("?", 1)[0].startswith(("/workbench/", "/dev/"))

    def _serve_manifest(self):
        """manifest 动态发:把访问用的票回填进 start_url。

        「添加到主屏」之后,从主屏图标进的是一个独立的浏览器上下文——iOS 上 PWA 与 Safari
        不共享 Cookie。而 relay 那侧首次访问会 302 掉地址栏里的 token(种成 Cookie),
        于是主屏图标启动时既没票也没 Cookie,一开就是 401(2026-08-18 用户实见)。
        manifest 是由页面自己发起的请求(same-origin credentials),Cookie 会带上,
        所以这里读得到票;把它写进 start_url,主屏图标就带票启动,进去再种它自己那份 Cookie。

        本机直连(没有票)时原样发,start_url 保持 "/"。
        """
        try:
            with open(os.path.join(FRONTEND, "manifest.json"), encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return super().do_GET()
        tok = ""
        for part in str(self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "cd_remote" and v:
                tok = v
                break
        if not tok:   # 首访那一次票还在地址栏上(302 之前),也认
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            for kv in q.split("&"):
                k, _, v = kv.partition("=")
                if k == "token" and v:
                    tok = urllib.parse.unquote(v)
                    break
        marks = []
        if tok:
            marks.append("token=" + urllib.parse.quote(tok))
        if marks:
            data["start_url"] = "/?" + "&".join(marks)
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")   # 票会变,别让它被缓存住
        self.end_headers()
        self.wfile.write(raw)

    def _serve_index(self):
        # index.html 给每份 css/js 引用打自己的内容指纹；文件变化即 URL 变化，
        # webview 因此重取那一份前端资源（绕开 WKWebView 缓存竞态）。运行时是否需要整页
        # 更新则由 SSE 握手里的整树版本负责，二者分工见 ui_static.py。
        try:
            with open(os.path.join(FRONTEND, "index.html"), encoding="utf-8") as f:
                html = f.read()
        except Exception:
            return super().do_GET()
        # 指纹逐个文件算(见 _asset_token):改了哪个只有哪个的缓存键变,
        # 其余的客户端照旧用缓存,不必重下。
        html = html.replace('href="/style.css"',
                            f'href="/style.css?v={_asset_token("/style.css")}"')
        # app.js 已拆成 app-*.js:给每个本地脚本加令牌(逐一缓存刷新)
        html = re.sub(r'src="(/app[\w-]*\.js)"',
                      lambda m: f'src="{m.group(1)}?v={_asset_token(m.group(1))}"', html)
        # index.html 里静态引用的 vendor 库(markdown-it、highlight、github-dark)一并打令牌:
        # 原来漏了这几个,长缓存的规则只认 style.css/app-*.js,这几个每次都 no-store 重下
        # (2026-08-24 实见:手机经隧道首次加载慢,这几个占了将近一半的静态字节)。
        # 动态按需加载的 vendor 资源不在这条路上，靠 /vendor/ 的 no-cache + 条件请求兜底。
        html = re.sub(r'(?:href|src)="(/vendor/[\w.-]+\.(?:js|css))"',
                     lambda m: m.group(0).replace(
                         m.group(1), f'{m.group(1)}?v={_asset_token(m.group(1))}'), html)
        # 开机那份数据直接内联进来:index.html 本身是 no-store、每次加载都要取,
        # 顺路把它捎上就少一个往返。经隧道时一个往返一两秒(与响应大小几乎无关),
        # 而热缓存刷新原本只剩两个请求——index 与 /workbench/boot,内联之后只剩一个。
        #
        # 数据是请求时现取的,与单独调那条路一样新;前端拿不到就照旧去请求,不挑后端版本。
        # 取不到不能拖垮首页:白页比慢一秒严重得多,所以整段包在 try 里。
        try:
            boot = json.dumps(self._boot_data(), ensure_ascii=False)
            # `</script>` 若原样出现在 JSON 字符串里会提前闭合脚本标签,整页从那里断掉。
            # 转成 \u003c 即可,JSON 解析出来仍是同一个字符。
            boot = boot.replace("<", "\\u003c")
            html = html.replace('<script src="/app-core.js',
                                '<script>window.__BOOT__=%s;</script>\n  <script src="/app-core.js'
                                % boot, 1)
        except Exception as e:
            deskcfg.log("boot", "内联开机数据失败,前端会自己再取一趟:%s" % str(e)[:80])
        data = html.encode("utf-8")
        # 入口页走的是这条自己的路,吃不到 send_head 那道 gzip。它是首字节,压一下值得
        # (16KB → 5KB);而且它每次都现拼令牌,不能落缓存,只能现压——几十 KB,可以忽略。
        enc = None
        if "gzip" in (self.headers.get("Accept-Encoding") or ""):
            data, enc = gzip.compress(data, 6), "gzip"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if enc:
            self.send_header("Content-Encoding", enc)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
