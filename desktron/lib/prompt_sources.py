"""Stable Prompt source IDs mapped to their current implementation locations.

Prompt manifests refer only to these semantic IDs.  Refactors update this one
registry; delivery policy and governance declarations remain unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSource:
    kind: str
    path: str = ""
    symbol: str = ""
    resolver: str = ""
    provider: str = ""
    description: str = ""


SOURCES = {
    "desk.style": PromptSource("file", path="prompts/style.md"),
    "desk.role": PromptSource("generated", path="desktron/lib/engine_adapters/cli_store.py",
                              resolver="SessionStoreMixin.agent_body"),
    "desk.grounding": PromptSource("generated", path="desktron/lib/grounding/builder.py",
                                   resolver="constant"),
    "desk.scene": PromptSource("generated", path="desktron/lib/grounding/builder.py",
                               resolver="scene"),
    "desk.task-review": PromptSource("generated", path="desktron/lib/task_ledger.py",
                                     resolver="consume_review_hint"),
    "claude.memory-boundary": PromptSource("python_symbol",
        path="desktron/lib/engine_adapters/claude.py", symbol="ClaudeAdapter.CLAUDE_DELTA"),
    "claude.tool-intent": PromptSource("python_symbol",
        path="desktron/lib/engine_adapters/claude.py", symbol="ClaudeAdapter.INTENT_RULE"),
    "claude.bash-cwd": PromptSource("python_symbol",
        path="desktron/lib/shepherd/claude_codec.py", symbol="BASH_CWD_PROMPT"),
    "codex.channel": PromptSource("python_symbol",
        path="desktron/lib/engine_adapters/codex.py", symbol="CodexAdapter.CHANNEL_RULE"),
    "codex.tool-intent": PromptSource("python_symbol",
        path="desktron/lib/engine_adapters/codex.py", symbol="CodexAdapter.INTENT_RULE"),
    "codex.skill-discovery": PromptSource("python_symbol",
        path="desktron/lib/engine_adapters/codex.py", symbol="CODEX_DELTA"),
    "pi.runtime-rules": PromptSource("generated",
        path="desktron/lib/engine_adapters/pi.py", resolver="runtime_prompt_rules"),
    "shepherd.history": PromptSource("generated", path="desktron/lib/shepherd/history_prompt.py",
                                     resolver="history_prompt"),
    "claude.native-default": PromptSource("native", provider="Claude Code",
        description="Claude Code 按交互方式、工具清单和安装版本生成的原生系统提示。"),
    "claude.native-context": PromptSource("native", provider="Claude Code",
        description="CLAUDE.md、SessionStart hook、技能列表、环境更新、用户身份与署名等原生附件。"),
    "codex.native-default": PromptSource("native", provider="Codex app-server",
        description="引擎自行维护的默认提示、Plan 模式说明和工具协议规则。"),
    "pi.native-default": PromptSource("native", provider="Pi Coding Agent",
        description="Pi 原生提示、工具协议和工作目录；技能与上下文文件自动发现已关闭。"),
}


def source(source_id: str) -> PromptSource:
    try:
        return SOURCES[source_id]
    except KeyError as exc:
        raise KeyError(f"unknown Prompt source id: {source_id}") from exc
