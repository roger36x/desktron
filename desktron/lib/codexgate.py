"""codex 域内准入闸接线:provision 一个 desk 管的 CODEX_HOME(软链登录 auth + sessions、首拷 config、
写 desk 的 hooks.json 指向 codex_gate.py),让准入闸只作用于 desk 起的 codex,不碰用户全局 ~/.codex。

desk 起 codex 时注入 CODEX_HOME。TUI 那条路还带 `--dangerously-bypass-hook-trust`(hook 是 desk
自审来源,正是该 flag 的设计用途),TUI 2026-08-20 退场后只剩 GUI 这条——而 `codex app-server`
不认这个标志(实测 --help 里没有),未持久化 trust 时 hook 是否 fire 待验证。配置照旧就位,
拦得住就拦,拦不住也不阻断启动。

幂等:auth/sessions 软链回全局(共享登录 + 会话;codex 写 rollout 经软链落 ~/.codex/sessions,
term.py 仍读 ~/.codex/sessions 不变);config.toml 首次拷用户配置,之后只幂等打开
[features].hooks=true,不覆盖其他配置;hooks.json desk 拥有,幂等覆盖。
"""
import json
import os
import re
import sys
import shutil
import time

try:
    import tomllib          # 3.11+
except ImportError:
    try:
        import tomli as tomllib  # Python 3.9/3.10
    except ImportError:
        tomllib = None      # 最小安装缺少解析器时降级为正则检查

from deskcfg import REPO_ROOT, DATA_HOME

USER_CODEX = os.path.expanduser("~/.codex")
CODEX_HOME = os.path.join(os.path.dirname(DATA_HOME), "codex-home")   # ~/.local/share/desktron/codex-home
_GATE = os.path.join(REPO_ROOT, ".claude", "skills", "memory", "scripts", "codex_gate.py")


def _hooks_json():
    cmd = 'python3 "' + _GATE + '"'
    return {"hooks": {"PreToolUse": [
        {"matcher": ".*", "hooks": [{"type": "command", "command": cmd}]},
    ]}}


def _enable_hooks_config(path):
    """Ensure managed Codex config enables hooks without replacing other settings."""
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                text = f.read()
        else:
            text = ""
    except Exception:
        text = ""
    lines = text.splitlines()
    out = []
    in_features = False
    saw_features = False
    saw_hooks = False
    multiline = ""
    for line in lines:
        active = not multiline
        section = _toml_table_path(line) if active else None
        if section is not None:
            if in_features and not saw_hooks:
                out.append("hooks = true")
                saw_hooks = True
            in_features = section == ("features",)
            saw_features = saw_features or in_features
            out.append(line)
        elif active and in_features and re.match(r"^\s*hooks\s*=", line):
            out.append("hooks = true")
            saw_hooks = True
        else:
            out.append(line)
        multiline = _toml_multiline_state(line, multiline)
    if in_features and not saw_hooks:
        out.append("hooks = true")
    if not saw_features:
        if out and out[-1].strip():
            out.append("")
        out.extend(["[features]", "hooks = true"])
    body = "\n".join(out).rstrip() + "\n"
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(path + ".tmp", path)


def _toml_table_header(line):
    """Return ``(is_array, raw_key)`` for a physical TOML table-header line."""
    text = str(line or "").lstrip()
    array = text.startswith("[[")
    if not (array or text.startswith("[")):
        return None
    start = 2 if array else 1
    close = "]]" if array else "]"
    quote, escaped = "", False
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if quote == '"' and ch == "\\" and not escaped:
                escaped = True
                continue
            if ch == quote and not escaped:
                quote = ""
            escaped = False
            continue
        if ch in ('"', "'"):
            quote = ch
        elif text.startswith(close, i):
            tail = text[i + len(close):].strip()
            if not tail or tail.startswith("#"):
                return array, text[start:i].strip()
            return None
    return None


def _toml_key_path(raw):
    """Parse the small TOML dotted-key grammar used by table headers."""
    text, i, parts = str(raw or ""), 0, []
    escapes = {'b': '\b', 't': '\t', 'n': '\n', 'f': '\f', 'r': '\r', '"': '"', '\\': '\\'}
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return tuple(parts) if parts else None
        if text[i] in ('"', "'"):
            quote, i, value = text[i], i + 1, []
            while i < len(text) and text[i] != quote:
                ch = text[i]
                if quote == '"' and ch == "\\":
                    i += 1
                    if i >= len(text):
                        return None
                    code = text[i]
                    if code in escapes:
                        value.append(escapes[code])
                    elif code in ('u', 'U'):
                        width = 4 if code == 'u' else 8
                        digits = text[i + 1:i + 1 + width]
                        if len(digits) != width or not re.fullmatch(r'[0-9A-Fa-f]+', digits):
                            return None
                        try:
                            value.append(chr(int(digits, 16)))
                        except ValueError:
                            return None
                        i += width
                    else:
                        return None
                else:
                    value.append(ch)
                i += 1
            if i >= len(text) or text[i] != quote:
                return None
            parts.append("".join(value))
            i += 1
        else:
            start = i
            while i < len(text) and not text[i].isspace() and text[i] != '.':
                i += 1
            if i == start:
                return None
            parts.append(text[start:i])
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return tuple(parts)
        if text[i] != '.':
            return None
        i += 1


def _toml_table_path(line):
    """Resolve equivalent quoted/bare table spellings to one semantic key path."""
    header = _toml_table_header(line)
    if header is None:
        return None
    if tomllib is None:
        # Minimal installs still need semantic equality for equivalent quoted keys;
        # otherwise appending the managed table would create a duplicate definition.
        return _toml_key_path(header[1]) or ("__other_table__", header[1])
    probe = "__desktron_table_probe_51c4d7d3__"
    try:
        parsed = tomllib.loads(str(line).strip() + "\n" + probe + " = true\n")
    except Exception:
        return ("__other_table__", header[1])

    def find(value, path=()):
        if isinstance(value, dict):
            if probe in value:
                return path
            for key, child in value.items():
                found = find(child, path + (str(key),))
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in reversed(value):
                found = find(child, path)
                if found is not None:
                    return found
        return None

    return find(parsed) or ("__other_table__", header[1])


def _toml_multiline_state(line, state=""):
    """Track TOML triple-quoted strings so their contents are never treated as tables."""
    text, i = str(line or ""), 0
    while i < len(text):
        if state:
            delimiter = state
            j = i
            while True:
                j = text.find(delimiter, j)
                if j < 0:
                    return state
                if delimiter == "'''":
                    break
                backslashes, k = 0, j - 1
                while k >= 0 and text[k] == "\\":
                    backslashes += 1
                    k -= 1
                if backslashes % 2 == 0:
                    break
                j += 1
            state, i = "", j + 3
            continue
        if text[i] == "#":
            break
        if text.startswith('"""', i) or text.startswith("'''", i):
            state, i = text[i:i + 3], i + 3
            continue
        if text[i] in ('"', "'"):
            quote, i = text[i], i + 1
            escaped = False
            while i < len(text):
                ch = text[i]
                if quote == '"' and ch == "\\" and not escaped:
                    escaped, i = True, i + 1
                    continue
                if ch == quote and not escaped:
                    i += 1
                    break
                escaped, i = False, i + 1
            continue
        i += 1
    return state


def _configure_app_control(path):
    """Desk 的 Codex 只接 Desk App Control，不挂 ChatGPT Browser Plugin。

    Browser Plugin 的 iab 后端连接 ChatGPT/Codex 宿主的原生管道，不是 Desk WKWebView。
    历史 managed config 还可能长期保留已不存在的 Codex.app 路径与旧插件版本。这里仅
    替换这两个明确由 Desk 管理的 section，不覆盖用户的模型、其他 MCP 或本地配置。
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        lines = []
    blocked = {
        ('mcp_servers', 'node_repl'),
        ('mcp_servers', 'node_repl', 'env'),
        ('plugins', 'browser@openai-bundled'),
        ('mcp_servers', 'desk_app_control'),
        ('mcp_servers', 'desk_app_control', 'env'),
    }
    out = []
    skipping = False
    in_features = False
    multiline = ""
    for line in lines:
        # 普通 table 与 array-of-tables 都会结束前一段；quoted key 可以合法包含 ]，
        # 多行字符串里的同形正文则不是表头。
        active = not multiline
        section = _toml_table_path(line) if active else None
        if section is not None:
            skipping = section in blocked
            in_features = section == ('features',)
        # 清掉 Desk 曾经写坏的两个键(2026-09-02 的 d85fe97,当天 d8a95c4 撤回)。
        # 那一版把 code_mode/code_mode_host 写成 false,本意是让 MCP 工具改走 direct 面;
        # 实测证伪:code_mode_only 是模型自身的属性(GPT-5.6-Sol 即是),关掉不会回退到
        # direct,只会掐断唯一通路,codex 每轮直接回 "code-mode host is disabled"。
        #
        # 撤回代码不够:已经落进各机器 CODEX_HOME 的那两行不会自己消失,而新代码既不写
        # 也不删,它们就永久留着。这里主动清一次,让本机、服务器与任何跑过那一版的
        # 机器都能自愈;codex 的默认值随之恢复。清理是幂等的,清完不再写回。
        if active and in_features and re.match(r"^\s*(code_mode|code_mode_host)\s*=", line):
            multiline = _toml_multiline_state(line, multiline)
            continue
        if not skipping:
            out.append(line)
        multiline = _toml_multiline_state(line, multiline)
    while out and not out[-1].strip():
        out.pop()
    # MCP 必须跟随当前实际运行的 Desk 副本，而不是 REPO_ROOT。开发态 REPO_ROOT
    # 恰好指向源码仓，但发行版和另一台电脑未必存在同一绝对路径；把源码
    # 路径持久化进 CODEX_HOME 后，下一次 app-server 启动会直接 ENOENT。
    # 2026-09-02 复发一次:REPO_ROOT 在运行态指向 workspace,拼出的路径根本不存在,
    # codex 每轮报 "MCP startup failed: No such file or directory"。
    #
    # 同时显式用当前 Python 启动脚本，不依赖可执行位与 shebang 的二次解释器查找。
    command = sys.executable
    script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin",
                                          "app-control-mcp"))
    if out:
        out.append("")
    out.extend([
        '[plugins."browser@openai-bundled"]',
        "enabled = false",
        "",
        "[mcp_servers.desk_app_control]",
        "command = " + json.dumps(command),
        "args = [" + json.dumps(script) + "]",
        # MCP 外层若继续用 on-request approval，code-mode host 会把调用卡在
        # tools/call 之前；App Control Runtime 已按动作风险独立审批，这里无需重复拦截。
        'default_tools_approval_mode = "approve"',
        # Codex 会清洗 MCP 子进程环境；会话能力是绑定当前 Desk 会话的随机 bearer，
        # 必须随 MCP 进程转发，但不进入配置值、日志或模型正文。
        'env_vars = ["DESKTRON_SESSION_ID", "DESKTRON_APP_CONTROL_SESSION_CAPABILITY", "DESKTRON_APP_CONTROL_SOCKET", "DESKTRON_UI_PORT", "DESKTRON_UI_URL"]',
        "startup_timeout_sec = 5",
        "tool_timeout_sec = 70",
    ])
    body = "\n".join(out).rstrip() + "\n"
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(path + ".tmp", path)


def _config_needs_refresh(path):
    """Return True when the managed Codex config is stale enough to break startup.

    Desk keeps its own CODEX_HOME. That config may outlive a user's global Codex
    reset, including model_catalog_json files that were moved away. Refresh only
    on parse failure or missing referenced catalog so local Desk-specific edits
    are otherwise preserved.
    """
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return True
    except Exception:
        return False
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except Exception:
            return True
        catalog = data.get("model_catalog_json")
        if not isinstance(catalog, str):
            return False
    else:
        # Python < 3.11:不做 parse 校验,只查 catalog 引用(双引号字符串)
        m = re.search(r'(?m)^\s*model_catalog_json\s*=\s*"([^"]+)"', text)
        if not m:
            return False
        catalog = m.group(1)
    return not os.path.exists(os.path.expanduser(catalog))


def _copy_user_config(dst):
    src = os.path.join(USER_CODEX, "config.toml")
    if not os.path.exists(src):
        return False
    if _config_needs_refresh(src):
        return False    # 用户全局 config 一样 stale:拷了也修不好,还会每次启动堆一个备份
    if os.path.exists(dst):
        backup = dst + ".backup-stale-" + time.strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy(dst, backup)
        except Exception:
            pass
    shutil.copy(src, dst)
    return True


def _link_user_skills():
    """Expose user-level Codex skills inside Desk's managed CODEX_HOME."""
    tgt = os.path.join(USER_CODEX, "skills")
    link = os.path.join(CODEX_HOME, "skills")
    os.makedirs(tgt, exist_ok=True)
    if os.path.islink(link):
        return os.path.realpath(link) == os.path.realpath(tgt)
    if not os.path.lexists(link):
        os.symlink(tgt, link)
        return True
    if not os.path.isdir(link):
        return False

    # .system 是 codex 自生成的内建缓存(可再生),.DS_Store 是 Finder 副产品,都不算用户内容
    entries = [n for n in os.listdir(link) if n not in (".system", ".DS_Store")]
    for name in entries:
        src = os.path.join(tgt, name)
        cur = os.path.join(link, name)
        if not os.path.lexists(src) or not os.path.islink(cur):
            return False
        if os.path.realpath(cur) != os.path.realpath(src):
            return False
    shutil.rmtree(link)
    os.symlink(tgt, link)
    return True


def _inherit_workspace_trust(path):
    """Copy an explicit global decision only when Desk has none for this workspace.

    Never infer trust from directory ownership or sandbox mode. Existing managed
    decisions (including untrusted) take precedence; unrelated projects stay out.
    """
    if tomllib is None:
        return
    try:
        with open(os.path.join(USER_CODEX, "config.toml"), encoding="utf-8") as f:
            source = tomllib.loads(f.read()).get("projects", {})
        with open(path, encoding="utf-8") as f:
            original = f.read()
        managed = tomllib.loads(original).get("projects", {})
        workspace = os.path.realpath(REPO_ROOT)
        if any(os.path.realpath(key) == workspace for key in managed):
            return
        decision = source.get(REPO_ROOT)
        if decision is None:
            matches = [value for key, value in source.items() if os.path.realpath(key) == workspace]
            if len(matches) != 1:
                return
            decision = matches[0]
        level = decision.get("trust_level") if isinstance(decision, dict) else None
        if level not in ("trusted", "untrusted"):
            return
        body = original.rstrip() + "\n\n# Inherited from the user's explicit Codex workspace decision.\n"
        body += f'[projects.{json.dumps(workspace, ensure_ascii=False)}]\ntrust_level = "{level}"\n'
        tomllib.loads(body)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(path + ".tmp", path)
    except (OSError, ValueError, TypeError, AttributeError):
        return  # Keep the native warning when the source cannot be verified.


def ensure_codex_home():
    """幂等 provision。返回 CODEX_HOME;任一步失败返回 None(调用方回退默认 ~/.codex,不接闸、不 bypass)。"""
    try:
        os.makedirs(CODEX_HOME, exist_ok=True)
        # 首次使用也建立共享目标；已有隔离实体目录保留，由读取侧兼容。
        os.makedirs(os.path.join(USER_CODEX, "sessions"), exist_ok=True)
        for name in ("auth.json", "sessions"):   # 软链回全局:共享登录 + 会话可见
            link = os.path.join(CODEX_HOME, name)
            tgt = os.path.join(USER_CODEX, name)
            if not os.path.lexists(link) and os.path.exists(tgt):
                os.symlink(tgt, link)
        _link_user_skills()                          # 软链回全局:复用用户 Codex skills
        cfg = os.path.join(CODEX_HOME, "config.toml")   # 首次拷用户配置;之后只确保 hooks 打开
        if _config_needs_refresh(cfg):
            _copy_user_config(cfg)
        _enable_hooks_config(cfg)
        _configure_app_control(cfg)
        _inherit_workspace_trust(cfg)
        hp = os.path.join(CODEX_HOME, "hooks.json")     # desk 拥有,幂等覆盖
        with open(hp + ".tmp", "w", encoding="utf-8") as f:
            json.dump(_hooks_json(), f, ensure_ascii=False, indent=2)
        os.replace(hp + ".tmp", hp)
        return CODEX_HOME
    except Exception:
        return None
