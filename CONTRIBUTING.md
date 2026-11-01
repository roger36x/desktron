# Contributing

Desktron is an early preview. Read [AGENTS.md](AGENTS.md) and [docs/development.md](docs/development.md) before changing code.

## Checks

```sh
bash desktron/bin/desktron deps
python3 -m pip install -r scripts/dev/python-test-requirements.txt # in a Python 3.12 virtualenv
bash scripts/dev/build-builtin-apps.sh      # once, fetches Excalidraw/Monaco from npm
bash scripts/dev/refactor-gate.sh quick
python3 scripts/dev/run-tests.py            # full offline suite, one file per process
python3 scripts/check_public_tree.py
python3 scripts/check_public_tree.py --staged --reviewed
```

Tests marked `native` start real engine CLIs and are skipped unless explicitly selected. Do not start a service or alter an existing installation merely to run a test.

`check_public_tree.py` is a heuristic scan of the working tree or index; it does not scan history, and it does not establish ownership or license compatibility. Run an independent secret scan and review the complete diff before publishing.

CI and the `full`/`release` gates use `--reviewed`. The versioned `scripts/public-tree-review.json` records reviewed non-sensitive fixtures, network-policy examples and product binaries by exact path, SHA-256, detected rules and reason. Changed bytes, new rules/files, stale entries and malformed manifests fail closed. Forbidden directories/configuration, unsafe paths and symlinks cannot be approved. `--staged --reviewed` reads both the complete index and its manifest, never an unstaged approval. The default raw scan still reports every heuristic finding; there is no automatic baseline-generation or bypass option. Review each match before updating an entry, and never approve real credentials or personal data. Binary security review does not resolve an asset's redistribution rights; missing licenses remain a merge/release blocker.

## Data you must not commit

Personal configuration, private endpoints or internal domain names, real conversations, chat or user IDs, operational logs and credentials, including in fixtures and documentation. Use placeholders such as `example.com`, `corp.example`, `/Users/alice` and `cli_test_app`. Organization-specific settings belong in environment variables (`DESKTRON_INTERNAL_DOMAINS`, `DESKTRON_UPDATE_BASE`, `DESKTRON_SETUP_PROXY`, `DESKTRON_LARK_DOC_BASE`) or in the user's `~/.config/desktron/env`.

## Third-party code

Confirm permission to redistribute before adding code or assets. Record version, origin, license and required notices in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), and keep upstream copyright notices.

## Contributions

By intentionally submitting a contribution for inclusion, you agree to the contribution terms in section 5 of Apache-2.0. Submit only material you are authorized to contribute. Apache-2.0 does not grant trademark rights.
