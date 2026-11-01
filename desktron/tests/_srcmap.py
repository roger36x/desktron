"""源码锚定的取源助手(2026-08-27 全仓分拆,specs/2026-08-27-repo-partition-2.md)。

大文件拆成了多份;读源码做正则锚定的测试改从这里取「分拆集按序拼接」的整体。
锚定强度不降——目标删失照样红;将来文件继续拆,测试也不必跟着改路径。
拼接顺序尽量贴原单文件的段顺序,让「A 在 B 之前」这类顺序锚少翻面。
"""
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
FRONTEND = os.path.join(ROOT, "frontend")


def _cat(paths):
    return "\n".join(open(p, encoding="utf-8").read() for p in paths)


def engine_adapters_src():
    d = os.path.join(LIB, "engine_adapters")
    order = ["__init__.py", "persist.py", "bus.py", "cli.py", "cli_store.py", "cli_turn.py",
             "claude.py", "claude_transcript.py", "codex.py", "codex_rollout.py",
             "codex_subagents.py"]
    return _cat(os.path.join(d, f) for f in order)


def ui_server_src():
    order = ["ui_static.py", "ui_boot.py", "ui_contract.py", "ui_remote_gate.py",
             "ui_term_ws.py", "ui_server.py"]
    return _cat(os.path.join(LIB, f) for f in order)


def chat_src():
    """app-chat 十三片按切片顺序拼接=原单文件(逐字节,切分时自证过),顺序锚不翻面。"""
    order = ["app-chat-core.js", "app-chat-model.js", "app-chat-tools.js", "app-chat-detail.js",
             "app-chat-refs.js", "app-chat-overlays.js", "app-chat-reconcile.js",
             "app-chat-composer.js", "app-chat-opts.js", "app-chat-input.js",
             "app-chat-tasksbar.js", "app-chat-panels.js", "app-chat-situation.js", "app-chat-page.js"]
    return _cat(os.path.join(FRONTEND, f) for f in order)


def frontend_src(name):
    """按原文件名取源:被拆掉的名字返回拼接整体,其余原样读。测试的本地 read 助手接它。"""
    if name == "app-chat.js":
        return chat_src()
    if name == "app-panel.js":
        return panel_src()
    if name == "app-rail.js":
        return rail_src()
    if name == "app-assets.js":
        return assets_src()
    if name == "app-settings.js":
        return settings_src()
    return open(os.path.join(FRONTEND, name), encoding="utf-8").read()


def chat_src_file():
    """给 node 侧的 harness 用:把拼接整体落一份临时文件,返回路径(按内容散列缓存)。"""
    import hashlib, tempfile
    body = chat_src()
    h = hashlib.blake2b(body.encode("utf-8"), digest_size=8).hexdigest()
    p = os.path.join(tempfile.gettempdir(), f"cd-chat-src-{h}.js")
    if not os.path.exists(p):
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
    return p


# 这里描述的是拆分前 app-panel.js 的逻辑源码视图，供跨片源码断言使用；运行时为了让
# 手机侧栏单包启动，sidebar/terms 也会进入 RAIL_ORDER，实际交付归属由 frontend_bundle.py
# 与 test_frontend_bundle_budget.py 共同锁定。
PANEL_ORDER = ["app-panel-sidebar.js", "app-panel-tasks-page.js",
               "app-panel-arrangement-system.js", "app-panel-arrangements.js",
               "app-panel-task-memo.js", "app-panel-terms.js"]
RAIL_ORDER = ["app-rail-nav.js", "app-panel-sidebar.js", "app-panel-terms.js", "app-rail-topnav.js",
              "app-desk-control.js", "app-native-control.js"]


def panel_src():
    return _cat(os.path.join(FRONTEND, f) for f in PANEL_ORDER)


def rail_src():
    return _cat(os.path.join(FRONTEND, f) for f in RAIL_ORDER)


ASSETS_ORDER = ["app-assets-term.js", "app-assets-term-ui.js",
                "app-assets-apps.js", "app-assets-create.js"]
SETTINGS_ORDER = ["app-settings-core.js", "app-settings-sections.js", "app-settings-network.js", "app-settings-usage.js",
                  "app-settings-memory.js", "app-settings-legacy.js"]


def assets_src():
    return _cat(os.path.join(FRONTEND, f) for f in ASSETS_ORDER)


def settings_src():
    return _cat(os.path.join(FRONTEND, f) for f in SETTINGS_ORDER)


def term_src():
    """term 的分册按原段序拼接(分册是单命名空间的 exec 分册,见 term.py 头部说明)。"""
    order = ["term_titles.py", "term_records.py", "term_sessions.py", "term.py"]
    return _cat(os.path.join(LIB, f) for f in order)
