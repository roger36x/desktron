# Feishu Bot

Manage message access, execution settings, and conversations in "Agents → Feishu Bot". Bots use this machine's runtime environment.

## Setup steps

1. Choose to create a new Bot or import an existing app, and follow the wizard to prepare the Feishu app.
2. Distinguish the Bot code name in Desk from the Feishu App ID; enter the App Secret in the dedicated credential entry.
3. Configure message permissions, event subscriptions, and availability scope, and complete any required publishing on the Feishu side.
4. Choose the engine, model, working directory, and context settings, and check that engine's login status.
5. After connecting, send a test message and confirm that receiving, running, and replying all succeed.

Bots receive messages over a long-lived connection. The detailed permission names are maintained by the setup wizard; seeing "online" alone does not mean model calls and message replies also work.

## Data and limits

The declaration, connection status, and conversation mapping are stored separately, and the App Secret never enters the Bot declaration or the repository. When configuring allowed users and group chats, keep them consistent with actual use.

Do not enable the same Feishu app on several machines at once, or messages may be consumed or answered twice; when migrating, disable the Bot on the original machine first. Syncing code does not sync the Bot declaration and credentials.

The Bot's outbound check helps identify sensitive content but cannot replace user authorization and data boundaries. Third-party messages also cannot stand in for the workbench owner's approval of an operation.

When reporting a fault, give the time it happened, the connection status, the related conversation, and the stage where the error occurred; do not submit the App Secret. See [Logs and fault reports](diagnostics.md).
