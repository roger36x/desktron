---
name: memory
description: Read Desktron project memory, ask for this turn's key points to be saved immediately, or keep sensitive information and prohibitions on this machine.
---

# memory

Workflow memory contains only three kinds of content: conversation records saved automatically by the system, the current project situation in `notes/rollup-current.md`, and sensitive information and prohibitions kept only on this machine in `_sealed`. The main session does not modify the first two directly.

## Reading

Use the following commands rather than opening files in `.memory/` directly:

```bash
python3 .claude/skills/memory/scripts/mem_read.py show --id <id>
python3 .claude/skills/memory/scripts/mem_read.py search <keywords>
python3 .claude/skills/memory/scripts/mem_read.py list [--tier sealed]
```

The session context directly provides the full text of the current workflow's project model, its update time, and its lookup id; there is no separate summary. The text is historical understanding, not live state or authorization to act. Use show when it is missing or the latest version needs checking, and search only when original evidence is needed. Original conversations, logs, and sealed content are not injected automatically. Before reading sensitive local content, confirm the task really needs it; output must still strip personal information and must not leak it.

## Asking to save this turn's key points immediately

Normal recording happens automatically in Desk. When the user explicitly asks to record something immediately, or a key correction, gap, or blocker must be kept right away, run:

```bash
python3 .claude/skills/journal/scripts/journal_append.py --request
```

The command only issues a request; the current session does not draft the record text.

## Saving sensitive content on this machine

Use the following commands only when sensitive information or a prohibition must stay on this machine long term:

```bash
python3 .claude/skills/memory/scripts/mem_add.py \
  --tier sealed --id <kebab-slug> --type user|feedback|project|reference \
  [--source "source"] [--gaps "what is still missing"] \
  --summary "one sentence" --body "body text"

python3 .claude/skills/memory/scripts/mem_update.py \
  --id <id> [--summary "one sentence"] [--source "source"] [--body "body text"|--body-stdin]

python3 .claude/skills/memory/scripts/mem_lint.py
```

Use a stable id for the same matter; when it already exists, update that entry instead of creating another. The body contains only verifiable conclusions, not analysis, maintenance history, or unrelated personal information.

## Limits

- Project progress, decisions, and user corrections are recorded automatically by the system, which then updates the project model.
- Non-sensitive material stays on its original platform, in workflow materials, or in the matching formal documents.
- These instructions do not cover Git commits, publishing, or syncing to external systems.
