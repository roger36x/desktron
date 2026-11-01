---
name: make-arrangement
description: Create a scheduled or manual arrangement in Desktron. Confirm what to run and when first, then create it as described.
---

# Create an arrangement

An arrangement is a first-class object. By default Desk's current engine runs it; the user does not need to create an Agent first.

## Procedure

1. From the conversation, confirm "what to do each time" and how it is triggered. If required information is missing, keep asking; do not guess times.
2. Use exactly one of these triggers:
   - `manual`: manual only; no time needed.
   - `once`: a given date, once only; needs `--date YYYY-MM-DD --time HH:MM`, which must be in the future.
   - `interval`: at a fixed interval; needs `--minutes 1..10080`; timing starts when saved or resumed.
   - `daily`: every day; needs a local time `HH:MM`.
   - `weekdays`: working days; needs a local time `HH:MM`.
   - `weekly`: every week; needs a weekday `0` to `6` (Monday is `0`) and a local time `HH:MM`.
3. Before actually creating it, restate the task and trigger time to the user in one sentence and get confirmation. If the user already gave complete information in the same turn and asked to create it immediately, you may proceed directly.
4. Call the script. By default, do not pass `--agent-id`, so Desk runs it:

```bash
python3 .claude/skills/make-arrangement/scripts/create_arrangement.py \
  --instruction "Summarize yesterday's project changes and list issues to handle" \
  --kind weekdays --time 09:00
```

Weekly example:

```bash
python3 .claude/skills/make-arrangement/scripts/create_arrangement.py \
  --name "Friday review" --instruction "Summarize this week's progress" \
  --kind weekly --weekday 4 --time 17:00
```

Times are interpreted in the local time zone of the selected runtime; if the user names another time zone, check the runtime's time zone before converting. After the script succeeds, report the name, how it runs, and the time. Do not create a separate chat session for scheduled runs; results appear on the Arrangements page.

## Limits

- Pass `--agent-id` only when the user explicitly names an existing Agent or Feishu Bot. Verify the code name and runtime through `/workbench/lark-bot/agents`; do not guess code names or create Agents on the user's behalf.
- Binding a Feishu Bot reuses its engine, directory, and context settings; results return to the Arrangements page and are not sent to Feishu automatically.
- Only the local Desk API is supported for now; `--backend` selects a connected runtime.
