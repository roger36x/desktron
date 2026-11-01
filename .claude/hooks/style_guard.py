#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
表达约束 Stop 钩子 —— 引擎通用（claude / codex）。

机制：助手答完触发 Stop 事件，本脚本读 transcript 最后一条 assistant 文本，
扫一遍可枚举的违禁词与装饰符号。命中就 decision=block 打回重写；干净就放行。

契约（已核 code.claude.com/docs/en/hooks，codex 同格式）：
- stdin: JSON，含 session_id / transcript_path / hook_event_name。
- 放行: exit 0，无输出。
- 打回: exit 0，stdout 输出 {"decision":"block","reason":...}，reason 展示给模型。

只拦"在中文正文里几乎不可能合法"的词。代码块、行内代码、URL 在扫描前剥离，
避免把命令、路径、英文里的半角标点和叹号误伤。因此半角标点/感叹号不进硬拦表
（误报率太高），留给 output-style 的软约束。

防循环：按 session_id 计数，连续打回 MAX_BLOCKS 次后放行，避免模型改不掉时锁死。

调参：改下面三个列表即可。BLOCK_TERMS 加词会更严但误报上升；
OPTIONAL_TERMS 是误报敏感词（核心/关键/最/彻底等技术语境常合法），默认不启用。
"""
import sys, os, json, re, hashlib

MAX_BLOCKS = 3  # 同一会话连续打回上限，到顶放行防锁死
STATE_PATH = os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "desktron-style-guard-state.json"
)

# 硬拦：夸张/引导/自我表演类，正文里几乎不可能是合法用词
BLOCK_TERMS = [
    "杀手锏", "最值钱", "革命性", "颠覆", "根本性",
    "环环相扣", "一句话讲清", "太关键了", "至关重要", "真正的",
    "最关键", "最重要", "最致命",
]

# 误报敏感，默认不启用（技术语境常合法：核心系统/关键路径/关键结果/彻底删除）
# 需要更严时把词搬进 BLOCK_TERMS。
OPTIONAL_TERMS = ["核心", "关键", "本质上", "彻底", "致命", "要命"]

# 装饰符号与 emoji：用户除非明确分类，否则禁用
SYMBOL_RE = re.compile(
    "[\U0001F300-\U0001FAFF"   # 符号与象形、表情、交通、补充符号
    "\U00002600-\U000026FF"    # 杂项符号（含 ⚠ ★ 等）
    "\U00002700-\U000027BF"    # Dingbats（含 ✅ ❌ ✔ ✖）
    "\U0001F1E6-\U0001F1FF"    # 区域指示（国旗）
    "\U00002B00-\U00002BFF"    # 杂项符号与箭头（含实心方块/圆点装饰）
    "️]"                  # 变体选择符（emoji 呈现）
)

FENCE_RE = re.compile(r"```.*?```", re.DOTALL)   # 围栏代码块
INLINE_RE = re.compile(r"`[^`]*`")               # 行内代码
URL_RE = re.compile(r"https?://\S+")


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass


def last_assistant_text(transcript_path):
    """取 transcript 最后一条 assistant 消息的正文（拼接所有 text 块）。"""
    try:
        with open(transcript_path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return ""
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            return "\n".join(p for p in parts if p)
        return ""
    return ""


def scan(text):
    """剥离代码/URL 后扫描，返回命中的违禁项列表。"""
    prose = FENCE_RE.sub(" ", text)
    prose = INLINE_RE.sub(" ", prose)
    prose = URL_RE.sub(" ", prose)
    hits = []
    for term in BLOCK_TERMS:
        if term in prose:
            hits.append(term)
    syms = sorted(set(SYMBOL_RE.findall(prose)))
    hits.extend(syms)
    return hits


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # 读不到输入，放行

    sid = data.get("session_id", "default")
    tpath = data.get("transcript_path", "")
    if not tpath:
        sys.exit(0)

    text = last_assistant_text(tpath)
    if not text:
        sys.exit(0)  # 纯工具轮，无正文

    hits = scan(text)
    state = load_state()

    if not hits:
        if sid in state:  # 这一轮干净，清计数
            state.pop(sid, None)
            save_state(state)
        sys.exit(0)

    count = int(state.get(sid, 0))
    if count >= MAX_BLOCKS:
        state.pop(sid, None)  # 到顶放行并复位，避免锁死
        save_state(state)
        sys.exit(0)

    state[sid] = count + 1
    save_state(state)

    reason = (
        "本次回复命中表达约束的禁用项：" + "、".join(hits) + "。"
        "请改写上一条回复，去掉这些词/符号，用克制的书面表达重述同样的信息，"
        "不要解释为什么改、不要道歉，直接给改写后的版本。"
    )
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
