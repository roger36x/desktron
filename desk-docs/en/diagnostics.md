# Logs and fault reports

Preserve the scene of a failure first, then decide how to retry. The last stage in the logs and the command's exit result locate a problem more easily than a single "failed" screenshot.

## Runtime problems

The live log panel shows front-end and server events; terminal output reconstructs the interaction. Runtime logs and engine history are different materials and are not guaranteed to be in the same export package.

In a report, give the time, version, entry point used, expected result, and actual result. Avoid uploading passwords, tokens, full authentication responses, or unrelated conversations.

## Sessions and restarts

| Symptom | Check first |
| --- | --- |
| No progress for a long time | Whether the model or tools produced new output, or there is only heartbeat and pipe activity; a soft stall does not mean failure |
| Native compaction reached the maximum time | Compaction state, the whole-turn hard deadline, and the native final state; recent output does not prove it did not time out |
| Final state not confirmed | The last tool and the actual repository/remote result; keep the original request and do not resend it directly |
| Subagent records still visible after interruption | Tell historical messages, notifications awaiting cancellation confirmation, and new turns apart; save the run id and time |
| Restart returns 409 | The type of blocking operation and the current lifecycle state; do not restart in a loop or bypass supervision |
| Restart accepted | Means only that it was taken over; still check the new boot, the running version, and connection recovery |

A phone screen showing "Processing" or the older "Received, waiting for engine response" does not prove the model service received the request. Unified-storage Claude limits initialization, SDK input delivery, and waiting for the first response separately: initialization and input delivery each take at most 30 seconds. After about 60 seconds without model progress only a cancellable notice is shown, and it ends only after 10 consecutive minutes without an upstream response, native retry, or other native progress. For long periods without output, keep the session name, time, and timeout notice, and do not blame Tailscale alone.

The macOS folder authorization dialog appears only on the computer; you can deny it or allow only the folders the task needs, and Full Disk Access is not required. SDK tool approval cannot replace system authorization. An initialization timeout means the user input was not dispatched; a timeout after dispatch still requires verifying the native execution record, so do not resend directly. After a disconnection, check against the original request's receipt, and do not treat an unknown result as not sent.

When the restart state is `unknown` or `restart_error`, keep the receipt and logs and first confirm whether the host has taken over; do not kill processes by hand, clear locks, or submit repeatedly. Authentication errors should be checked against the engine and provider named in the message; switching engines does not prove the old authentication has recovered.
