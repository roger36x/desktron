# Pilot: task completion conditions and evidence

Old tasks keep their original behavior. New or existing tasks can explicitly enable a completion contract; once enabled, the Agent must not change the acceptance scope as work progresses. In the pilot each task has at most one delivery commit in a code repository and eight completion conditions. The user can still adjust task state directly in the panel.

## Declaring completion conditions

Example `contract.json`:

```json
{"criteria":[
  {"id":"code","kind":"commit","summary":"Fix committed"},
  {"id":"tests","kind":"test","summary":"Relevant tests pass"}
]}
```

```bash
desk-task create --title 'Fix relative links' --contract-file contract.json
desk-task show --task-id <id>
```

Kinds include commit, test, deploy, acceptance, and artifact. Choose according to what the user asked for: a code fix task need not include going live; a go-live task must name the target and the runtime acceptance check. A research task may declare only artifact. Do not add future improvements to the current delivery conditions.

An existing task enables the contract for the first time through `progress --contract-file`, which must include `--revision`. Conditions are never derived automatically for old tasks.

## Registering evidence

Evidence uses a separate JSON array and does not take up the progress summary:

```json
[
  {"criterion":"code","summary":"Fixed clicks on two line numbers", "ref":"commit URL or description",
   "repo":"/absolute/repo","commit":"full commit SHA"},
  {"criterion":"tests","result":"passed","summary":"15 passed", "ref":"/absolute/test-result.json",
   "subject":"full commit SHA matching code"}
]
```

```bash
desk-task progress --task-id <id> --revision <revision> \
  --evidence-file evidence.json --done 'Fixed and verified' --next '' --blocker ''
desk-task review --task-id <id> --revision <new revision>
```

Git objects are verified locally; other evidence must declare result as passed or failed and is an acceptance report registered by the Agent, so the system does not treat a non-empty reference as independent verification of the business outcome. Evidence holds only summaries and references, never credentials, log text, or sensitive data. Other evidence for a code task must bind to the code commit through subject; once a new commit replaces the old one, the old verification no longer counts as passing. Releases across sessions register deployment evidence under the original task id and request review.

## Automatic checking

When a normal turn ends, the system checks the contract tasks actually updated in that turn; it closes a task only when all evidence is present, the delivery summary is not empty, and next/blocker are empty. Cancellation, failure, and uncertain delivery state never close a task automatically. This does not depend on recap and adds no model calls.

When items are missing, a review is written to the ledger, and the next ordinary message to be sent carries a targeted reminder of at most three items. The same evidence state is reminded only once; slash commands get no reminder. User adjustments, concurrent revision conflicts, and corrupted state are never overwritten by an old check. Recap no longer modifies contract tasks and only records logs.

`show` displays the conditions, evidence, revision, and missing items; tasks without a contract are still managed in the original way. The pilot is not connected to automatic collection by the release tooling, and it does not scan every repository or perform acceptance on the Agent's behalf.
