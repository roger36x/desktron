#!/usr/bin/env bash
# 从工作区移除一个应用。用法: delete_app.sh <domain> <uid或id> [--yes]
# 默认 dry-run:只显示将移除什么;加 --yes 才真删。从某工作区(target)移除;
# 若是它最后一个 target(或 domain=*),整条应用定义删除。
set -euo pipefail
DOM="${1:?用法: delete_app.sh <domain> <uid或id> [--yes];domain=* 表示从所有工作区移除}"
KEY="${2:?缺 uid 或 id}"
YES="${3:-}"
PORT="${DESKTRON_UI_PORT:-7765}"

INFO=$(curl -s -m 8 "http://127.0.0.1:$PORT/workbench/apps")
echo "$INFO" | python3 -c "
import json, sys
apps = json.load(sys.stdin)
apps = apps if isinstance(apps, list) else [a for v in apps.values() for a in v]
dom, key = sys.argv[1], sys.argv[2]
a = next((x for x in apps if x.get('uid') == key or x.get('id') == key), None)
if not a:
    print(f\"✗ 没找到应用 '{key}'。现有:{[(x.get('id'), x.get('uid')) for x in apps]}\", file=sys.stderr)
    sys.exit(2)
tg = a.get('targets') or []
print(f\"将从工作区 '{dom}' 移除应用:{a.get('label', a.get('id'))}(id={a.get('id')},uid={a.get('uid')})\")
print(f\"  当前归属 targets: {tg}\")
last = dom == '*' or [t for t in tg if t != dom] == []
print('  这是它最后一个归属 → 整条应用定义将被删除。' if last else '  仅从该工作区移除,其它工作区仍保留。')
" "$DOM" "$KEY"

if [ "$YES" != "--yes" ]; then
  echo "—— 以上是预览,未执行删除。确认无误后重跑并加 --yes:"
  echo "   bash .claude/skills/delete-app/scripts/delete_app.sh $DOM $KEY --yes"
  exit 0
fi

Q() { python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "$1"; }
RESP=$(curl -s -m 8 -X DELETE "http://127.0.0.1:$PORT/workbench/apps/$(Q "$DOM")/$(Q "$KEY")")
echo "$RESP" | python3 -c "
import json, sys
r = json.load(sys.stdin)
print('✓ 已移除(面板刷新后消失)') if r.get('ok') else (print('✗ 删除被拒:', r.get('error','未知错误'), file=sys.stderr) or sys.exit(1))
"
