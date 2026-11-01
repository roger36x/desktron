# First-run setup

Check this machine's environment, the AI engines, and your work tools before starting sessions.

## Order of steps

1. Open first-run setup; once inside Desk, reopen it from "Settings → First-run setup".
2. Prepare Python, Node, and Git according to the check results. Python must be at least 3.11; the release installer prepares a Desk-managed interpreter and its dependencies and does not use the system Python. Node must meet both the version and the machine architecture requirements. Re-run the check after installing; a closed terminal window does not mean success.
3. Under AI engines, choose an installed Claude Code, Codex, or Pi Coding, and complete that engine's login or provider setup.
4. Connect work tools such as lark-cli as your work requires. A tool being installed, being logged in, and having permission on the target resource are different states.
5. Create or open a workflow, send a simple request, and confirm that the engine completes a turn and shows the result.

## Installation acceptance check

A release installation runs an isolated acceptance check before opening the client, verifying that the real backend starts, the page entry point works, and Shepherd is healthy; on failure the installation returns an error, and you cannot skip it and treat Desk as usable. The result is at `~/.local/share/desktron/install-check.json` (in the parent directory when you use a custom data directory). Run `desktron install-check` to check again; it does not use existing credentials, send model requests, or restart the running Desk.

Passing the offline check does not mean you are logged in or that models can be called. After configuring an engine, the user still needs to send a real request as in step 5.

## Windows

The Windows version needs 64-bit Windows 10 1809 or later. After extracting the release package, run `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1` in the package directory; administrator rights are not required. The installer prepares the WebView2 Runtime (if missing), a Desk-managed Python, the program directory, and the Start menu entry, and adds `%USERPROFILE%\.local\bin` to the user PATH; only newly opened terminals see that change.

- Git for Windows is required: skill scripts and Claude Code both run through the bash it provides. First-run setup gives the install command.
- The embedded terminal uses PowerShell by default.
- App shortcuts: ⌘X on macOS corresponds to `Ctrl+Shift+X`, and ⌘⇧X to `Ctrl+Shift+Alt+X`; plain Ctrl combinations are left to terminal programs.
- In-app updates are not supported yet; to update, run the new installer. Quit Desktron from inside the app before running it.
- "Native application observation" is macOS only; the Windows version does not show that setting.

## Operation assistant

Installation and authentication steps may open the operation assistant. It shows progress, terminal output, and the exit result. When a password or confirmation prompt appears, complete it in that terminal; closing the window does not mean the operation finished.

If it reports exit code 127, save the output and diagnostic records first, then check the path and environment of the failing command. Even if a tool works in your everyday terminal, Desk's child processes may still see a different PATH or install location.

Next: [Settings and updates](settings.md), [Logs and fault reports](diagnostics.md).
