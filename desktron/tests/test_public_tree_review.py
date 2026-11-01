"""Fail-closed publication reviews for the working tree and complete Git index."""
import hashlib
from _platform import symlink
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('public_guard', ROOT / 'scripts/check_public_tree.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, 'init', '--quiet')
    return tmp_path


def put(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def review(root, name='asset.png', data=b'png\0fixture', rules=None):
    put(root, name, data)
    row = {'path': name, 'sha256': hashlib.sha256(data).hexdigest(),
           'rules': rules or ['binary-file-review'], 'reason': 'Synthetic regression fixture; no user data.'}
    put(root, guard.REVIEW_MANIFEST, json.dumps({'schema': 1, 'reviews': [row]}).encode())
    return row


def save_rows(root, rows):
    put(root, guard.REVIEW_MANIFEST, json.dumps({'schema': 1, 'reviews': rows}).encode())


def test_raw_scan_still_reports_reviewed_binary(repo):
    review(repo)
    assert ('asset.png', ['binary-file-review']) in guard.inspect(repo)[1]
    assert guard.inspect(repo, reviewed=True)[1] == []


@pytest.mark.parametrize('data', [b'changed\0fixture', b'now clean text'])
def test_changed_review_requires_reapproval_even_if_now_clean(repo, data):
    review(repo)
    put(repo, 'asset.png', data)
    assert 'review-manifest-mismatch' in dict(guard.inspect(repo, reviewed=True)[1])['asset.png']


def test_deleted_review_requires_removal(repo):
    review(repo)
    (repo / 'asset.png').unlink()
    assert guard.inspect(repo, reviewed=True)[1] == [('asset.png', ['review-manifest-mismatch'])]


def test_new_file_cannot_inherit_review_by_same_bytes(repo):
    review(repo)
    put(repo, 'another.png', b'png\0fixture')
    assert guard.inspect(repo, reviewed=True)[1] == [('another.png', ['binary-file-review'])]


def test_new_rule_requires_review_even_if_hash_matches(repo):
    payload = ('gh' + 'p_' + 'A' * 25).encode()
    review(repo, 'fixture.txt', payload, ['private-host'])
    rules = dict(guard.inspect(repo, reviewed=True)[1])['fixture.txt']
    assert set(rules) == {'credential-token', 'review-manifest-mismatch'}


def test_synthetic_token_requires_exact_review(repo):
    payload = ('gh' + 'p_' + 'A' * 25).encode()
    review(repo, 'fixture.txt', payload, ['credential-token'])
    assert guard.inspect(repo, reviewed=True)[1] == []
    put(repo, 'fixture.txt', payload + b'changed')
    assert guard.inspect(repo, reviewed=True)[1]


def test_symlink_cannot_inherit_review(repo):
    review(repo)
    (repo / 'asset.png').rename(repo / 'target.png')
    symlink('target.png', repo / 'asset.png')
    assert 'symlink-review' in dict(guard.inspect(repo, reviewed=True)[1])['asset.png']


def test_missing_manifest_fails_closed(repo, capsys):
    assert guard.main(['--root', str(repo), '--reviewed']) == 2
    assert 'No file content was logged' in capsys.readouterr().out


@pytest.mark.parametrize('mutation', ['duplicate', 'digest', 'empty_reason', 'unknown_rule', 'forbidden_path', 'extra_field', 'unsorted_rules', 'schema'])
def test_invalid_manifest_fails_closed(repo, mutation):
    row = review(repo)
    rows = [row]
    if mutation == 'duplicate':
        rows.append(row.copy())
    elif mutation == 'digest':
        row['sha256'] = 'invalid'
    elif mutation == 'empty_reason':
        row['reason'] = ' '
    elif mutation == 'unknown_rule':
        row['rules'] = ['symlink-review']
    elif mutation == 'forbidden_path':
        row['path'] = 'config/settings.json'
    elif mutation == 'extra_field':
        row['approved'] = True
    elif mutation == 'unsorted_rules':
        row['rules'] = ['private-host', 'binary-file-review']
    save_rows(repo, rows)
    if mutation == 'schema':
        put(repo, guard.REVIEW_MANIFEST, b'{"schema": true, "reviews": []}')
    assert guard.main(['--root', str(repo), '--reviewed']) == 2


def test_duplicate_json_keys_fail_closed(repo):
    put(repo, guard.REVIEW_MANIFEST, b'{"schema": 1, "schema": 1, "reviews": []}')
    assert guard.main(['--root', str(repo), '--reviewed']) == 2


def test_symlink_manifest_fails_closed(repo):
    review(repo)
    path = repo / guard.REVIEW_MANIFEST
    path.rename(repo / 'manifest.json')
    symlink('../manifest.json', path)
    assert guard.main(['--root', str(repo), '--reviewed']) == 2


def test_staged_scan_uses_index_manifest_and_content_not_worktree(repo):
    review(repo)
    git(repo, 'add', '.')
    put(repo, 'asset.png', b'unstaged\0changed')
    put(repo, guard.REVIEW_MANIFEST, b'invalid unstaged manifest')
    assert guard.inspect(repo, staged=True, reviewed=True)[1] == []
    assert guard.main(['--root', str(repo), '--reviewed']) == 2


def test_staged_content_drift_cannot_use_unstaged_approval(repo):
    row = review(repo)
    git(repo, 'add', '.')
    changed = b'staged\0change'
    put(repo, 'asset.png', changed)
    git(repo, 'add', 'asset.png')
    row['sha256'] = hashlib.sha256(changed).hexdigest()
    save_rows(repo, [row])
    assert guard.inspect(repo, reviewed=True)[1] == []
    assert 'review-manifest-mismatch' in dict(guard.inspect(repo, staged=True, reviewed=True)[1])['asset.png']


def test_staged_unchanged_sensitive_files_are_scanned(repo):
    review(repo)
    put(repo, 'unexpected.png', b'new\0binary')
    git(repo, 'add', '.')
    assert ('unexpected.png', ['binary-file-review']) in guard.inspect(repo, staged=True, reviewed=True)[1]


def test_unreviewable_paths_always_block(repo):
    review(repo)
    put(repo, 'config/settings.json', b'{}')
    assert ('config/settings.json', ['private-or-generated-directory']) in guard.inspect(repo, reviewed=True)[1]


def test_cli_never_logs_matched_content(repo, capsys):
    payload = ('gh' + 'p_' + 'A' * 25).encode()
    put(repo, 'sensitive.txt', payload)
    assert guard.main(['--root', str(repo)]) == 1
    output = capsys.readouterr().out
    assert payload.decode() not in output
    assert 'credential-token' in output


def test_ci_and_full_gate_share_reviewed_scan_and_runtime_dependencies():
    workflow = (ROOT / '.github/workflows/check.yml').read_text(encoding='utf-8')
    gate = (ROOT / 'scripts/dev/refactor-gate.sh').read_text(encoding='utf-8')
    requirements = (ROOT / 'scripts/dev/python-test-requirements.txt').read_text(encoding='utf-8')
    assert 'source desktron/lib/provision/python_runtime.sh' in workflow
    assert 'desk_python_ensure' in workflow
    assert 'sys.version_info[:3] == (3, 12, 14)' in workflow
    assert 'echo "$DESK_PYTHON_HOME/bin" >> "$GITHUB_PATH"' in workflow
    assert 'DESKTRON_DATA_HOME: ${{ runner.temp }}/desktron-ci/data' in workflow
    assert 'actions/setup-python' not in workflow
    assert 'node-version: "22.23.2"' in workflow
    assert 'python3 scripts/check_public_tree.py --reviewed' in workflow
    assert 'scripts/dev/python-test-requirements.txt' in workflow
    assert 'bash scripts/dev/refactor-gate.sh full' in workflow
    assert 'python3 scripts/check_public_tree.py --reviewed' in gate
    assert 'desktron/lib/provision/python-requirements.txt' in requirements
    assert 'pytest==' in requirements
    assert 'Prepare offline Pi SDK' in workflow
    assert '--ignore-scripts @earendil-works/pi-coding-agent@1.0.2' in workflow
    assert 'echo "$RUNNER_TEMP/desktron-ci-pi/bin" >> "$GITHUB_PATH"' in workflow
    assert workflow.index('Prepare Python test dependencies') < workflow.index('Offline full gate')
    assert workflow.index('Build built-in app assets') < workflow.index('Offline full gate')
    assert workflow.index('Prepare offline Pi SDK') < workflow.index('Offline full gate')
    assert gate.index('check_public_tree.py --reviewed') < gate.index('install_check.py')
