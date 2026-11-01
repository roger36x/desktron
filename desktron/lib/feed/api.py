"""回答中的飞书消息引用只读预览，不提供已下线的事件流。"""
import os
import urllib.parse

from deskcfg import REPO_ROOT


def owns(p, method):
    return p == "/workbench/lark-message"


def handle(h, p, q, method):
    if p == "/workbench/lark-message" and method == "GET":
        # 先查已有本机同步材料，必要时通过当前用户的 lark-cli 权限只读获取。
        # 不依赖 Timeline 或它的后台同步任务，不创建新的消息正文缓存。
        from connectors.im import lark
        params = urllib.parse.parse_qs(q)
        url = str((params.get("url") or [""])[0]).strip()
        if not url:
            return h._send_json({"ok": False, "code": "missing_url", "error": "缺少飞书消息链接"}, 422)
        sync_root = os.path.expanduser(os.environ.get(
            "DESKTRON_SYNC_ROOT", "~/.local/share/desktron/sync"))
        result = lark.message_reference(sync_root, url, os.path.join(REPO_ROOT, ".workbench"))
        return h._send_json(result, 200 if result.get("ok") else 422)
    return h._send_json({"error": "unknown"}, 404)
