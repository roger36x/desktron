---
name: journal
description: Ask Desk to save this turn's key points immediately, when the user explicitly asks for a record or a key correction, gap, or blocker must be kept right away.
---

# journal

Normally Desk saves key points automatically after each turn. Run the following only when the user explicitly asks to record something immediately, or a key correction, gap, or blocker cannot wait:

```bash
python3 .claude/skills/journal/scripts/journal_append.py --request
```

The command only issues a save request for this turn and accepts no text. The current session must not edit memory files directly or draft the saved content itself.

Older records and the current project situation are consolidated by the system periodically; ordinary tasks do not need to compact or move files by hand.
