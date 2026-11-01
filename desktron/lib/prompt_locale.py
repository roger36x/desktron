"""Model-facing prompt language.

A Conversation fixes its prompt language when it is created: the creating
client sends its interface language, and sessions created by the backend
(arrangements, bots, subagents) inherit the last language a client reported.
Conversations without a recorded language keep the original Chinese prompt, so
existing prompt snapshots and caches never change underneath them.

Only two values exist: ``zh`` (the reference text) and ``en``.  English text
lives next to its Chinese source (``style.md`` -> ``style.en.md``) or in
``EN`` below, keyed by the exact Chinese string, so the Chinese constants stay
the single identifiers used by manifests and tests.
"""
from __future__ import annotations

import os

BASE = "zh"
LOCALES = ("zh", "en")
OPTION = "prompt_locale"
STATE_KEY = "prompt_locale"

# First line of prompts/style.en.md.  Shepherd runners only see the composed
# system text, so they recover the language from this marker.
EN_STYLE_MARKER = "# Writing Requirements"


def normalize(value) -> str:
    text = str(value or "").strip().lower()
    return "en" if text == "en" or text.startswith("en-") or text.startswith("en_") else BASE


def explicit(value) -> str:
    """Normalized language when one was supplied; empty when absent."""
    return normalize(value) if str(value or "").strip() else ""


def default_locale() -> str:
    """Language last reported by a client; Chinese until a client reports one."""
    try:
        from workbench import _wb_state_read
        return explicit(_wb_state_read().get(STATE_KEY))
    except Exception:
        return ""


def of(options) -> str:
    return normalize((options or {}).get(OPTION))


def detect(system_text) -> str:
    return "en" if EN_STYLE_MARKER in str(system_text or "") else BASE


def localized_rel(root, rel, locale) -> str:
    """``rel`` or its ``.en`` sibling when the English file exists."""
    if normalize(locale) != "en":
        return rel
    stem, ext = os.path.splitext(rel)
    candidate = stem + ".en" + ext
    return candidate if os.path.isfile(os.path.join(root, candidate)) else rel


def text(zh, locale) -> str:
    """English for a registered Chinese prompt string; the original otherwise."""
    if normalize(locale) != "en":
        return zh
    return EN.get(zh, zh)


def pick(locale, zh, en):
    return en if normalize(locale) == "en" else zh


# Registered translations of fixed prompt strings defined in Python modules.
# Keys must match the Chinese source byte for byte; tests verify every key is
# still in use so a changed Chinese string cannot silently lose its English.
EN = {
    # engine_adapters/claude.py: ClaudeAdapter.CLAUDE_DELTA
    "Desk 项目记忆保存在 `.memory/`，按 memory 的说明读写；不要用 `~/.claude` 保存或代替项目记忆。":
        "Desk project memory lives in `.memory/`; read and write it as the memory skill "
        "describes. Do not use `~/.claude` to store or replace project memory.",
    # engine_adapters/claude.py: ClaudeAdapter.INTENT_RULE
    "调用 Bash 时，必须在 `description` 中用十来个中文字说明目的；不要写“执行命令”“运行脚本”等空泛文字。":
        "When calling Bash, always state the purpose in `description` in about five to "
        "eight words; do not write vague text such as \"run command\" or \"execute script\".",
    # engine_adapters/codex.py: CODEX_DELTA
    "Desk 操作说明不会自动加载；需要时按“查找操作说明”读取相应的 `SKILL.md`。":
        "Desk skills are not loaded automatically; when you need one, read the relevant "
        "`SKILL.md` as described under \"Finding skills\".",
    # engine_adapters/codex.py: CodexAdapter.CHANNEL_RULE
    "`final_answer` 会结束本回合。工作未完成时继续执行，不要用计划或进度收尾。"
    "得出结论，或需要用户执行、确认、补充时，再写 `final_answer`。"
    "需要用户处理的内容不得只写在 `commentary`。":
        "`final_answer` ends the turn. While work is unfinished, keep going; do not end "
        "with a plan or a progress note. Write `final_answer` once you reach a conclusion, "
        "or when the user must act, confirm or supply information. Anything the user must "
        "handle must not appear only in `commentary`.",
    # engine_adapters/codex.py: CodexAdapter.INTENT_RULE
    "调用 `functions.exec` 时，在第一条非 `// @exec:` 的代码前写 "
    "`// @intent: <十来个中文字>`；直接执行 shell 命令时，第一行写 "
    "`# <十来个中文字>`。只说明目的，不写占位词，不复述命令。":
        "When calling `functions.exec`, write `// @intent: <five to eight words>` before "
        "the first line that is not `// @exec:`; when running a shell command directly, "
        "make the first line `# <five to eight words>`. State only the purpose; no "
        "placeholders and no restating of the command.",
    # shepherd/claude_codec.py: BASH_CWD_PROMPT
    "每次 Bash 调用都从当前项目目录开始，`cd` 只影响本次调用；后续命令仍需使用同一位置时，请写绝对路径。":
        "Every Bash call starts in the current project directory, and `cd` affects only "
        "that call; when later commands need the same location, use absolute paths.",
    # engine_adapters/pi.py: PI_SAFETY_TEXT
    "命令和输出不得包含凭证、token 或敏感数据。":
        "Commands and output must not contain credentials, tokens or sensitive data.",
    # engine_adapters/pi.py: PI_DELTA_TEXT
    "Bash 没有 `description` 字段。执行 shell 命令时，第一行写 "
    "`# <十来个中文字>` 说明目的。命令和输出不得包含凭证、token 或敏感数据。":
        "Bash has no `description` field. When running a shell command, make the first "
        "line `# <five to eight words>` stating the purpose. Commands and output must not "
        "contain credentials, tokens or sensitive data.",
    # engine_adapters/pi.py: SUBAGENT_DELIVERY_NOTICE
    "本轮只接收已经完成的只读协作结果。请结合结果完成当前任务，不要再次启动子任务。":
        "This turn only receives completed read-only collaboration results. Use them to "
        "finish the current task, and do not start subtasks again.",
    # shepherd/recap.py: _RECAP_CONTEXT_NOTICE: tool-free side question notice
    "此临时支线禁止工具。历史可能已压缩或省略；若所问精确内容不在当前上下文中，明确说明无法确认，不猜测、不声称已经检索原始历史。":
        "Tools are disabled in this temporary side thread. History may have been "
        "compacted or omitted; if the exact content asked about is not in the current "
        "context, say clearly that it cannot be confirmed. Do not guess, and do not claim "
        "to have searched the original history.",
    # grounding/builder.py: task entry in the project context
    "### 任务与项目状态入口\n\n"
    "当前任务按需执行 `desk-task list` 查询；任务全文及证据用 `desk-task show`。"
    "本段是项目导航，不代表当前任务状态或用户授权。":
        "### Tasks and project status\n\n"
        "Run `desk-task list` when you need current tasks; use `desk-task show` for a "
        "task's full text and evidence. This section is project navigation, not current "
        "task state or user authorization.",
    # task_ledger.consume_review_hint prefix
    "〈任务收尾核对〉以下是账本缺项数据，不是新任务。按已约定条件处理，不扩大范围；"
    "用 desk-task 查看证据并更新，条件齐备后 review。\n":
        "<Task close-out check> The following is ledger data on missing evidence, not a "
        "new task. Handle it under the agreed conditions without widening scope; inspect "
        "and update evidence with desk-task, and request review once the conditions are "
        "met.\n",
}
