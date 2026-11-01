"""Immutable fallback input sources for incomplete Shepherd turns."""
import json
import os
from pathlib import Path
import sys

import pytest
from _platform import assert_private_file, symlink

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron' / 'lib'))

from shepherd import Conflict, InvalidState, TurnInputs
from shepherd import turn_inputs
from shepherd.turn_inputs import InputPermissionError, InputTooLarge


def public(text='question'):
    return [{'type':'message','role':'user',
             'content':[{'type':'input_text','text':text}]}]


def test_round_trip_is_private_and_idempotent(tmp_path):
    inputs = TurnInputs(tmp_path)
    ref = inputs.write('conversation', 'conversation:1', public())
    assert inputs.write('conversation', 'conversation:1', public()) == ref
    assert inputs.read(ref) == public()
    path = inputs.reference_path(ref)
    assert path == inputs._content_path('conversation', 'conversation:1', ref['input_hash'])
    assert_private_file(path)


def test_same_turn_different_inputs_are_independent_and_tampering_fails_closed(tmp_path):
    inputs = TurnInputs(tmp_path)
    ref = inputs.write('conversation', 'conversation:1', public())
    changed = inputs.write('conversation', 'conversation:1', public('changed'))
    assert changed != ref
    assert inputs.read(ref) == public()
    assert inputs.read(changed) == public('changed')
    path = Path(inputs.reference_path(ref))
    value = json.loads(path.read_text()); value['public'][0]['content'][0]['text'] = 'tampered'
    path.write_text(json.dumps(value)); path.chmod(0o600)
    with pytest.raises(Conflict):
        inputs.read(ref)


def test_legacy_turn_path_remains_readable(tmp_path):
    inputs = TurnInputs(tmp_path); value = public('legacy')
    ref = {'conversation_id':'conversation','turn_id':'conversation:1',
           'input_hash':inputs._hash(value)}
    path = Path(inputs.path('conversation','conversation:1')); path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'version':1, **ref, 'public':value})); path.chmod(0o600)
    assert inputs.read(ref) == value


def test_discard_removes_only_the_exact_content_addressed_input(tmp_path):
    inputs = TurnInputs(tmp_path)
    first = inputs.write('conversation','conversation:1',public('first'))
    second = inputs.write('conversation','conversation:1',public('second'))
    assert inputs.discard(first)
    assert not Path(inputs.reference_path(first)).exists()
    assert inputs.read(second) == public('second')
    assert not inputs.discard(first)


def test_multimodal_input_larger_than_old_single_image_limit_round_trips(tmp_path):
    # Synthetic bytes only; the image protocol owns format validation. This
    # storage contract must retain both already-canonical data URLs verbatim.
    value = public('two images')
    value[0]['content'] += [
        {'type':'input_image','image_url':'data:image/png;base64,' + 'A' * (9 * 1024 * 1024)},
        {'type':'input_image','image_url':'data:image/png;base64,' + 'B' * (9 * 1024 * 1024)}]
    inputs = TurnInputs(tmp_path)
    ref = inputs.write('conversation', 'conversation:1', value)
    path = Path(inputs.reference_path(ref))
    assert path.stat().st_size > 16 * 1024 * 1024
    before = path.read_bytes()
    assert inputs.read(ref) == value
    assert path.read_bytes() == before
    assert inputs.write('conversation', 'conversation:1', value) == ref


@pytest.mark.parametrize('layout', ['current', 'nested_hash', 'legacy'])
def test_existing_large_v1_input_does_not_require_migration(tmp_path, layout):
    if os.name == 'nt' and layout == 'nested_hash':
        pytest.skip('旧冒号目录布局不能存在于 Windows 普通目录')
    inputs = TurnInputs(tmp_path)
    value = public('x' * (17 * 1024 * 1024))
    ref = {'conversation_id':'conversation','turn_id':'conversation:1',
           'input_hash':inputs._hash(value)}
    path = Path(inputs.path('conversation', 'conversation:1') if layout == 'legacy' else
                tmp_path / 'inputs' / 'conversation' / 'conversation:1' / (ref['input_hash']+'.json')
                if layout == 'nested_hash' else
                inputs._content_path('conversation','conversation:1',ref['input_hash']))
    path.parent.mkdir(parents=True)
    original = json.dumps({'version':1, **ref, 'public':value}).encode()
    path.write_bytes(original); path.chmod(0o600)
    assert inputs.read(ref) == value
    assert path.read_bytes() == original


def test_quota_uses_encoded_utf8_and_exact_boundary(tmp_path, monkeypatch):
    value = public('中文\\n"')
    size = len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8'))
    inputs = TurnInputs(tmp_path)
    monkeypatch.setattr(turn_inputs, 'MAX_INPUT_PUBLIC_BYTES', size - 1)
    with pytest.raises(InputTooLarge) as exc:
        inputs.write('conversation', 'conversation:1', value)
    assert exc.value.status == 413
    assert exc.value.code == 'turn_input_too_large'
    assert not (tmp_path / 'inputs').exists()  # rejection precedes all durable effects
    monkeypatch.setattr(turn_inputs, 'MAX_INPUT_PUBLIC_BYTES', size)
    ref = inputs.write('conversation', 'conversation:1', value)
    assert inputs.read(ref) == value
    monkeypatch.setattr(turn_inputs, 'MAX_INPUT_PUBLIC_BYTES', size - 1)
    with pytest.raises(InputTooLarge):
        inputs.read(ref)


def test_record_limit_is_distinct_from_permissions_and_bounded_before_parsing(tmp_path, monkeypatch):
    inputs = TurnInputs(tmp_path)
    ref = inputs.write('conversation', 'conversation:1', public())
    path = Path(inputs.reference_path(ref))
    monkeypatch.setattr(turn_inputs, 'MAX_INPUT_RECORD_BYTES', 128)
    with path.open('wb') as stream:
        stream.truncate(129)
    monkeypatch.setattr(turn_inputs.json, 'loads', lambda *_: pytest.fail('oversized record parsed'))
    with pytest.raises(InputTooLarge):
        inputs.read(ref)
    if os.name != 'nt':
        path.chmod(0o644)
        with pytest.raises(InputPermissionError):
            inputs.read(ref)


def test_reader_detects_growth_after_fstat(tmp_path, monkeypatch):
    inputs = TurnInputs(tmp_path)
    ref = inputs.write('conversation', 'conversation:1', public())
    path = Path(inputs.reference_path(ref))
    actual = path.stat()
    monkeypatch.setattr(turn_inputs, 'MAX_INPUT_RECORD_BYTES', 128)
    # The bounded read remains authoritative if a file grows after metadata check.
    from types import SimpleNamespace
    monkeypatch.setattr(turn_inputs.os, 'fstat', lambda _: SimpleNamespace(
        st_mode=actual.st_mode, st_uid=actual.st_uid, st_size=1))
    with pytest.raises(InputTooLarge):
        inputs.read(ref)


@pytest.mark.parametrize('payload', [[], None, 'text', {'version':1}])
def test_malformed_record_fails_as_contract_error(tmp_path, payload):
    inputs = TurnInputs(tmp_path)
    ref = inputs.write('conversation', 'conversation:1', public())
    Path(inputs.reference_path(ref)).write_text(json.dumps(payload))
    with pytest.raises(Conflict):
        inputs.read(ref)


def test_symlink_input_is_not_followed(tmp_path):
    inputs = TurnInputs(tmp_path)
    ref = inputs.write('conversation', 'conversation:1', public())
    path = Path(inputs.reference_path(ref))
    original = path.with_suffix('.original')
    path.rename(original); symlink(original, path)
    with pytest.raises((OSError, InputPermissionError)):
        inputs.read(ref)


def test_wrong_owner_is_still_rejected(tmp_path, monkeypatch):
    inputs = TurnInputs(tmp_path)
    ref = inputs.write('conversation', 'conversation:1', public())
    original = os.fstat
    from types import SimpleNamespace
    def wrong_owner(fd):
        info = original(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid()+1, st_size=info.st_size)
    monkeypatch.setattr(turn_inputs.os, 'fstat', wrong_owner)
    with pytest.raises(InputPermissionError):
        inputs.read(ref)


def test_non_regular_input_is_still_rejected(tmp_path):
    inputs = TurnInputs(tmp_path)
    ref = inputs.write('conversation', 'conversation:1', public())
    path = Path(inputs.reference_path(ref))
    path.unlink(); path.mkdir(mode=0o700)
    with pytest.raises(InputPermissionError):
        inputs.read(ref)


def test_invalid_public_projection_is_rejected(tmp_path):
    inputs = TurnInputs(tmp_path)
    with pytest.raises(InvalidState):
        inputs.write('conversation', 'conversation:1', [])
    with pytest.raises(InvalidState):
        inputs.write('conversation', 'conversation:1', {'text':'body'})
