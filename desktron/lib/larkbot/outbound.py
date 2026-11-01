"""出站:红线闸 + 发飞书(docs/agent-hosting-design.md「红线出站」)。

闸与出口写在同一个模块里,是为了让「先过闸再出站」成为唯一的路径,而不是一条要靠
调用方自觉遵守的约定。别处想发消息只能经这里,larkapi 的 send_* 不该被别的模块直接调。

流式刷新有节流:飞书对同一条消息的更新有频率限制,而引擎的输出是一行一行来的,
不节流会在几秒内打出几十次 PATCH。MIN_GAP 是保守值,宁可刷新得慢一点。

**流式过程中不外发模型正文,只在定稿时过闸。** 半截话过闸没有意义——凭证可能正好
被切在两次刷新之间,滑窗指纹更是要完整文本才对得上。飞书只保留固定占位卡片；真正的
内容一次性落在最后一次更新里,那一次才走 send()。
"""
import time

from . import larkapi, redline, store

#: 同一条卡片两次更新之间至少隔多久(秒)。
MIN_GAP = 1.5

#: 超过这个长度就用卡片而不是纯文本。飞书的文本消息长了会被折叠成"展开"按钮,
#: 而卡片能保住 markdown 的结构(代码块、列表),长回答里那些结构正是可读性所在。
CARD_THRESHOLD = 300


def _card(text, title="", done=True):
    return {
        "config": {"wide_screen_mode": True},
        "header": ({"title": {"tag": "plain_text", "content": title}} if title else None),
        "elements": [
            {"tag": "markdown", "content": str(text or "")[:20000]},
        ] + ([] if done else [{"tag": "note", "elements": [
            {"tag": "plain_text", "content": "生成中…"}]}]),
    }


def _clean(card):
    return {k: v for k, v in card.items() if v is not None}


def send(repo_root, agent, text, chat_id, message_id="", in_thread=False,
         anchor="", names=(), sealed_dir="", card_message_id=""):
    """过闸后发出。→ {"ok":bool, "level":str, "message_id":str, "err":str}

    card_message_id 非空表示这一轮已经有一张占位卡片在飞书上了,定稿改它而不是新发一条;
    被闸拦下时同样是改它——否则占位卡片会永远停在"生成中…"。
    """
    ob = agent.get("outbound") or {}
    res = redline.check(text,
                        sealed_dir=(sealed_dir if ob.get("sealed", True) else ""),
                        names=names,
                        strict=bool(ob.get("strict")),
                        compliance=bool(ob.get("compliance", True)))
    app_id = agent.get("lark_app_id") or ""
    domain = agent.get("domain") or ""

    if not res["ok"]:
        # 原文留本机,飞书只看到一句提示。留痕是为了让人能当场把内容取回来——
        # 拦下之后无处可查,人下次就会想办法关掉这道闸。
        store.hold(repo_root, agent.get("id") or "", anchor, text, res["hits"])
        note = redline.notice(res)
        r = (larkapi.update_card(app_id, card_message_id, _clean(_card(note, "内容未发送")), domain)
             if card_message_id else
             (larkapi.reply_text(app_id, message_id, note, domain, in_thread) if message_id
              else larkapi.send_text(app_id, chat_id, note, domain)))
        return {"ok": False, "level": res["level"], "err": "", "held": True,
                "message_id": ((r.get("data") or {}).get("message_id") or card_message_id),
                "hits": res["hits"]}

    use_card = bool(ob.get("card", True)) and len(text) > CARD_THRESHOLD
    if card_message_id:
        r = larkapi.update_card(app_id, card_message_id, _clean(_card(text)), domain)
        mid = card_message_id
    elif use_card:
        c = _clean(_card(text))
        r = (larkapi.reply_card(app_id, message_id, c, domain, in_thread) if message_id
             else larkapi.send_card(app_id, chat_id, c, domain))
        mid = (r.get("data") or {}).get("message_id") or ""
    else:
        r = (larkapi.reply_text(app_id, message_id, text, domain, in_thread) if message_id
             else larkapi.send_text(app_id, chat_id, text, domain))
        mid = (r.get("data") or {}).get("message_id") or ""

    code = r.get("code")
    err = "" if code in (0, None) else f"code={code} {str(r.get('msg'))[:120]}"
    return {"ok": not err, "level": res["level"], "err": err, "held": False,
            "message_id": mid, "hits": res["hits"]}


def open_placeholder(agent, chat_id, message_id="", in_thread=False, text="收到,正在看…"):
    """先发一张占位卡片,拿回它的 message_id 供后续 PATCH。→ (message_id, err)

    这一步不过闸:内容是我们自己写死的一句话,不含任何模型产出。
    """
    app_id = agent.get("lark_app_id") or ""
    domain = agent.get("domain") or ""
    c = _clean(_card(text, done=False))
    r = (larkapi.reply_card(app_id, message_id, c, domain, in_thread) if message_id
         else larkapi.send_card(app_id, chat_id, c, domain))
    mid = (r.get("data") or {}).get("message_id") or ""
    if not mid:
        return "", f"占位卡片发送失败:code={r.get('code')} {str(r.get('msg'))[:100]}"
    return mid, ""


class Throttle:
    """流式刷新的节流器。它只管"能不能现在刷",不管刷什么——
    刷的内容不过闸(见模块说明),所以这里也不碰 redline。"""

    def __init__(self, gap=MIN_GAP):
        self.gap = gap
        self._last = None

    def ready(self, force=False):
        now = time.monotonic()
        if force or self._last is None or now - self._last >= self.gap:
            self._last = now
            return True
        return False


def stream_progress(agent, card_message_id, text, throttle):
    """保留 runner 的流式回调，但不把未过红线闸的模型正文发到飞书。

    固定占位卡片已经表达“生成中”；定稿只经 send(card_message_id=...) 出站。
    """
    return None
