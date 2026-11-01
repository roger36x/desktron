"""一条飞书消息落到哪个 agent、哪条会话(docs/agent-hosting-design.md「触发方式与消息面」)。

纯函数,不碰网络也不读文件:调用方把消息事件和 agent 声明喂进来,这里只出判定。
所以它可以被完整测到——路由是这套东西里最容易错、错了又最难从现象反推的一环。

两件事:
  should_reply()  这条消息该不该理
  anchor_of()     该理的话,归到哪条会话

锚点(anchor)是会话的身份。同一个锚点的消息进同一条会话、共用同一份历史;
换了锚点就是另一条会话。群、话题、私聊在这里被统一成一个字符串。
"""

#: 群里必须被 @ 才理。私聊不需要——私聊本来就只有你和它两个人。
#: 话题群里也不需要:进了话题就是在跟它说话,每句都要 @ 是折磨。


def _mentions(msg):
    """消息 @ 到的 open_id / app_id 集合(两种 id 都收,飞书两种都可能给)。"""
    out = set()
    for m in (msg.get("mentions") or []):
        mid = m.get("id")
        if isinstance(mid, dict):
            for k in ("open_id", "app_id", "union_id"):
                if mid.get(k):
                    out.add(mid[k])
        elif isinstance(mid, str) and mid:
            out.add(mid)
        # 飞书在 mentions 里另给一份平铺的 id,SDK 版本不同位置不同,一并收
        for k in ("open_id", "app_id", "union_id"):
            if m.get(k):
                out.add(m[k])
    return out


def should_reply(msg, bot_ids, chat_mode=""):
    """→ (是否回应, 理由)。理由是给日志看的,排查「为什么没理我」全靠它。

    bot_ids:这个机器人的身份集合(open_id / app_id),@ 判定拿它比对。
    chat_mode:群的形态,"topic" 表示话题群(需调用方查过 im/v1/chats 拿到)。
    """
    ctype = msg.get("chat_type") or "group"
    if ctype == "p2p":
        return True, "p2p"
    # 话题群:在话题里说话即触发。判据是「这条消息处于某个话题内」——
    # thread_id 才是权威信号,root_id 不是(见 anchor_of 的注释)。
    if chat_mode == "topic" and msg.get("thread_id"):
        return True, "topic"
    if _mentions(msg) & set(bot_ids or ()):
        return True, "mention"
    return False, "no-mention"


def anchor_of(msg, chat_mode=""):
    """→ (锚点, 形态)。形态取值见设计稿的五行表,只用于日志与调试。

    判据以 thread_id 为准,不认单独出现的 root_id:飞书客户端的引用气泡("回复"那个小箭头)
    会给一条用户视角的顶层消息塞上 root_id 却不塞 thread_id。只看 root_id 会把引用
    错认成话题回复,把人从群会话踢进一条不存在的话题——botmux 踩过并记了下来
    (event-dispatcher.ts:2603 一带)。官方文档也是这个口径:root_id/parent_id
    「仅在回复消息场景会有返回值」,而 thread_id「不返回说明该消息非话题消息」。
    """
    ctype = msg.get("chat_type") or "group"
    chat_id = msg.get("chat_id") or ""
    if ctype == "p2p":
        return f"chat:{chat_id}", "p2p"
    tid = msg.get("thread_id") or ""
    rid = msg.get("root_id") or ""
    if tid and rid:
        return f"thread:{rid}", "real-thread"          # 真话题回复:锚在话题根
    if tid:
        return f"thread:{msg.get('message_id') or tid}", "regular-group-thread"
    if chat_mode == "topic":
        # 话题群的顶层消息:这条自己就是一个新话题的种子,后续回复会带 thread_id 指回来
        return f"thread:{msg.get('message_id') or ''}", "topic-chat"
    return f"chat:{chat_id}", "regular-group-chat"     # 普通群 @:整个群一条会话


def allowed(msg, agent):
    """可见范围闸:allowed_users / allowed_chats 任一为空表示该维度不限制。
    → (是否放行, 理由)。两个都配了要同时满足。"""
    sender = ((msg.get("sender") or {}).get("id") or "")
    if isinstance(sender, dict):
        sender = sender.get("open_id") or sender.get("union_id") or ""
    us = [u for u in (agent.get("allowed_users") or []) if u]
    cs = [c for c in (agent.get("allowed_chats") or []) if c]
    if us and sender not in us:
        return False, "user-not-allowed"
    if cs and (msg.get("chat_id") or "") not in cs:
        return False, "chat-not-allowed"
    return True, "ok"


def pick(msg, agents, bot_ids_of, chat_mode=""):
    """从若干 agent 里挑该由谁接这条消息。

    → (agent, 锚点, 形态, 理由);没人接则 agent 为 None。

    挑法:同一个飞书 app 下只可能有一个 agent 接(一个 bot 一个身份),所以先按
    app_id 过滤,再看该不该理、允不允许。多个 agent 绑同一个 app 是配置错误,
    这里取第一个并在理由里标出来,不静默。
    """
    app_id = msg.get("_app_id") or ""
    cand = [a for a in agents if not app_id or (a.get("lark_app_id") or "") == app_id]
    if not cand:
        return None, "", "", "no-agent-for-app"
    a = cand[0]
    ok, why = should_reply(msg, bot_ids_of(a), chat_mode)
    if not ok:
        return None, "", "", why
    ok2, why2 = allowed(msg, a)
    if not ok2:
        return None, "", "", why2
    anc, kind = anchor_of(msg, chat_mode)
    return a, anc, kind, ("dup-app:" + why if len(cand) > 1 else why)
