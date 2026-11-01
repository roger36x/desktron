## Judgment rules

Choose how to act by the following rules. A workflow may add to them but must not relax safety requirements.

**Confirm something can actually be used before judging its content.** When material or a feature appears unused, first walk through real use. If the entry point, permissions, or dependencies are broken, fix those first instead of concluding the content is useless.

**A fallback must not hide the original problem.** Before adding a fallback, make sure the original problem reports a clear error and leaves a record; do not let a fallback mask a fault for long.

**Distinguish four kinds of evidence.** Source code or a data format definition proves only that something is implemented; tests prove only that it passes under given conditions; responses from the currently running version prove that it is deployed and responding; only a user actually completing the operation proves it is usable. None of the four substitutes for another.

**When you do not know whether the last attempt succeeded, do not run it again.** After a request timeout, a dropped connection, or no final result, check the saved records and actual results before deciding whether to continue.

**Record rejected approaches.** When saving a conclusion, also record the approaches ruled out and why, so they are not debated again.

**Append-only records must not be rewritten.** Conversation and event records accept only new corrections that point to the original record; documents describing the current situation may be updated, but must keep their sources and verification times.

**States must be specific.** Do not use phrases such as "in progress" or "handled" on their own; state at least one of a version, commit, task number, count, time, or next step.
