# Subagents and result notifications

The main Pi Agent can hand independent read-only tasks to subagents, such as searching code, checking changes, or gathering evidence. They inherit the parent session's model, thinking level, and working directory; the capabilities available depend on whether the local dependencies are prepared.

## Running and viewing

Synchronous tasks wait for their result inside the tool call. Asynchronous tasks first return a run id so the main Agent can continue independent work. By default an asynchronous task is a required dependency of the current main call: finished results enter the context at a safe boundary, and remaining results are awaited and integrated before the main call ends, without starting a separate notification turn. When the parent Agent needs a result to continue, it can block on a given `run_id` with `action=wait`, without polling repeatedly.

Only independent background tasks explicitly set with `required=false` keep the behavior of sending a separate notification on completion. Review and acceptance before completion must not use this mode. The parent Agent can still query status or cancel a given run; querying status does not consume the result. Details show queued, running, and final states; a queued notice does not wake the main Agent by itself.

Only controlled read-only roles and tools are available for now. Subagents cannot edit files, perform write operations, or spawn further subagents, and receiving their results does not count as user approval. When a task fails, results from other tasks in the same batch are kept.

Time limits are set centrally by Desk: a subagent with no progress for 5 consecutive minutes, or running 30 minutes in total, is terminated, not merely no longer awaited. Time spent queued or executing tools does not count as no progress. Termination for no progress is recorded as a failure, and any partial results are returned.

## Result messages

Asynchronous results from independent background tasks (`required=false`) reuse the Agent message style of `desk-msg`, with a title showing the Pi subagent and its completion state. Long text is collapsed by default and reads as Markdown when expanded; the run id for a single batch is kept in the title tooltip. Old history, live results, and batched results follow the same display rules.

Results that pile up close together can be merged into one notification. Notifications are still processed through the subagent's own persistent delivery queue; the message style does not mean they go through `desk-msg`. Notification turns do not offer the `subagent` tool; the main Agent should integrate the results and close out the current task.

## Behavior after interruption

A user interruption establishes a persistent cancellation boundary, requests that in-flight subagents stop, and withdraws result notifications not yet dispatched. Results from before the boundary, even if they succeed later, are kept in the subagent record and no longer wake the main session.

Cancellation still needs confirmation; a main turn that has already started or an operation already completed cannot be taken back by hiding a notification. If a result shows as unconfirmed, check the original run id and delivery state first, and do not start the same task again.

When the original session temporarily switches away from Pi, results are kept and checked for deliverability after switching back; they are never sent directly to another engine. After a restart, the original records and receipts are read, and historical subtasks are not restarted. Dependencies of a turn are bound to the original main call and do not inject a new user request automatically. Results whose delivery was proposed but whose consumption cannot be proven stay unconfirmed and do not fall back to a separate notification automatically. Query by the original `run_id`; dependencies not yet delivered can be awaited explicitly for recovery.

For other ways of collaborating see [Sessions and collaboration](sessions.md); for troubleshooting see [Logs and fault reports](diagnostics.md).
