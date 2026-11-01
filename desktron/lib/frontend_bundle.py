"""首屏虚拟合包：源码保持分片，HTTP 仍按原逻辑文件交付。"""
import os
import threading


# 顺序就是原单文件的段顺序。经典脚本共享全局作用域，顺序是运行契约，不可排序。
# app-approval.js 并进 /app-chat.js 那一包:它画的就是会话过程区里的那张权限卡,用的全是
# 会话页的符号(_hsViews / hsProcHost / HS_ICONS / hsNewTurn)。不单开 script——首屏脚本
# 请求数有预算(test_首屏脚本不超过优化前预算),每加一个都是一次往返,经隧道时尤其贵。
ASSET_BUNDLES = {
    # 界面语言模块与各语言包排在核心脚本之前，与它同一个请求交付：其后所有脚本都能用
    # DeskI18n，首屏请求数不变。新增语言包时加在 app-i18n-en.js 之后、app-core.js 之前。
    "/app-core.js": ("app-i18n.js", "app-i18n-zh.js", "app-i18n-en.js", "app-core.js"),
    "/app-rail.js": (
        "app-rail-nav.js", "app-panel-sidebar.js", "app-panel-terms.js", "app-rail-topnav.js",
        "app-desk-control.js", "app-native-control.js",
    ),
    "/app-panel.js": (
        "app-panel-tasks-page.js",
        "app-panel-arrangement-system.js", "app-panel-arrangements.js",
        "app-panel-task-memo.js",
    ),
    "/app-assets.js": (
        "app-assets-term.js", "app-assets-term-ui.js", "app-assets-apps.js", "app-assets-create.js",
    ),
    "/app-chat.js": (
        "app-chat-core.js", "app-chat-model.js", "app-chat-tools.js", "app-chat-detail.js",
        "app-chat-refs.js", "app-chat-overlays.js", "app-chat-reconcile.js",
        "app-chat-composer.js", "app-chat-opts.js", "app-chat-input.js",
        "app-chat-tasksbar.js", "app-chat-panels.js", "app-chat-situation.js", "app-chat-page.js",
        "app-approval.js",
    ),
    "/app-settings.js": (
        "app-settings-core.js", "app-settings-sections.js", "app-settings-network.js", "app-settings-usage.js", "app-settings-setup.js",
        "app-settings-memory.js", "app-settings-legacy.js",
    ),
}

_BUNDLE_CACHE = {}
_BUNDLE_LOCK = threading.Lock()


def bundle_data(frontend, relpath):
    """返回 (组成文件签名, 拼接字节, 最新 mtime)；不是虚拟资源或缺片时返回 None。"""
    relpath = "/" + str(relpath or "").lstrip("/")
    parts = ASSET_BUNDLES.get(relpath)
    if not parts:
        return None
    try:
        paths = [os.path.join(frontend, name) for name in parts]
        stats = [os.stat(path) for path in paths]
    except OSError:
        return None
    signature = tuple((st.st_mtime_ns, st.st_size) for st in stats)
    cache_key = (os.path.realpath(frontend), relpath)
    with _BUNDLE_LOCK:
        hit = _BUNDLE_CACHE.get(cache_key)
        if hit and hit[0] == signature:
            return hit
    chunks = []
    try:
        for path in paths:
            with open(path, "rb") as f:
                chunks.append(f.read())
    except OSError:
        return None
    body = b"\n".join(chunks)
    hit = (signature, body, max(st.st_mtime for st in stats))
    with _BUNDLE_LOCK:
        _BUNDLE_CACHE[cache_key] = hit
    return hit
