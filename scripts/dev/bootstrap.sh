#!/usr/bin/env bash
# desktron 匿名 curl 安装引导(静态托管,curl 不带鉴权即可下)。
#   curl -fsSL <read-base>/install | bash
# 干的事:按架构下对应 tarball → 解包 → 跑里面的 install.sh(同步 payload、装 .app、打开首次设置)。
# 版本、架构、校验和都由 release 在发布时注入。bootstrap 与包是同一批产物，不在用户机上
# 解析 release.json：干净 Mac 不一定有 Python，sed 也不能可靠解析任意合法 JSON。
set -euo pipefail

BASE="__READ_BASE__"
ARCH="$(uname -m)"   # macOS: arm64 / x86_64
RELEASE_REV="__RELEASE_REV__"
RELEASE_CHANNEL="__RELEASE_CHANNEL__"
RELEASE_EXPIRES_AT="__RELEASE_EXPIRES_AT__"
ARM64_FILE="__ARM64_FILE__"
ARM64_SHA256="__ARM64_SHA256__"
X86_64_FILE="__X86_64_FILE__"
X86_64_SHA256="__X86_64_SHA256__"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 带重试:CDN 首次回源未命中时会返回 404(正文其实是完整的二进制,实测 2026-09-03),
# 重试即 200。不重试的话第一个装的人会莫名失败,而那个 404 指向完全错误的方向
# ——他会以为包没发出去。
_get() {
  for i in 1 2 3; do
    curl -fsSL "$1" -o "$2" && return 0
    [ $i -lt 3 ] && { echo "  第 $i 次没取到,2 秒后重试…" >&2; sleep 2; }
  done
  return 1
}
# 这些字段必须由发布器完整注入。拿不到可信校验和时失败关闭，不静默退回无校验安装。
case "$ARCH" in
  arm64)  TARBALL="$ARM64_FILE";  WANT_SHA="$ARM64_SHA256" ;;
  x86_64) TARBALL="$X86_64_FILE"; WANT_SHA="$X86_64_SHA256" ;;
  *) echo "暂不支持这台机器的架构：$ARCH（支持 arm64 / x86_64）。" >&2; exit 1 ;;
esac
case "$BASE$RELEASE_REV$TARBALL$WANT_SHA" in
  *"__"*) echo "安装入口没有完成发布注入，请联系发布者重新发布。" >&2; exit 1 ;;
esac
if [ -z "$TARBALL" ] || [ -z "$WANT_SHA" ]; then
  echo "这一版没有发布 $ARCH 的包。" >&2
  exit 1
fi
echo "  版本 $(printf '%s' "$RELEASE_REV" | cut -c1-12) · 发布通道 $RELEASE_CHANNEL"

# 过期提醒不依赖 Python。macOS 用 BSD date；Linux 兼容 GNU date。
if [ -n "$RELEASE_EXPIRES_AT" ]; then
  _e="$(date -j -u -f "%Y-%m-%dT%H:%M:%SZ" "$RELEASE_EXPIRES_AT" "+%s" 2>/dev/null \
    || date -u -d "$RELEASE_EXPIRES_AT" "+%s" 2>/dev/null || true)"
  if [ -n "$_e" ]; then
    _left=$(( (_e - $(date -u "+%s")) / 86400 ))
    if [ "$_left" -lt 0 ]; then
      echo "  ⚠ 这个发布地址已于 $RELEASE_EXPIRES_AT 过期,装到的可能不是最新的。" >&2
    elif [ "$_left" -lt 30 ]; then
      echo "  ⚠ 这个发布地址还有 $_left 天到期($RELEASE_EXPIRES_AT),提醒发布者续期。" >&2
    fi
  fi
fi

echo "→ 下载 $TARBALL …"
if ! _get "$BASE/$TARBALL" "$TMP/pkg.tgz"; then
  echo "下载失败:$BASE/$TARBALL" >&2
  echo "(发布的 .app 是 universal,arm64/x86_64 都能跑;下不到多半是没发过这个架构的包)" >&2
  exit 1
fi
# 校验:CDN 回源、代理、传输截断都可能给出一个能下完却不完整的文件。
if command -v shasum >/dev/null 2>&1; then
  GOT_SHA="$(shasum -a 256 "$TMP/pkg.tgz" | awk '{print $1}')"
elif command -v sha256sum >/dev/null 2>&1; then
  GOT_SHA="$(sha256sum "$TMP/pkg.tgz" | awk '{print $1}')"
else
  echo "这台机器没有 SHA-256 校验工具，安装已停止。" >&2
  exit 1
fi
if [ "$GOT_SHA" != "$WANT_SHA" ]; then
  echo "校验失败:下载的包与发布信息对不上。" >&2
  echo "  期望 $WANT_SHA" >&2
  echo "  实得 $GOT_SHA" >&2
  echo "(多半是发布切换或传输出了问题,稍后重跑;反复不一致请联系发布者)" >&2
  exit 1
fi
echo "  校验通过 ${GOT_SHA:0:16}…"
tar xzf "$TMP/pkg.tgz" -C "$TMP"
DIR="$(find "$TMP" -maxdepth 1 -type d -name 'desktron-*' | head -1)"
[ -n "$DIR" ] || { echo "解包异常:未找到 desktron-* 目录" >&2; exit 1; }
bash "$DIR/install.sh" </dev/null
