# Extending and contributing

Turn work you repeat into apps, arrangements, or skills; verify first that a real task can be completed, then consider wider use.

## Choosing how to extend

| Need | What to use |
| --- | --- |
| Set up context for a long-term project | make-workspace skill |
| Create a tool or task entry point you can open repeatedly | make-app skill |
| Create a manual or scheduled execution rule | make-arrangement skill |
| Capture a procedure | Write and verify a SKILL.md |
| Bring a verified local capability into the product | contribute skill |

Read the skill text of the installed version before use; parameters and destinations are constrained by both the skill and backend validation, so do not copy commands from historical designs.

## Requirements for apps

A task app states who runs it, what it does, and how, and defines parameters and where it runs as needed. A local web app provides a working page. Keep the stable uid and update existing entries rather than registering them again.

Every app declares how far an Agent can operate it, and the matching control interface, according to the [App Control contract](reference/app-control.md); without an interface, state explicitly that it cannot be operated. Wrapping something in an app must not bypass authorization for external writes. Credentials stay only in dedicated local storage, and manifests use references or placeholders.

## Requirements for contributions

Before submitting, describe the specific problem solved, the result of the operation, and how it was verified. Official apps are built and distributed from source, and local declarations and runtime data must not be mixed into release packages. Releases use a clean snapshot and the appropriate authorization.

The development entry point of the source repository is `docs/00-index.md`; the user guide keeps only execution boundaries and does not copy details of development scripts.
