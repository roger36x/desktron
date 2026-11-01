# 由 ui_server.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)
from engine import _bus, _adapter, _shepherd, _shepherd_trial, GUI_ENGINES, _cleanup, _parent_death_watchdog
from engine_adapters import SESSION_WINDOW   # 铺几个回合的单一口径,搬迁与取消息共用
from term import (_TERMS, _TERMS_LOCK, _live_map, _term_attach_mark, _term_seen, _term_meta_read,
                  _term_persisted, _term_quiet_set, _term_context_domain, _git_branch_for_cwd,
                  SIDEBAR_SERVED, _gui_read_status)
from workbench import _wb_state_read, _engine_pref
from session_search import search_sessions, history_around
import http.server, socketserver, urllib.request, urllib.error, urllib.parse
import sys, os, signal, atexit, time, socket, subprocess, json, re, queue, struct, gzip, io, stat, threading


_SESSION_META_CONTEXT = threading.local()


def _engine_update(name):
    try:
        import engine_updates
        from deskcfg import REPO_ROOT as _root
        return engine_updates.status(name, engine_updates.install_cmd(_root, name))
    except Exception:
        return {}


_LEGACY_PEER_RESULT = re.compile(
    r"^另一个 Desk 会话已完成问询 (pm_[0-9a-f]{32})。(?:\n|$)")


def _release_legacy_peer_result(sid, text):
    """Fence detached watchers started by the pre-isolation desk-msg version."""
    text = str(text or "")
    hit = _LEGACY_PEER_RESULT.match(text)
    if (not hit or "——这是先前跨会话问询的迟到结果，不是用户的新指令。" not in text):
        return False
    try:
        import peer_mailbox
        row = peer_mailbox.get(hit.group(1))
        legacy_claim = row and row.get("result_status") in ("replied", "failed")
        if (legacy_claim and row.get("status") == "delivering"
                and row.get("source_sid") == str(sid or "")):
            peer_mailbox.release_delivery(
                hit.group(1), "Desk 已升级为显式 wait；旧后台反投已拦截")
    except Exception:
        pass
    # The old envelope itself is a versioned migration marker.  Fence it even
    # if its seven-day mailbox row was pruned between the detached process
    # reading the result and POSTing it to this upgraded server.
    return True


class UiContractMixin:

    def _is_contract(self):
        """引擎契约端点:各引擎一律由适配器实现。"""
        p = self.path.split("?", 1)[0]
        return (p == "/session" or p.startswith("/session/") or
                p in ("/agent", "/config", "/event"))

    @staticmethod
    def _adapter_owning(sid):
        """哪个引擎的适配器持有这段会话。

        各引擎有独立存储(data/adapter/<引擎>/),而会话 id 是全局唯一的一串随机值。
        原来所有会话端点都按「当前偏好的引擎」取适配器,于是切一次引擎,已经开着的那个
        会话视图就把请求发给了一个不认识它的适配器:读消息读回空的,界面据此重建,
        已展开的工具步、正在流的那一段全没了。按 id 找归属与偏好无关,切引擎不再影响它。
        """
        # Shepherd Conversation 是用户可见会话的独立门面，不是 GUI 执行引擎；优先认领。
        try:
            shepherd = _shepherd_trial() if str(sid).startswith("adp_trial_") else _shepherd()
            if sid in (getattr(shepherd, "index", {}) or {}):
                return shepherd
        except Exception:
            pass
        if str(sid).startswith("adp_trial_"):
            return None
        # 两种 id 都认:工作台自己的会话号(adp_,索引的键),以及引擎侧的锚点。
        for name in GUI_ENGINES:
            try:
                ad = _adapter(name)
            except Exception:
                continue
            idx = getattr(ad, "index", {}) or {}
            if sid in idx:
                return ad
            for rec in idx.values():
                if isinstance(rec, dict) and rec.get("engine_sid") == sid:
                    return ad
        return None

    @staticmethod
    def _session_belonging(row):
        """补齐一行会话记录的归属(domain)与引用(refs)。

        归属首选会话记录自己写的那一份:GUI 新建会话时带着当前工作流建,写在适配器索引里,
        与引擎侧有没有 id 无关。搬迁进来的老会话当年建的时候没带工作流(见 import_session),
        索引里那一格是空的,那就退到会话表按引擎 id 记的那份 meta——那是终端那条路登记的。
        两者都没有才算真的没有归属。
        """
        out = dict(row)
        esid = str(out.get("engine_sid") or "").strip()
        meta_index = getattr(_SESSION_META_CONTEXT, "value", None)
        meta = ((meta_index if meta_index is not None else _term_meta_read()).get(esid) or {}) if esid else {}
        if not str(out.get("domain") or "").strip():
            out["domain"] = str(meta.get("domain") or "")
        refs = [x for x in (meta.get("refs") or []) if isinstance(x, str) and x]
        if refs:
            out["refs"] = refs
        return out

    @classmethod
    def _sessions_listing(cls, only=""):
        """全部 GUI 引擎的会话,按更新时间倒排,每行标明自己属于哪个引擎。

        原来只列当前偏好那一个适配器的会话。会话 id 全局唯一,而前端拿着一个 id 来问
        「它的引擎侧 id 是什么、它属于哪个工作流」——偏好一旦不是它自己那个引擎,这段会话
        就不在返回里,前端据此判定「查不到」。列表变长的代价很小(本机四个适配器合计几十行),
        换来的是任何引擎下的会话都查得到。要单看一个引擎的,带 ?engine= 。
        """
        rows = []
        # term-meta is one shared immutable snapshot for this response. Reading it
        # per row was an avoidable disk N+1 on the all-engine session endpoint.
        try:
            meta_index = _term_meta_read()
        except Exception:
            meta_index = {}
        _SESSION_META_CONTEXT.value = meta_index
        try:
            for getter in (_shepherd, _shepherd_trial):
                try:
                    items = getter().list_sessions() or []
                except Exception:
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    engine = item.get("engine") or ""
                    if only and engine != only:
                        continue
                    row = cls._session_belonging(item)
                    row["engine"] = engine
                    row["shepherd_mode"] = True
                    rows.append(row)
            for name in GUI_ENGINES:
                if only and name != only:
                    continue
                try:
                    ad = _adapter(name)
                    items = ad.list_sessions() or []
                except Exception:
                    continue
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    row = cls._session_belonging(it)
                    row["engine"] = name
                    rows.append(row)
        finally:
            _SESSION_META_CONTEXT.value = None
        rows.sort(key=lambda s: -((s.get("time") or {}).get("updated") or 0))
        return rows

    def _adapter_route(self, method):
        p = self.path.split("?", 1)[0]
        # 新建会话的引擎在请求体里，读到之后再选适配器。
        ad = None if p == "/session" and method == "POST" else _adapter(_engine_pref())
        seg = p.split("/")
        # 会话相关的端点一律按会话归属走,不看偏好。找不到归属的(比如刚要新建)退回偏好。
        if len(seg) >= 3 and seg[1] == "session" and seg[2] not in ("", "adopt", "search"):
            owner = self._adapter_owning(seg[2])
            if owner is not None:
                ad = owner
            elif seg[2].startswith("adp_trial_"):
                return self._send_json({"error": "trial session not found"}, 404)
        if p == "/session/search" and method == "GET":
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            query = (q.get("q") or [""])[0].strip()
            engine = (q.get("engine") or [_engine_pref()])[0].strip()
            sort = (q.get("sort") or [""])[0].strip()
            if engine == "shepherd":
                responses = []
                for getter in (_shepherd, _shepherd_trial):
                    try:
                        responses.append(getter().search_sessions(query, sort=sort))
                    except Exception as exc:
                        responses.append({"results": [], "indexing": False,
                                          "progress": {"done": 0, "total": 0},
                                          "error": str(exc)[:200]})
                results = [row for response in responses for row in (response.get("results") or [])]
                if sort == "time":
                    results.sort(key=lambda row: -max(
                        [int(hit.get("at") or 0) for hit in (row.get("hits") or [])] or [0]))
                else:
                    results.sort(key=lambda row: (-int(row.get("count") or 0),
                                                  -int(row.get("updated") or 0)))
                progress = {"done": sum(int((r.get("progress") or {}).get("done") or 0)
                                        for r in responses),
                            "total": sum(int((r.get("progress") or {}).get("total") or 0)
                                         for r in responses)}
                errors = [str(r.get("error") or "") for r in responses if r.get("error")]
                return self._send_json({"results": results[:30],
                    "indexing": any(bool(r.get("indexing")) for r in responses),
                    "progress": progress, "error": errors[0] if errors else "",
                    "warnings": [w for r in responses for w in (r.get("warnings") or [])]})
            if engine not in GUI_ENGINES:
                return self._send_json({"error": "unknown engine"}, 400)
            try:
                search_adapter = _adapter(engine)
            except Exception:
                return self._send_json({"error": "engine unavailable"}, 503)
            return self._send_json(search_sessions(query, search_adapter, sort=sort))
        if p == "/event":
            return self._adapter_sse()
        if p == "/agent":
            return self._send_json(ad.agents() or [{"name": ad.name}])
        if p == "/config":
            # caps:引擎能力位,前端据此显隐按钮与切档位表。各引擎的差别都收在这里,
            # 前端不按引擎名写 if——加第四个引擎时界面零改动。
            # 允许显式问某个引擎:终端头部那枚「网页视图」要按被点的那段会话所属引擎判断
            # 能不能搬迁,而那未必是当前偏好的引擎。
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            want = (q.get("engine") or [""])[0].strip()
            session_id = (q.get("session") or [""])[0].strip()
            if session_id:
                owner = self._adapter_owning(session_id)
                if owner is not None:
                    ad = owner
            current_engine = ad.execution_name(session_id) if hasattr(ad, "execution_name") else ad.name
            if want and want != current_engine and ad.name != "shepherd":
                try:
                    ad = _adapter(want)
                except Exception:
                    pass
            engine_name = ad.execution_name(session_id) if hasattr(ad, "execution_name") else ad.name
            legacy = _wb_state_read().get("engine_opts", {}).get(engine_name, {})
            # 会话跑出来的异常比引擎级预检更具体。原来字典里写了两次 environment,
            # 后一项会静默覆盖前一项,刷新后证书/限流等现场因此消失。
            environment_revision = 0
            if session_id and callable(getattr(type(ad), 'environment_snapshot', None)):
                snapshot = ad.environment_snapshot(session_id)
                environment, environment_revision = snapshot['environment'], snapshot['revision']
            else:
                environment = ad.session_environment(session_id) if session_id else None
            if not environment and hasattr(ad, "env_notice"):
                environment = ad.env_notice()
            if not environment and callable(getattr(ad, "recap_notice", None)):
                environment = ad.recap_notice()
            caps = {
                "recall": bool(getattr(ad, "recall_supported", True)),
                "rewind": bool(getattr(ad, "rewind_supported", False)),
                "fork": bool(getattr(ad, "fork_supported", False)
                             or getattr(ad, "fork_flag", None)),
                "compact": bool(getattr(ad, "compact_supported", False)
                                and (not callable(getattr(ad, "compact_available", None))
                                     or not session_id or ad.compact_available(session_id))),
                "adopt": bool(getattr(ad, "import_session", None)),
                "search": bool(getattr(ad, "search_supported", False)
                               and callable(getattr(ad, "search_source_path", None))
                               and callable(getattr(ad, "search_documents", None))),
                "stream": bool(getattr(ad, "streams_text", False)),
                "permission": bool(getattr(ad, "asks_permission", False)),
            }
            native_commands = (ad.slash_commands_for_session(session_id)
                              if session_id and hasattr(ad, "slash_commands_for_session")
                              else ad.slash_commands() if hasattr(ad, "slash_commands") else [])
            from engine_adapters.command_manifest import resolve as command_manifest
            return self._send_json({
                "engine": engine_name,
                "environment_revision": environment_revision,
                "shepherd_mode": bool(ad.name == "shepherd"),
                "shepherd_trial": getattr(ad, "trial", False) is True,
                "trial_policy": ad.trial_policy() if getattr(ad, "trial", False) is True else None,
                "model": ad.config_label(session_id),   # 按这一段会话自报的来,不是引擎下任意一段
                "opts": ad.session_options(session_id, legacy) if session_id else dict(legacy),
                "environment": environment,
                # 后台命令(Bash run_in_background)此刻有几条在跑。跟 environment 一样走
                # /config 而不是只靠 SSE:它活得比回合长,刷新之后不从这里拿就一刷归零。
                "bgtasks": (ad.bg_tasks(session_id) if session_id and hasattr(ad, "bg_tasks")
                            else {"running": 0, "started": []}),
                # 这家引擎有没有新版本。同样只读缓存立刻回,问 npm 的活在后台线程里
                # (见 engine_updates)。Desk 此前对这件事完全无感——available() 只答
                # 「装没装」,而知道引擎更新了全靠在终端里跑它时它自己提示。
                "update": _engine_update(engine_name),
                "options": (ad.options_for_session(session_id) if hasattr(ad, "options_for_session")
                            else ad.options_catalog() if hasattr(ad, "options_catalog") else {}),
                "execution": ad.execution_status(session_id) if hasattr(ad, "execution_status") else None,
                "slash": native_commands,
                "command_manifest": command_manifest(
                    engine=engine_name, shepherd=ad.name == 'shepherd',
                    caps=caps, native_commands=native_commands),
                "caps": caps,
            })
        if p == "/session" and method == "GET":
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            return self._send_json(self._sessions_listing((q.get("engine") or [""])[0].strip()))
        if p == "/session/adopt" and method == "POST":
            # 认领一段引擎侧会话:把引擎自己的记录翻成契约消息,整份覆盖进适配器存储。
            # 终端会话与 HTML 会话由此成为同一段会话的两个视图——两条路最终 resume 的是
            # 同一个引擎会话,历史也在同一份记录里。各引擎读法不同:claude 读 transcript,
            # codex 家族读 rollout。
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send_json({"error": "bad body"}, 400)
            # 被点的那段会话未必属于当前偏好的引擎。两种指认方式:显式给引擎名(从终端头部
            # 进来时用,那时还没有我们这侧的会话),或者给已有会话的 id(回合末对账用,
            # 那时会话已经存在,按归属找比让前端记住自己属于谁可靠)。都没有才退回偏好。
            want = str(body.get("engine") or "").strip()
            owner = self._adapter_owning(str(body.get("sid") or "")) if body.get("sid") else None
            if owner is not None:
                ad = owner
            elif want and want != ad.name:
                try:
                    ad = _adapter(want)
                except Exception:
                    pass
            if not getattr(ad, "import_session", None):
                return self._send_json({"error": "该引擎不支持视图切换"}, 400)
            r = ad.import_session(str(body.get("engine_sid") or ""))
            return self._send_json(r or {"error": "读不到这段会话的记录"}, 200 if r else 404)
        if p == "/session" and method == "POST":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send_json({"error": "bad body"}, 400)
            if not isinstance(body, dict):
                return self._send_json({"error": "bad body"}, 400)
            want = body.get("engine")
            if want is not None and not isinstance(want, str):
                return self._send_json({"error": "unknown engine"}, 400)
            want = (want or _engine_pref()).strip()
            if want not in GUI_ENGINES:
                return self._send_json({"error": "unknown engine"}, 400)
            if 'shepherd_trial' in body and type(body['shepherd_trial']) is not bool:
                return self._send_json({'error':'bad shepherd_trial'},400)
            if body.get('shepherd_trial') is True and body.get('shepherd_mode') is not True:
                return self._send_json({'error':'trial requires shepherd_mode'},400)
            try:
                ad = ((_shepherd_trial() if body.get('shepherd_trial') is True else _shepherd())
                      if body.get("shepherd_mode") is True else _adapter(want))
            except Exception:
                return self._send_json({"error": "engine unavailable"}, 503)
            import self_update
            with self_update.admit_work(user_activity=True) as admitted:
                if not admitted:
                    return self._send_json({"error": "Desk 正在准备强制更新，请稍后再新建会话"}, 409)
                initial_opts = {}
                if "desk_role" in body:
                    initial_opts["desk_role"] = body.get("desk_role")
                if body.get("shepherd_mode") is True:
                    initial_opts["shepherd_mode"] = True
                    initial_opts["active_engine"] = want
                try:
                    row = ad.create_session(str(body.get("domain") or ""),
                                            task=bool(body.get("task")), opts=initial_opts or None)
                except (RuntimeError, ValueError) as exc:
                    return self._send_json({'error':str(exc)},409)
                return self._send_json(row)
        if len(seg) == 3 and method == "DELETE":
            try:
                ad.delete_session(seg[2])
            except RuntimeError as exc:
                if getattr(exc, "status", None) == 409:
                    error = {"error": str(exc)}
                    if getattr(exc, "code", None):
                        error["code"] = exc.code
                    return self._send_json(error, 409)
                raise
            self.send_response(204); self.end_headers()
            return
        if len(seg) == 4 and seg[3] == "situation" and method == "GET":
            # 会话页右卡的四项口径：项目与制品属于工作流；Recap 属于当前会话；
            # 变更由前端从当前会话已载入的结构化编辑 part 汇总，仍复用审阅侧栏。
            sid = seg[2]
            domain = (ad.grounding_domain(sid) if hasattr(ad, "grounding_domain")
                      else ad.session_domain(sid) if hasattr(ad, "session_domain") else "")
            workspace = str(domain or "_desktop").split(":", 1)[0] or "_desktop"
            try:
                from domains import _repo_items
                projects = []
                for item in _repo_items(workspace):
                    row = dict(item)
                    row["branch"] = _git_branch_for_cwd(row.get("dir") or row.get("path"))
                    projects.append(row)
            except Exception:
                projects = []
            try:
                import artifacts as _artifacts
                items = _artifacts.list_items(workspace)
            except Exception:
                items = []
            recap = dict(((ad.index.get(sid) or {}).get("recap") or {}))
            return self._send_json({"domain": workspace, "projects": projects,
                                    "artifacts": items, "recap": recap or None})
        if len(seg) == 4 and seg[3] == "queue-recovery" and method == "GET":
            from engine_adapters.queue_recovery import available
            return self._send_json({"available":available(ad, seg[2])})
        if len(seg) == 4 and seg[3] == "queue-recovery" and method == "POST":
            from engine_adapters.queue_recovery import recover
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                with ad._lock:
                    if seg[2] not in ad.index:
                        return self._send_json({"error":"session not found"}, 404)
                    result = recover(ad, seg[2], body.get("owner"), body.get("ack", ""))
                return self._send_json(result)
            except ValueError as exc:
                return self._send_json({"error":str(exc)}, 409)
        if len(seg) == 6 and seg[3] == "queue-recovery" and method == "GET":
            from engine_adapters.queue_recovery import attachment
            path = attachment(ad, seg[2], seg[4], seg[5])
            if not path or not os.path.isfile(path):
                return self._send_json({"error":"attachment not found"}, 404)
            with open(path, "rb") as stream:
                raw = stream.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if len(seg) == 4 and seg[3] == "abort" and method == "POST":
            # 撤回:这句话还没被处理,杀掉进程并把正文交还前端重编辑
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            r = ad.abort(seg[2], recall=bool(body.get("recall")),
                         **({'client_request_id':str(body.get('client_request_id') or '')}
                            if hasattr(ad, 'request_status') else {}))
            return self._send_json(r or {"error": "这一回合已经结束"}, 200 if r else 409)
        if len(seg) == 5 and seg[3] == 'request' and method == 'GET':
            if not hasattr(ad, 'request_status'):
                return self._send_json({'error':'unsupported'}, 404)
            from engine_adapters.queue_recovery import request_status
            return self._send_json(request_status(ad, seg[2], seg[4]) or ad.request_status(seg[2], seg[4]))
        if len(seg) == 6 and seg[3] == 'request' and seg[5] == 'cancel-unsent' and method == 'POST':
            if ad.name == 'shepherd':
                return self._send_json(ad.requests.cancel(seg[2], seg[4], only_before_start=True))
            if not getattr(ad, 'delivery', None):
                return self._send_json({'error':'unsupported'}, 404)
            return self._send_json(ad.cancel_unsent_delivery(seg[2], seg[4]))
        if len(seg) == 6 and seg[3] == 'request' and seg[5] == 'accept-unknown' and method == 'POST':
            if ad.name == 'shepherd' or not getattr(ad, 'delivery', None):
                return self._send_json({'error':'unsupported'}, 404)
            return self._send_json(ad.resolve_delivery(seg[2], seg[4]))
        if len(seg) == 4 and seg[3] == "state" and method == "GET":
            # 控制面权威状态：前端据 ready 释放队列，不从消息内容或超时反推引擎是否空闲。
            return self._send_json(ad.turn_state(seg[2]) if hasattr(ad, "turn_state")
                                   else {"state": "ready", "generation": 0, "contract": 0})
        if len(seg) == 4 and seg[3] == "around" and method == "GET":
            # 全文搜索点进来只铺目标附近的小窗，不为一处命中加载整段长历史。
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            want = (q.get("engine") or [""])[0].strip()
            if want:
                if want not in GUI_ENGINES or (ad.name != "shepherd" and ad.name != want):
                    return self._send_json({"error": "session not found in engine"}, 404)
            def _i(k, default):
                try:
                    return max(0, int((q.get(k) or [default])[0]))
                except Exception:
                    return default
            target = (q.get("message") or [""])[0]
            result = history_around(ad, seg[2], _i("turn_from_end", 0),
                                    _i("before", 6), _i("after", 6), target)
            result["target"] = target
            return self._send_json(result)
        if len(seg) == 4 and seg[3] == "history" and method == "GET":
            # 往上翻页:更早的回合,只读不落盘
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            def _i(k, d):
                try: return max(0, int((q.get(k) or [d])[0]))
                except Exception: return d
            return self._send_json(ad.history(seg[2], _i("have", 0), _i("limit", 30))
                                   if hasattr(ad, "history") else [])
        if len(seg) == 4 and seg[3] == "goal" and method == "GET":
            # 会话级目标(/goal 注册的 Stop hook):条件与是否已达成
            return self._send_json(ad.goal(seg[2]) if hasattr(ad, "goal") else {})
        if len(seg) == 4 and seg[3] == "agents" and method == "GET":
            # 这一段会话派出去的子代理:名字、起止、最新一步
            return self._send_json(ad.subagents(seg[2]) if hasattr(ad, "subagents") else [])
        if len(seg) == 5 and seg[3] == "agents" and method == "GET":
            # 某个子代理的输出,只读
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            limit_bytes = (q.get("tail_bytes") or [""])[0]
            try:
                limit = max(0, int((q.get("limit") or [0])[0]))
            except Exception:
                limit = 0
            # Agent IDs from Shepherd contain ':' and are URL encoded by the browser.
            # Decode once here, then let each adapter apply its own strict identity regex.
            agent_id = urllib.parse.unquote(seg[4])
            kwargs = {"limit_bytes": limit_bytes}
            if limit:
                kwargs["limit"] = limit
            return self._send_json(ad.subagent_log(seg[2], agent_id, **kwargs)
                                   if hasattr(ad, "subagent_log") else [])
        if len(seg) == 4 and seg[3] == "pulse" and method == "GET":
            # 记录有没有变:大小与修改时刻,给界面轮询用
            return self._send_json(ad.transcript_stat(seg[2]) if hasattr(ad, "transcript_stat") else {})
        if len(seg) == 4 and seg[3] == "seen" and method == "POST":
            sid = seg[2]
            if sid not in ad.index:
                sid = next((k for k, row in ad.index.items() if row.get("engine_sid") == sid), sid)
            if sid not in ad.index:
                return self._send_json({"error": "session not found"}, 404)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                generation = body.get("generation")
                if generation is not None and (type(generation) is not int or generation < 0):
                    raise ValueError("invalid generation")
            except (ValueError, TypeError, AttributeError):
                return self._send_json({"error": "bad body"}, 400)
            status = _gui_read_status(ad, sid)
            if status is not None:
                current = status["completion_generation"]
                # 打开仍在执行的回合，只读过此前内容；不提前确认本轮将来的完成。
                ceiling = status.get("completed_generation",
                                     max(0, current - 1) if status["busy"] else current)
                seen = min(generation, ceiling) if generation is not None else ceiling
                _term_seen((ad.name, sid), seen)
                return self._send_json({"seen_generation": seen})
            esid = (ad.index.get(sid) or {}).get("engine_sid")
            if esid:
                _term_seen(esid)
            self.send_response(204); self.end_headers()
            return
        if len(seg) == 4 and seg[3] == "reconcile" and method == "POST":
            if ad.name != "shepherd":
                return self._send_json({"error": "not a Shepherd conversation"}, 400)
            try:
                body = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length", 0))) or b"{}")
                attempt_id = str(body.get("attempt_id") or "").strip()
                action = str(body.get("action") or "").strip()
                if not attempt_id or action != "omit_and_continue":
                    raise ValueError("bad reconciliation action")
                return self._send_json(ad.reconcile_end(seg[2], attempt_id))
            except (ValueError, RuntimeError) as exc:
                return self._send_json({"error": str(exc)}, 409)
        if len(seg) == 4 and seg[3] == "engine":
            if method == "GET":
                if ad.name != "shepherd":
                    return self._send_json({"error": "not a Shepherd conversation"}, 400)
                active = ad.service.active(seg[2])
                binding = active.get("binding") or {}
                state = active.get("state") or {}
                return self._send_json({"engine": binding.get("engine") or "",
                    "committed_offset": state.get("committed_offset", 0),
                    "applied_offset": binding.get("applied_offset", 0),
                    "switching": bool(state.get("pending_switch"))})
            if method == "PUT":
                if ad.name != "shepherd":
                    return self._send_json({"error": "not a Shepherd conversation"}, 400)
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                    target = str(body.get("engine") or "").strip()
                    if target not in GUI_ENGINES:
                        raise ValueError("unknown engine")
                    return self._send_json(ad.switch_engine(seg[2], target))
                except (ValueError, RuntimeError) as exc:
                    return self._send_json({"error": str(exc)}, 409)
        if len(seg) == 4 and seg[3] == "settings":
            engine_name = ad.execution_name(seg[2]) if hasattr(ad, "execution_name") else ad.name
            legacy = _wb_state_read().get("engine_opts", {}).get(engine_name, {})
            if method == "GET":
                return self._send_json({"opts": ad.session_options(seg[2], legacy)})
            if method == "POST":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                    patch = body.get("opts") or {}
                    target_engine = str(body.get("engine") or engine_name).strip()
                    assert isinstance(patch, dict)
                    if ad.name == "shepherd" and target_engine not in GUI_ENGINES:
                        raise ValueError("unknown engine")
                    if ad.name == "shepherd":
                        legacy = _wb_state_read().get("engine_opts", {}).get(target_engine, {})
                except Exception:
                    return self._send_json({"error": "bad body"}, 400)
                try:
                    opts = (ad.update_session_options(seg[2], patch, legacy, engine=target_engine)
                            if ad.name == "shepherd"
                            else ad.update_session_options(seg[2], patch, legacy))
                except (ValueError, RuntimeError) as exc:
                    return self._send_json({"error": str(exc)}, getattr(exc, "status", 400))
                if opts is None:
                    return self._send_json({"error": "no such session"}, 404)
                return self._send_json({"ok": True, "opts": opts})
        if len(seg) == 5 and seg[3] == "part" and method == "GET":
            # 某一块的全文。列表里工具输出是截断的(见 LIST_OUTPUT_CAP),展开那一步按这条取回来
            # ——codex 的会话里工具输出占返回体的 99%,而它们默认折叠着,整份送过去是白送。
            got = ad.part(seg[2], urllib.parse.unquote(seg[4])) if hasattr(ad, "part") else None
            return self._send_json(got or {}, 200 if got else 404)
        if len(seg) == 5 and seg[3] == "asset" and method == "GET":
            # 会话附件(截图、上传的文件)。正文里只留这条 URL,不再内联 base64——
            # 理由见 engine_adapters 的 ASSET_SEG 一段。文件名带时间戳或内容哈希,
            # 同名即同物,所以给一年的不可变缓存:切回同一段会话时图片不再重取。
            import mimetypes
            path = ad.asset_path(seg[2], urllib.parse.unquote(seg[4]))
            if not path:
                return self._send_json({"error": "not found"}, 404)
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
            except OSError:
                return self._send_json({"error": "not found"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(raw)
            return
        if len(seg) == 4 and seg[3] == "message" and method == "GET":
            # 只给最近若干回合,与搬迁那条路同一个上限。存储是只增的:每跑一回合追加一条,
            # 长会话不设限地全量吐给界面,一次要铺几百个回合上千个工具卡,开一次会话就卡住
            # ——而更早的并没有丢,往上拉到顶会按 30 个一批经 /history 现取。
            # 上限设在这里而不是 messages():那个方法还被 _drop_since 之类的写回路径用着,
            # 在那里截断等于把存储本身裁掉。
            # 窗口内那段已经序列化好、按文件戳缓存(见 messages_window_json):
            # 同一段会话反复打开时不再重读重解析重序列化。跑动中的那一条每次都新拼——
            # 它本来就在变,缓存它没有意义,而且会让缓存每一帧都失效。
            body = ad.messages_window_json(seg[2], SESSION_WINDOW)
            live = ad.live_message(seg[2]) if hasattr(ad, "live_message") else None
            if live:
                one = json.dumps(live, ensure_ascii=False).encode("utf-8")
                body = body + b"," + one if body else one
            # 走公用的发送:它按 Accept-Encoding 压缩。这一份是全站最大的一个响应
            # (2026-08-25 实测一段会话 741KB,gzip 之后 205KB),经隧道时四秒多。
            self._send_json_bytes(b"[" + body + b"]")
            return
        if len(seg) == 4 and seg[3] == "recall" and method == "POST":
            if not hasattr(ad, "resolve_recall"):
                return self._send_json({"error": "当前引擎不支持撤回恢复"}, 400)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                operation_id, action = body["operation_id"], body["action"]
                assert isinstance(operation_id, str) and action in ("auto", "retry", "keep", "recover")
            except Exception:
                return self._send_json({"error": "bad recall action"}, 400)
            import self_update
            with self_update.tracked_work(user_activity=True) as admitted:
                if not admitted:
                    return self._send_json({"error": "Desk 正在准备更新，请稍后处理撤回"}, 409)
                result = ad.resolve_recall(seg[2], operation_id, action)
            return self._send_json(result, 200 if result.get("ok") else 409)
        if len(seg) == 4 and seg[3] == "fork" and method == "POST":
            # 分支:从某条助手消息另开一段会话,原会话不动(前端「分支」钮)
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send_json({"error": "bad body"}, 400)
            if not isinstance(body, dict) or not isinstance(body.get('msg_id', ''), str):
                return self._send_json({'error': 'bad fork message'}, 400)
            import self_update
            from shepherd.store import Conflict, InvalidState
            try:
                with self_update.tracked_work(user_activity=True) as admitted:
                    if not admitted:
                        return self._send_json({'error': 'Desk 正在准备更新，请稍后分支'}, 409)
                    r = ad.fork_session(seg[2], body.get("msg_id") or "")
            except (Conflict, InvalidState) as exc:
                return self._send_json({'error': str(exc), 'code': 'fork_unavailable'}, 409)
            if not r:
                return self._send_json({"error": "该消息没有可分支的锚点(旧记录或引擎不支持)"}, 400)
            return self._send_json(r)
        if len(seg) == 6 and seg[3] == "compact" and seg[5] == "cancel-unaccepted" and method == "POST":
            if not hasattr(ad, "cancel_unaccepted_compact"):
                return self._send_json({"error":"unsupported"}, 404)
            return self._send_json(ad.cancel_unaccepted_compact(seg[2], seg[4]))
        if len(seg) == 5 and seg[3] == "compact" and method == "GET":
            if not hasattr(ad, "compact_status"):
                return self._send_json({"error":"unsupported"}, 404)
            return self._send_json(ad.compact_status(seg[2], seg[4]))
        if len(seg) == 4 and seg[3] == "compact" and method == "POST":
            if not hasattr(ad, "compact_session"):
                return self._send_json({"error": "当前引擎不支持 GUI 压缩"}, 400)
            # Shepherd compaction can run for minutes. A mobile/proxy HTTP link
            # is not a completion channel: acknowledge admission immediately and
            # let session.compact + /state carry the durable terminal result.
            run = getattr(ad, "compact_session_async", None)
            if run:
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except Exception:
                    return self._send_json({"error": "bad body"}, 400)
                r = run(seg[2], str(body.get("operation_id") or ""))
            else:
                r = ad.compact_session(seg[2])
            return self._send_json(r, 202 if r.get("accepted") else 200 if r.get("ok") else 400)
        if len(seg) == 4 and seg[3] == "rewind" and method == "POST":
            # 回退到某条用户消息之前。dry_run 只预览(回帧带 filesChanged 与增删行数),
            # 界面拿它渲染确认框,用户点实的再发一次不带 dry_run 的。
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send_json({"error": "bad body"}, 400)
            r = ad.rewind(seg[2], body.get("message_id") or "", bool(body.get("dry_run", True)))
            return self._send_json(r, 400 if r.get("error") else 200)
        if len(seg) == 4 and seg[3] == "permission" and method == "POST":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send_json({"error": "bad body"}, 400)
            ok = ad.respond_permission(body.get("id"), bool(body.get("allow")),
                                       body.get("answers") or None,
                                       body.get("response") or "")
            return self._send_json({"ok": ok})
        if len(seg) == 4 and seg[3] in ("prompt_async", "prompt", "message") and method == "POST":
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self.close_connection = True
                return self._send_json({"error": "bad body length"}, 400)
            if length < 0:
                self.close_connection = True
                return self._send_json({"error": "bad body length"}, 400)
            if ad.name == 'shepherd':
                from shepherd.turn_inputs import MAX_PROMPT_BODY_BYTES
                if length > MAX_PROMPT_BODY_BYTES:
                    # Do not consume an unbounded body or reuse its remaining
                    # bytes as the next request line on an HTTP/1.1 connection.
                    self.close_connection = True
                    return self._send_json({"error": "发送请求超过 65 MiB 上限；请减少附件或分轮发送",
                                            "code": "prompt_body_too_large"}, 413)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                parts = body.get("parts") or []
                text = next((x.get("text", "") for x in parts if x.get("type") == "text"), "")
            except Exception:
                return self._send_json({"error": "bad body"}, 400)
            if body.get("client_request_id"):
                from engine_adapters.queue_recovery import request_status
                previous = request_status(ad, seg[2], str(body["client_request_id"]))
                if previous:
                    return self._send_json(previous)
            peer_request_id = str(body.get("peer_request_id") or "")
            requested_engine = str(body.get("engine") or "").strip()
            # A detached watcher from an older Desk can survive code upgrade and
            # repost a late answer without peer_request_id.  Verify its mailbox
            # claim and consume it as a migration event before any ordinary turn
            # admission or persistence.
            if not peer_request_id and _release_legacy_peer_result(seg[2], text):
                return self._send_json({"ok": True, "legacy_peer_result": "mailbox"}, 202)
            block = ad.recall_block(seg[2]) if hasattr(ad, "recall_block") else None
            if block:
                return self._send_json(block, 409)
            # Validate before mailbox acceptance; otherwise an invalid Shepherd
            # target is accepted but never gets a worker or terminal result.
            if ad.name == "shepherd" and requested_engine not in ("", *GUI_ENGINES):
                return self._send_json({"error": "unknown engine"}, 400)
            confirmed_peer = str(body.get("confirm_peer_cancel") or "")  # legacy clients; ignored
            if peer_request_id:
                try:
                    import peer_mailbox
                    request, accepted = peer_mailbox.accept(peer_request_id, seg[2])
                except Exception:
                    request, accepted = None, False
                if not request or not accepted:
                    return self._send_json({"error": "invalid peer request"}, 400)
            try:
                import self_update
                with self_update.admit_work(user_activity=not bool(peer_request_id)) as admitted:
                    if not admitted:
                        if peer_request_id:
                            try:
                                peer_mailbox.update(peer_request_id, "failed",
                                                    error="Desk 正在准备强制更新")
                            except Exception:
                                pass
                        return self._send_json({"error": "Desk 正在准备强制更新，请稍后再发送"}, 409)
                    engine_name = ad.execution_name(seg[2]) if hasattr(ad, "execution_name") else ad.name
                    execution_engine = (ad.session_engine(seg[2])
                                        if hasattr(ad, "execution_options") else engine_name)
                    legacy = _wb_state_read().get("engine_opts", {}).get(execution_engine, {})
                    opts = (ad.execution_options(seg[2], legacy)
                            if hasattr(ad, "execution_options")
                            else ad.session_options(seg[2], legacy))
                    # 附件与正文一起来：图片直接进模型，普通文件落盘后给路径；用户显式从
                    # 原生网页加入的 context 由适配器作为本轮只读材料前置，不改用户气泡正文。
                    atts = [x for x in parts if x.get("type") in ("image", "file", "context")]
                    # 再给一个重取口:这条消息可能排在正在跑的回合后面,而人往往就在那段时间里改
                    # 档位。以开跑那一刻的会话档位为准,不是按下发送那一刻的(见 _run_serial)。
                    refresh_opts = lambda sid=seg[2]: (
                        ad.execution_options(sid, _wb_state_read().get("engine_opts", {}).get(
                            ad.session_engine(sid), {}))
                        if hasattr(ad, "execution_options") else ad.session_options(
                            sid, _wb_state_read().get("engine_opts", {}).get(
                                ad.execution_name(sid) if hasattr(ad, "execution_name") else ad.name, {})))
                    peer_head = next((x.get("peer_head", "") for x in parts
                                      if x.get("type") == "text"), "")
                    if peer_request_id:
                        # ask is a no-tools, text-only sidecar.  Letting it enter the
                        # ordinary prompt path would persist a fake user turn and feed
                        # recap/compaction; attachments or engine retargeting would also
                        # smuggle state across session boundaries.
                        if atts or requested_engine:
                            import peer_mailbox
                            peer_mailbox.update(peer_request_id, "failed",
                                                error="隔离问询只接受文本且不能切换目标引擎")
                            return self._send_json({"error": "invalid peer request payload"}, 400)
                        receipt = ad.peer_prompt_async(
                            seg[2], text, opts, refresh_opts=refresh_opts,
                            peer_request_id=peer_request_id, peer_head=peer_head)
                    else:
                        receipt = ad.prompt_async(seg[2], text, opts, atts,
                                        **({'requested_engine':requested_engine}
                                           if ad.name == 'shepherd' else {}),
                                        **({'client_request_id': str(body['client_request_id'])[:128]}
                                           if body.get('client_request_id') else {}),
                                        confirmed_peer_cancel=confirmed_peer,
                                        refresh_opts=refresh_opts, peer_head=peer_head)
            except Exception as exc:
                if peer_request_id:
                    try:
                        from engine_adapters.recap import _peer_public_error
                        peer_mailbox.update(peer_request_id, "failed",
                                            error=_peer_public_error(exc))
                    except Exception:
                        pass
                block = getattr(exc, "recall_block", None)
                if getattr(exc, "code", None) in ("invalid_image", "invalid_attachment"):
                    return self._send_json({"error": str(exc), "code": exc.code}, 400)
                if getattr(exc, "code", None) in ("turn_input_too_large", "image_transport_too_large"):
                    return self._send_json({"error": str(exc), "code": exc.code}, 413)
                if getattr(exc, "status", None) == 409:
                    error = {"error": str(exc)}
                    if getattr(exc, "code", None):
                        error["code"] = exc.code
                    if getattr(exc, "code", None) in ("delivery_uncertain", "delivery_cleanup"):
                        try:
                            state = ad.turn_state(seg[2]) if hasattr(ad, "turn_state") else None
                            if isinstance(state, dict) and state.get("delivery"):
                                error["delivery"] = state["delivery"]
                                error["state"] = state.get("state")
                                error["generation"] = state.get("generation")
                        except Exception:
                            pass
                    if isinstance(getattr(exc, "peer", None), dict):
                        error["peer"] = exc.peer
                    return self._send_json(error, 409)
                if isinstance(block, dict) and block.get("code") == "recall_pending":
                    return self._send_json(block, 409)
                detail = type(exc).__name__
                if getattr(exc, "code", None):
                    detail += ":" + str(exc.code)[:80]
                return self._send_json({"error": "prompt dispatch failed",
                                        "detail": detail}, 500)
            if isinstance(receipt, dict) and body.get('client_request_id'):
                return self._send_json(receipt, 202)
            self.send_response(204); self.end_headers()
            return
        self._send_json({"error": "unsupported"}, 404)

    def _adapter_sse(self):
        q = _bus.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._sse_prime()
            # 每条新连接先报“连到的是哪个进程、哪版前端”。iOS 把主屏网页冻结在内存里时,
            # 后端已经可能重启甚至换过前端；这帧随 SSE 本身送达，不另加轮询。
            from system.api import runtime_status
            hello = {"type": "server.connected", "properties": {"runtime": runtime_status()}}
            self.wfile.write(f"data: {json.dumps(hello, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=20)
                    self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                except queue.Empty:
                    # 注释帧能保住 TCP，却不会通知 EventSource JS。iOS 的连接可能仍显示 OPEN、
                    # 实际再也收不到字节；具名心跳让前端能判断 45 秒无帧并主动换连接。
                    # 顺带带上前端版本：sync-home 会原地替换 CSS/JS，但不一定重启当前后端，
                    # 只在连接首帧报版本会让已打开的手机页永久停在旧样式。
                    ping = {"runtime": runtime_status()}
                    self.wfile.write(
                        f"event: ping\ndata: {json.dumps(ping, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
        except Exception:
            pass
        finally:
            _bus.unsubscribe(q)
