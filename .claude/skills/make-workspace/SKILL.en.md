---
name: make-workspace
description: Create a new workflow in Desktron, with a responsibility statement and its own working directory. Use when the user wants a category of work set up as its own workflow.
---

# make-workspace

Creates a new workflow (formerly "workspace") in the workbench. A workflow is one domain in the left sidebar and the boundary of one category of work: it has its own responsibility statement and its own agent working directory (where a composed AGENTS.md combines the project constitution with this workflow's statement), and sessions and apps belong to it. It mirrors "projects": a project is an opened code repository folder, while a workflow is a working folder created deliberately.

Every workflow lives in the runtime registry `.workbench/domains.json` and is registered through the endpoint below (since 2026-07 the release ships no built-in workflows, so there is no such thing as a "shipped workspace"). Do not touch files shipped in the repository.

## Steps

### 1. Establish what the workspace is

Confirm with the user (ask about any missing field; do not invent it for them):

- `id`: the workflow identifier, `a-z0-9_-` only, short and stable (for example `capacity`, `oncall-quality`). Once created it is the key that sessions and apps belong to, so do not change it lightly. It must not collide with existing domain ids (including the two built-in managers coding and campaigns).
- `name`: display name in the left sidebar (for example "Capacity governance").
- `desc`: a one-line subtitle (optional).
- `icon`: the sidebar icon, one of `zap bell shield clipboard activity layers list book monitor code memory`. Defaults to `layers`.
- Statement: the workspace's responsibilities and way of working. Agents working here follow it.

### 2. Write the statement in markdown

Free form; two or three paragraphs are enough, no template. Cover the substance: the agent's role and discipline (read-only or writable, which rate limits to respect), what it produces and how to mark missing data, the rough scope (which services, pipelines, or ledgers are involved), and which skills or monitors to use.

Example:

```markdown
# Capacity governance

Capacity governance assistant, read-only inventory: capacity levels for compute, storage, and bandwidth, coverage of scale-out plans, and resource gaps for critical events. It does not scale anything; governance actions go through production channels.

Pull levels and ledgers with the installed data skills; mark missing data explicitly as "no data" and never invent it.
```

### 3. Register

Write the above into a `workspace.json` (the `fivew` field holds the full statement markdown; the field name is a historical contract, and the content is simply the free-form statement), then:

```bash
bash .claude/skills/make-workspace/scripts/register_workspace.sh <path to workspace.json>
```

Example workspace.json:

```json
{
  "id": "capacity",
  "name": "Capacity governance",
  "desc": "Resource level inventory and scaling advice",
  "icon": "layers",
  "fivew": "# Capacity governance\n\nCapacity governance assistant, read-only inventory: …\n"
}
```

After successful registration the workspace appears in the left sidebar (visible after refreshing or re-entering the workbench), and its own directory and composed AGENTS.md are ready. Sessions opened in this workspace afterwards use it as the agent's working directory and read its responsibility statement natively.

## Publishing (sharing across users)

There is no such path: workflows left the package model in 2026-07, the `desktron-workspaces` content repository is retired, and workflows are no longer serialized into packages. To reproduce a workflow on another deployment, copy its whole directory into the other side's `domains/`, and mount it there through "New workflow -> Import existing directory" in the UI.

## Limits

- This only creates workspaces, not apps. To capture an app use `make-app`, with the workspace id created here as the domain.
- Do not rewrite shipped repository files such as `frontend/workbench.json`; user data lands only in `.workbench/`. Workspaces are content and are not hand-written into the base `frontend/workspaces/` (a leftover from before the repository split).
- An existing id is treated as an update (overwriting name/desc/icon/statement), not an error.
