"""Sanitized native-event replay and presentation/execution boundary contracts."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from engine_adapters.response_timing import ResponseTiming
from engine_adapters.shepherd_events import EventProjection, EventContractError, normalize
from shepherd.store import InvalidState, SourceVerificationError
from test_engine_adapters_gui import ea, Recorder


REPLAYS = [
    ('claude', ({'type':'user','message':{'role':'user','content':'<command-name>/compact</command-name>'}},)),
    ('claude', ({'type':'user','isCompactSummary':True,'message':{'role':'user','content':'Previous context summary'}},)),
    ('claude', ({'type':'assistant','message':{'role':'assistant','content':'<local-command-stdout>Compacted</local-command-stdout>'}},)),
    ('pi', ({'type':'message_update','assistantMessageEvent':{'type':'text_delta','contentIndex':0,'delta':'hello'}},)),
    ('codex', ('item/started',{'item':{'id':'a','type':'agentMessage','text':'hello','phase':'final_answer'}})),
    ('codex', ('model/selected',{'model':'gpt-5.6-sol'})),
]


class EventContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def boundary(self, engine):
        cls = {'claude':ea.ClaudeAdapter,'pi':ea.PiAdapter,
               'codex':ea.CodexAdapter}[engine]
        worker = cls(self.tmp.name, self.tmp.name, Recorder())
        ctx = {'sid':'adp_event_contract','msg_id':'m','seq':1,'parts':{},'order':[], 't0':0}
        worker._live[ctx['sid']] = ctx
        warnings = []
        return EventProjection(engine, worker, ctx, warnings.append), warnings

    def test_sanitized_replays_use_real_translators_without_mutating_source(self):
        for engine, args in REPLAYS:
            with self.subTest(engine=engine,args=args):
                boundary, warnings = self.boundary(engine)
                before = copy.deepcopy(args)
                boundary(*args)
                self.assertEqual(args, before)
                self.assertFalse(warnings)
                self.assertNotIn('_projection_failures',boundary.ctx)

    def test_pi_projection_records_first_reasoning_and_text_without_event_payload(self):
        boundary, warnings = self.boundary('pi')
        boundary.ctx['_response_timing'] = ResponseTiming()
        boundary({'type':'message_update','assistantMessageEvent':{
            'type':'thinking_delta','contentIndex':0,'delta':'private reasoning'}})
        boundary({'type':'message_update','assistantMessageEvent':{
            'type':'text_delta','contentIndex':1,'delta':'private answer'}})
        boundary({'type':'message_update','assistantMessageEvent':{
            'type':'text_delta','contentIndex':1,'delta':'more private answer'}})
        marks = boundary.ctx['_response_timing'].marks
        self.assertEqual(set(marks), {'first_reasoning_ms','first_text_ms'})
        self.assertTrue(all(isinstance(value, (int, float)) and value >= 0
                            for value in marks.values()))
        self.assertNotIn('private', json.dumps(marks))
        self.assertFalse(warnings)

    def test_string_normalization_shares_text_and_leaves_raw_source_intact(self):
        event = REPLAYS[0][1][0]
        result, = normalize('claude',(event,))
        self.assertIs(result['message']['content'][0]['text'],event['message']['content'])
        self.assertIsInstance(event['message']['content'],str)
        block_event = {'type':'assistant','message':{'content':[{'type':'text','text':'hello'}]}}
        self.assertIs(normalize('claude',(block_event,))[0],block_event)

    def test_pi_string_transcripts_preserve_source_and_allow_following_answer(self):
        for role in ('system', 'user', 'custom'):
            for text in ('', 'private transcript'):
                with self.subTest(role=role, text=text):
                    boundary, warnings = self.boundary('pi')
                    for kind in ('message_start', 'message_end'):
                        event = {'type':kind, 'message':{'role':role, 'content':text,
                                 'sections':{'instructions':'private section'}}}
                        before = copy.deepcopy(event)
                        normalized, = normalize('pi', (event,))
                        self.assertEqual(normalized['message']['content'], [{'type':'text','text':text}])
                        self.assertIs(normalized['message']['sections'], event['message']['sections'])
                        boundary(event)
                        self.assertEqual(event, before)
                    boundary({'type':'message_start', 'message':{'role':'assistant','content':[]}})
                    boundary({'type':'message_update','assistantMessageEvent':{
                        'type':'text_delta','contentIndex':0,'delta':'READY'}})
                    answer = {'role':'assistant','content':[{'type':'text','text':'READY'}],
                              'stopReason':'stop'}
                    boundary({'type':'message_end','message':answer})
                    boundary({'type':'turn_end','message':answer})
                    self.assertFalse(warnings)
                    self.assertNotIn('_projection_failures', boundary.ctx)
                    self.assertTrue(boundary.ctx['parts'])

    def test_pi_assistant_tool_and_invalid_contents_still_fail_closed(self):
        for role, content in [('assistant','bad'), ('toolResult','bad'),
                              ('system',42), ('custom',{}), ('user',['bad'])]:
            with self.subTest(role=role):
                with self.assertRaises(EventContractError):
                    normalize('pi', ({'type':'message_end','message':{
                        'role':role, 'content':content}},))
        with self.assertRaises(EventContractError):
            normalize('pi', ({'type':'tool_execution_end','toolCallId':'call',
                              'result':{'content':'bad'}},))

    def test_malformed_display_shapes_warn_once_without_leaking_content(self):
        rows = [('claude',({'type':'user','message':{'content':['private secret']}},)),
                ('pi',({'type':'message_update','assistantMessageEvent':'private secret'},)),
                ('codex',('item/started',{'item':'private secret'}))]
        for engine,args in rows:
            boundary,warnings = self.boundary(engine)
            with self.assertLogs('engine_adapters.shepherd_events',level='ERROR') as logs:
                for _ in range(20): boundary(*args)
            self.assertEqual(len(warnings),1)
            self.assertEqual(len(logs.output),1)
            self.assertEqual(boundary.failures,20)
            self.assertNotIn('private secret',json.dumps(warnings)+''.join(logs.output))
            self.assertTrue(warnings[0]['field'])
            self.assertTrue(warnings[0]['stack'])
            self.assertFalse(boundary.ctx.get('_close_stdin'))
            self.assertNotIn('engine_error',boundary.ctx)

    def test_programming_error_in_display_is_visible_and_next_event_still_runs(self):
        for engine,args in REPLAYS:
            boundary,warnings = self.boundary(engine)
            method = '_app_event' if engine == 'codex' else 'handle_event'
            with mock.patch.object(boundary.worker,method,side_effect=[AttributeError('private text'),None]) as handler:
                with self.assertLogs('engine_adapters.shepherd_events',level='ERROR'):
                    boundary(*args)
                boundary(*args)
                self.assertEqual(handler.call_count,2)
            self.assertEqual(len(warnings),1)

    def test_permission_terminal_and_unknown_events_cannot_be_swallowed(self):
        rows = [('claude',({'type':'result','usage':'bad'},)),
                ('pi',({'type':'message_end','message':'bad'},)),
                ('codex',('turn/completed',{'turn':'bad'})),
                ('pi',({'type':'extension_ui_request','method':'confirm'},)),
                ('pi',({'type':'extension_error'},)),
                ('claude',({'type':'control_request'},)),
                ('claude',({'type':'future_control'},))]
        for engine,args in rows:
            boundary,warnings = self.boundary(engine)
            method = '_app_event' if engine == 'codex' else 'handle_event'
            with mock.patch.object(boundary.worker,method,side_effect=TypeError('control failure')):
                with self.assertLogs('engine_adapters.shepherd_events',level='ERROR'):
                    with self.assertRaises((EventContractError,TypeError)):boundary(*args)
            self.assertFalse(warnings)

    def test_storage_identity_and_unclassified_exceptions_still_abort(self):
        for error in (OSError('disk full'),SourceVerificationError('hash mismatch'),
                      InvalidState('invalid state'),RuntimeError('unknown failure')):
            boundary,warnings = self.boundary('claude')
            with mock.patch.object(boundary.worker,'handle_event',side_effect=error):
                with self.assertRaises(type(error)):boundary(*REPLAYS[0][1])
            self.assertFalse(warnings)

    def test_warning_persistence_failure_is_not_hidden(self):
        boundary,_ = self.boundary('claude')
        boundary.warn = mock.Mock(side_effect=OSError('cannot persist warning'))
        with self.assertLogs('engine_adapters.shepherd_events',level='ERROR'):
            with self.assertRaises(OSError):
                boundary({'type':'user','message':{'content':['bad block']}})

    def test_closed_or_superseded_turn_has_no_warning_or_side_effect(self):
        for closed in (False,True):
            boundary,warnings = self.boundary('claude')
            if closed:boundary.ctx['_lifecycle_closed']=True
            else:boundary.worker._live[boundary.ctx['sid']]={'seq':2}
            boundary({'type':'user','message':'bad'})
            self.assertFalse(warnings)
            self.assertEqual(boundary.failures,0)

    def test_malformed_envelope_fails_with_field_name(self):
        for engine,args in [('claude',('bad',)),('pi',({'type':[]},)),('codex',([],{}))]:
            boundary,warnings = self.boundary(engine)
            with self.assertLogs('engine_adapters.shepherd_events',level='ERROR'):
                with self.assertRaises(EventContractError):boundary(*args)
            self.assertFalse(warnings)
