"""Claude Code 登录态探测。

只问 CLI 当前能否使用已有订阅登录，不读取、不返回钥匙串内容。钥匙串条目可能已经过期，
也可能只属于 Claude Desktop 注入给内部进程的会话，存在本身不能证明独立 CLI 可用。
"""
import json
import os
import subprocess

from credstore import secrets_env


def subscription_status():
    """返回 True/False/None：订阅可用、订阅不可用、无法可靠探测。

    只认订阅。loggedIn 这一个字段分不出订阅与 API key:环境里放一个 ANTHROPIC_API_KEY,
    没有任何订阅也会得到 loggedIn: true——同一份输出里 apiKeySource 写着 ANTHROPIC_API_KEY、
    subscriptionType 是 null(2026-09-09 实测)。照 loggedIn 显示,界面会对一个从没登录过的人
    写「订阅已登录」,新装 Desk 的同事当场发现这条不对。

    subscriptionType 缺席时退回 loggedIn:老版本 CLI 不发这个字段,拿不到证据就不改判。
    """
    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            env=secrets_env(os.environ),
            timeout=5,
        )
    except Exception:
        # 登录探测是提示信息，环境文件损坏或输出解码失败都不应拖垮 Desk。
        return None

    # 新版输出格式化的多行 JSON；旧版或包装脚本也可能只留一行，兼容两种形状。
    raw_output = result.stdout or ""
    candidates = [raw_output, *reversed(raw_output.splitlines())]
    for raw in candidates:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        logged_in = payload.get("loggedIn")
        if not isinstance(logged_in, bool):
            continue
        if not logged_in:
            return False
        # 有 apiKeySource 就说明这一次实际用的是 API key,不是订阅——即使钥匙串里还留着订阅,
        # CLI 也会优先走 key(那种情形下 subscriptionType 同样是 null)。
        if payload.get("apiKeySource"):
            return False
        if "subscriptionType" in payload:
            return bool(payload.get("subscriptionType"))
        return True
    return None


# ---- 恢复前的订阅令牌续期 ----------------------------------------------------------
#
# Desk 的 Claude 会话经 Agent SDK 从 Desk 自有存储恢复。SDK 恢复时新建临时配置目录，
# 从钥匙串复制一份凭证，并刻意删掉 refreshToken（refresh token 会轮换，副本若自行续期，
# 钥匙串里的那枚随即作废）。于是会话进程只能用复制那一刻的 access token，自己无法续期。
# 钥匙串只在某个读它的 CLI 进程发 API 请求、且令牌剩余不足 5 分钟时才会续期；只用 Desk
# 时没有这样的进程，钥匙串令牌放到过期，此后每次恢复都复制到已过期的令牌，首轮即 401，
# 表现为约每 8 小时要重新登录一次（2026-09-24 实测：29 份副本 refreshToken 全空；
# 9/21 09:30 恢复的会话复制到 09:10 已过期的令牌，首轮 401）。
#
# 这里在派发前读钥匙串里的到期时间（只取 expiresAt，不返回令牌），将到期时用默认配置目录
# 跑一次最小 CLI 请求，让 CLI 走它自己的加锁续期流程并写回钥匙串。`claude auth status`
# 不触发续期，CLI 也没有不经模型的续期入口，所以用一次 haiku 的一句话请求（约 5 秒）。
# 阈值与 CLI 的续期窗口一致：剩余多于 5 分钟时 CLI 会判定 not_needed，提前跑也续不了。
#
# 未覆盖：会话进程常驻期间，它手里那份副本到期后仍会 401，需要另行在到期前回收进程。

import sys
import threading
import time

KEYCHAIN_SERVICE = "Claude Code-credentials"
REFRESH_WINDOW_S = 300           # 与 CLI 的续期判定一致（剩余 < 5 分钟才续）
_REFRESH_TIMEOUT_S = 90
_FAILURE_BACKOFF_S = 120
_refresh_lock = threading.Lock()
_last_failure = 0.0


def keychain_expires_at(runner=subprocess.run):
    """返回钥匙串订阅令牌的到期时间（Unix 秒）；非 macOS、无条目或解析失败返回 None。"""
    if sys.platform != "darwin":
        return None
    try:
        result = runner(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                        capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return None
        oauth = (json.loads(result.stdout.strip()) or {}).get("claudeAiOauth") or {}
        expires = oauth.get("expiresAt")
        if not oauth.get("refreshToken") or not isinstance(expires, (int, float)):
            return None     # 没有 refresh token 的条目续不了，交给正常的登录提示
        return expires / 1000.0
    except Exception:
        return None


def _refresh_env(env):
    clean = {key: value for key, value in dict(env).items()
             if not key.startswith(("CLAUDE_CODE_", "CLAUDE_AGENT_SDK"))
             and key not in ("CLAUDE_CONFIG_DIR", "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT",
                             "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")}
    return clean


def ensure_fresh_subscription(binary, env=None, now=time.time, expires_at=keychain_expires_at,
                              runner=subprocess.run):
    """令牌将到期时先续期。返回 fresh / refreshed / failed / skipped，任何异常都不向上抛。

    只在订阅模式下有意义：调用方已配 ANTHROPIC_API_KEY 或网关时直接 skipped。
    """
    global _last_failure
    env = dict(os.environ if env is None else env)
    if (env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_BASE_URL") or not binary
            or any(env.get(k) for k in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                                        "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_OAUTH_TOKEN"))):
        return "skipped"
    expires = expires_at()
    if expires is None:
        return "skipped"
    if expires - now() > REFRESH_WINDOW_S:
        return "fresh"
    with _refresh_lock:                                   # 同一后端内多个会话只续一次
        expires = expires_at()
        if expires is not None and expires - now() > REFRESH_WINDOW_S:
            return "refreshed"                            # 等锁期间别的会话已续上
        if now() - _last_failure < _FAILURE_BACKOFF_S:
            return "failed"
        try:
            runner([binary, "-p", "Reply with OK.", "--model", "haiku", "--safe-mode",
                    "--tools", "", "--strict-mcp-config", "--no-session-persistence",
                    "--max-turns", "1", "--output-format", "json"],
                   capture_output=True, text=True, timeout=_REFRESH_TIMEOUT_S,
                   cwd=os.path.expanduser("~"), env=_refresh_env(env), stdin=subprocess.DEVNULL)
        except Exception:
            pass
        expires = expires_at()
        if expires is not None and expires - now() > REFRESH_WINDOW_S:
            return "refreshed"
        _last_failure = now()
        return "failed"
