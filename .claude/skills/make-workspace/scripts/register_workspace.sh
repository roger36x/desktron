#!/usr/bin/env bash
# 新建一个工作区(域),让它出现在左栏。用法: register_workspace.sh <workspace.json 路径>
# workspace.json: {id,name,desc?,icon?,agent?,fivew?}  —  fivew = 职责说明 markdown 全文(字段名是历史契约)
# 经 ui_server /workbench/domains 落运行态注册表 .workbench/domains.json + 建独立 cwd 目录 + 落说明 markdown
# 不改写仓库分发物 frontend/workbench.json。
set -euo pipefail
WSFILE="${1:?用法: register_workspace.sh <workspace.json 路径>}"
PORT="${DESKTRON_UI_PORT:-7765}"
[ -f "$WSFILE" ] || { echo "找不到 $WSFILE" >&2; exit 1; }
BODY=$(cat "$WSFILE")
python3 -c "import json,sys; json.loads(sys.argv[1])" "$BODY" || { echo "workspace.json 不是合法 JSON" >&2; exit 1; }
RESP=$(curl -s -m 8 -X POST "http://127.0.0.1:$PORT/workbench/domains" -H 'content-type: application/json' -d "$BODY")
echo "$RESP" | python3 -c "
import json,sys
r=json.load(sys.stdin)
if r.get('ok'):
    print(f\"✓ 已新建工作区 {r['id']} —— 左栏可见(刷新或重进工作台)，agent cwd 与 AGENTS.md 已就绪\")
else:
    print('✗ 新建被拒:', r.get('error','未知错误'), file=sys.stderr)
    sys.exit(1)
"
