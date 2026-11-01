"""Claude ToolSearch references survive archival as inert historical results."""
import copy
import json
import unittest

from shepherd.source_codec import claude_public, _tool_output
from shepherd.store import InvalidState
from shepherd.image_content import image_block
from test_shepherd_images import PNG, roundtrip


def entries(content):
    return [
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'search_1', 'name': 'ToolSearch',
             'input': {'query': 'docs'}}]}},
        {'type': 'user', 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'search_1', 'content': content}]}},
    ]


class ToolReferences(unittest.TestCase):
    def test_reference_only_result_preserves_names_and_source(self):
        refs = [{'type': 'tool_reference', 'tool_name': name}
                for name in ('mcp__docs__lookup', 'mcp__docs__read')]
        source = entries(refs)
        before = copy.deepcopy(source)
        public = claude_public(source, version=2)
        self.assertEqual(source, before)
        self.assertEqual([item['type'] for item in public],
                         ['function_call', 'function_call_output'])
        lines = public[-1]['output'].splitlines()
        self.assertEqual([json.loads(line.split('] ', 1)[1]) for line in lines], refs)
        self.assertTrue(all(line.startswith('[Claude tool_reference] ') for line in lines))

    def test_all_engine_pairs_keep_reference_as_result_not_call(self):
        public = claude_public(entries([
            {'type': 'text', 'text': 'Available tool:'},
            {'type': 'tool_reference', 'tool_name': 'mcp__docs__lookup'}]), version=2)
        row = {'round_id': 'conversation:1', 'offset': 1, 'entry_hash': 'a'*64,
               'public': public}
        for source in ('claude', 'pi', 'codex'):
            for target in ('claude', 'pi', 'codex'):
                with self.subTest(source=source, target=target):
                    restored = roundtrip(target, {**row, 'public': roundtrip(source, row)})
                    self.assertEqual(restored, public)
                    self.assertEqual([x['name'] for x in restored
                                      if x['type'] == 'function_call'], ['ToolSearch'])

    def test_mixed_image_and_reference_keep_order_and_bytes(self):
        content = [{'type': 'text', 'text': 'before'},
                   {'type': 'tool_reference', 'tool_name': 'read'},
                   {'type': 'image', 'source': {'type': 'base64',
                       'media_type': 'image/png', 'data': PNG}},
                   {'type': 'text', 'text': 'after'}]
        public = claude_public(entries(content), version=2)
        output = public[-1]['output']
        self.assertEqual([b['type'] for b in output],
                         ['input_text', 'input_text', 'input_image', 'input_text'])
        self.assertEqual(output[2], image_block(PNG, 'image/png'))
        row = {'round_id': 'conversation:1', 'offset': 1, 'entry_hash': 'a'*64,
               'public': public}
        for engine in ('claude', 'pi', 'codex'):
            self.assertEqual(roundtrip(engine, row), public)

    def test_invalid_references_and_unknown_blocks_still_reject(self):
        for block in ({'type': 'tool_reference'},
                      {'type': 'tool_reference', 'tool_name': None},
                      {'type': 'tool_reference', 'tool_name': '  '},
                      {'type': 'unknown_result', 'text': 'do not silently drop'}):
            with self.subTest(block=block), self.assertRaises(InvalidState):
                claude_public(entries([block]), version=2)

    def test_reference_extensions_are_preserved_as_inert_text(self):
        ref = {'type': 'tool_reference', 'tool_name': 'read',
               'url': 'https://example.invalid', 'future_metadata': {'version': 2}}
        public = claude_public(entries([ref]), version=2)
        self.assertEqual(json.loads(public[-1]['output'].split('] ', 1)[1]), ref)
        self.assertEqual([x['type'] for x in public], ['function_call', 'function_call_output'])

    def test_support_is_scoped_to_claude_v2_tool_results(self):
        ref = {'type': 'tool_reference', 'tool_name': 'read'}
        with self.assertRaises(InvalidState):
            claude_public(entries([ref]), version=1)
        for engine in ('pi', 'codex'):
            with self.assertRaises(InvalidState):
                _tool_output([ref], engine, version=2)
        with self.assertRaises(InvalidState):
            claude_public([{'type': 'assistant', 'message': {
                'role': 'assistant', 'content': [ref]}}], version=2)

    def test_redacted_thinking_preserves_visible_history_without_leaking_ciphertext(self):
        source = entries([{'type': 'text', 'text': 'tool result'}])
        source[0]['message']['content'].insert(0, {
            'type': 'redacted_thinking', 'data': 'opaque-never-forward'})
        before = copy.deepcopy(source)
        public = claude_public(source, version=2)
        self.assertEqual(source, before)
        self.assertEqual(public[0]['type'], 'reasoning')
        self.assertNotIn('opaque-never-forward', json.dumps(public))
        row = {'round_id': 'conversation:1', 'offset': 1, 'entry_hash': 'a'*64, 'public': public}
        from shepherd.claude_codec import encode_rounds
        from shepherd.pi_codec import encode_round as pi_encode
        from shepherd.codex_codec import encode_round as codex_encode
        from shepherd.source_codec import pi_public, codex_public
        binding = {'binding_id': 'b', 'native_session_id': 'n', 'native_cwd': '/tmp'}
        self.assertEqual(claude_public(encode_rounds(binding, [row]), version=2), public)
        pi_entries = [item if item.get('type') == 'custom' else {'type': 'message', 'message': item}
                      for item in pi_encode(row)]
        self.assertEqual(pi_public(pi_entries, version=2), public)
        self.assertEqual(codex_public(codex_encode(row), version=2), public)
        source[0]['message']['content'][0]['data'] = {}
        with self.assertRaises(InvalidState): claude_public(source, version=2)

    def test_existing_text_and_failure_semantics_stay_unchanged(self):
        content = [{'type': 'text', 'text': 'before'}, {'type': 'text', 'text': 'after'}]
        self.assertEqual(_tool_output(content, 'claude', version=2), 'before\nafter')
        ref = [{'type': 'tool_reference', 'tool_name': 'read'}]
        self.assertTrue(_tool_output(ref, 'claude', failed=True, version=2)
                        .startswith('[工具执行失败] [Claude tool_reference] '))


if __name__ == '__main__':
    unittest.main()
