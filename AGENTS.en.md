# Desktron Working Rules

## Rules that must not be broken

- Any model source may be used, but personal data or other compliance-restricted data must not be sent to external models or services that are not authorized to receive it. When a task does not need individual user data, query only de-identified or aggregated results; if that is not possible, refuse.
- Compliance-sensitive content, and judgments about organizations or named individuals, must not be written to this repository; treat Git history as visible to other collaborators.
- API keys, tokens, and other credentials must not be written to the repository, logs, command arguments, or release packages. Configuration keeps only placeholders; credentials live only in controlled locations on this machine.
- Restarting the backend interrupts running turns. Do not restart without the user's explicit consent, and do not repeat an external operation whose result has not been confirmed.
- Before running `sync-home`, committing shared changes, or restarting, take stock of every change, the staging area, and related sessions in the shared working tree; do not overwrite or carry along anyone else's unfinished changes.
- After the user agrees to a restart, read `/desk-probe` first. If `supervised` is not `true`, install the new native shell from outside Desk and reopen it, and continue only after confirming the Supervisor is healthy; do not let the old version upgrade itself.
- If `supervised` is `true`, run `bash desktron/bin/desktron sync-home` in the source repository root, then run `"$HOME/.local/share/desktron/home/desktron/bin/restart-desk" --detach --yes`. Leave the restart to the native shell; do not substitute launchd, nohup, process scanning, or manual signals.
