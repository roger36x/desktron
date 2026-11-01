# 由 ui_server.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)
from term import (_TERMS, _TERMS_LOCK, _live_map, _term_attach_mark, _term_seen, _term_meta_read,
                  _term_persisted, _term_quiet_set, _term_context_domain, SIDEBAR_SERVED)
import http.server, socketserver, urllib.request, urllib.error, urllib.parse
import sys, os, signal, atexit, time, socket, subprocess, json, re, queue, struct, gzip, io, stat
import termpty   # 进程内 PTY 层:pty.fork 引擎、mux 监管、attach 中继。ui_server 重启会断会话(可 --resume)


def _start_pending_command(sess, sid):
    """附着后释放固定命令；写入失败时保留门闸，下一次附着还能重试。"""
    with _TERMS_LOCK:
        if not sess.get("command_pending") or sess.get("command_starting"):
            return False
        sess["command_starting"] = True
    written = False
    try:
        # ConPTY 的控制台行输入需要 CR 才提交；POSIX 的 read 保持 LF。
        start_signal = b"\r" if os.name == "nt" else b"\n"
        written = bool(termpty.pty_input(sid, start_signal))
        return written
    finally:
        with _TERMS_LOCK:
            sess.pop("command_starting", None)
            if written:
                sess.pop("command_pending", None)
        if written:
            try:
                from provision import setup_tasks
                setup_tasks.started(sid)
            except Exception as error:
                print('[setup-task] 保存启动状态失败: ' + type(error).__name__, flush=True)


class UiTermWsMixin:

    # ---- 终端嵌入:WebSocket 附着到常驻终端会话(/term?session=<id> 切换;?engine=<e> 开新) ----
    def _term_ws(self):
        import base64, hashlib, select, threading
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        sid = (q.get("session") or [None])[0]
        requested_context = (q.get("context_domain") or [""])[0]
        context_domain = _term_context_domain(sid, requested_context) if sid else ""
        def _q_int(name):
            try:
                v = int((q.get(name) or [""])[0])
                return v if v > 0 else None
            except Exception:
                return None
        init_cols, init_rows = _q_int("cols"), _q_int("rows")
        # 引擎的 TUI 形态 2026-08-20 退场:这条 WS 只负责附着已经存在的 PTY
        # (shell 终端、引擎登录、自更新)。会话由建它的那条路先起好进程,这里不再代开——
        # 原来 sid 认不出时会 _term_open 拉一个引擎起来,那是 TUI 唯一的入口。
        live_ids = set(_live_map().keys())
        reattach = bool(sid and sid in live_ids)   # 连上前 PTY 已活着=重连(同进程换标签页)
        sess = None
        if sid:
            with _TERMS_LOCK:
                sess = _TERMS.get(sid)
            if sess is None and sid in live_ids:
                # PTY 活着但 _TERMS 丢了元数据(重启后场景):补一条最小的,够 attach 用
                _m = _term_meta_read().get(sid) or {}
                sess = {"id": sid, "engine": _m.get("engine") or "shell",
                        "domain": _m.get("domain", ""), "title": "终端",
                        "created": int(time.time()), "task": bool(_m.get("task"))}
                with _TERMS_LOCK:
                    _TERMS[sid] = sess
        if not sess:
            self.send_error(404, "no such terminal"); return
        sid = sess["id"]
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400, "expected websocket"); return
        # 在 WS 握手前先装 on_close：进程若在握手/attach 前退出，
        # on_close 正常触发通知 WS 关闭，而非被 mux 静默移除导致 attach 失败
        sock = self.connection
        stop = {"v": False}
        handshook = {"v": False}
        def on_close(info=None):   # PTY 退出:把退出原因经 WS close 帧带给前端,再关连接、中断输入循环
            stop["v"] = True
            try:
                # 握手完成才发 WS close 帧(否则连接还在 HTTP 阶段,发帧无意义);
                # reason 限 123 字节,前端 ws.onclose 的 e.reason 取到后显示成退出说明。
                if handshook["v"] and info and info.get("text"):
                    reason = info["text"].encode("utf-8")[:123]
                    sock.sendall(self._ws_frame(struct.pack("!H", 1000) + reason, 0x8))
            except Exception: pass
            try: sock.close()
            except Exception: pass
        termpty.pty_set_on_close(sid, on_close)
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        handshook["v"] = True

        def sink(data):   # mux/回放 → 浏览器:原始 PTY 字节 → WS binary 帧(唯一发送方,与回放串行,无竞争)
            try: sock.sendall(self._ws_frame(data, 0x2))
            except Exception: stop["v"] = True
        engine = sess.get("engine")
        if init_cols and init_rows:
            termpty.pty_resize(sid, init_cols, init_rows)   # attach 前先按客户端几何定尺寸再 replay,使回放字节与客户端列宽一致(全引擎,含 claude)
        if not termpty.pty_attach(sid, sink, on_close):   # 串行回放当前屏 + 设 sink 接管实时输出
            # 原来这里会重开一个引擎再附着。TUI 退场后没有可重开的东西:PTY 没了就是没了,
            # 关掉 WS,前端按退出说明收起那一块。
            print(f"[TWS] pty_attach FALSE sid={sid}"
                  f"{' (on_close fired)' if stop['v'] else ''} → close WS", file=sys.stderr, flush=True)
            if not stop["v"]:
                try: sock.close()
                except Exception: pass
            return
        try:   # 诊断:切会话空白问题——确认 reattach/几何/buf 裁剪状态(若仍复现,看这行)
            _bi = termpty.pty_buf_info(sid)
            print(f"[TWS] attach sid={sid[:8]} eng={engine} reattach={reattach} geom={init_cols}x{init_rows} "
                  f"buf={_bi and _bi['len']} alt={_bi and _bi['alt']}", file=sys.stderr, flush=True)
        except Exception: pass
        _term_attach_mark(sid, True)   # 标记在看=已读;清未读
        _term_quiet_set(sid)           # 附着触发整屏重绘静默窗(回退路径用)
        buf = b""
        first_resize = True
        # 任务终端已能接收输出，才释放启动闸；只有写成功才清 pending。PTY 若正好在
        # attach 窗口切换，失败会留给下一次附着重试，不能静默吞掉唯一一次启动机会。
        _start_pending_command(sess, sid)
        try:
            while not stop["v"]:
                r, _, _ = select.select([sock], [], [], 30)
                if sock not in r:
                    continue
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    frame, buf, op = self._ws_parse(buf)
                    if frame is None: break
                    if op == 0x8: raise StopIteration
                    if op == 0x1:
                        try:
                            m = json.loads(frame.decode("utf-8", "ignore"))
                            if isinstance(m.get("resize"), list):
                                c, rr = int(m["resize"][0]), int(m["resize"][1])
                                # 重连首次 resize:抖一行逼 SIGWINCH,让内联 TUI 整屏重绘——回放只还原历史字节,
                                # 当前帧(输入框/状态栏)靠这次重绘补全。两次 resize 必须间隔:背靠背时 TUI 会把
                                # 两个 SIGWINCH 合并、只读到最终尺寸(与原来相同)而跳过重绘,这正是切到会话时
                                # 聊天框空白/残帧的主因。不分忙闲都抖:正在输出的会话只做增量更新、不会自己整屏
                                # 重绘,反而最需要这次强制重绘(此前按忙闲跳过是错的)。
                                force_redraw = bool(m.get("redraw")) and engine == "codex"
                                do_jiggle = force_redraw or (first_resize and reattach)
                                first_resize = False
                                if do_jiggle:
                                    termpty.pty_redraw(sid, c, rr)   # 两次 SIGWINCH 留间隔，逼 TUI 整屏重绘
                                else:
                                    termpty.pty_resize(sid, c, rr)
                                _term_quiet_set(sid)   # resize 触发重绘,静默一窗
                        except Exception: pass
                    elif op == 0x2:
                        termpty.pty_input(sid, frame)
        except (StopIteration, BrokenPipeError, OSError):
            pass
        finally:
            stop["v"] = True
            _term_attach_mark(sid, False)   # 不再在看
            _term_quiet_set(sid)            # 尾随重绘静默窗(回退路径)
            termpty.pty_detach(sid, sink)   # detach 保活,会话继续在 mux 里跑
            try: sock.close()
            except Exception: pass

    @staticmethod
    def _ws_frame(payload, opcode=0x2):
        # server→client,不加掩码
        n = len(payload); h = bytearray([0x80 | opcode])
        if n < 126: h.append(n)
        elif n < 65536: h.append(126); h += __import__("struct").pack(">H", n)
        else: h.append(127); h += __import__("struct").pack(">Q", n)
        return bytes(h) + payload

    @staticmethod
    def _ws_parse(buf):
        # 返回 (payload|None, 剩余buf, opcode);client→server 帧带掩码
        if len(buf) < 2: return None, buf, 0
        b0, b1 = buf[0], buf[1]
        op = b0 & 0x0f; masked = b1 & 0x80; ln = b1 & 0x7f; i = 2
        if ln == 126:
            if len(buf) < 4: return None, buf, 0
            ln = __import__("struct").unpack(">H", buf[2:4])[0]; i = 4
        elif ln == 127:
            if len(buf) < 10: return None, buf, 0
            ln = __import__("struct").unpack(">Q", buf[2:10])[0]; i = 10
        mask = b""
        if masked:
            if len(buf) < i + 4: return None, buf, 0
            mask = buf[i:i + 4]; i += 4
        if len(buf) < i + ln: return None, buf, 0
        payload = bytearray(buf[i:i + ln])
        if masked:
            for j in range(ln): payload[j] ^= mask[j & 3]
        return bytes(payload), buf[i + ln:], op
