#!/usr/bin/env bash
# 注册一个本地应用到工作区面板。用法: register_app.sh <targets> <app.json路径>
# targets: 一个或多个工作区 id,逗号分隔(如 incident 或 incident,risk),_desktop=全局,*=所有工作区。
#   一份定义可 apply 到多个工作区(引用,非拷贝)。也可在 app.json 里直接写 "targets": [...]。
#   经 ui_server /workbench/apps 校验后落 .workbench/apps.json(扁平注册表)。
set -euo pipefail
TARGETS="${1:?用法: register_app.sh <targets> <app.json路径>;targets 逗号分隔,如 incident 或 incident,risk 或 *}"
APPFILE="${2:?缺 app.json 路径}"
PORT="${DESKTRON_UI_PORT:-7765}"
[ -f "$APPFILE" ] || { echo "找不到 $APPFILE" >&2; exit 1; }
APP=$(cat "$APPFILE")
BODY=$(python3 -c "
import json,sys
targets=[t for t in sys.argv[1].split(',') if t.strip()]
print(json.dumps({'targets':targets,'domain':targets[0] if targets else '','app':json.loads(sys.argv[2])}))
" "$TARGETS" "$APP")
RESP=$(curl -s -m 8 -X POST "http://127.0.0.1:$PORT/workbench/apps" -H 'content-type: application/json' -d "$BODY")
echo "$RESP" | python3 -c "
import json,sys
r=json.load(sys.stdin)
if r.get('ok'):
    print(f\"✓ 已注册 {r['id']} → 工作区 {', '.join(r.get('targets',[]))}（刷新或重进工作区可见）\")
    print(f\"  uid={r['uid']}  （跟踪/优化此应用时，在 app.json 带上这个 uid）\")
else:
    print('✗ 注册被拒，请按下列错误修正 app.json：', file=sys.stderr)
    for e in r.get('errors',[r.get('error','未知错误')]): print('  -', e, file=sys.stderr)
    sys.exit(1)
"
