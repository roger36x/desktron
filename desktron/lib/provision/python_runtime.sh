#!/usr/bin/env bash
# 安装器与启动器共用的托管 Python；不依赖系统 Python，不改登录 shell。
DESK_PYTHON_VERSION="3.12.14"
DESK_PYTHON_TAG="20260814"
DESK_PYTHON_REQUIREMENTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/python-requirements.txt"

desk_python_platform() {
  case "$(uname -s)" in
    Darwin) DESK_PYTHON_OS="darwin"; DESK_PYTHON_TRIPLE="apple-darwin" ;;
    Linux) DESK_PYTHON_OS="linux"; DESK_PYTHON_TRIPLE="unknown-linux-gnu" ;;
    *) echo "Desk 暂不支持此平台的托管 Python。" >&2; return 1 ;;
  esac
  case "$(uname -m)" in
    arm64|aarch64) DESK_PYTHON_ARCH="arm64"; DESK_PYTHON_DOWNLOAD_ARCH="aarch64" ;;
    x86_64|amd64) DESK_PYTHON_ARCH="x64"; DESK_PYTHON_DOWNLOAD_ARCH="x86_64" ;;
    *) echo "Desk 暂不支持此架构的托管 Python。" >&2; return 1 ;;
  esac
  DESK_PYTHON_ROOT="$(dirname "${DESKTRON_DATA_HOME:-$HOME/.local/share/desktron/data}")/runtime"
  DESK_PYTHON_HOME="$DESK_PYTHON_ROOT/python-$DESK_PYTHON_VERSION-$DESK_PYTHON_OS-$DESK_PYTHON_ARCH"
}

desk_python_check() {
  [[ -x "$1" ]] || return 1
  "$1" -c 'import platform, sys
arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(platform.machine().lower())
sys.exit(0 if sys.version_info >= (3, 11) and arch == sys.argv[1] else 1)' "$DESK_PYTHON_ARCH" >/dev/null 2>&1
}

desk_python_download() {
  if curl --retry 3 --retry-delay 2 -fsSL "$1" -o "$2"; then return 0; fi
  echo "Desk Python 下载失败；未完成运行时准备。" >&2
  [[ -n "${DESKTRON_SETUP_PROXY:-}" ]] || return 1
  echo "使用显式配置的备用代理重试。" >&2
  env http_proxy="$DESKTRON_SETUP_PROXY" https_proxy="$DESKTRON_SETUP_PROXY" \
    HTTP_PROXY="$DESKTRON_SETUP_PROXY" HTTPS_PROXY="$DESKTRON_SETUP_PROXY" \
    curl --retry 3 --retry-delay 2 -fsSL "$1" -o "$2"
}

desk_python_install() (
  mkdir -p "$DESK_PYTHON_ROOT" || exit 1
  # 已在子 shell 隔离；EXIT trap 在 macOS Bash 3.2 中不能依赖函数 local。
  lock="$DESK_PYTHON_ROOT/python-install.lockdir"
  work=""
  attempt=0
  for ((attempt=0; attempt<90; attempt++)); do
    mkdir "$lock" 2>/dev/null && break
    sleep 1
  done
  if [[ $attempt -eq 90 ]]; then
    echo "Python 安装锁等待超时；请核对其他安装的结果，不要重复覆盖运行时。" >&2
    exit 1
  fi
  trap '[[ -z "$work" ]] || rm -rf "$work"; rmdir "$lock" 2>/dev/null || true' EXIT
  desk_python_check "$DESK_PYTHON_HOME/bin/python3" && exit 0
  work="$(mktemp -d "$DESK_PYTHON_ROOT/python-setup.XXXXXX")" || exit 1
  local name="cpython-$DESK_PYTHON_VERSION+$DESK_PYTHON_TAG-$DESK_PYTHON_DOWNLOAD_ARCH-$DESK_PYTHON_TRIPLE-install_only.tar.gz"
  local base="https://github.com/astral-sh/python-build-standalone/releases/download/$DESK_PYTHON_TAG"
  local expected actual count
  echo "→ 准备 Desk 托管 Python $DESK_PYTHON_VERSION ($DESK_PYTHON_ARCH)" >&2
  desk_python_download "$base/${name/+/%2B}" "$work/python.tgz" || exit 1
  desk_python_download "$base/SHA256SUMS" "$work/SHA256SUMS" || exit 1
  count="$(awk -v name="$name" '$2 == name { n++ } END { print n+0 }' "$work/SHA256SUMS")"
  expected="$(awk -v name="$name" '$2 == name { print $1 }' "$work/SHA256SUMS")"
  if command -v shasum >/dev/null 2>&1; then
    actual="$(shasum -a 256 "$work/python.tgz" | awk '{print $1}')"
  else
    actual="$(sha256sum "$work/python.tgz" | awk '{print $1}')"
  fi
  if [[ "$count" != 1 || ! "$expected" =~ ^[0-9a-f]{64}$ || "$expected" != "$actual" ]]; then
    echo "Python 安装包校验失败；未解包或替换运行时。" >&2; exit 1
  fi
  tar xzf "$work/python.tgz" -C "$work" || exit 1
  desk_python_check "$work/python/bin/python3" || {
    echo "下载的 Python 版本或架构不合格；保留原运行时。" >&2; exit 1;
  }
  if [[ -e "$DESK_PYTHON_HOME" ]]; then
    mv "$DESK_PYTHON_HOME" "$work/previous" || exit 1
  fi
  if ! mv "$work/python" "$DESK_PYTHON_HOME"; then
    if [[ -e "$work/previous" ]] && ! mv "$work/previous" "$DESK_PYTHON_HOME"; then
      echo "Python 替换与恢复均失败；原目录保留于 $work/previous" >&2
      work=""  # 不删除唯一恢复副本。
    fi
    exit 1
  fi
  if [[ "$DESK_PYTHON_OS" == darwin ]]; then
    xattr -dr com.apple.quarantine "$DESK_PYTHON_HOME" 2>/dev/null || true
  fi
)

desk_python_dependencies() {
  if "$DESK_PYTHON_HOME/bin/python3" -c 'import yaml, PIL; assert yaml.__version__ == "6.0.3" and PIL.__version__ == "12.1.1"' >/dev/null 2>&1; then
    return 0
  fi
  echo "→ 准备 Desk Python 运行依赖(PyYAML 6.0.3、Pillow 12.1.1)" >&2
  if ! PIP_CONFIG_FILE=/dev/null "$DESK_PYTHON_HOME/bin/python3" -m pip --isolated \
    --disable-pip-version-check install --no-input --requirement "$DESK_PYTHON_REQUIREMENTS"; then
    echo "Desk Python 运行依赖准备失败；安装未完成，请检查网络后重试。" >&2
    return 1
  fi
  "$DESK_PYTHON_HOME/bin/python3" -c 'import yaml, PIL; assert yaml.__version__ == "6.0.3" and PIL.__version__ == "12.1.1"'
}

desk_python_ensure() {
  desk_python_platform || return 1
  desk_python_check "$DESK_PYTHON_HOME/bin/python3" || desk_python_install || return 1
  desk_python_dependencies || return 1
  export PATH="$DESK_PYTHON_HOME/bin:$PATH"
}

desk_python_select() {
  desk_python_platform || return 1
  if desk_python_check "$DESK_PYTHON_HOME/bin/python3"; then
    export PATH="$DESK_PYTHON_HOME/bin:$PATH"
    return 0
  fi
  # 源码开发允许显式选用本机合格解释器；发行安装始终使用固定版本槽。
  if [[ -d "$PROJECT_ROOT/.git" || -f "$PROJECT_ROOT/.git" ]]; then
    desk_python_check "$(command -v python3)" && return 0
  fi
  echo "Desk 需要原生 Python ≥3.11 的托管运行时；请重新运行发行安装器，源码开发可运行 desktron prepare。" >&2
  return 1
}
