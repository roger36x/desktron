#!/usr/bin/env bash
# 统一回归入口。日常只跑直接影响；模块收尾扩大邻接；合并与发布各执行一次完整门禁。
set -euo pipefail
cd "$(dirname "$0")/../.."
mode="${1:-full}"
case "$mode" in
  quick)   args=(--changed) ;;
  module)  args=(--module) ;;
  full)    args=() ;;
  release) args=() ;;
  desktop) args=(--desktop) ;;
  *) echo "用法: $0 {quick|module|full|release|desktop}" >&2; exit 2 ;;
esac

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "GATE FAIL: Python ≥3.11 必需；请先准备 Desk 托管运行时。" >&2; exit 1;
}
if [[ "$mode" == full || "$mode" == release ]]; then
  python3 scripts/check_public_tree.py --reviewed || { echo "GATE FAIL: 公开树审核"; exit 1; }
fi
python3 scripts/check_frontend.py >/dev/null || { echo "GATE FAIL: 前端静态检查"; exit 1; }
python3 desktron/lib/provision/install_check.py --root "$PWD" || {
  echo "GATE FAIL: 隔离后端启动验收" >&2; exit 1;
}
if ((${#args[@]})); then
  python3 scripts/dev/run-tests.py "${args[@]}"
else
  python3 scripts/dev/run-tests.py
fi
if [[ "$mode" == release ]]; then
  python3 scripts/dev/run-tests.py --native
  python3 scripts/dev/run-tests.py --desktop
fi
echo "GATE PASS ($mode)"
