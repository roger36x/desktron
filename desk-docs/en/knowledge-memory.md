# Knowledge, skills, and memory

Knowledge provides citable material, skills provide procedures, and memory restores project background. Each has its own entry point and way of being maintained.

## Knowledge and skills

"Agents → Knowledge" manages knowledge connections. Git knowledge bases are maintained from their registration information for use by every workflow. Desk shows connection, version, and sync status, while the original content is still maintained on its source platform.

"Agents → Skills" shows installed skills and their sources, and lets you preview marketplace skills before installing them. Skill usage records come from several channels, and the counts cover only calls that can actually be observed; a zero count does not mean a skill was never used.

Before reading external material, confirm that the access path works. Material that goes unused may be unsearchable or not mounted; do not first attribute it to poor quality.

## Project memory

| Content | Purpose | How it is maintained |
| --- | --- | --- |
| Conversation records and journal | Original evidence, and this turn's facts, corrections, and blockers | The system saves conversations, and an isolated recap appends to the log |
| notes / project model | Current project situation | The memory engine rewrites rollup-current.md |
| sealed | Sensitive information and prohibitions kept only on this machine | Maintained through the memory skill and never distributed |

Sessions carry the current workflow's full project model text, its update time, and lookup id by default; no separate memory summary is stored or shown. Original conversations, the journal, and sealed content are not injected automatically; search them when original evidence is needed. The project model is historical understanding; it cannot replace verification of live state and does not constitute authorization to act.

Right-click a workflow title in the sidebar and choose "View memory" to read the same project text, divided into project overview, decided approaches, long-term constraints, and open questions; old English headings are shown in these four categories too. The lookup id stays at the bottom, and there is no separate "Session summary" section any more. This view is read-only and never rewrites the original records. The summary field of old models is no longer used and is removed when the memory engine successfully rebuilds the model.

Project memory is rewritten periodically by the memory engine, using Desk's shared writing requirements. Project progress, decisions, and user corrections are recorded by the system, which then updates the project model; the main session does not modify conversations, logs, or the project model directly. Only sensitive information or prohibitions that must stay on this machine are written to sealed through the memory skill. Relationships between people and dependencies are looked up only in the graph, never inferred from conversations or notes.

When something must be kept explicitly, have the Agent request a recap through the journal skill; the current session does not draft the log text. To correct an append-only record, append a new entry that names the content being corrected; withdrawn judgments should keep the reason for withdrawal.

Do not edit memory's physical files directly or copy them into release packages. The lower layer still supports old pinned / fact entries in `sot`: confirmed judgments keep their original priority, unconfirmed judgments are not promoted automatically, and facts are used only for knowledge search; this is not an entry point that asks the main session to add pinned entries. Existing records are not deleted just because this guide narrows memory to three kinds of content.
