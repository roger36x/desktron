#!/usr/bin/env python3
"""外部 Chrome 兼容能力(Playwright MCP)的逐引擎安装适配器 + 状态查询。

Desk 默认设置页已使用内置浏览器 App Control；本模块保留 /workbench/browser 与 CLI
(status/enable/disable)，兼容此前主动启用外部 Chrome 的用户，不再作为默认操控路径。
做的事:把 [`@playwright/mcp`](https://github.com/microsoft/playwright-mcp) 的 extension 模式
注册进各引擎自己的 MCP 配置。extension 模式连用户**已运行、已登录**的 Chrome 标签页,复用
其 cookie/会话——内网 SSO 站免重登。走 DOM/可访问性树,不像素点击。

之所以逐引擎适配:每个引擎读不同的 MCP 配置(cursor 读 ~/.cursor/mcp.json,claude 读
~/.claude.json,codex 被 Desk 启动参数清空,需框架改动)。纯本机文件操作、零凭证。

不可自动化的一步:装 Chrome 扩展(Chrome 安全模型不允许静默装)。前端引导用户点一次
(`/workbench/open-external` 开商店页);装好后可把扩展 UI 里的 token 存进配置免每次批准。
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

MCP_NAME = "playwright"
# Playwright Chrome 扩展(复用已登录会话)——Chrome 应用商店
EXTENSION_URL = ("https://chromewebstore.google.com/detail/"
                 "playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm")
TOKEN_ENV = "PLAYWRIGHT_MCP_EXTENSION_TOKEN"


def _npx():
    return shutil.which("npx")


def npx_available():
    return bool(_npx())


def _server_entry(token=""):
    # env.PATH 兜底:GUI 启动的引擎 PATH 可能受限(不含 nvm/homebrew 的 node),
    # 注入 npx 所在目录 + 常见 node 目录,保证 MCP 子进程能找到 npx→node。
    npx = _npx()
    parts = []
    if npx:
        parts.append(os.path.dirname(npx))
    parts += ["/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin"),
              os.environ.get("PATH", "/usr/bin:/bin")]
    env = {"PATH": ":".join(dict.fromkeys(p for p in parts if p))}   # 去重保序
    if token:
        env[TOKEN_ENV] = token
    return {"command": "npx", "args": ["@playwright/mcp@latest", "--extension"],
            "type": "stdio", "env": env}


# ---- 文件读写(保留其它键,只动 mcpServers.<name>) ----

def _read_json(path):
    """读 JSON 配置。文件不存在 / 空 → {};存在但解析失败 → **抛出**。
    绝不拿 {} 回落再写回:~/.claude.json 含用户全部项目/历史,静默覆盖=灾难性数据丢失。
    写路径(_set)用本函数,解析失败即中止,不落盘。"""
    p = Path(path)
    if not p.exists():
        return {}
    txt = p.read_text("utf-8")
    if not txt.strip():
        return {}
    return json.loads(txt)


def _read_json_safe(path):
    """只读场景(状态查询):解析失败也不抛,回落 {}——查状态不该因坏文件崩;写路径另有严格校验。"""
    try:
        return _read_json(path)
    except Exception:
        return {}


def _write_json(path, obj):
    """原子写:先写 .tmp 再 os.replace,进程中断也不会把用户配置写成半截。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(tmp, p)


def _entry(path):
    return (_read_json_safe(path).get("mcpServers") or {}).get(MCP_NAME) or {}


def _existing_token(path):
    return ((_entry(path).get("env") or {}).get(TOKEN_ENV)) or ""


def _set(path, enabled, token=""):
    d = _read_json(path)   # 严格读:解析失败即抛,由上层转成错误返回,绝不覆盖用户既有配置
    servers = d.setdefault("mcpServers", {})
    if enabled:
        servers[MCP_NAME] = _server_entry(token)
    else:
        servers.pop(MCP_NAME, None)
        if not servers:
            d.pop("mcpServers", None)
    _write_json(path, d)


# ---- 逐引擎适配器注册表 ----
# supported=True 走 mcpServers 文件路径;False 给出原因(待补 / 需框架改动)。

CURSOR_CFG = os.path.expanduser("~/.cursor/mcp.json")
CLAUDE_CFG = os.path.expanduser("~/.claude.json")

# 注意:cursor/claude 都还没有 Desk 隔离配置层,写的是引擎的全局配置——开关影响本机该引擎
# 的所有会话(含 Desk 之外用户自己开的),config 文案里向用户写明。隔离层就绪后应迁过去。
ADAPTERS = {
    "cursor-agent": {"supported": True, "path": CURSOR_CFG, "label": "cursor",
                     "config": "~/.cursor/mcp.json（全局：影响本机所有 cursor 会话，含 Desk 外）"},
    "claude": {"supported": True, "path": CLAUDE_CFG, "label": "claude",
               "config": "~/.claude.json（全局：影响本机所有 claude 会话，含 Desk 外）"},
    "codex": {"supported": False,
              "note": "Desk 已读取 Codex MCP 配置；浏览器 MCP 的 TOML 配置适配器待补"},
    "pi": {"supported": False,
           "note": "Pi 0.84.x 官方不内置 MCP；需通过独立扩展桥接，当前浏览器 MCP 尚未交付"},
}


def status_all():
    """每引擎:{enabled, supported, has_token, config, note}+ 全局 npx/扩展信息。"""
    out = {"npx_available": npx_available(), "npx_path": _npx() or "",
           "extension_url": EXTENSION_URL, "engines": {}}
    for eng, a in ADAPTERS.items():
        row = {"supported": a["supported"], "label": a.get("label", eng),
               "config": a.get("config", ""), "note": a.get("note", ""),
               "enabled": False, "has_token": False}
        if a["supported"]:
            row["enabled"] = bool(_entry(a["path"]))
            row["has_token"] = bool(_existing_token(a["path"]))
        out["engines"][eng] = row
    return out


def set_engine(engine, enabled, token=""):
    a = ADAPTERS.get(engine)
    if not a:
        raise ValueError(f"未知引擎: {engine}")
    if not a["supported"]:
        raise ValueError(f"{engine} 暂不支持开关: {a.get('note', '')}")
    # 开启但本次没给 token 时,保留已存的 token(拨动开关不清 token)
    if enabled and not token:
        token = _existing_token(a["path"])
    _set(a["path"], enabled, token)
    return {"ok": True, "engine": engine, "enabled": enabled,
            "has_token": bool(enabled and token), "config": a.get("config", ""),
            "hint": "MCP 在会话启动时加载——新开一个该引擎会话才生效。"
                    "extension 模式首次操作会让你选连接哪个标签页。"}


def main():
    ap = argparse.ArgumentParser(description="浏览器操控(Playwright MCP)逐引擎安装适配器")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="查每引擎状态")
    pe = sub.add_parser("enable", help="给某引擎挂上 playwright")
    pe.add_argument("engine")
    pe.add_argument("--token", default="", help="扩展 token(免每次批准,可选)")
    pdd = sub.add_parser("disable", help="从某引擎摘掉 playwright")
    pdd.add_argument("engine")
    a = ap.parse_args()
    if a.cmd == "status":
        print(json.dumps(status_all(), ensure_ascii=False, indent=2))
    else:
        try:
            print(json.dumps(set_engine(a.engine, a.cmd == "enable", getattr(a, "token", "")),
                             ensure_ascii=False, indent=2))
        except ValueError as e:
            print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
