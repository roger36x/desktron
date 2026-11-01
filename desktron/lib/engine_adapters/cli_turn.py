# 由 engine_adapters.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)
import collections
import copy
import json
import os
import queue
import re as re_mod
import subprocess
import threading
import time
import uuid
from credstore import secrets_env
from deskcfg import UI_PORT
from app_control.capability import issue as issue_app_control_capability
from .bus import _TurnWatch
from .execution_returns import append_result, attach_return, find_tool_part, handed_off
from .persist import _EngineProc, _PERSIST, _PERSIST_LOCK, _PersistUnusable, _persist_drop, _persist_trim
from .response_timing import ResponseTiming, mark as timing_mark
from .attachment_context import (attachment_name, attachment_bytes, stored_context,
                                 AttachmentInputError)


class TurnRunnerMixin:

    @staticmethod
    def _turn_usage(ctx):
        def as_int(value):
            try:
                if isinstance(value, bool) or value is None:
                    return None
                n = int(value)
                return n if n >= 0 else None
            except (TypeError, ValueError):
                return None
        detail = ctx.get("_token_usage") if isinstance(ctx.get("_token_usage"), dict) else {}
        tokens = as_int(detail.get("total"))
        if tokens is None:
            tokens = as_int(ctx.get("tokens"))
        if tokens is None:
            tokens = as_int(ctx.get("_pi_tokens"))
        cached = as_int(detail.get("cached"))
        if cached is None:
            cached = as_int(ctx.get("cached_tokens"))
        if cached is None:
            cached = as_int(ctx.get("_pi_cache_read"))
        if tokens is None and cached is None:
            return None
        tokens, cached = int(tokens or 0), int(cached or 0)
        if tokens <= 0 and cached <= 0:
            return None
        out = {"tokens": tokens, "cached": cached}
        for key in ("input", "output", "cache_write"):
            value = as_int(detail.get(key))
            if value is not None:
                out[key] = value
        return out

    def prompt_async(self, sid, text, opts=None, atts=None, refresh_opts=None,
                     peer_request_id="", peer_head="", client_request_id="",
                     confirmed_peer_cancel="", _peer_admitted=False,
                     _peer_prepare_run=None, _peer_fence_token=None,
                     _before_dispatch=None):
        """发起一个回合。opts 是发起那一刻的档位快照;要跟上人中途的改动,再给一个
        refresh_opts —— 排到队里的回合真正开跑时用它重取一次(见 _run_serial)。"""
        if peer_request_id:
            if atts:
                raise ValueError('隔离问询不接受附件')
            return self.peer_prompt_async(
                sid, text, opts or {}, refresh_opts=refresh_opts,
                peer_request_id=peer_request_id, peer_head=peer_head)
        delivery = getattr(self, 'delivery', None)
        rid = str(client_request_id or (('request_' + uuid.uuid4().hex) if delivery else ''))
        delivery_claimed = False
        if getattr(self, '_shutting_down', False):
            raise RuntimeError('Desk 正在退出，消息未发送')
        if delivery and rid:
            # 上一轮若因意外退出而结果未知，先按证据自动收口；旧进程仍存活时照旧拒绝。
            self._delivery_autoresolve(sid)
            receipt, fresh = delivery.claim(sid, rid, {
                'text':str(text or ''),
                'attachments':[dict(row) for row in (atts or []) if isinstance(row, dict)],
            })
            if not fresh:
                return receipt
            delivery_claimed = True
        own_peer_fence = not _peer_admitted
        peer_prepare_run = _peer_prepare_run
        token = _peer_fence_token if isinstance(_peer_fence_token, dict) else {
            'owned': False, 'transferred': False}
        if own_peer_fence:
            peer_prepare_run = self._peer_user_admit(sid, confirmed_peer_cancel)
            token['owned'] = True
        try:
            return self._prompt_user_async(
                sid, text, opts, atts, refresh_opts, peer_head, rid,
                peer_prepare_run=peer_prepare_run, peer_fence_token=token,
                delivery_claimed=delivery_claimed, before_dispatch=_before_dispatch)
        except Exception as exc:
            if delivery_claimed:
                delivery.reject(sid, rid, reason=str(exc)[:500])
            raise
        finally:
            if own_peer_fence and token.get('owned') and not token.get('transferred'):
                token['owned'] = False
                self._peer_user_release(sid)

    def _prompt_user_async(self, sid, text, opts, atts, refresh_opts, peer_head,
                           client_request_id, peer_prepare_run=None, peer_fence_token=None,
                           delivery_claimed=False, before_dispatch=None):
        requested_at = time.monotonic()
        delivery = getattr(self, 'delivery', None)
        rid = str(client_request_id or (('request_' + uuid.uuid4().hex) if delivery else ''))
        if getattr(self, '_shutting_down', False):
            raise RuntimeError('Desk 正在退出，消息未发送')
        if delivery and rid and not delivery_claimed:
            self._delivery_autoresolve(sid)
            receipt, fresh = delivery.claim(sid, rid, {
                'text':str(text or ''),
                'attachments':[dict(row) for row in (atts or []) if isinstance(row, dict)],
            })
            if not fresh:
                return receipt
        # 用户一回来就撤掉空闲 recap；peer 支线已在上面的原子准入闸按请求号处理。
        self._recap_cancel(sid)
        import self_update
        if not self_update.begin_work(user_activity=True, restart_blocking=False):
            if delivery and rid:
                delivery.reject(sid, rid, reason='work_admission_closed')
            raise RuntimeError("Desk 正在准备强制更新，请稍后再发送")
        local = getattr(self, '_delivery_local', None)
        def run():
            try:
                try:
                    self._peer_wait_prepared(sid, peer_prepare_run)
                except Exception as exc:
                    with self._lock:
                        target_exists = sid in self.index
                    if target_exists and delivery and rid:
                        delivery.reject(sid, rid, reason=str(exc)[:500])
                    requests = getattr(self, 'requests', None)
                    if (target_exists and rid and requests is not None
                            and hasattr(requests, 'update')):
                        requests.update(sid, rid, 'rejected', reason=str(exc)[:500])
                    return
                if local is not None:
                    local.rid = rid
                self._run_serial(sid, text, opts or {}, atts or [], refresh_opts, "", peer_head,
                                 requested_at=requested_at, client_request_id=rid,
                                 _peer_fence_token=peer_fence_token,
                                 _before_dispatch=before_dispatch)
            finally:
                if isinstance(peer_fence_token, dict) and peer_fence_token.get('owned'):
                    peer_fence_token['owned'] = False
                    self._peer_user_release(sid)
                if local is not None:
                    local.rid = ''
                self_update.finish_work(restart_blocking=False)
        try:
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            if isinstance(peer_fence_token, dict):
                peer_fence_token['transferred'] = True
        except Exception:
            self_update.finish_work(restart_blocking=False)
            if delivery and rid:
                delivery.reject(sid, rid, reason='thread_start_failed')
            raise
        return delivery.get(sid, rid) if delivery and rid else None

    def _run_serial(self, sid, text, opts=None, atts=None, refresh_opts=None,
                    peer_request_id="", peer_head="", requested_at=None, client_request_id="",
                    _peer_fence_token=None, _before_dispatch=None):
        """同一会话一次只允许一个回合进入适配器状态机。

        Codex app-server 自己也有 writer 锁，但那道锁位于 `_live` 赋值之后：两个入口同时发话时，
        后一轮会先覆盖前一轮的控制面状态，再阻塞在引擎锁上。把边界提到通用层后，所有引擎都
        对外提供同一语义，同时不改变各自的中断协议。
        """
        peer_request_id = str(peer_request_id or "")
        if peer_request_id:
            # All supported callers are redirected by prompt_async.  A direct
            # internal call must fail closed rather than recreate a fake user turn.
            raise RuntimeError('peer ask 必须走临时隔离支线')
        with self._lock:
            guard = self._turn_locks.setdefault(sid, threading.Lock())
        try:
            with guard:
                if isinstance(_peer_fence_token, dict) and _peer_fence_token.get('owned'):
                    with self._lock:
                        valid = (sid in self.index
                                 and not getattr(self, '_shutting_down', False)
                                 and int(self._peer_blocks.get(sid) or 0) == 1)
                    _peer_fence_token['owned'] = False
                    self._peer_user_release(sid)
                    if not valid:
                        raise RuntimeError('目标会话已删除或正在执行控制操作，消息未发送')
                if callable(_before_dispatch):
                    _before_dispatch()
                # 排队消息可能等上一轮结束；要在拿到锁后再取消一次,撤掉上一轮刚布的计时器。
                self._recap_cancel(sid)
                # 某些会话门面需要在下一回合真正开始前完成持久状态迁移。钩子位于
                # 会话串行锁内、用户消息落盘前；失败会阻止本轮，不会留下“已发送”气泡。
                before_turn = getattr(self, '_before_turn_start', None)
                try:
                    import self_update
                    delivery = getattr(self, 'delivery', None)
                    if delivery and client_request_id:
                        blocked = delivery.blocking_uncertain(sid, exclude=client_request_id)
                        if blocked:
                            from .delivery import DeliveryConflict
                            raise DeliveryConflict('上一条请求可能已经执行；请先核验后再继续',
                                                   code='delivery_uncertain')
                        receipt = delivery.get(sid, client_request_id)
                        if receipt.get('status') in ('dispatched', 'uncertain'):
                            from .delivery import DeliveryConflict
                            raise DeliveryConflict('上一条请求可能已经执行；请先核验后再继续',
                                                   code='delivery_uncertain')
                        if receipt.get('status') in ('rejected', 'cancelled'):
                            from .delivery import DeliveryConflict
                            raise DeliveryConflict('请求已在派发前取消', code='prompt_not_started')
                    if getattr(self, '_shutting_down', False):
                        if before_turn is not None:
                            before_turn(sid)  # Preserve facade-specific rejection receipts/errors.
                        raise RuntimeError('Desk 正在退出，排队消息未发送，请重新打开后继续')
                    if self_update.external_draining():
                        raise RuntimeError('Desk 正在重启，排队消息未发送，请稍后重试')
                    if before_turn is not None:
                        before_turn(sid)
                except Exception as exc:
                    message = str(exc) or '会话状态迁移失败'
                    self.bus.publish({'type':'session.error','properties':{
                        'sessionID':sid,'requestID':client_request_id,
                        'error':{'message':message,'code':'prompt_not_started'}}})
                    raise
                # 档位也要在这一刻重取,不能用发起时那份快照。模型是进程启动参数(见
                # build_cmd 的 --model 与 _persist_key),在跑的回合改不了,只能等下一个回合
                # 换代生效;而这一句消息排在队里时人往往正是在改档位——用快照的话,人改完
                # 看着菜单已经是新模型,排在前面的几轮却仍旧按旧模型跑,表现成「切换不生效」
                # (2026-09-04 用户实见:菜单圆点在 Opus 5,档位钮与实跑都是 Fable 5.1)。
                # 队列里没有「每条消息各自的档位」这回事,档位是会话的当前属性,以最新的为准。
                if refresh_opts is not None:
                    try:
                        fresh = refresh_opts()
                    except Exception as exc:
                        # Ordinary option refresh remains best-effort. Internal
                        # queued turns may explicitly fence an engine/session
                        # change; swallowing that signal would dispatch them to
                        # the newly selected engine.
                        if getattr(exc, 'abort_queued_turn', False):
                            raise
                        fresh = None
                    if isinstance(fresh, dict) and fresh:
                        opts = fresh
                if peer_head:
                    opts = {**(opts or {}), "_peer_head": peer_head}
                try:
                    if requested_at is not None:
                        self._trace(sid, {"ev": "response.queue", "wait_ms": round(
                            (time.monotonic() - requested_at) * 1000, 2)})
                    self._run(sid, text, opts, atts)
                except Exception as exc:
                    # Attachment preparation failed before a user record or a
                    # native turn existed. Correlate rejection like the pre-turn hook.
                    if getattr(exc, 'code', None) in ('invalid_image', 'invalid_attachment'):
                        self.bus.publish({'type':'session.error','properties':{
                            'sessionID':sid,'requestID':client_request_id,
                            'error':{'message':str(exc),'code':'prompt_not_started','stage':'attachment'}}})
                    raise
        finally:
            delivery = getattr(self, 'delivery', None)
            if delivery and client_request_id:
                row = delivery.get(sid, client_request_id)
                if row.get('status') == 'accepted':
                    delivery.reject(sid, client_request_id, reason='failed_before_store')
                elif row.get('status') in ('storing', 'stored'):
                    row = delivery.require_cleanup(sid, client_request_id)
                    self._finish_delivery_cleanup(sid, row)
                elif (row.get('status') == 'cleanup_required'
                      and row.get('cleanup_from') == 'storing'):
                    self._finish_delivery_cleanup(sid, row)
            resume_recall = getattr(self, "resume_pending_recall", None)
            if resume_recall:
                resume_recall(sid)

    def _stash(self, sid, atts):
        """附件落盘,返回 [(种类, 路径, 名字, media_type, base64)]。

        图片直接作为 image 块喂给模型;其它文件只落盘、把路径写进正文,由模型用读文件的工具去看
        ——文本、代码、日志动辄几十 KB,塞进对话上下文既贵又常常超限,而引擎本来就会读文件。
        两类都留一份实体,回头还能翻。"""
        out = []
        if not atts:
            return out
        d = os.path.join(self.dir, "uploads", sid)
        created = []
        try:
            os.makedirs(d, exist_ok=True)
            for a in atts:
                name = attachment_name(a.get("name"))
                raw = attachment_bytes(a)
                # Multiple pasted images can share a name and millisecond. Never
                # overwrite an earlier attachment while its URL is already durable.
                path = os.path.join(d, f"{int(time.time() * 1000)}_{uuid.uuid4().hex}_{name}")
                with open(path, "xb") as f:
                    created.append(path)
                    f.write(raw)
                out.append((a.get("type"), path, name, a.get("media_type") or "", a.get("data_b64") or ""))
        except (OSError, AttachmentInputError) as exc:
            for path in created:
                try: os.unlink(path)
                except OSError: pass  # Only this unaccepted batch's new files are eligible.
            if isinstance(exc, AttachmentInputError):
                raise
            raise AttachmentInputError('附件无法保存，消息未发送') from exc
        return out

    # ---- 内部 ----
    def abort(self, sid, recall=False, client_request_id=''):
        """中止在跑的回合。recall=True 是「撤回」:这句话还没被处理,连同刚落盘的用户消息
        一起撤掉,正文交回前端填进输入框重编辑。返回撤回的正文(没撤回则空串)。"""
        ctx = self._live.get(sid)
        rid = str(client_request_id or (ctx or {}).get('client_request_id') or '')
        if not ctx:
            if self.delivery and rid:
                receipt = self.cancel_unsent_delivery(sid, rid)
                if receipt.get('status') in ('cancelled', 'rejected'):
                    return {'text':'', 'request':receipt, **self.turn_state(sid)}
            return None
        if recall and self.delivery and rid:
            receipt = self.delivery.get(sid, rid)
            if receipt.get('status') in ('dispatched', 'uncertain'):
                recall = False
        ctx["aborted"] = "recall" if recall else "interrupt"
        ctx["interrupt_id"] = ctx.get("interrupt_id") or f"int-{uuid.uuid4()}"
        self._turn_state_publish(sid, ctx, self.TURN_CANCELLING)
        proc = ctx.get("proc")
        # 软打断优先:往控制通道写一帧,让引擎自己收住这一回合,进程留着。杀进程会把刚建立
        # 起来的上下文与预填一起丢掉,下一回合退回冷启动——常驻带来的那点提速,每打断一次
        # 就消失一次。软打断没被认下来的,3 秒后仍旧退回杀进程。
        # 软打断不要求 proc 在手:codex 的回合活在常驻 app-server 那侧,按得足够快时
        # ctx["proc"] 还没回填,而回合已经发出去了。这里若因为没有 proc 就跳过,那一回合
        # 既停不下来,前端又当它已经结束,下一条消息就撞上同一条 thread 的写入权。
        if self._soft_interrupt(sid, proc):
            if proc and self.interrupt_kill:
                threading.Timer(3, lambda: self._interrupt_fallback(sid, ctx, proc)).start()
        elif proc and proc.poll() is None and self.interrupt_kill:
            try:
                proc.kill()
            except Exception:
                pass
            _persist_drop(sid)   # 杀掉的那个就是池里的,不摘掉下一回合会去复用一个死进程
        gone = self._pop_last_user_record(sid) if recall else None
        if self.delivery and rid and recall:
            self.delivery.reject(sid, rid, reason='user_recalled', cancelled=True)
        if recall:
            # 以真正被摘掉的用户消息时间为准。live ctx 可能在极快的连续发送/撤回中已经
            # 指向下一轮；用它的 t0 会让 cut 起点偏晚，刷新重建时上一条消息重新出现。
            self._mark_recall(sid, (gone or {}).get("at") or ctx.get("t0") or 0)
        return {"text": (gone or {}).get("text", ""), **self.turn_state(sid)}

    # 引擎支持的软打断控制帧。形状取自 claude 二进制:
    # {type:"control_request", request_id, request:{subtype:"interrupt", cancel_queued:true}}
    # 引擎收到后收住当前回合、照常发 result(terminal_reason 是 aborted_streaming 或
    # aborted_tools),进程不退。不支持的引擎覆盖成 None,自动退回杀进程。
    interrupt_request = None

    # 软打断没落地时是否退回杀进程。对每回合独立起进程的引擎,杀的就是这一回合,是对的;
    # 对 codex 这种回合活在常驻 app-server 里的,ctx["proc"] 是整条连接而不是回合,
    # 杀它既停不下回合,又要等它死透才放开 thread 的写入权,下一条消息直接撞上。
    interrupt_kill = True

    def _esid_of(self, sid):
        return (self.index.get(sid) or {}).get("engine_sid") or ""

    def _sid_by_esid(self, esid):
        for sid, row in list(self.index.items()):
            if isinstance(row, dict) and row.get("engine_sid") == esid:
                return sid
        return ""

    def engine_has_history(self, sid, engine_sid):
        """稳定引擎 id 是否已经对应至少一个已启动回合。

        多数引擎在首轮开始后才返回 id，因此默认等价于 bool(engine_sid)。允许能预分配 id 的
        引擎覆盖，避免把一段尚未发出首轮的新会话误判成续接。
        """
        return bool(engine_sid)

    def _soft_interrupt(self, sid, proc):
        """请引擎自己停下。写成功返回 True,不支持或写失败返回 False,由调用方退回杀进程。"""
        if not self.interrupt_request or not proc or not proc.stdin:
            return False
        frame = {"type": "control_request", "request_id": f"int-{uuid.uuid4()}",
                 "request": dict(self.interrupt_request)}
        try:
            proc.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            return True
        except Exception:
            return False

    def _interrupt_fallback(self, sid, ctx, proc):
        """软打断的兜底。回合还挂在那里就说明引擎没认这一帧,退回杀进程——
        打断这个动作不能有「按了没反应」这一档。"""
        if self._live.get(sid) is not ctx or proc.poll() is not None:
            return
        try:
            proc.kill()
        except Exception:
            pass
        _persist_drop(sid)

    def _mark_recall(self, sid, t0):
        """记下被撤回那一回合的时刻区间。存储只是引擎记录的投影,光把消息从存储里摘掉
        拦不住下一次重建按记录读回来,得让重建也认这笔账。
        区间右端取此刻:进程已经杀掉,它写进记录的条目时刻都不会晚于这一刻,
        不留余量——留了会把用户紧接着重发的那一条也一起吞掉。"""
        if not t0:
            return
        with self._lock:
            e = self.index.get(sid)
            if e is None:
                return
            cuts = e.setdefault("recall_cuts", [])
            cuts.append([int(t0), int(time.time() * 1000)])
            del cuts[:-50]
            self._save_index()

    def _drop_since(self, sid, t0):
        """把这一回合已经被导入进存储的那份摘掉,再落自己这份。

        两套 id 会撞在一起:认领是以引擎记录为准整份重建存储,消息 id 用记录里的 uuid;
        实跑收尾是自己追加一条,id 用时间戳。回合还在跑时刷新页面会触发一次认领,那时记录里
        已经有这一回合的半截,于是它先以 uuid 的身份进了存储;等回合真正收尾,这里再追加一条
        完整的,两条互不相认——界面上就是同一回合前后出现两次,前一次还是残的。
        按时刻切:回合开始之后成文的助手消息只可能是它自己。"""
        if not t0:
            return
        msgs = self.messages(sid)
        keep = [m for m in msgs
                if not ((m.get("info") or {}).get("role") == "assistant"
                        and ((m.get("info") or {}).get("at") or 0) >= t0)]
        if len(keep) == len(msgs):
            return
        self._rewrite_msgs(sid, keep)

    # 记录文件的重写:临时名要唯一,同一段会话要排队。
    # 几条路都会整份重写这份 jsonl——撤回、认领导入、翻页对账——而它们分别跑在回合线程、
    # HTTP 线程与后台线程上。原来各自写死一个 `<sid>.jsonl.tmp`:两个人同时写同一个临时名,
    # 先改名的那个把它取走,后到的 os.replace 就报 FileNotFoundError,那一次落盘整个丢掉。
    # 表现是切引擎时过程信息消失、Esc 报错(撤回也走这条路)。
    _MSG_LOCKS = {}
    _MSG_LOCKS_GUARD = threading.Lock()

    @classmethod
    def _msg_lock(cls, path):
        with cls._MSG_LOCKS_GUARD:
            return cls._MSG_LOCKS.setdefault(path, threading.Lock())

    def _rewrite_msgs(self, sid, msgs=None, *, transform=None):
        path = self._msg_path(sid)
        with self._msg_lock(path):
            try:
                with open(path, encoding="utf-8") as source:
                    current = [json.loads(line) for line in source if line.strip()]
            except FileNotFoundError:
                current = []
            if transform is not None:
                # 需要读改写原子性时，在同一把追加/重写锁内读取最新文件。
                msgs = transform(current)
            from peer_metadata import restore
            restore(path, msgs, current)
            tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
            with open(tmp, "w", encoding="utf-8") as f:
                for m in msgs:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
            os.replace(tmp, path)
            self._turn_index(sid).refresh()

    def _restore_peer_heads(self, sid, msgs):
        from peer_metadata import restore
        path = self._msg_path(sid)
        with self._msg_lock(path):
            return restore(path, msgs, self.messages(sid))

    def _pop_last_user_record(self, sid):
        """把末尾用户消息从存储里摘掉，返回正文及其权威落盘时间。只在末尾是真人发言时动手——
        已经有回复接在后面,就不是「还没开始处理」,不该撤;压缩行、目标行与自续轮那条
        分割线在契约里同样是 role=user,但它们不是谁打的字,摘掉等于把一段历史撤没了。"""
        msgs = self.messages(sid)
        if not msgs or not self._turn_start(msgs[-1]):
            return None
        gone = msgs.pop()
        self._rewrite_msgs(sid, msgs)
        self._forget_reference_attachments(sid, message_id=(gone.get('info') or {}).get('id'))
        return {
            "text": "".join(q.get("text") or "" for q in (gone.get("parts") or [])
                            if q.get("type") == "text"),
            "at": int((gone.get("info") or {}).get("at") or 0),
        }

    def _pop_last_user(self, sid):
        """兼容旧调用：只返回被撤回消息的正文。"""
        return (self._pop_last_user_record(sid) or {}).get("text", "")

    def _run(self, sid, text, opts=None, atts=None):
        if getattr(self, '_shutting_down', False):
            raise RuntimeError('Desk 正在退出，消息未发送')
        # Every structured adapter uses the same metadata-only timing contract.
        # Keeping Pi and Shepherd observable is essential: otherwise a
        # slow first response can only be guessed from terminal journal times.
        timing = ResponseTiming()
        opts = dict(opts or {})
        # _peer_request_id was the old path that persisted agent coordination as
        # source=user.  Do not honor a hidden compatibility key here: peer asks
        # are accepted only by peer_prompt_async and never enter this function.
        opts.pop("_peer_request_id", None)
        from peer_message import split_peer_message
        text, peer_head = split_peer_message(text, opts.pop("_peer_head", ""))
        # 后端重载会留下上一轮的 live checkpoint。它只临时展示、不在消息 JSONL 里；
        # 若直接起下一轮，下面首次 _checkpoint_live 会覆盖同名文件，上一轮过程永久消失。
        # 因此续轮的第一步先固化旧快照，再追加本轮用户消息。
        self._archive_live_checkpoint(sid)
        # 上一轮的通用错误已经被用户的新发送取代；认证、恢复等专用环境故障不在此清除。
        self._clear_turn_error_environment(sid)
        now = int(time.time() * 1000)
        saved = self._stash(sid, atts)
        context_files = [x for x in saved if x[0] == "context"]
        attached_context = stored_context(context_files)  # Must succeed before storing the user bubble.
        files = [x for x in saved if x[0] not in ("image", "context")]
        if files:   # 非图片的附件把路径交给模型,让它自己去读
            text = (text + "\n\n附件(已存到本机,按路径读取):\n"
                    + "\n".join("- " + x[1] for x in files)).strip()
        umid = f"msg_u_{now}"
        seq = self._seq_next(sid)   # 这一轮的序号:界面据它排，不再自己猜(见 _seq_next)
        # 截图跟这条消息一起留下。原来只存正文,刷新之后气泡里的图就没了——要等对账以引擎
        # 记录为准重建一次才回来,而对账在忙、在排队、在翻页时都不跑,中间那段时间它就是不在。
        # 存的是引用不是内容:文件在 _save_atts 里已经落盘了(a[1] 就是它的路径),
        # 把同一份内容再以 base64 抄一遍进正文,等于每次打开会话都把它重传重解析一遍。
        # 原来那条 1.5MB 的上限也随之撤掉:大图现在只是多一次按需取,不再撑大每一次正文。
        uparts = [{"id": umid + "_p0", "type": "text", "text": text}]
        if peer_head:
            uparts[0]["peer_head"] = peer_head
        for i, a in enumerate(saved):
            if a[0] == "image" and a[1]:
                uparts.append({"id": f"{umid}_i{i}", "type": "image",
                               "media_type": a[3] or "image/png",
                               "url": self.asset_url(sid, a[1])})
            elif a[0] == "context" and a[1]:
                uparts.append({"id": f"{umid}_c{i}", "type": "file",
                               "name": a[2] or "上下文.txt",
                               "media_type": a[3] or "text/plain",
                               "url": self.asset_url(sid, a[1]),
                               "context_kind": "lark_web"})
        user_msg = {"info": {"id": umid, "role": "user", "sessionID": sid,
                             "seq": seq, "source": self.SRC_USER, "at": now}, "parts": uparts}
        rid = str(getattr(getattr(self, '_delivery_local', None), 'rid', '') or '')
        delivery = getattr(self, 'delivery', None)
        if delivery and rid:
            user_msg["info"]["delivery_request_id"] = rid
        from .attachment_context import reference_catalog
        _references, reference_index = reference_catalog(self, sid, user_msg)
        if delivery and rid:
            delivery.storing(sid, rid, message_id=umid)
        self._append_msg(sid, user_msg)
        if delivery and rid:
            delivery.stored(sid, rid, message_id=umid)
        self.bus.publish({"type": "message.updated", "properties": {"info": user_msg["info"]}})
        # Remote clients need the same persisted image URLs as history reload;
        # publishing only the text leaves attachments invisible until refresh.
        for part in user_msg['parts']:
            self.bus.publish({"type": "message.part.updated", "properties": {
                "part": {**part, "messageID": umid, "sessionID": sid}}})
        self._touch(sid, title=text)
        if timing:
            timing.mark("stored_ms")

        engine_sid = self.index.get(sid, {}).get("engine_sid")
        # Recompute a compact project navigation snapshot for every engine entry
        # and resume. It belongs to application context, never the user frame.
        _g = self.grounding(sid, first_turn=True)
        from .session_naming import prepare, delivered
        name_ticket = prepare(self, sid)
        product_command = getattr(self, '_is_product_command', None)
        native_command = ((getattr(self, 'name', '') in ('claude', 'pi')
                           and text.lstrip().startswith('/'))
                          or (callable(product_command) and product_command(text)))
        task_hint = ''
        if not native_command:
            try:
                import task_ledger
                from deskcfg import DATA_HOME
                task_hint = task_ledger.consume_review_hint(
                    DATA_HOME, self.workspace, self.grounding_domain(sid) or '_desktop', sid)
            except Exception as exc:
                try:
                    self._trace(sid, {'ev': 'task.review.error', 'error': str(exc)[:160]})
                except Exception:
                    pass  # Auxiliary task diagnostics must not block message dispatch.
        if native_command:
            attached_context = ""
        opts = {**(opts or {}), "_grounding": _g.get("constant") or "",
                "_application_context": _g.get("scene") or "",
                "_task_review": task_hint,
                "_reference_context": '' if native_command else '\n\n'.join(
                    value for value in (attached_context, reference_index) if value)}
        cmd = self.build_cmd(text, engine_sid, opts)
        if timing:
            timing.mark("grounded_ms")
        # 分支:新会话的头一回合带上分支标志,让引擎从被 resume 的那条线上另开一支,
        # 原会话的记录一个字节都不动。只用一次,用掉即清。
        with self._lock:
            if self.index.get(sid, {}).pop("fork_pending", None) and self.fork_flag:
                cmd = cmd + [self.fork_flag]
                self._save_index()
        ctx = {"sid": sid, "_session_name": name_ticket, "msg_id": f"msg_a_{now}", "parts": {}, "order": [], "last_tool": None,
               "client_request_id": rid,
               "_response_timing": timing, "_user_text": uparts[0]["text"],
               "_attached_context": attached_context,
               "t0": now, "saved": saved, "seq": seq, "source": self.SRC_USER,
               "peer_request_id": "",
               # 上一个回合是谁:自续轮读的是上一轮派出去的子代理,它只能挂在那一轮下面。
               # 在这里取,不在自续轮开场时现找——那一刻用户这一轮已经排在末尾了。
               "prev": self._turn_prev.get(sid)}
        self._turn_prev[sid] = {"msg_id": ctx["msg_id"], "seq": seq}
        # 在跑的回合只存在于内存与 SSE 里,落盘要等收尾。中途刷新页面,已经流出去的那些内容
        # 界面上就没了——重新读消息读不到它。挂进 _live,读消息时补一份快照,刷新后接得上。
        self._live[sid] = ctx
        self._turn_state_publish(sid, ctx, self.TURN_RUNNING)
        self._checkpoint_live(ctx, force=True)
        # 动态读取凭证，使设置面板的修改下一回合即生效；运行时路径与会话身份仍由 Desk 托管。
        env = secrets_env(os.environ)
        # 数据根:memory / knowledge 的命令据此定位 curator 实例。不给这一条,skill 脚本只能
        # 靠启发式猜——分发态里 skill 经软链 resolve 到代码根(home),而数据根是并列的
        # workspace,猜的结果是 home/.memory(随发行版附带的空实例),search/list/graph 全查
        # 不到东西(2026-08-21 实测)。_mem_common 那侧已补上「向上找已存在实例」的兜底,
        # 但权威来源应该是这里。
        env["DESKTRON_REPO_ROOT"] = self.workspace
        # App Control CLI transport：所有引擎拿到同一套会话身份与本机 Runtime 地址。
        # 宿主令牌不进入引擎环境；模型只能认领用户已经显式交出的租约。
        control_bin = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                                   "bin", "app-control")
        control_dir = os.path.dirname(control_bin)
        if control_dir not in env.get("PATH", "").split(os.pathsep):
            env["PATH"] = control_dir + os.pathsep + env.get("PATH", "")
        # 主会话不得自己概括并写 journal。需要立即沉淀时，journal skill 只在这个会话专属
        # 路径落一个请求标记；本回合干净收尾后由隔离 recap 支线读取完整上下文并写正文。
        env.update(self._recap_request_env(sid))
        extra_env = self.spawn_env(opts or {})   # 适配器附加 env(如 claude 的思考预算)
        env.update(extra_env)
        # 这些值是 Desk 托管的调用方身份，必须最后覆盖，不能让适配器选项或用户环境替换。
        env["DESKTRON_UI_PORT"] = str(UI_PORT)
        env["DESKTRON_SESSION_ID"] = sid
        env["DESKTRON_APP_CONTROL_SESSION_CAPABILITY"] = issue_app_control_capability(sid)
        from app_control.capability import channel_address
        env["DESKTRON_APP_CONTROL_SOCKET"] = channel_address(
            os.environ.get("DESKTRON_DATA_HOME", os.path.expanduser(
                "~/.local/share/desktron/data")))
        env["DESKTRON_ENGINE"] = self.name
        env["DESKTRON_PUBLIC_ENGINE"] = self.name
        env["DESKTRON_APP_CONTROL_CLI"] = control_bin
        # 用户帧只有双向 stream-json 用得上。两条路都要它,在这里拼一份,不各写一遍。
        uframe = self.build_user_frame(text, saved) if self.use_stdin_prompt else None
        # 卡住的探测:网络断了 claude 会自己重试,期间一帧都不发(实测连不上时静默了
        # 182 秒才吐出结论),界面上是一动不动的「处理中」。这里替它报一声「没在动」。
        # 工具在跑不算卡:一条命令跑五分钟,本来就没有帧。
        def _stall_watch():
            previous = None
            while self._live.get(sid) is ctx:
                time.sleep(5)
                if self._live.get(sid) is not ctx:
                    break
                activity = self.turn_activity(sid, ctx)
                if activity != previous:
                    previous = activity
                    self.bus.publish({"type": "session.stalled", "properties": {
                        "sessionID": sid, "generation": int(ctx.get("seq") or 0),
                        "seconds": activity["seconds"] if activity["kind"] == "no_progress" else 0,
                        "activity": activity}})
        ctx["_line_at"] = time.time()
        threading.Thread(target=_stall_watch, daemon=True).start()
        ctx["text"] = text      # 不经子进程跑回合的适配器要用它
        ctx["opts"] = dict(opts or {})
        abnormal = None
        try:
            timing_mark(ctx, "prepared_ms")
            abnormal = self._turn_dispatch(ctx, cmd, env, uframe)
        except FileNotFoundError:
            self._emit_text(ctx, f"(未找到 {self.binary},请先安装并登录)")
            abnormal = f"未找到 {self.binary}"
        except Exception as e:
            if getattr(e, 'code', None) == 'prompt_not_started':
                # Shepherd proved the native request never crossed its durable
                # dispatch fence. Its facade restores the user bubble below.
                ctx['_prompt_not_started'] = str(e) or '消息未发送'
                abnormal = ctx['_prompt_not_started'][:200]
            else:
                # Adapter diagnostics belong to the structured terminal/error path.
                # Projecting them as assistant text makes infrastructure failures look
                # model-authored and duplicates the visible warning environment.
                abnormal = str(e)[:200]
        # 收尾:持久化助手消息(即使异常退出也保存已累积内容)+ 收尾事件
        delivered(self, sid, name_ticket, ctx, abnormal)
        self._finish_turn(sid, ctx, abnormal)
        self._persist_idle_after(ctx)

    def _delivery_dispatched(self, ctx, *, engine_session_id='', native_turn_id=''):
        rid = str(ctx.get('client_request_id') or '')
        delivery = getattr(self, 'delivery', None)
        if delivery and rid:
            return delivery.dispatched(ctx['sid'], rid,
                engine_session_id=str(engine_session_id or ''), native_turn_id=str(native_turn_id or ''))
        return None

    def _delivery_bind_native(self, ctx, *, engine_session_id='', native_turn_id=''):
        rid = str(ctx.get('client_request_id') or '')
        delivery = getattr(self, 'delivery', None)
        if delivery and rid:
            return delivery.bind_native(ctx['sid'], rid,
                engine_session_id=str(engine_session_id or ''), native_turn_id=str(native_turn_id or ''))
        return None

    def _turn_dispatch(self, ctx, cmd, env, uframe):
        """跑完这一回合。默认是起子进程读它的输出;不走子进程的适配器覆写这里。"""
        if getattr(self, '_shutting_down', False):
            ctx['aborted'] = 'interrupt'
            return None
        if self.persist_on() and uframe is not None:
            try:
                return self._turn_persist(ctx, cmd, env, uframe)
            except _PersistUnusable:
                # 常驻这一趟没起作用,且还没吐出任何内容:退回一回合一进程,用户不该
                # 因为一条内部优化就丢掉这次提问。
                return self._turn_oneshot(ctx, cmd, env, uframe)
        return self._turn_oneshot(ctx, cmd, env, uframe)

    def build_user_frame(self, text, saved):
        """构造双向协议的一条用户帧；默认保持 Claude stream-json 形状。

        双向只是一项传输能力，不代表各引擎共用同一份 wire schema。Pi RPC 同样经 stdin
        收 prompt，但它的图片块与 Claude 不同；让适配器只覆写这一小层，回合排队、附件落盘、
        常驻复用、看门狗与异常收尾仍走同一条通用路径。
        """
        content = [{"type": "text", "text": text}]
        for kind, _path, _name, mt, b64 in saved:
            if kind == "image" and b64:
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": mt or "image/png", "data": b64}})
        return {"type": "user", "message": {"role": "user", "content": content}}

    @staticmethod
    def _turn_has_running_tools(ctx):
        parts = (ctx or {}).get("parts") or {}
        return any((part or {}).get("type") == "tool"
                   and (((part or {}).get("state") or {}).get("status") == "running")
                   for part in parts.values())

    def _turn_watch(self, ctx, proc):
        def on_dead():
            if not ctx.get("engine_failure"):
                seconds = int(getattr(self, "TURN_TIMEOUT", 900) or 900)
                ctx["engine_failure"] = {"source": "transport", "code": "WATCHDOG_TIMEOUT",
                    "message": f"引擎 {seconds} 秒没有协议进展，Desk 已停止该回合；请求未重发"}
                ctx["engine_error"] = ctx["engine_failure"]["message"]
            proc.kill()
        return _TurnWatch(self.TURN_TIMEOUT, on_dead,
                          should_defer=lambda: self._turn_has_running_tools(ctx)).start()

    @staticmethod
    def _native_startup_warning(line):
        text = str(line or "").strip()
        return bool(re_mod.match(r"^Warning: No project session found with id .+; creating a new session", text))

    def _record_process_warning(self, ctx, line):
        if not self._native_startup_warning(line):
            return False
        ctx.setdefault("process_warnings", []).append(str(line or "").strip())
        return True

    def _process_error_tail(self, ctx, text):
        lines = []
        for line in str(text or "").splitlines():
            if not self._record_process_warning(ctx, line):
                lines.append(line)
        return "\n".join(lines)[-300:]

    def _turn_oneshot(self, ctx, cmd, env, uframe):
        """老路:一回合一个进程,跑完即退。返回异常说明,没有异常返回 None。"""
        abnormal = None
        native_done = False
        proc = self._spawn_process(ctx['sid'], cmd, cwd=self.session_cwd(ctx["sid"]), text=True, env=env,
                                stdin=subprocess.PIPE if self.use_stdin_prompt else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ctx["proc"] = proc
        self._note_proc(ctx["sid"], proc)   # 老路也记:这一回合的子代理活在这个进程里
        # stderr 得一边跑一边抽干。原来只在收尾 read 一次:管道大约 64KB,写满之后子进程就
        # 卡在写 stderr 上,不再往 stdout 写一个字,而我们正等在 stdout 上——两边互等,
        # 表现成「活干完了、进程还在、界面一直等待中」,直到回合超时才被杀掉。
        # 话多的引擎(警告、进度)在长会话里很容易写到这个量。常驻那条路已经这么做了。
        errbuf = collections.deque(maxlen=40)

        def drain_err():
            try:
                for ln in proc.stderr:
                    line = ln.rstrip("\n")
                    if not self._record_process_warning(ctx, line):
                        errbuf.append(line)
            except Exception:
                pass

        threading.Thread(target=drain_err, daemon=True).start()
        name_reader = None
        if uframe is not None:
            if (ctx.get('_session_name') or {}).get('native_title') and self.name in ('pi', 'claude'):
                from .persist import _EngineProc
                from .session_naming import sync_pipe
                name_reader = _EngineProc(proc, ())
                name_reader.pump_start()
                try:
                    sync_pipe(self, ctx, name_reader)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=5)
                    raise
            self._delivery_dispatched(ctx, engine_session_id=(self.index.get(ctx['sid']) or {}).get('engine_sid') or '')
            timing_mark(ctx, "submitted_ms")
            proc.stdin.write(json.dumps(uframe, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        killer = self._turn_watch(ctx, proc)
        try:
            lines = iter(name_reader.lines.get, None) if name_reader else proc.stdout
            for line in lines:
                ctx["_line_at"] = time.time()
                killer.beat()
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                try:
                    self.handle_event(obj, ctx)
                except Exception:
                    pass
                if ctx.pop("_close_stdin", False):
                    native_done = True
                    try:
                        proc.stdin.close()   # 回合结束信号:关 stdin 让进程退出
                    except Exception:
                        pass
            proc.wait()
            if proc.returncode not in (0, None):
                # 进程异常退出:发 session.error 让前端标注中断并保留已生成内容,不再静默空白
                err = "\n".join(errbuf)[-300:]   # 抽干线程收着的那份,不再自己 read
                abnormal = f"进程退出码 {proc.returncode}" + (f":{err}" if err else "")
                if not ctx["order"]:
                    self._emit_text(ctx, f"(引擎退出 {proc.returncode}:{err})")
            elif self.name in ("pi", "claude") and uframe is not None and not native_done and not ctx.get("aborted"):
                abnormal = "原生进程退出但未确认回合终态；请求未重发"
                ctx.setdefault("engine_failure", {"source": "transport",
                    "code": "NATIVE_TERMINAL_MISSING", "message": abnormal})
        finally:
            killer.cancel()
        return abnormal

    @staticmethod
    def _iso_ms(v):
        """记录里的 ISO 时刻转毫秒。带 Z 的是 UTC,不补时区会差一个时区。
        与引擎无关:三家的记录都用 ISO 写时刻,所以放基类。"""
        v = str(v or "")
        if not v:
            return 0
        try:
            from datetime import datetime, timezone
            return int(datetime.strptime(v[:23], "%Y-%m-%dT%H:%M:%S.%f")
                       .replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            return 0

    def _history_window(self, msgs, have, limit):
        """从整份记录里切出「界面上那一段再往前的 limit 个回合」。

        have 是界面已经有多少个回合(从末尾数)。按发言切分,不按消息——一个回合含一条发言
        与若干条助手消息,按消息数切会把回合从中间劈开。各引擎的记录读法各异,切法一致。"""
        msgs = msgs or []
        starts = [i for i, m in enumerate(msgs) if self._turn_start(m)]
        if len(starts) <= have:
            return []
        end = starts[len(starts) - have] if have else len(msgs)
        lo = max(0, len(starts) - have - limit)
        return msgs[starts[lo]:end]

    def _import_poorer(self, sid, msgs):
        """这份重建结果是不是比现有存储更穷。更穷就别覆盖。

        搬迁是整份覆盖,以引擎记录为准。可回合失败时记录里根本没有这一轮——比如 codex 报
        「thread already has an active writer」,那一轮压根没进 rollout——而我们这侧已经把
        用户那句话落了盘。照覆盖不误,用户刚打的字就没了(2026-08-10 实见)。
        判据取「人说过几句话」:助手内容可能因为引擎侧压缩而合法变少,人说过的话不会。
        """
        try:
            cur = self._filter_recalled_turns(sid, self.messages(sid))
        except Exception:
            return False
        msgs = self._filter_recalled_turns(sid, msgs or [])
        have = sum(1 for m in cur if self._turn_start(m))
        got = sum(1 for m in (msgs or []) if self._turn_start(m))
        return bool(have) and got < have

    def _filter_recalled_turns(self, sid, msgs):
        """从引擎历史投影中摘掉 Esc 撤回的完整回合。

        cut 只需命中回合开头的用户消息；其后的助手内容即使晚于 Esc 才落进原生记录，
        也随这一轮一起丢掉，直到下一条真实用户消息开始。
        """
        cuts = list((self.index.get(sid) or {}).get("recall_cuts") or [])
        if not cuts:
            return msgs
        out, dropping = [], False
        for message in msgs or []:
            if self._turn_start(message):
                at = int((message.get("info") or {}).get("at") or 0)
                dropping = bool(at and any(int(a) <= at <= int(b) for a, b in cuts))
            if not dropping:
                out.append(message)
        return out

    def _reinstate_local_only(self, sid, msgs):
        """把引擎未落盘的异常或中断回复按时间位补回重建结果。"""
        try:
            old = self.messages(sid)
        except Exception:
            return msgs
        keep = [m for m in old if ((m.get("info") or {}).get("local_only")
                                   and (m.get("info") or {}).get("at"))]
        if not keep:
            return msgs
        out = list(msgs)
        for m in keep:
            info = m.get("info") or {}
            at = info.get("at") or 0
            t0 = info.get("turn_t0") or 0
            if t0 and any((x.get("info") or {}).get("role") == "assistant"
                          and t0 <= ((x.get("info") or {}).get("at") or 0) <= at for x in out):
                continue
            pos = next((i for i, x in enumerate(out)
                        if ((x.get("info") or {}).get("at") or 0) > at), len(out))
            out.insert(pos, m)
        return out

    @staticmethod
    def _turn_start(m):
        """这条消息是不是一个回合的开头。压缩行、目标行与自续轮那条分割线在契约里也是
        role=user——它们是「会话在此处换了状态」的标记,不是谁的发言。按发言切分的地方
        (翻页、取最近 N 回合、回插自动压缩、猜标题)都要认这个判据,少认一处就整体错位。

        判据与引擎无关,放在基类:另两个引擎的搬迁与翻页同样按发言切分。"""
        i = m.get("info") or {}
        return (i.get("role") == "user" and not i.get("compact")
                and not i.get("goal") and not i.get("aside"))

    # ---- 回合的顺序与来路 ----
    # 一个回合有两种来路:用户发的消息(走 HTTP 进 _run),以及子代理或后台任务交回引发的
    # 自续轮(走注入)。两条路在后端本来就是分开的,顺序也由后端决定,所以序号与来路在这里
    # 产生、随事件下发。界面此前只能靠「末尾那一回合像不像空的」去猜自续轮该挂在谁下面,
    # 用户排队的消息与子代理交回撞在同一刻时就猜错——那条「处理后台任务的返回」画在了
    # 用户自己的提问下面(2026-08-16 实见)。

    SRC_USER = "user"          # 这一轮由用户发起
    SRC_PEER = "peer"          # 跨会话 ask 的临时支线；不得进入普通消息历史
    SRC_ASIDE = "task-notification"   # 这一轮由子代理(或后台任务)交回引发

    def _aside_drop_hint(self, ctx):
        """回合收尾时把还挂着的预告丢掉。

        预告是「下一条通知属于哪个子代理」的一次性凭据。回合都结束了还没被消费,说明引擎
        这次把通知并进了本轮,留着只会错套下一轮(见 handle_event 里那条判据)。

        2026-08-20 这个方法从 _aside_bail 缩成现在这样:旧机制把自续轮的内容收在影子 ctx 里,
        收场信号不来界面就一直停在「处理中」,所以那时必须在这里补发一条收场。分段追认之后
        内容一直在本回合的 parts 与 live 快照里,不追认只是少一条线,不会丢东西——影子 ctx
        连同 _aside_new / _aside_close / _aside_note / _aside_revoke 一并撤了。"""
        ctx.pop("aside_hint_at", None)

    def _seq_next(self, sid):
        """这一段会话的下一个回合序号。单调递增,且不与重启前发出去的序号相撞。

        序号只用于「界面上这条内容归哪一轮」的对齐,所以要跨进程唯一:纯计数器在后端
        重启后从头开始,新回合的 1 号会认到记录里旧回合的 1 号上。取毫秒时刻作下界,
        既单调又不重号;同一毫秒内连着开两轮时退回「上一个 + 1」。"""
        with self._lock:
            n = max(int(self._turn_seq.get(sid) or 0) + 1, int(time.time() * 1000))
            self._turn_seq[sid] = n
            return n

    @staticmethod
    def _turn_mark(info, ctx):
        """把这一轮的序号与来路盖在一条消息(或事件)的 info 上。两者缺一不可:
        序号定先后,来路定它该挂进哪个容器。"""
        for k in ("seq", "source", "host"):
            if ctx.get(k):
                info[k] = ctx[k]
        return info

    def _turn_engine(self, sid, ctx):
        """这一回合由哪个引擎生成。普通会话就是适配器自己;Shepherd 按本回合的 Producer 覆写。"""
        return self.name

    def _finish_turn(self, sid, ctx, abnormal):
        """回合收尾:落盘助手消息(即使异常退出也保存已累积内容)+ 收尾事件。

        常驻之后这一步不再由「进程退出」触发,而是由 result 帧划出的回合边界触发。"""
        timing_mark(ctx, "engine_done_ms")
        from .lifecycle import commit_request_failure, compact_update
        commit_request_failure(ctx)  # Native settled, or the dispatch loop has exited.
        if ctx.get("compact_t0"):
            compact_update(self, ctx, {"running": False, "result": "unknown",
                "error": "回合结束前未确认压缩结果"})
        # Preserve last retry as inactive failure evidence, not a live wait.
        if isinstance(ctx.get("retry"), dict):
            ctx["retry"]["active"] = False
        for key in ("_maintenance_retry", "_provider_wait", "_provider_queue", "native_queue"):
            ctx.pop(key, None)
        ctx["_lifecycle_closed"] = True  # Late retry callbacks cannot recreate live state.
        self._aside_drop_hint(ctx)   # 没被消费的预告不跨回合,见下
        # Keep the control state running until the durable outcome is written.
        # A concurrent GET must not advertise ready during final persistence.
        # 引擎自己报的失败也算异常收尾。codex 家族把它记在 ctx["engine_error"](turn/failed、
        # error、turn/completed 带 status=failed 三条都写这里),而此前全仓六处赋值、零处读取:
        # RPC 本身没出错,_turn_dispatch 就返回 None,于是回合照常走到 session.idle,界面上是
        # 「已完成」——用户看到的是「会话无缘无故结束、一个字都没有」(2026-08-18 实见;
        # 存储里那几轮只有用户消息,连助手记录都没落下)。
        # 判据放在这条唯一的收尾路径上:除了报错本身,还顺带把日志与 local_only 标记补齐。
        abnormal = abnormal or ctx.get("engine_error")
        from error_notice import failure, safe_text
        # An expected shutdown is not a provider failure. A native failure
        # already observed before shutdown must retain its actual outcome.
        native = ctx.get('native_terminal') or {}
        if ctx.get('_shutdown_reason') and not ctx.get('engine_failure') and native.get('status') != 'failed':
            ctx['aborted'] = 'interrupt'
        if abnormal and not ctx.get('engine_failure'):
            ctx['engine_failure'] = failure(abnormal, source='transport')
        if abnormal:
            abnormal = safe_text(abnormal)
        with self._lock:
            alive = sid in self.index
        parts = [ctx["parts"][k] for k in ctx["order"]]
        # 这一轮是不是正常收尾。不正常 = 引擎没写完那条 assistant 记录,产出只存在于我们这边
        # (见 info["local_only"])。打断、看门狗超时、常驻进程中途崩,都算;撤回不算,那是
        # 有意不留痕。判据放在这里而不是各分支里:收尾路径只有这一条,新增的失败方式自动覆盖。
        native_interrupted = (ctx.get("native_terminal") or {}).get("status") == "interrupted"
        ended_clean = not ctx.get("aborted") and not abnormal and not native_interrupted
        if ended_clean and native.get('status') == 'completed':
            try:
                self.resolve_auth(self._notice_source(sid, ctx), int(ctx.get('t0') or 0))
            except Exception:
                # Notice cleanup must never turn a successful native turn into failure.
                import logging
                logging.getLogger(__name__).warning('Authentication notice cleanup failed', exc_info=True)
        done_at = int(time.time() * 1000)
        if ctx.get("aborted"):
            abnormal = None              # 用户主动停的,不是故障
            if ctx["aborted"] == "recall":
                parts = []               # 撤回的回合不留痕;打断的保留已经跑出来的内容
            elif ctx.get("interrupt_snapshot"):
                snap = ctx.get("interrupt_snapshot") or {}
                sparts = snap.get("parts") or {}
                parts = [sparts[k] for k in (snap.get("order") or []) if k in sparts]
        interrupted = ctx.get("aborted") == "interrupt" or (native_interrupted and not abnormal)
        if (parts or interrupted or abnormal) and alive:
            self._drop_since(sid, ctx.get("t0") or 0)
            info = self._turn_mark({"id": ctx["msg_id"], "role": "assistant", "sessionID": sid}, ctx)
            # 成文时刻取收尾这一刻。消息 id 里的毫秒是回合起点(= 用户发出那一刻),界面原先
            # 拿不到 at 就退回 id 里的时间,于是回复底下显示的是提问时间。
            # 单独落 done_at,不写 at:at 是「按时刻切」的判据(_drop_since、local_only 补回、
            # 界面对账都按它),排队中的用户回合起点可能早于上一条自续轮收尾,写进 at 会被误删。
            info["done_at"] = done_at
            info["terminal"] = {
                "status": "interrupted" if interrupted else "failed" if abnormal else "completed",
                "source": (native.get('source') or ('desk_shutdown' if ctx.get('_shutdown_reason')
                           else 'transport' if abnormal else 'dispatch_return'))}
            if ctx.get("native_terminal"):
                info["terminal"]["native"] = copy.deepcopy(ctx["native_terminal"])
            engine = self._turn_engine(sid, ctx)
            if engine:
                info["engine"] = engine   # 由谁生成;Shepherd 会话里各回合可能不同
            actual_model = str(ctx.get("actual_model") or "").strip()
            if actual_model:
                # 模型署名属于这一回合，只认原生事件在本轮明确自报的值；不拿会话
                # 最近模型或配置值反推，避免切模型后把旧回复改署成新模型。
                info["model"] = actual_model
            if ctx.get("esid"):
                info["engine_sid"] = ctx["esid"]   # 分支锚点:从这条消息分叉 = resume 这个引擎会话
            if ctx.get("ms"):
                info["ms"] = ctx["ms"]   # 本回合耗时随消息落盘,刷新后历史回合仍能显示
            usage = self._turn_usage(ctx)
            if usage:
                info["usage"] = usage     # 仅数值统计:供底部展示,不含正文、prompt 或工具参数
            if interrupted:
                info["interrupted"] = True
            elif abnormal:
                # Keep failure as a turn outcome, not assistant prose. Structured adapters also
                # persist the native source/code beside the boolean, so refreshed history and
                # diagnostics do not have to infer AUTH/TIMEOUT/approval semantics from wording.
                info["failed"] = True
                failure = ctx.get("engine_failure")
                if isinstance(failure, dict):
                    info["error"] = copy.deepcopy(failure)
                if isinstance(ctx.get("retry"), dict):
                    info["retry"] = copy.deepcopy(ctx["retry"])
            if ctx.get("aside_row"):
                # 间隙里读到的自续轮:与记录投影里那条(见 claude_transcript 的 flush_aside)
                # 同构,重建与实时两条路在界面上走同一段 hsAsideRow。
                info["aside"] = ctx["aside_row"]
            if not ended_clean:
                # 引擎只在一条 assistant 消息完整收尾时才落盘,流式吐到一半被打断的那些字
                # 它不写(2026-08-15 在 436ddefc 的记录里实证:用户发话 2 秒后就是中断标记,
                # 中间没有任何 assistant 条目)。而 import_session 按 transcript 整份覆盖投影,
                # 于是这段内容当时看得见、下次以 HTML 视图打开就被抹掉。标记在这里,
                # 覆盖之后由 _reinstate_local_only 按时间位补回去。
                info["local_only"] = True
                info["at"] = done_at
                info["turn_t0"] = int(ctx.get("t0") or 0)   # 这一轮的起点,补回时据它判有没有被记录覆盖
            self._append_msg(sid, {"info": info, "parts": parts})
            self._trim_store(sid)   # 回合收尾时截断:增长的唯一入口就在这里
        rid = str(ctx.get('client_request_id') or '')
        delivery = getattr(self, 'delivery', None)
        if delivery and rid:
            receipt = delivery.get(sid, rid)
            if receipt.get('status') == 'stored':
                # This process observed the pre-dispatch failure and can persist a
                # coherent user+failed-assistant turn. Restart cleanup is different:
                # there is no assistant outcome, so it removes the projected user.
                delivery.finish(sid, rid, reason='failed_before_dispatch', outcome='failed')
            elif receipt.get('status') == 'cleanup_required':
                # Cancellation/restart cleanup owns this fence. A native/assistant
                # return cannot release it while the correlated user projection remains.
                ctx['delivery_blocked'] = True
            else:
                failure = ctx.get('engine_failure') or {}
                # Once native dispatch may have happened, only an explicit native
                # terminal can release the next turn. Transport errors, EOF and
                # process exit are diagnostics, never proof that tools did not run.
                unknown = receipt.get('status') == 'dispatched' and not native.get('status')
                if unknown:
                    delivery.uncertain(sid, rid, reason=failure.get('code'),
                        engine_session_id=ctx.get('esid') or '',
                        native_turn_id=ctx.get('engine_turn_id') or '')
                    ctx['delivery_blocked'] = True
                else:
                    outcome = 'interrupted' if interrupted else 'failed' if abnormal else 'completed'
                    delivery.finish(sid, rid, reason=(native.get('source') or
                        ('desk_shutdown' if ctx.get('_shutdown_reason') else
                         'transport' if abnormal else 'dispatch_return')), outcome=outcome)
        self._clear_live_checkpoint(sid)
        if alive:
            self._touch(sid)
        if abnormal:
            # 也记一条日志。此前这个说明只经 SSE 发一次:页面没开、或者那一帧没被渲染,
            # 它就彻底没了,事后只能从「助手消息以工具块收尾、ms 为空」反推回合是怎么断的。
            # 结构化适配器再把原生 source/code 带上；排障不反向解析展示文案。
            failure = ctx.get("engine_failure")
            error = dict(failure) if isinstance(failure, dict) else {"message": abnormal}
            error.setdefault("message", abnormal)
            category = (f" [{error.get('source')}/{error.get('code')}]"
                        if error.get("source") and error.get("code") else "")
            try:
                import deskcfg
                deskcfg.log(self.name, f"回合异常收尾 {sid}{category}:{abnormal}")
            except Exception:
                pass
            self._turn_error_environment(sid, error)
            if not ctx.get("_engine_error_published"):
                self._publish_turn_error(sid, ctx, error)
                ctx["_engine_error_published"] = True
        # 预告不跨回合。它是「下一条 init 属于某个子代理」的一次性凭据,而自续轮的 init
        # 与通知帧同在一个读循环里——回合都收尾了还没被消费,说明引擎这次把通知并进了本轮
        # (实测常态如此),那它对下一轮就只是个会误伤的陈迹:下一轮的第一条 init 是用户的。
        # 判据那侧已经改成「第二条 init 才可能是自续轮」,这里再断一道,两处都不依赖 TTL。
        self._aside_hint.pop(sid, None)
        # 台账在回合边界补发一次。界面原来只在建视图时从 /config 取一次,之后全靠增量——
        # 销号那一帧没来(命令跑完之后没人再跟这段会话说话,通知就一直不送达),数字就挂着不动。
        # 这一发让它每回合对一次账:后端是几就是几。
        self._bg_publish(sid)
        # idle 留给旧页面和既有消费者；ready 才是新控制契约里的「下一轮可安全开始」。
        # 两帧有意按这个顺序发：旧页面照常在 idle 收尾，新页面等 ready 再释放队列。
        boundary = {"sessionID": sid, "generation": int(ctx.get("seq") or 0), "contract": 1,
                    "at": done_at, **({"failed": True} if abnormal else {})}
        if interrupted:
            boundary["interrupted"] = True
        if abnormal:
            failure = ctx.get("engine_failure")
            boundary["error"] = (copy.deepcopy(failure) if isinstance(failure, dict)
                                 else {"message": str(abnormal)[:300]})
            if isinstance(ctx.get("retry"), dict):
                boundary["retry"] = copy.deepcopy(ctx["retry"])
        if self._turn_engine(sid, ctx):
            boundary["engine"] = self._turn_engine(sid, ctx)
        actual_model = str(ctx.get("actual_model") or "").strip()
        if actual_model:
            boundary["model"] = actual_model
        usage = self._turn_usage(ctx)
        if usage:
            boundary["usage"] = usage
        if usage and alive:
            try:
                from usage_stats import record_turn
                record_turn(os.path.dirname(os.path.dirname(self.dir)), sid,
                    at=done_at, seq=int(ctx.get("seq") or 0),
                    engine=self._turn_engine(sid, ctx) or self.name,
                    model=str(ctx.get("actual_model") or ""),
                    domain=self.grounding_domain(sid) or "_desktop", usage=usage)
            except Exception:
                pass
        # Review while this turn still owns the session; a new turn cannot add
        # evidence between the completion boundary and the task snapshot.
        if ended_clean and alive and not ctx.get('delivery_blocked'):
            try:
                import task_ledger
                from deskcfg import DATA_HOME
                reviewed = task_ledger.review_turn(DATA_HOME, self.workspace,
                    self.grounding_domain(sid) or '_desktop', sid,
                    int(ctx.get('t0') or 0), int(ctx.get('seq') or 0), done_at)
                for result in reviewed:
                    self._trace(sid, {'ev': 'task.review', **result})
            except Exception as exc:
                try:
                    self._trace(sid, {'ev': 'task.review.error', 'error': str(exc)[:160]})
                except Exception:
                    pass
        # Facades own separate durable receipts. Keep GET /state running until
        # their admission fence has settled, before publishing idle/ready.
        before_ready = getattr(self, '_before_turn_ready', None)
        ready_block = before_ready(sid, ctx) if before_ready is not None else None
        if self._live.get(sid) is ctx:
            self._live.pop(sid, None)
        self.bus.publish({"type": "session.idle", "properties": boundary})
        if abnormal:
            ctx["turn_failed"] = True
        if ready_block:
            self.bus.publish({'type':'session.state', 'properties':{
                'sessionID':sid, 'state':'blocked', 'generation':int(ctx.get('seq') or 0),
                'contract':1, **ready_block}})
        elif ctx.get('delivery_blocked'):
            props = {"sessionID":sid, "state":"blocked", "generation":int(ctx.get('seq') or 0),
                     "contract":1, "delivery":delivery.get(sid, rid)}
            self.bus.publish({"type":"session.state", "properties":props})
        else:
            self._turn_state_publish(sid, ctx, self.TURN_READY)
        ctx.pop("turn_failed", None)
        timing_mark(ctx, "ready_ms")
        timing = ctx.get("_response_timing")
        if timing is not None:
            outcome = ("cancelled" if ctx.get("aborted") else
                       "error" if abnormal else "completed")
            self._trace(sid, {"ev": "response.timing", "seq": ctx.get("seq", 0),
                              "ms": dict(timing.marks), "outcome": outcome})
            try:
                self.performance.record_timing(
                    sid, int(ctx.get("seq") or 0), dict(timing.marks), outcome)
            except (OSError, ValueError):
                pass
        # recap 布防:收尾后 debounce 一段再对本会话吐一条 journal(见 recap.py)。
        # 放在 READY 之后——它是旁路,不参与回合契约,失败也不该影响任何状态帧。
        if ended_clean and not ready_block:
            try:
                self._recap_arm(sid, ctx)
            except Exception:
                pass

    # ---- 常驻引擎进程 ----
    def persist_on(self):
        """常驻是否可用。留一个环境变量的关断口:真出问题时不必改代码、不必等发版,
        置 0 重启就回到一回合一进程那条老路。"""
        return self.persist_supported and os.environ.get("DESKTRON_PERSIST", "1") != "0"

    @staticmethod
    def _persist_key(cmd):
        """选项指纹:同一条会话里两回合的选项一致,才能复用同一个进程。

        `--resume <id>` 那一对要摘掉。它每回合都在变(引擎每回合返回新的 session_id),
        而活着的进程本来就在这条线上续着,不需要也不能再 resume 一次——留着它比指纹永远
        不命中、每回合都重开还糟:那等于常驻完全没生效。

        系统正文不能热改。保留其指纹，规则或项目导航变化时在下一回合换代；
        不允许旧常驻进程悄悄忽略最新约束。只删除 resume，不删除系统正文。
        正文用摘要进入身份键，不在调试输出中暴露材料。"""
        drop_pairs = ("--resume",)
        out, skip = [], False
        for a in cmd:
            if skip:
                skip = False
                continue
            if a in drop_pairs:
                skip = True
                continue
            if out and out[-1] == '--append-system-prompt':
                import hashlib
                out.append('sha256:' + hashlib.sha256(str(a).encode()).hexdigest())
            else:
                out.append(a)
        return tuple(out)

    def _persist_get(self, sid, cmd, env):
        """取一条可用的常驻进程:没有、已经死了、或者选项变了,都重开一个。"""
        if getattr(self, '_shutting_down', False):
            raise RuntimeError('Desk 正在退出，不能接入常驻进程')
        key = (self._persist_key(cmd), self.persist_env_key(env))
        with _PERSIST_LOCK:
            ep = _PERSIST.get(sid)
            if ep is not None and (ep.proc.poll() is not None or ep.key != key):
                ep = None
        if ep is None:
            _persist_drop(sid)
            _persist_trim(sid)
            proc = self._spawn_process(sid, cmd, cwd=self.session_cwd(sid), text=True, env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, bufsize=1)
            ep = _EngineProc(proc, key, owner=self)
            self._note_proc(sid, proc)   # 换了实例:此前派出的子代理随旧实例一起没了
            self._aside_hint.pop(sid, None)   # 预告随旧进程的管道一起失效,留着会错套下一条 init
            threading.Thread(target=ep.drain_err, daemon=True).start()
            ep.pump_start()
            with _PERSIST_LOCK:
                _PERSIST[sid] = ep
        return ep

    def _spawn_process(self, sid, cmd, **kwargs):
        from owned_process import command, protect, writer_lock_path
        kwargs['start_new_session'] = True
        lock = writer_lock_path(self.dir, sid)
        return protect(subprocess.Popen(command(cmd, lock_path=lock), **kwargs))

    def persist_env_key(self, env):
        """会改变引擎行为、因而要求常驻进程换代的环境变量指纹。"""
        return ()

    # ---- 回合之间的管道 ----
    # 常驻进程在两个回合之间并不安静:子代理或后台命令交回时,harness 往上下文里注入一条
    # 通知,引擎就着它自己续一轮(读报告、判断下一步,有时接着调工具)。那一轮的帧写在间隙里。
    # 此前间隙里没人读 stdout,它们一直躺到用户下一次发话,才在那一轮的读循环开头被消费:
    # 界面上是「子代理返回了,要刷新才看得到」;更糟的是这些帧与用户那一轮连成一串,
    # 归属靠 result 的 origin 回溯追认,首段又不能追认(里面可能有用户自己的话),于是
    # 自续轮的正文重复落进用户那一轮,前端再按执行返回的路由把用户的回答也带进返回卡里
    # (2026-09-08 用户实见:正文重复、回复跑到自己的提问上面)。
    #
    # 现在间隙里也读:回合收尾后起一条空闲读,见到通知或 init 就开一个自己的 ctx,把这一轮
    # 当作独立的一轮发布、落盘(source 是 task-notification,消息 id 用 msg_x_ 前缀,与记录
    # 投影那条同构)。用户发话时回合读先接管管道(见 _persist_take):空闲读正收着一轮就等它
    # 收完再让路,管道里于是不再有残帧。

    ASIDE_OPEN_GRACE = 20     # 通知之后等 init 多久。引擎不一定续轮(实见两例),不能一直挂着

    def aside_frame_opens(self, obj):
        """这一帧算不算一轮的开场。默认不认:不走 stream-json 的适配器没有这种间隙轮。"""
        return False

    def _aside_turn_row(self, ctx, obj):
        """间隙里那一轮落盘时挂在 info.aside 上的出处。适配器按自己的帧形状覆写(claude 从
        预告帧与 result 取名字、时长);这里是没等到收尾帧时的兜底,至少让重放认得出它是哪张
        返回卡下面的正文。"""
        returned = next((p for p in (ctx.get("parts") or {}).values()
                         if p.get("type") == "execution-result"), {})
        return {"origin": self.SRC_ASIDE, "presentation": "continuation",
                "return_id": returned.get("id") or "", "started": int(ctx.get("t0") or 0),
                "ms": int(ctx.get("ms") or 0), "task_id": str(returned.get("task_id") or ""),
                "agent": None}

    def _persist_take(self, sid, ep, ctx):
        """回合读接管管道。空闲读正收着一轮时等它收完;等待期间不算卡住。
        中止了返回 False,拿到锁返回 True(由调用方负责释放)。"""
        ep.wanted = True
        # 唤醒正在空队列上等待的空闲读，不必等它的 500ms 轮询到点。
        # 空行不是协议帧：若锁已交接，回合读也会自然跳过；不打断已开场的自续轮。
        ep.lines.put("")
        waited = False
        while not ep.reader_lock.acquire(timeout=0.5):
            waited = True
            ctx["_line_at"] = time.time()
            if ctx.get("aborted"):
                return False
        if waited:
            self._trace(sid, {"ev": "turn.took_over"})
        return True

    def _persist_idle_after(self, ctx):
        ep = ctx.pop("_idle_after", None)
        if ep is not None:
            self._persist_idle_start(ctx["sid"], ep)

    def _persist_idle_start(self, sid, ep):
        if ep.idle_thread is not None and ep.idle_thread.is_alive():
            return
        if ep.proc.poll() is not None:
            return
        ep.idle_thread = threading.Thread(target=self._persist_idle, args=(sid, ep), daemon=True)
        ep.idle_thread.start()

    def _aside_ctx(self, sid, proc):
        """间隙里那一轮的 ctx。形状同 _run 里用户那一轮的,差别只在来路与消息 id:
        没有用户消息,序号照样取(界面按它对齐先后),msg_x_ 前缀让前端与记录投影那条认同一类。"""
        now = int(time.time() * 1000)
        ctx = {"sid": sid, "msg_id": f"msg_x_{now}", "parts": {}, "order": [], "last_tool": None,
               "t0": now, "saved": [], "seq": self._seq_next(sid), "source": self.SRC_ASIDE,
               "peer_request_id": "", "prev": self._turn_prev.get(sid),
               "proc": proc, "persist": True, "aside_turn": True, "text": "", "opts": {},
               "_line_at": time.time()}
        return ctx

    def _persist_idle(self, sid, ep):
        """间隙里的读循环。一次只收一轮;用户回合要接管时,没开场的当场让路,开了场的收完再让。"""
        while not ep.reader_lock.acquire(timeout=0.5):
            if ep.wanted:
                return
        ctx, killer, abnormal = None, None, None
        try:
            while True:
                if ep.wanted and (ctx is None or not ctx.get("init_seen")):
                    break                       # 用户在等;手上这轮还没开场(顶多一张返回卡)
                with _PERSIST_LOCK:
                    current = _PERSIST.get(sid)
                if current is not ep:
                    break                       # 进程已被摘掉(回收、切换、中止),不再是池里那个
                try:
                    line = ep.lines.get(timeout=0.5)
                except queue.Empty:
                    if (ctx is not None and not ctx.get("init_seen")
                            and time.time() - ctx["t0"] / 1000 > self.ASIDE_OPEN_GRACE):
                        break                   # 通知之后引擎没续轮:只留那张返回卡,收掉
                    continue
                if line is None:
                    if ctx is not None and ctx.get("init_seen"):
                        abnormal = f"常驻进程中断(退出码 {ep.proc.poll()})"
                    break                       # 进程没了
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if ctx is None:
                    if not self.aside_frame_opens(obj):
                        continue                # 间隙里的杂帧(上一轮的尾巴之类),不开轮
                    ctx = self._aside_ctx(sid, ep.proc)
                    ep.busy = True              # 这段时间别把进程回收掉
                    self._live[sid] = ctx
                    self._trace(sid, {"ev": "aside.begin", "msg_id": ctx["msg_id"]})
                    killer = self._turn_watch(ctx, ep.proc)
                ctx["_line_at"] = time.time()
                killer.beat()
                self._trace_frame(ctx, obj)
                try:
                    self.handle_event(obj, ctx)
                except Exception:
                    pass
                if not ctx.get("_state_told"):
                    # 第一帧处理完才报「在跑」:通知帧会先建出返回卡那一轮,状态灯挂在它上面
                    # 才是对的;报早了灯先落到上一回合上,再搬一次。
                    ctx["_state_told"] = True
                    self._turn_state_publish(sid, ctx, self.TURN_RUNNING)
                    self._checkpoint_live(ctx, force=True)
                if ctx.pop("_close_stdin", False):
                    self._aside_finish(sid, ctx, None, killer)
                    ctx, killer = None, None
                    ep.at = time.time()
        finally:
            if ctx is not None:
                self._aside_finish(sid, ctx, abnormal, killer)
            ep.busy = False
            ep.reader_lock.release()

    def _aside_finish(self, sid, ctx, abnormal, killer):
        if killer is not None:
            killer.cancel()
        self._trace(sid, {"ev": "aside.end", "abnormal": abnormal or "",
                          "parts": len(ctx.get("order") or [])})
        if not ctx.get("_state_told"):
            self._turn_state_publish(sid, ctx, self.TURN_RUNNING)
        if not ctx.get("aside_row") and ctx.get("init_seen"):
            # 没等到收尾帧(进程中途没了):出处照样要有,否则这条消息重放时会被当成普通回合
            ctx["aside_row"] = self._aside_turn_row(ctx, {})
        # 返回卡单独落成一条 role=tool 的消息,排在这一轮的正文之前——记录投影就是这个形状
        # (见 claude_transcript 里 from_notification 那一支),重放时前端先由它建出返回卡,
        # 再按 info.aside.return_id 把正文接在卡下面。留在同一条消息里,重放会把卡和正文
        # 一起并进上一个回合。
        cards = [k for k in ctx["order"] if (ctx["parts"].get(k) or {}).get("type") == "execution-result"]
        if cards and sid in self.index:
            for k in cards:
                card = dict(ctx["parts"][k], messageID="msg_" + k)
                self._append_msg(sid, {"info": {"id": "msg_" + k, "role": "tool", "sessionID": sid,
                                                "at": int(time.time() * 1000)}, "parts": [card]})
            ctx["order"] = [k for k in ctx["order"] if k not in cards]
        self._finish_turn(sid, ctx, abnormal)

    def _trace(self, sid, rec):
        """常驻回合的逐帧轨迹,按会话落 trace/<sid>.jsonl。

        归属判断(自续轮还是用户轮)的现场只存在于帧序里,帧读过即丢,判错之后无从复盘
        ——此前每次卡死都只能靠猜次序。这里只留判断用的摘要字段;单文件过 2MB 截半。"""
        try:
            d = os.path.join(self.dir, "trace")
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, f"{sid}.jsonl")
            try:
                if os.path.getsize(p) > 2_000_000:
                    with open(p, encoding="utf-8") as f:
                        keep = f.readlines()[-4000:]
                    with open(p, "w", encoding="utf-8") as f:
                        f.writelines(keep)
            except OSError:
                pass
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(dict(rec, t=int(time.time() * 1000)),
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _trace_frame(self, ctx, obj):
        """一帧一行:帧的身份字段 + 闸在这一帧到达时的状态(处理前拍下,便于回放判断路径)。"""
        rec = {"ev": "frame", "type": obj.get("type"), "sub": obj.get("subtype"),
               "origin": (obj.get("origin") or {}).get("kind"),
               "task": obj.get("task_id"), "uuid": (obj.get("uuid") or "")[:8],
               "gate": {"init": bool(ctx.get("init_seen")),
                        "aside": ctx.get("aside") is not None,
                        "hint": ctx["sid"] in self._aside_hint}}
        if obj.get("parent_tool_use_id"):
            rec["sub_agent"] = True
        self._trace(ctx["sid"], rec)

    def _turn_persist(self, ctx, cmd, env, uframe):
        """常驻路:一条会话一个进程,回合之间不退。返回异常说明,没有异常返回 None。

        仅在用户帧尚未尝试写入时允许退回老路。写入/flush 失败也可能已送达，不能重发。"""
        sid = ctx["sid"]
        ep = self._persist_get(sid, cmd, env)
        timing_mark(ctx, "resident_ms")
        proc = ep.proc
        ctx["proc"] = proc
        # 这一回合跑在跨回合复用的进程上:管道里可能还留着上一轮的帧。适配器据此判断
        # 哪些帧属于本回合(见 ClaudeAdapter.handle_event 的 init 闸)。
        ctx["persist"] = True
        ep.pump_start()
        # 先把管道接过来。间隙里空闲读可能正收着一条自续轮(引擎在读子代理的报告),
        # 那一轮收完它才让路——引擎本来也是一轮接一轮跑的,这里等的就是它真实的空档。
        # 用户帧等接管之后再写:写早了,引擎会把它排在自续轮后面,而那一轮的帧就与本轮
        # 连成一串,归属又要靠猜。
        if not self._persist_take(sid, ep, ctx):
            ep.wanted = False
            return None            # 等的时候被中止了:一个字没发出去,按中断收尾即可
        ep.busy = True
        ep.at = time.time()
        submission_attempted = False
        try:
            from .session_naming import sync_pipe
            sync_pipe(self, ctx, ep)
            self._delivery_dispatched(ctx, engine_session_id=(self.index.get(sid) or {}).get('engine_sid') or '')
            timing_mark(ctx, "submitted_ms")
            submission_attempted = True
            proc.stdin.write(json.dumps(uframe, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except Exception as e:
            ep.busy = False
            ep.wanted = False
            ep.reader_lock.release()
            _persist_drop(sid)
            if submission_attempted:
                ctx["engine_failure"] = {"source": "transport", "code": "DELIVERY_UNKNOWN",
                    "message": "原生请求写入中断，送达状态未确认；未自动重发"}
                return ctx["engine_failure"]["message"]
            raise _PersistUnusable(f"写入失败:{e}")
        try:
            message = uframe.get("message")
            if isinstance(message, str):
                _prev = message[:80]
            else:
                _prev = "".join(b.get("text", "") for b in ((message or {}).get("content") or [])
                                if isinstance(b, dict) and b.get("type") == "text")[:80]
        except Exception:
            _prev = ""
        self._trace(sid, {"ev": "turn.begin", "sid": sid,
                          "seq": int(ctx.get("seq") or 0), "text": _prev})
        abnormal, got, done = None, False, False
        killer = self._turn_watch(ctx, proc)
        try:
            # 帧来自泵线程灌的队列(见 _EngineProc.lines),不直接碰 stdout:回合之间也有人读,
            # 两个读者一条管道是不行的。
            while True:
                line = ep.lines.get()
                if line is None:
                    break                       # 进程没了(被中止、被超时杀掉、或自己退了)
                ctx["_line_at"] = time.time()
                killer.beat()
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                got = True
                self._trace_frame(ctx, obj)
                try:
                    self.handle_event(obj, ctx)
                except Exception:
                    pass
                # 老路把这个标志当「关 stdin 让进程退出」用;常驻下它就是回合边界本身,
                # stdin 要留着给下一回合。
                if ctx.pop("_close_stdin", False):
                    done = True
                    break
        finally:
            killer.cancel()
            ep.busy = False
            ep.wanted = False
            ep.reader_lock.release()
        self._trace(sid, {"ev": "turn.end", "sid": sid,
                          "seq": int(ctx.get("seq") or 0), "done": done, "got": got,
                          "aborted": bool(ctx.get("aborted"))})
        if done:
            ep.at = time.time()
            # 间隙里继续读。不在这里起线程:本轮的 _finish_turn 还没跑,它会清掉预告
            # (_aside_hint),而紧跟在 result 后面的通知帧可能已经被空闲读收下——起早了
            # 那条预告就被本轮的收尾顺手丢掉。改在 _run 收尾之后起(见 _persist_idle_after)。
            ctx["_idle_after"] = ep
            return None
        # Sent does not mean acknowledged. EOF without output cannot authorize replay.
        _persist_drop(sid)
        if not ctx.get("aborted"):
            code = proc.poll()
            err = self._process_error_tail(ctx, ep.tail_err())
            abnormal = f"常驻进程中断(退出码 {code})" + (f":{err}" if err else "")
            ctx.setdefault("engine_failure", {"source": "transport",
                "code": "NATIVE_TERMINAL_MISSING", "message": abnormal})
            if not ctx["order"]:
                self._emit_text(ctx, f"(引擎中断 {code}:{err})")
        return abnormal

    # ---- 事件发布辅助 ----
    # 曾经有一档 no_live:影子 ctx(自续轮)照发事件、不写 live 快照。影子 ctx 2026-08-20
    # 随旧机制一起撤了,没有生产者再设这个键,所以这里不再分档,直接写快照。
    def _live_snap(self, ctx, force=False):
        self._checkpoint_live(ctx, force=force)

    def _ensure_part(self, ctx, pid, ptype, **kw):
        if pid not in ctx["parts"]:
            part = {"id": pid, "type": ptype, "messageID": ctx["msg_id"], "sessionID": ctx["sid"]}
            part.update(kw)
            if ptype == "text" and "text" not in part:
                part["text"] = ""
            if ctx.get("origin"):
                part["origin"] = ctx["origin"]   # 这一块的来路,随 part 一起走
            ctx["parts"][pid] = part
            ctx["order"].append(pid)
            info = {"id": ctx["msg_id"], "role": "assistant", "sessionID": ctx["sid"]}
            self.bus.publish({"type": "message.updated",
                              "properties": {"info": self._turn_mark(info, ctx)}})
        return ctx["parts"][pid]

    def _delta(self, ctx, pid, field, delta, *, timing_field=None):
        semantic_field = timing_field or field
        if delta and semantic_field in ("text", "reasoning"):
            timing_mark(ctx, "first_" + semantic_field + "_ms")
        self.bus.publish({"type": "message.part.delta",
                          "properties": {"partID": pid, "messageID": ctx["msg_id"],
                                         "sessionID": ctx["sid"], "field": field, "delta": delta}})
        self._live_snap(ctx)

    def _part_updated(self, ctx, part):
        if part.get("type") in ("text", "reasoning") and part.get("text"):
            timing_mark(ctx, "first_" + part["type"] + "_ms")
        self.bus.publish({"type": "message.part.updated", "properties": {"part": part}})
        self._live_snap(ctx, force=True)

    def _emit_execution_result(self, ctx, result):
        """后台任务或子代理交回。先回填到派出它的那一步(本回合里的,或已落盘的上一回合里的),
        步骤行上就是「已返回 · 退出码 · 耗时」,展开看报告;刷新前后同一形态,因为记录投影
        也按调用 id 回填到同一步。认不出那一步时才退回独立的返回卡——Codex 的 agent_message
        没有对应的工具步骤,一直走这条。"""
        if not result:
            return
        if self._return_to_step(ctx, result):
            return
        rows = [ctx["parts"][key] for key in ctx["order"]]
        result = append_result(rows, result)
        result.setdefault("messageID", ctx["msg_id"])
        result.setdefault("sessionID", ctx["sid"])
        if result["id"] not in ctx["parts"]:
            ctx["order"].append(result["id"])
        ctx["parts"][result["id"]] = result
        self.bus.publish({"type": "message.part.updated", "properties": {"part": result}})
        self._live_snap(ctx, force=True)

    def _return_to_step(self, ctx, result):
        parts = ctx.get("parts") or {}
        pid = (ctx.get("tuid") or {}).get(result.get("call_id"))
        step = parts.get(pid) if pid else None
        if step is None:
            step = find_tool_part(list(parts.values()), result.get("call_id"), result.get("task_id"))
        if step is not None:
            # 同步命令的通知只是回声:这一步早已带着自己的结果,通知里没有它还不知道的东西。
            # 既不改它的动词,也不另起一张返回卡——落到卡那条路上就更远了。
            if handed_off(step, result):
                attach_return(step, result)
                self._part_updated(ctx, step)
            return True
        return self._return_to_stored_step(ctx.get("sid"), result)

    def _return_to_stored_step(self, sid, result):
        """派它出去的那一轮早已收尾落盘(引擎空闲时交回的那种):改存储里那一块,并按它所在
        消息的 id 发一次 part.updated,界面上那一行原位更新。"""
        if not sid:
            return False
        hit = {}

        def transform(msgs):
            for m in reversed(msgs):
                step = find_tool_part(m.get("parts") or [], result.get("call_id"), result.get("task_id"))
                if step is not None:
                    attach_return(step, result)
                    hit["part"] = step
                    hit["mid"] = (m.get("info") or {}).get("id") or ""
                    break
            return msgs

        try:
            found = None
            for m in self.messages(sid):
                found = find_tool_part(m.get("parts") or [], result.get("call_id"),
                                       result.get("task_id")) or found
            if found is None:
                return False
            if not handed_off(found, result):
                return True     # 同步回声:不动记录,也不另起返回卡
            self._rewrite_msgs(sid, transform=transform)
        except Exception:
            return False
        if not hit:
            return False
        part = dict(hit["part"], messageID=hit["mid"], sessionID=sid)
        self.bus.publish({"type": "message.part.updated", "properties": {"part": part}})
        return True

    def _part_replaced(self, ctx, part):
        """发布一份明确覆盖旧内容的权威快照。

        普通 updated 仍保留前端的防倒退语义；只有引擎明确把一种表示切到另一种表示
        （如 Codex reasoning 原文切摘要）时才走这里。replace 只属于传输帧，不写进
        live checkpoint 或最终消息。
        """
        snap = dict(part)
        snap["replace"] = True
        self.bus.publish({"type": "message.part.updated", "properties": {"part": snap}})
        self._live_snap(ctx, force=True)

    def _emit_text(self, ctx, text):
        pid = ctx["msg_id"] + "_pX"
        part = self._ensure_part(ctx, pid, "text")
        part["text"] += text
        self._part_updated(ctx, part)

    def build_cmd(self, text, engine_sid, opts):
        raise NotImplementedError

    def _managed_home(self):
        try:
            from codexgate import ensure_codex_home
            return ensure_codex_home()
        except Exception:
            return None

    def spawn_env(self, opts):
        return {}

    def handle_event(self, obj, ctx):
        raise NotImplementedError
