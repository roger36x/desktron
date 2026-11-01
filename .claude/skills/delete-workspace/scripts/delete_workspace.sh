#!/usr/bin/env bash
# 删除(注销)一个运行态工作区。用法: delete_workspace.sh <id> [--yes]
# 默认 dry-run:只显示将删什么;加 --yes 才真删。只删运行态(.workbench/domains.json)工作区;
# 工作流全部是运行态条目(不再随发行版内置),一律可删。删除只摘注册表,目录与会话不动。
set -euo pipefail
ID="${1:?用法: delete_workspace.sh <id> [--yes]}"
YES="${2:-}"
PORT="${DESKTRON_UI_PORT:-7765}"

INFO=$(curl -s -m 8 "http://127.0.0.1:$PORT/workbench/domains")
echo "$INFO" | python3 -c "
import json, sys
doms = json.load(sys.stdin)
did = sys.argv[1]
d = next((x for x in doms if x.get('id') == did), None)
if not d:
    ids = [x.get('id') for x in doms]
    print(f\"✗ 没有这个工作流 '{did}'。先列一遍 .workbench/domains.json 让用户指认。\", file=sys.stderr)
    print(f\"  现有运行态工作区: {ids}\", file=sys.stderr)
    sys.exit(2)
print(f\"将删除工作区:{d.get('name')}(id={did},kind={d.get('kind','workspace')})\")
print(\"  只从注册表注销;它的独立目录 domains/%s/ 与名下会话不会被删。\" % did)
" "$ID"

if [ "$YES" != "--yes" ]; then
  echo "—— 以上是预览,未执行删除。确认无误后重跑并加 --yes:"
  echo "   bash .claude/skills/delete-workspace/scripts/delete_workspace.sh $ID --yes"
  exit 0
fi

RESP=$(curl -s -m 8 -X DELETE "http://127.0.0.1:$PORT/workbench/domains/$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "$ID")")
echo "$RESP" | python3 -c "
import json, sys
r = json.load(sys.stdin)
print('✓ 已注销工作区(左栏刷新后消失)') if r.get('ok') else (print('✗ 删除被拒:', r.get('error','未知错误'), file=sys.stderr) or sys.exit(1))
"
