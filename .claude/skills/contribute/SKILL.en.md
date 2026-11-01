---
name: contribute
description: Move a Desktron app or skill already verified on this machine into the product source for a later release. Prepares code and verification only; never commits, pushes, publishes, or restarts automatically.
---

# contribute

Promotes a reusable local asset into `desktron` so it ships with the next product release. Official assets are no longer published to separate App, Workspace, or Skill content repositories.

## Confirm the destination first

- App: the manifest goes into `desktopApps` in `frontend/workbench.json`; pages, icons, and dependencies go into the matching `frontend/` directories.
- Skill: the whole directory goes into `.claude/skills/<name>/`.
- Workflows, sessions, memory, and project bindings are not contributed by default; they belong to the user's runtime state.

## How to work

1. Locate the App in the runtime state by its stable uid, or the skill in the skills directory; first confirm it has seen real use and is not a placeholder.
2. Find the `desktron` source repository. Prefer the current git root; otherwise read `DESKTRON_SOURCE`, then try `~/work/desktron`.
3. Inspect the source working tree. Change only the files this asset needs; existing uncommitted changes belong to the user or other sessions, so do not overwrite them or stage the whole tree.
4. Make a minimal migration: keep the App's id, uid, and page contract; dependencies need pinned versions and a reproducible build entry. If an official asset with the same ID exists, update it by uid rather than adding a parallel copy.
5. Scan for credentials, personal absolute paths, evaluations of named individuals, organizational judgments, and compliance-sensitive content. Stop on any credential or sensitive hit; there is no way to force past it.
6. Run checks proportionate to the change:
   - App: `python3 scripts/check_frontend.py`, plus `node --check` on changed JS; when Excalidraw or Monaco is involved, run `bash scripts/dev/build-builtin-apps.sh`.
   - Skill: run skill-creator's `quick_validate.py`; if scripts changed, run their side-effect-free tests.
   - Release boundary: confirm the build does not depend on the old content or KB repositories.
7. Deliver the file list, test results, and the risks that still need human review. Do not automatically commit, push, publish, sync the runtime directory, or restart Desk.

## Limits

Third-party Git packages are still installed explicitly through pkgstore and are not official assets. Becoming official means entering the product's git history and affecting every new installation, so it needs human review. When the working tree has parallel changes, prefer adding isolated files or making narrow patches; if you cannot show that you will not overwrite someone else's changes, pause on that file.
