from .codex_tool_result import result_state, result_text
# 由 engine_adapters.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)
import copy
import json
import os
import re as re_mod
import threading
from .bus import SCENE_SEPS
from .execution_returns import from_agent_message, append_result


class CodexRolloutMixin:


    @classmethod
    def _rollout_tool(cls, name, raw):
        """一条工具调用翻成(工具名, 入参)。

        两种载荷:function_call 的 arguments 是 JSON;custom_tool_call 的 input 是一段
        调用 tools.exec_command 的脚本。直接命令在 cmd 字段；functions.exec 则可能把
        Code Mode 源码放在 JSON 的 input 字段。两种都要解开——卡片上应显示实际意图
        与命令，不是包装参数。
        """
        name = str(name or "")
        # 表里没有的回落成名字本身(小写),不要一律当 task。前端按工具名给动词,
        # task 的动词是「调度」——兼容引擎的工具可能叫 Read / Grep / Edit / Bash / TodoWrite,
        # 不在这张为 codex 写的表里,整段历史会全成「已调度 grep」「已调度 bash」。小写之后 read / grep / edit / bash / todowrite 都能直接命中
        # 前端的动词表;真认不出的也会显示原名,比「调度」准确。
        tool = cls.CODEX_TOOL.get(name) or name.lower() or "task"
        args = {}
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                args = json.loads(raw)
            except ValueError:
                args = {}
        cmd = args.get("cmd") or ""
        if not cmd and isinstance(raw, str):
            m = re_mod.search(r'"cmd"\s*:\s*("(?:[^"\\]|\\.)*")', raw)
            if m:
                try:
                    cmd = json.loads(m.group(1))
                except ValueError:
                    cmd = ""
        if cmd:
            return tool, cls._command_input(cmd)
        if name in ("exec_command", "exec", "shell") and isinstance(raw, str):
            return tool, cls._command_input(raw)
        if name == "update_plan":
            steps = args.get("plan") or []
            return tool, {"description": f"更新计划({len(steps)} 步)" if steps else "更新计划"}
        if name in ("create_goal", "update_goal"):
            return tool, {"description": args.get("objective") or args.get("status") or name}
        if name == "apply_patch":
            return tool, {"description": "应用补丁"}
        if isinstance(args.get("todos"), list):
            n = len(args["todos"])
            return tool, {"description": f"更新计划({n} 项)" if n else "更新计划"}
        # 剩下的别只报一个工具名。兼容引擎的记录里入参通常是标准字段名
        # (Grep 给 pattern、Read/Edit 给 file_path、Bash 给 command 与 description),
        # 前端 hsToolObj 全都认得——丢掉它们,历史里的每张卡片就只剩「已搜索 grep」,
        # 搜的是什么、读的是哪个文件全看不见(2026-08-11 实见)。
        keep = {}
        for k in ("description", "file_path", "filePath", "path", "notebook_path",
                  "pattern", "url", "command"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                keep[k] = v
        return tool, keep or {"description": name}

    @staticmethod
    def _rollout_text(blocks):
        if not isinstance(blocks, list):
            return str(blocks or "")
        return "".join(b.get("text") or "" for b in blocks if isinstance(b, dict))

    @staticmethod
    def _rollout_items(row):
        """Return response-like items from Codex rollouts."""
        payload = (row or {}).get("payload") or {}
        if not isinstance(payload, dict):
            return []
        if row.get("type") == "response_item":
            return [payload]
        if row.get("type") == "history_mutation" and payload.get("operation") == "append":
            return [item for item in (payload.get("items") or []) if isinstance(item, dict)]
        if not row.get("type") and payload.get("type"):
            return [payload]
        return []

    _MEM_CITATION = re_mod.compile(
        r"\s*<oai-mem-citation>.*?</oai-mem-citation>\s*$", re_mod.I | re_mod.S)

    def _rollout_message_text(self, item):
        text = self._rollout_text((item or {}).get("content") or [])
        if (item or {}).get("role") == "assistant":
            text = self._MEM_CITATION.sub("", text)
        return text.strip()

    def _rollout_user_text(self, item):
        """Return a human prompt, excluding harness and Desk injections."""
        if (item or {}).get("type") != "message" or item.get("role") != "user":
            return ""
        text = self._rollout_message_text(item)
        head = text.lstrip()
        if ((head.startswith("<") and not head.lower().startswith("<image ")) or
                head.startswith("# AGENTS.md instructions")):
            return ""
        # Desk 自己前置的两段按顺序摘掉,不整条丢弃:seed 之后跟着的是用户真正说的那句话,
        # 连它一起丢会让重建出来的历史里第一次提问凭空消失(2026-08-24 核对发现)。
        if self.SEED_SEP in text:
            text = text.split(self.SEED_SEP, 1)[1]
        for sep in SCENE_SEPS:
            if sep in text:
                text = text.split(sep, 1)[1]
                break
        return text.strip()


    ITEM_TOOL = {
        "collab_agent": "task",          # 前端按 task/agent 认「调度」,并据此去取子代理清单
        "sub_agent_activity": "task",
        "command_execution": "bash",
        "file_change": "edit",
        "patch_apply": "patch",
        "mcp_tool_call": "mcp",
        "web_search": "websearch",
        "todo_list": "todowrite",
    }

    # 搬迁:codex 没有 serve,只能按 claude 那条路读它自己的记录。记录是 rollout 文件,
    # 一行一个条目,文件名里带着会话 id。
    ROLLOUT_DIRS = ("~/.codex/sessions", "~/.codex/archived_sessions")

    # 图片标记有两种写法,都要从正文里剥掉:
    #   <image path="...">…</image>   Desk 上传时自己加的,能按路径还原成图片
    #   <image name=[Image #1]>       TUI 里贴图时加的,没有路径
    # 原来的正则强制要 path,于是第二种整条留在正文里,界面上就是一行 HTML 源码
    # (2026-08-11 实见)。剥离与还原因此拆成两个正则:_IMAGE_TAG 只管认出「这是个图片标记」,
    # _IMAGE_PATH 管「能不能还原」。第二种的图在 rollout 里要么是远端对象键(取不到),
    # 要么是 data URI(极少),都不值得为它另写一条取图链路——先保证不把标记当正文显示。
    _IMAGE_TAG = re_mod.compile(
        r'<image\b[^>]*>(?:\s*</image>)?', re_mod.I | re_mod.S)
    _IMAGE_PATH = re_mod.compile(
        r'<image\b[^>]*\bpath=(["\'])(.*?)\1[^>]*>', re_mod.I | re_mod.S)

    def _rollout_user_parts(self, txt, mid, sid):
        """恢复 Desk 上传图片；只读取当前会话 uploads 目录，避免 rollout 注入任意路径。"""
        import mimetypes
        root = os.path.realpath(os.path.join(self.dir, "uploads", sid))
        images = []
        for match in self._IMAGE_PATH.finditer(txt or ""):
            path = os.path.realpath(match.group(2))
            try:
                if os.path.commonpath((root, path)) != root or not os.path.isfile(path):
                    continue
                mime = mimetypes.guess_type(path)[0] or "image/png"
                if not mime.startswith("image/"):
                    continue
                # 文件本来就在这段会话的 uploads 下,把它读出来转成 base64 再塞进正文
                # 是白做一遍:直接给引用,浏览器自己去取,还能缓存。原来那条 1.125MB 的
                # 体积上限也随之撤掉——大图不再影响正文的大小。
                images.append({"id": f"{mid}_i{len(images)}", "type": "image",
                               "media_type": mime, "url": self.asset_url(sid, path),
                               "messageID": mid, "sessionID": sid})
            except (OSError, ValueError):
                continue
        clean = self._IMAGE_TAG.sub("", txt or "")
        clean = re_mod.sub(r"\n{3,}", "\n\n", clean).strip()
        parts = []
        if clean:
            parts.append({"id": mid + "_p0", "type": "text", "text": clean,
                          "messageID": mid, "sessionID": sid})
        return parts + images

    def messages(self, sid):
        """读旧投影时即时修复图片标记与 Code Mode 包装脚本摘要。"""
        rows = super().messages(sid)
        for msg in rows:
            info = msg.get("info") or {}
            if info.get("role") == "user":
                fixed = []
                for part in msg.get("parts") or []:
                    text = part.get("text") or ""
                    if part.get("type") == "text" and self._IMAGE_TAG.search(text):
                        fixed.extend(self._rollout_user_parts(
                            text, info.get("id") or part.get("messageID") or "msg_u", sid))
                    else:
                        fixed.append(part)
                msg["parts"] = fixed
                continue
            for part in msg.get("parts") or []:
                if part.get("type") != "tool":
                    continue
                state = part.get("state") or {}
                state.update(result_state(state, state.get("status", "done")))
                if part.get("tool") != "bash":
                    continue
                inp = state.get("input") or {}
                command = inp.get("command")
                desc = str(inp.get("description") or "").strip()
                if command and (not desc or desc in ("const", "//")
                                or self._intent_placeholder(desc)):
                    state["input"] = self._command_input(command)
        return rows

    def history(self, sid, have=0, limit=30):
        """更早的回合。存储里只留最近那一段,往上翻页看的是 rollout 本身,只读不落盘。

        与 claude 那侧同一个契约,差别只在记录的读法。

        2026-08-23 改成开尾窗。原来整份读,理由写的是「单段最大也就几十兆(实测 39MB 解析
        0.3 秒)」——那个前提已经不成立:本机最大的一份 rollout 是 352MB。实测 25MB 整份读
        进程峰值内存 +90MB,按比例 352MB 约 +1.3GB,而这条路每次往上翻都跑一遍。

        尾窗会把工具调用与它的输出切散(两者靠 call_id 相认,分在窗口两侧就配不上对)——
        这一点由 import_session 那边同样的做法解决:窗口开完之后裁到第一条真人发言,
        半个回合不要。窗口不够就翻四倍再来,与那边同一条循环。
        """
        esid = (self.index.get(sid) or {}).get("engine_sid")
        if not esid:
            return []
        path = self._rollout_path(esid)
        if not path:
            return []
        return self._history_window(self._rollout_tail(path, sid, have + limit) or [], have, limit)

    #: 回合起点在 rollout 里的字节标记。一个回合以一条 role=user 的 response_item 开头
    #: (2026-08-24 拿本机最大那份实测:头 4MB 里 `"role":"user"` 出现 46 次,带空格的
    #: 那种写法一次都没有——codex 落盘用的是紧凑 JSON)。
    # 两种写法都认。本机的 codex 写的是紧凑 JSON,但这是它的序列化实现顺手定的,不是契约。
    # 只认一种的话,哪天它改成带空格,标记就一个也找不到、一律从头整份读——退化是静默的,
    # 只表现为「翻历史变慢了」,没有任何地方会报错。
    _ROLL_USER = (b'"role":"user"', b'"role": "user"')
    _ROLL_COMPACTED = b'"compacted"'
    _ROLL_PROBE = 8_000_000    # 认写法先在末尾这么多字节里找,找不到再退回全扫
    _ROLL_HEADER_BYTES = 2048  # 顶层 type 与 payload role 都在行首；缺失时宁可多读窗口

    @staticmethod
    def _json_string_end(raw, start):
        """返回 JSON 字符串闭引号之后的位置；raw 可以只是完整行的有界前缀。"""
        if start < 0 or start >= len(raw) or raw[start] != 34:  # '"'
            return -1
        pos = start + 1
        while pos < len(raw):
            byte = raw[pos]
            if byte == 92:  # '\\'，跳过转义字符
                pos += 2
                continue
            if byte == 34:
                return pos + 1
            pos += 1
        return -1

    @staticmethod
    def _json_ws(raw, start):
        while start < len(raw) and raw[start] in b" \t\r\n":
            start += 1
        return start

    @classmethod
    def _json_skip_value(cls, raw, start):
        """在有界前缀内跳过一个 JSON 值；值没收完就返回 -1。"""
        start = cls._json_ws(raw, start)
        if start >= len(raw):
            return -1
        byte = raw[start]
        if byte == 34:
            return cls._json_string_end(raw, start)
        if byte not in (91, 123):  # '[' / '{'
            pos = start
            while pos < len(raw) and raw[pos] not in b",}]":
                pos += 1
            return pos
        stack = [93 if byte == 91 else 125]
        pos = start + 1
        while pos < len(raw):
            byte = raw[pos]
            if byte == 34:
                pos = cls._json_string_end(raw, pos)
                if pos < 0:
                    return -1
                continue
            if byte == 91:
                stack.append(93)
            elif byte == 123:
                stack.append(125)
            elif byte in (93, 125):
                if not stack or byte != stack[-1]:
                    return -1
                stack.pop()
                if not stack:
                    return pos + 1
            pos += 1
        return -1

    @classmethod
    def _json_member_start(cls, raw, obj_start, wanted):
        """找对象的直属成员值起点，不把嵌套对象里的同名字段误当顶层字段。"""
        pos = cls._json_ws(raw, obj_start)
        if pos >= len(raw) or raw[pos] != 123:  # '{'
            return -1
        pos += 1
        while pos < len(raw):
            pos = cls._json_ws(raw, pos)
            if pos < len(raw) and raw[pos] == 44:  # ','
                pos = cls._json_ws(raw, pos + 1)
            if pos >= len(raw) or raw[pos] == 125:  # '}'
                return -1
            key_end = cls._json_string_end(raw, pos)
            if key_end < 0:
                return -1
            key = raw[pos + 1:key_end - 1]
            pos = cls._json_ws(raw, key_end)
            if pos >= len(raw) or raw[pos] != 58:  # ':'
                return -1
            value_start = cls._json_ws(raw, pos + 1)
            if key == wanted:
                return value_start
            pos = cls._json_skip_value(raw, value_start)
            if pos < 0:
                return -1
        return -1

    @classmethod
    def _json_direct_string(cls, raw, obj_start, wanted):
        start = cls._json_member_start(raw, obj_start, wanted)
        end = cls._json_string_end(raw, start)
        return raw[start + 1:end - 1] if end >= 0 else b""

    @classmethod
    def _rollout_top_type(cls, source, start=0):
        """只读行首有界前缀，返回 rollout 行的顶层 type。"""
        head = source[start:start + cls._ROLL_HEADER_BYTES]
        obj = cls._json_ws(head, 0)
        return cls._json_direct_string(head, obj, b"type")

    @classmethod
    def _rollout_is_user_row(cls, source, start=0):
        """只认顶层 response_item 下直属 payload 的 message/user。"""
        head = source[start:start + cls._ROLL_HEADER_BYTES]
        obj = cls._json_ws(head, 0)
        if cls._json_direct_string(head, obj, b"type") != b"response_item":
            return False
        payload = cls._json_member_start(head, obj, b"payload")
        return (payload >= 0 and
                cls._json_direct_string(head, payload, b"type") == b"message" and
                cls._json_direct_string(head, payload, b"role") == b"user")

    def _rollout_window_start(self, path, size, marks):
        """从末尾往前数 marks 个 role=user 标记,返回该从哪个字节开始解析;数不够返回 0。

        只做字节扫描,不解析 JSON。原来的做法是「开 4MB 窗口解析一遍,回合不够就翻四倍
        重来」——每一轮都从新起点整份重读重解析,上一轮的活全扔。2026-08-24 拿 383MB 的
        Desk Opt 实测:开窗四次(4→16→64→256MB)、累计读 324MB、耗时 0.93 秒、进程峰值
        +393MB,而最后那一窗给出 71 个回合,要的只有 25 个。用户说的「这两段切起来特别慢」
        就是它。

        标记法只找起点,一次 mmap 扫描不建任何对象;定位之后解析一次,读多少用多少。

        role=user 的字节不都是回合起点:压缩行把被压掉的内容原样带着,注入进来的环境上下文
        也挂这个角色,_turn_start 都不认。所以扫描顺便看一眼顶层 type,只数真的起点——
        否则标记数比回合数多出一截,定位出来的窗口给不够,还要重来一趟。调用方仍会核一次
        真实回合数,不够就加倍再找(见 _rollout_tail)。
        """
        import mmap
        try:
            with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                # 两种写法只在第一轮各探一次,认准了就固定用那一种。都留着的话,文件里
                # 不存在的那一种每轮都要从 hi 一路扫到文件头——2026-08-24 拿 383MB 的
                # Desk Opt 实测:165 轮里每轮多扫一遍全文,单是 rfind 就 4.1 秒,
                # 比它省下来的读取还贵。
                hi, n, keys = min(size, len(mm)), 0, self._ROLL_USER
                while hi > 0:
                    if len(keys) > 1:
                        # 探测先只看末尾一段。扫全文会把整份文件的页都碰一遍、跟着算进
                        # 进程内存(383MB 那段实测峰值 +393MB),而认写法用不着扫那么远。
                        lim = max(0, hi - self._ROLL_PROBE)
                        at, key = max((mm.rfind(k, lim, hi), k) for k in keys)
                        if at < 0 and lim:
                            at, key = max((mm.rfind(k, 0, hi), k) for k in keys)
                        keys = (key,)
                    else:
                        at = mm.rfind(keys[0], 0, hi)
                    if at < 0:
                        return 0            # 前面没有更多回合了,从头读
                    lo = mm.rfind(b"\n", 0, at) + 1
                    # 只认真正的回合起点。判据只读行首有界前缀,同时核顶层 type 与直属
                    # payload 的 type/role；不能切 lo:at——at 可能在 10MB compacted 巨行末端。
                    # 压缩行(type=compacted)把被压掉的内容原样带在 payload 里,里面同样有
                    # 未转义的 "role":"user",按整行找会把它算成一个起点;而它在契约里是
                    # 压缩标记不是发言,_turn_start 不认。2026-08-24 拿 Desk Opt 实测:
                    # 29 个标记里 9 个是压缩行,真回合只有 15 个,于是白定位一次再重来。
                    if not self._rollout_is_user_row(mm, lo):
                        hi = lo if lo < hi else at
                        continue
                    n += 1
                    if n >= marks:
                        return lo
                    hi = lo if lo < hi else at
        except (OSError, ValueError):
            return 0
        return 0

    def _rollout_tail(self, path, sid, want):
        """从末尾开窗读,直到窗口里的回合数超过 want(或已经读到文件头)。

        判据是「超过」不是「够了」:_history_window 从末尾数回合,窗口里恰好 want 个时
        分不出「再往前没有了」与「再往前还有、只是没读到」,前者该返回空、后者不该。

        读不出来时返回 None,与「读出来是空的」区分开:import_session 拿它整份覆盖投影,
        读不了的时候不能当成「这段会话没有消息」把已有的抹掉。
        """
        want = want or 0        # import_session 用 None/0 表示「有一个回合就够」
        try:
            path = self._rollout_view(path)
            size = os.path.getsize(path)
        except (OSError, ValueError):
            return None
        # 扫描只读字节、解析只读定位之后那一段,比原来「整份重读四次」便宜得多。
        # 余量给得足一些:标记仍略多于回合(同一轮里可以有多条 user 消息,环境上下文那类
        # 也占一条),而两边的代价不对称——多要几个标记只是窗口大一点,少要了就得整份
        # 重新解析。2026-08-24 实测 Desk Opt/Iter 的比例在 1.3 上下,取 1.5 加常数兜住。
        marks = want + want // 2 + 4
        for _ in range(4):
            start = self._rollout_window_start(path, size, marks)
            msgs = self._group_rollout(path, sid, start)
            if msgs is None:
                return None
            if start:
                # 起点落在那条 user 行的行首,但仍裁一次:标记法只认字节,万一那一行不是
                # 真的回合起点(出现在别的字段里),裁到第一条真人发言。
                first = next((i for i, m in enumerate(msgs) if self._turn_start(m)), None)
                msgs = msgs[first:] if first is not None else []
            if start == 0 or sum(1 for m in msgs if self._turn_start(m)) > want:
                return msgs
            marks *= 2
        return msgs

    def _group_rollout(self, path, sid, start=0, exact=False):
        """rollout 翻成契约的消息形态。

        只取三类:人说的话、模型说的话、工具调用连同它的输出。其余(session_meta、
        turn_context、world_state、event_msg)是引擎的内部记账,界面不画。模型消息必须按
        phase 分流:commentary 是它主动给人的过程说明,进「处理中」；final_answer 才是正文。
        developer 角色的整段跳过——那是沙箱说明、角色纪律这类注入,不是谁的发言;
        我们自己在首回合塞进去的那段〈系统约定〉也一并摘掉。
        """
        try:
            path = self._rollout_view(path)
        except (OSError, ValueError):
            return None
        msgs, tools, pend = [], {}, []
        patch_wrappers = []
        n = [0]
        last_at = [0]      # 记录里最后见到的时刻,收尾那批消息用它拼 id
        pend_at = [0]      # 助手回合自己的末条时刻,不能借下一条用户消息的时刻
        pend_ms = [0]      # 本回合耗时,取 rollout 里 task_complete 自报的 duration_ms
        pend_interrupted = [False]
        used = {}          # id 去重:同一毫秒里可以有两条消息,撞了前端会认错

        def mkid(kind, at):
            base = f"msg_{kind}_{at}"
            k = used.get(base, 0)
            used[base] = k + 1
            return base if not k else f"{base}_{k}"   # 顺序解析,后缀因此是稳定的

        def pid(kind):
            n[0] += 1
            return f"{kind}_{n[0]}"

        goal_seen = [("", False)]   # 上一次画出去的 (条件, 是否达成);只画状态真的变了的那次
        # 目标回执里带着 threadId,要与本会话的引擎侧 id 对上才算数(别的会话的回执也可能
        # 落在同一份文件的视野里)。id 从索引取,取不到就从文件名尾部认——rollout 的文件名
        # 就是 rollout-<时刻>-<thread_id>.jsonl。
        goal_esid = str((self.index.get(sid) or {}).get("engine_sid") or "")
        if not goal_esid:
            m_id = re_mod.search(r"rollout-[\d T:-]*-([0-9a-f-]{16,})\.jsonl$", str(path))
            goal_esid = m_id.group(1) if m_id else ""

        def flush(force=False):
            if not pend and not force:
                return
            at = pend_at[0] or last_at[0]
            mid = mkid("a", at)
            for q in pend:
                q["messageID"] = mid
                q["sessionID"] = sid
            info = {"id": mid, "role": "assistant", "sessionID": sid, "at": at}
            # 耗时随消息一起重建。回合跑完时 _finish_turn 落过一次 ms,但存储会被这里整份覆盖,
            # 于是重启之后「已处理」后面那个时间就没了(2026-08-10 实见)。rollout 自己记着
            # 这一笔,取回来即可,不必另算。
            if pend_ms[0]:
                info["ms"] = pend_ms[0]
            if pend_interrupted[0]:
                info["interrupted"] = True
            msgs.append({"info": info, "parts": list(pend)})
            pend.clear()
            pend_at[0] = 0
            pend_ms[0] = 0
            pend_interrupted[0] = False
            patch_wrappers.clear()

        try:
            fh = open(path, "rb")
        except OSError:
            return None
        with fh:
            if start:
                try:
                    fh.seek(max(0, int(start)))
                    if not exact:
                        fh.readline()   # 尾窗多半从一行中间切入,先丢掉半截 JSON。
                except (OSError, ValueError):
                    return None
            for raw in fh:
                # 只记录这里发生过压缩，不展开 replacement_history。快照可能有数十 MB，
                # 其中的用户消息不是新提问；标记作为 part 保留前后输出与工具配对的顺序。
                if (raw.find(self._ROLL_COMPACTED, 0, self._ROLL_HEADER_BYTES) >= 0 and
                        self._rollout_top_type(raw) == b"compacted"):
                    # 边写边读时，未落完的一行还不能宣布压缩成功。
                    if raw.endswith(b"\n"):
                        pend.append({"id": pid("c"), "type": "compact", "text": "", "compact": {}})
                    continue
                ln = raw.decode("utf-8", "ignore")
                try:
                    o = json.loads(ln)
                except ValueError:
                    continue
                p = o.get("payload") or {}
                at = int((self._iso_ms(o.get("timestamp")) or 0))
                if at:
                    last_at[0] = at
                # 目标的设与达成也要在正文里留痕。此前只有 goal() 读得出「现在挂着什么」,
                # 而窄条只说现状——「什么时候设的、到哪一轮达成的」回溯不到,而那正是
                # 这条痕存在的理由(见前端 hsGoalRow)。claude 那侧一直有,codex 家族缺
                # (2026-08-23 用户实见)。形状与 claude 的哨兵消息一致,前端共用同一套渲染。
                g = self._goal_from_rollout_output(o, goal_esid) if goal_esid else None
                if g:
                    key = (g.get("condition") or "", bool(g.get("met")))
                    # 同一个目标在 rollout 里每一轮都写一条 active,只画状态真正变了的那两次
                    if key != goal_seen[0]:
                        goal_seen[0] = key
                        flush()
                        msgs.append({"info": {"id": "goal_%s" % pid("g"), "role": "user",
                                              "sessionID": sid, "at": at or last_at[0],
                                              "goal": {"condition": key[0], "met": key[1]}},
                                     "parts": []})
                    continue
                # 补丁正文只落在 patch_apply_end；只读 response_item 时，刷新后的编辑卡
                # 只能看到包装调用和空结果，无法恢复实际增删内容。
                if o.get("type") == "event_msg" and p.get("type") == "task_complete":
                    # 回合收尾的自报耗时(毫秒)。它落在本回合所有 response_item 之后、
                    # 下一条用户消息之前,所以此刻 pend 里正是这一回合,flush 时带走。
                    try:
                        pend_ms[0] = int(p.get("duration_ms") or 0)
                    except (TypeError, ValueError):
                        pend_ms[0] = 0
                    continue
                if o.get("type") == "event_msg" and p.get("type") == "turn_aborted":
                    try:
                        pend_ms[0] = int(p.get("duration_ms") or 0)
                    except (TypeError, ValueError):
                        pend_ms[0] = 0
                    pend_at[0] = at or pend_at[0]
                    pend_interrupted[0] = True
                    flush(force=True)
                    continue
                if o.get("type") == "event_msg" and p.get("type") == "patch_apply_end":
                    changes = []
                    for path, change in (p.get("changes") or {}).items():
                        if isinstance(change, dict):
                            changes.append({"path": path, "kind": change.get("type") or "update",
                                            "diff": change.get("unified_diff") or change.get("diff") or ""})
                    if changes:
                        # live app-server 只画 fileChange，不画外层 tools.apply_patch 包装；
                        # 历史重建保持一致，避免同一次编辑出现两张工具卡。
                        if patch_wrappers:
                            wrapper = patch_wrappers.pop()
                            if wrapper in pend:
                                pend.remove(wrapper)
                            for call_id, linked in list(tools.items()):
                                if linked is wrapper:
                                    tools.pop(call_id, None)
                        item = {"type": "file_change", "changes": changes,
                                "status": "completed" if p.get("success") else "failed"}
                        pend.append({"id": pid("k"), "type": "tool", "tool": "edit",
                                     "state": {"status": "done" if p.get("success") else "error",
                                               "input": self._tool_input(item),
                                               "output": self._tool_output(item)}})
                        pend_at[0] = at or pend_at[0]
                    continue
                for p in self._rollout_items(o):
                    t = p.get("type")
                    if t == "agent_message":
                        card = append_result(pend, from_agent_message(p))
                        if card:
                            card["at"] = at
                            pend_at[0] = at or pend_at[0]
                        continue
                    if t == "message":
                        role = p.get("role")
                        if role == "developer":
                            continue
                        txt = self._rollout_message_text(p)
                        if not txt:
                            continue
                        if role == "user":
                            txt = self._rollout_user_text(p)
                            if not txt:
                                continue
                            flush()
                            mid = mkid("u", at)
                            parts = self._rollout_user_parts(txt, mid, sid)
                            if parts:
                                msgs.append({"info": {"id": mid, "role": "user", "sessionID": sid, "at": at},
                                             "parts": parts})
                        elif role == "assistant":
                            ptype = "reasoning" if p.get("phase") == "commentary" else "text"
                            pend.append({"id": pid("r" if ptype == "reasoning" else "t"),
                                         "type": ptype, "text": txt})
                            pend_at[0] = at or pend_at[0]
                        continue
                    if t == "reasoning":
                        txt = self._rollout_text(p.get("summary") or p.get("content") or []).strip()
                        if txt:
                            pend.append({"id": pid("r"), "type": "reasoning", "text": txt})
                            pend_at[0] = at or pend_at[0]
                        continue
                    if t in ("custom_tool_call", "function_call"):
                        # 记录里的工具名是 codex 自己的叫法(exec / shell),实时那条路已经把
                        # command_execution 归到 bash 上。两处叫法不一致的话,同一段会话搬进来
                        # 之后动词会变成「调用 exec」,跑出来的那次却是「执行」。
                        raw = p.get("input") or p.get("arguments") or ""
                        nm, inp = self._rollout_tool(
                            str(p.get("name") or "tool").lower(),
                            raw)
                        part = {"id": pid("k"), "type": "tool", "tool": nm, "call_id": p.get("call_id"),
                                "state": {"status": "running",
                                          "input": inp,
                                          "output": ""}}
                        if str(p.get("name") or "").rsplit(".", 1)[-1] in {"create_goal", "update_goal"}:
                            part["execution_kind"] = "internal"
                        pend.append(part)
                        if (str(p.get("name") or "").lower() == "apply_patch" or
                                (isinstance(raw, str) and re_mod.search(r"\btools\.apply_patch\s*\(", raw))):
                            patch_wrappers.append(part)
                        pend_at[0] = at or pend_at[0]
                        if p.get("call_id"):
                            tools[p["call_id"]] = part
                        continue
                    if t in ("custom_tool_call_output", "function_call_output"):
                        part = tools.get(p.get("call_id"))
                        if not part:
                            continue
                        out = p.get("output")
                        if isinstance(out, list):
                            out = "".join(c.get("text") or "" for c in out if isinstance(c, dict))
                        part["state"] = {**result_state(p), "input": part["state"]["input"],
                                         "output": str(out or "")}
                        pend_at[0] = at or pend_at[0]
        # 收尾那一批用记录里最后一条的时刻,不用现在几点。消息 id 是由时刻拼的,
        # 取墙钟会让同一份记录每读一次末条就换一个 id——翻页与回合末对账都要拿 id 认人,
        # 换了就成了两条,界面上同一段话出现两遍。
        flush()
        return self._filter_recalled_turns(sid, msgs)

    @staticmethod
    def _iso_ms(s):
        """ISO 时刻转毫秒。记录里的时刻带 Z,是 UTC——按本地时区解析会整体差一个时差。"""
        try:
            from datetime import datetime, timezone
            return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000
        except Exception:
            return 0

    # codex 把每条命令包在 `/bin/bash -lc "…"` 里。前端取首词当宾语,不剥这层壳,
    # 一整列过程行全是「已执行 /bin/bash」,看不出做了什么。claude 那侧的 Bash 入参就是
    # 命令本身,没有这层包装,所以剥壳只在这个引擎需要。
    _SHELL_WRAP = re_mod.compile(r"^(?:[\w./-]*/)?(?:bash|zsh|sh)\s+-[a-zA-Z]*c\s+(.+)$", re_mod.S)

    @classmethod
    def _unwrap_cmd(cls, cmd):
        m = cls._SHELL_WRAP.match((cmd or "").strip())
        if not m:
            return cmd or ""
        inner = m.group(1).strip()
        if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
            inner = inner[1:-1]
        return inner or cmd or ""

    _NESTED_CMD = re_mod.compile(
        r'(?:["\']cmd["\']|\bcmd)\s*:\s*'
        r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`(?:[^`\\]|\\.)*`)', re_mod.S)
    _NESTED_TITLE = re_mod.compile(
        r'(?:["\']title["\']|\btitle)\s*:\s*'
        r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`(?:[^`\\]|\\.)*`)', re_mod.S)

    @staticmethod
    def _js_string(literal):
        """解析 Code Mode 包装里的普通 JS 字符串，不执行模型输出。

        单引号分支不能把已经正确解码的中文转成 UTF-8 字节后再按
        unicode_escape 展开，否则界面会出现 mojibake。这里只逐个处理
        JS 常用转义，未转义的 Unicode 原样保留。
        """
        try:
            if literal.startswith('"'):
                return json.loads(literal)
            if (len(literal) < 2 or literal[0] not in ("'", "`")
                    or literal[-1] != literal[0]):
                return ""
            body = literal[1:-1]
            out = []
            escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b",
                       "f": "\f", "v": "\v", "0": "\0"}
            i = 0
            while i < len(body):
                char = body[i]
                if char != "\\":
                    out.append(char)
                    i += 1
                    continue
                i += 1
                if i >= len(body):
                    break
                char = body[i]
                if char == "\n":
                    i += 1
                    continue
                if char == "\r":
                    i += 2 if i + 1 < len(body) and body[i + 1] == "\n" else 1
                    continue
                if char == "x" and re_mod.fullmatch(r"[0-9a-fA-F]{2}", body[i + 1:i + 3]):
                    out.append(chr(int(body[i + 1:i + 3], 16)))
                    i += 3
                    continue
                if char == "u" and re_mod.fullmatch(r"[0-9a-fA-F]{4}", body[i + 1:i + 5]):
                    out.append(chr(int(body[i + 1:i + 5], 16)))
                    i += 5
                    continue
                out.append(escapes.get(char, char))
                i += 1
            return "".join(out)
        except Exception:
            return ""

    @classmethod
    def _nested_commands(cls, raw):
        return [cls._unwrap_cmd(v) for v in
                (cls._js_string(m.group(1)) for m in cls._NESTED_CMD.finditer(raw or "")) if v]

    @classmethod
    def _nested_title(cls, raw):
        match = cls._NESTED_TITLE.search(raw or "")
        return cls._js_string(match.group(1)).strip() if match else ""

    # 检索式与路径长得像,正则的元字符是可靠的分界:一句给人读的说明里不会出现它们。
    # 前端同名的 HS_PATTERNISH 是同一条判据(frontend/app-chat.js)。
    _PATTERNISH = re_mod.compile(r"[|\\*+?^$\[\]()]")

    @staticmethod
    def _command_stages(cmd):
        """把命令拆成阶段。; && || | 都是分隔,引号里的不算——`grep -E "a|b"` 是一个阶段。"""
        out, cur, quote, s, i = [], [], "", str(cmd or ""), 0
        while i < len(s):
            c = s[i]
            if quote:
                cur.append(c)
                if c == quote and (i == 0 or s[i - 1] != "\\"):
                    quote = ""
            elif c in "\"'":
                quote = c
                cur.append(c)
            elif c in ";|&":
                if i + 1 < len(s) and s[i + 1] == c:
                    i += 1                      # && 与 || 是一个分隔,不是两个
                out.append("".join(cur))
                cur = []
            else:
                cur.append(c)
            i += 1
        out.append("".join(cur))
        return [x.strip() for x in out if x.strip()]

    @classmethod
    def _command_args(cls, seg):
        """这一段里够格当宾语的实参。选项、重定向、引号里的内容都不算。"""
        bare = re_mod.sub(r"(['\"]).*?\1", " ", str(seg), flags=re_mod.S)
        return [a for a in bare.split()[1:]
                if a and not a.startswith("-") and not re_mod.match(r"\d*[<>]", a)]

    @classmethod
    def _command_arg(cls, seg):
        args = cls._command_args(seg)
        base = os.path.basename(args[-1].rstrip("/")) if args else ""
        return "" if cls._PATTERNISH.search(base) else base

    # git 的全局选项:取值的那几个要连值一起剥
    _GIT_GLOBAL_VALUE = ("-C", "-c", "--git-dir", "--work-tree", "--namespace",
                         "--exec-path", "--super-prefix")

    @classmethod
    def _git_norm(cls, head):
        r"""把 git 的全局选项剥掉,只留 `git <子命令> …`。

        codex 惯用 `git -C /abs/path status`——它的 cwd 未必是仓根,加 -C 比先 cd 稳。
        而判子命令的正则是 `^git\s+status\b`,中间夹着 -C 与一长串路径就一条都不命中,
        于是一屏 git 卡片全退回命令原文(2026-08-20 用户实见:status / diff / log 六条
        全是 `git -C /Users/…/desktron diff…`)。剥完再判,与 `git status` 同一条路。
        """
        toks = head.split()
        if not toks or toks[0].lower() != "git":
            return head
        i = 1
        while i < len(toks):
            t = toks[i]
            if t in cls._GIT_GLOBAL_VALUE:
                i += 2          # 连它的值一起跳
                continue
            if t.startswith("-"):
                i += 1          # --no-pager / --git-dir=… / -P 这类不带独立值
                continue
            break               # 到子命令了
        return " ".join(["git"] + toks[i:])

    @classmethod
    def _command_summary_one(cls, cmd):
        """一条命令翻成「这一步想干什么」。翻不出返回空串,由调用处退回命令原文。

        只看第一个真正的动作那一段。后面的 | head、; echo、2>&1 是配料,不是这一步的目的——
        原来在整条命令上找第一个命中的词,于是 `ls 长路径 | head` 被说成「读取 head 后面
        那个文件」、`pytest x.py | tail` 被说成「读取」。拿本机会话里实录的 71 条单行命令
        实测,命中的 62 条里 29 条这样张冠李戴、22 条把 grep 的正则原样倒出来,真正说对的
        只有 11 条(2026-08-17)。逐条与前端 hsCmdIntent 对齐,同一份用例两侧同跑
        (test_frontend_tool_object.py 的跨语言一致性用例)。
        """
        raw = str(cmd or "")
        if "<<" in raw or "\n" in raw:
            return cls._script_summary(cmd)
        head = next((s for s in cls._command_stages(raw)
                     if not re_mod.match(r"cd(\s|$)", s)), "")
        if not head:
            return ""
        head = cls._git_norm(head)
        low = head.lower()
        obj = cls._command_arg(head)

        def say(verb):
            return (verb + " " + obj) if obj else ""

        if re_mod.match(r"(?:rg|grep)\b", low) or re_mod.match(r"git\s+grep\b", low):
            q = re_mod.search(r"(['\"])(.+?)\1", head, re_mod.S)
            pat = q.group(2) if q else next(
                (a for a in head.split()[1:] if a and not a.startswith("-")), "")
            # 检索式带正则元字符:倒出来是一串谁也读不懂的转义,不如原样的命令
            if not pat or cls._PATTERNISH.search(pat):
                return ""
            return "查找 " + pat[:28]
        if re_mod.match(r"git\s", low):
            # 子命令(diff / log …)是动词的一部分,不是宾语
            gobj = cls._command_arg(re_mod.sub(r"^\s*git\s+\S+", "git", head, flags=re_mod.I))

            def gsay(verb):
                return (verb + " " + gobj) if gobj else verb

            if re_mod.match(r"git\s+status\b", low):
                return "检查仓库状态"
            if re_mod.match(r"git\s+diff\b", low):
                return gsay("检查代码差异")
            if re_mod.match(r"git\s+show\b", low):
                return "查看提交差异"
            if re_mod.match(r"git\s+log\b", low):
                return gsay("查看提交历史")
            if re_mod.match(r"git\s+branch\b", low):
                return "查看分支"
            if re_mod.match(r"git\s+rev-parse\b", low):
                return "解析 Git 引用"
            if re_mod.match(r"git\s+ls-files\b", low):
                return "列出仓库文件"
            return ""
        if re_mod.search(r"\b(?:pytest|unittest)\b", low):
            return say("运行测试") or "运行测试"
        if "py_compile" in low or "node --check" in low or "ast.parse" in low:
            return say("检查语法") or "检查语法"
        if re_mod.match(r"(?:shasum|sha256sum|md5)\b", low):
            return say("核对")
        # -i 是就地改写,与只读的那几个不是一件事,说成「读取」是反的
        if re_mod.match(r"sed\b", low):
            return say("改写") if re_mod.search(r"\s-i\b", head) else say("读取")
        # wc 不在这里:它数行数不是读内容,而且常常一次给好几个文件
        if re_mod.match(r"(?:cat|head|tail|less|more)\b", low):
            return say("读取")
        if re_mod.match(r"(?:ls|tree)\b", low):
            return say("列出")
        if re_mod.match(r"find\b", low):
            n = re_mod.search(r"-name\s+['\"]?([^\s'\"]+)", head)
            if n and not cls._PATTERNISH.search(n.group(1)):
                return "查找文件 " + n.group(1)
            return ""
        if re_mod.match(r"curl\b", low):
            return "检查接口"
        if re_mod.match(r"(?:python\d*|node|bash|sh|zsh)\b", low):
            # 脚本名取第一个,不取末尾:后面跟的是脚本自己的参数
            s = next((a for a in cls._command_args(head)
                      if re_mod.search(r"\.(?:py|js|mjs|sh)$", a)), "")
            return "运行 " + os.path.basename(s) if s else ""
        return ""

    @classmethod
    def _script_summary(cls, cmd):
        raw = str(cmd or "")
        head = raw.splitlines()[0].strip()
        low = raw.lower()
        if re_mod.search(r"\b(pytest|unittest)\b", low):
            return "运行测试脚本"
        if "ast.parse" in raw or "py_compile" in low or "node --check" in low:
            return "运行语法检查脚本"
        if "subprocess" in low and "git" in low:
            return "运行 Git 检查脚本"
        if re_mod.search(r"\b(rg|grep)\b", low):
            return "运行代码搜索脚本"
        if re_mod.match(r"python\d*\b", head):
            return "运行 Python 脚本"
        if re_mod.match(r"node\b", head):
            return "运行 Node 脚本"
        if "<<" in head:
            return "运行脚本"
        return cls._command_summary_one(head)

    @classmethod
    def _field_text(cls, *vals):
        for val in vals:
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, list):
                txt = ", ".join(str(x).strip() for x in val if str(x).strip())
                if txt:
                    return txt
            if isinstance(val, dict):
                txt = cls._field_text(val.get("query"), val.get("queries"), val.get("pattern"))
                if txt:
                    return txt
        return ""

    @classmethod
    def _web_query(cls, item):
        args = item.get("arguments")
        if isinstance(args, str) and args.strip().startswith("{"):
            try:
                args = json.loads(args)
            except ValueError:
                pass
        return cls._field_text(item.get("query"), item.get("queries"), item.get("input"), args)

    _CODE_INTENT = re_mod.compile(r"^//\s*@intent\s*[:：]\s*(.+?)\s*$", re_mod.I)

    _INTENT_PLACEHOLDERS = {
        "意图", "本步意图", "本步目的", "目的", "摘要", "intent", "purpose",
    }

    @classmethod
    def _intent_placeholder(cls, value):
        """提示里的示意词不是摘要。模型偶尔会把 `# 意图` 原样照抄。"""
        text = str(value or "").strip().strip("<>《》【】[]（）()").strip().casefold()
        return text in cls._INTENT_PLACEHOLDERS

    @staticmethod
    def _command_source(command):
        """解开 Code Mode 工具参数，并清掉多行命令的显示引号。

        functions.exec 是自由文本工具，但 rollout 的 function_call.arguments 会把它
        存成 `{"input":"..."}`。这层不是命令正文；不解开的话 @intent 困在 JSON
        字符串里，界面只能显示整段参数。这里只接受唯一 input 字段的合法 JSON，
        不执行其中代码。

        实录形态是开头 `'# 意图`,末尾 `done"`。只处理「引号后紧跟 #」这一种,
        普通以引号字符串开头的命令不动。这里只用于 GUI 投影,不会改实际执行内容。
        """
        src = str(command or "").strip()
        if src.startswith("{"):
            try:
                wrapped = json.loads(src)
            except (TypeError, ValueError):
                wrapped = None
            if (isinstance(wrapped, dict) and set(wrapped) == {"input"}
                    and isinstance(wrapped.get("input"), str)):
                src = wrapped["input"].strip()
        if len(src) > 1 and src[0] in "\"'" and src[1] == "#":
            src = src[1:]
            if src and src[-1] in "\"'":
                src = src[:-1]
        return src

    @classmethod
    def _intent_marker(cls, command):
        """取意图标记里的原文，包括需要丢弃的占位词。"""
        lines = cls._command_source(command).splitlines()
        if not lines:
            return ""
        first = lines[0].strip()
        if first.startswith("#") and not first.startswith("#!"):
            return first.lstrip("#").strip()[:46]
        # 只看 JS 的前导注释区。进入代码正文后不再找,避免把字符串或示例里的 marker
        # 当成这次工具调用的意图。
        for line in lines:
            line = line.strip()
            if not line:
                continue
            match = cls._CODE_INTENT.match(line)
            if match:
                return match.group(1).strip()[:46]
            if line.startswith("// @exec:"):
                continue
            break
        return ""

    @classmethod
    def _intent_comment(cls, command):
        """模型自述的「这一步想干什么」(见 CodexAdapter.INTENT_RULE)。

        直接 shell 认首行 `# 意图`;Code Mode 认 JS 前导区的 `// @intent: 意图`,并允许
        工具自己的 `// @exec:` pragma 摆在它前面。没有显式意图时才去猜
        (_command_summary_one 的词表),而检索式与长管道是猜不出来的。
        shebang 不算:`#!/usr/bin/env bash` 说的是拿什么跑,不是想干什么。
        """
        value = cls._intent_marker(command)
        return "" if cls._intent_placeholder(value) else value

    @classmethod
    def _command_without_intent(cls, command):
        """意图已经进 description 后,从供展开区显示的命令里删掉那一行。"""
        src = cls._command_source(command)
        lines = src.splitlines()
        if not lines:
            return src
        first = lines[0].strip()
        if first.startswith("#") and not first.startswith("#!"):
            return "\n".join(lines[1:]).strip()
        # Code Mode 允许 @exec pragma 在 @intent 前。两行都是过程区元数据,一起隐藏。
        pragma = None
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            if cls._CODE_INTENT.match(line):
                del lines[i]
                if pragma is not None:
                    del lines[pragma]
                return "\n".join(lines).strip()
            if line.startswith("// @exec:"):
                pragma = i
                continue
            break
        return src

    @classmethod
    def _command_input(cls, raw):
        command = cls._command_source(cls._unwrap_cmd(str(raw or "")))
        marker = cls._intent_marker(command)
        intent = cls._intent_comment(command)
        if intent:
            return {"description": intent, "command": cls._command_without_intent(command)[:4000]}
        # 提示示例曾写成 `# 意图`,模型会偶尔原样照抄。它既不能当标题,也不属于真正命令；
        # 摘掉后再按命令词表降级，至少不让一整屏都显示同一个占位词。
        if marker and cls._intent_placeholder(marker):
            command = cls._command_without_intent(command)
        nested = cls._nested_commands(command)
        # 旧提示只要求 shell 的 `# 意图`,Code Mode 因此会把它写进嵌套 cmd 而不是 JS 外层。
        # 先兼容这批现存会话；新提示优先写外层 `// @intent:`,一组并行命令只需一个组意图。
        nested_intent = ""
        for item in nested:
            nested_intent = cls._intent_comment(item)
            if nested_intent:
                break
        if nested_intent:
            if len(nested) > 1:
                nested_intent += f" 等 {len(nested)} 项"
            return {"description": nested_intent[:46], "command": command[:4000]}
        title = cls._nested_title(command)
        actual = nested or [command]
        summary_actual = []
        for item in actual:
            nested_marker = cls._intent_marker(item)
            summary_actual.append(cls._command_without_intent(item)
                                  if nested_marker and cls._intent_placeholder(nested_marker)
                                  else item)
        summary = cls._command_summary_one(summary_actual[0]) if nested or not title else title
        # 翻不出意图时留空,让界面拿 command 那一栏按命令原文显示(等宽,自己再翻一次)。
        # 但包在 tools.exec_command 壳里的那种不行:command 存的是整段包装代码,原样摆出来
        # 是一行「const results = await Promise.all([…」,谁也看不出跑了什么。那种退回壳里
        # 的第一条命令。
        if not summary and nested:
            summary = re_mod.sub(r"\s+", " ", summary_actual[0]).strip()
        if len(actual) > 1 and summary:
            summary += f" 等 {len(actual)} 项"
        return {"description": summary[:46], "command": command[:4000]}

    _CHANGE_KIND = {"add": "新增", "update": "修改", "delete": "删除", "rename": "改名"}

    @staticmethod
    def _change_kind(change):
        """改动种类。两条路给的形状不同:`codex exec --json` 给字符串 "update",
        app-server 给对象 {"type": "update", "move_path": null}。原来直接拿它当字典键,
        遇到 app-server 那种就是 TypeError(cannot use 'dict' as a dict key),整个回合
        在改文件那一步炸掉——2026-08-10 从日志里逮到的正是这一条。"""
        k = (change or {}).get("kind")
        if isinstance(k, dict):
            k = k.get("type")
        k = str(k or "")
        return CodexAdapter._CHANGE_KIND.get(k, k or "改动")

    def _item_tool(self, kind, item):
        """这一条 item 画成哪种工具卡。默认按 item 类型查表;派生类可以按 item 的内容细分——
        例如兼容引擎把 Grep / Read / Bash 全报成 commandExecution,只看类型的话它们都成了「执行」。"""
        return self.ITEM_TOOL.get(kind)

    @classmethod
    def _tool_input(cls, item):
        """把 item 上那几个字段摊成前端认得的入参形状(command / file_path / pattern / description)。"""
        t = item.get("type")
        if t == "command_execution":
            return cls._command_input(item.get("command"))
        if t in ("file_change", "patch_apply"):
            ch = [c for c in (item.get("changes") or []) if isinstance(c, dict) and c.get("path")]
            packed, left = [], 8000
            for c in ch:
                diff = str(c.get("diff") or c.get("unified_diff") or "")[:left]
                left = max(0, left - len(diff))
                if diff:
                    packed.append({"file_path": c["path"], "diff": diff})
            if len(ch) == 1:
                return packed[0] if packed else {"file_path": ch[0]["path"]}
            if not ch:
                return {}
            # 多文件:给文件名,不给「改动 3 个文件」。后者占着宾语的位置却什么也没说,
            # 而这一行最该认出来的正是「动了哪个东西」。多于三个才折成计数。
            names = [os.path.basename(c["path"]) for c in ch]
            head = "、".join(names[:3])
            out = {"description": head if len(names) <= 3 else f"{head} 等 {len(names)} 个文件"}
            if packed:
                out["changes"] = packed
            return out
        if t == "web_search":
            q = cls._web_query(item)
            return {"pattern": q, "description": "搜索 " + q if q else "搜索"}
        if t == "mcp_tool_call":
            return {"description": f'{item.get("server") or "mcp"}.{item.get("tool") or ""}'.strip(".")}
        if t == "collab_agent":
            # 宾语取派发时给的提示词头一句:它就是「让这个子代理去做什么」。
            # 拿工具名(spawnAgent)当宾语等于把动作说两遍。
            head = str(item.get("prompt") or "").strip().splitlines()
            desc = (head[0] if head else "") or {"spawnAgent": "派出子代理", "sendInput": "追加输入",
                                                 "resumeAgent": "继续子代理", "wait": "等子代理",
                                                 "closeAgent": "收掉子代理"}.get(item.get("tool") or "", "子代理")
            return {"description": desc[:120]}
        if t == "sub_agent_activity":
            return {"description": {"started": "子代理已启动", "interacted": "与子代理往来",
                                    "interrupted": "子代理被打断"}.get(item.get("kind") or "", "子代理")}
        if t == "todo_list":
            return {"description": "更新待办"}
        return {}

    @staticmethod
    def _tool_output(item):
        t = item.get("type")
        if t == "command_execution":
            out = item.get("aggregated_output") or ""
            code = item.get("exit_code")
            return (out + (f"\n(退出码 {code})" if code not in (0, None) else ""))[:8000]
        if t in ("file_change", "patch_apply"):
            # 改了哪些文件、各是什么改动。不给的话展开区只剩一个「…」——
            # 界面上是一条能点开、点开却什么都没有的行。
            ch = [c for c in (item.get("changes") or []) if isinstance(c, dict)]
            # app-server 的 fileChange 还带着 diff 正文,有就一并给出——展开区从一张
            # 「改了哪几个文件」的清单变成看得见改了什么。exec --json 那条路没有,退回清单。
            out = []
            for c in ch:
                out.append(f'{CodexAdapter._change_kind(c)}  {c.get("path")}')
                diff = str(c.get("diff") or c.get("unified_diff") or "").strip()
                if diff:
                    out.append(diff)
            return "\n".join(out)[:8000]
        if t == "todo_list":
            items = item.get("items") or []
            return "\n".join(f'- {x.get("text") or x}' for x in items)[:8000]
        for k in ("result", "output", "summary"):
            if item.get(k):
                return result_text(item[k])[:8000]
        return ""

    _DONE = {"completed": "done", "in_progress": "running", "failed": "error"}

    # ---- 搬迁 ----
    # codex 没有 serve,记录是自己的 rollout 文件,所以这条路更像 claude 那侧:读文件、翻成契约。
    # 但形状不同——rollout 里同一句话会出现两次(event_msg 是给人看的那份,response_item 是
    # 送给模型的那份,后者还夹着 developer 角色的注入与 <recommended_plugins> 之类的模板),
    # 所以对话取 event_msg,工具取 response_item,两者互不重叠。
    _ROLLOUT_TOOL = {"exec": "bash", "shell": "bash", "apply_patch": "patch"}

    def transcript_stat(self, sid):
        """rollout 的大小与修改时刻。界面每十秒查一次,变了才去重建——不能拿整份解析来轮询。

        这一条决定了「在 macOS 终端里跟同一段会话聊天,GUI 会不会跟着更新」:此前只有
        claude 实现了它,codex 拿到空对象,前端在 `if (!st.size) return` 就退了,
        于是 codex 的 GUI 完全不跟进(2026-08-10 实见)。
        """
        esid = (self.index.get(sid) or {}).get("engine_sid")
        if not esid:
            return {}
        path = self._rollout_path(esid)
        if not path:
            return {}
        try:
            st = os.stat(path)
        except OSError:
            return {}
        return {"size": st.st_size, "mtime": int(st.st_mtime * 1000)}

    # Codex 的目标不是 Claude 那种 transcript attachment。模型调用 create_goal / update_goal
    # 后,工具的成功回执写进 rollout,形如 {"goal":{"objective":…,"status":"active"}}。
    # 只认这份回执,不拿用户输入的 /goal 文本猜结果:工具可能失败,模型也可能把短目标展开成
    # 更完整的交付条件。缓存按完整 JSONL 行推进,332MB 的长会话首次从末尾找一次,之后只扫增量。
    _goal_rollout_cache = {}      # path -> {ino, size, goal}
    _goal_rollout_lock = threading.Lock()

    def goal(self, sid):
        """返回 Codex 已登记的最新目标,形状与 Claude goal() 保持一致。"""
        esid = (self.index.get(sid) or {}).get("engine_sid")
        if not esid:
            return {}
        path = self._rollout_path(esid)
        if not path:
            return {}
        try:
            st = os.stat(path)
            end = self._goal_safe_end(path, st.st_size)
        except OSError:
            return {}
        with self._goal_rollout_lock:
            hit = self._goal_rollout_cache.get(path)
        same = bool(hit and hit.get("ino") == st.st_ino and end >= hit.get("size", 0))
        if same and end == hit.get("size"):
            return copy.deepcopy(hit.get("goal") or {})
        start = hit.get("size", 0) if same else 0
        got = self._goal_rollout_scan(path, str(esid), start, end)
        if not got and same:
            got = hit.get("goal") or {}
        with self._goal_rollout_lock:
            self._goal_rollout_cache[path] = {
                "ino": st.st_ino, "size": end, "goal": copy.deepcopy(got)}
        return copy.deepcopy(got)

    @staticmethod
    def _goal_safe_end(path, size):
        """只把换行结束的 JSONL 计入扫描,避免正撞上 rollout 的半行写入。"""
        if size <= 0:
            return 0
        with open(path, "rb") as f:
            f.seek(size - 1)
            if f.read(1) == b"\n":
                return size
            pos = size
            while pos:
                start = max(0, pos - 65536)
                f.seek(start)
                chunk = f.read(pos - start)
                at = chunk.rfind(b"\n")
                if at >= 0:
                    return start + at + 1
                pos = start
        return 0

    @classmethod
    def _goal_rollout_scan(cls, path, esid, start, end):
        """从指定增量中倒找最新目标回执；候选还要与真实目标工具调用配对。"""
        if end <= start:
            return {}
        import mmap
        try:
            with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                hi = end
                markers = (b'\\"goal\\"', b'"goal"')
                while hi > start:
                    at = max(mm.rfind(markers[0], start, hi), mm.rfind(markers[1], start, hi))
                    if at < 0:
                        break
                    lo = mm.rfind(b"\n", 0, at) + 1
                    nl = mm.find(b"\n", at, end)
                    line_end = end if nl < 0 else nl
                    try:
                        row = json.loads(mm[lo:line_end])
                    except (ValueError, UnicodeDecodeError):
                        row = {}
                    got = cls._goal_from_rollout_output(row, esid)
                    pay = row.get("payload") or {}
                    if got and cls._goal_output_has_call(mm, lo, pay.get("call_id")):
                        return got
                    hi = lo if lo < hi else at
        except (OSError, ValueError):
            return {}
        return {}

    @staticmethod
    def _goal_output_has_call(mm, before, call_id):
        """排除普通命令恰好打印了一段 goal JSON 的情况。"""
        call_id = str(call_id or "")
        if not call_id:
            return False
        needle = call_id.encode()
        hi = before
        while hi > 0:
            at = mm.rfind(needle, 0, hi)
            if at < 0:
                return False
            lo = mm.rfind(b"\n", 0, at) + 1
            nl = mm.find(b"\n", at, before)
            line_end = before if nl < 0 else nl
            try:
                row = json.loads(mm[lo:line_end])
            except (ValueError, UnicodeDecodeError):
                row = {}
            pay = row.get("payload") or {}
            if str(pay.get("call_id") or "") == call_id and pay.get("type") in (
                    "custom_tool_call", "function_call"):
                name = str(pay.get("name") or "").lower()
                raw = str(pay.get("input") or pay.get("arguments") or "")
                return (name in ("create_goal", "update_goal") or
                        bool(re_mod.search(r"\btools\.(?:create_goal|update_goal)\s*\(", raw)))
            hi = lo if lo < hi else at
        return False

    @classmethod
    def _goal_from_rollout_output(cls, row, esid):
        pay = row.get("payload") or {}
        if row.get("type") != "response_item" or pay.get("type") not in (
                "custom_tool_call_output", "function_call_output"):
            return {}
        out = pay.get("output")
        if isinstance(out, list):
            text = "".join(str(x.get("text") or "") for x in out if isinstance(x, dict))
        elif isinstance(out, dict):
            text = json.dumps(out, ensure_ascii=False)
        else:
            text = str(out or "")
        if '"goal"' not in text:
            return {}
        decoder = json.JSONDecoder()
        for m in re_mod.finditer(r"\{", text):
            try:
                obj, _ = decoder.raw_decode(text[m.start():])
            except ValueError:
                continue
            goal = obj.get("goal") if isinstance(obj, dict) else None
            if not isinstance(goal, dict):
                continue
            thread_id = str(goal.get("threadId") or goal.get("thread_id") or "")
            condition = str(goal.get("objective") or "").strip()
            status = str(goal.get("status") or "").lower()
            if thread_id != esid or not condition or status not in (
                    "active", "complete", "completed", "blocked"):
                continue
            at = int(goal.get("updatedAt") or goal.get("updated_at") or 0)
            if at and at < 100_000_000_000:
                at *= 1000
            return {"condition": condition, "met": status != "active",
                    "status": status, "at": at}
        return {}
