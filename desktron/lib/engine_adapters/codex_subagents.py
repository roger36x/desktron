# 由 engine_adapters.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)
import json
import os
import re as re_mod
import threading
import time


class CodexSubagentsMixin:

    _rollout_path_cache = {}
    _rollout_dir_cache = {}
    _rollout_path_lock = threading.Lock()

    def _rollout_roots(self):
        # 显式覆盖用于导入/测试；默认目录与侧栏 store 共用同一来源。
        roots = tuple(os.path.expanduser(d) for d in self.ROLLOUT_DIRS)
        defaults = {"codex": ("~/.codex/sessions", "~/.codex/archived_sessions")}
        if tuple(self.ROLLOUT_DIRS) == defaults.get(self.name):
            from engines import get
            configured = roots
            roots = get(self.name).store.session_dirs()
            if self.name == "codex":
                roots += tuple(os.path.join(os.path.dirname(p), "archived_sessions") for p in roots)
                roots += configured[1:]
        paths = {}
        for path in roots:
            paths.setdefault(os.path.realpath(path), path)
        return tuple(paths.values())

    def _rollout_path(self, esid):
        """按会话 id 找 rollout 文件。文件名形如 rollout-<时间>-<会话 id>.jsonl,
        按日期分目录存;归档目录也要找,老会话会被挪过去。"""
        if not re_mod.fullmatch(r"[0-9a-fA-F-]{8,64}", str(esid or "")):
            return ""
        roots = self._rollout_roots()
        if self.name == 'codex':
            from codex_history import indexed_path
            try:
                active = indexed_path(roots, esid)
            except (OSError, ValueError):
                return ''  # An unreadable active branch must not revive an old one.
            if active is not None:
                return active
        key = (roots, esid)
        with self._rollout_path_lock:
            hit = self._rollout_path_cache.get(key)
            directories = self._rollout_dir_cache.get(key, ())
        if hit and directories and os.path.isfile(hit):
            try:
                if all(os.stat(p).st_mtime_ns == stamp for p, stamp in directories):
                    return hit
            except OSError:
                pass
        choices, directories = [], []
        for base in roots:
            for root, _dirs, files in os.walk(base):
                directories.append((root, os.stat(root).st_mtime_ns))
                for fn in files:
                    legacy = fn.startswith('rollout-') and fn.endswith(f'-{esid}.jsonl')
                    segment = (self.name == 'codex' and fn.startswith('rollout-')
                               and f'-{esid}_' in fn and fn.endswith('.jsonl'))
                    if not (legacy or segment):
                        continue
                    path = os.path.join(root, fn)
                    try:
                        from codex_history import head
                        meta = head(path)
                    except (OSError, ValueError):
                        meta = {}
                    if segment and meta.get('id') != esid:
                        continue
                    choices.append((str(meta.get('timestamp') or ''), path))
        path = max(choices)[1] if choices else ''
        with self._rollout_path_lock:
            self._rollout_path_cache[key] = path
            self._rollout_dir_cache[key] = tuple(directories)
        return path

    def _rollout_view(self, path):
        if self.name != 'codex':
            return path
        from codex_history import history_view
        return history_view(path, self._rollout_roots(), os.path.join(self.dir, 'history-views'))

    # ---- 子代理 ----------------------------------------------------------
    # codex 系派子代理的方式与 claude 不同:子代理不是主记录旁边的附属文件,
    # 而是一条独立的 thread,有自己的 rollout。父子关系写在子 rollout 的头行里:
    #   session_meta.payload.source.subagent.thread_spawn.parent_thread_id
    # 同一处还带着派发时给的名字(agent_nickname)与角色(agent_role),正是界面要显示的两样。
    # 所以「这段会话派了哪些子代理」= 扫记录目录,挑出父指着它的那些 rollout。
    _SUB_TTL = 3.0            # 前端每 3 秒问一次,同一拍内复用上次扫描结果
    _sub_cache = {}           # (记录根, esid) -> (时刻, [(路径, 头行)])
    _spawn_cache = {}         # (记录根, esid) -> 已解析偏移与派发表
    _sub_start_cache = {}     # 子 rollout -> 自身任务开始处；此前是 fork 继承的父历史
    _sub_lock = threading.Lock()

    @staticmethod
    def _rollout_head(path):
        """rollout 的头行(session_meta)。只读一行,不碰整份文件。"""
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                return json.loads(f.readline() or "{}")
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _spawn_of(head):
        pay = (head or {}).get("payload") or {}
        if not isinstance(pay, dict):
            return {}
        src = pay.get("source")
        # source 有两种形态:主会话写的是一个字符串(来路,如 "cli"),子会话写的才是对象。
        # 不判类型的话,主会话那一份会在这里抛 AttributeError,而它跑在扫描循环里——
        # 一份主记录就能让整次扫描失败,子代理一个都列不出来。
        src = src.get("subagent") if isinstance(src, dict) else None
        return (src or {}).get("thread_spawn") or {}

    def _subagent_files(self, esid):
        """父会话是 esid 的那些子 rollout。先按文件时刻粗筛再读头行——记录目录里动辄上千份,
        每份都读头行的话一次扫描要几百毫秒,而这是 3 秒一次的轮询。"""
        now = time.time()
        roots = self._rollout_roots()
        key = (roots, esid)
        with self._sub_lock:
            hit = self._sub_cache.get(key)
            if hit and now - hit[0] < self._SUB_TTL:
                return hit[1]
        parent = self._rollout_path(esid)
        if not parent:
            return []
        try:
            floor = os.path.getmtime(parent) - 30 * 86400   # 子代理不会比父会话早太多
        except OSError:
            floor = 0
        out = []
        for base in roots:
            for root, _dirs, files in os.walk(base):
                for fn in files:
                    if not (fn.startswith("rollout-") and fn.endswith(".jsonl")):
                        continue
                    path = os.path.join(root, fn)
                    if path == parent:
                        continue
                    try:
                        if os.path.getmtime(path) < floor:
                            continue
                    except OSError:
                        continue
                    head = self._rollout_head(path)
                    if self._spawn_of(head).get("parent_thread_id") == esid:
                        out.append((path, head))
        out.sort(key=lambda x: ((x[1].get("payload") or {}).get("timestamp") or ""))
        with self._sub_lock:
            self._sub_cache[key] = (now, out)
        return out

    _SUB_FRESH = 90_000       # 记录多久没动就不算「正在写」(毫秒)
    _SPAWN_TOOLS = ("spawn_agent", "Agent", "Task", "spawnAgent")
    _SPAWN_WINDOW_BYTES = 8 * 1024 * 1024

    def _spawn_index(self, esid):
        """父记录里的派发表:子代理 id → 派它出去时给的那句话。

        codex 原生给子代理起的是随机代号(Ohm / Carver / Lagrange),从子记录那侧看不出
        它去做什么;而父记录里 spawn_agent 的调用带着 message、返回带着 agent_id,
        同一个 call_id 把两者串起来。这是唯一能把「代号」还原成「任务」的地方。

        父记录会长到几十 MB,而这是 3 秒一次的轮询。冷读只看末尾窗口,之后从上次偏移
        续读新增行；更早的代理仍可从子记录首条提示回落出任务名,不值得为一个标题反复
        全量解析父记录。"""
        path = self._rollout_path(esid)
        if not path:
            return {}
        try:
            st = os.stat(path)
        except OSError:
            return {}
        roots = self._rollout_roots()
        key = (roots, esid)
        with self._sub_lock:
            hit = self._spawn_cache.get(key)
        if hit and hit.get("path") == path and hit.get("inode") == st.st_ino:
            if hit.get("offset") == st.st_size and hit.get("mtime_ns") == st.st_mtime_ns:
                return hit.get("table") or {}
            append = st.st_size > hit.get("offset", 0)
        else:
            append = False
        if append:
            start = hit["offset"]
            call = dict(hit.get("calls") or {})
            out = dict(hit.get("agents") or {})
        else:
            start = max(0, st.st_size - self._SPAWN_WINDOW_BYTES)
            call, out = {}, {}
        offset = start
        try:
            with open(path, "rb") as f:
                f.seek(start)
                # 冷读窗口可能落在一条 JSON 中间；增量偏移始终在完整行边界上,不能跳。
                if start and not append:
                    f.readline()
                offset = f.tell()
                while True:
                    line_at = f.tell()
                    ln = f.readline()
                    if not ln:
                        break
                    if not ln.endswith(b"\n"):
                        try:
                            json.loads(ln)
                        except ValueError:
                            offset = line_at
                            break
                    offset = f.tell()
                    if b"agent" not in ln and b"Agent" not in ln:
                        continue
                    try:
                        o = json.loads(ln)
                    except ValueError:
                        continue
                    for pay in self._rollout_items(o):
                        cid = pay.get("call_id") or ""
                        if not cid:
                            continue
                        if pay.get("type") in ("function_call", "custom_tool_call") \
                                and str(pay.get("name") or "") in self._SPAWN_TOOLS:
                            args = {}
                            raw = pay.get("arguments") or pay.get("input") or ""
                            if isinstance(raw, str) and raw.strip().startswith("{"):
                                try:
                                    args = json.loads(raw)
                                except ValueError:
                                    args = {}
                            txt = str(args.get("description") or args.get("message")
                                      or args.get("prompt") or "").strip()
                            if txt:
                                call[cid] = txt.splitlines()[0][:120]
                        elif pay.get("type") in ("function_call_output", "custom_tool_call_output"):
                            raw = pay.get("output")
                            if isinstance(raw, str) and raw.strip().startswith("{"):
                                try:
                                    got = json.loads(raw)
                                except ValueError:
                                    got = {}
                                if isinstance(got, dict) and got.get("agent_id"):
                                    out[cid] = got["agent_id"]
        except OSError:
            return {}
        table = {aid: call[cid] for cid, aid in out.items() if call.get(cid)}
        try:
            st = os.stat(path)
        except OSError:
            return table
        with self._sub_lock:
            self._spawn_cache[key] = {
                "path": path, "inode": st.st_ino, "offset": offset,
                "mtime_ns": st.st_mtime_ns, "calls": call, "agents": out, "table": table,
            }
        return table

    def _subagent_task(self, path, head, back=4096):
        """这个子代理被派去做什么。

        昵称有两种:有的引擎写派发时那句 description(「定位 aborted 写入点」),可以直接用;
        codex 原生给的是随机代号(Ohm / Carver / Lagrange),它不说明这个代理去做什么。
        判据取形态:成词成句的留用,单个拉丁词当代号,让位给它收到的第一句提示词的首行——
        那才是派它出来的那句话。提示词也读不到时仍旧显示代号,总比空白强。"""
        spawn = self._spawn_of(head)
        pay = head.get("payload") or {}
        agent_path = str(spawn.get("agent_path") or "").strip().rstrip("/")
        if agent_path:
            # 新版协作协议把稳定任务名放在 /root/<task_name>；message 会加密，父返回也不再
            # 给 agent_id。优先用这份明确元数据，不能再从 fork 进来的首条主会话消息猜标题。
            return agent_path.rsplit("/", 1)[-1]
        name = str(spawn.get("agent_nickname") or pay.get("agent_nickname") or "").strip()
        if name and (re_mod.search(r"[\u4e00-\u9fff\s]", name) or len(name.split()) > 1):
            return name
        try:
            with open(path, "rb") as f:
                start = self._subagent_start(path)
                if start:
                    f.seek(start)
                for _ in range(80):          # 提示词在开头几行,不为一个名字读整份
                    ln = f.readline()
                    if not ln:
                        break
                    try:
                        o = json.loads(ln)
                    except ValueError:
                        continue
                    p2 = o.get("payload") or {}
                    if not isinstance(p2, dict):
                        continue
                    if p2.get("type") in ("user_message", "message") and p2.get("role") == "user":
                        txt = p2.get("message") or self._rollout_text(p2.get("content"))
                        head1 = str(txt or "").strip().splitlines()
                        if head1:
                            return head1[0][:120]
        except OSError:
            pass
        return name   # 提示词读不到时回落代号

    def _subagent_start(self, path):
        """返回子代理自身任务的起点；full-history fork 在它之前复制了整段父会话。

        Codex 用 inter_agent_communication_metadata 标记真正发给子代理的 NEW_TASK。Desk 以前
        复用主会话解析器从文件头读，于是把继承历史也画进子代理面板。起点一旦写下不会移动，
        按路径缓存即可。
        """
        with self._sub_lock:
            if path in self._sub_start_cache:
                return self._sub_start_cache[path]
        start = 0
        try:
            with open(path, "rb") as f:
                for raw in f:
                    if b"inter_agent_communication_metadata" not in raw:
                        continue
                    try:
                        row = json.loads(raw)
                    except ValueError:
                        continue
                    if row.get("type") == "inter_agent_communication_metadata":
                        start = f.tell()
                        break
        except OSError:
            pass
        with self._sub_lock:
            self._sub_start_cache[path] = start
        return start

    def _subagent_tail(self, path, back=65536):
        """子记录的末尾:最新一步、有没有收场、收场时刻。只读末段,不解析整份。"""
        last, done_at = "", 0
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                f.seek(max(0, size - back))
                raw = f.read().decode("utf-8", "ignore")
        except OSError:
            return last, done_at
        for ln in raw.split("\n"):
            ln = ln.strip()
            if not ln.startswith("{"):
                continue
            try:
                o = json.loads(ln)
            except ValueError:
                continue
            pay = o.get("payload") or {}
            kind = pay.get("type")
            if o.get("type") == "event_msg" and kind == "task_complete":
                done_at = int(pay.get("completed_at") or 0) * 1000 or self._iso_ms(o.get("timestamp"))
                last = "已交回"
            elif o.get("type") == "event_msg" and kind == "task_started":
                done_at = 0        # 又开了一轮:上一次的收场不算数
            for item in self._rollout_items(o):
                ikind = item.get("type")
                if ikind in ("function_call", "custom_tool_call"):
                    name = item.get("name") or ""
                    if name:
                        last = "调用 " + name
                elif ikind == "message" and item.get("role") == "assistant":
                    last = "正在输出"
        return last, done_at

    def subagents(self, sid):
        """这一段会话派出去的子代理。字段与 claude 那一份对齐,前端共用同一套呈现。"""
        esid = (self.index.get(sid) or {}).get("engine_sid")
        if not esid:
            return []
        return self._subagents_of(esid, bool(self._live.get(sid)))

    def running_agents(self, esid):
        """这段引擎会话此刻有几个子代理在跑。口径与 claude 那一份一致(见那边的说明):
        入参是引擎会话 uuid,索引里查不到 Desk 会话 id 时,「父回合在跑」那一维不参与。"""
        sid = self._sid_by_esid(esid)
        return sum(1 for a in self._subagents_of(esid, bool(sid and self._live.get(sid)))
                   if a.get("status") == "running")

    def _subagent_display_task(self, spawned, aid, path, head):
        return spawned.get(aid) or self._subagent_task(path, head)

    def _subagents_of(self, esid, live):
        spawned = self._spawn_index(esid)   # 代号 → 派发时那句话
        out = []
        for path, head in self._subagent_files(esid):
            pay = head.get("payload") or {}
            spawn = self._spawn_of(head)
            aid = pay.get("id") or ""
            if not aid:
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            last, done_at = self._subagent_tail(path)
            started = self._iso_ms(pay.get("timestamp")) or int(st.st_mtime * 1000)
            updated = int(st.st_mtime * 1000)
            # 收场判据按引擎写下的事实走:子记录里有 task_complete、其后没有新一轮,就是交回了。
            # 没有收场证据时看它自己的记录还在不在动——不能只看父回合是否在跑:codex 的
            # collab 子代理是异步的,父回合结束之后它照样在写,那时按父回合判会把它误报成
            # 「被带走」。记录也不动了才算收场。
            fresh = int(time.time() * 1000) - updated < self._SUB_FRESH
            if done_at:
                status, outcome, ended = "done", "completed", done_at
            elif fresh or live:
                status, outcome, ended = "running", "", 0
            else:
                status, outcome, ended = "done", "gone", updated
            out.append({"id": aid,
                        "kind": spawn.get("agent_role") or pay.get("agent_role") or "",
                        "tool_use_id": "",
                        "task": self._subagent_display_task(spawned, aid, path, head),
                        "last": last, "started": started, "updated": updated, "size": st.st_size,
                        "status": status, "outcome": outcome, "ended": ended,
                        "ms": max(0, (ended or updated) - started)})
        out.sort(key=lambda x: x["started"])
        return out

    def subagent_log(self, sid, agent_id, limit=200, limit_bytes=None):
        """某个子代理的记录,翻成契约的消息形态。子代理是独立 thread,读它自己的 rollout,
        与主会话共用同一个解析器——两处长得一样,不必各写一套。"""
        esid = (self.index.get(sid) or {}).get("engine_sid")
        if not esid or not re_mod.fullmatch(r"[0-9a-fA-F-]{8,64}", str(agent_id or "")):
            return []
        for path, head in self._subagent_files(esid):
            if ((head.get("payload") or {}).get("id") or "") == agent_id:
                child_start = self._subagent_start(path)
                start = child_start
                try:
                    max_bytes = max(0, int(limit_bytes or 0))
                except (TypeError, ValueError):
                    max_bytes = 0
                if max_bytes:
                    try:
                        start = max(child_start, os.path.getsize(path) - max_bytes)
                    except OSError:
                        return []
                return (self._group_rollout(
                    path, sid, start, exact=bool(start and start == child_start)) or [])[-limit:]
        return []
