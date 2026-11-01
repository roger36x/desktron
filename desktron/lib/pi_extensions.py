"""Single Desk-owned provider entry for Pi runtime, probes, recap and control RPC.

The aggregate registers only release-controlled providers. Individual providers remain inert
until their own non-secret setup/configuration is ready. Never enable arbitrary user extensions.
"""
import json
from pathlib import Path
import subprocess

import pi_subagents


def provider_extension_args():
    return ["--extension", str(Path(__file__).resolve().parent / "engine_adapters" / "pi_providers.ts")]


def prompt_isolation_args():
    """只让 Desk 管理项目规则和技能发现，不采用 Pi 自动发现的同类内容。"""
    return ["--no-skills", "--no-context-files"]


def enforce_prompt_isolation(args):
    """移除显式技能路径，并确保所有 Pi 启动方式关闭自动提示来源。"""
    source = list(args or [])
    out = []
    index = 0
    while index < len(source):
        arg = source[index]
        if arg == "--skill":
            index += 2
            continue
        if str(arg).startswith("--skill="):
            index += 1
            continue
        out.append(arg)
        index += 1
    insert_at = out.index("--") if "--" in out else len(out)
    for flag in prompt_isolation_args():
        if flag not in out:
            out.insert(insert_at, flag)
            insert_at += 1
    return out


def appended_system_prompts(args):
    """返回最终 argv 中按出现顺序追加的系统提示正文。"""
    source = list(args or [])
    values = []
    index = 0
    while index < len(source):
        arg = str(source[index])
        if arg == "--append-system-prompt" and index + 1 < len(source):
            values.append(str(source[index + 1]))
            index += 2
            continue
        if arg.startswith("--append-system-prompt="):
            values.append(arg.split("=", 1)[1])
        index += 1
    return values


def append_system_prompt_if_missing(args, text):
    """追加缺失的系统正文；已有正文包含该段时不重复。"""
    out = list(args or [])
    text = str(text or "").strip()
    if not text or any(text in value for value in appended_system_prompts(out)):
        return out
    insert_at = out.index("--") if "--" in out else len(out)
    out[insert_at:insert_at] = ["--append-system-prompt", text]
    return out


def enforce_session_path(args, path):
    """移除冲突的会话选择参数，并固定到指定 Pi 会话文件。"""
    source = list(args or [])
    with_value = {"--session", "--session-id", "--fork"}
    standalone = {"--continue", "-c", "--resume", "-r", "--no-session"}
    out = []
    index = 0
    while index < len(source):
        arg = str(source[index])
        if arg in with_value:
            index += 2
            continue
        if any(arg.startswith(flag + "=") for flag in with_value) or arg in standalone:
            index += 1
            continue
        out.append(source[index])
        index += 1
    insert_at = out.index("--") if "--" in out else len(out)
    out[insert_at:insert_at] = ["--session", str(path)]
    return out


def subagent_extension_args():
    if not pi_subagents.available():
        return []
    return ["--extension", str(Path(__file__).resolve().parent / "engine_adapters/pi_children.ts")]


LEGACY_WEB_SECRET_ENV = "VOLCENGINE_WEB_SEARCH_API_KEY"


def without_legacy_web_secrets(env=None):
    cleaned = dict(env or {})
    cleaned.pop(LEGACY_WEB_SECRET_ENV, None)
    return cleaned


def web_extension_args(env=None):
    env = env or {}
    enabled = str(env.get("DESKTRON_WEB_TOOLS") or "").strip().lower()
    if enabled in {"0", "false", "off", "no"}:
        return []
    return ["--extension", str(Path(__file__).resolve().parent / "engine_adapters/pi_web.ts")]


def subagent_env(adapter=None):
    if adapter is None or not pi_subagents.available():
        return {}
    from pi_children import environment
    return environment(adapter)
