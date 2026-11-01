# App Control contract

The App Control Runtime manages app discovery, observation, actions, leases, authorization, and receipts in one place. The CLI, MCP, and the browser bridge are access channels; none may implement its own permission checks that bypass the Runtime.

## App declarations

Every new or updated app must declare `agent_operability: none|L1|L2|L3|L4`. none needs an `agent_operability_reason`; other levels need a matching `agent_control`. A level describes the scope of capability; it does not prove that an adapter implements all of it.

| Level | Capability |
| --- | --- |
| L1 | Open and observe |
| L2 | Adds reversible local changes |
| L3 | Adds external writes that need authorization |
| L4 | Adds background execution, waiting on events, and recovery |

The structure is defined by the [app-definition schema](../../../schemas/app-definition.schema.json) and the [agent-control schema](../../../schemas/agent-control.schema.json). A control declaration contains identity, resources, observations, actions, permissions, events, and receipts; do not infer default permissions from historical examples.

## Identity and execution

[capability.py](../../../desktron/lib/app_control/capability.py) verifies a per-session random capability token and the process identity. A public session id does not constitute authorization; Unix socket requests also check process ownership. Tokens must never enter source code, log text, or permanent session history.

The [Runtime](../../../desktron/lib/app_control/runtime.py) persists leases, operations, approvals, and receipts. Actions check resource scope, current state, the expected observation version, and idempotency conditions. After navigation, old nodes or observations may be invalid; callers should observe again rather than blindly replaying clicks.

Pages the Agent opens itself and user pages it takes over are handled separately; settings and authorization screens are protected. The native browser bridge handles observation and actions, while background execution and waiting are recovered from the corresponding operation state; a foreground DOM lease must not pose as long-term authorization.

## Verification

At minimum, check unauthorized access, out-of-scope resources, page updates, duplicate requests, human takeover, recovery after disconnection, and handling of sensitive evidence. Screenshots and body text still need to be judged by their actual content; structured fields being redacted does not guarantee that the whole material can be made public.

The entry point for development verification is `docs/agent-operable-app.md` in the source repository.
