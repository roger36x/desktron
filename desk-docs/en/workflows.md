# Workflows and tasks

A workflow holds the background, reference materials, repository entry points, and tasks that a project needs over time. Sessions move work forward inside a workflow, and long-term state is carried by tasks and memory.

## Creating a workflow

Click the plus sign next to "Workflows" in the sidebar, enter a name, and press Enter; this menu does not import existing directories. After creating it, describe its purpose and add project reference materials. Bind existing repositories when you need to work on code; repositories may live outside the workflow directory. Binding does not generate any Desk scaffolding in external repositories.

A workflow can reference existing sessions. Importing a session only adds a reference; it does not change the session's original working directory, engine history, or ownership. When several branches are developed in parallel, choose the matching repository and worktree explicitly, and do not mistake the workflow name for a Git branch.

## Task list

The panel and the Agent edit the same `TASKS.md`:

| Marker | State |
| --- | --- |
| `- [ ]` | To do |
| `- [~]` | In progress |
| `- [x]` | Done |

Task details show the task's own lightweight state first: the title is the goal, and `done/next/blocker` mean what is finished, the next step, and the blocker; when completion conditions are enabled, acceptance items are shown too, and linked sessions can be opened directly. Task streams split work into stages. Memos are optional supplementary material, suited to long background, proposals, and reference links; task tracking works without them.

The Agent maintains tasks with `desk-task create|claim|progress|block|complete|release`. `done/next/blocker` each hold a current snapshot of at most 160 characters; the detailed process goes to the journal. Edits the user makes in the interface take precedence; on a revision conflict, re-read first and do not overwrite.

An HTML comment at the end of each line stores the task id, times, task stream, and memo link. Keep existing fields; low-level line edits also check the line number and the original title. The exact fields are in the [task data contract](reference/task-format.md).

A task can explicitly enable [completion conditions and evidence](reference/task-evidence.md): list the commits, tests, deployments, acceptance checks, or artifacts that the requirement calls for. Once enabled, completion conditions must not grow as work progresses; when all evidence is present and there is no next step or blocker, the normal end of a turn can verify and close it automatically. Old tasks do not adopt this pilot automatically.

## Background and state

Put reference materials in the workflow's context entry point, and manage long-term constraints through the memory skill. The automatically generated full project model goes straight into the session context to restore background; it cannot replace task acceptance, and "in progress" must not be treated as verifiable progress; state a version, date, or count of completed items.

More: [Sessions and collaboration](sessions.md), [Knowledge, skills, and memory](knowledge-memory.md).
