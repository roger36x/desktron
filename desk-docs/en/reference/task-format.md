# Workflow and task data contract

Workflows provide context, tasks track to-dos, and sessions keep execution history. A session is not bound to a single task and is not grouped by task. Importing an existing session only adds a reference and never rewrites the original directory or transcript. The task bar on the session page shows all open tasks of the workflow; this does not mean the current session is linked to each of them. Only starting from a task's "New session" entry, or the Agent running claim/progress with a stable task id, creates a many-to-many link in the private ledger, which recap uses to close tasks under strict completion conditions.

## Workflows

[workflow/api.py](../../../desktron/lib/workflow/api.py) manages `/workbench/domains`, reference material, and repository relationships. Repositories can be outside the workflow directory; the session's cwd is still the workflow directory, and code is accessed by the absolute path of the bound repository. Desk does not create branch worktrees automatically; the Agent decides on branches per task. The id and directory are validated before registration, and a display name must never be used as a path or branch.

## Task lines

`TASKS.md` is the source of truth shared by the panel and the Agent. `[ ]`, `[~]`, and `[x]` mean to do, in progress, and done. Line numbers start at **0**. Updates pass both `line` and the original title as `expect`; a mismatch returns a conflict, and the caller re-reads and retries.

The global task bar aggregates every workflow through `/workbench/tasks?scope=all`, without copying into `_desktop`. The AI maintains tasks with `desk-task create|claim|progress|block|complete|release`; work that is multi-step, spans turns, is asynchronous, is blocked, or produces artifacts should be recorded, while short questions and answers are not.

Run `desk-task list` before recording; reuse the existing `t` for the same deliverable instead of creating a new task for each turn's wording. Before sending the final answer, re-check the tasks actually advanced in this turn and update the result, next step, or blocker explicitly; complete only when acceptance is satisfied, and something not deployed must not be described as live. If the current session finishes deployment, a restart, or acceptance left by another session, it still updates or closes the original task id rather than leaving it behind because the session differs. Updates use stable ids; same-name ambiguity or stale ids must be re-read and must not degrade into creating a new task. Completed tasks are restored to to-do only explicitly by the user.

Recap only adds progress, blockers, or completion results to existing task ids and never runs create/claim directly. Newly discovered items stay in the log first and are recorded by the main session after verification. Task details directly show the bounded ledger's current snapshot, completion conditions, and linked sessions; these fields are shown separately from the memo and are never spliced into or rewrite the memo text. The memo is optional long-form supplementary material; execution history still belongs to the journal and is not poured into task details or memos.

The private sidecar `DATA_HOME/task-ledger.json` stores only a bounded current state: `done/next/blocker`, revision, the closing reason, idempotency receipts, and the 5 most recently linked sessions; tasks and sessions are many-to-many. Execution history belongs to the journal and is not appended to the sidecar or the task memo. Precedence is user/UI > explicit Agent operations > recap filling gaps > old state; a UI change advances the revision, invalidating earlier recap snapshots and preventing later ordinary progress/block calls from overwriting the user's state. A linked task still open may be closed automatically by an updated recap with a high-confidence result and no next step or blocker; a task the user completed is never revived by recap.

Fields in the end-of-line comment:

| Field | Meaning |
| --- | --- |
| `t` | Stable task id |
| `c`, `d` | Creation and completion times; reopening clears d |
| `note` | Linked memo id |
| `flow` | Stage and node declaration |
| `fdone` | Coordinates of completed nodes, comma-separated |
| `nl` | Links between nodes and task ids, comma-separated |
| `fcur` | Old stage cursor, read only for compatibility |

## Task evidence pilot

Explicit completion conditions, structured evidence, `show/review`, and end-of-turn rules are described in the [task evidence pilot](task-evidence.md). Tasks without a contract keep the original flow above.

## Task flow coordinates

v2 `flow` separates stages with a native `|` and parallel nodes within a stage with `+`; node names are URL-encoded individually. Stages and nodes both start at **0**, and coordinates are written `stage.node`, not task line numbers.

```text
flow:Plan|Build+Review|Ship
fdone:0.0,1.0
nl:1.0=task_a,1.1=task_b
```

This example has three stages, with two nodes in the second; `1.0` is Build. Node names containing separator characters must be encoded. Old v1 is a linear chain URL-encoded as a whole string, and the parser remains compatible with it.

Changing `flow` clears old `fdone` and `nl`, because coordinates may change; pass the new links at the same time to keep updated progress. Once `nl` is set, the old `fcur` is retired. Ticking a node in a later stage in the UI fills in the earlier stages; be clear about this behavior before writing comments directly.

Implementation: [tasks.py](../../../desktron/lib/tasks.py), [front-end parsing](../../../frontend/app-panel-tasks-page.js). The development verification entry point is `docs/workflow-task-model.md` in the source repository.
