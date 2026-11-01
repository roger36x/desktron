#!/usr/bin/env python3
"""Desk Pi Agent 的只读检查、安装计划和脱敏诊断。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[4]
INSTALL_PI = ("npm i -g --prefix $HOME/.local @earendil-works/pi-coding-agent@latest "
              "--registry=https://registry.npmjs.org/")
OPEN_PI_LOGIN = "pi"
TIMEOUT = 8

SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer\s+)?)[^\s,;]+"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|secret)\b\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\b(?:sk|pat|xox[baprs])-[-A-Za-z0-9_]{12,}\b"),
)


def scrub(value: object) -> str:
    text = str(value or "")
    home = str(Path.home())
    if home:
        text = text.replace(home, "~")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", text)
    return text[:4000]


def run(argv: list[str], timeout: int = TIMEOUT) -> dict:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "NO_COLOR": "1"})
        return {"ok": proc.returncode == 0, "code": proc.returncode,
                "stdout": scrub(proc.stdout.strip()), "stderr": scrub(proc.stderr.strip())}
    except FileNotFoundError:
        return {"ok": False, "code": 127, "stdout": "", "stderr": "命令不存在"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": 124, "stdout": "", "stderr": "检查超时"}
    except Exception as error:
        return {"ok": False, "code": 1, "stdout": "", "stderr": scrub(error)}


def binary(name: str, version_args: list[str]) -> dict:
    path = shutil.which(name) or ""
    if not path:
        return {"name": name, "ok": False, "state": "missing", "path": "", "version": ""}
    result = run([path, *version_args])
    version = (result["stdout"] or result["stderr"]).splitlines()[:1]
    return {"name": name, "ok": result["ok"], "state": "ready" if result["ok"] else "unknown",
            "path": scrub(path), "version": version[0] if version else ""}


def pi_protocol(pi_ok: bool) -> dict:
    if not pi_ok:
        return {"ok": False, "state": "blocked", "reason": "pi_missing"}
    lib = ROOT / "desktron" / "lib"
    if str(lib) not in sys.path:
        sys.path.insert(0, str(lib))
    try:
        import engine_protocol
        result = engine_protocol.availability("pi")
    except Exception as error:
        return {"ok": False, "state": "unknown", "reason": "probe_failed",
                "error": scrub(error)}
    return {"ok": bool(result.get("available")),
            "state": "ready" if result.get("available") else "repair_required",
            "reason": result.get("reason", ""), "sdk_version": result.get("sdk_version", ""),
            "error": "" if result.get("available") else scrub(result.get("message", ""))}


def model_catalog(pi_ok: bool) -> dict:
    if not pi_ok:
        return {"ok": False, "state": "blocked", "count": 0}
    pi = shutil.which("pi") or "pi"
    result = run([pi, "--offline", "--no-session", "--list-models"], timeout=12)
    lines = [line for line in result["stdout"].splitlines() if line.strip()]
    return {"ok": result["ok"] and bool(lines),
            "state": "ready" if result["ok"] and lines else ("empty" if result["ok"] else "unknown"),
            "count": len(lines), "error": "" if result["ok"] else result["stderr"]}


def next_actions(checks: dict) -> list[dict]:
    actions = []
    if not checks["node"]["ok"] or not checks["npm"]["ok"]:
        actions.append({"id": "install-node", "label": "安装 Node.js 与 npm", "command": "brew install node",
                        "effect": "在本机安装 Pi 所需运行时", "interactive": False})
        return actions
    if not checks["pi"]["ok"]:
        actions.append({"id": "install-pi", "label": "安装 Pi Coding Agent", "command": INSTALL_PI,
                        "effect": "安装到 ~/.local，不改已有 Provider 凭证", "interactive": False})
        return actions
    if not checks.get("pi_protocol", {}).get("ok"):
        actions.append({"id": "repair-pi", "label": "修复 Pi Coding Agent", "command": INSTALL_PI,
                        "effect": "显式升级 ~/.local 中的 Pi，并重新执行 Desk 协议探针",
                        "interactive": False})
        return actions
    actions.append({"id": "open-pi-login", "label": "登录 Provider", "command": OPEN_PI_LOGIN,
                    "effect": "打开 Pi，由用户在 /login 中选择 Provider", "interactive": True})
    if checks["catalog"]["ok"]:
        actions.append({"id": "verify", "label": "发送真实验证请求", "command": "",
                        "effect": "对用户选择的 Provider 发出最小请求", "interactive": False})
    return actions


def snapshot() -> dict:
    checks = {
        "node": binary("node", ["--version"]),
        "npm": binary("npm", ["--version"]),
        "pi": binary("pi", ["--version"]),
    }
    checks["pi_protocol"] = pi_protocol(checks["pi"]["ok"])
    checks["catalog"] = model_catalog(checks["pi_protocol"]["ok"])
    blocked = not checks["node"]["ok"] or not checks["npm"]["ok"] or not checks["pi"]["ok"]
    repair = checks["pi"]["ok"] and not checks["pi_protocol"]["ok"]
    ready = checks["pi_protocol"]["ok"] and checks["catalog"]["ok"]
    state = "ready_for_verification" if ready else ("blocked" if blocked else
            ("needs_repair" if repair else "needs_user"))
    return {
        "schema": "desk.pi-setup/v1", "mode": "check", "at": int(time.time()),
        "platform": {"system": platform.system(), "release": platform.release(),
                     "machine": platform.machine(), "python": platform.python_version()},
        "state": state, "checks": checks, "next_actions": next_actions(checks),
    }


def diagnostic(data: dict) -> dict:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:10].upper()
    return {
        "schema": "desk.pi-diagnostic/v1", "diagnostic_id": f"PI-{digest}",
        "created_at": int(time.time()), "state": data.get("state"),
        "platform": data.get("platform"), "checks": data.get("checks"),
        "privacy": {"credentials": False, "conversation_content": False,
                    "user_files": False, "full_config": False},
    }


def report_draft(args) -> dict:
    data = diagnostic(snapshot())
    code = re.sub(r"[^A-Z0-9_.-]", "-", args.error_code.upper())[:64] or "PI-UNKNOWN"
    summary = scrub(args.summary).replace("\n", " ")[:300]
    return {
        "schema": "desk.pi-report-draft/v1", "ready_to_submit": False,
        "blocked_reason": "草稿仅供本机保存；提交渠道由用户决定",
        "title": f"[{code}] {summary}",
        "body": f"现象：{summary}\n错误码：{code}\n诊断编号：{data['diagnostic_id']}\n复现与期望结果：待补充",
        "diagnostic": data,
        "required_before_submit": ["检索重复问题", "检查脱敏结果", "取得用户明确授权"],
    }


def emit(value: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(value, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "plan", "diagnose"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--json", action="store_true")
    report = sub.add_parser("report-draft")
    report.add_argument("--error-code", required=True)
    report.add_argument("--summary", required=True)
    report.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.command == "report-draft":
        value = report_draft(args)
    else:
        value = snapshot()
        if args.command == "plan":
            value = {"schema": value["schema"], "mode": "plan", "state": value["state"],
                     "next_actions": value["next_actions"]}
        elif args.command == "diagnose":
            value = diagnostic(value)
    emit(value, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
