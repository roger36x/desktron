"""Claude 长期令牌：只供 Desk 的 Shepherd Claude 会话使用，单独存放在本机。

Shepherd 经 Agent SDK 恢复 Claude 会话时，会从钥匙串复制一份不含 refreshToken 的登录令牌，
常驻进程无法续期，副本到期后回合以 401 / OAuth token revoked 失败（见 claude_auth.py）。
`claude setup-token` 生成一年期令牌；设为 CLAUDE_CODE_OAUTH_TOKEN 时，CLI 优先使用它，
不再依赖那份复制件。

令牌不放进 ~/.config/desktron/env：那份文件会注入所有引擎（含 Pi、Codex）的子进程。
这里单独存放，只在 Shepherd 的 Claude 主回合与 recap 注入；Bash 工具的子进程由
Shepherd 的 BASH_ENV 脚本清除，不会被 env 之类的命令打印出来。

用法：
    python3 claude_token.py login    # 自动运行 `claude setup-token`，浏览器授权后直接保存令牌并验证
    python3 claude_token.py verify   # 只用已保存的令牌发一次最小请求，确认可用
    python3 claude_token.py status   # 只显示是否已配置与保存时间
    python3 claude_token.py set      # 手动粘贴令牌（从标准输入或不回显的提示读取）
    python3 claude_token.py clear

OAuth 授权必须由账号本人在浏览器里确认，这一步不能自动完成。login 在伪终端里运行
setup-token，从输出中取出令牌直接写入文件；令牌不打印、不经过命令行参数和日志。
"""
import oscompat  # noqa: F401  Windows 文件与进程兼容；不提供 POSIX PTY。
import json
import os
import re
import sys
import time

ENV_NAME = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_PATH = os.path.expanduser("~/.config/desktron/claude-oauth-token")
_MIN_LEN, _MAX_LEN = 20, 4096


def _valid(token):
    return (isinstance(token, str) and _MIN_LEN <= len(token) <= _MAX_LEN
            and token.isprintable() and not any(ch.isspace() for ch in token))


def read(path=None):
    """返回已保存的令牌；未配置返回空串。文件存在但无效时抛 ValueError，不悄悄忽略。"""
    path = path or TOKEN_PATH
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    except FileNotFoundError:
        return ""
    if not _valid(token):
        raise ValueError("Claude 长期令牌文件内容无效；请重新运行 claude_token.py set")
    return token


def write(token, path=None):
    path = path or TOKEN_PATH
    token = str(token or "").strip()
    if not _valid(token):
        raise ValueError("令牌格式无效：应为 `claude setup-token` 输出的一整段文字，不含空格或换行")
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    import tempfile
    # 独占创建随机的 0600 文件，不跟随遗留 .tmp 链接或复用其宽松权限。
    fd, tmp = tempfile.mkstemp(prefix=".claude-oauth-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def clear(path=None):
    try:
        os.remove(path or TOKEN_PATH)
        return True
    except FileNotFoundError:
        return False


def status(path=None):
    """不含令牌本身的状态，可安全显示。"""
    path = path or TOKEN_PATH
    try:
        configured = bool(read(path))
        error = ""
    except ValueError as exc:
        configured, error = False, str(exc)
    saved = ""
    if os.path.exists(path):
        saved = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
    return {"configured": configured, "path": path, "saved_at": saved, "error": error}


def apply(env, path=None, log=None):
    """给 Shepherd Claude 进程的环境补上长期令牌；调用方已显式设置时不覆盖。

    令牌文件无效时记录日志并保持原环境（回到钥匙串登录），不中断回合。
    """
    out = dict(env or {})
    if out.get(ENV_NAME):
        return out
    try:
        token = read(path)
    except (OSError, ValueError) as exc:
        if log:
            log("claude-token", f"未使用长期令牌：{exc}")
        return out
    if token:
        out[ENV_NAME] = token
    return out


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()*+][0-9A-Za-z]|\x1b[@-_]")
_LONG = re.compile(r"[A-Za-z0-9_\-]{24,}")
LOGIN_TIMEOUT_S = 300


def _child_env(env=None):
    """setup-token 用默认配置目录与钥匙串登录；去掉 Desk 会话注入的 Claude 运行变量。"""
    out = dict(os.environ if env is None else env)
    for key in list(out):
        if (key.startswith(("CLAUDE_CODE_", "CLAUDE_AGENT_SDK")) or key in (
                "CLAUDE_CONFIG_DIR", "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT",
                "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "BASH_ENV")):
            out.pop(key, None)
    out["TERM"] = out.get("TERM") if out.get("TERM", "dumb") != "dumb" else "xterm-256color"
    return out


def _claude_binary(binary=None):
    if binary:
        return binary
    try:
        import cli_path
        found = cli_path.which("claude")
    except Exception:
        import shutil
        found = shutil.which("claude")
    if not found:
        raise RuntimeError("找不到可用的 claude 命令")
    return found


_CURSOR_FORWARD = re.compile(r"\x1b\[(\d*)C")
_TOKEN_PREFIX = re.compile(r"sk-ant-oat\d+-[A-Za-z0-9_\-]{20,}")


def _plain(screen):
    """去掉终端控制码。Ink 用“光标右移”代替空格，先换回空格再去掉其余控制码。"""
    text = _CURSOR_FORWARD.sub(lambda m: " " * int(m.group(1) or 1), screen.replace("\r", "\n"))
    return _ANSI.sub("", text)


def extract_token(screen):
    """从 setup-token 的终端输出中取出令牌。

    先按界面结构取 “Your OAuth token (valid for …):” 与 “Store this token securely” 之间的文字；
    比较前去掉全部空白，因此不受空格、折行和颜色码的显示方式影响。结构对不上时，再按
    订阅令牌前缀 sk-ant-oat 识别。两条路径都必须等到后续保存提示完整到达；PTY 当前
    缓冲区末尾不代表令牌结束，不能保存中途分块的前缀。
    """
    compact = "".join(_plain(screen).split())
    boundary = compact.rfind("Storethistokensecurely")
    if boundary < 0:
        return ""
    start = compact.rfind("YourOAuthtoken(validfor", 0, boundary)
    if start >= 0:
        colon = compact.find("):", start)
        end = compact.find("Storethistokensecurely", colon)
        if colon >= 0 and end >= 0:
            token = compact[colon + 2:end]
            if _valid(token):
                return token
    found = _TOKEN_PREFIX.findall(compact[:boundary])
    return found[-1] if found and _valid(found[-1]) else ""


def _redacted_tail(screen, lines=8):
    rows = [" ".join(row.split()) for row in _plain(screen).splitlines() if row.strip()]
    return "\n".join(_LONG.sub("[已隐去]", row)[:200] for row in rows[-lines:])


def login(binary=None, timeout=LOGIN_TIMEOUT_S, path=None, say=print):
    """在伪终端里运行 `claude setup-token`，取得令牌后保存并结束子进程。

    令牌只在本进程内存与目标文件中出现；向调用方输出的只有进度说明和授权网址。
    返回 True 表示已保存。
    """
    if oscompat.IS_WINDOWS:
        raise RuntimeError('自动获取长期令牌需要 POSIX PTY；Windows 请在外部终端运行 claude setup-token，再用本工具 set 从标准输入保存。')
    import fcntl, pty, select, struct, termios
    exe = _claude_binary(binary)
    pid, fd = pty.fork()
    if pid == 0:   # 子进程：成为伪终端的会话首进程后执行 claude
        try:
            os.execvpe(exe, [exe, "setup-token"], _child_env())
        finally:
            os._exit(127)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 1000, 0, 0))   # 宽屏，避免令牌折行
    say("已启动 claude setup-token。浏览器会打开 Claude 的授权页面，请在页面上确认授权。")
    screen, shown_url, enters, saved = "", False, 0, False
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            screen = (screen + chunk.decode("utf-8", "replace"))[-200_000:]
            plain = _plain(screen)
            if not shown_url:
                url = re.search(r"https://(?:claude\.ai|claude\.com|console\.anthropic\.com|platform\.claude\.com)/\S+", plain)
                if url:
                    say("如果浏览器没有自动打开，请手动打开：" + url.group(0))
                    shown_url = True
            if plain.count("Press Enter") > enters:   # 欢迎页或确认页需要回车
                enters = plain.count("Press Enter")
                os.write(fd, b"\r")
            token = extract_token(screen)
            if token:
                # 先保存再收尾：令牌只显示这一次，收尾出任何问题都不能让它丢失。
                write(token, path)
                saved = True
                break
    finally:
        _stop_pty_child(pid, fd)
    if not saved:
        tail = _redacted_tail(screen)
        say("没有取得令牌（超时、授权被拒绝或输出格式变化）。最后的输出：\n" + (tail or "（无）"))
        return False
    say(f"令牌已保存到 {path or TOKEN_PATH}（权限 600），未显示令牌内容。")
    return True


def _stop_pty_child(pid, fd, grace=2.0):
    """结束伪终端里的子进程。

    macOS 上退出中的进程要等终端输出队列排空才能完成退出；不再读取主端时，它会停在
    退出状态，阻塞式 waitpid 随之永久等待（2026-10-09 实见）。所以先发 SIGTERM 并继续
    读掉输出，再关闭主端，最后才等待回收，整个过程有时限。
    """
    import select, signal
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace
    reaped = False
    while time.monotonic() < deadline and not reaped:
        try:
            if select.select([fd], [], [], 0.1)[0]:
                os.read(fd, 65536)
        except OSError:
            pass
        try:
            reaped = os.waitpid(pid, os.WNOHANG)[0] != 0
        except ChildProcessError:
            reaped = True
    try:
        os.close(fd)
    except OSError:
        pass
    if reaped:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for _ in range(50):
        try:
            if os.waitpid(pid, os.WNOHANG)[0]:
                return
        except ChildProcessError:
            return
        time.sleep(0.1)


def verify(binary=None, path=None, timeout=90):
    """只用已保存的令牌发一次最小请求；用空配置目录，排除钥匙串登录。返回 (成功, 说明)。"""
    import shutil, subprocess, tempfile
    token = read(path)
    if not token:
        return False, "未配置长期令牌"
    exe = _claude_binary(binary)
    home = tempfile.mkdtemp(prefix="desk-claude-token-check-")
    try:
        env = _child_env()
        env.update({ENV_NAME: token, "CLAUDE_CONFIG_DIR": home})
        result = subprocess.run([exe, "-p", "Reply with OK.", "--model", "haiku", "--tools", "",
                                 "--strict-mcp-config", "--no-session-persistence", "--max-turns", "1",
                                 "--output-format", "json"], capture_output=True, text=True,
                                timeout=timeout, cwd=home, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"{timeout} 秒内没有返回"
    finally:
        shutil.rmtree(home, ignore_errors=True)
    try:
        data = json.loads((result.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        data = {}
    if result.returncode == 0 and data.get("subtype") == "success" and not data.get("is_error"):
        return True, "令牌可用：不经钥匙串登录完成了一次模型请求"
    detail = data.get("result") or (result.stderr or "").strip()
    return False, "令牌不可用：" + _LONG.sub("[已隐去]", str(detail))[:200]


def desk_log(mod, msg):
    try:
        import deskcfg
        deskcfg.log(mod, msg)
    except Exception:
        print(f"[{mod}] {msg}", file=sys.stderr, flush=True)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="管理 Desk Shepherd Claude 会话使用的长期令牌")
    ap.add_argument("action", choices=("login", "verify", "set", "status", "clear"))
    ap.add_argument("--binary", help=argparse.SUPPRESS)
    ap.add_argument("--timeout", type=int, default=LOGIN_TIMEOUT_S, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.action == "login":
        try:
            if not login(a.binary, a.timeout):
                return 1
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"无法完成：{exc}", file=sys.stderr)
            return 1
        ok, message = verify(a.binary)
        print(message)
        print("重启 Desk 后，新的 Shepherd Claude 会话进程开始使用这个令牌。" if ok else
              "令牌已保存但验证失败；可再运行 `claude_token.py verify` 复查。")
        return 0 if ok else 1
    if a.action == "verify":
        try:
            ok, message = verify(a.binary)
        except (RuntimeError, OSError, ValueError) as exc:
            ok, message = False, str(exc)
        print(message)
        return 0 if ok else 1
    if a.action == "set":
        if sys.stdin.isatty():
            import getpass
            token = getpass.getpass("粘贴 claude setup-token 输出的令牌（不回显）：")
        else:
            token = sys.stdin.read()
        try:
            write(token)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"已保存到 {TOKEN_PATH}（权限 600）。重启 Desk 后，新的 Claude 会话进程开始使用。")
        return 0
    if a.action == "clear":
        print("已删除。" if clear() else "原本未配置。")
        return 0
    info = status()
    if info["error"]:
        print(info["error"], file=sys.stderr)
        return 1
    print(f"已配置，保存于 {info['saved_at']}（{info['path']}）" if info["configured"] else "未配置。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
