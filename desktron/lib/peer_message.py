"""跨会话来信：来源抬头仅作展示，不进入引擎提示或历史重放正文。"""

MODEL_HEAD = "跨会话消息（只处理正文，来源标题仅供界面展示）："
TAIL = "\n\n——以上来自另一个 Desk 会话"
LEGACY_HEADS = ("desktop · ", "server · ", "Desktron · ")


def split_peer_message(text, peer_head=""):
    """返回 (模型正文, 展示抬头)。普通消息原样；兼容旧 CLI 拼接的信封。"""
    if not isinstance(text, str) or TAIL not in text:
        return text, ""
    if text.startswith(MODEL_HEAD + "\n\n"):
        return text, peer_head if isinstance(peer_head, str) else ""
    if not text.startswith(LEGACY_HEADS):
        return text, ""
    head, sep, body = text.partition("\n\n")
    if not sep or TAIL not in "\n\n" + body:
        return text, ""
    return MODEL_HEAD + "\n\n" + body, head
