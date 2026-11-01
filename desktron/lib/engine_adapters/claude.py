# 由 engine_adapters.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)
import copy
import json
import os
import re
import subprocess
import threading
import time
import claude_auth
from .claude_transcript import ClaudeTranscriptMixin
from .cli import CliAdapter
from .execution_returns import from_notification, settle_return
from .persist import _PERSIST, _PERSIST_LOCK, _persist_drop


def _is_compact_cmd(text):
    """这一句是不是手动压缩。/compact 可以带自定义指示(「/compact 只留结论」),按首个词判。"""
    head = str(text or "").strip().split(None, 1)
    return bool(head) and head[0] == "/compact"


def _iso_ms(s):
    """记录里的 ISO 时间戳转毫秒。解析不了就返回 0,由调用方当成「没有」。"""
    import datetime
    try:
        return int(datetime.datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%S.%fZ")
                   .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
    except Exception:
        return 0


class ClaudeAdapter(ClaudeTranscriptMixin, CliAdapter):
    """claude code headless:-p --output-format stream-json --include-partial-messages。
    字符级 delta 原生;会话经 --resume 续(每回合返回新 session_id,滚动更新)。"""

    name = "claude"
    model_label = "claude code · 订阅"
    binary = "claude"
    _ENV_TTL = 30   # 登录态探测会起一次 CLI；每开一段会话查一次即可，别每次 /config 都查
    _ENV_LOCK = threading.Lock()
    use_stdin_prompt = True   # 双向 stream-json:支持 can_use_tool 权限询问
    fork_flag = "--fork-session"   # 分支:从被 resume 的会话另开一支,原会话不动
    streams_text = True
    asks_permission = True
    # 常驻的两个前提这里都成立:stdin 收 stream-json 帧,输出里的 result 帧划得出回合边界。
    persist_supported = True
    # cancel_queued:连同排队中的消息一起取消。打断的语义是「别做了」,留着队列会让
    # 下一句紧接着又跑起来,人按下停止之后还得再按一次。
    interrupt_request = {"subtype": "interrupt", "cancel_queued": True}
    UI_MODE_DEFAULT = "default"     # 权限档,键就是基类的 mode

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 待答元数据是适配器实例状态；引擎线程与 /state 快照统一受 _plock 保护。
        self._pending_input = {}
        self._pending_ask_meta = {}

    # claude 专属起手提示:claude 自带全局个人记忆(~/.claude),易与 Desk 工作层记忆混;
    # 只注给 claude 的 --append-system-prompt(headless 与 PTY 共用),不进引擎无关的合成 AGENTS.md。
    CLAUDE_DELTA = ("Desk 项目记忆保存在 `.memory/`，按 memory 的说明读写；"
                    "不要用 `~/.claude` 保存或代替项目记忆。")

    # Bash 工具的 description 入参是模型自愿填的软字段,claude 通常会填,前端 hsToolObj
    # 优先拿它当摘要;没有这一项才退回猜(词表)或摆命令原文——长命令、检索式截断在
    # 屏幕右边,读的人得自己解码。2026-08-24 实测「desktron rc」这个会话最新一回合:
    # 同一个 sonnet-5、一字不差的 opts,46 次 Bash 调用 description 100% 缺失;同一模型
    # 在另一段会话里是 34/34 100% 命中。差异没能定位到具体哪一段上下文,大概率是长会话/
    # 多次压缩后的状态漂移,不是这个模型本身不具备这个能力——既然定位不到触发条件,
    # 就不等下次复现,把软字段改成系统提示里的硬性要求兜底,与 CodexAdapter.INTENT_RULE
    # 同一处境(codex 家族的工具本来就没有 description 字段,只能整段让模型手写)。
    INTENT_RULE = (
        "调用 Bash 时，必须在 `description` 中用十来个中文字说明目的；"
        "不要写“执行命令”“运行脚本”等空泛文字。"
    )

    def resident_engine_sids(self):
        with _PERSIST_LOCK:
            sids = [sid for sid, ep in _PERSIST.items() if ep.proc.poll() is None]
        return {e for e in (self._esid_of(x) for x in sids) if e}

    def release_resident(self, esid):
        sid = self._sid_by_esid(esid)
        if not sid or sid not in _PERSIST:
            return False
        _persist_drop(sid)
        return True

    def recap_generate(self, sid, run, snap):
        from .recap import claude_recap
        return claude_recap(self, sid, run, snap)

    def build_cmd(self, text, engine_sid, opts):
        cmd = ["claude", "-p", "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        # --permission-prompt-tool stdio 是权限询问的开关:传了它 CLI 才发 can_use_tool
        # control_request,由 handle_event 转成 permission.asked 事件、respond_permission 回帧。
        # 2026-08-05 实测确认;此前判「CLI 不发 control_request,等上游放开」是错的,
        # 少的就是这个标志(读 claude-agent-sdk 的 sdk.mjs 发现:它在 canUseTool 存在时加的正是它)。
        # 有了它 default 才有意义——询问弹到界面上,不再等于静默拒,所以缺省值也从 acceptEdits 改回 default。
        mode = opts.get("mode") or "default"
        cmd += ["--permission-mode", mode, "--permission-prompt-tool", "stdio"]
        if opts.get("model"):
            cmd += ["--model", opts["model"]]
        # 扩展思考:新模型(opus-4-8/sonnet-4-6)的文档化开关是 --effort,非 MAX_THINKING_TOKENS。
        # sonnet 返回思考明文;opus-4-8 默认 display=omitted(只回签名、内容空),与 claude code 一致。
        # ultracode 不是 effort 的一档,是一个会话级开关:同时把 effort 钉到 xhigh,
        # 并常驻打开动态工作流编排(引擎自己按需拉起多代理)。引擎那侧的原话是
        # 「Set per session via the `ultracode` settings key(--settings 或 apply_flag_settings)」,
        # 所以这里走 --settings。开着的时候不再另外传 --effort:两者会打架,
        # 而它自己就等价于 xhigh。
        ultra = bool(opts.get("ultracode"))
        if ultra:
            cmd += ["--settings", json.dumps({"ultracode": True})]
        effort = opts.get("effort", "high")
        if not ultra and effort and effort != "off":
            cmd += ["--effort", effort]
        # 思考块要显式要一份摘要才拿得到明文:这两个模型的 API 默认是 display=omitted,
        # 只回签名、内容为空,前端那个思考块因此永远是空的(2026-08-06 实测)。
        # 取值只有 summarized / omitted 两种,没有"逐字原文"这一档。
        if opts.get("thinking_display", "summarized") != "off":
            cmd += ["--thinking-display", opts.get("thinking_display") or "summarized"]
        # 自动压缩窗口:上下文越长,每回合的预填耗时越大,首帧因此变慢。把窗口调小让 CLI 更早
        # 压缩,是缩短首帧等待最直接的一档。空值与 auto 都不传标志,保持引擎自己的默认行为。
        ac = str(opts.get("autocompact") or "").strip()
        if ac and ac != "auto":
            try:
                n = int(ac)
            except ValueError:
                n = 0
            # 越界或非数字一律当没配:拼进去只会让 CLI 直接拒绝启动,整个会话起不来。
            if 100000 <= n <= 1000000:
                cmd += ["--autocompact", str(n)]
        cmd += ["--append-system-prompt", self._turn_system_prompt(opts)]
        if engine_sid:
            cmd += ["--resume", engine_sid]
        return cmd

    def _turn_system_prompt(self, opts):
        from context_delivery import compose_system
        return compose_system(self.system_body(opts), opts, self.CLAUDE_DELTA, self.INTENT_RULE)

    def spawn_env(self, opts):
        # 扩展思考预算:headless 下不设此值 claude 不产 thinking 块(实测,非脱敏)。
        # 默认开启 reasoning 输出；opts.thinking=0 可关。
        budget = opts.get("thinking", 8000)
        env = {"MAX_THINKING_TOKENS": str(budget)} if budget else {}
        # 文件检查点:改文件前先留备份,rewind_files 靠它回滚。SDK 的 enableFileCheckpointing
        # 做的唯一一件事就是设这个变量(读 sdk.mjs 得到),所以纯 CLI 自己设即可,不必引入 SDK。
        # 必须从建会话起就开着——检查点在文件被改的那一刻生成,事后再开对旧会话无效。
        env["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] = "true"
        return env

    rewind_supported = True

    def _transcript_path(self, esid):
        """claude 的会话记录:~/.claude/projects/<编码后的 cwd>/<会话 id>.jsonl

        目录名由 cwd 编码而来,而会话的 cwd 不止一个:适配器自己跑在工作区根,终端会话跑在
        各工作流的域目录里。所以先按本适配器的 cwd 猜一次,猜不中就按会话 id 全局找——
        id 是 uuid,跨目录不会撞。"""
        import glob
        from term import _claude_proj_enc
        projects = os.path.join(os.path.expanduser('~'), '.claude', 'projects')
        guess = os.path.join(projects, _claude_proj_enc(self.workspace), esid + '.jsonl')
        if os.path.exists(guess):
            return guess
        hits = glob.glob(os.path.join(projects, '*', esid + '.jsonl'))
        return hits[0] if hits else guess

    def _compact_landed(self, esid, since_ms):
        """这一轮之后,记录里有没有落下新的压缩摘要。

        手动 /compact 的成败引擎在 stdout 上不一定说,记录里那条 isCompactSummary 是实打实
        的凭据(它的 timestamp 是压缩收尾的时刻)。只读文件末尾一段:摘要必定挨着末尾,而
        整份记录动辄上百 MB,为一次判断整读一遍不值。"""
        if not esid or not since_ms:
            return False
        path = self._transcript_path(esid)
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                if size > 2_000_000:
                    f.seek(size - 2_000_000)
                    f.readline()   # 跳掉被切断的半行
                blob = f.read()
        except OSError:
            return False
        for raw in reversed(blob.decode("utf-8", "replace").splitlines()):
            if '"isCompactSummary"' not in raw:
                continue
            try:
                o = json.loads(raw)
            except ValueError:
                continue
            if o.get("isCompactSummary"):
                # 留两秒余量:开压缩态用的是 Desk 这边的钟,记录里那个时刻由引擎写
                return _iso_ms(o.get("timestamp")) >= since_ms - 2000
        return False

    def search_source_path(self, sid, record):
        esid = str((record or {}).get("engine_sid") or "")
        path = self._transcript_path(esid) if esid else ""
        return path if path and os.path.isfile(path) else super().search_source_path(sid, record)

    def search_documents(self, sid, record, path):
        if path and path != self._msg_path(sid):
            from session_search import claude_documents
            return claude_documents(self, path, sid, record)
        return super().search_documents(sid, record, path)

    @staticmethod
    def _engine_title(path, _cache={}):
        """记录里最后一条 custom-title(claude 的 /rename 写在这里),没有则退到 summary。

        按文件大小做增量:第一次整份读,之后只读新追加的那一段。改名可以改很多次,
        取最后一次;这一段没有新的改名就沿用上一次的结果。整份读一份 136MB 的记录要几百毫秒,
        而每切一次会话都会走到这里。"""
        prev = _cache.get(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            return prev[1] if prev else ""
        if prev and prev[0] == size:
            return prev[1]
        start, title = (prev[0], prev[1]) if prev and prev[0] < size else (0, "")
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                if start:
                    f.seek(start)
                    f.readline()
                for ln in f:
                    ln = ln.strip()
                    if not ln or ('"custom-title"' not in ln and '"summary"' not in ln):
                        continue
                    try:
                        o = json.loads(ln)
                    except ValueError:
                        continue
                    if o.get("type") == "custom-title" and o.get("customTitle"):
                        title = o["customTitle"]
                    elif o.get("type") == "summary" and o.get("summary") and not title:
                        title = o["summary"]
        except OSError:
            return title
        _cache[path] = (size, title)
        return title

    def history(self, sid, have=0, limit=30):
        """更早的回合。have = 界面上已经有多少个回合(从末尾数),返回它前面的 limit 个。
        只读,不落盘——存储里始终只留最近那一段,翻页看的是记录本身。"""
        esid = (self.index.get(sid) or {}).get("engine_sid")
        if not esid:
            return []
        path = self._transcript_path(esid)
        if not os.path.exists(path):
            return []
        return self._history_window(self._group_transcript(path, sid, esid), have, limit)

    _ENV_CACHE = {}

    def env_notice(self, _cache=None):
        """未登录时给输入框上方那条一帧 warning。

        订阅登录态问独立 claude CLI(与设置页同一判据);另配了 ANTHROPIC_API_KEY 的走网关,
        不需要订阅登录,也就不该被提示。探测失败时不喊——查不到与没登录不是一回事。
        动作交给界面:登录必须在终端里做,这里只声明 kind=terminal,由前端切过去。
        """
        # /config 可能并发到达。把探测和缓存更新放在同一把锁内，避免同时拉起多个 CLI。
        if _cache is None:
            _cache = self._ENV_CACHE
        with self._ENV_LOCK:
            now = time.time()
            if "v" in _cache and now - _cache.get("at", 0) < self._ENV_TTL:
                hit = _cache["v"]
                return copy.deepcopy(hit) if hit else None
            ok = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
            if not ok:
                try:
                    from credstore import _secrets_read
                    ok = bool(str(_secrets_read().get("ANTHROPIC_API_KEY", "")).strip())
                except Exception:
                    pass
            if not ok:
                state = claude_auth.subscription_status()
                ok = state is not False
            row = None if ok else {
                "state": "warning", "stage": "auth", "engine":"claude", "provider":"claude", "label": "Claude Code 未登录",
                "details": ["独立 claude CLI 未登录,也没有配 ANTHROPIC_API_KEY。",
                            "Claude Desktop 的登录不会自动共享；请在终端执行 claude auth login。",
                            "若浏览器显示成功后仍未登录，请执行 claude doctor 检查 macOS 钥匙串。"],
                "action": {"label": "去终端登录", "kind": "terminal"},
            }
            _cache["v"] = row
            _cache["at"] = now
            return copy.deepcopy(row) if row else None

    def goal(self, sid):
        """当前会话的目标。/goal 注册的是一个会话级 Stop hook——条件没达成就不让这一轮收尾。
        设与达成各往记录里写一条 attachment:{type: goal_status, met, condition},成对出现。
        取最后一条即可:met=False 是还挂着,met=True 是已达成或已撤销。
        引擎内存里那个 iterations(被挡下几次)不落记录,这里给不出。"""
        esid = (self.index.get(sid) or {}).get("engine_sid")
        if not esid:
            return {}
        path = self._transcript_path(esid)
        try:
            size = os.path.getsize(path)
        except OSError:
            return {}
        # 先读末尾一段:目标的最后一次变更多半在近处。但它是会话级的,也可能很早就设下、
        # 一直挂到现在——尾窗里一条都没有时再整份找一次,按 (路径, 大小) 缓存,只付一次。
        got = self._goal_scan(path, size, tail=True)
        if not got:
            got = self._goal_scan(path, size, tail=False)
        return got

    def _goal_scan(self, path, size, tail, _cache={}):
        key = (path, size, tail)
        if not tail and key in _cache:
            return _cache[key]
        got = {}
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                if tail and size > 4_000_000:
                    f.seek(size - 4_000_000)
                    f.readline()
                for ln in f:
                    if '"goal_status"' not in ln:
                        continue
                    try:
                        o = json.loads(ln)
                    except ValueError:
                        continue
                    a = o.get("attachment") or {}
                    if a.get("type") != "goal_status":
                        continue
                    got = {"condition": a.get("condition") or "", "met": bool(a.get("met")),
                           "at": int((self._iso_ms(o.get("timestamp")) or 0))}
        except OSError:
            return {}
        if not tail:
            _cache[key] = got
        return got


    def subagents(self, sid):
        """这一段会话派出去的子代理。每个子代理有独立记录,落在主会话记录旁边的
        subagents/agent-<id>.jsonl,边跑边写。列表只取每个的名字、起止时刻与最新一步——
        真要看它在做什么,再按 id 取 subagent_log。

        每条还带一个 status:running 是还在干活,done 是已经收场。判据取主记录里引擎自己
        写下的事实(见 _agent_evidence),不按「记录多久没动」猜——一次长工具调用期间
        子代理可以安静好几分钟,时间窗在那里必然误判。再叠一维硬信号:派它出来的那个
        引擎进程实例还在不在(见 proc_instance 与 _agent_status 的 proc_started)。"""
        esid = (self.index.get(sid) or {}).get("engine_sid")
        if not esid:
            return []
        return self._subagents_of(esid, int(self.proc_instance(sid).get("started") or 0))

    def rewind(self, sid, msg_id, dry_run=True):
        # 回合跑着的时候不回退。控制帧与回合帧走同一条流,两个读者抢同一条通道:
        # 回退可能把本属于回合的帧取走(那一轮缺一段内容),回合的读者也可能吞掉回退的回应
        # (回退一直等不到答复)。而回退本身是个改文件的动作,在半途的回合上做尤其难说清
        # 结果——先让这一轮跑完,是更简单也更可控的规则。
        if self._live.get(sid):
            return {"error": "这一回合还在跑,等它结束再回退"}
        got, err = self._engine_umid(sid, msg_id)
        if not got:
            return {"error": err}
        esid, umid = got
        env = dict(os.environ)
        env.update(self.spawn_env({}))
        try:
            proc, fresh = self._rw_get(sid, esid, env)
        except OSError as e:
            return {"error": str(e)}
        out = {"error": "引擎没有回应"}
        killer = threading.Timer(60, proc.kill)
        try:
            def send(o):
                proc.stdin.write(json.dumps(o, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            # 先握手再发回退:控制协议要 initialize 之后才受理后续 control_request。
            # 续用的连接握过手了,不重发——重发会多出一帧响应,落在下面的读循环里。
            if fresh:
                send({"type": "control_request", "request_id": "init",
                      "request": {"subtype": "initialize"}})
            rid = "rw%d" % int(time.time() * 1000)
            send({"type": "control_request", "request_id": rid,
                  "request": {"subtype": "rewind_files", "user_message_id": umid,
                              "dry_run": bool(dry_run)}})
            killer.start()
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") != "control_response":
                    continue
                r = o.get("response") or {}
                if r.get("request_id") != rid:
                    continue
                out = (r.get("response") or {}) if r.get("subtype") == "success" \
                    else {"error": str(r.get("error") or "回退失败")}
                break
        finally:
            killer.cancel()
            # 进程留着给紧接着的那一次用。预览之后多半立刻就执行,那一次因此不必再冷读一遍。
            # 执行完(或出错、或进程已死)就收掉,不留常驻。
            if (not dry_run) or out.get("error") or proc.poll() is not None:
                self._rw_drop(sid)
            else:
                with self._rw_lock:
                    ent = self._rw_proc.get(sid)
                    if ent:
                        ent["at"] = time.time()
        return out

    def respond_permission(self, request_id, allow, answers=None, response="", *, resolution="",
                           asks_provider=None):
        """回权限帧。answers 非空时并进 updatedInput——AskUserQuestion 走的就是这条通道:
        它不是「要不要执行」,而是「把人的选择填进入参」。工具自己不问人,它只把入参里的
        answers 转成结果,填人的那一步由界面做(TUI 是就地弹卡,我们是弹在会话里)。
        实测:auto 模式下别的工具一个 control_request 都不发,唯独它照发。"""
        with self._plock:
            proc = self._pending.pop(request_id, None)
            meta = self._pending_ask_meta.pop(request_id, None)
            inp = self._pending_input.pop(request_id, {})
        def resolved(reason):
            if not meta:
                return
            sid = meta['sessionID']
            provider = asks_provider or getattr(self, '_asks_provider', None)
            try:
                asks = provider(sid) if provider else self._pending_ask(sid)
            except Exception:
                try: asks = self._pending_ask(sid)
                except Exception: asks = []
            try:
                self.bus.publish({'type': 'permission.resolved', 'properties': {
                    'sessionID': sid, 'id': request_id, 'resolution': reason,
                    'asks': asks}})
            except Exception:
                pass
        if proc is None or proc.poll() is not None:
            resolved("connection_error")
            return False
        if allow and (answers or response):
            inp = dict(inp)
            if answers:
                inp["answers"] = answers
            # response 是「没对上任何选项、直接说了一句」那一路:工具认它,
            # 结果会写成 The user responded: …,不必勉强把它塞进某个选项。
            if response:
                inp["response"] = response
        resp = {"type": "control_response",
                "response": {"subtype": "success", "request_id": request_id,
                             "response": ({"behavior": "allow", "updatedInput": inp}
                                          if allow else
                                          {"behavior": "deny", "message": "用户在工作台拒绝了该操作"})}}
        try:
            proc.stdin.write(json.dumps(resp, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            resolved(resolution or ("approved" if allow else "denied"))
            return True
        except Exception:
            resolved("connection_error")
            return False

    def _pending_ask(self, sid):
        """这一段会话里还等着人答的权限请求。断线漏掉那一帧之后,靠它把卡片补回来。"""
        with self._plock:
            return [copy.deepcopy(m) for m in self._pending_ask_meta.values()
                    if m.get("sessionID") == sid]

    # ---- 自续轮 ----
    # 引擎按 task-notification 自己起的那一轮。它有内容(读子代理的报告、判断下一步、
    # 有时接着调工具),所以收下来而不是丢掉:解析照旧,只是落在自己那份 ctx 里,带上来路,
    # 不混进用户这一轮的消息。界面怎么呈现是下一步的事,这里先把归属与来路留出来。

    ASIDE_ORIGIN = "task-notification"

    # 预告的时效。自续轮的 init 紧跟通知帧,同一进程里先后写出,实测隔不了几秒;
    # 原值 300 秒是按「宁可留着」定的,而轨迹证明留着只会误伤——用户发下一条消息
    # 往往就在几十秒内,陈旧预告正好套在那一轮的开场上。收到 30 秒,并且回合收尾即清。
    ASIDE_HINT_TTL = 30

    def _aside_agent(self, sid, task_id, status="", rows=None, at=0):
        """这一轮自续是哪个子代理交回引发的。对不上任何子代理时返回 None。

        预告帧里的 task_id 与子代理记录文件名 agent-<id>.jsonl 里的那个 id 是同一个
        ——通知正文里的 <task-id> 也是它,_agent_evidence 正是按这个 id 收通知的。
        注册表的键在有些形态下带前缀(agent: / bash:),取冒号后的那一段。

        后台命令、MCP 任务跑完同样发通知,那些对不上子代理:界面据此换一种说法,
        不要把「一条后台命令跑完了」说成某个子代理交回。

        outcome 优先用 subagents() 那一份(它连收场时刻一起给),拿不到时退回预告帧自报的
        status——记录是边跑边写的,通知那一行偶尔还没落到磁盘上。

        rows 给定时不再自己去取:历史重建里一份记录可能有几十个自续轮,名单取一次就够
        (见 _group_transcript 的 subs)。

        at 是「此刻」——开场那一路给的是通知落进上下文的时刻。收场证据是引擎边跑边写进
        主记录的,通知发到我们手上时它偶尔还没落盘,ended 于是为 0、时长报不出来。
        而通知本身就是子代理交回的那一刻,拿它当 ended 兜底,量级与实际一致;标题因此
        在开场就完整,不必等收场再补一段时长上去。"""
        tid = str(task_id or "").strip().rsplit(":", 1)[-1]
        if not tid:
            return None
        if rows is None:
            try:
                rows = self.subagents(sid)
            except Exception:
                return None
        a = next((x for x in rows if x.get("id") == tid), None)
        if not a:
            return None
        started, ended = int(a.get("started") or 0), int(a.get("ended") or 0)
        if not ended and at and started and at > started:
            ended = int(at)
        return {"id": tid, "task": a.get("task") or "", "kind": a.get("kind") or "",
                "outcome": a.get("outcome") or str(status or ""),
                "started": started, "ended": ended,
                # 子代理自己跑了多久。这条分割线讲的是「某个子代理交回了」,读的人关心的是
                # 那一个,不是引擎读它报告花的那几秒(后者是 row 里的 ms)。
                "ms": max(0, ended - started) if started and ended else 0}

    def _aside_mark_close(self, ctx, obj, mark=0):
        """分界点之后那段追认为自续轮的产出。内容留在本回合,只补一条线与出处。

        与 2026-08-20 撤掉的那一版(_aside_close)的区别在于「东西在哪」:那一版把整段收在影子 ctx 里,
        所以事件要把 parts 整批带过去、前端再铺一遍;这一版内容一直在本回合的 parts 里、
        也一直在 live 快照里(刷新看得见),事件只需说明「从第几块起是子代理交回的」。

        host 因此永远是本回合自己,不再需要跨回合去认上一轮——刷新或对账重建之后
        落盘消息的 id 会换成 uuid、seq 归零,那两把钥匙同时失效,线就画不出来了。

        身份仍取自预告帧(hint):追认这一条是这一段唯一的事件,名字、收场方式、时长都得在
        它里面写全,没有第二条来补。收场证据是引擎边跑边写进主记录的,追认时偶尔还没落盘,
        那时拿通知落进上下文的时刻(hint 的 at)当收场时刻兜底。"""
        order = ctx.get("order") or []
        mark = int(mark or 0)
        pids = order[mark:]
        hint = ctx.get("aside_hint_at") or {}
        task_id = re.sub(r"^(?:agent|bash):", "", str(hint.get("task") or ""))
        returned = next((p for p in reversed(list((ctx.get("parts") or {}).values()))
                         if p.get("type") == "execution-result"
                         and p.get("task_id") == task_id), {})
        row = {"origin": (obj.get("origin") or {}).get("kind") or self.ASIDE_ORIGIN,
               "presentation": "continuation",
               "return_id": returned.get("id") or "",
               "started": 0, "ms": int(obj.get("duration_ms") or 0),
               "task_id": str(hint.get("task") or ""),
               "agent": self._aside_agent(ctx["sid"], hint.get("task"), hint.get("status"),
                                          at=int(float(hint.get("at") or 0) * 1000)),
               "mark": mark, "part_ids": list(pids), "parts": []}
        ctx.setdefault("asides", []).append(row)
        self.bus.publish({"type": "session.aside",
                          "properties": dict(row, sessionID=ctx["sid"], phase="mark",
                                             msg_id=ctx["msg_id"], source=self.SRC_ASIDE,
                                             seq=ctx.get("seq") or 0,
                                             host=ctx["msg_id"])})
        return row

    def aside_frame_opens(self, obj):
        """间隙里哪种帧算一轮的开场(见 cli_turn._persist_idle)。通知帧是常态:它先于引擎
        自续的那条 init 到达,返回卡要建在那一轮自己的消息下。没有通知直接来 init 的也认——
        预告可能已被上一轮消费掉,那一轮照样是引擎自己起的。"""
        return obj.get("type") == "system" and obj.get("subtype") in ("task_notification", "init")

    def _aside_turn_row(self, ctx, obj):
        """间隙里独立收下的那一轮,落盘时挂在 info.aside 上的出处。与 _aside_mark_close 里
        实时追认那条、以及记录投影(claude_transcript 的 flush_aside)那条同构:前端三条路
        走同一段 hsAsideRow,按 return_id 找到返回卡,把这一轮的内容接在卡下面。"""
        if ctx.get("aside_hint_at") is None:
            ctx["aside_hint_at"] = self._aside_hint.pop(ctx["sid"], None)
        hint = ctx.get("aside_hint_at") or {}
        returned = next((p for p in (ctx.get("parts") or {}).values()
                         if p.get("type") == "execution-result"), {})
        return {"origin": (obj.get("origin") or {}).get("kind") or self.ASIDE_ORIGIN,
                "presentation": "continuation",
                "return_id": returned.get("id") or "",
                "started": int(ctx.get("t0") or 0), "ms": int(obj.get("duration_ms") or 0),
                "task_id": str(hint.get("task") or returned.get("task_id") or ""),
                "agent": self._aside_agent(ctx["sid"], hint.get("task") or returned.get("task_id"),
                                           hint.get("status"),
                                           at=int(float(hint.get("at") or 0) * 1000))}

    @staticmethod
    def _api_failure(status, message, retry=None):
        try:
            status = int(status or 0)
        except (TypeError, ValueError):
            status = 0
        code = {401: "AUTH", 403: "FORBIDDEN", 429: "RATE_LIMIT",
                529: "OVERLOADED"}.get(status, "API")
        failure = {"source": "provider", "provider": "claude", "code": code,
                   "message": str(message or "Claude API 请求失败")[:300]}
        if status:
            failure["status"] = status
        if isinstance(retry, dict) and retry:
            failure["retry"] = copy.deepcopy(retry)
        return failure

    def handle_event(self, obj, ctx):
        from .lifecycle import _current, request_failed, request_recovered, commit_request_failure, compact_update
        if not _current(self, ctx):
            return
        t = obj.get("type")
        # 子代理的帧带着 parent_tool_use_id。它们跟主线共用 content index,不拦的话
        # 子代理内部那次 Bash 的入参会按位置落到主线的「调度」那一步上——界面上就是
        # 一步写着「Agent」、入参却是一条 grep 命令(2026-08-08 实测)。
        # 子代理的过程另有出口:它自己那份记录,经 /session/<id>/agents 看。
        if obj.get("parent_tool_use_id"):
            return
        if t in ("assistant", "user"):
            content = (obj.get("message") or {}).get("content") or []
            # SDK messages use string | ContentBlock[]. /compact emits plain
            # strings too; project them as text, never iterate them as tools.
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
        notification = (t == "system" and obj.get("subtype") == "task_notification")
        injected = t == "user" and self._injected_user(obj)
        if notification or injected:
            result = from_notification(obj)
            if result:
                agent = self._aside_agent(ctx["sid"], result.get("task_id"))
                pid = (ctx.get("tuid") or {}).get(result.get("call_id"))
                tool = (ctx.get("parts") or {}).get(pid)
                self._emit_execution_result(ctx, from_notification(obj, agent, tool))
            if injected:
                return
        # ---- 这一帧属于哪一轮 ----
        # 常驻进程是跨回合复用的,而 claude 并不只在我们发话时才跑:后台任务或子代理交回时,
        # harness 往上下文里注入一条 task-notification,引擎就着它自己续一轮——读报告、判断
        # 下一步,有时接着调工具。那是真在干活,不是噪音,只是它不属于用户这一轮。
        #
        # 两种轮次的帧序一样(init … 内容 … result),init 帧的字段也一模一样,分不出来;
        # 分得出来的是 result:自续那一轮带 origin={"kind": "task-notification"},用户发起的
        # 那一轮 origin 为 null(2026-08-12 实测:一个进程里让后台任务交回,同时从这侧发话,
        # 拿到的正是 notification → init → 内容 → result(origin) → init → 内容 → result(null))。
        # 此前只认「见过一条 init」就开闸,自续轮那条 init 于是替用户这一轮把闸开了,紧跟着
        # 它的 result 把回合判完:界面提前翻牌、耗时报的是自续轮的秒数,而用户这一轮真正的
        # 内容再没有人读——刷新之后才从记录里整体出现。
        #
        # 判据因此落在归属上,不落在「见没见过 init」:
        #   1. task_notification 是自续轮的预告,接下来那一轮整个收到一边去(见 _aside_*);
        #   2. 带 origin 的 result 只收自续轮的场,不结束用户这一轮,并把闸重新扣上;
        #   3. 用户这一轮从它自己那条 init 开始,到 origin 为空的 result 为止。
        # 同步派出的子代理不受影响:它阻塞在工具调用里,不产生自续轮(TUI 那侧同样如此)。
        #
        # 只在常驻这条路上设闸:一回合一进程那条路的进程是新起的,管道里不可能有别人的帧,
        # 万一某一轮真的没有 init,也不该让这道闸把回合挂到超时。
        # 已经按下停止的回合也不设:那时等的是引擎对中断的回应,它未必排在 init 之后,
        # 拦掉就没人给回合收尾了。
        if ctx.get("persist") and not ctx.get("aborted"):
            if t == "system" and obj.get("subtype") == "task_notification":
                # 预告帧上有 task_id 与 status:接下来那条 init 是谁交回引发的。init 帧两轮
                # 一模一样,身份只能从这里拿。它未必落在轮次间隙——派出后台子代理的那一轮里,
                # 它常先于本轮 result 到达(2026-08-15 实测),所以任何时刻都要收,且记在
                # 适配器上跨回合传(此前只在轮次间隙收、记在回合自己的 ctx 上,回合一结束
                # 预告就丢了,下一条 init 被错认成用户轮,正是「子代理返回前后发话就错乱」)。
                self._aside_hint[ctx["sid"]] = {"task": obj.get("task_id") or "",
                                                "status": obj.get("status") or "",
                                                "at": time.time()}
                self._bg_done(ctx["sid"], obj.get("task_id") or "")
                return
            if t == "result" and ctx.get("aside_turn"):
                # 间隙里独立收下的自续轮(见 cli_turn._persist_idle):这条 result 就是它自己的
                # 收尾,不追认、不分段,记下出处后按普通 result 收场。
                ctx["aside_row"] = self._aside_turn_row(ctx, obj)
            elif t == "result" and (obj.get("origin") or {}).get("kind"):
                # 分段收尾:这条 result 说明「上一段结束到此刻」这一段是自续轮的产出。
                #
                # 判据落在 result 而不是 init:origin 是引擎明说的,不用猜;而 init 帧两轮
                # 一模一样,从前在那里前瞻性地判归属,判错就是整轮错位,只能等下一条 result
                # 撤回——用户看到的「回答挂到上一回合、要等输出完才跳回来」就是那个撤回。
                # 回溯认定还有一个好处:自续轮在前(通知先到、引擎先自续)还是在后(先答用户),
                # 同一套判据都成立,不必分情形。
                #
                # 内容始终留在本回合的 parts 与 live 快照里,追认只是补一条线与出处,
                # 不搬家、不重铺——刷新时看到的就是最终形态。
                #
                # 追认前先把预告收下:线上那句话是「子代理 “某某” 执行完成返回」,名字只有
                # 预告帧上的 task_id 给得出(见 _aside_agent)。预告多半还挂在适配器上——
                # 本回合的第一条 init 不再消费它,而甲、乙两种真实帧序里这条 result 之前
                # 都没有第二条 init,不在这里取就一直取不到,线只能一律写成「处理后台任务
                # 的返回」。
                if ctx.get("aside_hint_at") is None:
                    ctx["aside_hint_at"] = self._aside_hint.pop(ctx["sid"], None)
                seg = int(ctx.get("seg_start") or 0)
                # 本回合的第一段不追认,哪怕引擎在 origin 里说它是通知起的。
                #
                # 2026-08-20 拿 adp_190349c63f7f48 的逐帧轨迹核过:用户在 9944 帧发了
                # 「子代理返回了吗?」,通知帧 9945~9947 紧跟着落在这一轮的开头,整轮的产出
                # 到 10153 帧收尾,而那条 result 带着 origin=task-notification。按原来的写法,
                # seg_start 还是 0,于是「用户的提问 + 给用户的回答」被整段追认成后台处理
                # ——界面上就是用户实见的那样:内容挂在「处理后台任务的返回」下面。
                #
                # origin 说的是「这一轮是被什么调度起来的」,不是「用户没说话」。通知落在
                # 用户这一轮的头上时两者都成立,它就不再能单独当归属判据。
                # 第一段里一定有用户自己那句话,所以只追认尾段(seg_start > 0,即前面已经
                # 收过一段);整轮没有用户正文的那种(别的引擎可能有)照旧追认。
                if seg > 0 or not (ctx.get("text") or "").strip():
                    self._aside_mark_close(ctx, obj, seg)
                ctx["seg_start"] = len(ctx.get("order") or [])
                ctx["init_seen"] = False    # 闸复位,等这一轮余下那段自己的 init
                ctx.pop("aside_hint_at", None)
                request_recovered(ctx)  # Synthetic error belonged to the aside result, not the user turn.
                return
            if not ctx.get("init_seen"):
                if not (t == "system" and obj.get("subtype") == "init"):
                    return       # 上一轮没读完的残帧:读循环停在 result 处,后面的留在管道里
                # 本回合的第一条 init 永远归本回合自己,不拿预告去套它。
                #
                # 此前这里正相反:一进这个分支就消费预告,而这个分支的前提恰恰是「本回合还没见过
                # init」——那正是用户轮的开场。2026-08-18 用六份逐帧轨迹核过:自续轮态共 3884 帧,
                # 【全部】套在本回合第一条 init 上,「本回合已见过 init、之后又来一条」一次都没有。
                # 成因是引擎收到 task-notification 后【不一定】另起一轮:实测它把通知并进当前这一轮
                # 接着跑,不发新的 init,于是预告没有消费者一直挂着,等用户发下一条消息,那一轮的
                # 第一条 init 撞上它,整轮被收进影子 ctx——界面上用户自己的回答挂到了上一回合下面,
                # 直到 result 的 origin 为空触发撤回才重画。「要等输出完成才正常」说的就是这一段。
                # 现在归属一律等 result 的 origin(见上面的分段收尾),init 只管开闸。
                ctx["init_seen"] = True
            elif t == "system" and obj.get("subtype") == "init":
                # 同一个回合里的第二条 init:引擎又起了一段。它【不参与归属判定】——init 帧
                # 两轮一模一样,拿它猜谁是谁正是从前那些错位的来源。段落归属一律等 result,
                # 那时 origin 是引擎明说的(见下面的分段收尾)。这里只把预告收下备用:
                # 追认时要靠它认出是哪个子代理交回的。
                if ctx.get("aside_hint_at") is None:
                    ctx["aside_hint_at"] = self._aside_hint.pop(ctx["sid"], None)
                return
        if t == "control_request":
            req = obj.get("request") or {}
            rid = obj.get("request_id")
            if req.get("subtype") == "can_use_tool" and rid:
                # 连 sid 与 tool 一起留:permission.asked 只推一次,断线就丢。
                # 注册、回答与 /state 恢复必须在同一把锁下观察一致快照。
                with self._plock:
                    self._pending[rid] = ctx.get("proc")
                    self._pending_input[rid] = req.get("input") or {}
                    self._pending_ask_meta[rid] = {"id": rid, "sessionID": ctx["sid"],
                                                   "tool": req.get("tool_name", "tool"),
                                                   "input": req.get("input") or {}}
                self.bus.publish({"type": "permission.asked", "properties": {
                    "sessionID": ctx["sid"], "id": rid,
                    "tool": req.get("tool_name", "tool"), "input": req.get("input") or {}}})
                _t = threading.Timer(self.PERMISSION_TIMEOUT,
                                     lambda: self.respond_permission(rid, False, resolution="timeout"))
                _t.daemon = True   # 兜底定时器不该拖住进程退出:它一等就是五分钟
                _t.start()
            return
        if t == "result":
            ctx["_close_stdin"] = True
            # 手动压缩的收尾:回合终态是唯一可靠的界线(引擎可能全程不发 status 帧)。
            # 成败不猜,去记录里看这一轮之后有没有落下新的压缩摘要——有就是压过了。
            # 分隔线上的 token 与摘要正文随后由对账按记录补全(见 claude_transcript
            # 里认 isCompactSummary 那一支)。
            if ctx.get("compact_manual"):
                ctx["compact_manual"] = False
                landed = self._compact_landed(ctx.get("esid"), ctx.get("compact_t0") or 0)
                ctx["compact_t0"] = 0
                compact_update(self, ctx, {
                    "sessionID": ctx["sid"], "running": False, "started": 0,
                    "trigger": "manual" if landed else "",
                    "result": "success" if landed else "failed",
                    "error": "" if landed else "引擎没有写下压缩摘要"})
            # 这一回合的用量与耗时:界面把它和「已完成」并进同一行,免得人再去别处找。
            # token 取输入输出之和(含缓存读写,那也是真花掉的上下文);耗时用引擎自报的,
            # 比界面自己掐表准——它不含前端收到首帧之前的那段。
            u = obj.get("usage") or {}
            tok = sum(int(u.get(k) or 0) for k in
                      ("input_tokens", "output_tokens",
                       "cache_creation_input_tokens", "cache_read_input_tokens"))
            ctx["tokens"] = tok
            ctx["cached_tokens"] = int(u.get("cache_read_input_tokens") or 0)
            ctx["_token_usage"] = {
                "total": tok,
                "input": int(u.get("input_tokens") or 0)
                         + int(u.get("cache_creation_input_tokens") or 0)
                         + int(u.get("cache_read_input_tokens") or 0),
                "output": int(u.get("output_tokens") or 0),
                "cached": int(u.get("cache_read_input_tokens") or 0),
                "cache_write": int(u.get("cache_creation_input_tokens") or 0),
            }
            ctx["ms"] = int(obj.get("duration_ms") or 0)
            self.bus.publish({"type": "session.stats", "properties": {
                "sessionID": ctx["sid"], "tokens": tok,
                "ms": int(obj.get("duration_ms") or 0),
                "cost": obj.get("total_cost_usd") or 0}})
            # 引擎自报的失败(密钥失效、连不上、限流)。注意 subtype 在这类失败里照样写着
            # success——实测 401 与 ConnectionRefused 两次都是,据它判会全部漏掉。
            # 看 terminal_reason / api_error_status,正文在 result 里,已经是给人读的一句话。
            # 中断也让引擎把 is_error 置真(常驻下中断不杀进程,这条 result 会真的读到)。
            # 那是用户自己停的,不该在界面上报成故障。
            if (obj.get("is_error") or obj.get("api_error_status")
                    or obj.get("terminal_reason") == "api_error") and not ctx.get("aborted"):
                commit_request_failure(ctx)
                msg = str(obj.get("result") or "引擎报告了一个错误")[:300]
                if not isinstance(ctx.get("engine_failure"), dict):
                    ctx["engine_error"] = msg
                    ctx["engine_failure"] = self._api_failure(
                        obj.get("api_error_status"), msg, ctx.get("retry"))
                if not ctx.get("_engine_error_published"):
                    failure = ctx["engine_failure"]
                    auth = failure.get("code") == "AUTH"
                    self._ctx_env_publish(ctx, {
                        "state": "error", "stage": "auth" if auth else "turn",
                        "label": self._notice_head(failure["message"]),
                        "details": [failure["message"]],
                        **({"action": {"kind": "terminal", "label": "重新登录"}} if auth else {})})
                    self._publish_turn_error(ctx['sid'], ctx, ctx['engine_failure'])
                    ctx["_engine_error_published"] = True
            else:
                request_recovered(ctx)
            ctx["native_terminal"] = {"source": "result",
                "status": "interrupted" if ctx.get("aborted") else
                          "failed" if ctx.get("engine_error") else "completed"}
            return
        if t == "system" and obj.get("subtype") == "retry_budget_expired":
            minutes = max(1, int(obj.get("retry_budget_ms") or 0) // 60000)
            msg = f"Claude 连接重试已达到 {minutes} 分钟上限"
            failure = {"source":"provider", "provider":"claude", "code":"RETRY_TIMEOUT",
                       "message":msg, "retry":copy.deepcopy(ctx.get("retry") or {})}
            ctx["engine_error"] = msg
            ctx["engine_failure"] = failure
            self._ctx_env_publish(ctx, {"state":"error", "stage":"turn", "label":msg})
            self._publish_turn_error(ctx['sid'], ctx, failure)
            ctx["_engine_error_published"] = True
            return
        # 重试保持在 Claude 自己的同一个原生请求内。Shepherd Runner 开启 retry watchdog，
        # 因而服务端 Retry-After 超过 60 秒时也会原地等待；不从外层重新提交 Turn。
        # 每一帧同时写进 live checkpoint，刷新页面后仍能看到等待原因、轮次和剩余时间。
        if t == "system" and obj.get("subtype") == "api_retry":
            now_ms = int(time.time() * 1000)
            delay_ms = max(0, int(obj.get("retry_delay_ms") or 0))
            watchdog = bool(obj.get("retry_watchdog"))
            try:
                budget_left = max(0, int(obj.get("retry_budget_remaining_ms") or 0))
            except (TypeError, ValueError):
                budget_left = 0
            effective_wait = min(delay_ms, budget_left) if watchdog else delay_ms
            retry = {"attempt": int(obj.get("attempt") or 0),
                     # max_retries is the ordinary session budget. Watchdog mode
                     # intentionally continues beyond it until the one-hour cap.
                     "max": 0 if watchdog else int(obj.get("max_retries") or 0),
                     "provider_max": int(obj.get("max_retries") or 0),
                     "watchdog": watchdog, "delay_ms": effective_wait,
                     "deadline_at": now_ms + effective_wait,
                     "budget_deadline_at": now_ms + budget_left if watchdog else 0,
                     "status": obj.get("error_status"),
                     "error": str(obj.get("error") or "")[:120]}
            from .lifecycle import retry_update
            retry.update(provider="claude", source="native", signal="system.api_retry")
            retry_update(self, ctx, retry)
            return
        # 压缩上下文:整段过程一帧内容都不发,实测五万 token 要 90 秒。界面上分不出
        # 「在压缩」和「卡住了」,引擎在这里报了始末,据它单独显示一条进度。
        #
        # 收尾那一帧的形态(2026-08-26 实测 CLI 2.1.245,-p --output-format stream-json):
        #   {"subtype":"status","status":null,"compact_result":"failed",
        #    "compact_error":"Not enough messages to compact."}
        # 只有成败,没有压缩前后的 token,也没有 trigger——那些写进了 rollout 的
        # compact_boundary 记录,但那条记录不经 stdout(见下面那一支的说明)。
        # compact_error 原来整个丢掉了,压缩失败在界面上与压缩成功长得一模一样。
        if t == "system" and obj.get("subtype") == "status":
            on = obj.get("status") == "compacting"
            # 手动 /compact 的始末由我们自己记(见 init 那一支),引擎这边的 status 帧不再
            # 当依据。2026-09-04 实测 2.1.260:一次 210 秒的手动压缩全程一帧 status 也没发
            # (紧挨着的上一次自动压缩已经 announce 过,引擎不重复 announce);而那次自动
            # 压缩的 compact_result=failed 恰好落在这一轮开头,照原来的写法会被当成本轮的
            # 结果——刚开的压缩态当场被收掉,界面上报一次并不存在的「压缩失败」。
            if ctx.get("compact_manual"):
                return
            was = ctx.get("compact_t0") or 0
            ctx["compact_t0"] = int(time.time() * 1000) if on else 0
            # 只有两种帧算压缩事件:正在压缩,以及结束我们见过的那一次压缩。
            # status 帧不止在压缩时发(每回合开头还有一条 status=requesting),而
            # compact_result 在后续帧里可能仍带着上一次的值;不加这道配对,压缩结束很久
            # 之后还会再发一次「收尾」,前端据它又画一条分隔线,界面上就多出一条光秃的线
            # (2026-08-26 用户实见「压缩完出现两条线,刷新后恢复」)。
            # 失败那一次也照样先发 compacting(见 tests/test_compact_status_frame.py 抓到的
            # 原始帧),所以按「见过开始」配对不会把失败漏掉。
            if not on and not was:
                return
            if not on:
                from .session_naming import invalidate
                invalidate(self, ctx['sid'])
            compact_update(self, ctx, {
                "sessionID": ctx["sid"], "running": on,
                "started": ctx.get("compact_t0") or 0,
                "result": obj.get("compact_result") or "",
                "error": str(obj.get("compact_error") or "")[:200]})
            return
        # 2026-08-26 实测:claude 的 stream-json 从不发这一帧,压缩的始末全在上面那条
        # system/status 里。compact_boundary 只出现在 rollout 记录中(它带 trigger 与
        # preTokens/postTokens,重建那条路由 messages() 认领,见 compactMetadata 那一支)。
        # 留着不删——它是引擎侧的事件契约,换个版本可能就发了,发了就该按更全的那份用。
        if t == "system" and obj.get("subtype") == "compact_boundary":
            from .session_naming import invalidate
            invalidate(self, ctx["sid"])
            md = obj.get("compact_metadata") or {}
            ctx["compact_t0"] = 0
            compact_update(self, ctx, {
                "sessionID": ctx["sid"], "running": False, "started": 0,
                "pre": md.get("pre_tokens") or 0, "post": md.get("post_tokens") or 0,
                "ms": md.get("duration_ms") or 0, "trigger": md.get("trigger") or ""})
            return
        if t == "system" and obj.get("subtype") == "init":
            # claude 每回合返回一个新的 session_id,滚动更新。同时记进 ctx:这一回合的产出
            # 属于这个引擎会话,分支就是拿它去 resume,所以要随助手消息一起落盘。
            ctx["esid"] = obj.get("session_id")
            self._set_engine_sid(ctx["sid"], obj.get("session_id"))
            # 手动 /compact:claude 没有压缩的控制接口(不像 codex 的 thread/compact/start),
            # 这一句只能当普通提示词发进去,于是「这一轮是压缩」全靠引擎自己 announce。
            # 它不 announce 的时候,界面上敲完 /compact 就什么都没有——没有进度条,压缩完
            # 也没有分隔线,人分不出压过了还是卡住了(2026-09-04 用户实见,压缩实际跑了
            # 三分半)。人敲的是什么 Desk 自己知道,据它开压缩态,不等引擎开口。
            if _is_compact_cmd(ctx.get("text")):
                ctx["compact_manual"] = True
                ctx["compact_t0"] = int(time.time() * 1000)
                compact_update(self, ctx, {
                    "sessionID": ctx["sid"], "running": True,
                    "started": ctx["compact_t0"], "trigger": "manual"})
            # 引擎自报的斜杠命令(内置命令 + 技能 + 自定义命令,实测 147 条)。
            # 界面原来只有七条本地动作,引擎那批一条都没有,/goal 这类只能靠盲打。
            sl = obj.get("slash_commands")
            if sl:
                self._save_slash(sl)
        elif t == "stream_event":
            ev = obj.get("event") or {}
            et = ev.get("type")
            idx = ev.get("index")
            if et == "message_start":
                # 本回合第几条 API 消息。这个计数器**不能**写进 ctx["seq"]——那是
                # 回合序号(cli_turn._seq_next 给的毫秒时刻,全局单调),recap 生成完
                # 之后要拿它与适配器里的 _turn_seq 比对,判断「期间有没有来新回合」。
                # 2026-09-01 之前两者同名:claude 一回合发几条 API 消息,回合序号就被
                # 顶高几,复核永远不等,recap 每次生成完 200 多字再被当成过期丢掉——
                # 而丢弃是正常分支,不报错,所以整条链完全静默(claude 46 段 0 条 recap)。
                ctx["msg_seq"] = ctx.get("msg_seq", 0) + 1
                m = ((ev.get("message") or {}).get("model")) or None
                if m:
                    # 回合署名只使用本轮原生自报，不能从会话最近模型反推。
                    ctx["actual_model"] = m
                    # 带上本轮起始时刻:人中途换过模型的话,这一轮报的是旧模型,不作数
                    self.set_session_model(ctx.get("sid"), m, since=ctx.get("t0"))
            pid = f'{ctx["msg_id"]}_m{ctx.get("msg_seq", 0)}_p{idx}'
            if et == "content_block_start":
                cb = ev.get("content_block") or {}
                if cb.get("type") == "text":
                    # Claude 没有 Codex 的 commentary/final phase。它提供的可靠边界是
                    # 一条原生 assistant message:只要这条 message 后面出现 tool_use,
                    # 其中的可见文字就是执行前说明,应进过程区；没有工具的终结 message
                    # 才是主线正文。tool_use 常晚于 text 到达,所以这里同时支持先标记后建块。
                    ptype = ("reasoning" if (ctx.get("claude_tool_msgs") or {}).get(
                        ctx.get("msg_seq", 0)) else "text")
                    self._ensure_part(ctx, pid, ptype)
                elif cb.get("type") == "thinking":
                    pass   # 等首个非空 thinking_delta 再建块(opus-4-8 只回签名、内容为空,不建空块)
                elif cb.get("type") == "redacted_thinking":
                    part = self._ensure_part(ctx, pid, "reasoning", text="")
                    note = "（思考内容由安全策略加密，不可显示）"
                    part["text"] = note
                    self._delta(ctx, pid, "reasoning", note)
                elif cb.get("type") == "tool_use":
                    msg_seq = ctx.get("msg_seq", 0)
                    ctx.setdefault("claude_tool_msgs", {})[msg_seq] = True
                    # text delta 已经可以先画出来；工具块出现时用同一 part id 原位迁入
                    # 过程区。前端对 text/reasoning 的类型切换会摘掉旧节点后重绘。
                    prefix = f'{ctx["msg_id"]}_m{msg_seq}_p'
                    for old_pid in ctx["order"]:
                        old = ctx["parts"].get(old_pid)
                        if (old_pid.startswith(prefix) and old
                                and old.get("type") == "text"):
                            old["type"] = "reasoning"
                            self._part_updated(ctx, old)
                    part = self._ensure_part(ctx, pid, "tool", tool=cb.get("name", "tool"),
                                             state={"status": "running", "input": {}})
                    ctx["last_tool"] = pid
                    # 记下引擎给这次调用的 id:整消息快照按 id 认块,不靠位置猜
                    ctx.setdefault("tuid", {})[cb.get("id") or pid] = pid
                    part["call_id"] = cb.get("id") or pid
                    self._part_updated(ctx, part)
            elif et == "content_block_delta":
                d = ev.get("delta") or {}
                if d.get("type") == "text_delta":
                    ptype = ("reasoning" if (ctx.get("claude_tool_msgs") or {}).get(
                        ctx.get("msg_seq", 0)) else "text")
                    part = self._ensure_part(ctx, pid, ptype)
                    part.setdefault("text", "")
                    part["text"] += d.get("text", "")
                    self._delta(ctx, pid, ptype, d.get("text", ""))
                elif d.get("type") == "thinking_delta":   # 扩展思考:明文逐字渲染;opus-4-8 此模式内容为空,跳过
                    txt = d.get("thinking", "")
                    if txt:
                        part = self._ensure_part(ctx, pid, "reasoning", text="")
                        part["text"] = part.get("text", "") + txt
                        self._delta(ctx, pid, "reasoning", txt)
                elif d.get("type") == "input_json_delta":
                    # 工具入参是逐片流过来的 JSON 文本。以前只靠回合末的整消息快照回填,
                    # 快照要是没对上(见下面按 id 认块的注释),这一步就只剩一个空壳:
                    # 界面上是没有宾语的「已执行」,展开区是一个光秃秃的 $。
                    # 这里边流边攒,块收尾时解析一次,不依赖快照。
                    ctx.setdefault("ijson", {})
                    ctx["ijson"][pid] = ctx["ijson"].get(pid, "") + (d.get("partial_json") or "")
                # signature_delta:思考块的加密签名(多轮一致性用),非展示内容,忽略
            elif et == "content_block_stop":
                raw = (ctx.get("ijson") or {}).pop(pid, "")
                part = ctx["parts"].get(pid)
                if raw and part is not None and part.get("type") == "tool":
                    try:
                        val = json.loads(raw)
                    except ValueError:
                        val = None
                    if isinstance(val, dict) and val:
                        part["state"]["input"] = val
                        self._part_updated(ctx, part)
        elif t == "assistant":
            if not obj.get("isApiErrorMessage") and not obj.get("apiErrorStatus"):
                request_recovered(ctx)
                from .lifecycle import retry_clear
                retry_clear(self, ctx)
            # Agent SDK 把鉴权失败等 API 错误作为 synthetic assistant 消息发出，随后才给
            # failed result。它没有 stream_event 文本增量；若只处理工具快照，界面最终只剩
            # 通用的“回合未完成”。保留原生错误正文，同时让回合状态行与可展开错误条都可见。
            if obj.get("isApiErrorMessage") or obj.get("apiErrorStatus"):
                msg = "\n".join(str(block.get("text") or "") for block in content
                                  if isinstance(block, dict) and block.get("type") == "text").strip()
                msg = (msg or "Claude API 请求失败")[:300]
                # Synthetic assistant describes one request. Only the main
                # result commits its outcome; retry watchdog failures stay intact.
                request_failed(ctx, self._api_failure(
                    obj.get("apiErrorStatus"), msg, ctx.get("retry")))
                return
            # 整消息快照:补全工具输入等(文本已走 delta)。
            # 按 tool_use 的 id 认块,位置只作兜底:快照里的 seq 是「到目前为止第几条 API 消息」,
            # 而快照本身并不保证紧跟在它那条消息的 message_stop 之后——权限询问会把流打断,
            # 下一条 message_start 可能先到,seq 就已经加过了,按位置拼出来的 pid 指向别处,
            # 于是那次调用的入参永远没人填。实测这样丢过 21 次(都是需要批准的命令)。
            for i, c in enumerate(content):
                if c.get("type") != "tool_use":
                    continue
                pid = (ctx.get("tuid") or {}).get(c.get("id")) \
                    or f'{ctx["msg_id"]}_m{ctx.get("msg_seq", 0)}_p{i}'
                if pid in ctx["parts"] and (c.get("input") or {}):
                    ctx["parts"][pid]["state"]["input"] = c.get("input") or {}
                    self._part_updated(ctx, ctx["parts"][pid])
        elif t == "user":
            # 结果只能按调用 ID 关联，不能把并发或乱序结果写到最近一次调用。
            has_tool_result = False
            for c in content:
                if c.get("type") == "tool_result":
                    has_tool_result = True
                    pid = (ctx.get("tuid") or {}).get(c.get("tool_use_id"))
                    part = ctx["parts"].get(pid)
                    if part is None:
                        continue
                    chunks = c.get("content")
                    if isinstance(chunks, list):
                        out = "\n".join(x.get("text", "") for x in chunks if isinstance(x, dict))
                    else:
                        out = str(chunks or "")
                    part["call_id"] = c.get("tool_use_id")
                    part["state"]["output"] = out
                    part["state"]["status"] = "error" if c.get("is_error") else "done"
                    # 通知可能先于这条结果到达,那次回填是暂判;结果落地了就复核一次,
                    # 同步跑完的命令在这里把「已返回」撤回去。
                    settle_return(part)
                    self._part_updated(ctx, part)
                    self._bg_note(ctx["sid"], out)
            if has_tool_result:
                # 工具结果把两次 assistant message 隔开。真实流随后会发新的
                # message_start；这里也显式复位，使缺省略该帧的兼容输入不会把工具后的
                # 终结正文继续当成上一条工具消息的过程说明。
                (ctx.get("claude_tool_msgs") or {}).pop(ctx.get("msg_seq", 0), None)
