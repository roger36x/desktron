"""Image bytes survive native persistence and every history conversion."""
import copy
import base64
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'desktron/lib'))
from shepherd.image_content import (image_block, image_parts, model_image_info,
                                    restore_native_images, MAX_IMAGE_BYTES,
                                    MODEL_IMAGE_PATCH_LIMIT)
from shepherd.source_codec import claude_public, pi_public, codex_public
from shepherd.claude_codec import encode_rounds as claude_encode
from shepherd.pi_codec import encode_round as pi_encode
from shepherd.codex_codec import encode_round as codex_encode
from shepherd.coordinator import digest
from shepherd.store import InvalidState
from shepherd import Service, Conflict
_argv = sys.argv
sys.argv = ['ui_server.py', str(ROOT/'frontend'), '7999', '7998', str(ROOT)]
try:
    from engine_adapters.shepherd import ShepherdAdapter
finally:
    sys.argv = _argv

def fixture_png(width=32, height=32, pixels=True):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind+data))
    raw = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))
    body = (b'\0'+b'\xff\0\0'*width)*height if pixels else b'\0'
    raw += chunk(b'IDAT',zlib.compress(body)) + chunk(b'IEND',b'')
    return base64.b64encode(raw).decode()


def fixture_image(mime, width=640, height=480):
    if mime == 'image/jpeg':
        payload = b'\x08' + struct.pack('>HH', height, width) + b'\x03\x01\x11\0\x02\x11\0\x03\x11\0'
        raw = b'\xff\xd8\xff\xff\xc0' + struct.pack('>H', len(payload) + 2) + payload + b'\xff\xd9'
    elif mime == 'image/gif':
        raw = b'GIF89a' + struct.pack('<HH', width, height) + b'\x00\x00\x00;'
    else:
        if mime == 'image/webp-vp8x':
            kind = b'VP8X'; payload = b'\0' * 4 + (width - 1).to_bytes(3, 'little') + (height - 1).to_bytes(3, 'little')
        elif mime == 'image/webp-vp8l':
            kind = b'VP8L'; payload = b'\x2f' + ((width - 1) | ((height - 1) << 14)).to_bytes(4, 'little')
        else:
            kind = b'VP8 '; payload = b'\0\0\0\x9d\x01\x2a' + struct.pack('<HH', width, height)
        chunk = kind + len(payload).to_bytes(4, 'little') + payload
        if len(payload) % 2: chunk += b'\0'
        raw = b'RIFF' + (len(chunk) + 4).to_bytes(4, 'little') + b'WEBP' + chunk
    return base64.b64encode(raw).decode()


PNG = fixture_png()
OVERSIZED_PNG = fixture_png(3348, 15828, pixels=False)
BINDING = {'binding_id':'fixture','native_session_id':'native','native_cwd':'/tmp'}


def public_round():
    return {'round_id':'conversation:1','offset':1,'entry_hash':'a'*64,'public':[
        {'type':'message','role':'user','content':[
            {'type':'input_text','text':'compare'}, image_block(PNG, 'image/png'),
            {'type':'input_text','text':'and'}, image_block(PNG, 'image/png')]},
        {'type':'function_call','name':'read','call_id':'call_1','arguments':'{}'},
        {'type':'function_call_output','call_id':'call_1','output':'historical result'},
        {'type':'message','role':'assistant','content':[{'type':'output_text','text':'done'}]}]}


def roundtrip(engine, row):
    if engine == 'claude':
        return claude_public(claude_encode(BINDING, [row]))
    if engine == 'pi':
        return pi_public([{'type':'message','message':m} for m in pi_encode(row)])
    return codex_public(codex_encode(row))


class ImageProtocol(unittest.TestCase):
    def test_claude_image_import_retry_preserves_parent_and_rejects_changes(self):
        with tempfile.TemporaryDirectory() as root:
            service=Service(Path(root)/'state'); service.create('images','claude',root)
            connector=service.connector(service.active('images')['binding'])
            row=public_round()
            with mock.patch.object(connector,'_available'), mock.patch.object(connector,'_verify'):
                revision=connector.append([row],'')['native_revision']
                self.assertEqual(connector.append([row],revision)['native_revision'],revision)
                self.assertEqual(connector.append([row],'')['native_revision'],revision)
                changed=copy.deepcopy(row);changed['public'][0]['content'][0]['text']='altered'
                with self.assertRaises(Conflict): connector.append([changed],revision)

    def test_all_engine_pairs_preserve_order_bytes_and_tools(self):
        original = public_round()
        for source in ('claude','pi','codex'):
            for target in ('claude','pi','codex'):
                with self.subTest(source=source,target=target):
                    projected = roundtrip(source, original)
                    actual = roundtrip(target, {**original,'public':projected})
                    self.assertEqual(actual, original['public'])
                    self.assertEqual(digest(actual), digest(original['public']))

    def test_image_only_user_message_is_not_dropped(self):
        row = public_round()
        row['public'] = [{'type':'message','role':'user',
                          'content':[image_block(PNG,'image/png')]}]
        for engine in ('claude','pi','codex'):
            self.assertEqual(roundtrip(engine,row), row['public'])

    def test_tool_result_images_roundtrip_across_all_engines(self):
        output = [{'type':'input_text','text':'image result'}, image_block(PNG,'image/png')]
        row = public_round()
        row['public'] = [
            {'type':'message','role':'user','content':[{'type':'input_text','text':'read image'}]},
            {'type':'function_call','name':'read','call_id':'call_image','arguments':'{}'},
            {'type':'function_call_output','call_id':'call_image','output':output},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'done'}]},
        ]
        for source in ('claude','pi','codex'):
            for target in ('claude','pi','codex'):
                with self.subTest(source=source,target=target):
                    projected = roundtrip(source, row)
                    actual = roundtrip(target, {**row, 'public':projected})
                    self.assertEqual(actual, row['public'])
                    self.assertEqual(digest(actual), digest(row['public']))

    def test_claude_parallel_image_results_keep_tool_results_first(self):
        # 2026-10-02 adp_6c287a7752584a：两次并行看图的结果与 "[Image: ...]" 说明交错写成
        # [result, text, result, text]，Claude 以 "tool use concurrency issues" 拒绝整段历史。
        note = {'type':'message','role':'user','content':[
            {'type':'input_text','text':'[Image: original 2360x1584, displayed at 2000x1342.]'}]}
        row = public_round()
        row['public'] = [
            {'type':'message','role':'user','content':[{'type':'input_text','text':'look'}]},
            {'type':'function_call','name':'Read','call_id':'call_a','arguments':'{}'},
            {'type':'function_call','name':'Read','call_id':'call_b','arguments':'{}'},
            {'type':'function_call_output','call_id':'call_a','output':[image_block(PNG,'image/png')]},
            note,
            {'type':'function_call_output','call_id':'call_b','output':[image_block(PNG,'image/png')]},
            copy.deepcopy(note),
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'done'}]},
        ]
        entries = claude_encode(BINDING, [row])
        users = [e['message']['content'] for e in entries if e['type'] == 'user']
        results = [blocks for blocks in users if any(b.get('type') == 'tool_result' for b in blocks)]
        self.assertEqual(len(results), 1)
        kinds = [b['type'] for b in results[0]]
        self.assertEqual(kinds, ['tool_result', 'tool_result', 'text', 'text'])
        self.assertEqual([b.get('tool_use_id') for b in results[0][:2]],
                         [b['id'] for e in entries if e['type'] == 'assistant'
                          for b in e['message']['content'] if b.get('type') == 'tool_use'])

    def test_image_locator_is_restored_only_from_hash_bound_turn_input(self):
        trusted = [{'type':'message','role':'user','content':[
            {'type':'input_text','text':'look'}, image_block(PNG,'image/png')]}]
        native = [{'type':'message','role':'user','content':[
            {'type':'input_text','text':'look'},
            {'type':'input_image','image_url':'storage/private/object','detail':'original'}]}]
        restored = restore_native_images(native, trusted)
        self.assertTrue(restored[0]['content'][1]['image_url'].startswith('data:image/png;base64,'))
        self.assertEqual(restored[0]['content'][1]['detail'], 'original')
        self.assertEqual(native[0]['content'][1]['image_url'], 'storage/private/object')
        with self.assertRaisesRegex(InvalidState, '槽位'):
            restore_native_images(native + copy.deepcopy(native), trusted)
        with self.assertRaisesRegex(InvalidState, '持久化字节'):
            restore_native_images(native, [{'type':'message','role':'user','content':[
                {'type':'input_image','image_url':'https://example.org/untrusted'}]}])

    def test_tool_result_locator_is_not_restored_from_user_input_or_network(self):
        trusted = [{'type':'message','role':'user','content':[
            {'type':'input_text','text':'look'}, image_block(PNG,'image/png')]}]
        native = [
            {'type':'message','role':'user','content':[
                {'type':'input_text','text':'look'},
                {'type':'input_image','image_url':'storage/user/object','detail':'original'}]},
            {'type':'function_call','call_id':'c','name':'read','arguments':'{}'},
            {'type':'function_call_output','call_id':'c','output':[
                {'type':'input_image','image_url':'storage/tool/object','detail':'original'}]},
        ]
        restored = restore_native_images(native, trusted)
        self.assertTrue(restored[0]['content'][1]['image_url'].startswith('data:image/png;base64,'))
        with self.assertRaisesRegex(InvalidState, '持久化字节'):
            codex_public(restored)

    def test_all_supported_dimensions_include_jpeg_fill_and_webp_variants(self):
        fixtures = [('image/jpeg', fixture_image('image/jpeg')),
                    ('image/gif', fixture_image('image/gif')),
                    ('image/webp', fixture_image('image/webp-vp8x')),
                    ('image/webp', fixture_image('image/webp-vp8l')),
                    ('image/webp', fixture_image('image/webp-vp8'))]
        for mime, data in fixtures:
            with self.subTest(mime=mime, size=len(data)):
                info = model_image_info(data, mime)
                self.assertEqual((info['width'], info['height']), (640, 480))

    def test_model_patch_limit_is_measured_and_oversized_history_becomes_marker(self):
        exact = model_image_info(fixture_png(640, 48000, pixels=False), 'image/png')
        over = model_image_info(fixture_png(608, 50528, pixels=False), 'image/png')
        self.assertEqual(exact['patches'], MODEL_IMAGE_PATCH_LIMIT)
        self.assertEqual(over['patches'], MODEL_IMAGE_PATCH_LIMIT + 1)
        info = model_image_info(OVERSIZED_PNG, 'image/png')
        self.assertEqual((info['width'], info['height'], info['patches']),
                         (3348, 15828, 51975))
        self.assertGreater(info['patches'], MODEL_IMAGE_PATCH_LIMIT)
        row = {'round_id':'conversation:oversized','offset':2,'entry_hash':'b'*64,
               'outcome':'failed','public':[{'type':'message','role':'user','content':[
                   {'type':'input_text','text':'keep screenshot'},
                   image_block(OVERSIZED_PNG,'image/png')]}]}
        self.assertEqual(pi_encode(row)[0]['content'][1]['type'], 'image')
        self.assertEqual(codex_encode(row)[0]['content'][1]['type'], 'input_image')
        pi = pi_encode(row, model_image_limit=True)[0]['content']
        codex = codex_encode(row, model_image_limit=True)[0]['content']
        self.assertEqual([block['type'] for block in pi], ['text','text'])
        self.assertEqual([block['type'] for block in codex], ['input_text','input_text'])
        self.assertIn('3348×15828', pi[1]['text'])
        self.assertIn('原图仍保留', codex[1]['text'])

    def test_unified_history_limit_follows_target_route(self):
        row = {'round_id':'conversation:route','offset':1,'entry_hash':'c'*64,
               'outcome':'failed','public':[{'type':'message','role':'user','content':[
                   image_block(OVERSIZED_PNG,'image/png')]}]}
        cases = [('pi', {'model':'google/gemini'}, 'image'),
                 ('pi', {'model':'openai/gpt-6-astra'}, 'text'),
                 ('codex', {'model':'gpt-6-astra'}, 'input_text')]
        with tempfile.TemporaryDirectory() as root:
            service = Service(Path(root)/'state', storage='unified')
            for index, (engine, opts, expected) in enumerate(cases):
                cid = f'route-{index}'
                service.create(cid, engine, root)
                connector = service.connector(service.active(cid)['binding'])
                connector.configure_target(opts)
                self.assertEqual(connector.encode(row)[0]['content'][0]['type'], expected)

    def test_native_recap_connectors_follow_target_route(self):
        from shepherd.pi_connector import PiConnector
        from shepherd.codex_connector import CodexFamilyConnector
        row = {'round_id':'conversation:native-route','offset':1,'entry_hash':'d'*64,
               'outcome':'failed','public':[{'type':'message','role':'user','content':[
                   image_block(OVERSIZED_PNG,'image/png')]}]}
        cases = [(PiConnector, 'pi', {'model':'openai/gpt'}, 'text'),
                 (PiConnector, 'pi', {'model':'google/gemini'}, 'image'),
                 (CodexFamilyConnector, 'codex', {}, 'input_text')]
        for cls, engine, opts, expected in cases:
            connector = object.__new__(cls)
            connector.engine = engine
            connector.model_image_limit = False
            connector.configure_target(opts)
            self.assertEqual(connector._encode(row)[0]['content'][0]['type'], expected)

    def test_switch_to_unsaved_pi_uses_pi_defaults_not_current_engine_options(self):
        from engine_adapters.bus import EventBus
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        adapter = ShepherdAdapter(tmp.name, tmp.name, EventBus())
        with mock.patch.object(adapter, 'session_engine', return_value='claude'), \
             mock.patch.object(adapter, 'saved_engine_options', return_value={}), \
             mock.patch.object(adapter, '_worker', return_value=object()), \
             mock.patch.object(adapter, '_engine_options', side_effect=lambda e, w, raw: raw), \
             mock.patch('workbench._wb_state_read', return_value={
                 'engine_opts': {'pi': {'model':'openai/gpt-6-astra'}}}):
            self.assertTrue(adapter._model_patch_limited(
                'public', 'pi', {'model':'google/gemini'}))

    def test_remote_paths_invalid_data_and_mime_are_rejected(self):
        for url in ('https://example.org/private.png','file:///private/key','/tmp/image',
                    'data:image/svg+xml;base64,PHN2Zz4=', 'data:image/png;base64,%%%'):
            with self.subTest(url=url), self.assertRaises(InvalidState):
                image_parts({'image_url':url})
        with self.assertRaises(InvalidState): image_block(PNG,'image/jpeg')
        with self.assertRaises(InvalidState): image_block('A'*(4*((MAX_IMAGE_BYTES+2)//3)+1),'image/png')
        raw = base64.b64decode(PNG)
        for damaged in (raw[:8],raw[:-2],raw[:40]+b'xx'+raw[42:]):
            with self.assertRaises(InvalidState): image_block(base64.b64encode(damaged).decode(),'image/png')

    def test_unknown_blocks_and_non_user_images_fail_closed(self):
        for engine in ('claude','pi','codex'):
            row = public_round()
            row['public'][0]['content'].append({'type':'audio','data':'x'})
            with self.subTest(engine=engine), self.assertRaises(ValueError): roundtrip(engine,row)
            row = public_round(); row['public'][0]['role'] = 'assistant'
            with self.assertRaises(ValueError): roundtrip(engine,row)

    def test_valid_upload_keeps_request_correlation_and_invalid_never_starts(self):
        from engine_adapters.bus import EventBus
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        adapter = ShepherdAdapter(tmp.name, tmp.name, EventBus())
        atts = [{'type':'image','name':'x.png','media_type':'image/png','data_b64':PNG}]
        with mock.patch('engine_adapters.cli.CliAdapter.prompt_async') as start:
            adapter.prompt_async('public','look',atts=atts,client_request_id='request')
            self.assertEqual(start.call_args.kwargs['atts'],atts)
            self.assertEqual(start.call_args.kwargs['client_request_id'],'request')
            adapter.requests.cancel('public','request',only_before_start=True)
            start.reset_mock()
            with self.assertRaises(ValueError):
                adapter.prompt_async('public','look',atts=[{**atts[0],'data_b64':'bad'}])
            start.assert_not_called()
            with mock.patch.object(adapter, '_model_patch_limited', return_value=True):
                with self.assertRaisesRegex(ValueError, '51975.*30000'):
                    adapter.prompt_async('public','look',atts=[{
                        **atts[0], 'data_b64':OVERSIZED_PNG}],
                        client_request_id='oversized')
            start.assert_not_called()
            with mock.patch.object(adapter, '_model_patch_limited', return_value=False):
                adapter.prompt_async('public','look',atts=[{
                    **atts[0], 'data_b64':OVERSIZED_PNG}],
                    client_request_id='non-openai')
            start.assert_called_once()

    def test_aggregate_quota_rejects_valid_images_before_accepting_or_switching(self):
        from engine_adapters.bus import EventBus
        from shepherd.turn_inputs import InputTooLarge
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        adapter = ShepherdAdapter(tmp.name, tmp.name, EventBus())
        image = {'type':'image','name':'x.png','media_type':'image/png','data_b64':PNG}
        one = [{'type':'message','role':'user','content':[
            {'type':'input_text','text':'look'}, image_block(PNG,'image/png')]}]
        size = len(json.dumps(one, ensure_ascii=False, sort_keys=True).encode('utf-8'))
        with mock.patch('shepherd.turn_inputs.MAX_INPUT_PUBLIC_BYTES', size), \
             mock.patch('engine_adapters.cli.CliAdapter.prompt_async') as start:
            with self.assertRaises(InputTooLarge) as raised:
                adapter.prompt_async('public','look',atts=[image,image],
                    client_request_id='aggregate', requested_engine='pi')
            self.assertEqual(raised.exception.status, 413)
            self.assertEqual(raised.exception.code, 'turn_input_too_large')
            self.assertEqual(adapter.requests.get('public','aggregate')['status'], 'rejected')
            start.assert_not_called()
            adapter.prompt_async('public','look',atts=[image],client_request_id='fits')
            start.assert_called_once()

    def test_context_is_included_in_the_same_admission_quota(self):
        from engine_adapters.bus import EventBus
        from shepherd.turn_inputs import InputTooLarge, TurnInputs
        from engine_adapters.attachment_context import request_context
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        adapter = ShepherdAdapter(tmp.name, tmp.name, EventBus())
        context = {'type':'context','name':'reference.txt',
                   'data_b64':base64.b64encode(('参考' * 20).encode()).decode()}
        value = TurnInputs.public_input('look', context=request_context([context]))
        size = len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())
        with mock.patch('shepherd.turn_inputs.MAX_INPUT_PUBLIC_BYTES', size - 1), \
             mock.patch('engine_adapters.cli.CliAdapter.prompt_async') as start:
            with self.assertRaises(InputTooLarge):
                adapter.prompt_async('public','look',atts=[context],client_request_id='context')
            start.assert_not_called()
        with mock.patch('shepherd.turn_inputs.MAX_INPUT_PUBLIC_BYTES', size), \
             mock.patch('engine_adapters.cli.CliAdapter.prompt_async') as start:
            adapter.prompt_async('public','look',atts=[context],client_request_id='fits_context')
            start.assert_called_once()

    def test_patch_limit_is_scoped_to_codex_and_openai_pi(self):
        from engine_adapters.bus import EventBus
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        adapter = ShepherdAdapter(tmp.name, tmp.name, EventBus())
        with mock.patch.object(adapter, 'session_engine', return_value='claude'):
            self.assertFalse(adapter._model_patch_limited('public', '', {}))
        self.assertTrue(adapter._model_patch_limited('public', 'codex', {}))
        with mock.patch.object(adapter, 'session_engine', return_value='pi'), \
             mock.patch.object(adapter, 'session_options',
                               return_value={'model':'openai/gpt-6-astra',
                                             'provider':'google'}):
            self.assertTrue(adapter._model_patch_limited('public', '', {}))
        with mock.patch.object(adapter, 'session_engine', return_value='pi'), \
             mock.patch.object(adapter, 'session_options',
                               return_value={'model':'google/gemini',
                                             'provider':'openai'}):
            self.assertFalse(adapter._model_patch_limited('public', '', {}))

    def test_existing_text_only_projection_stays_unchanged(self):
        # Existing Manifest hashes must not drift when image support is added.
        self.assertEqual(claude_public([{'type':'user','message':{'role':'user','content':[
            {'type':'text','text':'a'},{'type':'text','text':'b'}]}}]),[
                {'type':'message','role':'user','content':[{'type':'input_text','text':x}]}
                for x in ('a','b')])
        expected = [{'type':'message','role':'user','content':[{'type':'input_text','text':'a\nb'}]}]
        self.assertEqual(pi_public([{'type':'message','message':{'role':'user','content':[
            {'type':'text','text':'a'},{'type':'text','text':'b'}]}}]),expected)
        self.assertEqual(codex_public([{'type':'message','role':'user','content':[
            {'type':'input_text','text':'a'},{'type':'input_text','text':'b'}]}]),expected)


if __name__ == '__main__': unittest.main()
