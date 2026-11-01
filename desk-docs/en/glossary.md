# Glossary

| Name | Current meaning |
| --- | --- |
| Desk | The workbench that manages sessions, projects, and tools |
| Workflow | A set of context, materials, and tasks for long-term work |
| Session | One ongoing discussion and execution record; Shepherd sessions can switch execution engines |
| Shepherd | The session mode with unified history, source verification, and switching across engines |
| Subagent | A task executor spawned by the main Agent; Pi subagents are currently read-only |
| Agent message | The display style for cross-session messages or subagent results; not user authorization |
| Task | An item with trackable state in TASKS.md |
| Arrangement | A combination of what to run, a time rule, and the identity that runs it |
| App | A tool or task entry point you can open repeatedly |
| Agents | The management entry point for Feishu Bots, knowledge bases, and skills |
| Feishu Bot | A running instance that sends and receives messages through a Feishu app and calls an engine |
| Engine | An execution adapter such as Claude Code, Codex, or Pi Coding |
| Model | The specific inference model an engine uses |
| App Control | The controlled interface through which an Agent operates browsers and apps |
| journal | The work log appended by an isolated recap |
| notes / project model | The current project state maintained by the memory engine |
| pinned | Confirmed constraints in old memory's compatible input, in the physical directory `sot`; not a write entry point for the current main session |
| sealed | Sensitive constraints kept only on this machine |
| Skill | A package containing a procedure and its constraints |
| Supervisor | The host that manages backend process replacement: the native shell on macOS/Windows, systemd on Linux |
| boot_id | The identity of one backend start, checked separately from the version when accepting a restart |
| unknown | A state whose result cannot be confirmed; it does not mean the operation did not run |
| payload | The programs and official assets installed with the product |

Names such as "工坊" (Workshop), "Agents", and "curator" may appear in old files, module names, or historical material. Current interface names follow [features and entry points](overview.md); compatibility in module names does not mean the old product structure is still in use.
