#!/usr/bin/env python3
"""通过 Desk 本地 API 创建一项安排。"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def build_parser():
    parser = argparse.ArgumentParser(description="创建 Desktron 安排")
    parser.add_argument("--instruction", required=True, help="每次运行要做的事")
    parser.add_argument("--name", default="", help="安排名称；留空则从指令生成")
    parser.add_argument("--kind", choices=("manual", "once", "interval", "daily", "weekdays", "weekly"),
                        required=True, help="触发方式")
    parser.add_argument("--time", help="运行端本地时间 HH:MM")
    parser.add_argument("--date", help="单次执行日期 YYYY-MM-DD")
    parser.add_argument("--minutes", type=int, help="执行间隔，1~10080 分钟")
    parser.add_argument("--weekday", type=int, choices=range(7), help="周一为 0，周日为 6")
    parser.add_argument("--agent-id", default="", help="可选的已有 Agent 或飞书 Bot 代号")
    parser.add_argument("--backend", default="", help="可选运行端；默认当前端")
    parser.add_argument("--disabled", action="store_true", help="创建为暂停状态")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.kind not in {"manual", "interval"} and not args.time:
        parser.error(f"{args.kind} 需要 --time HH:MM")
    if args.kind == "once" and not args.date:
        parser.error("once 需要 --date YYYY-MM-DD")
    if args.kind == "interval" and (args.minutes is None or not 1 <= args.minutes <= 10080):
        parser.error("interval 需要 --minutes 1..10080")
    if args.kind == "weekly" and args.weekday is None:
        parser.error("weekly 需要 --weekday 0..6")
    trigger = {"kind": args.kind}
    if args.kind not in {"manual", "interval"}:
        trigger["time"] = args.time
    if args.kind == "once":
        trigger["date"] = args.date
    if args.kind == "interval":
        trigger["minutes"] = args.minutes
    if args.kind == "weekly":
        trigger["weekday"] = args.weekday
    body = {"instruction": args.instruction.strip(), "trigger": trigger,
            "enabled": not args.disabled}
    session_id = os.environ.get("DESKTRON_SESSION_ID", "").strip()
    if session_id:
        body["session_id"] = session_id
    if args.name.strip():
        body["name"] = args.name.strip()
    if args.agent_id.strip():
        body["agent_id"] = args.agent_id.strip()
    port = os.environ.get("DESKTRON_UI_PORT", "7765")
    url = f"http://127.0.0.1:{port}/workbench/arrangements"
    if args.backend.strip():
        url += "?backend=" + urllib.parse.quote(args.backend.strip())
    request = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
    except urllib.error.HTTPError as ex:
        try:
            detail = json.load(ex).get("error")
        except Exception:
            detail = str(ex)
        print("创建失败：" + str(detail), file=sys.stderr)
        return 1
    except Exception as ex:
        print("创建失败：无法连接 Desk（" + str(ex) + "）", file=sys.stderr)
        return 1
    if not result.get("ok"):
        print("创建失败：" + str(result.get("error") or "未知错误"), file=sys.stderr)
        return 1
    item = result.get("item") or {}
    print(json.dumps({"ok": True, "id": item.get("id"), "name": item.get("name"),
                      "backend": item.get("backend"), "agent_id": item.get("agent_id"),
                      "trigger": item.get("trigger")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
