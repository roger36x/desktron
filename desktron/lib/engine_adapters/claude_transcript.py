# 由 engine_adapters.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)
import json
import os
import re as re_mod
import subprocess
import threading
import time
from .bus import SESSION_WINDOW, clean_session_title, strip_scene_envelope
from .execution_returns import (from_notification, append_result, attach_return,
                                find_tool_part, handed_off, settle_return)


class ClaudeTranscriptMixin:

    def running_agents(self, esid):
        """这段引擎会话此刻有几个子代理在跑。判据一概复用上面那份,这里只做计数。

        入参是引擎会话 uuid 而不是 Desk 会话 id:会话列表手里只有前者,而终端那侧开的会话
        本来就不在适配器索引里(索引记的是 GUI 会话)。索引里查得到时把进程实例那一维
        一并带上;查不到就传 0,那一维不参与(见 _agent_status 的 proc_started)。"""
        sid = self._sid_by_esid(esid)
        epoch = int(self.proc_instance(sid).get("started") or 0) if sid else 0
        return sum(1 for a in self._subagents_of(esid, epoch) if a.get("status") == "running")

    def _subagents_of(self, esid, epoch):
        tpath = self._transcript_path(esid)
        d = os.path.join(os.path.dirname(tpath), esid, "subagents")
        try:
            names = [x for x in os.listdir(d) if x.startswith("agent-") and x.endswith(".jsonl")]
        except OSError:
            return []
        out = []
        for n in names:
            path = os.path.join(d, n)
            try:
                st = os.stat(path)
            except OSError:
                continue
            info = self._subagent_head(path)
            # 身份取同名的 .meta.json:里面是引擎自己写的 agentType 与 description
            # (正是派发时给的那句短描述),还有回指父工具调用的 toolUseId。
            # 早先只能拿首条提示词的头一行凑,既长又不准。
            meta = {}
            try:
                with open(path[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as f:
                    meta = json.load(f) or {}
            except Exception:
                meta = {}
            out.append({"id": n[len("agent-"):-len(".jsonl")],
                        "kind": meta.get("agentType") or "",
                        "tool_use_id": meta.get("toolUseId") or "",
                        "task": meta.get("description") or info.get("task") or "",
                        "last": info.get("last") or "",
                        "started": info.get("started") or int(st.st_mtime * 1000),
                        "updated": int(st.st_mtime * 1000),
                        "size": st.st_size})
        ev = self._agent_evidence(tpath, {a["tool_use_id"]: a["id"] for a in out if a["tool_use_id"]})
        for a in out:
            a.update(self._agent_status(a["id"], a["tool_use_id"], ev, a["updated"],
                                        a["started"], epoch))
        out.sort(key=lambda x: x["started"])
        return out

    # 「没等到收场的证据」不等于「还在跑」。进程被杀、会话被压缩把那几帧挤掉、通知落进了
    # 另一份记录,都会让证据永远等不来,而按证据判就会把它一直算作在跑——实见一批 33 天前的
    # 子代理全判成 running,界面一边写着「无响应 47640 分钟」一边算它活着,入口上挂出
    # 「33 个代理运行中」。所以再加一道旁证:子代理自己的记录多久没动。一次大扫描安静两三
    # 分钟是常事,半小时不写一个字则不像还在干活,这时宁可交回「不确定」,让调用方另想办法。
    # 这是估计,不是事实。补上进程实例那一维(见 proc_instance)之后它退成真正的兜底:
    # 进程还活着、只是通知丢了的那种情形才轮到它。
    _AG_RUN_MAX_IDLE = 30 * 60 * 1000

    # 进程实例这一维的余量。子代理由进程派出,它的起始时刻必然晚于进程自己的起始时刻,
    # 严格比较本来就够。留一分钟是给系统对时留的:NTP 往回校一下,两个时刻的先后就可能反过来,
    # 而这一维一旦判错就是把在跑的代理判死。落进余量里的那一分钟退回原有判据,代价只是
    # 多等一会儿由静默兜底接手。
    _AG_PROC_GRACE = 60 * 1000

    # 「恢复后又跑起来」这一维的余量。一个子代理可以被恢复:给已经交回的它再发一条消息,
    # 它接着往下跑,而上一轮那条完成通知仍然留在主记录里(通知正文里那句 note 也写着
    # the same task-id may notify more than once)。判据是它自己的记录在最后一条通知之后
    # 又动过,而两个时刻来自两处不同来源:通知时刻取主记录里那一行的 timestamp,记录变动
    # 时刻取子代理记录文件的 mtime。本机 201 个样本量过一遍,mtime 一律不晚于通知时刻,
    # 超出的最大值 0.08 秒(交回的顺序本来就是先写完自己的记录、再注入通知)。十秒是这个
    # 量级的一百多倍,足够吃掉写入先后带来的抖动;detection 的代价也只是恢复之后最多晚
    # 十秒才在界面上现身,比进程实例那一维的一分钟合适——那一维判错是把在跑的判死,
    # 宁可多等;这一维判错只是晚一点显示。
    _AG_NOTIF_GRACE = 10 * 1000

    @classmethod
    def _agent_status(cls, aid, tuid, ev, updated=0, started=0, proc_started=0):
        """一个子代理此刻的状态,以及它是怎么收的场。

        三个字段:status 只有 running / done / 空,空表示判不出——主记录里找不到这个子代理的
        任何痕迹(派它出来的那一轮被撤回过,或是别的子代理派出的下一级),或是有派发记录却
        既等不到收场、记录也早就不动了;这时由调用方另想办法。outcome 是收场的方式
        (completed / stopped / killed / gone),done 才有;ended 是收场时刻。

        proc_started 是这条会话当前引擎进程实例的起始时刻(见 proc_instance)。子代理活在
        进程的事件循环里,派它出来的进程没了它必然跟着没——所以起始早于当前实例的那些,
        不需要任何推断就能判定不在跑,收场方式记 gone(进程已换,没等到它自己的收场证据)。
        为 0 表示 Desk 手里没有这条会话的进程记录,这一维不参与,照原有判据走。

        四维的优先次序:先看有没有通知,有就再看它是不是被恢复后又跑起来了(见
        _AG_NOTIF_GRACE);没有通知才轮到同步/异步那一路的返回;时间窗只在前两样都判不出
        「已收场」时兜底;进程实例摆在最后,凡是没判成 done 的一律再过一遍,所以一个被恢复
        又跑起来、却属于已经消失的进程实例的子代理,仍然判死(outcome=gone)——那个进程没了
        它必然跟着没,恢复的痕迹只是旧记录。"""
        st, outcome, at = "", "", 0
        n = ev["notif"].get(aid)
        r = ev["res"].get(tuid)
        if n and updated and updated - n[1] > cls._AG_NOTIF_GRACE:
            # 最后一条通知之后它自己的记录又动过,说明它被恢复、正在接着跑。见到通知就判
            # done 会让这种代理一直报「执行完成」——实见 2026-08-12 恢复的一个子代理,接口给
            # 的是 done/completed 而它确实在跑,界面那一栏因此不出现(入口只在有 running
            # 时显示)。这一支交给静默兜底:又跑起来但此后长期不动的,仍然不认它在跑。
            idle = int(time.time() * 1000) - updated
            st = "" if idle > cls._AG_RUN_MAX_IDLE else "running"
        elif n:
            # 跑完时 harness 往主记录里注入一条通知,带 task-id 与 status。这是最准的一手。
            # 取最后一条:同一个 task-id 可以通知多次,收场方式以最近那一次为准
            # (实见同一个子代理先 completed 后 stopped,也见过反过来的)。
            st, outcome, at = "done", n[0], n[1]
        elif r and not r[0]:
            # 同步派出的那一路没有通知:父回合那次 Agent 调用直接返回整份报告,返回即交回。
            st, outcome, at = "done", "completed", r[1]
        elif r or tuid in ev["calls"]:
            # 调用还挂着(同步),或者只拿到一句「已在后台派出」(异步)——两者都还在跑。
            idle = int(time.time() * 1000) - (updated or 0)
            st = "" if (updated and idle > cls._AG_RUN_MAX_IDLE) else "running"
        # 收场证据在手就按证据走:它连收场方式和时刻一起给,比这一维讲得清楚。进程实例只管
        # 一件事——把「证据永远等不来、于是永远挂在 running」这个错消掉。
        if st != "done" and proc_started and started and started < proc_started - cls._AG_PROC_GRACE:
            st, outcome = "done", "gone"
        return {"status": st, "outcome": outcome, "ended": at}

    _AG_TASK_ID = re_mod.compile(r"<task-id>\s*([0-9a-zA-Z_-]{4,64})\s*</task-id>")
    _AG_STATUS = re_mod.compile(r"<status>\s*([a-z_]{2,24})\s*</status>")

    def _agent_evidence(self, path, ids, _cache={}):
        """主记录里关于子代理的三类事实。ids 是 {父工具调用 id: 子代理 id}。

        calls:派它出去的那次 Agent 调用出现过;
        res:那次调用的返回,(是不是「已在后台派出」那句占位, 时刻);
        notif:跑完时注入的 task-notification,取最后一条的 (status, 时刻)。

        记录只增不改,按上次读到的位置续读——整份 62MB 扫一遍要 0.3 秒,而这个接口每几秒
        问一次。文件变短说明被重写过,那时才从头来。

        收证据时**不按当前名单筛**:每派出一个新子代理就整份重扫一次的话,一段长会话里
        每次派活都要付 0.8 秒(最大的一份 4 秒),侧栏因此明显发涩(2026-08-14 用户实见)。
        改成把所有 Agent 调用与它们的返回一并收下,缓存对以后出现的任何 id 都成立,
        续读的位置就再没有作废的理由。多存的是几十个 id,可以忽略。"""
        try:
            size = os.path.getsize(path)
        except OSError:
            return {"calls": set(), "res": {}, "notif": {}}
        c = _cache.get(path)
        if not c or c["off"] > size:
            c = {"off": 0, "ids": set(), "calls": set(), "res": {}, "notif": {}}
        c["ids"] |= set(ids)
        if c["off"] == size:
            return c
        # 二进制读:续读的断点要精确到字节,才能既不重读也不漏读。按行累加字节数记位置,
        # 半行(还没写完的那一行)留到下一趟——记录是边跑边追加的,末尾常常正写到一半。
        # 行级预筛:先用固定标记挑出可能有用的行,再解析。tool_result 那一路按已收下的调用 id
        # 认——调用行总排在它的返回之前,扫到返回时该 id 已经在 calls 里了。
        _TOK_CALL = (b'"Agent"', b'"Task"')
        _TOK_NOTIF = b"task-notification"
        try:
            with open(path, "rb") as f:
                f.seek(c["off"])
                off = c["off"]
                for ln in f:
                    if not ln.endswith(b"\n"):
                        break
                    off += len(ln)
                    if not (_TOK_NOTIF in ln or any(t in ln for t in _TOK_CALL)
                            or (b'"tool_result"' in ln
                                and any(cid.encode() in ln for cid in c["calls"]))):
                        continue
                    try:
                        o = json.loads(ln.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    at = int(self._iso_ms(o.get("timestamp")) or 0)
                    txt = self._notification_text(o)
                    if txt is not None:
                        m = self._AG_TASK_ID.search(txt)
                        s = self._AG_STATUS.search(txt)
                        if m:
                            # 收下时不按名单筛:同一份通知里 task-id 才是子代理的身份,
                            # 而扫到这一行时名单未必已经认得它。取的时候按 id 对就行。
                            # 同一个 task-id 会通知不止一次(子代理可以被恢复后接着跑,通知
                            # 正文里那句 note 明说了这一点),留最后一条。按时刻比,不按扫到的
                            # 先后:记录被压缩重写过、或旁支合并进来时,行的顺序未必就是时序。
                            prev = c["notif"].get(m.group(1))
                            if not prev or at >= prev[1]:
                                c["notif"][m.group(1)] = (s.group(1) if s else "completed", at)
                        continue
                    body = (o.get("message") or {}).get("content")
                    for b in body if isinstance(body, list) else []:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "tool_use" and str(b.get("name") or "") in ("Agent", "Task"):
                            if b.get("id"):
                                c["calls"].add(b["id"])
                        elif b.get("type") == "tool_result" and b.get("tool_use_id") in c["calls"]:
                            txt = self._raw_text(b)
                            # 异步派出时返回的不是报告,而是一句占位,正文里写着它的 agentId。
                            # 认这一句才能把「派出去了」和「交回了」分开——只看有没有返回,
                            # 异步那一路会在派出的一瞬间就被判成已完成。
                            asy = "Async agent launched" in txt[:400] or "agentId: " in txt[:400]
                            c["res"][b["tool_use_id"]] = (asy, at)
                c["off"] = off
        except OSError:
            return c
        _cache[path] = c
        return c

    @classmethod
    def _notification_text(cls, o):
        """harness 注入的 task-notification 正文；不是通知就返回 None。

        通知有两种落盘形态。回合间隙里交回时是一条注入的 user 条目；回合进行中交回时，
        CLI 把它并进当前回合，记成 attachment(queued_command, commandMode=task-notification)，
        不再另写 user 条目。只认前一种，会把后一种子代理一直算作在跑，侧栏持续显示
        「子代理工作中」，直到 30 分钟静默兜底(2026-10-01 实见)。
        """
        if o.get("type") == "user":
            return cls._raw_text(o) if cls._injected_user(o) else None
        if o.get("type") == "attachment":
            a = o.get("attachment") or {}
            if (a.get("type") == "queued_command"
                    and (a.get("commandMode") == "task-notification"
                         or (a.get("origin") or {}).get("kind") == "task-notification")):
                prompt = a.get("prompt")
                if isinstance(prompt, list):
                    prompt = "".join(x.get("text") or "" for x in prompt if isinstance(x, dict))
                return prompt if isinstance(prompt, str) else None
        return None

    @staticmethod
    def _raw_text(o):
        """一条记录(或一个内容块)里的纯文本,原样拼接,不做截断也不动换行——
        通知与工具返回都要按正文里的标签认,_plain 那种「取最后一段」的口径在这里不够用。"""
        c = o.get("content") if "content" in o else ((o.get("message") or {}).get("content"))
        if isinstance(c, str):
            return c
        return "".join(x.get("text") or "" for x in c or []
                       if isinstance(x, dict) and x.get("type") == "text")

    def _subagent_head(self, path, _cache={}):
        """名字与派它出来的那句话取自首条;最新一步取自末尾。
        文件只增不改,首条那部分按 path 缓存一次就够。"""
        try:
            size = os.path.getsize(path)
        except OSError:
            return {}
        head = _cache.get(path)
        if head is None:
            head = {}
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for ln in f:
                        try:
                            o = json.loads(ln)
                        except ValueError:
                            continue
                        # 显示名取派给它的那句话的头一行。slug 是会话级的随机串
                        # (同一段会话里所有子代理都一样),认不出谁是谁;
                        # 回指父工具调用那条线(sourceToolAssistantUUID)实测常为空,不靠它。
                        first = self._plain(o).strip()
                        head = {"slug": o.get("slug") or "", "started": self._iso_ms(o.get("timestamp")),
                                "task": (first.split("。")[0].split(". ")[0] or first)[:60]}
                        break
            except OSError:
                return {}
            _cache[path] = head
        last = ""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                if size > 200_000:
                    f.seek(size - 200_000)
                    f.readline()
                for ln in f:
                    try:
                        o = json.loads(ln)
                    except ValueError:
                        continue
                    if o.get("type") != "assistant":
                        continue
                    t = self._plain(o)
                    if t:
                        last = t[:90]
        except OSError:
            pass
        return dict(head, last=last)

    @staticmethod
    def _plain(o):
        """一条记录里可读的那一句:正文优先,其次工具名。"""
        c = ((o.get("message") or {}).get("content"))
        if isinstance(c, str):
            return c.strip().replace("\n", " ")
        txt, tool = "", ""
        for b in c or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and (b.get("text") or "").strip():
                txt = b["text"].strip().replace("\n", " ")
            elif b.get("type") == "tool_use" and not tool:
                tool = str(b.get("name") or "")
        return txt or (("调用 " + tool) if tool else "")

    # 后台任务(Bash run_in_background)与子代理跑完时,harness 会往上下文里塞一条
    # <task-notification>:任务 id、输出文件、状态、摘要,子代理那种还带上整份返回正文。
    # 它在记录里是一条普通的 type=user 条目,不带 isMeta,所以此前那几道过滤都放它过去了,
    # 界面上就出现一个谁也没说过的用户气泡,里面是几千字的尖括号标签(2026-08-12 实见,
    # 紧跟在自动压缩那条分隔线之后)。这类通知的来路由 origin.kind 标明,据此认。
    _INJECT_ORIGINS = ("task-notification",)

    @classmethod
    def _injected_user(cls, o):
        """一条 type=user 的记录是不是 harness 注入的通知,而不是真人发言。

        先认 origin.kind——它是引擎自己写下的来路,不会误伤用户粘贴的尖括号内容;
        再退到正文前缀兜底,给不写 origin 的旧版本留一条路。两道判据都要求整条以
        <task-notification> 开头,用户把这段标签当正文发出来的可能可以忽略。
        """
        if ((o.get("origin") or {}).get("kind") or "") in cls._INJECT_ORIGINS:
            return True
        c = (o.get("message") or {}).get("content")
        if isinstance(c, list):
            txt = "".join(x.get("text") or "" for x in c
                          if isinstance(x, dict) and x.get("type") == "text")
        else:
            txt = c if isinstance(c, str) else ""
        return txt.lstrip().startswith("<task-notification>")

    # 意外退出后取证时最多读取记录末尾这么多字节。一轮正常对话远小于此；超出时只看窗口内
    # 的尾部状态，判不出起点就返回 None，交给“写入进程已退出”这一层兜底。
    _OUTCOME_TAIL_BYTES = 16 * 1024 * 1024

    @classmethod
    def _human_text(cls, o):
        """真人发出的一条 user 记录的正文；工具结果、注入通知、元信息与压缩摘要返回 None。"""
        if o.get("type") != "user" or o.get("isMeta") or o.get("isCompactSummary"):
            return None
        if cls._injected_user(o):
            return None
        content = (o.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                return None
            text = "".join(b.get("text") or "" for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
        else:
            return None
        text = text.strip()
        if not text or text.startswith(("<local-command-", "<command-name>", "<command-message>")):
            return None
        return text

    def native_turn_outcome(self, sid, receipt):
        """按 Claude 对话记录判断某一轮在意外退出前是否已经正常结束。

        回执的 message_id 带着 Desk 落下用户消息的毫秒时刻；记录里此后第一条真人消息就是
        这一轮的起点。起点之后最后一条助手消息以 end_turn/stop_sequence 结束、且没有尚未
        返回结果的工具调用，判 completed；最后一条是 CLI 写入的 API 错误，判 failed；
        否则 interrupted。记录里找不到这一轮(窗口已覆盖
        起点)说明请求没进入引擎，同样判 interrupted。判不出时返回 None。
        """
        m = re_mod.match(r"msg_u_(\d+)$", str(receipt.get("message_id") or ""))
        with self._lock:
            esid = str(receipt.get("engine_session_id") or (self.index.get(sid) or {}).get("engine_sid") or "")
        if not m or not esid:
            return None
        sent_ms = int(m.group(1))
        start_ms = sent_ms - 5000       # 只用于判断窗口是否覆盖起点
        path = self._transcript_path(esid)
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                f.seek(max(0, size - self._OUTCOME_TAIL_BYTES))
                raw = f.read()
        except OSError:
            return None
        lines = raw.split(b"\n")
        if size > self._OUTCOME_TAIL_BYTES:
            lines = lines[1:]          # 第一行可能被截断
        rows, covered = [], size <= self._OUTCOME_TAIL_BYTES
        for ln in lines:
            if not ln.strip():
                continue
            try:
                o = json.loads(ln.decode("utf-8", "replace"))
            except ValueError:
                continue
            at = int(self._iso_ms(o.get("timestamp")) or 0)
            if at and at < start_ms:
                covered = True
                continue
            rows.append((at, o))
        # 起点：Desk 落盘之后的第一条真人发言。上一轮的中断标记可能落在容差之内，不能当起点。
        def opens_turn(at, o):
            text = self._human_text(o)
            return (text is not None and not text.startswith("[Request interrupted by user")
                    and (not at or at >= sent_ms - 2000))
        begin = next((i for i, (at, o) in enumerate(rows) if opens_turn(at, o)), None)
        if begin is None:
            return "interrupted" if covered else None
        pending, last_stop, interrupted, api_error = set(), "", False, False
        answered = False
        for _at, o in rows[begin + 1:]:
            text = self._human_text(o)
            if text is not None:
                if text.startswith("[Request interrupted by user"):
                    interrupted = True
                    continue
                if not answered:
                    continue            # 同一次发言被拆成多条(如图片说明)，仍属本轮输入
                break                   # 下一轮真人消息：本轮到此为止
            blocks = (o.get("message") or {}).get("content")
            blocks = blocks if isinstance(blocks, list) else []
            if o.get("type") == "assistant":
                answered = True
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                        pending.add(b["id"])
                last_stop = str((o.get("message") or {}).get("stop_reason") or "")
                # CLI 把连接中断等 API 错误写成一条 <synthetic> 助手消息，结束原因同样是
                # stop_sequence；它表示这一轮失败，不是正常收尾。
                api_error = bool(o.get("isApiErrorMessage")
                                 or (o.get("message") or {}).get("model") == "<synthetic>")
            elif o.get("type") == "user":
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        pending.discard(b.get("tool_use_id"))
        if api_error and not pending:
            return "failed"
        if not pending and not interrupted and last_stop in ("end_turn", "stop_sequence"):
            return "completed"
        return "interrupted"

    def subagent_log(self, sid, agent_id, limit=200, limit_bytes=None):
        """某个子代理的输出,翻成契约的 parts 形态,只读。"""
        esid = (self.index.get(sid) or {}).get("engine_sid")
        if not esid or not re_mod.fullmatch(r"[0-9a-zA-Z_-]{4,64}", agent_id or ""):
            return []
        path = os.path.join(os.path.dirname(self._transcript_path(esid)), esid,
                            "subagents", f"agent-{agent_id}.jsonl")
        if not os.path.exists(path):
            return []
        start = 0
        try:
            max_bytes = max(0, int(limit_bytes or 0))
        except (TypeError, ValueError):
            max_bytes = 0
        if max_bytes:
            try:
                start = max(0, os.path.getsize(path) - max_bytes)
            except OSError:
                return []
        return (self._group_transcript(path, sid, esid, start=start, side=True) or [])[-limit:]

    def transcript_stat(self, sid):
        """记录的大小与修改时刻。界面据此判断「那边有没有新内容」——
        全量重读一份 134MB 的记录要 200ms,不能拿来轮询。"""
        esid = (self.index.get(sid) or {}).get("engine_sid")
        if not esid:
            return {}
        try:
            st = os.stat(self._transcript_path(esid))
        except OSError:
            return {}
        return {"size": st.st_size, "mtime": int(st.st_mtime * 1000)}

    def _group_transcript(self, path, sid, esid, start=0, side=False, sink=None):
        """把记录按回合分组成契约里的消息形态。整份返回,由调用方决定取哪一段——
        打开会话只要末尾几十个回合,往上翻页要更早的那些,两者读的是同一份分组结果。"""
        msgs, tools = [], {}      # tools: tool_use_id → 已建好的 tool part,等结果回填
        returned = {}             # 通知与工具完成回执按调用/任务 ID 去重。
        seq = [0]
        cmeta = {}                # 最近一条压缩元信息,等它后面那条摘要来认领
        seen_cmp = set()          # 已经收过的压缩摘要 uuid:同一条会被 resume 重写进记录
        pend, pend_uuid = [], [""]   # 当前回合累积的 assistant parts
        request_parts = {}            # 同一 Claude API 请求拆成多行时仍按 requestId 归组
        span = [0.0, 0.0]            # 本回合的起止时刻(秒),用来还原耗时
        aside = [None]               # 正在收的自续轮:{uuid, at, task, status};不在自续轮里时为 None
        memo = {}                    # 一次调用内的取数缓存(子代理名单)

        def pid(kind):
            seq[0] += 1
            return f"imp_{kind}_{seq[0]}"

        def ts(o):
            """记录里的 ISO 时刻转秒。回合耗时靠它还原——引擎自报的 duration_ms 只在实跑那一次
            有,而每回合末尾都会以记录为准重建一遍,重建后耗时就没了。
            记录里写的是 UTC(尾部带 Z),不补时区就会当成本地时刻,取差值时抵消看不出来,
            拿去和本机时刻比、或直接当成文时刻显示,就整整差一个时区。"""
            v = str(o.get("timestamp") or "")
            if not v:
                return 0.0
            try:
                from datetime import datetime, timezone
                return (datetime.strptime(v[:23], "%Y-%m-%dT%H:%M:%S.%f")
                        .replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                return 0.0

        def subs():
            """这份记录里出现过的子代理。一次 _group_transcript 只取一份:名单要 listdir、
            逐个读记录的首末、再扫一遍主记录收证据,按自续轮的条数重复取会把重建拖慢。"""
            if "rows" not in memo:
                try:
                    memo["rows"] = self.subagents(sid)
                except Exception:
                    memo["rows"] = []
            return memo["rows"]

        def flush_aside():
            """自续轮收场,产出一条与实时那条 session.aside 同构的行。

            没有内容的不留痕:通知落进上下文、引擎却没有接着跑,这种情形实见两例
            (用户紧跟着发话把它盖过去,以及会话就此结束)。那时并不存在「引擎在处理
            子代理返回」这件事,画一条分割线是无中生有。"""
            a = aside[0]
            aside[0] = None
            parts, at, end = list(pend), span[0], span[1]
            pend.clear()
            pend_uuid[0] = ""
            span[0] = span[1] = 0.0
            if not parts:
                return
            mid = "msg_x_" + (a.get("uuid") or pid("m"))
            for q in parts:
                q["messageID"] = mid
                q["sessionID"] = sid
            # ms 是自续轮自己跑了多久。实时那条取 result 帧的 duration_ms,重建时没有这个数
            # (它只在实跑那一次有),退回首末时刻之差——起点用通知那条的时刻,它正是引擎起
            # 这一轮的时刻,与 duration_ms 的起点一致。口径同普通回合的耗时还原,不另编一个数。
            row = {"origin": self.ASIDE_ORIGIN, "presentation": "continuation", "started": int(at * 1000),
                   "return_id": a.get("return_id") or "",
                   "ms": int((end - at) * 1000) if end > at else 0,
                   "task_id": a.get("task") or "",
                   "agent": self._aside_agent(sid, a.get("task"), a.get("status"), rows=subs())}
            msgs.append({"info": {"id": mid, "role": "assistant", "sessionID": sid,
                                  "engine_sid": esid, "at": int(at * 1000), "aside": row},
                         "parts": parts})

        def flush():
            if aside[0] is not None:
                flush_aside()
                return
            if not pend:
                return
            mid = "msg_a_" + (pend_uuid[0] or pid("m"))
            for q in pend:
                q["messageID"] = mid
                q["sessionID"] = sid
            info = {"id": mid, "role": "assistant", "sessionID": sid, "engine_sid": esid}
            if span[1]:
                info["at"] = int(span[1] * 1000)   # 这一轮的成文时刻;消息 id 用的是记录里的 uuid,里头没有时间
            if span[0] and span[1] > span[0]:
                info["ms"] = int((span[1] - span[0]) * 1000)
            msgs.append({"info": info, "parts": list(pend)})
            pend.clear()
            pend_uuid[0] = ""
            span[0] = span[1] = 0.0

        # 撤回过的回合:引擎的记录里仍留着那句话、以及它已经跑出来的半截内容。而我们的存储
        # 每回合都以记录为准重建,不在这里排掉,撤回的消息下一次对账就自己回来了——重编辑
        # 再发,界面上就是两条。一段会话是单线的,[发出, 撤回] 之间的条目全属于那个回合。
        cuts = list((self.index.get(sid) or {}).get("recall_cuts") or [])
        try:
            # errors=replace 是必须的:seek 落在多字节字符中间会直接抛解码错。
            # 那半行本来就要丢,替换掉即可。
            with open(path, encoding="utf-8", errors="replace") as f:
                if start:
                    f.seek(start)
                    f.readline()   # 断点多半落在半行上,丢掉它
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        o = json.loads(ln)
                    except ValueError:
                        continue
                    if cuts:
                        at = ts(o) * 1000
                        if at and any(a <= at <= b for a, b in cuts):
                            continue
                    t = o.get("type")
                    # 目标的设与达成:引擎写成一条 attachment。窄条只说「现在还挂着什么」,
                    # 什么时候设的、到哪一轮达成的,得在正文流里留个位置才回溯得到。
                    if t == "attachment":
                        a = o.get("attachment") or {}
                        # 回合中途送达的通知:引擎正在跑,harness 把它并进当前回合,记录里没有
                        # user 条目,只有这条 attachment(queued_command,commandMode 为
                        # task-notification)。此前投影认不出它,回合一收尾按记录重建,返回就没了
                        # (2026-09-08 用户实见)。回填到派出它的那一步,与实时那一路同形;
                        # 不开自续轮——它没有自己的一轮。
                        if (a.get("type") == "queued_command"
                                and a.get("commandMode") == "task-notification"):
                            body_a = str(a.get("prompt") or "")
                            r0 = from_notification({"message": {"content": body_a}})
                            if r0:
                                step = find_tool_part(list(tools.values()), r0.get("call_id"), r0.get("task_id"))
                                if step is not None:
                                    agent = self._aside_agent(sid, r0.get("task_id"), rows=subs())
                                    r1 = from_notification({"message": {"content": body_a}}, agent, step)
                                    r1["at"] = int(ts(o) * 1000)
                                    if handed_off(step, r1):
                                        attach_return(step, r1)
                            continue
                        if a.get("type") == "goal_status":
                            # 同一个目标在记录里不止两条:设定那条带 sentinel,达成那条 met 为真,
                            # 中间还夹着每次尝试收尾时写下的「还没达成」判定——目标挂多久就有多少条。
                            # 三种都画,正文里就会反复出现同一句「已设定目标」。只画状态真正改变的
                            # 那两条:设定与达成。
                            if not a.get("sentinel") and not a.get("met"):
                                continue
                            flush()
                            gid = "goal_" + (o.get("uuid") or pid("g"))
                            msgs.append({"info": {"id": gid, "role": "user", "sessionID": sid,
                                                  "at": int(ts(o) * 1000),
                                                  "goal": {"condition": a.get("condition") or "",
                                                           "met": bool(a.get("met"))}},
                                         "parts": []})
                        continue
                    # 压缩的元信息(触发方式、压缩前后的 token)单独一条 system,紧挨着摘要那条
                    if t == "system" and o.get("compactMetadata"):
                        cmeta = o.get("compactMetadata") or {}
                    # 子代理的记录整份都是 isSidechain,读它的时候当然不能按支线跳过;
                    # 读主会话时照旧跳——支线的过程不该混进主线的回合里。
                    if t not in ("user", "assistant") or (o.get("isSidechain") and not side):
                        continue
                    # CLI 自己合成的 assistant 条目:model 写作 <synthetic>,表示这句话不是模型
                    # 出的,是 CLI 在流断掉之后替它写的一句交代。实见两类:重试耗尽后的
                    # 「API Error: Unable to connect to API (…CERTIFICATE…)」,以及没让模型说话
                    # 时的「No response requested.」。照正文渲染,界面上就是 AI 在回复里报证书错。
                    # 报错另有去处:记进 sink,由 import_session 送到输入框上方那条系统信息里。
                    if t == "assistant" and (o.get("message") or {}).get("model") == "<synthetic>":
                        if sink is not None and o.get("isApiErrorMessage"):
                            sink["notice"] = self._raw_text(o).strip()[:300]
                        continue
                    # isMeta 的 user 条目是系统注入的上下文,不是人打的字——技能正文、
                    # 本地命令回显都走这一条。渲染成用户消息,界面上就出现一大段没人说过的话。
                    # 技能的调用本身已经由工具步骤呈现,这里整条跳过。
                    if t == "user" and o.get("isMeta"):
                        continue
                    # 后台任务与子代理的完成通知是工具来源，转换为只读执行返回，
                    # 不作为用户消息；随后产生的 assistant 内容仍属于主代理。
                    # 通知同时划分续答段：从这里到下一条发言/通知之间的 assistant
                    # 仍按助手存储，aside 元信息只保留回合归属，不再冒充消息来源。
                    if t == "user" and self._injected_user(o):
                        # 子代理自己那份记录(side)里没有自续轮,不必在那条路上认。
                        if not side:
                            flush()          # 上一轮(或上一个自续轮)到此为止
                            body = self._raw_text(o)
                            m_id = self._AG_TASK_ID.search(body)
                            m_st = self._AG_STATUS.search(body)
                            result = from_notification(o)
                            if result:
                                agent = self._aside_agent(sid, result.get("task_id"), rows=subs())
                                step = find_tool_part(list(tools.values()), result.get("call_id"), result.get("task_id"))
                                result = from_notification(o, agent, step or tools.get(result.get("call_id")))
                                result["at"] = int(ts(o) * 1000)
                                if step is not None:
                                    # 派出它的那一步就在记录里:回填到那一步,不另起来源消息。
                                    # 同步命令的通知不算交回,那一步早已带着自己的结果,
                                    # 回填只会把行上的动词改成不实的「已返回」。
                                    if handed_off(step, result):
                                        attach_return(step, result)
                                else:
                                    result_parts = []
                                    append_result(result_parts, result, returned)
                                    if result_parts:
                                        mid = "msg_" + result["id"]
                                        result_parts[0].update(messageID=mid, sessionID=sid)
                                        msgs.append({"info": {"id": mid, "role": "tool", "sessionID": sid,
                                                              "at": result["at"]}, "parts": result_parts})
                            # 通知正文里的 <task-id> 与实时那路预告帧的 task_id 是同一个 id,
                            # 只是取法不同:那边有独立字段,这边只能从正文里取。
                            aside[0] = {"uuid": str(o.get("uuid") or ""), "at": ts(o),
                                        "return_id": result["id"] if result else "",
                                        "task": m_id.group(1) if m_id else "",
                                        "status": m_st.group(1) if m_st else ""}
                            span[0] = ts(o)   # 自续轮从通知落进上下文那一刻算起
                        continue
                    content = (o.get("message") or {}).get("content")
                    blocks = content if isinstance(content, list) else (
                        [{"type": "text", "text": content}] if isinstance(content, str) else [])
                    # 本地命令的回显:CLI 把「你敲了什么」和「命令打印了什么」各写成一条
                    # 普通的 user 条目,不带 isMeta,所以上面那道过滤放它们过去了,
                    # 界面上就出现两团尖括号标签。敲的那条还原成人看得懂的一行(/goal 参数),
                    # 命令的输出整条丢掉——它是命令的产物,不是谁说的话。
                    if t == "user":
                        # 旧记录可能把 Desk 上下文拼在首个文本块前。只剥离带固定头和
                        # 完整分隔符的信封，保留用户在后续块里引用的相同字面文案。
                        clean_blocks, first_text = [], True
                        for block in blocks:
                            if (first_text and isinstance(block, dict)
                                    and block.get("type") == "text"):
                                block = {**block, "text": strip_scene_envelope(block.get("text") or "")}
                                first_text = False
                            clean_blocks.append(block)
                        blocks = clean_blocks
                        one = "".join(b.get("text") or "" for b in blocks
                                      if isinstance(b, dict) and b.get("type") == "text").strip()
                        if one.startswith(("<local-command-stdout>", "<local-command-stderr>",
                                           "<local-command-caveat>")):
                            continue
                        # 三个标签的先后顺序不固定(实测 /doctor 是 message 在前),
                        # 所以认这一组里的任意一个开头,再各自按名字取
                        if one.startswith(("<command-name>", "<command-message>", "<command-args>")):
                            nm = re_mod.search(r"<command-name>(.*?)</command-name>", one, re_mod.S)
                            ag = re_mod.search(r"<command-args>(.*?)</command-args>", one, re_mod.S)
                            line = ((nm.group(1).strip() if nm else "")
                                    + " " + (ag.group(1).strip() if ag else "")).strip()
                            if not line:
                                continue
                            blocks = [{"type": "text", "text": line}]
                        # 中断标记:引擎在回合被打断时把这一句写进记录,占的是一条 user 条目,
                        # 于是界面上出现一个谁也没说过的气泡。回合被打断这件事状态行已经报了,
                        # 这里只需摘掉。剥前缀而不是整条丢——用户打断之后紧接着说的话会跟标记
                        # 写在同一条里(实测「[Request interrupted by user] 可以了」),
                        # 整条丢会把那句话一起丢掉。
                        cut = re_mod.match(r"\[Request interrupted by user[^\]]*\]\s*", one)
                        if cut:
                            rest = one[cut.end():].strip()
                            if not rest:
                                continue
                            blocks = [{"type": "text", "text": rest}]
                    # 工具结果先回填,它挂在紧随其后的 user 条目里,不自成一条消息
                    results = [x for x in blocks if isinstance(x, dict) and x.get("type") == "tool_result"]
                    for r in results:
                        part = tools.get(r.get("tool_use_id"))
                        if not part:
                            continue
                        c = r.get("content")
                        txt = c if isinstance(c, str) else " ".join(
                            x.get("text", "") for x in (c or []) if isinstance(x, dict))
                        part["call_id"] = r.get("tool_use_id")
                        part["state"]["output"] = txt or ""
                        settle_return(part)   # 通知比结果先落进记录时,复核那次暂判
                        part["state"]["status"] = "error" if r.get("is_error") else "done"
                    if results:
                        span[1] = ts(o) or span[1]
                        continue
                    parts = []
                    request_id = str(o.get("requestId") or "") if t == "assistant" else ""
                    request_has_tool = (t == "assistant" and any(
                        isinstance(x, dict) and x.get("type") == "tool_use" for x in blocks))
                    if request_has_tool and request_id:
                        # Claude transcript 会把同一次请求的 text 与 tool_use 拆成相邻的
                        # assistant 行,但 requestId 不变。工具出现时追认此前文字为过程输出。
                        for old in request_parts.get(request_id, []):
                            if old.get("type") == "text":
                                old["type"] = "reasoning"
                    for b in blocks:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "text":
                            txt = (b.get("text") or "").strip()
                            if not txt or txt.startswith("<system-reminder>"):
                                continue
                            q = {"id": pid("t"),
                                 "type": "reasoning" if request_has_tool else "text",
                                 "text": txt}
                            parts.append(q)
                            if request_id:
                                request_parts.setdefault(request_id, []).append(q)
                        elif bt == "thinking":
                            txt = (b.get("thinking") or "").strip()
                            if txt:
                                parts.append({"id": pid("r"), "type": "reasoning", "text": txt})
                        elif bt == "image":
                            # 截图要跟着回合留下来:发出去那一刻界面上有,重建之后就没了,
                            # 因为存储里只留了正文。记录里存的是 base64,而这条路上盘上
                            # 没有原件,所以先按内容哈希落一份再给引用——正文里不留 base64。
                            # 哈希命名让它对重建幂等:同一张图对账多少次都是同一个文件。
                            src = b.get("source") or {}
                            data = src.get("data") or ""
                            if src.get("type") == "base64" and data:
                                mt = src.get("media_type") or "image/png"
                                u = self.asset_save(sid, data, mt)
                                if u:
                                    parts.append({"id": pid("i"), "type": "image",
                                                  "media_type": mt, "url": u})
                        elif bt == "tool_use":
                            q = {"id": pid("u"), "type": "tool", "tool": b.get("name") or "tool",
                                 "state": {"status": "done", "input": b.get("input") or {}, "output": ""}}
                            tools[b.get("id")] = q
                            parts.append(q)
                    if not parts:
                        continue
                    # 这条报错后面又有了真的产出:说明引擎已经恢复,那条系统信息不该再挂着。
                    if sink is not None:
                        sink["notice"] = ""
                    if t == "user":
                        # 一次发言到下一次发言之间的所有 assistant 条目合成一个回合。
                        # 记录里每次工具调用都是独立一条 assistant,逐条建消息会把一段长会话
                        # 变成几千个回合(实测 2220 个),界面直接卡死;适配器自己跑出来的形态
                        # 也是一回合一条 assistant 消息,这里对齐它。
                        flush()
                        mid = "msg_u_" + str(o.get("uuid") or pid("m"))
                        for q in parts:
                            q["messageID"] = mid
                            q["sessionID"] = sid
                        nfo = {"id": mid, "role": "user", "sessionID": sid, "at": int(ts(o) * 1000)}
                        # 压缩摘要在记录里也是一条 user,但那不是人打的字,是引擎把前文重写了一遍
                        # 塞回上下文。当成发言渲染,界面上就出现一条几千字的「用户消息」。
                        # 这里给它挂个标记,由视图改成一条分隔线。回合边界照旧,序号也不动——
                        # 回退按「第几条真人发言」对齐引擎,漏掉一条就整体错位。
                        if o.get("isCompactSummary") and o.get("uuid") in seen_cmp:
                            continue   # resume 会把同一条摘要重写进记录,两条都铺就并排出现
                        if o.get("isCompactSummary"):
                            seen_cmp.add(o.get("uuid"))
                            # postTokens 一并带上:界面那条分隔线要报「压缩前 → 压缩后」,
                            # 只给压缩前等于只说了一半。实时那条路本来就有,重建这侧原来漏了。
                            nfo["compact"] = {"trigger": (cmeta or {}).get("trigger") or "",
                                              "pre": (cmeta or {}).get("preTokens") or 0,
                                              "post": (cmeta or {}).get("postTokens") or 0}
                            cmeta = {}
                        # 保留真实输出边界；前端把摘要标记接在前一块输出之后，过程区可折叠。
                        row = {"info": nfo, "parts": parts}
                        msgs.append(row)
                        span[0] = ts(o)   # 回合从这句话发出算起
                    else:
                        pend.extend(parts)
                        if not pend_uuid[0]:
                            pend_uuid[0] = str(o.get("uuid") or "")
                        span[1] = ts(o) or span[1]   # 到最后一条产出为止
        except OSError:
            return None
        flush()   # 收尾:最后一个回合还没遇到下一次发言
        return msgs

    def import_session(self, esid, sid=None, limit=SESSION_WINDOW):
        """把 claude 的 transcript 翻成契约的消息形态,整份覆盖进我们的存储。

        这是「终端会话与 HTML 会话是同一段会话的两个视图」的底座:两条路最终都是
        `claude --resume <uuid>`,历史都落在同一份 transcript 里。所以每次用 HTML 打开就重读
        一遍——终端那侧新添的回合、我们这侧新添的回合,都在里面,不必两处对账。

        整份覆盖而不是增量:transcript 是 claude 的权威记录,我们的存储只是它的投影。
        """
        path = self._transcript_path(esid)
        if not os.path.exists(path):
            return None
        if not sid:
            with self._lock:
                sid = next((k for k, v in self.index.items() if v.get("engine_sid") == esid), None)
            if not sid:
                sid = self.create_session(self.adopt_domain(esid))["id"]
        # 只要最近若干回合,却整份读一遍:实测那份 136MB 的记录光解析就 0.5 秒,
        # 加上取标题又一遍,切一次会话要一秒出头。改成从末尾开一个窗口读,不够再放大。
        # 末尾 4MB 解析只要 0.02 秒。
        size = os.path.getsize(path)
        win, msgs = 4_000_000, None
        sink = {}
        while True:
            start = max(0, size - win) if win < size else 0
            msgs = self._group_transcript(path, sid, esid, start, sink=sink)
            if msgs is None:
                return None
            if start:
                # 窗口多半从一个回合中间切进来,前面那截没有发言开头,丢掉
                first = next((i for i, m in enumerate(msgs) if self._turn_start(m)), None)
                msgs = msgs[first:] if first is not None else []
            # 数的是真人发言:压缩行、目标行、自续轮那条分割线在契约里也是 role=user,
            # 一并算进来会让窗口以为已经够了,实际取到的回合数不足 limit。判据与下面
            # 按回合裁剪那一步同源。
            got = sum(1 for m in msgs if self._turn_start(m))
            if start == 0 or (limit and got > limit) or (not limit and got):
                break
            win *= 4
        # 只保留最近若干回合。长会话动辄几百回合、上千次工具调用,一次性铺进 DOM 会把界面
        # 卡死(实测一段会话导出 662 条消息、1271 个工具卡)。更早的不是丢了——往上拉到顶
        # 会按 30 个一批从记录里现取(见 history)。
        if limit:
            starts = [i for i, m in enumerate(msgs) if self._turn_start(m)]
            if len(starts) > limit:
                msgs = msgs[starts[-limit]:]
        # 正在跑的那一回合不从记录里取。记录是边跑边写的,此刻取到的是半截;而这一回合在
        # 界面上另有来源——读消息时补的那份内存快照。两边都进,界面上就是同一回合出现两次:
        # 前一次带着「已处理 18.3s」和时间戳(像是跑完了),后一次才是真正在跑的那条。
        live = self._live.get(sid)
        cut = (live or {}).get("t0") or 0
        if cut:
            msgs = [m for m in msgs
                    if not (m["info"].get("role") == "assistant" and (m["info"].get("at") or 0) >= cut)]
        msgs = self._reinstate_local_only(sid, msgs)
        self._rewrite_msgs(sid, msgs)
        with self._lock:
            e = self.index.get(sid)
            if e is not None:
                e["engine_sid"] = esid
                # 标题跟终端那侧同源:自定义标题优先,其次记录里的 summary,最后才退到首条发言。
                # 自己取「第一条用户消息」会踩到压缩产生的 <local-command-caveat> 之类的块——
                # 那不是人打的字,同一段会话在两个视图里于是显示成两个名字。
                title = ""
                try:
                    from term import _custom_title_for
                    title = _custom_title_for(esid) or ""
                except Exception:
                    title = ""
                # 引擎侧的改名(/rename)落在 transcript 里的 custom-title 条目,不在
                # .workbench/custom-titles.json,也不是 summary 行——终端那侧显示的就是它。
                # 取最后一条,改名可以改很多次。
                if not title:
                    title = self._engine_title(path)
                if not title:
                    for m in msgs:
                        # 目标行的 parts 是空的,不排除它这里会直接下标越界
                        if not self._turn_start(m):
                            continue
                        t0 = ((m["parts"] or [{}])[0].get("text") or "").strip()
                        if not t0 or t0.startswith("<"):
                            continue
                        # 引擎记录里的首条用户消息带着 Desk 前置的当前上下文(界面气泡里没有),
                        # 直接拿来当名字就成了「起手已知### 项目记忆摘要…」——2026-09-03
                        # 用户实见。清洗返回空串表示这一条里没有用户的话,接着找下一条,
                        # 不能 break。codex 那侧 2026-08-26 已修,claude 当时落下了。
                        t0 = clean_session_title(t0)
                        if t0:
                            title = t0
                            break
                if title:
                    e["title"] = title[:60]
                self._save_index()
        self._term_register(sid, esid)   # 找回聊天走的是这条路,登记不能只挂在新建那条
        # 引擎合成的那句报错(证书校验、限流、登录失效)不进正文,挂到输入框上方那条系统信息:
        # 它讲的是引擎此刻通不通,不是这一轮的产出。后面又跑出东西来就说明已经恢复,自己撤掉。
        note = sink.get("notice")
        if note is not None:
            cur = (self.index.get(sid) or {}).get("environment") or {}
            head = self._notice_head(note)
            if note and cur.get("label") != head:
                self._env_publish(sid, {"state": "error", "stage": "engine", "label": head,
                                        "details": [note] if note != head else []})
            elif not note and cur.get("stage") == "engine":
                self._env_publish(sid, {"state": "ready", "stage": "engine", "label": "引擎已恢复"})
        return dict(self.index.get(sid) or {}, id=sid)

    @classmethod
    def _human_turns(cls, path):
        """记录里属于真人发言的条目,按出现顺序给 [(uuid, 正文)]。
        要滤掉四类同为 type=user 的东西:工具结果回填、子代理支线(isSidechain)、
        系统注入的提醒块,以及后台任务与子代理的完成通知——它们都不是人打的字,
        混进来会让序号对不上。这里的口径必须与 _group_transcript 一致:回退按
        「第几条真人发言」在两边对齐,一侧多滤一条就整体错位。"""
        out = []
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        o = json.loads(ln)
                    except ValueError:
                        continue
                    if o.get("type") != "user" or o.get("isSidechain") or o.get("isMeta"):
                        continue   # isMeta:系统注入,不是真人发言(与 import_session 同口径)
                    if cls._injected_user(o):
                        continue   # 后台任务/子代理的完成通知,同上
                    c = (o.get("message") or {}).get("content")
                    if isinstance(c, list):
                        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
                            continue
                        txt = " ".join(x.get("text", "") for x in c
                                       if isinstance(x, dict) and x.get("type") == "text")
                    else:
                        txt = c if isinstance(c, str) else ""
                    txt = strip_scene_envelope(txt).strip()
                    if not txt or txt.startswith("<system-reminder>"):
                        continue
                    out.append((o.get("uuid") or "", txt))
        except OSError:
            pass
        return out

    def _engine_umid(self, sid, msg_id):
        """把我们的消息 id 换成 claude 记录里那条真人发言的 uuid。
        rewind_files 只认后者,而两边的 id 体系互不相通——流里 user 帧带的 uuid 也不是记录里
        的那个(实测两者不同),所以按「第几条真人发言」对齐,再用正文校一次防错位。"""
        esid = (self.index.get(sid) or {}).get("engine_sid")
        if not esid:
            return None, "会话还没有引擎侧锚点"
        # 自续轮那条分割线在契约里也是 role=user,但引擎侧的 _human_turns 把触发它的那条通知
        # 滤掉了(它不是真人发言)。这边不排掉,一个自续轮就让两侧的序号差一位。压缩摘要相反:
        # 记录里它就是一条 user 条目,_human_turns 收着它,所以这边照旧留着,两侧仍对得上。
        ours = [m for m in self.messages(sid)
                if (m.get("info") or {}).get("role") == "user"
                and not (m.get("info") or {}).get("aside")]
        idx = next((i for i, m in enumerate(ours) if (m.get("info") or {}).get("id") == msg_id), -1)
        if idx < 0:
            return None, "找不到这条消息"
        mine = " ".join(q.get("text", "") for q in (ours[idx].get("parts") or [])
                        if q.get("type") == "text").strip()
        turns = self._human_turns(self._transcript_path(esid))
        if not turns:
            return None, "读不到引擎的会话记录"
        if idx < len(turns) and (not mine or turns[idx][1].strip() == mine):
            return (esid, turns[idx][0]), ""
        hit = next((u for u, t in turns if t.strip() == mine), "")   # 序号对不上时按正文兜底
        if hit:
            return (esid, hit), ""
        return None, "这条消息在引擎记录里对不上"

    # 回退用的那条连接。界面要问两次(先预览、再执行),而每次新起一个 --resume 都要把
    # 整份记录冷读一遍——一段几 MB 的会话,这一步就是十几秒,问两次付两遍。
    # 预览之后把进程留着,执行那一次接着用同一条;60 秒没人续用就自己收掉。
    _RW_KEEP = 60
    _rw_proc = {}
    _rw_lock = threading.Lock()

    def _rw_drop(self, sid):
        with self._rw_lock:
            ent = self._rw_proc.pop(sid, None)
        if ent:
            try:
                ent["proc"].stdin.close()
            except Exception:
                pass
            try:
                ent["proc"].kill()
            except Exception:
                pass

    def _rw_get(self, sid, esid, env):
        """取一条能受理 rewind 的连接:能续用就续用,否则冷起一条。"""
        now = time.time()
        with self._rw_lock:
            ent = self._rw_proc.get(sid)
            if ent and ent["esid"] == esid and ent["proc"].poll() is None \
                    and now - ent["at"] < self._RW_KEEP:
                ent["at"] = now
                return ent["proc"], False
        self._rw_drop(sid)
        cmd = ["claude", "-p", "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose", "--resume", esid]
        proc = subprocess.Popen(cmd, cwd=self.session_cwd(sid), text=True, encoding="utf-8", env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        with self._rw_lock:
            self._rw_proc[sid] = {"proc": proc, "esid": esid, "at": now}
        return proc, True
