"""Claude menu labels track releases without changing native model aliases."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / 'frontend/app-chat-opts.js').read_text(encoding='utf-8')


def run(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for frontend contract')
    result = subprocess.run([node, '-e', script], capture_output=True, text=True,
                            check=True, timeout=10, encoding='utf-8')
    return json.loads(result.stdout)


def models():
    match = re.search(r'const HS_MODELS = \[[\s\S]*?\n\];', SOURCE)
    assert match
    return run(match.group(0) + '\nconsole.log(JSON.stringify(HS_MODELS));')


def test_sonnet_55_label_preserves_native_alias():
    entries = models()
    assert next(row for row in entries if row['id'] == 'sonnet') == {
        'id': 'sonnet', 'label': 'Sonnet 5.5'}
    assert len([row for row in entries if row['id'] == 'sonnet']) == 1


def test_opus_46_uses_pinned_native_model_id():
    entries = models()
    assert [row for row in entries if row['label'] == 'Opus 4.6'] == [
        {'id': 'claude-opus-4-6', 'label': 'Opus 4.6'}]
    assert len({row['id'] for row in entries}) == len(entries)


def test_other_claude_aliases_unchanged():
    assert [row for row in models() if row['id'] != 'claude-opus-4-6'] == [
        {'id': '', 'label': '默认'}, {'id': 'fable', 'label': 'Fable 5.1'},
        {'id': 'opus', 'label': 'Opus 5.5'}, {'id': 'sonnet', 'label': 'Sonnet 5.5'},
        {'id': 'haiku', 'label': 'Haiku 4.5'},
    ]


@pytest.mark.parametrize('native,expected', [
    ('claude-sonnet-5-5', 'Sonnet 5.5'),
    ('claude-sonnet-5.5', 'Sonnet 5.5'),
    ('claude-sonnet-5', 'Sonnet 5'),
    ('claude-opus-4-6', 'Opus 4.6'),
])
def test_running_label_uses_reported_version_not_menu(native, expected):
    match = re.search(r'function hsPrettyModel\(id\) \{[\s\S]*?\n\}', SOURCE)
    assert match
    value = run(match.group(0) + '\nconsole.log(JSON.stringify(hsPrettyModel('
                + json.dumps(native) + ')));')
    assert value == expected
