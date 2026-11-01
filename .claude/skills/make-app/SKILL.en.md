---
name: make-app
description: Turn a procedure developed in the current session into a Desktron app and add it to a given workflow or the desktop. Use when the user explicitly asks to "make it an app"; not for Feishu Miaoda apps.
---

# make-app

Captures the output of vibing as a workspace app. An app is a consumable wrapper around assets; a matrix of apps forms a workspace's solution.

## Two kinds of app (this is the only split; no finer types are assumed)

- **agentic**: calls a model to carry out a procedure, using a 5W declaration plus a parameter form, with a fixed execution path.
- **non-agentic**: a local html page that runs deterministically without calling a model (calculators, dashboards, embedded pages, observation panels).

## Agent operability (every app must choose)

Whether an app calls a model (agentic) and whether an Agent can operate it (agent operability) are two independent axes. When creating or updating app.json you must declare `agent_operability`:

- `none`: no structured control in this version; also fill in a concrete `agent_operability_reason`. Do not choose none by default just because implementation is inconvenient; say which observation or action adapter is missing.
- `L1`: can be opened and observed.
- `L2`: adds local, reversible changes.
- `L3`: adds external writes behind a confirmation gate.
- `L4`: adds background execution, waiting on events, and recovery.

Choosing L1-L4 requires an `agent_control` of the same level that follows `desk-docs/reference/app-control.md` and `schemas/agent-control.schema.json`; never expose arbitrary JavaScript, and never expose credentials or sensitive values to the model. The registration endpoint enforces this: a missing choice, none without a reason, or mismatched levels are all rejected.

## Agentic apps: the 5W declaration

Fields (who/what/how required; when/where/params/skill optional):

- `who`: role and discipline ("You are …, read-only, respect rate limit X")
- `what`: output specification (what to produce, in what format, what to mark)
- `how`: execution steps (a fixed procedure outline that may cite skill commands; use `${param}` placeholders)
- `when`: trigger, manual (default) / scheduled / event
- `where`: scope constraints, with `${param}` placeholders
- `params`: form declarations for the placeholders, each `{name, label, required?, default?, enum?[], placeholder?}`
- `skill`: name of a referenced skill (must exist in .claude/skills/)

**Hard validation rules** (enforced at registration; anything failing is rejected):
- `id` uses only `a-z0-9_-`; `label` is not empty
- `agent_operability` must be `none` or `L1`-`L4`; none needs a reason, and L1-L4 need an `agent_control` of the same level
- every param's `${name}` must actually appear in the who/what/how/where text
- `skill`, if given, must be an existing skill

At run time: parameters are filled into the placeholders, and four sections [Role] [Scope] [Steps] [Output] are assembled and sent to the agent.

## Non-agentic apps: one html page

Write it to `.workbench/pages/<id>.html` (local; it overrides a shipped page with the same name).
- Use a `transparent` background (to blend into the frosted overlay); see existing pages for dark text colors.
- Front end only: calculation logic, forms, SVG/canvas charts are all fine; **do not** send data to non-intranet domains, use eval, or read credentials.
- When a real use happens (for example a calculation is run or data is fetched successfully), you may report it with `window.parent.postMessage({type:'app-used'},'*')` for usage counting (optional).

## Registration (making the app appear in the panel)

1. Non-agentic: write `.workbench/pages/<id>.html` first.
2. Register with a POST (domain must be an **existing** workspace id: one created with make-workspace, or `_desktop`. If the workspace does not exist yet, create it with make-workspace first; this skill does not create workspaces):

```bash
bash .claude/skills/make-app/scripts/register_app.sh <domain> <path to app.json>
```

Example app.json (agentic):
```json
{"id":"meeting-review","label":"Meeting review","desc":"Summarize meetings and to-dos for a date","icon":"clipboard",
 "agentic":true,"agent_operability":"none",
 "agent_operability_reason":"Runs through a session; there is no separate observable and operable interface","when":"manual",
 "who":"You are an office assistant. Read calendars and to-dos only; do not change external state for the user.",
 "what":"List meetings, open to-dos, and follow-ups in time order, with their sources.",
 "how":"Query ${date} with the installed calendar and task capabilities, merge duplicates, then output.",
 "where":"Target date=${date}.",
 "params":[{"name":"date","label":"Date","required":true,"placeholder":"2026-08-31"}]}
```

Example app.json (non-agentic):
```json
{"id":"my-calc","label":"My calculator","desc":"Work out a quick value","icon":"monitor","agentic":false,"page":"my-calc",
 "agent_operability":"none","agent_operability_reason":"No observation or input adapter for the calculator form yet"}
```

Successful registration returns a **uid** (of the form `author.shortname.random`, globally unique, stable, and unchanging). Tell the user this uid and keep it:
- **Improving or iterating an existing app**: include the original uid in app.json (`"uid":"author.shortname.random"`) and register again; it overwrites the same app by uid (keeping uid, author, and creation time) instead of creating a new one. When the user says "improve app X" in a session, locate it by uid, not by name (names change and repeat).
- **New app**: leave uid out of app.json; one is generated at registration.

A failed registration returns a specific error; fix app.json accordingly and register again.

## Data produced by apps

Results produced by an app stay in the app or on the original platform by default and are not written to memory files directly. Project progress and decisions are updated by Desk's normal recording flow; when this turn's key points must be saved immediately, send a request as `journal` describes. Only when sensitive information or a prohibition must stay on this machine, write it as `memory` describes.

Apps must not store credentials, open_id, chat_id, Feishu tokens, evaluations of named individuals, or other unnecessary personal information.

## Providing it to other users

This skill produces only **local** apps (stored in the runtime `.workbench/`). To ship it with every new Desk installation, use the `contribute` skill to promote the stable uid, manifest, page, and dependencies into the `desktron` source; it ships with the next release after human review.

## Discipline

- Do not decide to "make it an app" for the user; first establish the scenario, inputs and outputs, and whether it is read-only or calls a model.
- One app does one thing; if it duplicates an existing app, change that one instead of building another.
- Use lowercase, hyphenated ids and short labels.
- icon defaults to `monitor`; the allowed values match make-workspace: `zap bell shield clipboard activity layers list book monitor code memory`.
- During creation, write only to `.workbench/`; only an explicit official promotion moves it into `frontend/` through `contribute`.
