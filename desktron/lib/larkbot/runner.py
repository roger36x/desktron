"""跑一轮引擎(docs/agent-hosting-design.md「交付顺序」第 1 步)。

非交互一次性调用,不维持 PTY 会话。这不是图省事:上下文由我们自己组装(context.py),
本来就不依赖引擎自己攒的历史,再养一个常驻 PTY 只是多一份会漂移的状态。
代价是每轮都要重新起进程——用户在飞书里等的那几秒里,大头是模型推理不是进程启动。

prompt 走 stdin 不走命令行参数:组装完的上下文动辄几万字,argv 有长度上限
(macOS 1MB、Linux 通常 2MB,再算上环境变量),超了是 E2BIG,而且报错信息与
"prompt 太长"毫无字面关联,排查会绕远。
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import os
import shutil
import subprocess
import threading
import time

from pi_extensions import prompt_isolation_args

_PI_GATE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..",
                                        "engine_adapters", "pi_gate.ts"))
_PROCS = set()
_CANCELLED = set()
_PROCS_LOCK = threading.Lock()
_READY = {}
_READY_LOCK = threading.Lock()

# 登录态通常数小时才变化。每条消息都重新拉一个 CLI 做 auth status，会把一次本可忽略的
# 安全检查变成稳定的首段延迟；成功缓存十分钟，失败只缓存十秒，用户刚登录后无需久等。
READY_OK_TTL = 600
READY_FAIL_TTL = 10

# Bot 每轮已经由 Desk 明确组装角色、记忆、知识与历史。fast 模式隔离交互式客户端的
# 插件/MCP/会话配置，避免把与这个 Bot 无关的几十个工具定义再次送进模型；仍保留引擎
# 自带的只读文件与 shell 能力，lark-cli 等工具继续从 PATH 调用。
FAST_ARGS = {
    "claude": ["--safe-mode", "--no-session-persistence", "--effort", "low"],
    "codex": ["--ignore-user-config", "--ephemeral",
              "-c", "project_doc_max_bytes=0",
              "-c", "model_reasoning_effort=low",
              "-c", "model_verbosity=low"],
    "pi": ["--thinking", "low"],
}

AUTH_CHECKS = {
    "claude": ["auth", "status"],
    "codex": ["login", "status"],
}


def _forget_process(p):
    with _PROCS_LOCK:
        _PROCS.discard(p)


def _finish_process(p):
    with _PROCS_LOCK:
        _PROCS.discard(p)
        cancelled = p in _CANCELLED
        _CANCELLED.discard(p)
    return cancelled


def _kill_process(p):
    try:
        os.killpg(os.getpgid(p.pid), 9)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def active_count():
    with _PROCS_LOCK:
        return sum(1 for process in _PROCS if process.poll() is None)


def cancel_all():
    """Desk 回收时收掉进程内一次性引擎，避免安排运行留下孤儿。"""
    with _PROCS_LOCK:
        procs = [p for p in _PROCS if p.poll() is None]
        _CANCELLED.update(procs)
        _PROCS.clear()
    for p in procs:
        _kill_process(p)

#: 各引擎的非交互调用。值是 (二进制名, 参数表, prompt 怎么给, 模型旗标)。
#: prompt_via="stdin" 表示从标准输入喂,"arg" 表示追加为最后一个参数。
#: 一律用 stdin——留 arg 这个口子只为将来某个引擎不支持 stdin 时不必改结构。
#:
#: 模型旗标是「引擎 × 模型」两根轴里的第二根(参考 botmux:每 bot 有 cliId 也有 model,
#: 同一个 claude 可以跑 opus 或 sonnet)。旗标为 None 表示没核实过怎么传——那时忽略
#: model 并在日志里说一声,不猜:猜错旗标的表现是引擎直接报参数错,一条消息都答不了。
ENGINES = {
    "claude":  ("claude",   ["-p"],   "stdin", "--model"),   # 2026-08-26 实测 --help
    "codex":   ("codex",    ["exec"], "stdin", "-m"),        # 同上
    # 飞书里没有逐工具审批界面，因此 Pi 只开放只读工具；显式 gate 仍负责知识读取纪律。
    # --no-session 避免这条由 Desk 自己携带历史的一次性调用重复落一份 Pi 会话。
    "pi": ("pi", ["-p", "--offline", "--no-session", "--no-extensions",
                  "--extension", _PI_GATE, "--no-approve"] + prompt_isolation_args() +
                 ["--tools", "read,grep,find,ls"], "stdin", "--model"),
}

DEFAULT_TIMEOUT = 300

#: 各引擎怎么列可选模型。值是 (argv 尾部, 输出格式)。格式:
#:   "lines"      一行一个名字
#:   "codex-json" {"models":[{"slug","visibility"...}]},只取 visibility=="list" 的
#:                (做法同 botmux 的 parseDebugModelsJson:单条坏数据跳过,不拖垮整张清单)
#:   "pi-table"   `provider model ...` 表格，压平成 provider/model。
#: claude 没有列表命令,官方就是别名表(--help 原话:alias 或全名),写死这三个。
MODEL_LISTERS = {
    "claude":   None,
    "codex":    (["debug", "models"], "codex-json"),
    "pi":       (["--offline", "--no-extensions", "--no-approve"] +
                 prompt_isolation_args() + ["--list-models"], "pi-table"),
}

CLAUDE_ALIASES = ["opus", "sonnet", "haiku"]


def list_models(engine, timeout=20):
    """→ (模型名列表, err)。列表可空:引擎没装、命令超时、输出解析不出都算 err。

    每次现问引擎,不缓存:名单随引擎版本与账号权限变,缓存的旧名单会让人选到
    一个已经下线的模型——那与手填错名字是同一种失败,这个功能就白做了。
    """
    if engine == "claude":
        return list(CLAUDE_ALIASES), ""
    spec = MODEL_LISTERS.get(engine)
    if not spec:
        return [], f"{engine} 没有列模型的办法,手填"
    bin_path, err = available(engine)
    if err:
        return [], err
    try:
        r = subprocess.run([bin_path] + spec[0], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL,
                           env=dict(os.environ, NO_COLOR="1", TERM="dumb"))
    except subprocess.TimeoutExpired:
        return [], f"{engine} 列模型超时({timeout}s)"
    except Exception as ex:
        return [], f"{engine} 列模型失败:{str(ex)[:120]}"
    out = r.stdout or ""
    if spec[1] == "codex-json":
        try:
            import json as _json
            d = _json.loads(out)
        except ValueError:
            return [], f"{engine} 的模型清单不是 JSON"
        got = []
        for m in (d.get("models") or []):
            if isinstance(m, dict) and isinstance(m.get("slug"), str) \
                    and m["slug"] and m.get("visibility") == "list":
                got.append(m["slug"])
        return got, "" if got else f"{engine} 的清单里没有可见模型"
    if spec[1] == "pi-table":
        got = []
        for line in out.splitlines():
            cols = line.split()
            if len(cols) >= 2 and cols[0] != "provider":
                got.append(f"{cols[0]}/{cols[1]}")
        return got, "" if got else f"{engine} 的清单里没有可用模型"
    got = [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.startswith(" ")]
    return got, "" if got else f"{engine} 返回了空清单" + (f":{(r.stderr or '')[:80]}" if r.stderr else "")


def available(engine):
    """→ (二进制的绝对路径, err)。找不到就说清楚找的是谁,别只回一句 not found。"""
    spec = ENGINES.get(engine)
    if not spec:
        return "", f"不认识的引擎 {engine}(可选:{'/'.join(sorted(ENGINES))})"
    p = shutil.which(spec[0])
    if not p:
        return "", f"{engine} 的命令 `{spec[0]}` 不在 PATH 上"
    return p, ""


def ready(engine, timeout=6, env=None, force=False):
    """→ (是否可执行, 原因)。不仅看二进制，还验证会让进程长时间空等的登录态。

    Pi 的认证与 provider/model 绑定，没有一个可靠的全局 status；它仍由真实运行返回
    精确错误。其余三个 CLI 都有只读 status 命令，先失败比等满 300 秒更可控。
    """
    bin_path, err = available(engine)
    if err:
        return False, err
    check = AUTH_CHECKS.get(engine)
    if not check:
        return True, ""
    e = dict(os.environ)
    e.update(env or {})
    # 同一个二进制在不同 CODEX_HOME/CLAUDE_CONFIG_DIR 下可能是两套登录态。
    home_key = tuple((k, e.get(k, "")) for k in
                     ("CODEX_HOME", "CLAUDE_CONFIG_DIR"))
    key = (engine, bin_path, home_key)
    now = time.monotonic()
    with _READY_LOCK:
        hit = _READY.get(key)
        if hit and not force and now < hit[0]:
            return hit[1], hit[2]
    try:
        r = subprocess.run([bin_path] + check, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL,
                           env=dict(e, NO_COLOR="1", TERM="dumb"))
        ok = r.returncode == 0
        why = "" if ok else f"{engine} 尚未登录，请先在运行这台 Desk 的终端完成登录"
    except subprocess.TimeoutExpired:
        ok, why = False, f"{engine} 登录态检查超时({timeout}s)"
    except Exception as ex:
        ok, why = False, f"{engine} 登录态检查失败:{str(ex)[:100]}"
    with _READY_LOCK:
        _READY[key] = (now + (READY_OK_TTL if ok else READY_FAIL_TTL), ok, why)
    return ok, why


def _argv(engine, bin_path, model="", performance="fast"):
    """构造一次性运行命令。独立成函数，便于把性能配置钉成单测。"""
    spec = ENGINES[engine]
    argv = [bin_path] + list(spec[1])
    if performance == "fast":
        argv += list(FAST_ARGS.get(engine) or [])
    if model and spec[3]:
        argv += [spec[3], model]
    return argv


def run(engine, prompt, cwd="", timeout=DEFAULT_TIMEOUT, env=None, on_chunk=None,
        model="", log=None, performance="fast", metrics=None, check_ready=True):
    """跑一轮。→ (输出文本, err)。

    on_chunk 给流式刷新用:每读到一段就回调一次,调用方拿它去 PATCH 飞书卡片。
    不传则只在结束时拿全量——两条路都走 Popen,不为省事分叉出两套读法。

    model 为空表示用引擎自己的默认。模型名不做校验——名单随引擎版本变,这里核不准;
    名字错了引擎自己会报,那句报错会原样回到飞书里,人看得到错在哪。
    """
    started = time.monotonic()
    if metrics is not None:
        metrics.clear()
        metrics["input_chars"] = len(str(prompt or ""))
    bin_path, err = available(engine)
    if err:
        if metrics is not None:
            metrics["total_ms"] = round((time.monotonic() - started) * 1000, 1)
        return "", err
    if check_ready:
        ok, err = ready(engine, env=env)
        if metrics is not None:
            metrics["ready_ms"] = round((time.monotonic() - started) * 1000, 1)
        if not ok:
            if metrics is not None:
                metrics["total_ms"] = round((time.monotonic() - started) * 1000, 1)
            return "", err
    spec = ENGINES[engine]
    argv = _argv(engine, bin_path, model, performance)
    if spec[2] == "arg":
        argv.append(prompt)

    e = dict(os.environ)
    e.update(env or {})
    e.setdefault("NO_COLOR", "1")        # 卡片里不需要 ANSI 转义序列
    e.setdefault("TERM", "dumb")

    wd = cwd if cwd and os.path.isdir(cwd) else None
    try:
        p = subprocess.Popen(argv, cwd=wd, env=e,
                             stdin=subprocess.PIPE if spec[2] == "stdin" else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, bufsize=1, start_new_session=True)
    except Exception as ex:
        if metrics is not None:
            metrics["total_ms"] = round((time.monotonic() - started) * 1000, 1)
        return "", f"启动 {engine} 失败:{str(ex)[:200]}"
    if metrics is not None:
        metrics["spawn_ms"] = round((time.monotonic() - started) * 1000, 1)
    with _PROCS_LOCK:
        _PROCS.add(p)

    buf, errbuf = [], []

    first_lock = threading.Lock()
    callback_at = [0.0]

    def _pump(stream, sink, cb):
        try:
            for line in stream:
                sink.append(line)
                if sink is buf and metrics is not None and "first_output_ms" not in metrics:
                    with first_lock:
                        metrics.setdefault("first_output_ms",
                                           round((time.monotonic() - started) * 1000, 1))
                # 引擎可能一秒吐数百行；每行 join 全文是 O(n²)。飞书自身 1.5 秒才允许
                # 更新一次，这里 100ms 合并一次足够流畅，也把 CPU 与临时字符串压下来。
                now = time.monotonic()
                if cb and now - callback_at[0] >= 0.1:
                    callback_at[0] = now
                    try:
                        cb("".join(sink))
                    except Exception:
                        pass          # 回调是给 UI 刷新的,它出错不该带塌这一轮
        except Exception:
            pass

    t1 = threading.Thread(target=_pump, args=(p.stdout, buf, on_chunk), daemon=True)
    t2 = threading.Thread(target=_pump, args=(p.stderr, errbuf, None), daemon=True)
    t1.start()
    t2.start()

    if spec[2] == "stdin":
        try:
            p.stdin.write(prompt)
            p.stdin.close()
        except Exception as ex:
            _kill_process(p)
            _forget_process(p)
            if metrics is not None:
                metrics["total_ms"] = round((time.monotonic() - started) * 1000, 1)
            return "", f"写 prompt 失败:{str(ex)[:120]}"

    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # start_new_session 让它自成进程组,killpg 才收得干净:引擎会拉起子进程
        # (语言服务、工具进程),只 kill 父进程会留下一地孤儿
        _kill_process(p)
        t1.join(2)
        _forget_process(p)
        if metrics is not None:
            metrics["timed_out"] = True
            metrics["output_chars"] = len("".join(buf).strip())
            metrics["total_ms"] = round((time.monotonic() - started) * 1000, 1)
        return "".join(buf).strip(), f"超时({timeout}s),已终止"

    t1.join(5)
    t2.join(1)
    if _finish_process(p):
        if metrics is not None:
            metrics["total_ms"] = round((time.monotonic() - started) * 1000, 1)
        return "".join(buf).strip(), "Desk 正在回收，运行已终止"
    out = "".join(buf).strip()
    if metrics is not None:
        metrics["exit_code"] = p.returncode
        metrics["output_chars"] = len(out)
        metrics["total_ms"] = round((time.monotonic() - started) * 1000, 1)
    if p.returncode:
        tail = "".join(errbuf).strip()[-400:]
        return out, f"{engine} 异常退出(code={p.returncode})" + (f";stderr: {tail}" if tail else "")
    if not out:
        tail = "".join(errbuf).strip()[-400:]
        return "", f"{engine} 没有输出" + (f";stderr: {tail}" if tail else "")
    return out, ""
