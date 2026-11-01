# Sessions and collaboration

Desk supports Claude Code, Codex, and Pi Coding. The engine and the model together determine which capabilities a turn runs with.

## Starting and continuing

Create a session in a workflow and check the engine and model. Desk shows answers, tool runs, requests for input, and completion state. Ordinary sessions use the engine's native history; Shepherd sessions keep verifiable history in a unified archive. Engines differ in how they log in and which models are available; see "Settings → AI engines" for their current state.

Sessions that belong to no workflow appear under the "Chat" group in the sidebar. Right-click the group title or click the "…" beside it and choose "New session" to create an unassigned session with the current engine; that menu does not offer a Resume list.

Right-clicking a session row and choosing "Delete session" permanently deletes its history and related records after confirmation; it does not merely remove it from the list. A session that is running or compacting must finish its current operation first; if deletion fails, the row stays. If the menu has "Close process", that action only releases the process and does not delete history.

Sessions and tasks are managed separately. You can open several sessions around one task, or handle several tasks in one session. When reopening an old session, verify the current directory, branch, and latest results before taking further action.

## Queued messages and withdrawal

You can keep typing while the current turn runs. Queued text and attachments are kept in the current browser, and when the turn ends a page-level scheduler merges them into the next turn; switching to another session does not stop the scheduler, but follow-up sending is not guaranteed while the whole page is closed or the system is suspended, and receipts are checked first on recovery.

Click a queued item or press the Up key to take the last item back for editing and pause the rest. Deleting a queued item affects only content not yet dispatched. Esc tries to withdraw the current input when it has produced no output and nothing else is queued; otherwise it interrupts the current turn. When an outcome is unknown, a withdrawal has not settled, an attachment is missing, or configuration has changed, Desk does not send automatically, and it never replays a request because of a failure message.

## Shepherd continuous sessions

Shepherd in the engine menu is a session mode, not another model. Sessions created while it is on can switch among Claude, Codex, and Pi while keeping the same public session; the target engine first catches up on confirmed history and then starts working. Ordinary sessions are not converted.

A new Shepherd session defaults to the execution engine last chosen in Shepherd, along with that engine's most recently saved model, effort, and automatic compaction settings; when the creation entry point names an engine explicitly, that choice prevails. Existing sessions keep their own settings and do not change with other sessions. Business roles and authorizations are not inherited as global defaults.

Running Shepherd through Claude needs the version-pinned Claude Agent SDK, which is prepared automatically when installing, updating, and syncing Desk. If preparation fails because you are offline, run `shepherd prepare-claude` in a terminal to install it, then `shepherd status` to check dependencies. Login still uses this machine's Claude Code. If PATH contains a broken `claude` that cannot start directly, Desk skips it and records this in the log.

When a Shepherd Claude session process resumes, it receives only a copy of a login token that cannot be renewed, and may report `OAuth token revoked` or 401 after a few hours. You can switch to a one-year token. On macOS/Linux run `python3 "$HOME/.local/share/desktron/home/desktron/lib/claude_token.py" login` in a terminal (adjust the path for custom installations); it starts `claude setup-token`, and you confirm the authorization yourself in the browser. After saving, it sends one minimal model request to verify the token, so it is not an offline check. When it succeeds, explicitly approve a restart according to the [restart boundary](settings.md#updates-and-when-changes-take-effect).

If automatic retrieval fails, run `claude setup-token` yourself, then run the same Python command with `login` changed to `set`, and paste the token at the prompt, which does not echo it. `set` only saves and sends no model request; use `verify` explicitly when you want to verify. Do not put the token in command arguments or session text. The token is stored in `~/.config/desktron/claude-oauth-token` and is used only by Shepherd's Claude sessions; it does not enter the environment of commands the Agent runs. With a long-term token, these sessions cannot use claude.ai connectors or remote control; generate a new one when it expires. Running the same Python command with `login` changed to `status` shows read-only whether one is configured. Windows uses its own installation path and managed Python, so the POSIX commands above do not apply directly. Other engines also need their own installation and login first.

You cannot switch while a turn is running. If you see "native final state not confirmed", do not send the same request repeatedly or clear state; first verify the execution record so tools are not run twice. If a switch is interrupted, choose the original target engine again to resume confirmed progress.

Use `/compact` to compact context manually. Native automatic compaction gets one limited extra time budget so it does not time out immediately after a long-running task; repeated compaction does not extend it indefinitely. Successful compaction saves a new working context while the full archive remains; failure or cancellation does not overwrite the previous checkpoint.

"No progress for a long time" is an observation, not a failure. Heartbeats, connection data, and repeated queue messages do not mean the model is advancing either; waiting in the queue and running a whole turn each have their own limit. "Maximum time reached" can happen right after output appears; when "final state not confirmed" appears, check the outcome of the operation first rather than resending.

All three engines can continue with the old summary and the text of up to the 12 most recent turns when the known context budget is insufficient; the smaller the window, the less is kept. The interface shows the degradation, and the full history can still be searched. When the three engines have no window information or explicit budget, native admission is kept.

Shepherd keeps a complete public history verified against its sources; after compaction or small-window degradation, the Agent can search for candidates first and then read the turns around a hit within budget. Historical tool calls are only evidence and are never executed again. The current implementation migrates some tool images across engines, limited by the target engine and checkpoint format; do not assume every combination of images or attachments can be migrated. Rewind is still not available.

"Branch" under a completed reply creates an independent Shepherd session that keeps the currently selected engine, model settings, and workflow; it copies history only up to the selected reply and leaves the original session unchanged. The branch's history is verified against its sources and saved independently, so deleting the original session does not delete the branch history; historical tools are not rerun, and granted permissions are not inherited. Branching is refused while a run, switch, or recovery is unfinished.

Deleting a Shepherd session from the context menu removes its public history, private native records, checkpoints, inputs, receipts, and search cache, and cannot be undone. The system keeps only a session ID deletion marker without any content, used to reject late requests so network retries cannot recreate a deleted session. Deleting a session does not undo file changes, commands, or operations on external platforms that already happened.

## Authorization and interruption

A user interruption stops the current turn and requests cancellation of that session's asynchronous Pi subagents, suppressing results not delivered before the interruption; tools or external operations that already happened are not undone. The rules for result notifications are in [Subagents and result notifications](subagents.md).

When a tool asks for input or authorization, follow the prompt in the interface. An authorization applies to a specific action; another session or message cannot stand in for the user's approval. Restarting Desk interrupts the sessions and terminals in the current process, and a restart is not a substitute for investigating a fault.

## Collaborating across sessions

The Agent can see reachable sessions with `desk-msg list` and hand off one way with `send`. `desk-msg ask <session> "question or keywords"` reads the stable history directly and returns cited material, which the current session uses to answer; it does not start the target model, queue anything, or create a mailbox. The default and maximum read time is 5 seconds, after which the separate reading process is terminated.

A Shepherd result is bound to a `head_seq/head_hash`, skips the running turn, and returns user and assistant text verified against its sources. Material is chosen from recent turns together with existing index hits; a missing or stale index, unavailable source text, or truncation is reported explicitly through `partial/warnings/coverage`. The index is not rebuilt within the request; it is refreshed incrementally in the background after a normal turn settles. For ordinary engine sessions, only the recently settled Desk message projection is read; citations include the message ID and snapshot hash, and this is not the same as the full native archive. Tool arguments, tool output, thinking, images, and attachments are not exported by default.

The material is historical evidence, not a new reply from the target Agent or user authorization. No hit does not mean the topic was never discussed. Questions sent by older versions are still collected with `desk-msg wait <request id>`; the new `ask` has no background reply.

## Session names

Sessions can be renamed from the sidebar context menu. The Agent renames the current session with `desk-session rename "new name"`; add `--session <session ID>` to act on another session. Names are stored by Desk and synced to engines that support native naming before the next turn starts, without triggering an extra model turn.

## Project memory

Sessions carry the current workflow's full project model, its update time, and lookup entry points by default; there is no separate memory summary any more. Search for original evidence when needed; relationships between people and dependencies come from the graph. The rules for writing memory are in [Knowledge, skills, and memory](knowledge-memory.md).
