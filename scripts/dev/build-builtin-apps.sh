#!/usr/bin/env bash
# 构建随 Desktron 发行的 Excalidraw 与 Monaco IDE 静态资产。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_ROOT="$ROOT/frontend/app-assets"
OUT_ROOT="${1:-$APP_ROOT}"

command -v node >/dev/null 2>&1 || { echo "缺少 node，无法构建内置 App" >&2; exit 1; }
command -v npm >/dev/null 2>&1 || { echo "缺少 npm，无法构建内置 App" >&2; exit 1; }

if [ ! -d "$APP_ROOT/node_modules/@excalidraw/excalidraw" ] || \
   [ ! -d "$APP_ROOT/node_modules/monaco-editor" ] || \
   [ ! -d "$APP_ROOT/node_modules/esbuild" ]; then
  echo "→ 安装内置 App 的锁定构建依赖"
  # 必须 cd 进去跑,不能用 --prefix:npm 11 下 --prefix 只改安装落点,package.json 仍按
  # 当前目录解析,于是把目录名当成根包名,报 `Missing: app-assets@1.0.0 from lock file`
  # 而中止——全新克隆按文档构建会当场失败(2026-09-01 干净克隆实测)。
  ( cd "$APP_ROOT" && npm ci --ignore-scripts )
fi

EXCALIDRAW_OUT="$OUT_ROOT/excalidraw/vendor"
FILES_OUT="$OUT_ROOT/files/vendor"
rm -rf "$EXCALIDRAW_OUT" "$FILES_OUT"
mkdir -p "$EXCALIDRAW_OUT" "$FILES_OUT"

node "$APP_ROOT/excalidraw/src/build.mjs" "$EXCALIDRAW_OUT"
node "$APP_ROOT/files/src/build.mjs" "$FILES_OUT"

for rel in \
  excalidraw/vendor/app.bundle.js \
  excalidraw/vendor/index.css \
  excalidraw/vendor/fonts/Virgil/Virgil-Regular.woff2 \
  files/vendor/app.bundle.js \
  files/vendor/app.bundle.css \
  files/vendor/codicon.ttf \
  files/vendor/editor.worker.js \
  files/vendor/json.worker.js \
  files/vendor/css.worker.js \
  files/vendor/html.worker.js \
  files/vendor/ts.worker.js; do
  if [ ! -s "$OUT_ROOT/$rel" ]; then
    echo "内置 App 构建产物缺失或为空: $rel" >&2
    exit 1
  fi
done
shopt -s nullglob
_zh_cn_locales=("$EXCALIDRAW_OUT"/locales/zh-CN-*.js)
if ((${#_zh_cn_locales[@]} == 0)); then
  echo "内置 App 构建产物缺失: excalidraw/vendor/locales/zh-CN-*.js" >&2
  exit 1
fi
echo "→ 2 个内置 App 的静态资产已就绪"
