"""凭证与提供方配置的本机存储(设置面板后端)。

写入面:claude/codex 的 key/网关在 SECRETS(env 注入);
绝不碰 repo 分发物与用户全局 ~/.codex、~/.claude。
纯文件 I/O 叶子,不依赖运行态;上层 _settings_get/apply 组合调用本模块。
"""
import os
import re

SECRETS_PATH = os.path.expanduser("~/.config/desktron/env")

# 这份文件是凭证仓，不是进程启动配置。PATH 可能被旧安装写进来；shell source 时
# 它会展开，但 Python 适配器动态重读时只得到带引号的字面量，反而把 systemd 已配置好的
# Node/Python 路径覆盖掉。Desk 的运行目录与会话身份同样不能被这里悄悄改写。
_MANAGED_ENV = frozenset({
    "PATH", "HOME", "PWD", "OLDPWD", "SHELL", "TMPDIR", "TMP", "TEMP",
    "USER", "LOGNAME", "SHLVL", "_", "IFS", "ENV", "BASH_ENV", "CDPATH", "GLOBIGNORE",
    "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
})
_MANAGED_ENV_PREFIXES = ("DESKTRON_", "LD_", "DYLD_")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _secrets_read():
    out = {}
    try:
        with open(SECRETS_PATH, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, _, v = ln.partition("=")
                    out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def secrets_env(base=None):
    """把凭证合入子进程环境，同时保留 Desk 自管的运行时与身份字段。"""
    env = dict(os.environ if base is None else base)
    for key, value in _secrets_read().items():
        if (env.get('DESKTRON_NETWORK_MODE') in ('system', 'direct', 'manual')
                and key.lower() in {'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}):
            continue   # 网络配置在启动时冻结；旧 env 不能覆盖或使新保存的配置提前生效。
        if (_ENV_KEY.fullmatch(key) and key not in _MANAGED_ENV
                and not key.startswith(_MANAGED_ENV_PREFIXES)):
            env[key] = value
    return env


def _secrets_write(updates):
    """合并写:更新/删除指定键(None=删),保留文件里其余行与注释。"""
    lines = []
    try:
        with open(SECRETS_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        pass
    done = set()
    out = []
    for ln in lines:
        st = ln.strip()
        if st and not st.startswith("#") and "=" in st:
            k = st.partition("=")[0].strip()
            if k in updates:
                done.add(k)
                if updates[k] is not None:
                    out.append(f"{k}={updates[k]}")
                continue
        out.append(ln)
    for k, v in updates.items():
        if k not in done and v is not None:
            out.append(f"{k}={v}")
    os.makedirs(os.path.dirname(SECRETS_PATH), exist_ok=True)
    with open(SECRETS_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + ("\n" if out else ""))
    os.chmod(SECRETS_PATH, 0o600)


def _mask_key(k):
    """掩码:头 4 + 固定 8 星 + 尾 4(中段定长,不泄露真实长度)。本机单用户应用,
    完整 key 仍随接口回前端供设置页显示/眼睛切换——key 本就在本机 auth.json。"""
    if not k:
        return ""
    if len(k) <= 8:
        return k[:2] + "•" * 12
    return k[:4] + "•" * 16 + k[-4:]
