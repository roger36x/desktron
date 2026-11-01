"""持久撤回事务：状态属于 Desk，历史读取与修改属于引擎 adapter。

只保留最近一笔操作；未完成时不能覆盖，完成凭证跨重启保留。网络调用不持有
索引锁；每次状态写入按 operation id 校验，迟到回调不能改写另一笔操作。
"""
import copy
import time
import uuid
import threading


class RecallPendingError(RuntimeError):
    def __init__(self, block):
        super().__init__(block["error"])
        self.recall_block = block


class RecallMixin:
    AUTO_RECALL_ATTEMPTS = 3
    _RECALL_TRANSITIONS = {
        "requested": {"checking", "done"},
        "checking": {"applying", "done", "blocked", "retryable"},
        "applying": {"verifying", "checking", "done", "blocked", "retryable"},
        "verifying": {"checking", "done", "blocked", "retryable"},
        "blocked": {"checking", "done"},
        "retryable": {"checking", "done"},
    }

    def _recall_load(self, sid):
        with self._lock:
            row = self.index.get(sid) or {}
            old = row.get("recall_revert")
            if old and not row.get("recall"):
                row["recall"] = dict(old, id=str(uuid.uuid4()), phase="requested",
                                     delivery="sent", attempts=0, created_at=time.time())
                row.pop("recall_revert", None)
                self._save_index()
            return copy.deepcopy(row.get("recall") or {})

    def _recall_update(self, sid, operation_id, **patch):
        with self._lock:
            row = self.index.get(sid)
            op = (row or {}).get("recall") or {}
            if op.get("id") != operation_id or op.get("phase") == "done":
                return False
            phase = patch.get("phase", op.get("phase"))
            if phase != op.get("phase") and phase not in self._RECALL_TRANSITIONS.get(op.get("phase"), set()):
                raise ValueError("非法撤回状态迁移")
            if any(op.get(k) and k in patch and patch[k] != op[k]
                   for k in ("thread_id", "before_turn_id")):
                raise ValueError("撤回事务的原生目标不可替换")
            op.update(copy.deepcopy(patch), updated_at=time.time())
            self._save_index()
            return True

    def _recall_begin(self, ctx):
        sid = ctx["sid"]
        self._recall_load(sid)  # 旧字段迁移也持索引锁，不嵌套非重入锁。
        messages = self.messages(sid)
        last = messages[-1] if messages and self._turn_start(messages[-1]) else {}
        info = last.get("info") or {}
        with self._lock:
            op = (self.index.get(sid) or {}).get("recall") or {}
            if op and op.get("phase") != "done":
                # 同一回合的重复 Esc 复用原事务；别的回合不得覆盖待处理的撤回。
                return op.get("id") if op.get("id") == ctx.get("recall_id") else None
            if ctx.get("recall_id"):
                return ctx["recall_id"]
            operation_id = str(uuid.uuid4())
            ctx["recall_id"] = operation_id
            ctx["aborted"] = "recall"
            self.index[sid]["recall"] = {
                "id": operation_id, "phase": "requested", "attempts": 0,
                "created_at": time.time(), "generation": ctx.get("seq"),
                "thread_id": ctx.get("esid") or self._esid_of(sid),
                "before_turn_id": ctx.get("engine_turn_id") or "",
                "delivery": "sent" if ctx.get("recall_dispatched") or
                            ctx.get("engine_turn_id") else "not_sent",
                "projection": {"id": info.get("id"), "at": info.get("at"),
                               "until": int(time.time() * 1000), "message": copy.deepcopy(last)},
            }
            self._save_index()
            return operation_id

    def _remember_recall_revert(self, ctx):
        # 保留调用入口，状态和兼容迁移统一在本模块；不再写第二套 pending 字段。
        operation_id = self._recall_begin(ctx)
        if not operation_id:
            return False
        if ctx.get("engine_turn_id"):
            self._recall_update(ctx["sid"], operation_id,
                                thread_id=ctx.get("esid") or self._esid_of(ctx["sid"]),
                                before_turn_id=ctx["engine_turn_id"], delivery="sent")
        return True

    def _apply_recall_revert(self, sid, env):
        guard = self._recall_guard(sid)
        if not guard.acquire(blocking=False):
            return {"ok": False, "error": "撤回正在核验，请稍后重试"}
        try:
            return self._recall_apply_locked(sid, env)
        finally:
            guard.release()

    def _recall_guard(self, sid):
        with self._lock:
            if not hasattr(self, "_recall_locks"):
                self._recall_locks = {}
            return self._recall_locks.setdefault(sid, threading.Lock())

    def resume_pending_recalls(self):
        """适配器首次加载时续跑持久撤回事务；页面是否打开不影响恢复。"""
        with self._lock:
            pending = [sid for sid, row in self.index.items()
                       if isinstance(row, dict)
                       for op in [row.get("recall") or {}]
                       if op.get("id") and op.get("phase") != "done" and
                          not op.get("auto_failed") and not op.get("recovery_session_id")]
        for sid in pending:
            self.resume_pending_recall(sid)
        return len(pending)

    def resume_pending_recall(self, sid):
        with self._lock:
            op = (self.index.get(sid) or {}).get("recall") or {}
            if (not op.get("id") or op.get("phase") == "done" or
                    op.get("auto_failed") or op.get("recovery_session_id")):
                return False
            if not hasattr(self, "_recall_workers"):
                self._recall_workers = set()
            key = (sid, op["id"])
            if key in self._recall_workers:
                return False
            self._recall_workers.add(key)
        def run():
            try:
                import self_update
                if not self_update.begin_work(user_activity=False, restart_blocking=False):
                    return
                try:
                    self.resolve_recall(sid, op["id"], "auto")
                finally:
                    self_update.finish_work(restart_blocking=False)
            finally:
                with self._lock:
                    self._recall_workers.discard(key)
        threading.Thread(target=run, daemon=True, name=f"recall-{sid[-8:]}").start()
        return True

    def recall_actions(self, sid):
        op = self._recall_load(sid)
        if not op or op.get("phase") == "done":
            return []
        if op.get("recovery_session_id"):
            return [{"id": "recover", "label": "打开恢复副本"}]
        actions = [{"id": "retry", "label": "重试撤回"}]
        # 写入意图落盘之后，响应丢失也可能意味着历史已删除，不能再恢复界面消息。
        if op.get("delivery") == "sent" and not op.get("expected") and (op.get("projection") or {}).get("message"):
            actions.append({"id": "keep", "label": "保留消息继续"})
        if self.name == "codex" and op.get("thread_id") and op.get("before_turn_id"):
            actions.append({"id": "recover", "label": "恢复到副本"})
        return actions

    def recall_block(self, sid):
        op = self._recall_load(sid)
        if op and op.get("phase") != "done":
            return {"error": "上一条消息的撤回尚未完成，请先处理撤回", "code": "recall_pending",
                    "recall": self.turn_state(sid).get("recall")}
        if (self.index.get(sid) or {}).get("recovery_pending"):
            return {"error": "恢复副本正在核验，请稍后发送", "code": "recall_pending"}
        return None

    def prompt_async(self, sid, *args, **kwargs):
        block = self.recall_block(sid)
        if block:
            raise RecallPendingError(block)
        return super().prompt_async(sid, *args, **kwargs)

    def resolve_recall(self, sid, operation_id, action):
        # 与正常发送共用回合锁，不能在检查空闲之后又被另一条发送抢进去。
        with self._lock:
            turn_guard = self._turn_locks.setdefault(sid, threading.Lock())
        if not turn_guard.acquire(blocking=False):
            return {"ok": False, "error": "回合仍在停止，请稍后处理撤回"}
        guard = self._recall_guard(sid)
        acquired = False
        try:
            acquired = guard.acquire(blocking=False)
            if not acquired:
                return {"ok": False, "error": "撤回正在核验，请稍后重试"}
            op = self._recall_load(sid)
            if not op or op.get("id") != operation_id:
                return {"ok": False, "error": "撤回事务已变化，请刷新后重试"}
            if op.get("phase") == "done":
                return {"ok": True, "receipt": op.get("receipt"), **self.turn_state(sid)}
            if action != "auto" and action not in {a["id"] for a in self.recall_actions(sid)}:
                return {"ok": False, "error": "当前撤回状态不支持此操作"}
            if self._live.get(sid):
                return {"ok": False, "error": "回合仍在停止，请稍后处理撤回"}
            self._recall_update(sid, operation_id, resolution_error=None, auto_failed=False)
            from .recap import _fork_engine_env
            env = _fork_engine_env(self, self.session_options(sid))
            if action == "auto":
                return {**self._recall_auto_locked(sid, operation_id, env),
                        **self.turn_state(sid)}
            if action == "keep":
                message = copy.deepcopy(op["projection"]["message"])
                # 先恢复消息，后撤掉 cut；中途退出仍被 pending 挡住，重试按 ID 幂等。
                def restore(messages):
                    if not any((m.get("info") or {}).get("id") == op["projection"]["id"] for m in messages):
                        position = next((i for i, m in enumerate(messages)
                                         if ((m.get("info") or {}).get("at") or 0) > op["projection"]["at"]),
                                        len(messages))
                        messages.insert(position, message)
                    return messages
                self._rewrite_msgs(sid, transform=restore)
                with self._lock:
                    row = self.index[sid]
                    row["recall_cuts"] = [c for c in row.get("recall_cuts", [])
                                          if c[0] != op["projection"]["at"]]
                    row["recall"].update(phase="done", error=None, completed_at=time.time(),
                                          receipt={"ok": True, "method": "kept-interrupted"})
                    self._save_index()
                self._env_publish(sid, {"state": "ready", "stage": "recall",
                                       "label": "撤回已取消，原消息保留", "details": []})
                return {"ok": True, **self.turn_state(sid)}
            if action == "retry":
                out = self._recall_apply_locked(sid, env)
            else:
                out = self._recall_recover_locked(sid, op, env)
            return {**out, **self.turn_state(sid)}
        except Exception as exc:
            if self._recall_update(sid, operation_id, resolution_error=str(exc)[:300],
                                   auto_failed=action == "auto"):
                self._recall_warning(sid, str(exc)[:300])
            return {"ok": False, "error": str(exc)[:300]}
        finally:
            if acquired:
                guard.release()
            turn_guard.release()

    def _recall_auto_locked(self, sid, operation_id, env):
        """兑现用户的“撤回”意图，不让用户选择内部补偿策略。

        原会话先走可验证、可幂等的原生撤回；临时错误有限重试。确认原历史不能
        安全修改后，在隔离副本中完成撤回并发布副本，原会话始终保留。
        """
        while True:
            op = self._recall_load(sid)
            if not op or op.get("id") != operation_id:
                return {"ok": False, "error": "撤回事务已变化"}
            if op.get("phase") == "done":
                return {"ok": True, "receipt": op.get("receipt")}
            if op.get("recovery_session_id"):
                return {"ok": True, "session_id": op["recovery_session_id"]}

            can_recover = (self.name == "codex" and op.get("thread_id") and
                           op.get("before_turn_id"))
            if op.get("phase") == "blocked":
                if can_recover:
                    self._env_publish(sid, {"state": "working", "stage": "recall",
                        "label": "原会话无法安全撤回，正在恢复到副本", "details": []})
                    return self._recall_recover_locked(sid, op, env)
                error = op.get("error") or "无法确认原会话的安全撤回边界"
                self._recall_update(sid, operation_id, auto_failed=True,
                                    resolution_error=error)
                return {"ok": False, "error": error}

            if int(op.get("attempts") or 0) < self.AUTO_RECALL_ATTEMPTS:
                out = self._recall_apply_locked(sid, env)
                if out.get("ok"):
                    return out
                fresh = self._recall_load(sid)
                if fresh.get("phase") == "blocked":
                    continue
                if int(fresh.get("attempts") or 0) < self.AUTO_RECALL_ATTEMPTS:
                    time.sleep(0.4 * max(1, int(fresh.get("attempts") or 1)))
                    continue
                op = fresh

            if can_recover:
                self._env_publish(sid, {"state": "working", "stage": "recall",
                    "label": "正在恢复到安全副本", "details": []})
                return self._recall_recover_locked(sid, op, env)
            error = op.get("error") or "自动撤回未完成"
            self._recall_update(sid, operation_id, auto_failed=True,
                                resolution_error=error)
            return {"ok": False, "error": error}

    def _recall_recover_locked(self, sid, op, env):
        if op.get("recovery_session_id"):
            if op["recovery_session_id"] not in self.index:
                raise RuntimeError("恢复副本已不存在，不能自动重新创建")
            return {"ok": True, "session_id": op["recovery_session_id"]}
        from codex_recovery import recover_recalled_thread
        import os
        state = copy.deepcopy(op.get("recovery") or {})
        def checkpoint(**patch):
            state.update(patch)
            if not self._recall_update(sid, op["id"], recovery=state):
                raise RuntimeError("撤回事务已变化，停止恢复")
        out = recover_recalled_thread(
            op["thread_id"], op["before_turn_id"], self.session_cwd(sid), env,
            os.path.join(self.dir, "recall-recovery", op["id"]), state, checkpoint)
        # 预留副本索引和原事务指针一起落盘；崩溃后继续填这一个副本，不重复建会话。
        with self._lock:
            source = self.index[sid]
            nsid = state.get("session_id") or "adp_" + uuid.uuid4().hex[:14]
            if nsid not in self.index:
                self.index[nsid] = {"id": nsid, "domain": source.get("domain", ""),
                    "opts": copy.deepcopy(source.get("opts") or {}), "engine_sid": out["thread_id"],
                    "title": "恢复副本 · " + str(source.get("title") or "Codex 会话")[:45],
                    "time": {"created": int(time.time()*1000), "updated": int(time.time()*1000)},
                    "recovery_pending": True, "recovered_from": sid}
            state["session_id"] = nsid
            source["recall"]["recovery"] = copy.deepcopy(state)
            self._save_index()
        if not self.import_session(out["thread_id"], sid=nsid):
            raise RuntimeError("副本历史已验证，但界面记录尚未恢复；可重试继续")
        with self._lock:
            self.index[nsid].pop("recovery_pending", None)
            self.index[nsid]["title"] = "恢复副本 · " + str(self.index[sid].get("title") or "Codex 会话")[:45]
            self.index[sid]["recall"].update(recovery_session_id=nsid, recovery_receipt=out)
            self._save_index()
        self._env_publish(sid, {"state": "ready", "stage": "recall",
            "label": "已恢复到安全副本", "details": ["原会话已保留"],
            "action": {"kind": "recall-open", "label": "打开副本",
                       "sessionID": nsid}})
        self.bus.publish({"type": "session.updated", "properties": {"sessionID": nsid}})
        return {"ok": True, "session_id": nsid}

    def _recall_warning(self, sid, detail):
        op = self._recall_load(sid)
        failed = bool(op.get("auto_failed"))
        self._env_publish(sid, {"state": "warning", "stage": "recall",
            "label": "自动恢复未完成" if failed else "正在自动恢复撤回",
            "details": [detail],
            "action": ({"kind": "recall", "label": "再次尝试"} if failed else None)})

    def _recall_apply_locked(self, sid, env):
        op = self._recall_load(sid)
        if not op or op.get("phase") == "done":
            return {"ok": True}
        if op.get("recovery_session_id"):
            return {"ok": False, "error": "恢复副本已就绪，请打开副本继续"}
        operation_id = op["id"]
        # 崩溃可能发生在写入意图之后、删除界面投影之前。恢复时只删同一消息 ID，
        # cut 终点使用请求时刻，不能把后来输入的新消息也圈进去。
        projection = op.get("projection") or {}
        if projection.get("id") and projection.get("at"):
            self._rewrite_msgs(sid, transform=lambda messages: [
                m for m in messages if (m.get("info") or {}).get("id") != projection["id"]])
            with self._lock:
                row = self.index.get(sid)
                if row is not None:
                    cuts = row.setdefault("recall_cuts", [])
                    if not any(c[0] == projection["at"] for c in cuts):
                        cuts.append([projection["at"], projection["until"]])
                        del cuts[:-50]
                        self._save_index()
        if op.get("delivery") == "not_sent":
            out = {"ok": True, "method": "not-dispatched"}
        else:
            self._recall_update(sid, operation_id, phase="checking",
                                attempts=op.get("attempts", 0) + 1)
            try:
                out = self._recall_native(sid, op, env, lambda **patch:
                                          self._recall_update(sid, operation_id, **patch))
            except Exception as exc:
                out = {"ok": False, "error": str(exc)[:300]}
        if out.get("ok"):
            self._recall_update(sid, operation_id, phase="done", receipt=out,
                                error=None, completed_at=time.time())
            if ((self.index.get(sid) or {}).get("environment") or {}).get("stage") == "recall":
                self._env_publish(sid, {"state": "ready", "stage": "recall",
                                       "label": "消息已撤回", "details": []})
        else:
            self._recall_update(sid, operation_id, phase="blocked" if out.get("conflict")
                                else "retryable", error=str(out.get("error") or "未知错误")[:300])
            self._recall_warning(sid, str(out.get("error") or "未知错误")[:300])
        return out

    def turn_state(self, sid):
        out = super().turn_state(sid)
        op = self._recall_load(sid)
        if op:
            out["recall"] = {k: op[k] for k in ("id", "phase", "attempts", "error",
                                                         "auto_failed") if k in op}
            out["recall"]["actions"] = self.recall_actions(sid)
            out["recall"]["session_id"] = op.get("recovery_session_id")
            if op.get("resolution_error"):
                out["recall"]["error"] = op["resolution_error"]
            ctx = self._live.get(sid)
            out["recall"]["execution_state"] = "stopped" if not ctx or ctx.get("engine_turn_done") else "stopping"
        return out

    def _turn_state_publish(self, sid, ctx, state):
        # 覆盖通用发布器时仍须先记录完成水位，再生成事件和侧栏读取的状态。
        self._record_turn_completion(sid, ctx, state)
        props = {**self.turn_state(sid), "sessionID": sid, "state": state,
                 "generation": int((ctx or {}).get("seq") or 0), "contract": 1}
        if (ctx or {}).get("interrupt_id"):
            props["interruptID"] = ctx["interrupt_id"]
        self.bus.publish({"type": "session.state", "properties": props})
