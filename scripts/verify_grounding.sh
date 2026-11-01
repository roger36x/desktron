#!/bin/bash
# 重启后确认新注入路径生效。全部只读，跑完即知成败。
WS="$HOME/.local/share/desktron/workspace"
cd "$WS" || exit 1
ok=0; bad=0
chk(){ if eval "$2" >/dev/null 2>&1; then echo "  ok   $1"; ok=$((ok+1)); else echo "  FAIL $1"; bad=$((bad+1)); fi; }
echo "== 注入路径"
chk "适配器已刷新注入标记" "[ -f .workbench/grounding-injected ] && [ \$(( \$(date +%s) - \$(stat -f %m .workbench/grounding-injected) )) -lt 86400 ]"
chk "合成文件已停写并清掉" "[ ! -f domains/desk-iter/AGENTS.md ] && [ ! -f domains/desk-iter/CLAUDE.md ]"
echo "== 取用路径"
for c in "list --tier notes" "search grounding" "graph neighbors luo-jie" "show --id user-profile"; do
  chk "mem_read $c" "python3 .claude/skills/memory/scripts/mem_read.py $c | head -1 | grep -q ."
done
chk "会话 env 带数据根" "ps -axo command | grep -q '[u]i_server.py'"
echo "== 结果: $ok 通过 / $bad 失败"
[ $bad -eq 0 ] && echo "新注入路径已生效，无需其他操作。" || echo "有失败项：设 DESKTRON_GROUNDING_INJECTED=0 可强制退回旧路，把失败项发我。"
