---
name: delete-workspace
description: Delete a workflow from Desktron. Deletion cannot be undone and requires two confirmations as described; deletes only the workflow, not individual apps in it.
---

# delete-workspace

Deletes a workflow (a domain registered in `.workbench/domains.json`; a workflow is a working folder created deliberately, the counterpart of "project = an opened code repository folder"). This is irreversible; follow the two steps below and do not skip the preview.

## Scope (confirm first)

- Every workflow is a runtime entry and can be deleted (since 2026-07 the release no longer ships built-in workflows such as incident response or risk control, so the former "shipped workflows can only be hidden" restriction no longer applies).
- Deletion **removes only the registry entry**: the workflow's own directory (`domains/<id>/`) and its sessions are **not** deleted. If the user wants the directory or sessions cleared too, say so separately and handle it separately; do not delete data by default.

## Steps

1. Establish which one to delete: get the workspace name or id; if unsure, list the runtime workspaces and let the user point it out.
2. **Preview (dry run)**: run the script without `--yes`. It shows what will be deleted and a reminder that the directory and sessions are untouched. Show this preview to the user.
3. Only **after explicit confirmation from the user**, add `--yes` to delete. If the user has not clearly said to delete, stop at the preview.

```bash
# Preview (read only, deletes nothing)
bash .claude/skills/delete-workspace/scripts/delete_workspace.sh <id>
# Delete after the user confirms
bash .claude/skills/delete-workspace/scripts/delete_workspace.sh <id> --yes
```

After deletion the workflow disappears from the left sidebar (refresh or re-enter the workbench).
