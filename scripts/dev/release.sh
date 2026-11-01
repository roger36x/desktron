#!/usr/bin/env bash
# desktron release — 产出可分发 tarball:payload(仓库副本)+ 预编译 .app + install.sh。
# 用户侧:解开 → 跑 install.sh → 同步 payload 到规范家目录 + 装 .app + 装机(依赖/引擎/skills/工作区)。
# 预编译 .app 烤的是构建机路径,运行时自定位到规范家目录(见 main.swift repoRoot)。
# .app 按当前机器架构编译，并在打包、安装和更新边界验证稳定证书签名。
set -euo pipefail

# 默认只产本地 tarball;--site <base-url> 额外生成可静态托管的安装目录(不上传)。
SITE_BASE=""
case "${1:-}" in
  "") ;;
  --site)
    SITE_BASE="${2:-}"
    [ -n "$SITE_BASE" ] || { echo "release: --site 需要托管地址,例如 --site https://example.com/desktron" >&2; exit 2; } ;;
  *) echo "release: 未知参数 $1(可用:--site <base-url>)" >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # scripts/dev/ → 仓库根(上两级)
EXC="$ROOT/scripts/payload-excludes.txt"                     # 排除清单留 scripts/(随 payload,记录边界)

# bootstrap 是发布产物的一部分。把本次发布的可信字段直接烘进去，用户机无需安装 JSON
# 解析器，也不会因 release.json 的排版变化而选错架构或跳过校验。
render_bootstrap() { # output base rev channel expires arm-file arm-sha x86-file x86-sha
  local output="$1" base="$2" rev="$3" channel="$4" expires="$5"
  local arm_file="$6" arm_sha="$7" x86_file="$8" x86_sha="$9"
  sed \
    -e "s#__READ_BASE__#$base#g" \
    -e "s#__RELEASE_REV__#$rev#g" \
    -e "s#__RELEASE_CHANNEL__#$channel#g" \
    -e "s#__RELEASE_EXPIRES_AT__#$expires#g" \
    -e "s#__ARM64_FILE__#$arm_file#g" \
    -e "s#__ARM64_SHA256__#$arm_sha#g" \
    -e "s#__X86_64_FILE__#$x86_file#g" \
    -e "s#__X86_64_SHA256__#$x86_sha#g" \
    "$ROOT/scripts/dev/bootstrap.sh" > "$output"
  if grep -qE '__[A-Z0-9_]+__' "$output"; then
    echo "release: bootstrap 仍有未注入字段，拒绝发布。" >&2
    return 1
  fi
}

if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
  echo "发布被拒绝:工作树含未提交改动。先完成评审并形成干净快照。" >&2
  exit 2
fi
# 默认本地产物也属于交付快照：先从锁定依赖重建，再运行离线全量门禁。
# 真实引擎请求仍由独立的 release 门禁显式授权执行，不在构建时偷偷发送。
bash "$ROOT/scripts/dev/build-builtin-apps.sh"
bash "$ROOT/scripts/dev/refactor-gate.sh" full
VER="$(sed -n 's/^version:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' "$ROOT/manifest.yaml" | head -1)"
_release_rev="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
# 构建序号只承担发布先后排序，不能用提交数量：分支切换或历史整理会让计数倒退。
# 流水线可显式给定；本地发布默认使用 UTC 秒级时间戳。
_build_number="${DESKTRON_BUILD_NUMBER:-$(date -u '+%Y%m%d%H%M%S')}"
case "$_build_number" in *[!0-9]*|'') echo "DESKTRON_BUILD_NUMBER 必须是正整数" >&2; exit 2 ;; esac
STAMP="$(date +%Y%m%d-%H%M)"
NAME="desktron-v${VER:-0}-${STAMP}"
WORK="$(mktemp -d)"
STAGE="$WORK/$NAME"
mkdir -p "$STAGE/payload"

echo "[1/4] 编译 .app(暂存,不碰运行中的 app)…"
DESKTRON_APPDIR="$STAGE/Desktron.app" DESKTRON_APP_REV="$_release_rev" \
  DESKTRON_APP_BUILD="$_build_number" DESKTRON_APP_VERSION="${VER:-0}" \
  bash "$ROOT/desktron/bin/desktron" app-install swift >/dev/null
python3 "$ROOT/desktron/lib/macos_codesign.py" verify "$STAGE/Desktron.app" >/dev/null

echo "[2/4] 暂存仓库 payload(排除见 payload-excludes.txt)…"
# 发行只接受 Git 看得见的源码：已跟踪文件 + 正常待提交文件。这样即使开发机的
# .gitignore 里有会话转储、本机 skill 软链或工作台状态，rsync 也没有机会把它们带走。
# 先过滤当前工作树里已经不存在的 tracked 路径，兼容 macOS 自带的旧版 rsync。
FILES="$WORK/payload-files"
while IFS= read -r -d '' rel; do
  if [ -e "$ROOT/$rel" ] || [ -L "$ROOT/$rel" ]; then
    printf '%s\0' "$rel"
  fi
done < <(git -C "$ROOT" ls-files --cached --others --exclude-standard -z) > "$FILES"
# 分两步走,不能把 --files-from 和 --exclude-from 写在同一条 rsync 上:清单里显式列出的
# 文件是「点名传输」,rsync 不再拿排除规则去筛它们。于是 payload-excludes.txt 里那些
# 目录级规则(/docs、/knowledge 等)对本次传输全部落空,而这一点从表面看不出来——
# 包照打、脚本照过,只是每次都多带一份不该发的东西。docs/ 长期只有 1.6MB,这个洞就一直
# 没露头;2026-09-09 文档重组让它涨到 22MB,发行包才从 23MB 跳到 39MB、超过静态托管的
# 50MiB 上限(实测:最小复现里写着 /docs,docs/a.md 照样被拷过去)。
#
# 第一步只解决「发行只接受 Git 看得见的源码」,第二步才是排除规则——递归传输那一路
# --exclude-from 的语义是正常的。多一次本机拷贝(40MB 级)换规则真的生效,值。
RAW="$WORK/payload-raw"
mkdir -p "$RAW"
rsync -a --from0 --files-from="$FILES" "$ROOT/" "$RAW/"
rsync -a --exclude-from="$EXC" "$RAW/" "$STAGE/payload/"
rm -rf "$RAW"

# 官方 App 必须由锁定依赖在干净源码上可重建，不能依赖发布机碰巧残留的 ignored vendor/。
echo "  构建官方 App 静态资产…"
bash "$ROOT/scripts/dev/build-builtin-apps.sh" "$STAGE/payload/frontend/app-assets"

# payload 卫生检查:Desk 文档扫描 PII/凭证模式；运行态 knowledge 不得进入发行包。
echo "  payload 卫生检查…"
PAYLOAD_DIRTY=false
# PII / 凭证正则(和 curator rules.yaml 的 forbidden_in_body 正则段对齐)
while IFS= read -r hit; do
  echo "  ⚠ PII/凭证: $hit"
  PAYLOAD_DIRTY=true
done < <(grep -rn -E 'ou_[a-z0-9]{10,}|oc_[a-z0-9]{10,}|\bt-[a-zA-Z0-9_]{16,}\b' \
  "$STAGE/payload/desk-docs/" 2>/dev/null || true)
if [ -e "$STAGE/payload/knowledge" ]; then
  echo "  ⚠ 运行态 knowledge/ 泄漏进 payload"
  PAYLOAD_DIRTY=true
fi
if $PAYLOAD_DIRTY; then
  echo "payload 卫生检查未通过,中止 release。修复后重试。" >&2
  rm -rf "$WORK"; exit 1
fi
echo "  payload 卫生检查通过"

# 提交号写进包:tarball 解开之后没有 .git,装的时候查不到自己是哪一版。
# 原来靠装机时 git rev-parse 取,只在「从 git clone 装」那条路上有效,而 curl 装机
# 恰恰不是那条路——查不到还罢了,它在 set -e 下直接让脚本以 128 退出(实测),
# 于是文件都装好了、人看到的却是失败(2026-09-03 另一会话 review 指出)。
printf '%s\n' "$_release_rev" > "$STAGE/payload/.release-rev"
printf '%s\n' "$_build_number" > "$STAGE/payload/.release-build"
echo "[3/4] 写 install.sh …"
cat > "$STAGE/install.sh" <<'INSTALL'
#!/usr/bin/env bash
# desktron 装机:同步 payload 到规范家目录 → 装 .app → 准备工作区。
# Python 与 Node 自动准备；AI 引擎与工作工具在应用内按引导处理。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/share/desktron/home"

# 开箱即用:把 ~/.local/bin 并入当前进程 PATH,并持久化到登录 shell 配置(claude/codex 装到这里)。
ensure_local_bin_on_path() {
  local bin="$HOME/.local/bin"
  case ":$PATH:" in *":$bin:"*) ;; *) export PATH="$bin:$PATH";; esac
  local rc
  case "${SHELL:-}" in
    *zsh)  rc="$HOME/.zshrc" ;;
    *bash) rc="$HOME/.bash_profile" ;;
    *)     rc="$HOME/.profile" ;;
  esac
  if ! grep -qsF '.local/bin' "$rc" 2>/dev/null; then
    printf '\n# desktron:引擎命令落地处\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
    echo "→ 已把 ~/.local/bin 写入 $rc(新开终端生效)"
  fi
}
ensure_local_bin_on_path

# 不接受系统 Python 冒充安装运行时；安装器与启动器共用固定版本槽及架构检查。
ensure_python() {
  source "$HERE/payload/desktron/lib/provision/python_runtime.sh"
  desk_python_ensure
}
ensure_python

_PLATFORM="$(uname -s)"
_SIGN_TOOL="$HERE/payload/desktron/lib/macos_codesign.py"
_APP_SOURCE="$HERE/Desktron.app"
_APP_TARGET="$HOME/Applications/Desktron.app"
_APP_LOWER="$HOME/Applications/desktron.app"
_APP_NEW="$HOME/Applications/.Desktron.app.new"
_APP_PREV="$HOME/Applications/.Desktron.app.prev"
_MARKER_SIGNATURE="$HOME/.local/share/desktron/.codesign-requirement"
_CURRENT_APP=""
if [ "$_PLATFORM" = Darwin ]; then
  echo "→ 预检桌面应用稳定签名"
  mkdir -p "$HOME/Applications"
  [ -e "$_APP_TARGET" ] && _CURRENT_APP="$_APP_TARGET"
  [ -z "$_CURRENT_APP" ] && [ -e "$_APP_LOWER" ] && _CURRENT_APP="$_APP_LOWER"
  python3 "$_SIGN_TOOL" verify-compatible "$_APP_SOURCE" "$_CURRENT_APP" \
    --requirement-marker "$_MARKER_SIGNATURE"
  rm -rf "$_APP_NEW"
  if ! cp -R "$_APP_SOURCE" "$_APP_NEW"; then
    echo "拷贝 .app 失败，尚未改动现有安装。" >&2
    rm -rf "$_APP_NEW"; exit 1
  fi
  if ! python3 "$_SIGN_TOOL" verify-compatible "$_APP_NEW" "$_CURRENT_APP" \
      --requirement-marker "$_MARKER_SIGNATURE"; then
    echo "桌面应用复制后签名无效或身份不连续，尚未改动现有安装。" >&2
    rm -rf "$_APP_NEW"; exit 1
  fi
fi

mkdir -p "$HOME/.local/share/desktron"
_TXN="$(mktemp -d "$HOME/.local/share/desktron/.install-txn.XXXXXX")"
BAK="$HOME/.local/share/desktron/home.prev"
OLD="$HOME/.local/share/desktron/app"
_MARKER_REV="$HOME/.local/share/desktron/.update-rev"
_MARKER_BUILD="$HOME/.local/share/desktron/.update-build"
_committed=false
_home_touched=false
_home_backup=false
_migrated_old=false
_app_old_moved=false
_app_new_moved=false
for _marker in rev build signature; do
  eval "_path=\$_MARKER_$(printf '%s' "$_marker" | tr '[:lower:]' '[:upper:]')"
  if [ -e "$_path" ]; then
    cp -p "$_path" "$_TXN/marker.$_marker"
  else
    : > "$_TXN/marker.$_marker.absent"
  fi
done
rollback_install() {
  _status=$?
  trap - EXIT INT TERM
  set +e
  if [ "$_committed" != true ]; then
    echo "→ 安装未完成，恢复原 payload、App 与版本标记…" >&2
    if [ "$_app_new_moved" = true ]; then
      rm -rf "$_APP_TARGET"
    fi
    if [ "$_app_old_moved" = true ] && [ -e "$_APP_PREV" ]; then
      mv "$_APP_PREV" "$_APP_TARGET"
    fi
    if [ "$_home_touched" = true ]; then
      rm -rf "$DEST"
      [ "$_home_backup" = true ] && [ -d "$BAK" ] && mv "$BAK" "$DEST"
    fi
    if [ "$_migrated_old" = true ] && [ ! -e "$OLD" ]; then
      [ -d "$DEST" ] || mkdir -p "$DEST"
      mv "$DEST" "$OLD"
    fi
    for _marker in rev build signature; do
      eval "_path=\$_MARKER_$(printf '%s' "$_marker" | tr '[:lower:]' '[:upper:]')"
      if [ -e "$_TXN/marker.$_marker" ]; then
        cp -p "$_TXN/marker.$_marker" "$_path"
      else
        rm -f "$_path"
      fi
    done
  fi
  rm -rf "$_APP_NEW" "$_TXN"
  exit "$_status"
}
trap rollback_install EXIT
trap 'exit 130' INT TERM

# 旧版安装在 app/,迁移到 home/(mv 比 cp+rm 快,保留 inode)。迁移也属于本次事务。
if [ -d "$OLD" ] && [ ! -d "$DEST" ]; then
  mv "$OLD" "$DEST"
  _migrated_old=true
  echo "→ 迁移 app/ → home/"
fi
# 先备份再覆盖。备份失败必须停止；否则后续 App 或装机验收失败时无从恢复。
echo "→ 同步 payload 到 $DEST"
mkdir -p "$DEST"
if [ -n "$(ls -A "$DEST" 2>/dev/null)" ]; then
  _bn="$BAK.new"
  rm -rf "$_bn"
  if ! cp -a "$DEST" "$_bn"; then
    rm -rf "$_bn"
    echo "无法完整备份当前 payload，拒绝覆盖。" >&2
    exit 1
  fi
  rm -rf "$BAK"
  mv "$_bn" "$BAK"
  _home_backup=true
fi
_home_touched=true
rsync -a --delete "$HERE/payload/" "$DEST/"

# .app 是 macOS 客户端外壳。候选已在 payload 写入前复制并验签；这里用三目录切换，
# 后续 prepare/install-check 任一步失败都会由 EXIT trap 恢复旧 App 与 payload。
if [ "$_PLATFORM" = Darwin ]; then
  echo "→ 装 .app 到 ~/Applications"
  rm -rf "$_APP_PREV"
  if [ -e "$_APP_TARGET" ]; then
    mv "$_APP_TARGET" "$_APP_PREV"
    _app_old_moved=true
  fi
  mv "$_APP_NEW" "$_APP_TARGET"
  _app_new_moved=true
  _compat_current="$_CURRENT_APP"
  [ -e "$_APP_PREV" ] && _compat_current="$_APP_PREV"
  python3 "$DEST/desktron/lib/macos_codesign.py" verify-compatible \
    "$_APP_TARGET" "$_compat_current" --requirement-marker "$_MARKER_SIGNATURE"
  xattr -dr com.apple.quarantine "$_APP_TARGET" 2>/dev/null || true
  python3 "$DEST/desktron/lib/macos_codesign.py" verify-compatible \
    "$_APP_TARGET" "$_compat_current" --requirement-marker "$_MARKER_SIGNATURE"
else
  echo "→ 非 macOS:跳过 .app(这一份只跑后端)"
fi
echo "→ 准备 Desk 工作区"
bash "$DEST/desktron/bin/desktron" prepare
echo "→ 隔离验收后端启动与 Shepherd 状态(不发送模型请求)"
bash "$DEST/desktron/bin/desktron" install-check \
  --report "$(dirname "${DESKTRON_DATA_HOME:-$HOME/.local/share/desktron/data}")/install-check.json"
if [ "$_PLATFORM" = Darwin ]; then
  python3 "$DEST/desktron/lib/macos_codesign.py" record-requirement \
    "$_APP_TARGET" "$_MARKER_SIGNATURE"
fi
# 记版本标记:供 app 内 update-check 判断是否最新。无法取得值时保留旧标记；
# 已取得值却无法写入则让事务失败，避免文件已切换而版本仍指向旧安装。
_rev=""
[ -f "$HERE/payload/.release-rev" ] && _rev="$(cat "$HERE/payload/.release-rev" 2>/dev/null || true)"
[ -z "$_rev" ] && _rev="$(git -C "$HERE" rev-parse HEAD 2>/dev/null || true)"
if [ -n "$_rev" ]; then printf '%s\n' "$_rev" > "$_MARKER_REV"; fi
_build=""
[ -f "$HERE/payload/.release-build" ] && _build="$(cat "$HERE/payload/.release-build" 2>/dev/null || true)"
if [ -n "$_build" ]; then printf '%s\n' "$_build" > "$_MARKER_BUILD"; fi

# 从这里起才允许清理旧 App；EXIT trap 此后只清理临时候选，不再回滚。
_committed=true
if [ "$_PLATFORM" = Darwin ]; then
  rm -rf "$_APP_PREV"
  # 默认 APFS 不区分大小写，旧小写名可能就是刚装好的目标，不能删除同一对象。
  if [ -e "$_APP_LOWER" ] && ! [ "$_APP_LOWER" -ef "$_APP_TARGET" ]; then
    rm -rf "$_APP_LOWER"
  fi
fi
echo
if [ "$_PLATFORM" = Darwin ]; then
  echo "完成。正在打开 Desktron，请按页面引导完成配置。"
  open "$HOME/Applications/Desktron.app" 2>/dev/null || \
    echo "请双击 ~/Applications/Open\ Desk.app 启动。"
else
  echo "完成(后端已就位,未装客户端外壳)。"
  echo "常驻与对外开放接着跑:bash $DEST/scripts/install-remote.sh"
fi
INSTALL
chmod +x "$STAGE/install.sh"

echo "[4/4] 打 tarball …"
mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/${NAME}.tgz"
# macOS 扩展属性会生成额外的 ._* 顶层成员，破坏更新器的单目录契约。
COPYFILE_DISABLE=1 tar --no-xattrs --exclude=.DS_Store '--exclude=._*' -czf "$OUT" -C "$WORK" "$NAME"
# 用客户端更新器实际解包一次，确保发行归档满足客户端契约后再上传。
python3 - "$ROOT/desktron/lib" "$OUT" "$WORK/archive-check" <<'PYCHECK'
import os, sys
sys.path.insert(0, sys.argv[1])
from macos_codesign import verify_stable_app
from self_update_apply import safe_extract
root = safe_extract(sys.argv[2], sys.argv[3])
verify_stable_app(os.path.join(root, 'Desktron.app'))
PYCHECK
SIZE="$(du -h "$OUT" | cut -f1)"
echo "产出: $OUT ($SIZE)"
_sha="$(shasum -a 256 "$OUT" | awk '{print $1}')"
_rev="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
_size="$(wc -c < "$OUT" | tr -d ' ')"
_mandatory_after="$(sed -n 's/^mandatory_after:[[:space:]]*"\(.*\)"/\1/p' "$ROOT/manifest.yaml" | head -1)"
_min_supported_build="$(sed -n 's/^min_supported_build:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$ROOT/manifest.yaml" | head -1)"
_update_reason="$(sed -n 's/^update_reason:[[:space:]]*"\(.*\)"/\1/p' "$ROOT/manifest.yaml" | head -1)"
_min_supported_build="${_min_supported_build:-0}"
_mandatory_after_json="$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1],ensure_ascii=False))' "$_mandatory_after")"
_update_reason_json="$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1],ensure_ascii=False))' "$_update_reason")"


# 静态站点输出。--site <base-url> 在 dist/site/ 生成一个可直接托管的目录:
# 按架构命名的 tarball、release.json、注入好读地址的 install 引导脚本和 index.html。
# 本脚本不负责上传;把 dist/site/ 放到任意静态托管(对象存储、GitHub Pages 等)即可,
# 客户端更新器通过 manifest.yaml 的 update_base 或 DESKTRON_UPDATE_BASE 指向同一地址。
if [ -n "$SITE_BASE" ]; then
  RBASE="${SITE_BASE%/}"
  PUB="$ROOT/dist/site"
  rm -rf "$PUB"; mkdir -p "$PUB"
  # 两个架构各发一份。.app 是 universal、payload 是纯脚本与前端不分架构,所以两份内容
  # 完全一样——同一个 sha256,清单里都指得出来。不发的话 Intel 机器按 uname -m 取会 404,
  # 重试三次后报「下载失败」,而那个错误指不出真正的原因(2026-09-03 review 指出)。
  # 发两个名字之前先确认 .app 真的是 universal。app-install 在双架构编译失败时会退回
  # 本机单架构(见 bin/desktron 的 _sw 回落),而这里照样复制成两个名字——Intel 用户
  # 装到的就是个跑不起来的壳,而且看不出问题在哪(2026-09-03 另一会话 review 指出)。
  _bin="$STAGE/Desktron.app/Contents/MacOS/desktron"
  _archs="$(lipo -archs "$_bin" 2>/dev/null || echo "")"
  case " $_archs " in
    *" arm64 "*) ;;
    *) echo "[site] .app 里没有 arm64(实际:${_archs:-读不出}),不该发布。" >&2
       rm -rf "$WORK"; exit 2 ;;
  esac
  case " $_archs " in
    *" x86_64 "*)
      for _a in arm64 x86_64; do cp "$OUT" "$PUB/desktron-latest-$_a.tgz"; done
      _pub_archs='"arm64", "x86_64"' ;;
    *)
      echo "[site] 注意:.app 只有 ${_archs}——只发这一个架构,清单里不登记 x86_64。"
      echo "           (要发 Intel 的话先让 app-install 编出 universal)"
      cp "$OUT" "$PUB/desktron-latest-arm64.tgz"
      _pub_archs='"arm64"' ;;
  esac
  # 发布清单:版本、校验和、架构索引、过期时间集中一处。
  # 没有它的话每一样都要单独打补丁——架构靠拼 uname、版本靠 git、校验没有、过期没人管。
  # 有效期:发布时 365 天。写进清单,装机时临近会提醒,避免托管到期后静默 404。
  _exp="$(date -u -v+365d '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '+365 days' '+%Y-%m-%dT%H:%M:%SZ')"
  cat > "$PUB/release.json" <<JSON
{
  "name": "$NAME",
  "rev": "$_rev",
  "build_number": $_build_number,
  "built_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "expires_at": "$_exp",
  "channel": "site",
  "signed": true,
  "mandatory_after": $_mandatory_after_json,
  "min_supported_build": $_min_supported_build,
  "reason": $_update_reason_json,
  "artifacts": {
$(for _a in $(echo "$_pub_archs" | tr -d '",'); do
    printf '    "%s": { "file": "desktron-latest-%s.tgz", "sha256": "%s", "bytes": %s }' "$_a" "$_a" "$_sha" "$_size"
    [ "$_a" = "$(echo "$_pub_archs" | tr -d '",' | awk '{print $NF}')" ] || printf ',\n'
  done)
  }
}
JSON
  _arm_file="desktron-latest-arm64.tgz"
  _x86_file=""; _x86_sha=""
  case " $_pub_archs " in
    *' "x86_64" '*) _x86_file="desktron-latest-x86_64.tgz"; _x86_sha="$_sha" ;;
  esac
  render_bootstrap "$PUB/install" "$RBASE" "$_rev" "site" "$_exp" \
    "$_arm_file" "$_sha" "$_x86_file" "$_x86_sha"
  # 静态托管通常要求目录里有 index.html;顺带给访问者展示装机命令。
  cat > "$PUB/index.html" <<HTML
<!doctype html><meta charset="utf-8"><title>Desktron</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font:15px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;
max-width:680px;margin:12vh auto;padding:0 24px;color:#1a1a1a}
code{background:#f4f4f5;padding:2px 6px;border-radius:4px;font-size:13.5px}
pre{background:#f4f4f5;padding:14px 16px;border-radius:8px;overflow-x:auto}
.m{color:#6b7280;font-size:13.5px}</style>
<h1>Desktron</h1>
<p>在终端里粘这一行:</p>
<pre><code>curl -fsSL $RBASE/install | bash</code></pre>
<p class="m">装完双击 ~/Applications/Desktron.app 启动,首次打开会有一页设置向导,
按顺序准备运行环境并登录引擎。</p>
<p class="m">构建 $NAME · $(date '+%Y-%m-%d %H:%M')</p>
HTML
  echo "[site] 已生成: $PUB"
  echo "[site] 托管后安装: curl -fsSL $RBASE/install | bash"
fi

rm -rf "$WORK"
echo
echo "用户侧装机:"
echo "  本地包: tar xzf ${NAME}.tgz && cd $NAME && ./install.sh"
