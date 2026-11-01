# Apps

The Apps page manages tools you can open repeatedly. "All / Official / Local" on the left filters by source; the official list and entries created by the user are maintained separately.

## Current official tools

| App | Purpose |
| --- | --- |
| Memo | Keep work notes |
| Excalidraw | Draw diagrams |
| Terminal | Run commands and interactive operations |
| Browser | Open web pages |
| Monaco IDE | Edit files |
| File browser | Browse directories and files |
| TaskNote | Organize plain-text tasks |

These 7 items come from the current official list; whether a tool is usable still depends on local dependencies, permissions, and the current version.

TaskNote keeps the app identifier and task data of the former TaskPaper; its current icon is a warm white paper background with three small red check marks.
Timeline has been removed from the official app list; this does not delete Feishu event data or stop the sync service among the system arrangements.

## Local apps

A local app can be an HTML tool or an Agent task entry point with parameters. A task entry point collects parameters and starts a run; it does not run automatically at a fixed time, and scheduled needs are managed in Arrangements.

When updating an existing app, keep its stable identifier to avoid registering it twice. Before deleting an entry, confirm whether its related material is still used by other work.

## Sharing and control

Workbench apps are reusable entries. When statistics pages, dashboards, or presentation material need to be shared, build a standalone page and obtain a preview link through the publishing flow; publishing needs authorization.

Whether an Agent can operate an app depends on its declared capabilities and control interface; embedding it in Desk does not open every operation automatically. See [Browser control](browser-control.md) and [Extending and contributing](extend.md).
