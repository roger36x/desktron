---
name: kb-review
description: Read-only check before committing documentation - verify factual basis, location, duplication, and sensitive information. Gives advice only; does not change files or commit.
---

# kb-review

Use this for a final check before documentation enters Git history.

## What to check

1. **Suitability**: keep only verifiable statements; procedures belong in the matching instructions, and project judgments belong in project memory.
2. **Location**: Desk product documentation goes in the matching `desk-docs/` or `docs/`; business-specific content stays in that business's own formal source; content that only applies to the current user does not enter Git.
3. **Evidence**: confirm item by item that the sources directly support the text. When a source is unavailable, outdated, or supports only part of the content, narrow the statement and note the gap.
4. **Duplication**: compare with existing documents and the target source. When content is the same, prefer updating the existing formal entry.
5. **Sensitive information**: credentials, identifiers, evaluations of named individuals, and compliance-sensitive content must be removed or rewritten first.

## Output

```text
Checked: <file or entry>
Factual basis: pass / issue
Location: pass / suggested change
Duplication: none / existing entry
Sensitive information: none / needs handling
Conclusion: ready for review / revise first
```

These instructions do not commit, push, open merge requests, publish, or write memory.
