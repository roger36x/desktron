---
name: delete-app
description: Delete an app from a Desktron workflow. Deletion cannot be undone and requires two confirmations as described; not for deleting a whole workflow or a Feishu Miaoda app.
---

# delete-app

Removes an app from a workspace panel. This is irreversible; follow the two steps and do not skip the preview.

## Scope (confirm first)

- An app can be applied to several workspaces (targets). Removing it from one workspace removes only that target; **the app definition itself is deleted only when its last target is gone (or it is removed from all workspaces)**. The script's preview tells you which case applies.
- Apps are identified uniquely by `uid` (identical names do not collide); locate by uid first, and by id only when there is no uid.

## Steps

1. Establish which app to delete and from which workspace: get the app name or uid and the workspace id; if unsure, list them and let the user point it out.
2. **Preview (dry run)**: run the script without `--yes`. It shows what will be removed and whether it is "removed from this workspace only" or "deleted entirely". Show this to the user.
3. Only **after explicit confirmation from the user**, add `--yes` to delete.

```bash
# Preview (read only, deletes nothing)
bash .claude/skills/delete-app/scripts/delete_app.sh <domain> <uid or id>
# Delete after the user confirms
bash .claude/skills/delete-app/scripts/delete_app.sh <domain> <uid or id> --yes
```

Use `*` as `<domain>` to remove from all workspaces (deleting it entirely). After deletion the panel no longer shows it once refreshed.
