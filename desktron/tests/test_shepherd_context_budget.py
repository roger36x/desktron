"""Vision transport bytes do not consume text context; archive caps still do."""
import base64
import copy
import json
import struct
import zlib
from unittest import mock

import pytest
from shepherd.context_budget import IMAGE_TOKEN_RESERVE, estimate_context_tokens
from shepherd.coordinator import digest, estimate_tokens
from shepherd.image_content import image_block, native_content
from shepherd.service import Service
from shepherd.harness import ExecutionHarness
from shepherd.store import Conflict
from shepherd.unified_runners import UnifiedRunner
from test_shepherd_images import fixture_png
from test_shepherd_unified_storage import ENGINES, JournalRunner


@pytest.fixture(scope='module')
def pictures():
    # Valid PNG with a large ancillary chunk: encoded size is not vision cost.
    raw = base64.b64decode(fixture_png())
    data = b'note\0' + b'x' * 1200000
    chunk = struct.pack('>I', len(data)) + b'tEXt' + data
    chunk += struct.pack('>I', zlib.crc32(b'tEXt' + data))
    return [image_block(base64.b64encode(raw[:-12] + chunk + raw[-12:]).decode(), 'image/png')] * 3


def public(pictures):
    return [{'type': 'message', 'role': 'user', 'content': [
        {'type': 'input_text', 'text': 'Compare these screenshots.'}, *pictures]}]


def frame(engine, pictures):
    blocks = native_content(public(pictures)[0]['content'], engine)
    if engine == 'pi':
        return {'type': 'prompt', 'message': 'Compare these screenshots.', 'images': blocks[1:]}
    return {'type': 'user', 'message': {'role': 'user', 'content': blocks}}


@pytest.mark.parametrize('engine', ENGINES)
def test_three_images_reach_native_preparation_unchanged(tmp_path, pictures, engine):
    service = Service(tmp_path, storage='unified'); service.create('images', engine, tmp_path)
    c = service.connector(service.active('images')['binding'])
    user_frame = frame(engine, pictures) if engine in ('pi', 'claude') else None
    paths = ['first.png', 'second.png', 'third.png'] if user_frame is None else None
    runner = UnifiedRunner(c, options={'_context_window': 936000},
                           user_frame=user_frame, image_paths=paths)
    before = digest({'frame': user_frame, 'paths': paths})
    method = '_claude' if engine == 'claude' else '_pi' if engine == 'pi' else '_codex'
    with mock.patch.object(c, 'context', wraps=c.context) as context, \
         mock.patch.object(runner, method, side_effect=Conflict('native boundary reached')):
        with pytest.raises(Conflict, match='native boundary reached'):
            runner.run(prompt='Compare these screenshots.', round_id='images:1',
                       expected_offset=1, on_dispatched=lambda: pytest.fail('must not send'))
    cost = context.call_args.args[0]['_current_tokens']
    assert 3 * IMAGE_TOKEN_RESERVE <= cost < 3 * IMAGE_TOKEN_RESERVE + 5000
    assert digest({'frame': runner.user_frame, 'paths': runner.image_paths}) == before
    if user_frame is not None:
        assert estimate_tokens(user_frame) > 1500000


@pytest.mark.parametrize('engine', ENGINES)
def test_verified_image_history_fits_without_fallback(tmp_path, pictures, engine):
    service = Service(tmp_path, storage='unified'); service.create('history', engine, tmp_path)
    active = service.active('history'); c = service.connector(active['binding']); lease = active['lease']
    prior = public(pictures) + [{'type': 'message', 'role': 'assistant', 'content': [
        {'type': 'output_text', 'text': 'Earlier comparison.'}]}]
    ExecutionHarness(service.coordinator, c, JournalRunner(c, prior)).run('history',
        lease_id=lease['lease_id'], generation=lease['generation'], prompt='Compare these screenshots.',
        input_public=public(pictures))
    _, checkpoint, tail = c.context({'_require_context_budget': True, '_context_window': 936000})
    assert checkpoint is None
    assert len(tail) == 1
    assert estimate_tokens(c.encode(tail[0])) > 1500000
    assert 3 * IMAGE_TOKEN_RESERVE <= estimate_context_tokens(c.encode(tail[0])) < 60000


@pytest.mark.parametrize('engine', ENGINES)
def test_real_text_overflow_still_blocks_before_native_dispatch(tmp_path, engine):
    service = Service(tmp_path, storage='unified'); service.create('text', engine, tmp_path)
    c = service.connector(service.active('text')['binding'])
    runner = UnifiedRunner(c, options={'_context_window': 12000})
    with pytest.raises(Conflict, match='超过上下文窗口'):
        runner.run(prompt='x' * 60000, round_id='text:1', expected_offset=1,
                   on_dispatched=lambda: pytest.fail('must not send'))


def test_only_protocol_image_slots_are_discounted(pictures):
    payload = public(pictures)
    before = digest(payload)
    assert 3 * IMAGE_TOKEN_RESERVE <= estimate_context_tokens(payload) < 50000
    assert digest(payload) == before
    assert estimate_tokens(payload) > 1500000  # Storage/retrieval estimator unchanged.
    for ordinary in [json.dumps(payload), {'type': 'function_call', 'arguments': payload},
                     {'role': 'assistant', 'content': [{'type': 'toolCall', 'arguments': payload}]},
                     {'role': 'user', 'content': [{'type': 'text', 'text': json.dumps(payload)}]},
                     {'metadata': payload}]:
        assert estimate_context_tokens(ordinary) == estimate_tokens(ordinary)
    extra = copy.deepcopy(payload)
    extra[0]['content'][0]['text'] = 'x' * 60000
    assert estimate_context_tokens(extra) > estimate_context_tokens(payload) + 19000


@pytest.mark.parametrize('url', ['https://example.invalid/image.png', 'storage/private/image',
                               'data:image/png;base64,broken' + 'x' * 60000],
                         ids=['remote-url', 'storage-path', 'oversized-invalid-base64'])
def test_invalid_images_do_not_get_free_admission(url):
    value = public([{'type': 'input_image', 'image_url': url}])
    assert estimate_context_tokens(value) == estimate_tokens(value) + IMAGE_TOKEN_RESERVE


@pytest.mark.parametrize('engine', ENGINES)
def test_tool_result_images_keep_text_and_unknown_metadata(pictures, engine):
    images = native_content(pictures, engine)
    text = {'type': 'text', 'text': 'x' * 60000}
    if engine == 'claude':
        message = {'role': 'user', 'content': [{'type': 'tool_result', 'content': [text, *images]}]}
    elif engine == 'pi':
        message = {'role': 'toolResult', 'content': [text, *images]}
    else:
        message = {'type': 'function_call_output', 'output': [text, *images]}
    assert 69000 < estimate_context_tokens([message]) < 71000
    message['audit_text'] = 'x' * 60000
    assert 89000 < estimate_context_tokens([message]) < 91000


@pytest.mark.parametrize('engine', ENGINES)
def test_oversized_history_can_degrade_without_changing_current_images(tmp_path, pictures, engine):
    service = Service(tmp_path, storage='unified'); service.create('fallback', engine, tmp_path)
    active = service.active('fallback'); c = service.connector(active['binding']); lease = active['lease']
    prior = [{'type': 'message', 'role': 'assistant', 'content': [
        {'type': 'output_text', 'text': 'earlier ' * 65000}]}]
    ExecutionHarness(service.coordinator, c, JournalRunner(c, prior)).run('fallback',
        lease_id=lease['lease_id'], generation=lease['generation'], prompt='earlier question')
    user_frame = frame(engine, pictures) if engine in ('pi', 'claude') else None
    paths = ['first.png', 'second.png', 'third.png'] if user_frame is None else None
    runner = UnifiedRunner(c, options={'_context_window': 120000},
                           user_frame=user_frame, image_paths=paths)
    before = digest({'frame': user_frame, 'paths': paths})
    method = '_claude' if engine == 'claude' else '_pi' if engine == 'pi' else '_codex'
    with mock.patch.object(runner, method, side_effect=Conflict('native boundary reached')), \
         mock.patch('shepherd.resident_process.drop'), mock.patch('codex_appserver.drop_session'):
        with pytest.raises(Conflict, match='native boundary reached'):
            runner.run(prompt='Compare these screenshots.', round_id='fallback:2',
                       expected_offset=2, on_dispatched=lambda: pytest.fail('must not send'))
    assert runner.degraded_context['through_seq'] == 1
    assert digest({'frame': runner.user_frame, 'paths': runner.image_paths}) == before
