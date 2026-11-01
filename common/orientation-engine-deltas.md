# 每引擎 Prompt 增量（决策记录）

> 引擎专属提示不能进入共享的 `orientation.md`。共享层由各引擎共同使用，单引擎规则放入对应适配器，避免向其他引擎注入无效要求。
> 现有差异逐项评估：只有确实引擎专属、且共享上下文未覆盖的内容才注入。

## claude —— 已接

- `~/.claude` 的全局记忆（Claude 自带、跨项目）≠ Desk 的 `.memory/`（本工作台记忆）。Desk 记忆只经 memory skill 的受控入口读写，不与 `~/.claude` 混用。
- 注入点：常量 `ClaudeAdapter.CLAUDE_DELTA`（`engine_adapters/claude.py`），由 `--append-system-prompt` 追加。只注给 claude，不进 AGENTS.md。resume 路径不重注——claude 续接保留起手时的 system prompt（若实测不保留再补）。
- claude 有原生 skill loader、自动读 `.claude/skills/`，这条不必提（本就拿得到）。

## codex —— 已接（2026-08-21 从 AGENTS.md 迁出）

- 正文：「本仓的 skill 放在 `.claude/skills/`，你不会自动加载它们。动手之前先 `ls .claude/skills/`，读各 `<name>/SKILL.md` 的开头声明，判断哪个适用，再读它的正文照着做。」
- 迁出理由：这条只对读不到本仓 skill 的 Codex 有意义，而共享 grounding 由各引擎共同使用；其他引擎具备不同的技能加载路径，读到它会形成噪音。
- 前提已实测：codex 有自己的 `~/.codex/skills/`，读的是那个目录，本仓这份不会被它加载，所以这条仍然成立。
- 注入点：模块常量 `CODEX_DELTA`（`engine_adapters/codex.py`），拼进首轮 seed。
