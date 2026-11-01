#!/usr/bin/env bash
# 等其他会话都空下来 → 重启 Desk → 跑三项功能核验,结果写进 /tmp/cd_verify.txt。
# 只在明确要交付那三项(子代理 / 目标 / 压缩)时用:重启会打断所有在跑的会话,
# 记录还在,可以 --resume 接回去,但那一轮已经生成的内容会丢。
set -u
BASE="http://127.0.0.1:7765"
OUT=/tmp/cd_verify.txt
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# 发起这次重启的那段会话不算数:它要等重启完成才结束,而重启要等它结束——
# 两边互等就永远不动。SELF 传它的 id 前缀,数别人。
SELF="${SELF:-}"
busy_others() {
  curl -s "$BASE/workbench/terminals" 2>/dev/null | SELF="$SELF" python3 -c '
import json,os,sys
me=os.environ.get("SELF","")
try: d=json.load(sys.stdin)
except Exception: print(9); raise SystemExit
def mine(t):
    return me and (str(t.get("id","")).startswith(me) or str(t.get("resume_id","")).startswith(me))
print(sum(1 for t in d if t.get("busy") and not mine(t)))' 2>/dev/null || echo 9
}

echo "等其他会话空下来(不含发起方 $SELF)…"
for i in $(seq 1 360); do          # 最多等一小时
  n=$(busy_others)
  [ "$n" = "0" ] && { echo "都空了(等了约 $((i*10)) 秒)"; break; }
  sleep 10
done

# 服务是原生壳在启动时拉起的(main.swift 里 bash desktron/bin/desktron ui)。
# 只 pkill 掉服务的话,壳还活着,没人会把它重新拉起来——回来看到的是一个没有后端的空壳;
# 而 open -a 对已在运行的 app 只是把窗口带到前台,同样不会重起服务。
# 所以连壳一起退,再 open,让它按原路重新拉一遍。
echo "重启 Desk…"
pkill -f "Applications/.*\.app/Contents/MacOS/desktron" 2>/dev/null
pkill -f "lib/ui_server.py" 2>/dev/null
sleep 3
open -a "$HOME/Applications/Desktron.app" 2>/dev/null \
  || (cd "$ROOT" && nohup ./desktron/bin/desktron ui >/dev/null 2>&1 &)

echo "等服务起来…"
for i in $(seq 1 60); do
  curl -sf -o /dev/null "$BASE/config" && { echo "已就绪(约 $((i*2)) 秒)"; break; }
  sleep 2
done

echo "核验…"
python3 "$ROOT/scripts/check_session_features.py" | tee "$OUT"
echo "结果已写入 $OUT"
