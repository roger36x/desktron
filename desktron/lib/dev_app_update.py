#!/usr/bin/env python3
"""Build-side client for the native host development update transaction."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

from macos_codesign import verify_stable_app

PORT = 7765


def app_binary(app):
    return os.path.realpath(os.path.join(os.fspath(app), "Contents", "MacOS", "desktron"))


def is_running_app(app):
    """Read-only exact executable check; never scans by product name or sends signals."""
    binary = app_binary(app)
    try:
        result = subprocess.run(["ps", "-axo", "command="], capture_output=True,
                                text=True, timeout=5, check=False)
    except Exception as error:
        raise RuntimeError(f"无法确认原生壳是否运行：{error}") from error
    if result.returncode != 0:
        raise RuntimeError("无法确认原生壳是否运行：ps 返回 %d" % result.returncode)
    return any(row.strip() == binary or row.strip().startswith(binary + " ")
               for row in result.stdout.splitlines())


def request_activation(stage, *, port=PORT):
    stage = Path(stage).resolve()
    app = stage / "Desktron.app"
    host_rev = (app / "Contents/Resources/release-rev").read_text(encoding="utf-8").strip()
    if not host_rev or not (app / "Contents/MacOS/desktron").is_file():
        raise ValueError("开发壳暂存内容不完整")
    verify_stable_app(app)
    body = json.dumps({"stage": stage.name, "confirm": host_rev}).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/workbench/update/dev-app-apply",
        data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try: result = json.loads(error.read().decode("utf-8"))
        except Exception: result = {"ok": False, "error": f"Desk 返回 HTTP {error.code}"}
    if not isinstance(result, dict):
        raise ValueError("Desk 返回了无效激活回执")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    running = sub.add_parser("running"); running.add_argument("--app", required=True)
    activate = sub.add_parser("activate"); activate.add_argument("--stage", required=True)
    args = parser.parse_args()
    if args.command == "running":
        try: return 0 if is_running_app(args.app) else 1
        except Exception as error:
            print(f"desktron: {error}", file=sys.stderr); return 2
    try: result = request_activation(args.stage)
    except Exception as error:
        print(f"desktron: 无法提交开发壳激活事务：{error}", file=sys.stderr); return 1
    print(json.dumps(result, ensure_ascii=False))
    if not result.get("ok"):
        print("desktron: 开发壳激活被拒绝：" + str(result.get("error") or "未知原因"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
