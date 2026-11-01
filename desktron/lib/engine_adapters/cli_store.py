# 由 engine_adapters.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)
import copy
import json
import os
import re as re_mod
import shutil
import threading
import time
import uuid

from .bus import clean_session_title
from .execution_returns import is_async_result, settle_stored
from .persist import _ADAPTERS
from .message_index import JsonlTurnIndex


class SessionStoreMixin:

    def __init__(self, data_dir, workspace, bus):
        self.dir = os.path.join(data_dir, "adapter", self.name)
        os.makedirs(self.dir, exist_ok=True)
        self.workspace = workspace
        self.bus = bus
        from performance_telemetry import PerformanceTelemetry
        self.performance = PerformanceTelemetry(data_dir, self.name)
        try:
            self.performance.cleanup(force=True)
        except (OSError, ValueError):
            pass
        self.index_path = os.path.join(self.dir, "sessions.json")
        from durable_json import read
        self.index = read(self.index_path)
        self._lock = threading.Lock()
        self._pending = {}   # 权限询问:request_id → proc
        self._live = {}      # 在跑的回合:sid → ctx,供刷新后补一份未落盘的快照
        self._completed_seq = {}   # 真正发布收尾的回合，供侧栏按轮次确认已读
        self._procs = {}     # 常驻模式:sid → _EngineProc
        self._approvals = {}   # 等人拍板的审批:rid → (连接, 请求 id, method, params)
        self._plock = threading.Lock()
        self._turn_locks = {}   # sid → 回合锁；GUI、桥接等入口共享同一条串行边界
        self._janitor = None
        # 回合的顺序与来路(见 _seq_next)。序号由后端给,界面不再自己猜一条内容属于哪一轮。
        self._turn_seq = {}     # sid → 这一段会话已经发出去的最大序号
        self._turn_prev = {}    # sid → 上一个回合的 {msg_id, seq};自续轮挂在它下面
        # 手动压缩不是普通模型回合，不进 _live；单独留一份可查状态，
        # 手机切后台丢掉收尾事件后，回前台仍能判明它到底结束没有。
        self._compact_state = {}  # sid → {running, started, result?, error?, trigger}
        # 自续轮预告(task_notification)的跨回合暂存:sid → {task, status, at}。
        # 预告未必落在轮次间隙——派出后台子代理的那一轮里,它常先于本轮 result 到达
        # (2026-08-15 实测),而 ctx 随回合生灭,记在 ctx 上出了回合就丢。
        self._aside_hint = {}
        _ADAPTERS.add(self)   # 供 _bg_forget 按 sid 清后台命令台账
        self._bg_tasks = {}           # sid → {后台命令 id: {started, output}};命令活得比回合长,不挂 ctx
        self._msg_cache = {}          # sid → ((dev, ino, mtime_ns, size, window), 序列化好的字节)
        self._msg_cache_lock = threading.Lock()
        self._turn_indexes = {}        # sid → 可重建 JSONL 字节索引；权威数据仍是消息文件
        # recap 是回合外旁路,自己的锁不能借用会话索引锁:生成收尾与新回合取消会并发。
        self._recap_lock = threading.Lock()
        self._recap_timers = {}       # sid → debounce Timer
        self._recap_runs = {}         # sid → 可取消的 _RecapRun
        self._recap_shutdown = threading.Event()  # 终局 recap 超时后封住迟到的 run/进程绑定
        # desk-msg ask 复用只读临时分支，但不属于 recap，也绝不能落入普通回合历史。
        self._peer_runs = {}          # sid → 同时最多一条、准备后与主回合并行的 peer 支线
        self._peer_epoch = uuid.uuid4().hex  # 后端重启后让前端丢弃旧进程的 revision 水位
        self._peer_versions = {}      # sid → 问询可见状态版本；用于丢帧恢复与乱序事件去重
        self._peer_blocks = {}        # sid → 删除等独占操作的并发 fence 计数
        self._recap_snapshots = {}    # sid → 刚完成主回合实际使用的参数快照
        self._recap_done = {}         # sid → 已成功处理(含「无实质进展」)的 generation
        self._recap_state_lock = threading.Lock()  # 多会话可能并发更新同一引擎状态文件
        self._term_seen = set()       # 已给侧栏登记过的引擎 id;登记按回合兜底,别每轮都读写文件
        self._delivery_local = threading.local()
        from .delivery import DeliveryReceipts
        self.delivery = DeliveryReceipts(self.dir) if getattr(self, 'durable_delivery', False) else None
        if self.delivery:
            for sid in list(self.index):
                changed = self.delivery.recover(sid)
                # storing/stored 都可能跨在另一个文件的写入边界。先核验并清除精确的
                # delivery_request_id；只有消息文件可读且目标已不存在，才释放 receipt。
                for row in changed:
                    if row.get('status') == 'cleanup_required':
                        self._finish_delivery_cleanup(sid, row)
                if any(row.get('status') == 'uncertain' for row in changed):
                    self._delivery_autoresolve(sid, publish=False)

    # 引擎报告的真实模型。这个类属性是引擎级的兜底,真正的记录按会话落在 index[sid]["model"]
    # (见 set_session_model)。原来只有这一个字段,而适配器每引擎一个实例(engine.py 按名缓存),
    # 于是它记的是「这个引擎下任意一段会话最近报过的模型」:A 段选了 fable,B 段刚跑完一轮
    # opus,A 的档位钮就显示 opus(2026-09-02 用户实见)。
    last_model = None

    def set_session_model(self, sid, model, since=None):
        """引擎在某段会话里自报了模型:按会话记下来,顺带更新引擎级兜底。
        从读流的线程调进来,自己拿锁;索引条目不存在(临时会话)就只更新兜底。

        since 是报出这条的那个回合的起始时刻(毫秒)。模型是进程启动参数,在跑的回合改不了,
        所以人中途换模型时,那一轮会继续报旧模型——而换模型那一刻刚把这条记录清掉,为的是
        让档位钮先显示人选的那个。不挡的话旧值立刻被写回来,回合结束之后也不会自己消失,
        换一个窗口或刷新一次看到的就是「菜单选着 Opus 5,钮上写着 Fable 5.1」
        (2026-09-04 用户实见)。判据只认「这一轮比那次改动早」,不猜模型名。"""
        model = str(model or "").strip()
        if not model:
            return
        self.last_model = model
        if not sid:
            return
        with self._lock:
            entry = self.index.get(sid)
            if entry is None:
                return
            if since is not None and since < (entry.get("model_cut") or 0):
                return
            if entry.get("model") == model:
                return
            entry["model"] = model
            self._save_index()

    def session_model(self, sid):
        """这一段会话里引擎最近一次自报的模型;没报过(或刚换过模型还没跑)为空。"""
        if not sid:
            return ""
        with self._lock:
            return str((self.index.get(sid) or {}).get("model") or "")

    def style_body(self):
        """表达约束(prompts/style.md),各引擎共用的系统级约束。"""
        try:
            with open(os.path.join(self.workspace, "prompts", "style.md"), encoding="utf-8") as f:
                return f.read(8192).strip()
        except Exception:
            return ""

    @staticmethod
    def _desk_role_name(name):
        """Desk 业务角色名。空值是正常态；非法值与已退役角色都安全降级为空。"""
        name = str(name or "").strip()
        if name == "diagnose" or not re_mod.fullmatch(r"[a-z0-9_-]+", name):
            return ""
        return name

    def system_body(self, opts):
        role = self._desk_role_name((opts or {}).get("desk_role"))
        parts = [self.agent_body(role) if role else "", self.style_body()]
        return "\n\n".join(x for x in parts if x)

    def _agents_dir(self):
        """角色定义的引擎无关正身目录。"""
        return os.path.join(self.workspace, ".desk", "agents")

    def agent_body(self, name):
        """Desk 业务角色正文,渲染给显式选择该角色的会话；文件缺失返回空。"""
        if not re_mod.fullmatch(r"[a-z0-9_-]+", name or ""):
            return ""
        path = os.path.join(self._agents_dir(), name + ".md")
        try:
            with open(path, encoding="utf-8") as f:
                txt = f.read(16384)
        except Exception:
            return ""
        if txt.startswith("---"):
            end = txt.find("\n---", 3)
            if end > 0:
                txt = txt[end + 4:]
        return txt.strip()

    def execution_name(self, sid=None):
        """Public execution engine name; facades may override without becoming an engine."""
        return self.name

    def config_label(self, sid=None):
        """档位钮上显示的模型。有 sid 就按这一段会话自报的来,没有才退到引擎级兜底——
        兜底是给「还没有会话」的场合(引擎页、老测试)留的,不能拿来回答某一段会话的问题。
        没报过时只回引擎名,不拼「默认」:前端会把「· 」后面那一截当模型名显示,拼上
        「默认」它就把人选好的标签盖掉了。"""
        m = self.session_model(sid) if sid else self.last_model
        return f"{self.name} · {m}" if m else self.name

    def agents(self):
        """可选角色:.desk/agents/*.md 的文件名。各引擎共用同一批定义,
        由 agent_body 渲染进系统 prompt。"""
        d = self._agents_dir()
        try:
            names = sorted(x[:-3] for x in os.listdir(d) if x.endswith(".md"))
        except OSError:
            return []
        return [{"name": n} for n in names]

    def respond_permission(self, request_id, allow, answers=None, response=""):
        return False   # 默认引擎无权限协议

    @classmethod
    def available(cls):
        return bool(cls.binary and shutil.which(cls.binary))

    # ---- 契约:会话 ----
    def _finish_delivery_cleanup(self, sid, row):
        request_id = str((row or {}).get('request_id') or '')
        if not request_id:
            return False
        try:
            self._rewrite_msgs(sid, transform=lambda messages: [
                msg for msg in messages
                if (msg.get('info') or {}).get('delivery_request_id') != request_id])
            self._forget_reference_attachments(sid, request_id=request_id)
            self.delivery.finish_cleanup(sid, request_id)
            return True
        except Exception:
            return False

    def _drop_message_id(self, sid, message_id):
        message_id = str(message_id or '')
        if not message_id:
            return False
        try:
            before = self.messages(sid)
            after = [row for row in before if (row.get('info') or {}).get('id') != message_id]
            if len(after) == len(before):
                return False
            self._rewrite_msgs(sid, after)
            self._forget_reference_attachments(sid, message_id=message_id)
            return True
        except Exception:
            return False

    def request_status(self, sid, request_id):
        if not self.delivery:
            return {'request_id':request_id, 'status':'not_found'}
        return self.delivery.get(sid, request_id)

    def cancel_unsent_delivery(self, sid, request_id):
        if not self.delivery:
            return {'request_id':request_id, 'status':'not_found'}
        row = self.delivery.cancel_unsent(sid, request_id)
        if row.get('status') == 'cleanup_required' and row.get('cleanup_from') != 'storing':
            self._finish_delivery_cleanup(sid, row)
            row = self.delivery.get(sid, request_id)
        return row

    def native_writer_lock(self, sid):
        """本会话原生写入进程持有的锁文件；与 _spawn_process / Codex app-server 同一路径。"""
        from owned_process import writer_lock_path
        return writer_lock_path(self.dir, sid)

    def native_turn_outcome(self, sid, receipt):
        """从引擎原生记录判断这一轮是否正常结束；引擎未实现时返回 None。"""
        return None

    def _delivery_autoresolve(self, sid, publish=True, schedule=True):
        """旧写入进程已退出时，按原生证据收口未知回执，返回是否有回执被收口。

        进程仍存活或无法判定时不收口；schedule=True 时转入后台退避重探，
        收口后主动推送 ready，前端无需轮询或用户操作即可放行排队消息。
        """
        if not self.delivery or self._live.get(sid):
            return False
        lock = self.__dict__.setdefault('_autoresolve_lock', threading.Lock())
        tried = self.__dict__.setdefault('_autoresolve_at', {})
        now = time.monotonic()
        if (sid in tried and now - tried[sid] < 1.0) or not lock.acquire(blocking=False):
            return False
        resolved = []
        try:
            tried[sid] = now
            rows = [row for row in self.delivery.unresolved(sid) if row.get('status') == 'uncertain']
            if not rows:
                return False
            from .delivery_recovery import writer_released
            if not writer_released(self.native_writer_lock(sid)):
                if schedule:
                    self._delivery_autoresolve_later(sid)
                return False
            for row in rows:
                try:
                    outcome = self.native_turn_outcome(sid, row)
                except Exception:
                    outcome = None
                evidence = 'native_record' if outcome else 'writer_exited'
                self.delivery.resolve_recovered(
                    sid, row['request_id'], outcome=outcome or 'interrupted',
                    reason='auto_' + evidence + (':' + row['reason'] if row.get('reason') else ''))
                resolved.append((row['request_id'], outcome or 'interrupted', evidence))
        except Exception as exc:
            try:
                import deskcfg
                deskcfg.log(self.name, f"未知回执自动收口失败 {sid}:{str(exc)[:160]}")
            except Exception:
                pass
            return False
        finally:
            lock.release()
        try:
            import deskcfg
            for rid, outcome, evidence in resolved:
                deskcfg.log(self.name, f"未知回执已自动收口 {sid} {rid} outcome={outcome} evidence={evidence}")
        except Exception:
            pass
        if publish:
            label = ('上一轮已正常结束，可以继续' if all(o == 'completed' for _, o, _ in resolved)
                     else '上一轮被中断，可能已执行部分工具；不会自动重发')
            self._env_publish(sid, {'state': 'ready', 'stage': 'delivery', 'label': label})
            state = self.turn_state(sid)
            self.bus.publish({'type': 'session.state', 'properties': {**state, 'sessionID': sid}})
        return bool(resolved)

    def _delivery_autoresolve_later(self, sid):
        """写入进程尚未退出时，后台按退避间隔重探；同一会话只保留一条重探线程。"""
        pending = self.__dict__.setdefault('_autoresolve_pending', set())
        if sid in pending:
            return
        pending.add(sid)

        def run():
            from .delivery_recovery import RETRY_DELAYS
            try:
                for delay in RETRY_DELAYS:
                    time.sleep(delay)
                    if not any(row.get('status') == 'uncertain' for row in self.delivery.unresolved(sid)):
                        return
                    self.__dict__.setdefault('_autoresolve_at', {}).pop(sid, None)
                    if self._delivery_autoresolve(sid, schedule=False):
                        return
                try:
                    import deskcfg
                    deskcfg.log(self.name, f"未知回执仍无法自动收口 {sid}:旧写入进程未确认退出")
                except Exception:
                    pass
            finally:
                pending.discard(sid)

        threading.Thread(target=run, daemon=True, name=f'delivery-autoresolve-{sid}').start()

    def resolve_delivery(self, sid, request_id):
        if not self.delivery:
            return {'request_id':request_id, 'status':'not_found'}
        row = self.delivery.get(sid, request_id)
        if row.get('status') == 'cleanup_required':
            if row.get('cleanup_from') == 'storing':
                from .delivery import DeliveryConflict
                raise DeliveryConflict('用户消息仍在写入；请等待当前取消完成', code='delivery_cleanup')
            if not self._finish_delivery_cleanup(sid, row):
                from .delivery import DeliveryConflict
                raise DeliveryConflict('用户消息投影尚未安全清理', code='delivery_cleanup')
            return self.delivery.get(sid, request_id)
        return self.delivery.resolve_uncertain(sid, request_id)

    def list_sessions(self):
        with self._lock:
            items = [dict(row) for row in self.index.values()]
        from session_names import read_titles, resolve_title
        custom = read_titles(self.workspace, "custom-titles.json")
        automatic = read_titles(self.workspace, "auto-titles.json")
        for row in items:
            row["title"] = resolve_title(row, (row.get("engine_sid"), row.get("id")), custom, automatic)
        items.sort(key=lambda s: -(s.get("time", {}).get("updated") or 0))
        return items

    def session_title(self, sid):
        from session_names import session_title
        # Coordinated workers share the public session identity and its name.
        owner = getattr(self, "owner", self)
        with owner._lock:
            row = dict(owner.index.get(sid) or {})
        return session_title(owner.workspace, sid, row)

    def adopt_domain(self, esid):
        """搬迁进来的会话该归到哪个工作流。

        终端那条路在会话表里按引擎 id 登记过归属,搬迁时把它抄进适配器索引,新建的这行
        才带着归属出生。原来搬迁一律 create_session() 不带参数,索引里那一格恒为空——
        实测 claude 的 42 段会话里只有 1 段有归属,其余都是这么来的;凡是要按会话查工作流
        的地方(输入框上沿那条任务栏)就全落空。
        """
        esid = str(esid or "").strip()
        if not esid:
            return ""
        try:
            from term import _term_meta_read
            m = _term_meta_read().get(esid) or {}
        except Exception:
            return ""
        dom = str(m.get("domain") or "").strip()
        if dom:
            return dom
        refs = [x for x in (m.get("refs") or []) if isinstance(x, str) and x]
        return refs[0] if refs else ""

    def create_session(self, domain="", task=False, opts=None, *, _turn_guard=None):
        """task=True:agentic app 的执行记录会话,不进侧栏 Vibing 列(记录另在任务行里显示)。

        TUI 退场前这一档走 `_term_open(task=True)`,标记落在 term meta 上;GUI 建的会话
        没有 PTY,标记只能从这里带下去——落一份进适配器索引(_gui_unstarted 据它跳过),
        首轮拿到引擎 id 后再随归属一起登记进 meta(_term_persisted 据它跳过)。两处都要有,
        缺一处的表现是执行记录会在侧栏里冒出来一条同名会话。"""
        sid = getattr(self, "SESSION_ID_PREFIX", "adp_") + uuid.uuid4().hex[:14]
        now = int(time.time() * 1000)
        obj = {"id": sid, "title": "", "time": {"created": now, "updated": now}, "domain": domain}
        if task:
            obj["task"] = True
        if isinstance(opts, dict):
            obj["opts"] = self.normalize_options(opts)
        with self._lock:
            if _turn_guard is not None:
                self._turn_locks[sid] = _turn_guard
            self.index[sid] = obj
            self._save_index()
        return obj

    def session_domain(self, sid):
        return (self.index.get(sid) or {}).get("domain") or ""

    def _session_roots(self, sid, opts):
        """Return explicit writable roots registered for this conversation.

        The domain directory remains the primary cwd. runtime_roots and the
        workflow repository registry are additional task-owned roots; native
        engines may express them as sandbox allowlists or workspace roots.
        """
        out = [str(value) for value in ((opts or {}).get("runtime_roots") or [])
               if str(value)]
        dom = self.session_domain(sid)
        if dom:
            try:
                from domains import _repo_items
                for row in _repo_items(dom.split(":", 1)[0]):
                    directory = row.get("dir")
                    if directory and directory not in out:
                        out.append(directory)
            except Exception:
                pass
        return out

    def move_session_domain(self, sid, domain):
        """更新适配器索引里的会话归属；供没有终端 meta 的 GUI 会话使用。"""
        with self._lock:
            row = self.index.get(sid)
            if not isinstance(row, dict):
                return False, "会话不存在"
            if row.get("task"):
                return False, "任务执行记录不可移动"
            if self.name == "shell" or row.get("engine") == "shell":
                return False, "临时终端不可移动"
            if row.get("domain") != domain:
                row["domain"] = domain
                self._save_index()
        return True, ""

    def grounding_domain(self, sid):
        """构造 grounding 用哪个工作流。

        `domain` 是归属,`refs` 是引用。界面把被引用的会话显示在工作流下面,而归属仍是
        `_desktop`,于是这类会话按归属算就什么 grounding 都没有(2026-08-21 实测:本会话
        `{"refs": ["desk-iter"], "domain": "_desktop"}`,合成正文一段都没进上下文)。
        cwd 不动——它牵着写路径与 transcript 归位;只在构造注入时认引用。
        多个引用取最后一个:refs 按绑定顺序追加,最后一个即最近绑定的。带时间戳的排序
        要先给 refs 升成带 ts 的结构,是后续的事。
        """
        dom = self.session_domain(sid)
        if dom and dom != "_desktop":
            return dom
        try:
            from term import _term_meta_read
            meta = _term_meta_read()
            esid = (self.index.get(sid) or {}).get("engine_sid") or sid
            m = meta.get(esid) or meta.get(sid) or {}
            refs = [x for x in (m.get("refs") or []) if isinstance(x, str) and x]
            if refs:
                return refs[-1]
        except Exception:
            pass
        return dom

    def grounding_engine(self, sid):
        """grounding 按哪种执行方式裁剪技能索引(见 grounding/builder.py)。普通引擎就是自身。"""
        return self.name

    def grounding(self, sid, first_turn):
        """{"constant": 每回合, "scene": 仅首轮}。构造失败一律给空,不因此挡住回合。"""
        try:
            from grounding import build
            g = build(self.workspace, self.grounding_domain(sid),
                      first_turn=first_turn, engine=self.grounding_engine(sid))
            if (g.get("constant") or "").strip():
                # 注入成功即刷新标记:合成 AGENTS.md 那条老路据它自动退休(domains._legacy_agents_md_off)。
                # 不设人工开关——注入自己证明自己可用;坏掉时标记过期,老路自动恢复。
                try:
                    from domains import mark_grounding_injected
                    mark_grounding_injected()
                except Exception:
                    pass
            return g
        except Exception as e:
            try:
                import deskcfg   # 一直缺这个 import:这条错误路径原来一触发就 NameError
                deskcfg.log("grounding", f"构造失败:{str(e)[:120]}")
            except Exception:
                pass
            return {"constant": "", "scene": ""}

    def session_cwd(self, sid):
        """这段会话跑在哪个目录。

        工作流有自己的域目录(workspace/domains/<id>):合成的 AGENTS.md(职责说明、记忆索引、
        skill 索引)、软链进来的共享资产、挂载的 imports 都在那里,引擎原生就读得到。
        TUI 那条路一直是这么起的(cwd / -C);GUI 这条路在 2026-08-20 之前恒用 workspace 根,
        TUI 退场后必须补上——否则所有会话都丢掉工作流的 grounding,只剩一个通用的根目录。
        没有归属(或域目录建不出来)的会话仍落 workspace 根。
        """
        dom = self.session_domain(sid)
        if not dom:
            return self.workspace
        try:
            from domains import _ensure_domain_dir
            d = _ensure_domain_dir(dom)
        except Exception:
            return self.workspace
        return d if d and os.path.isdir(d) else self.workspace

    def session_options(self, sid, fallback=None):
        """当前会话的运行选项；老会话未保存时才沿用旧的引擎全局默认。"""
        with self._lock:
            entry = self.index.get(sid) or {}
            saved = entry.get("opts")
            inherited = dict(fallback or {})
            # desk_role 是会话/工作流语义,从不属于引擎全局默认。旧运行态即使曾
            # 误存过也在读取边界丢弃,否则 desktop 新会话会被上一工作流污染。
            inherited.pop("desk_role", None)
            if isinstance(saved, dict):
                # 存量会话可能只落过一部分字段。全局默认只补缺口，会话已经保存的键
                # （包括空 model）永远优先；这样无需另存一枚会泄漏、会丢失的 touched 标记。
                raw = inherited
                raw.update(saved)
            else:
                raw = inherited
        out = self.normalize_options(raw)
        # desk_role 是工作流语义,不是引擎全局默认。会话显式写过(包括空串)时以会话为准;
        # 没写过才继承所属工作流。desktop 与没有 desk_role 的工作流自然保持无角色。
        if "desk_role" not in raw and "desk_role" not in out:
            try:
                from domains import _domain_entry
                dom = self.session_domain(sid).split(":", 1)[0]
                role = self._desk_role_name((_domain_entry(dom) or {}).get("desk_role"))
            except Exception:
                role = ""
            if role:
                out["desk_role"] = role
        return out

    def normalize_options(self, opts):
        """适配器可覆写的会话选项白名单。

        存量 `agent` 是已退役的引擎专属选项；通用层只认识可选的 Desk 业务角色
        `desk_role`,避免 Claude/Codex 把旧配置误当角色正文。
        """
        out = {k: v for k, v in (opts or {}).items()
               if k != "agent" and isinstance(k, str)
               and isinstance(v, (str, int, float, bool, type(None)))}
        if "desk_role" in out:
            out["desk_role"] = self._desk_role_name(out["desk_role"])
        return out

    def update_session_options(self, sid, patch, fallback=None):
        with self._lock:
            entry = self.index.get(sid)
            if entry is None:
                return None
            current = entry.get("opts")
            if isinstance(current, dict):
                # 第一次触碰存量的部分 opts 时顺手把当下全局默认补成完整快照；当前会话
                # 已经写过的键仍优先，后续其它会话改 engine_opts 不会再反向污染它。
                merged = dict(fallback or {})
                merged.pop("desk_role", None)
                merged.update(current)
            else:
                merged = dict(fallback or {})
                merged.pop("desk_role", None)
            merged.update(patch or {})
            entry["opts"] = self.normalize_options(merged)
            # 人刚换了模型:引擎上一次自报的那个已经不作数,清掉,让档位钮先显示人选的
            # 标签,等引擎在下一回合自报了再换成真实版本。不清的话前端会优先显示旧值。
            if "model" in (patch or {}):
                entry.pop("model", None)
                # 划一条时刻线:比它早开的回合再报模型也不作数(见 set_session_model)。
                entry["model_cut"] = int(time.time() * 1000)
            self._save_index()
            out = dict(entry["opts"])
        # 在锁外算:config_label(sid) 经 session_model 要再拿这把锁,而它不可重入——
        # 写在 with 块里会把自己锁死(第一版就是这么挂住的,2026-09-02)。
        label = self.config_label(sid)
        # 广播给其余客户端。原来只把新值回给发起那一端,别处停在自己打开页面时读到的
        # 那份——同一段会话在 mac 上改成 max,手机上还显示 high,直到把会话页关掉重开
        # (2026-08-25 用户实见)。档位是会话的属性,不是某个页面的本地状态。
        # 发在锁外:publish 会走到订阅者,持锁调外部代码是自找死锁。
        try:
            # 顺带把这一段会话此刻该显示的模型带上:别的客户端收到后要能就地更新档位钮,
            # 那条路上没有 /config 可取(见 test_session_settings_broadcast)。
            self.bus.publish({"type": "session.settings",
                              "properties": {"sessionID": sid, "opts": dict(out), "model": label}})
        except Exception:
            pass
        if "model" in (patch or {}):
            self.refresh_environment(sid)
        return out

    def session_environment(self, sid):
        """需要跨刷新保留的引擎环境异常；正常启动态只走 SSE，不写索引。"""
        return self.environment_snapshot(sid)['environment']

    def environment_snapshot(self, sid):
        from error_notice import visible
        with self._lock:
            row = self.index.get(sid) or {}
            return {'environment':copy.deepcopy(visible(row, self.name)),
                    'revision':int(row.get('environment_revision') or 0)}

    def _pending_ask(self, sid):
        """这一段会话里还等着人回答的权限请求。默认没有——只有走 control_request
        那条路的引擎(claude)才有,各自覆盖。"""
        return []

    @staticmethod
    def _retry_view(retry):
        if not isinstance(retry, dict):
            return None
        out = copy.deepcopy(retry)
        try:
            deadline = int(out.get("deadline_at") or 0)
        except (TypeError, ValueError):
            deadline = 0
        out["remaining_ms"] = max(0, deadline - int(time.time() * 1000)) if deadline else max(
            0, int(out.get("delay_ms") or 0))
        return out

    def turn_state(self, sid):
        """Desk 控制面的回合状态；与引擎自己的事件名和传输方式无关。"""
        ctx = self._live.get(sid)
        if not ctx:
            out = {"state": self.TURN_READY,
                   "generation": int(self._turn_seq.get(sid) or 0),
                   "contract": 1}
        else:
            state = self.TURN_CANCELLING if ctx.get("aborted") else self.TURN_RUNNING
            out = {"state": state, "generation": int(ctx.get("seq") or 0), "contract": 1}
            if ctx.get("interrupt_id"):
                out["interruptID"] = ctx["interrupt_id"]
        if self.delivery:
            unresolved = self.delivery.unresolved(sid)
            uncertain = next((row for row in unresolved
                              if row.get('status') in ('cleanup_required', 'dispatched', 'uncertain')), None)
            if (uncertain and not ctx and uncertain.get('status') == 'uncertain'
                    and self._delivery_autoresolve(sid)):
                unresolved = self.delivery.unresolved(sid)
                uncertain = next((row for row in unresolved
                                  if row.get('status') in ('cleanup_required', 'dispatched', 'uncertain')), None)
            if uncertain and not ctx:
                out.update(state='blocked', delivery=uncertain)
        out["completed_generation"] = getattr(self, "_completed_seq", {}).get(sid, 0)
        peer_state = getattr(self, "peer_state", None)
        if peer_state is not None:
            out["peer"] = peer_state(sid)
        # 还等着人答的权限请求。permission.asked 只在发生那一刻推一次,服务端不重放——
        # 手机切后台、锁屏、蜂窝与 Wi-Fi 切换都会断开 SSE,那一帧就此丢失,选项卡再也不
        # 出现,而回合还在等(2026-09-03 用户实见)。挂进 /state:前端恢复时本来就查这一趟,
        # 不必另加端点与轮询。审批面那条走的是轮询待批,所以不受影响——同一个道理。
        pend = self._pending_ask(sid)
        out["asks"] = pend
        out["activity"] = self.turn_activity(sid, ctx, pend)
        compact = self._compact_state.get(sid)
        if compact:
            out["compact"] = dict(compact)
        compact_t0 = int((ctx or {}).get("compact_t0") or
                         ((compact or {}).get("started") if (compact or {}).get("running") else 0) or 0)
        if compact_t0:
            out["compacting"] = compact_t0
        if ctx:
            from .lifecycle import snapshot
            out.update(snapshot(self, ctx))
        return out

    def turn_activity(self, sid, ctx=None, asks=None):
        """可查事实，不把无输出猜成断网；工具运行和审批等待各有明确对象。"""
        ctx = ctx if ctx is not None else self._live.get(sid)
        if not ctx:
            return {"kind": "idle", "seconds": 0}
        now = time.time()
        progress = ctx.get("_progress_at", ctx.get("_line_at", now))
        # Request boundaries and transport heartbeats do not alter this clock.
        gap = max(0, int(now - float(progress)))
        out = {"kind": "working", "seconds": gap,
               "last_event_at": int(ctx.get("_line_at", now) * 1000),
               "last_progress_at": int(progress * 1000)}
        parts = list((ctx.get("parts") or {}).values())
        running = [p for p in parts if p.get("type") == "tool"
                   and (p.get("state") or {}).get("status") == "running"]
        failed = [p for p in parts if p.get("type") == "tool"
                  and (p.get("state") or {}).get("status") == "error"]
        if failed:
            p = failed[-1]
            out["last_failure"] = {"call_id": p.get("call_id"), "tool": p.get("tool"),
                                   **((p.get("state") or {}).get("failure") or {})}
        asks = self._pending_ask(sid) if asks is None else asks
        if ctx.get("aborted"):
            out["kind"] = "cancelling"
        elif asks:
            out.update(kind="approval", pending=len(asks))
        elif ctx.get("_provider_queue"):
            queue = ctx["_provider_queue"]
            out.update(kind="provider_queue", position=queue.get("position"))
        elif ctx.get("compact_t0") or (self._compact_state.get(sid) or {}).get("running"):
            out["kind"] = "compacting"
        elif ctx.get("_native_recovery"):
            out.update(kind="recovering", operation=ctx["_native_recovery"])
        elif ctx.get("_runner_stall"):
            stall = ctx["_runner_stall"]
            phase = stall.get("phase") if stall.get("phase") == "first_response" else ""
            out.update(kind="no_progress",
                       seconds=max(0, int(now - float(stall.get("since") or now))),
                       running_tools=max(len(running), int(stall.get("active_tools") or 0)),
                       soft=True, **({"phase":phase} if phase else {}))
        elif gap >= self.STALL_SECONDS:
            out.update(kind="no_progress", running_tools=len(running), soft=True)
        elif running:
            out.update(kind="tool", running_tools=len(running))
        elif ctx.get("_provider_wait"):
            wait = ctx["_provider_wait"]
            seconds = max(0, int(wait.get("elapsed_ms") or 0) // 1000)
            if seconds >= getattr(self, "PROVIDER_WAIT_VISIBLE_SECONDS", 60):
                out.update(kind="provider_wait", seconds=seconds,
                           remaining_ms=max(0, int(wait.get("remaining_ms") or 0)))
        return out

    def _record_turn_completion(self, sid, ctx, state):
        # generation 在准备输入时就分配，_live 此时尚未登记；ready + generation
        # 因而不等于本轮已完成。只在真正发布收尾边界时推进完成水位。
        if state == self.TURN_READY:
            if not hasattr(self, "_completed_seq"):
                self._completed_seq = {}
            self._completed_seq[sid] = max(self._completed_seq.get(sid, 0), int((ctx or {}).get("seq") or 0))

    def _turn_state_publish(self, sid, ctx, state):
        self._record_turn_completion(sid, ctx, state)
        props = {"sessionID": sid, "state": state,
                 "generation": int((ctx or {}).get("seq") or 0), "contract": 1}
        if (ctx or {}).get("interrupt_id"):
            props["interruptID"] = ctx["interrupt_id"]
        if (ctx or {}).get("turn_failed"):
            props["failed"] = True
            failure = (ctx or {}).get("engine_failure")
            if isinstance(failure, dict):
                props["error"] = copy.deepcopy(failure)
            elif (ctx or {}).get("engine_error"):
                props["error"] = {"message": str(ctx["engine_error"])[:300]}
            if isinstance((ctx or {}).get("retry"), dict):
                props["retry"] = copy.deepcopy(ctx["retry"])
        self.bus.publish({"type": "session.state", "properties": props})

    @staticmethod
    def _notice_head(note, limit=120):
        """系统信息那一条的摘要:取首行,过长时在词边界断开并补省略号。

        原来是硬切 `[:120]`。claude 的 529 那句正好在第 120 个字符落到「check」后面,
        界面上读到的是「…If it persists, check」——像话没说完,而不是被截断
        (2026-08-19 用户实见)。省略号是给人看的信号:后面还有,点开箭头。
        词边界离切点太远时(中文、长串标识符)照原位切,不为了对齐词而丢掉半行内容。
        """
        line = (str(note or "").splitlines() or [""])[0].strip()
        if len(line) <= limit:
            return line
        cut = line[:limit]
        sp = cut.rfind(" ")
        if sp >= limit - 24:
            cut = cut[:sp]
        return cut.rstrip(" ,.;:，。;:—-") + "…"

    def _env_publish(self, sid, payload):
        """引擎级的状态送到输入框上方那一条,并按需跨刷新保留。

        过程顶行只讲「这一轮停了」，结构化错误由失败回合的过程详情承载。证书校验不过、
        没登录、被限流等仍需处理的问题同时留在这里，并可附带恢复动作。
        """
        with self._lock:
            payload = self._env_project_locked(sid, payload)
        self.bus.publish({"type": "session.environment",
                          "properties": {"sessionID": sid, **payload}})

    def _env_project_locked(self, sid, payload):
        """Shared projection for callers already holding the index lock."""
        from error_notice import prepare, project, current_source
        payload = dict(payload or {})
        if payload.get('stage') == 'auth' and not payload.get('engine'):
            source_row = self.index.get(sid) or {}
            if self.name != 'shepherd':
                source_row = dict(source_row, selected_engine=self.name)
            engine, provider = current_source(source_row, self.name)
            payload.update(engine=engine, provider=provider)
        payload = prepare(sid, payload)
        row = self.index.get(sid)
        if row is not None:
            payload = project(row, payload, self.name)
            self._save_index()
        return payload

    def _ctx_env_publish(self, ctx, payload):
        live = self._live.get(ctx['sid'])
        if (live is not None and live is not ctx) or ctx.get('_lifecycle_closed'):
            return
        from error_notice import prepare
        payload = dict(payload, **self._notice_source(ctx['sid'], ctx), startedAt=int(ctx.get('t0') or 0))
        failure = payload.get('error') or ctx.get('engine_failure') or {}
        if payload.get('state') == 'error' and failure.get('code') == 'AUTH':
            payload.update(stage='auth', scope='environment')
            payload.setdefault('action', {'kind':'terminal', 'label':'重新登录'})
        self._env_publish(ctx['sid'], prepare(ctx['sid'], payload, int(ctx.get('seq') or 0)))

    def _notice_source(self, sid, ctx=None):
        ctx = ctx or {}
        engine = ctx.get('producer_engine') or self.name
        if engine == 'shepherd':
            engine = self.execution_name(sid)
        opts = ctx.get('opts') or self.session_options(sid)
        model = str(opts.get('model') or '')
        provider = ctx.get('_notice_provider') or (model.split('/', 1)[0]
                    if engine == 'pi' and '/' in model else engine)
        return {'engine': engine, 'provider': provider}

    def resolve_auth(self, source, through):
        """Clear only older failures from the verified source, across this store."""
        from error_notice import issue_key
        frames = []
        with self._lock:
            for sid, row in self.index.items():
                issues = row.get('environment_issues') or {}
                if not issues and row.get('environment'):
                    issues = {issue_key(row['environment']): row['environment']}
                matching = [p for p in issues.values() if p.get('stage') == 'auth' and
                    ((p.get('engine'), p.get('provider')) == (source['engine'], source['provider']) or
                     (not p.get('engine') and not row.get('shepherd_mode') and self.name == source['engine']))]
                if not matching:
                    key = issue_key({'stage':'auth', **source})
                    resolved = row.setdefault('environment_resolved', {})
                    resolved[key] = max(int(resolved.get(key, 0)), int(through))
                    continue
                # Upgrade a single-engine legacy notice only; never infer a mixed-engine origin.
                for prior in matching:
                    if not prior.get('engine'):
                        row.setdefault('environment_issues', dict(issues)).pop(issue_key(prior), None)
                frames.append((sid, self._env_project_locked(sid, {
                    'state':'ready', 'stage':'auth', 'label':'登录状态已恢复',
                    **source, 'resolvedThrough':int(through)})))
            self._save_index()
        for sid, payload in frames:
            self.bus.publish({'type':'session.environment', 'properties':{'sessionID':sid, **payload}})

    def refresh_environment(self, sid):
        """Republish the selected source after engine/provider changes."""
        self._env_publish(sid, {'state':'ready', 'stage':'environment', 'clear':True, 'label':''})

    def _publish_turn_error(self, sid, ctx, error):
        live = self._live.get(sid)
        if live is not None and live is not ctx:
            return
        self.bus.publish({'type': 'session.error', 'properties': {
            'sessionID': sid, 'generation': int(ctx.get('seq') or 0),
            'messageID': ctx.get('msg_id'), 'contract': 1,
            'issueID': f'turn:{sid}:{int(ctx.get("seq") or 0)}',
            'environment': self.session_environment(sid),
            **self._notice_source(sid, ctx),
            **({'retry':copy.deepcopy(ctx['retry'])} if isinstance(ctx.get('retry'), dict) else {}),
            'error': copy.deepcopy(error)}})

    def _turn_error_environment(self, sid, error):
        """将通用回合错误持久化到底部提示；不覆盖更具体的恢复提示与动作。"""
        error = dict(error or {})
        message = str(error.get("message") or "会话出错").strip() or "会话出错"
        head = self._notice_head(message)
        payload = {"state": "error", "stage": "turn", "label": head,
                   "generation": self._turn_seq.get(sid, 0), "details": [message], "error":error}
        ctx = self._live.get(sid)
        if ctx:
            payload.update(self._notice_source(sid, ctx), startedAt=int(ctx.get('t0') or 0))
        if error.get('code') == 'AUTH':
            payload.update(stage='auth', scope='environment', action={'kind':'terminal','label':'重新登录'})
        self._env_publish(sid, payload)

    def _clear_turn_error_environment(self, sid):
        """新回合开始时撤掉上一轮通用错误；专用环境故障继续保留。"""
        with self._lock:
            row = self.index.get(sid) or {}
            issues = list((row.get('environment_issues') or {}).values())
            if not issues and row.get('environment'):
                issues = [row['environment']]
        for issue in issues:
            if issue.get('scope') == 'turn' or issue.get('stage') == 'turn':
                self._env_publish(sid, {'state':'ready', 'stage':issue['stage'],
                    'scope':'turn', 'generation':issue.get('generation', 0), 'label':'继续执行'})

    def env_notice(self):
        """开会话前就该说的引擎状态,经 /config 带给界面,渲染成输入框上方那一条。
        与 session_environment 的区别:那一条是某段会话跑出来的异常,这一条与会话无关
        ——「没登录」在第一个回合之前就是真的,不该等到跑失败了才说。
        帧的形状与 SSE 的 session.environment 一致(见 app-chat.js 的 hsEnvironment)。"""
        return None

    def ui_contract(self):
        """界面按这份声明渲染档位,不按引擎名分流。每个 options_catalog 都要带上它。"""
        return {"modeKey": self.UI_MODE_KEY, "modeDefault": self.UI_MODE_DEFAULT}

    def options_catalog(self):
        return {"ui": self.ui_contract()}

    def own_sid(self, sid):
        """把引擎侧的锚点换成工作台自己的会话号(adp_);传进来的本来就是会话号则原样返回。

        侧栏休眠那一档摆的是引擎侧 id(claude 的 uuid / codex 家族的 thread id),
        而索引按会话号建键。不换一次的话,按那个 id 发过来的删除只是空转,
        记录留在盘上,而侧栏那一行已经消失——人以为删掉了。"""
        with self._lock:
            if sid in self.index:
                return sid
            for k, rec in (self.index or {}).items():
                if isinstance(rec, dict) and rec.get("engine_sid") == sid:
                    return k
        return sid

    def delete_session(self, sid):
        sid = self.own_sid(sid)
        self._peer_fence_enter(sid)
        try:
            # Cancel first without owning turn_guard: a queued ask may need that
            # guard once to observe cancellation and publish its terminal state.
            peer_run = self._peer_cancel(
                sid, reason="目标会话正在删除，隔离问询已取消")
            if peer_run is not None and not peer_run.finished.wait(3):
                raise RuntimeError('会话仍有协作问询正在退出，不能删除')
            with self._lock:
                guard = self._turn_locks.setdefault(sid, threading.Lock())
            with guard:
                return self._delete_session_peer_fenced_base(sid)
        finally:
            self._peer_fence_exit(sid)

    def _delete_session_peer_fenced_base(self, sid):
        # 会话号可从 UI 枚举；真正的 App Control 身份是仅注入该 Agent 进程的随机能力。
        # 删除时先撤销，旧子进程即使尚未退出也不能继续操作这条会话。
        try:
            from app_control.capability import revoke as revoke_app_control_capability
            revoke_app_control_capability(sid)
        except Exception:
            pass
        self._recap_cancel(sid)
        peer_run = self._peer_cancel(sid, reason="目标会话正在删除，隔离问询已取消")
        if peer_run is not None and not peer_run.finished.wait(3):
            raise RuntimeError('会话仍有协作问询正在退出，不能删除')
        self._recap_request_clear(sid)
        with self._lock:
            self.index.pop(sid, None)
            self._compact_state.pop(sid, None)
            self._save_index()
        delivery_path = self.delivery.path(sid) if self.delivery else ''
        msg_path = self._msg_path(sid)
        for path in (msg_path, msg_path + ".turn-index.jsonl",
                     msg_path + ".turn-index.json",  # v1 sidecar cleanup
                     msg_path + ".peer-heads.json", delivery_path):
            if not path:
                continue
            try:
                os.remove(path)
            except OSError:
                pass
        self.performance.delete(sid)
        try:
            shutil.rmtree(os.path.join(self.dir, 'message_queue', sid), ignore_errors=True)
            from usage_stats import delete_session
            delete_session(os.path.dirname(os.path.dirname(self.dir)), sid)
        except Exception:
            pass

    # ---- 后台命令台账 ----
    # `Bash(run_in_background)` 起的命令跑在回合之外:回合早就收尾了,它还在跑,跑完由
    # harness 注入一条 task-notification。界面上此前一个字都没有——2026-08-20 用户实见
    # 两条命令跑了 120 秒,期间只能靠问「子代理返回了吗」来确认,而它们根本不是子代理。
    #
    # 认它的依据不是 system/task_started(那类帧我们手上没有帧体,分不出后台命令、子代理
    # 还是别的什么),而是 Bash 工具自己的回执原文:
    #   Command running in background with ID: b80tqyx2a. Output is being written to: …
    # 这一句是照本机 transcript 抄的,不是照文档编的。按它认,认出来的一定是后台命令。
    #
    # 台账挂在适配器上不挂 ctx:命令活得比回合长,记在 ctx 上出了回合就没了。
    _BG_START = re_mod.compile(r"Command running in background with ID:\s*(\S+?)[\s.]")
    _BG_OUTPUT = re_mod.compile(r"Output is being written to:\s*(\S+?\.output)")

    def _bg_note(self, sid, text):
        """工具回执里认出一条刚起的后台命令。"""
        raw = str(text or "")[:1000]
        m = self._BG_START.search(raw)
        if not m:
            return
        tid = m.group(1)
        out = ""
        om = self._BG_OUTPUT.search(raw)
        if om:
            out = om.group(1)
        with self._lock:
            tasks = self._bg_tasks.setdefault(sid, {})
            if tid in tasks:
                return
            tasks[tid] = {"started": int(time.time() * 1000), "output": out}
        self._bg_publish(sid)

    def _bg_done(self, sid, task_id):
        """通知到了就销号。id 在有些形态下带 `bash:` 之类的前缀,取冒号后那一段(同 _aside_agent)。"""
        tid = str(task_id or "").strip().rsplit(":", 1)[-1]
        if not tid:
            return
        with self._lock:
            if not self._bg_tasks.get(sid, {}).pop(tid, None):
                return
        self._bg_publish(sid)

    def _bg_clear(self, sid):
        """清掉这段会话的整本台账。空的就不发,免得四个适配器各广播一遍。"""
        with self._lock:
            if not self._bg_tasks.pop(sid, None):
                return
        self._bg_publish(sid)

    @staticmethod
    def _bg_output_finished(path):
        """Claude 后台 Bash 完成后会在 output 文件尾部写终止标记；没有标记就不猜。"""
        if not path:
            return False
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 512))
                tail = f.read()
        except OSError:
            return False
        return b"[exited with code " in tail or b"[killed]" in tail

    def _bg_reconcile(self, sid):
        with self._lock:
            snapshot = dict(self._bg_tasks.get(sid) or {})
        done = []
        for tid, row in snapshot.items():
            out = row.get("output") if isinstance(row, dict) else ""
            if self._bg_output_finished(out):
                done.append(tid)
        if not done:
            return False
        changed = False
        with self._lock:
            tasks = self._bg_tasks.get(sid) or {}
            for tid in done:
                row = tasks.get(tid)
                out = row.get("output") if isinstance(row, dict) else ""
                if self._bg_output_finished(out):
                    tasks.pop(tid, None)
                    changed = True
            if not tasks:
                self._bg_tasks.pop(sid, None)
        return changed

    def _bg_publish(self, sid):
        self.bus.publish({"type": "session.bgtasks",
                          "properties": {"sessionID": sid, **self.bg_tasks(sid)}})

    def bg_tasks(self, sid):
        """这一段会话此刻有几条后台命令在跑。刷新之后经 /config 拿回来,不然一刷就归零。"""
        self._bg_reconcile(sid)
        with self._lock:
            rows = dict(self._bg_tasks.get(sid) or {})
        started = []
        for row in rows.values():
            if isinstance(row, dict):
                started.append(row.get("started") or 0)
            else:  # 兼容旧测试/旧内存形态
                started.append(row)
        return {"running": len(rows), "started": sorted(started)}

    # ---- 只读那条路的缓存 ----
    # `GET /session/<id>/message` 每次都把整份 jsonl 读出来、逐行 json.loads、再整个
    # json.dumps 回去。同一段会话反复打开,这一遍遍做的是同一件事,而且全程持 GIL——
    # 2026-08-20 实测:一个重请求在传时,轻请求(/session)从 4.6ms 涨到 75.9ms,放大 16.5 倍。
    # 缓存的是 bytes 不是对象:messages() 的返回值有人就地改(撤回会 pop 掉最后一条,
    # pi 那侧会修旧投影里的图片标记),共享一份可变结构迟早被改脏。
    _MSG_CACHE_MAX = 8      # 按会话留几份。0.9MB 一份,留 8 份约 7MB,够覆盖来回切的那几段

    #: 列表里每条工具输出最多带多少字符。存储里仍是全文,展开那一步按需取回(见 part())。
    #: 2026-08-24 实测一段 codex 会话:606 条工具输出、合计 2.0MB,占整个返回体的 99%,
    #: 而其中超过四分之一顶满 8000 的存储上限——p75 就是 8000。界面上这些输出默认是折叠的,
    #: 整份送过去等于让浏览器为看不见的内容建 2MB 的对象。截到 2000 省 62%。
    #: 不截更狠是因为多数输出本来就不长(p50 只有 1925),截太短反而让常见情形多跑一趟。
    LIST_OUTPUT_CAP = 2000
    # 思考块默认折叠着,整份送过去是白送。2026-09-04 拆一段 claude 会话(662KB):思考 166KB,
    # 143 块 p50 727 字、p90 2887 字;截到 600 字剩 66KB。展开那一刻同工具输出一样按 /part 取回。
    LIST_REASON_CAP = 600

    @classmethod
    def _trim_input(cls, inp):
        """入参里过长的字符串也截断,返回(新入参, 原长)。没有超长的返回 (None, 0)。

        原来只截输出。而 2026-08-25 拆一段真实会话的返回体发现:工具那一类占 69%,
        其中**入参 41% 比输出 22% 还大**——入参是命令与内联脚本,长命令、heredoc、
        整段 python 都在里面。只截输出等于把大头漏了。

        按值截而不是按字段名截:各工具的入参形状不同(bash 是 command、Agent 是 prompt、
        编辑类是 content/new_string),列字段名迟早漏掉一个,而「哪个值长就截哪个」
        对所有工具都成立。
        """
        if not isinstance(inp, dict):
            return None, 0
        longest = 0
        for v in inp.values():
            if isinstance(v, str) and len(v) > cls.LIST_OUTPUT_CAP:
                longest = max(longest, len(v))
        if not longest:
            return None, 0
        return ({k: (v[:cls.LIST_OUTPUT_CAP] if isinstance(v, str)
                     and len(v) > cls.LIST_OUTPUT_CAP else v)
                 for k, v in inp.items()}, longest)

    def _trim_outputs(self, msgs):
        """列表用的那一份:工具的输出与入参、思考正文都截断,并标出全长。原对象不改——它们是
        messages() 的返回,别处还拿着(见 messages_window_json 缓存字节而不缓存对象的理由)。
        展开那一刻前端按 /part/<id> 取回全块(见 hsStepFull)。"""
        def over(p):
            if p.get("type") == "execution-result":
                return len(str(p.get("text") or "")) > self.LIST_OUTPUT_CAP
            if p.get("type") == "reasoning":
                return len(str(p.get("text") or "")) > self.LIST_REASON_CAP
            if p.get("type") != "tool":
                return False
            st = p.get("state") or {}
            if len(str(st.get("output") or "")) > self.LIST_OUTPUT_CAP:
                return True
            if len(str((p.get("returned") or {}).get("text") or "")) > self.LIST_OUTPUT_CAP:
                return True
            return self._trim_input(st.get("input"))[1] > 0

        out = []
        for m in msgs:
            parts = m.get("parts") or []
            visible = [p for p in parts if p.get("type") != "execution-result" or is_async_result(p)]
            if len(visible) != len(parts):
                m = dict(m, parts=visible)
                parts = visible
                if not parts and (m.get("info") or {}).get("role") == "tool":
                    continue
            if not any(over(p) for p in parts):
                out.append(m)
                continue
            m2 = dict(m)
            m2["parts"] = []
            for p in parts:
                if not over(p):
                    m2["parts"].append(p)
                    continue
                if p.get("type") in {"reasoning", "execution-result"}:
                    text = str(p.get("text") or "")
                    p2 = dict(p)
                    cap = self.LIST_OUTPUT_CAP if p.get("type") == "execution-result" else self.LIST_REASON_CAP
                    p2["text"] = text[:cap]
                    p2["textMore"] = len(text)
                    m2["parts"].append(p2)
                    continue
                st = p.get("state") or {}
                st2 = dict(st)
                text = str(st.get("output") or "")
                if len(text) > self.LIST_OUTPUT_CAP:
                    st2["output"] = text[:self.LIST_OUTPUT_CAP]
                    st2["outputMore"] = len(text)
                inp2, n = self._trim_input(st.get("input"))
                if n:
                    st2["input"] = inp2
                    st2["inputMore"] = n
                p2 = dict(p)
                p2["state"] = st2
                ret = p.get("returned") or {}
                rtext = str(ret.get("text") or "")
                if len(rtext) > self.LIST_OUTPUT_CAP:
                    p2["returned"] = dict(ret, text=rtext[:self.LIST_OUTPUT_CAP])
                    p2["returnedMore"] = len(rtext)
                m2["parts"].append(p2)
            out.append(m2)
        return out

    def part(self, sid, pid):
        """某一块的全文。列表里工具输出是截断的,展开时按这条取回来。

        正在运行的 Pi 子代理尚未进入 messages；先查 live 投影，用户主动展开时才能拿到
        当下完整进度。折叠页面不会调用这条接口。"""
        wanted = str(pid)
        # 不为一个块构造并 deepcopy 整条 live_message。先定位目标，只复制这一块；复制后再
        # 核对 generation 对象仍是当前 live，若恰逢收尾换代则转去 durable 历史。
        ctx = self._live.get(sid)
        if ctx:
            source = (ctx.get("parts") or {}).get(pid)
            if source is None:
                source = next((p for p in (ctx.get("parts") or {}).values()
                               if str(p.get("id") or "") == wanted), None)
            if source is not None:
                got = copy.deepcopy(source)
                if self._live.get(sid) is ctx:
                    return got
        elif not ctx:
            recovered = self._recovered_live_message(sid)
            for p in (recovered or {}).get("parts") or []:
                if str(p.get("id") or "") == wanted:
                    return p
        for m in self.messages(sid):
            for p in m.get("parts") or []:
                if str(p.get("id") or "") == wanted:
                    return p
        return None

    def _presentation_messages(self, messages):
        """Return the UI projection without changing durable message storage."""
        return messages

    def messages_window_json(self, sid, window):
        """窗口内的消息,序列化好的字节(不含首尾方括号)。

        窗口截断放在这里而不是 messages():那个方法还被撤回、对账之类的写回路径用着,
        在那里截断等于把存储本身裁掉。
        """
        path = self._msg_path(sid)
        try:
            st = os.stat(path)
            # 改写走临时文件 + replace，每次都是新文件号。Windows 的时间戳按系统时钟刻度
            # (约 15 ms)走，同一刻度内的等长改写 mtime 与 size 都不变，只靠文件号区分。
            stamp = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size, int(window))
        except OSError:
            return b""
        with self._msg_cache_lock:
            hit = self._msg_cache.get(sid)
            if hit and hit[0] == stamp:
                return hit[1]
        with self._msg_lock(path):
            raw = self._turn_index(sid).raw_window(limit=window)
        out = [json.loads(line) for line in raw.splitlines() if line.strip()]
        for message in out:
            for part in message.get("parts") or []:
                if isinstance(part, dict) and part.get("returned"):
                    settle_stored(part)
        out = self._trim_outputs(self._presentation_messages(out))
        body = b",".join(json.dumps(m, ensure_ascii=False).encode("utf-8") for m in out)
        with self._msg_cache_lock:
            # 戳是读之前取的,所以 body 只可能与戳同新或更新,不会更旧:读的过程中文件又变了
            # 的话,下一次请求 stat 到的是新戳,照样未命中。不必在这里再核一遍。
            self._msg_cache.pop(sid, None)   # 先删再插,让插入序等于使用序(下面按它淘汰)
            self._msg_cache[sid] = (stamp, body)
            while len(self._msg_cache) > self._MSG_CACHE_MAX:
                self._msg_cache.pop(next(iter(self._msg_cache)))
        return body

    # ---- 会话附件:按 URL 取,不随正文内联 ----
    # 一张截图内联进正文要占 4/3 的体积,而正文每次打开会话都整份重传、重解析、重铺 DOM。
    # 2026-08-20 实测一段 3.1MB 的会话:49 条消息 322 个 part,其中 5 张图占 2.4MB(77%),
    # 后端读盘+反序列化+再序列化要 71ms,浏览器那侧还要再解一遍 base64。
    # 改成 URL 之后这几张图各取一次、走浏览器缓存,后续切回来是零成本。
    ASSET_SEG = "asset"

    def asset_path(self, sid, name):
        """附件的真实路径;不在本会话 uploads 目录里的一律当不存在。

        `/session/<id>/asset/<name>` 里的 name 来自 URL,必须当作不可信输入:
        只取 basename 还不够(realpath 之后仍可能经软链跳出去),所以最后再核一次前缀。
        """
        root = os.path.realpath(os.path.join(self.dir, "uploads", str(sid or "")))
        path = os.path.realpath(os.path.join(root, os.path.basename(str(name or ""))))
        if not path.startswith(root + os.sep) or not os.path.isfile(path):
            return ""
        return path

    @staticmethod
    def asset_url(sid, path):
        """正文里写的引用。文件名带毫秒时间戳(或内容哈希),同名即同物,所以可以长缓存。"""
        import urllib.parse as _up
        return "/session/%s/asset/%s" % (_up.quote(str(sid), safe=""),
                                         _up.quote(os.path.basename(path), safe=""))

    def asset_save(self, sid, data_b64, media_type="image/png"):
        """把一段 base64 落成文件并返回引用。用于记录里只有 base64、盘上没有原件的那条路
        (引擎自己的 transcript)。文件名取内容哈希:同一张图重建多少次都是同一个文件,
        不会每次对账都多出一份副本。"""
        import base64, hashlib, mimetypes
        try:
            raw = base64.b64decode(data_b64 or "")
        except Exception:
            return ""
        if not raw:
            return ""
        ext = mimetypes.guess_extension(media_type or "") or ".png"
        if ext == ".jpe":
            ext = ".jpg"
        name = hashlib.sha1(raw).hexdigest()[:16] + ext
        d = os.path.join(self.dir, "uploads", str(sid))
        path = os.path.join(d, name)
        if not os.path.isfile(path):
            try:
                os.makedirs(d, exist_ok=True)
                with open(path, "wb") as f:
                    f.write(raw)
            except OSError:
                return ""
        return self.asset_url(sid, path)

    def messages(self, sid):
        out = []
        try:
            with open(self._msg_path(sid), encoding="utf-8") as f:
                for ln in f:
                    if ln.strip():
                        out.append(json.loads(ln))
        except FileNotFoundError:
            pass
        for m in out:
            for q in m.get("parts") or []:
                if isinstance(q, dict) and q.get("returned"):
                    settle_stored(q)
        return out

    def fork_session(self, sid, msg_id=""):
        """把一段会话整体分叉出一支新的:记录全份抄走,引擎侧从同一条线另开一支,原会话不动。

        **只能从当前末尾分,不能从中间某条消息分。** 2026-08-06 实测:claude 的 `--resume <id>`
        会把新回合继续追加进被 resume 的那份 transcript,并另报一个新 id。所以「第 N 回合当时
        报的那个 id」不是那一刻的快照——它随后续回合一起长。按某条历史消息去锚定,拿到的是
        会话此刻的全部上下文,不是那时候的。msg_id 因此只留作记录,不参与定位。

        相对 rewind 的价值就在这里:不回滚任何已经发生的事,只是另起一支接着走。"""
        if not self.fork_flag:
            return None
        msgs = self.messages(sid)
        esid = (self.index.get(sid) or {}).get("engine_sid")
        cut = len(msgs) - 1
        if cut < 0 or not esid:
            return None
        new = self.create_session()
        nsid = new["id"]
        msgs = self._fork_reference_attachments(sid, nsid, msgs)
        for m in msgs[:cut + 1]:
            m = json.loads(json.dumps(m))   # 深拷贝:parts 里的 sessionID 要改写成新会话
            info = m.setdefault("info", {})
            info["sessionID"] = nsid
            for q in m.get("parts") or []:
                q["sessionID"] = nsid
            self._append_msg(nsid, m)
        src_title = (self.index.get(sid) or {}).get("title") or ""
        with self._lock:
            e = self.index.get(nsid)
            if e:
                # 这里的 esid 是「从哪一支分出来」的锚点,不是新会话自己的引擎 id——
                # 新的那个要等 fork_pending 兑现、引擎另报一条线才有。所以这一处不登记
                # 侧栏,登记留给那时候的 _set_engine_sid,否则两段会话会指向同一个 id。
                e["engine_sid"] = esid
                e["fork_pending"] = True
                e["forked_from"] = sid
                if src_title:
                    e["title"] = ("分支 · " + src_title)[:60]
                src_opts = (self.index.get(sid) or {}).get("opts")
                if isinstance(src_opts, dict):
                    e["opts"] = dict(src_opts)
            self._save_index()
        self.bus.publish({"type": "session.updated", "properties": {"sessionID": nsid}})
        return dict(self.index.get(nsid) or {}, id=nsid)

    def rewind(self, sid, msg_id, dry_run=True):
        """回退到某条用户消息之前。不支持的引擎返回错误,界面据此不显示入口。"""
        return {"error": "该引擎不支持回退"}

    def _slash_path(self):
        return os.path.join(self.dir, "slash.json")

    def _save_slash(self, names):
        if list(names) == list(self.slash_commands()):
            return
        try:
            with open(self._slash_path(), "w", encoding="utf-8") as f:
                json.dump(list(names), f, ensure_ascii=False)
        except OSError:
            pass
        self._slash_cache = list(names)

    def slash_commands(self):
        """引擎的斜杠命令表,尽量带上说明。落盘保存,重启后第一回合还没跑就能用上一次的。

        引擎只报名字,不报说明。技能与自定义命令的说明写在各自文件的 frontmatter 里,
        这里按名字去找;找不到的就只给名字——「引擎命令」这种占位讲不出任何东西,
        不如留白。"""
        if getattr(self, "_slash_cache", None) is not None:
            return self._slash_cache
        try:
            with open(self._slash_path(), encoding="utf-8") as f:
                names = json.load(f)
        except Exception:
            names = []
        desc = self._slash_desc()
        self._slash_cache = [{"n": n, "d": desc.get(str(n).split(":")[-1], "")} for n in names]
        return self._slash_cache

    def _slash_desc(self):
        """技能 / 自定义命令的一句话说明:名字 → description。
        用户级与项目级两处都扫,项目级优先(同名时它更贴近手头的活)。"""
        out = {}
        roots = [os.path.expanduser("~/.claude"), os.path.join(self.workspace, ".claude")]
        for root in roots:
            for name, path in self._md_candidates(root):
                d = self._front_desc(path)
                if d:
                    out[name] = d
        return out

    @staticmethod
    def _md_candidates(root):
        """(命令名, 说明文件) 对:技能是 skills/<名>/SKILL.md,自定义命令是 commands/<名>.md。"""
        skills = os.path.join(root, "skills")
        if os.path.isdir(skills):
            for n in os.listdir(skills):
                f = os.path.join(skills, n, "SKILL.md")
                if os.path.isfile(f):
                    yield n, f
        cmds = os.path.join(root, "commands")
        if os.path.isdir(cmds):
            for n in os.listdir(cmds):
                if n.endswith(".md"):
                    yield n[:-3], os.path.join(cmds, n)

    @staticmethod
    def _front_desc(path):
        """frontmatter 里的 description。只读开头几十行,不整份读。"""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                if f.readline().strip() != "---":
                    return ""
                for _ in range(60):
                    ln = f.readline()
                    if not ln or ln.strip() == "---":
                        break
                    if ln.lower().startswith("description:"):
                        return ln.split(":", 1)[1].strip().strip('"\'')[:120]
        except OSError:
            pass
        return ""

    def live_engine_sids(self):
        """当前正在跑的回合对应的引擎会话 uuid。侧栏据此判 GUI 会话的工作态——
        终端那侧靠嗅探 PTY 标题里的 spinner 帧,GUI 没有 PTY,只能由这里报。"""
        out = set()
        for sid, ctx in list(self._live.items()):
            e = ctx.get("esid") or (self.index.get(sid) or {}).get("engine_sid")
            if e:
                out.add(e)
        return out

    def resident_engine_sids(self):
        """当前握着常驻进程的引擎会话 uuid。

        与 live_engine_sids 不同:那个报「此刻有回合在跑」,这个报「进程还在,回合可跑可不跑」。
        GUI 会话没有 PTY,侧栏据 alive 判断时它恒为假,于是「关闭进程」这一项从来不出现——
        可它确实握着一条常驻进程(claude 是 _PERSIST 里那条,codex 是 app-server 连接)。
        """
        return set()

    def release_resident(self, esid):
        """摘掉某段会话的常驻进程,记录不动。下一回合冷起一次。不支持返回 False。"""
        return False

    def running_agents(self, esid):
        """这段引擎会话此刻有几个子代理在跑。侧栏据此让行首那颗点闪起来——主线答完了、
        异步派出的子代理还在后台干活,那段时间也该算「它在动」。
        不派子代理的引擎恒为 0;派的那几个各自实现,判据一概复用自家的 subagents()。"""
        return 0

    def _recovered_live_message(self, sid):
        """把后端重载遗留的处理中快照翻成可持久化的中断消息。

        快照原先只在 GET /message 时临时拼到响应末尾。下一轮一开始，新的 ctx 会把同名
        ``<sid>.live.json`` 覆盖掉；旧快照又没进消息 JSONL，于是重启前已经画出来的过程
        整段消失。这里统一完成状态收口，展示与续轮前固化共用同一份结果。
        """
        snap = self._read_live_checkpoint(sid)
        if not snap:
            return None
        stored = self.messages(sid)
        info = snap.setdefault("info", {})
        mid = info.get("id")
        if mid and any((m.get("info") or {}).get("id") == mid for m in stored):
            self._clear_live_checkpoint(sid)
            return None
        info["running"] = False
        info["recovered"] = True
        info["interrupted"] = True
        info["local_only"] = True
        # These were in-flight observations, not restart-persistent operations.
        for key in ("retry", "activity", "compacting", "native_queue"):
            info.pop(key, None)
        started = int(info.get("started") or 0)
        at = int(info.get("beat") or 0) or int(time.time() * 1000)
        info["at"] = at
        if started:
            info["turn_t0"] = started
            if at >= started:
                info.setdefault("ms", at - started)
        # 旧版 checkpoint 没存轮次标记；续轮前最后一条真人发言就是它所属的回合。
        prev = next((m for m in reversed(stored) if self._turn_start(m)), None)
        pinfo = (prev or {}).get("info") or {}
        for key in ("seq", "source", "host"):
            if not info.get(key) and pinfo.get(key):
                info[key] = pinfo[key]
        note = "应用重载，执行记录到此中断"
        for part in snap.get("parts") or []:
            state = part.get("state") or {}
            if part.get("type") == "tool" and state.get("status") == "running":
                state["status"] = "error"
                output = str(state.get("output") or "").rstrip()
                if note not in output:
                    state["output"] = (output + "\n" + note).strip()
        return snap

    def _archive_live_checkpoint(self, sid):
        """新回合覆盖 live 文件前，先把重载遗留快照固化进消息投影。"""
        if self._live.get(sid):
            return False
        snap = self._recovered_live_message(sid)
        if not snap:
            return False
        self._append_msg(sid, snap)
        self._trim_store(sid)
        self._clear_live_checkpoint(sid)
        return True

    def live_message(self, sid):
        """在跑那一回合的快照,形态跟落盘的助手消息一样,多一个 running 标记。
        没有在跑的回合就返回 None。"""
        ctx = self._live.get(sid)
        if not ctx:
            return self._recovered_live_message(sid)
        # A GET must not release a turn before its writer persists the outcome.
        # Process exit is handled by the dispatch owner and _finish_turn.
        # 一个字都还没产出也要报:回合已经在跑了,界面据此显示「处理中」。
        # 早先这里空手就返回 None,刷新之后界面以为没事在跑,状态行要等下一次输出才回来。
        parts = [ctx["parts"][k] for k in ctx.get("order") or [] if k in ctx["parts"]]
        info = {"id": ctx["msg_id"], "role": "assistant", "sessionID": sid, "running": True,
                "started": ctx.get("t0") or 0}   # 计时的起点,刷新后接着算而不是从零重来
        if ctx.get("esid"):
            info["engine_sid"] = ctx["esid"]
        if ctx.get("compact_t0"):
            info["compacting"] = ctx["compact_t0"]   # 压缩中刷新页面,进度接着原来的起点算
        if isinstance(ctx.get("environment"), dict):
            info["environment"] = copy.deepcopy(ctx["environment"])
        from .lifecycle import snapshot
        info.update(snapshot(self, ctx))
        return {"info": info, "parts": copy.deepcopy(parts)}

    def _live_checkpoint_path(self, sid):
        return os.path.join(self.dir, sid + ".live.json")

    def _checkpoint_live(self, ctx, force=False):
        """处理中内容独立落盘；前端或后端重载后仍能展开已产生的过程。"""
        now = time.time()
        if not force and now - (ctx.get("_checkpoint_at") or 0) < 0.25:
            return
        ctx["_checkpoint_at"] = now
        # beat 是这份快照最后一次被写下的时刻。界面据它判断快照新不新——回合被外力带走时
        # (进程被杀、服务重启)快照会留在盘上,而它一旦不再更新,就不该再按「正在跑」对待。
        # 原来那侧按「起始时刻过去多久」判,前提是回合有总时长上限;上限改成静默之后,
        # 一个跑了两小时的回合完全正常,起始时刻不再说明任何事。
        info = {"id": ctx["msg_id"], "role": "assistant", "sessionID": ctx["sid"],
                "running": True, "started": ctx.get("t0") or 0,
                "beat": int(now * 1000)}
        for key in ("seq", "source", "host", "engine", "model"):
            if ctx.get(key):
                info[key] = ctx[key]
        if ctx.get("esid"):
            info["engine_sid"] = ctx["esid"]
        if isinstance(ctx.get("environment"), dict):
            info["environment"] = copy.deepcopy(ctx["environment"])
        from .lifecycle import snapshot
        info.update(snapshot(self, ctx))
        snap = {"info": info,
                "parts": [copy.deepcopy(ctx["parts"][k]) for k in ctx.get("order") or []
                          if k in ctx["parts"]]}
        path = self._live_checkpoint_path(ctx["sid"])
        tmp = "%s.%d.tmp" % (path, threading.get_ident())
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _read_live_checkpoint(self, sid):
        try:
            with open(self._live_checkpoint_path(sid), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _clear_live_checkpoint(self, sid):
        try:
            os.remove(self._live_checkpoint_path(sid))
        except OSError:
            pass

    def _msg_path(self, sid):
        return os.path.join(self.dir, sid + ".jsonl")

    def _turn_index(self, sid):
        with self._msg_cache_lock:
            index = self._turn_indexes.get(sid)
            if index is None:
                index = JsonlTurnIndex(self._msg_path(sid), self._turn_start)
                self._turn_indexes[sid] = index
            return index

    def projected_history(self, sid, have=0, limit=30):
        """Page this adapter's JSONL projection by byte offset.

        This is used when the projection itself is the public immutable history
        (Shepherd). Native adapters keep their native transcript/rollout readers.
        """
        path = self._msg_path(sid)
        with self._msg_lock(path):
            raw = self._turn_index(sid).raw_window(have=have, limit=limit)
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        for message in rows:
            for part in message.get("parts") or []:
                if isinstance(part, dict) and part.get("returned"):
                    settle_stored(part)
        return self._trim_outputs(self._presentation_messages(rows))

    # ---- 引擎进程实例:一条会话此刻挂在哪个进程上 ----
    # 子代理不是独立进程,它是引擎进程事件循环上的一个异步任务,没有自己的 pid(实测一个
    # 子代理在跑时,claude 进程仍然只有一个)。所以「它还在不在跑」这件事,唯一的硬信号是
    # 派它出来的那个引擎进程实例还在不在。而实例会在一条会话的生命期里更换(同一段会话
    # 先后见过 pid 24515 与 40576),只问「有没有 claude 进程」不成立,得按实例记。
    # 身份取 pid + 起始时刻;判子代理只用起始时刻,pid 留作诊断与将来核对。
    # 记进会话索引(sessions.json),后端重启之后这一维仍然在——重启前派出的子代理正是
    # 最需要它的那一批。
    def _note_proc(self, sid, proc):
        """登记这条会话新起的引擎进程实例。会话不在索引里(临时会话)就不记。"""
        try:
            pid = int(getattr(proc, "pid", 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        rec = {"pid": pid, "started": int(time.time() * 1000)}
        try:
            from app_control.capability import register_process as register_app_control_process
            register_app_control_process(sid, proc)
        except Exception:
            pass
        with self._lock:
            s = self.index.get(sid)
            if not s:
                return rec
            s["proc"] = rec
            try:
                self._save_index()
            except OSError:
                pass
        return rec

    def proc_instance(self, sid):
        """这条会话最近一次由 Desk 起的引擎进程实例,{pid, started}。

        没有记录返回 {}:会话可能在 Desk 之外起过(比如用户在终端里 resume 同一段会话),
        那时进程不在 Desk 手里,这一维给不出事实。调用方遇到 {} 一律退回原有判据,
        不要因为「没记录」就把在跑的判死。"""
        return dict((self.index.get(sid) or {}).get("proc") or {})

    def _save_index(self):
        from durable_json import write
        write(self.index_path, self.index)
        try:
            from term import _term_catalog_invalidate
            _term_catalog_invalidate()
        except (ImportError, AttributeError):
            pass

    def _fork_reference_attachments(self, sid, nsid, messages):
        from .attachment_context import reference_catalog
        with self._lock:
            records, _text = reference_catalog(self, sid)
        rebound = copy.deepcopy(messages)
        if not records:
            return rebound
        destination = os.path.join(self.dir, 'uploads', nsid)
        os.makedirs(destination, exist_ok=True)
        for item in records:
            source = self.asset_path(sid, item['asset'])
            if source:
                shutil.copyfile(source, os.path.join(destination, item['asset']))
        # Inherit explicit reference ownership, including originals older than
        # the display window; do not share source paths or delivery identities.
        inherited = [{**item, 'request_id': ''} for item in records]
        with self._lock:
            self.index[nsid]['reference_attachments'] = inherited
            self._save_index()
        urls = {self.asset_url(sid, item['asset']): self.asset_url(nsid, item['asset'])
                for item in records}
        for message in rebound:
            message.get('info', {}).pop('delivery_request_id', None)
            for part in message.get('parts') or []:
                if part.get('context_kind') == 'lark_web' and part.get('url') in urls:
                    part['url'] = urls[part['url']]
        return rebound

    def _forget_reference_attachments(self, sid, *, message_id='', request_id=''):
        if not message_id and not request_id:
            return
        with self._lock:
            row = self.index.get(sid)
            if row is None:
                return
            previous = row.get('reference_attachments') or []
            remaining = [item for item in previous if not (
                (message_id and item.get('message_id') == message_id) or
                (request_id and item.get('request_id') == request_id))]
            if remaining != previous:
                row['reference_attachments'] = remaining
                self._save_index()

    def _append_msg(self, sid, msg):
        path = self._msg_path(sid)
        with self._msg_lock(path):   # 与整份重写互斥,免得追加落在正要被替换掉的那一份上
            from peer_metadata import restore
            restore(path, [msg])
            self._turn_index(sid).append(msg)
        # Keep metadata beyond the 25-turn display projection; plaintext stays in
        # the original upload. A crash before this write is repaired from messages
        # at the next append, never by scanning uploads or another session.
        from .attachment_context import reference_catalog
        with self._lock:
            records, _text = reference_catalog(self, sid)
            row = self.index.get(sid)
            if row is not None and records != (row.get('reference_attachments') or []):
                row['reference_attachments'] = records
                self._save_index()

    # 存储里保留多少个回合。它只是引擎记录的投影,更早的一律经 /history 从记录本身翻
    # (各引擎都实现了:claude 读 transcript、codex 家族读 rollout),
    # 这里不留第二份。与 import_session 的 limit 是同一个口径,两处要一致。
    STORE_TURNS = 25

    def _trim_store(self, sid):
        """只留最近 STORE_TURNS 个回合；在同一消息锁内按 offset 流式复制后缀。"""
        path = self._msg_path(sid)
        with self._msg_lock(path):
            try:
                self._turn_index(sid).trim_to_recent(self.STORE_TURNS)
            except OSError:
                return

    def _touch(self, sid, title=None):
        with self._lock:
            s = self.index.get(sid)
            if not s:
                return
            s["time"]["updated"] = int(time.time() * 1000)
            if title and not s.get("fallback_title") and not s.get("title"):
                # 默认名只在首条用户消息落盘时同步保存；自动命名是增强，失败、取消或超时都
                # 不能迫使会话列表回读完整原生历史。单独一层保持“自动名 > 默认名”。
                from session_names import FALLBACK_TITLE_MAX_CHARS
                fallback = clean_session_title(title, limit=FALLBACK_TITLE_MAX_CHARS)
                if len(fallback) > FALLBACK_TITLE_MAX_CHARS:
                    fallback = fallback[:FALLBACK_TITLE_MAX_CHARS - 1].rstrip() + "…"
                if fallback:
                    s["fallback_title"] = fallback
            if title and not s.get("title"):
                # 清洗见 bus.clean_session_title:剥当前上下文、去 markdown 标记、折空白、
                # 在标点上截断。压不出人话时退回原文,宁可难看也别让会话没有名字。
                s["title"] = clean_session_title(title) or title[:60]
            self._save_index()
        self.bus.publish({"type": "session.updated", "properties": {"sessionID": sid}})

    def _term_register(self, sid, engine_sid):
        """把这段会话在侧栏登记一份归属。写索引与登记侧栏拆开,是因为拿到引擎 id 的
        路子不止一条:新开线程有事件,resume 与 import 没有。以前只有事件那条路顺带
        登记,于是 resume 出来的会话怎么发消息都不进侧栏(2026-09-08 实见)。

        延迟导入:term 反过来要用适配器,模块级互相引用会成环。"""
        if not engine_sid:
            return
        with self._lock:
            if engine_sid in self._term_seen:
                return
            self._term_seen.add(engine_sid)
            rec = self.index.get(sid) or {}
            dom = rec.get("domain") or ""
            is_task = bool(rec.get("task"))
        try:
            from term import _term_migrate_identity, term_meta_register
            # 任务行可在 GUI 会话尚无引擎 id 时先绑定 adp_ id；首轮拿到 thread id 后，
            # 把任务注记与工作流 refs 一并迁到稳定 id，避免会话从任务下消失。
            _term_migrate_identity({"id": sid, "engine": self.name, "domain": dom}, engine_sid)
            term_meta_register(engine_sid, self.name, dom, task=is_task)
        except Exception:
            with self._lock:
                self._term_seen.discard(engine_sid)   # 失败不算登记过,下一轮还要再试

    def _set_engine_sid(self, sid, engine_sid):
        with self._lock:
            if sid in self.index:
                self.index[sid]["engine_sid"] = engine_sid
                self._save_index()
        # GUI 建的会话不走 PTY 那条路,以前没人登记,于是它只存在于 HTML 视图自己的
        # 菜单里,侧栏和工作流都看不见。
        self._term_register(sid, engine_sid)
