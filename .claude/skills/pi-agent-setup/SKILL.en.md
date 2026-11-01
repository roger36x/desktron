---
name: pi-agent-setup
description: Configure and troubleshoot Desk's Pi - environment checks, installation, model provider login, request verification, fault diagnosis, and feedback drafts.
---

# Pi Agent setup assistant

With this skill, leave deterministic checks and operations to the script; the agent explains results and chooses the next step. Checks and diagnosis must still work when Pi is not installed or unavailable.

When this skill runs as a temporary Desk setup session, read [references/runtime-agent.en.md](references/runtime-agent.en.md) first. That document defines completion criteria, the state machine, the system prompt, and structured output; ordinary command-line troubleshooting does not need it.

## Workflow

1. Run the read-only check first:

   ```bash
   python3 .claude/skills/pi-agent-setup/scripts/pi_setup.py check --json
   ```

2. Explain the current state from `next_actions`. Handle only failing items; do not overwrite existing Pi configuration or valid logins.
3. Before installing or repairing, show the user each action's `label`, `command`, and `effect`. Run it only when the user explicitly asks.
4. When a password, SSO, browser authorization, or device confirmation is needed, use Desk's interactive terminal and pause; run `check` again afterwards.
5. Completion must be proven by a real minimal request to the specified provider. A present binary, an existing configuration file, or a healthy provider status does not prove end-to-end success.

## Modes

- `check`: checks Node, npm, Pi, and the local model directory without changing state.
- `plan`: prints the next actions. It only plans and runs no commands.
- `diagnose`: prints a shareable, redacted diagnosis. Do not add conversation text, user files, full configuration, or credentials.
- `report-draft`: drafts an issue report from the diagnosis. It must not be sent anywhere before the user confirms.

## Provider routing

- Open an interactive `pi` terminal and guide the user to enter `/login` and choose a provider. Do not read or fill in API keys for them.
- `pi auth check` must name a specific provider and must not use `--credentials`. It only proves the credential check result for that provider.

## Faults and feedback

Generate a diagnosis:

```bash
python3 .claude/skills/pi-agent-setup/scripts/pi_setup.py diagnose --json
```

Generate a draft:

```bash
python3 .claude/skills/pi-agent-setup/scripts/pi_setup.py report-draft \
  --error-code PI-UNKNOWN --summary "Fault symptom visible to the user"
```

Drafts and diagnosis files are kept only on this machine. Whether and where to submit is the user's decision; before submitting, search for duplicate issues and get the user's authorization.

## Stop conditions

- State `needs_user`: wait for the user to finish the interaction; do not retry automatically.
- State `uncertain`: keep the operation id and evidence; do not repeat installation or create resources again.
- Suspected credential detected: stop sending anything out and keep only the redacted local result.
