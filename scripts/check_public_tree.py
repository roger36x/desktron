#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline heuristic publication guard. Never print matched file contents.

Default: tracked and non-ignored untracked files in the working tree.
--staged: every file blob in the index, including unchanged staged content.
--reviewed: accept only exact byte/rule matches in the checked review manifest.
Neither mode scans history or establishes redistribution rights.
"""

import argparse
import hashlib
import ipaddress
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

MAX_BYTES = 2 * 1024 * 1024
FORBIDDEN_DIRS = {
    ".memory", ".workbench", "_sealed", "secrets", "node_modules",
    ".venv", "__pycache__",
}
# .claude/ ships product skills and hooks; its *.local.json files are still caught below.
PRIVATE_ROOTS = {"data", "workspace", "config", ".agents", ".codex"}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db", ".log"}
TEXT_RULES = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")),
    ("credential-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})\b")),
    ("personal-home-path", re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+(?:/|\b)")),
    ("private-host", re.compile(r"\b(?:[A-Za-z0-9-]+\.)+(?:internal|corp|intranet)\b", re.I)),
    ("url-userinfo", re.compile(r"\b(?:https?|wss?)://[^\s/@]+@", re.I)),
)
ASSIGNMENT = re.compile(
    r'''(?im)["']?\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|relay[_-]?secret)\b["']?\s*[:=]\s*["']([^"'\r\n]+)["']'''
)
PLACEHOLDERS = {"", "placeholder", "changeme", "example", "replace-me", "your-key-here", "<redacted>"}
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
# RFC 1918 network addresses as integers; do not exempt CIDR text from scans.
PRIVATE_NETS = tuple(ipaddress.ip_network(net) for net in (
    (0x0A000000, 8), (0xAC100000, 12), (0xC0A80000, 16),
))


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)


def path_rules(name):
    path = PurePosixPath(name)
    parts = path.parts
    if not parts or path.is_absolute() or ".." in parts:
        return {"unsafe-path"}
    rules = set()
    if FORBIDDEN_DIRS.intersection(parts) or parts[0] in PRIVATE_ROOTS:
        rules.add("private-or-generated-directory")
    base = path.name.lower()
    if (base == ".env" or base.startswith(".env.")) and base != ".env.example":
        rules.add("private-config")
    if base.endswith(".local.json") or base in {"credentials.json", "sso_session.json", "id_rsa", "id_ed25519"}:
        rules.add("private-config")
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        rules.add("private-file-type")
    return rules


def text_rules(text):
    rules = {name for name, pattern in TEXT_RULES if pattern.search(text)}
    for match in ASSIGNMENT.finditer(text):
        value = match.group(1).strip()
        if value.lower() not in PLACEHOLDERS and not (value.startswith("${") and value.endswith("}")):
            rules.add("credential-assignment-review")
    for match in IPV4.finditer(text):
        try:
            address = ipaddress.ip_address(match.group())
        except ValueError:
            continue
        if any(address in network for network in PRIVATE_NETS):
            rules.add("private-ip-review")
    return rules


def entries(root, staged):
    if staged:
        result = []
        for entry in git(root, "ls-files", "--stage", "-z").split(b"\0"):
            if entry:
                metadata, name = entry.split(b"\t", 1)
                mode, oid, stage = metadata.decode("ascii").split()
                result.append((name.decode("utf-8"), mode, oid, stage))
        return result
    names = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return [(name, None, None, None) for name in sorted(set(names.decode("utf-8").split("\0"))) if name]


REVIEW_MANIFEST = "scripts/public-tree-review.json"
REVIEWABLE_RULES = {name for name, _ in TEXT_RULES} | {
    "credential-assignment-review", "private-ip-review",
    "binary-file-review", "non-utf8-file-review",
}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate review manifest key")
        result[key] = value
    return result


def load_reviews(root, staged):
    """Read the manifest from the same tree/index as the files being scanned."""
    if staged:
        rows = [row for row in entries(root, True) if row[0] == REVIEW_MANIFEST]
        if len(rows) != 1 or rows[0][1] not in {"100644", "100755"} or rows[0][3] != "0":
            raise ValueError("Missing or unsafe index review manifest")
        oid = rows[0][2]
        if int(git(root, "cat-file", "-s", oid)) > MAX_BYTES:
            raise ValueError("Oversized review manifest")
        data = git(root, "cat-file", "blob", oid)
    else:
        path = root / REVIEW_MANIFEST
        if any(p.is_symlink() for p in (path, *path.parents) if p != root):
            raise ValueError("Unsafe review manifest")
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError("Missing or oversized review manifest")
        data = path.read_bytes()
    document = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(document, dict) or set(document) != {"schema", "reviews"}:
        raise ValueError("Invalid review manifest")
    if type(document["schema"]) is not int or document["schema"] != 1 or not isinstance(document["reviews"], list):
        raise ValueError("Invalid review schema")
    reviews = {}
    for row in document["reviews"]:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "rules", "reason"}:
            raise ValueError("Invalid review entry")
        name, sha, rules, reason = (row[key] for key in ("path", "sha256", "rules", "reason"))
        if (not isinstance(name, str) or not name or name in reviews or path_rules(name)
                or PurePosixPath(name).as_posix() != name or name == REVIEW_MANIFEST):
            raise ValueError("Invalid review path")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("Invalid review digest")
        if (not isinstance(rules, list) or not rules or any(not isinstance(rule, str) for rule in rules)
                or rules != sorted(set(rules)) or not set(rules) <= REVIEWABLE_RULES):
            raise ValueError("Unreviewable rule")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Missing review reason")
        reviews[name] = (sha, rules)
    return reviews


def inspect(root, staged=False, reviewed=False):
    root = Path(root).resolve()
    if Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve() != root:
        raise ValueError("The scan root must be the Git repository root")
    reviews = load_reviews(root, staged) if reviewed else {}
    findings = {}
    observed = {}
    scanned = 0
    for name, mode, oid, stage in entries(root, staged):
        rules = path_rules(name)
        scanned += 1
        if "unsafe-path" in rules:
            findings[name] = rules
            continue
        data = None
        try:
            if staged:
                if stage != "0":
                    rules.add("unmerged-index")
                if mode not in {"100644", "100755"}:
                    rules.add("symlink-or-submodule-review")
                    data = None
                elif int(git(root, "cat-file", "-s", oid)) > MAX_BYTES:
                    rules.add("oversized-file-review")
                    data = None
                else:
                    data = git(root, "cat-file", "blob", oid)
            else:
                path = root / name
                if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root):
                    rules.add("symlink-review")
                    data = None
                elif not path.exists():
                    # Tracked deletions are absent from the proposed working tree.
                    continue
                elif not path.is_file():
                    rules.add("non-regular-file-review")
                    data = None
                elif path.stat().st_size > MAX_BYTES:
                    rules.add("oversized-file-review")
                    data = None
                else:
                    data = path.read_bytes()
            if data is not None:
                if b"\0" in data:
                    rules.add("binary-file-review")
                else:
                    try:
                        rules.update(text_rules(data.decode("utf-8")))
                    except UnicodeDecodeError:
                        rules.add("non-utf8-file-review")
        except (OSError, subprocess.CalledProcessError):
            rules.add("unreadable-file")
        if data is not None:
            observed[name] = (hashlib.sha256(data).hexdigest(), sorted(rules))
        if rules:
            findings[name] = rules
    for name, expected in reviews.items():
        if observed.get(name) == expected:
            findings.pop(name, None)
        else:
            # Changed, deleted, unsafe or now-clean files also need fresh review.
            findings.setdefault(name, set()).add("review-manifest-mismatch")
    return scanned, [(name, sorted(rules)) for name, rules in sorted(findings.items())]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--reviewed", action="store_true", help="Apply exact reviewed byte/rule baseline")
    args = parser.parse_args(argv)
    try:
        scanned, findings = inspect(args.root, args.staged, args.reviewed)
    except (OSError, ValueError, UnicodeError, subprocess.CalledProcessError):
        print("Scan failed: cannot inspect repository. No file content was logged.")
        return 2
    for name, rules in findings:
        print(json.dumps({"path": name, "rules": rules}, ensure_ascii=True))
    print(f"Scanned {scanned} files; {len(findings)} files require review. Heuristic scan only; history not scanned.")
    if args.reviewed:
        print("Exact reviewed byte/rule baseline applied; redistribution rights are not established.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
