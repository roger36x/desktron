import unittest

from shepherd.claude_codec import encode_rounds
from shepherd.pi_codec import encode_round as pi_encode
from shepherd.codex_codec import encode_round as codex_encode
from shepherd.source_codec import claude_public, pi_public, codex_public


class SemanticRoundtrip(unittest.TestCase):
    def test_unknown_reasoning_is_not_silently_dropped(self):
        row={'round_id':'r','offset':1,'entry_hash':'h','public':[
            {'type':'reasoning','summary':[{'type':'unknown','data':'evidence'}]}]}
        for encode in (pi_encode, codex_encode,
                       lambda r:encode_rounds({'binding_id':'b','native_session_id':'n','native_cwd':'/tmp'},[r])):
            with self.assertRaises(ValueError): encode(row)

    def test_custom_code_reasoning_and_error_survive_all_engines(self):
        public = [
            {'type':'reasoning','summary':[{'type':'summary_text','text':'check first'}]},
            {'type':'custom_tool_call','call_id':'call1','name':'functions.exec','input':'const x = 1;\ntext(x);'},
            {'type':'custom_tool_call_output','call_id':'call1','output':'[工具执行失败] denied'},
        ]
        binding = {'binding_id':'b','native_session_id':'n','native_cwd':'/tmp'}
        row = lambda p: {'round_id':'r','offset':1,'entry_hash':'h','public':p}
        claude = encode_rounds(binding, [row(public)])
        self.assertNotIn('_shepherd_', str([entry['message'] for entry in claude]))
        self.assertEqual(claude_public(claude), public)
        pi = pi_encode(row(claude_public(claude)))
        entries = [item if item.get('type') == 'custom' else {'type':'message','message':item} for item in pi]
        self.assertEqual(pi_public(entries), public)
        self.assertEqual(codex_public(codex_encode(row(pi_public(entries)))), public)

    def test_sanitized_tool_names_round_trip(self):
        public = [{'type':'function_call','call_id':'c','name':'mcp.foo/bar','arguments':'{}'},
                  {'type':'function_call_output','call_id':'c','output':'ok'}]
        entries = encode_rounds({'binding_id':'b','native_session_id':'n','native_cwd':'/tmp'},
                                [{'round_id':'r','public':public}])
        self.assertEqual(claude_public(entries), public)

    def test_legacy_projection_does_not_change_on_upgrade(self):
        reasoning = [{'type':'reasoning','summary':[{'type':'summary_text','text':'native thought'}]}]
        self.assertEqual(codex_public(reasoning), [])
        self.assertEqual(codex_public(reasoning, version=2), reasoning)
        pi = [{'type':'message','message':{'role':'toolResult','toolCallId':'c',
              'isError':True,'content':[{'type':'text','text':'denied'}]}}]
        self.assertEqual(pi_public(pi)[0]['output'], 'denied')
        self.assertEqual(pi_public(pi, version=2)[0]['output'], '[工具执行失败] denied')
        claude = [{'type':'user','message':{'role':'user','content':[
            {'type':'tool_result','tool_use_id':'c','content':'denied','is_error':True}]}}]
        self.assertEqual(claude_public(claude)[0]['output'], 'denied')
        self.assertEqual(claude_public(claude, version=2)[0]['output'], '[工具执行失败] denied')

    def test_legacy_claude_import_can_be_reencoded_without_new_metadata(self):
        binding = {'binding_id':'b','native_session_id':'n','native_cwd':'/tmp'}
        row = {'round_id':'r','public':[{'type':'reasoning','summary':[{'type':'summary_text','text':'thought'}]}]}
        old = encode_rounds(binding,[row],legacy_rounds={'r'})
        self.assertNotIn('semantics_v2', old[0]['shepherd'])
        self.assertEqual(claude_public(old)[0]['type'], 'message')
