"""工具失败不会冒充成功；审批与无进展可查询且不会互相遮盖。"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from test_codex_approval import ea, Bus, FakeConn, ASK
from engine_adapters.codex_tool_result import result_state

PATCH_ERROR = 'Script failed\nWall time 0.0 seconds\nOutput:\nScript error:\napply_patch verification failed: invalid patch: multiple operations target /tmp/example.html'


class Progress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus()
        self.ad = ea.CodexAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.sid = self.ad.create_session()['id']
        self.ctx = {'sid': self.sid, 'msg_id': 'm', 'seq': 1, 'parts': {}, 'order': [],
                    'esid': 'thread-1', 'engine_turn_id': 'turn-1',
                    't0': int((time.time()-300)*1000), '_line_at': time.time()-200,
                    '_progress_at': time.time()-200}
        self.ad._live[self.sid] = self.ctx

    def tearDown(self):
        self.tmp.cleanup()

    def test_wrapper_failure_live_and_history_agree(self):
        item = {'id':'call', 'type':'mcpToolCall', 'server':'functions', 'tool':'exec',
                'status':'completed', 'result':{'content':[{'type':'text', 'text':PATCH_ERROR}]}}
        # MCP 调用本身完成，结果内容中的脚本执行失败。
        with mock.patch.object(self.ad, '_live_snap'):
            self.ad._item(self.ad._app_item(item), self.ctx, done=True)
        live = next(iter(self.ctx['parts'].values()))['state']
        self.assertEqual(live['status'], 'error')
        self.assertEqual(live['failure']['kind'], 'validation')
        rows = [
            {'type':'response_item', 'payload': {'type':'message','role':'user','content':[{'text':'update'}]}},
            {'type':'response_item', 'payload': {'type':'custom_tool_call','name':'exec','call_id':'call','input':'text(await tools.apply_patch("..."));'}},
            {'type':'response_item', 'payload': {'type':'custom_tool_call_output','call_id':'call','output':[{'type':'input_text','text':PATCH_ERROR}]}},
        ]
        path = Path(self.tmp.name)/'rollout.jsonl'
        path.write_text('\n'.join(json.dumps(dict(r,timestamp=f'2026-09-08T12:40:0{i}.000Z')) for i,r in enumerate(rows)))
        messages = self.ad._group_rollout(str(path), self.sid)
        hist = [p['state'] for m in messages for p in m['parts'] if p['type']=='tool'][0]
        self.assertEqual(hist['status'],live['status'])
        self.assertEqual(hist['failure'],live['failure'])
        self.assertEqual(self.ad.turn_state(self.sid)['activity']['last_failure']['kind'], 'validation')

    def test_existing_stored_projection_is_repaired_without_rewriting_history(self):
        path=Path(self.ad._msg_path(self.sid))
        row={'info':{'role':'assistant','id':'old'},'parts':[{'id':'p','type':'tool','tool':'bash',
             'state':{'status':'done','input':{},'output':PATCH_ERROR}}]}
        original=json.dumps(row)+'\n';path.write_text(original)
        state=self.ad.messages(self.sid)[0]['parts'][0]['state']
        self.assertEqual(state['status'],'error')
        self.assertEqual(state['failure']['kind'],'validation')
        self.assertEqual(path.read_text(),original)

    def test_structured_errors_and_plain_output_not_guessed(self):
        for item in [{'exit_code': 1}, {'result': {'isError':True,'content':[{'text':'failed'}]}},
                     {'success':False}, {'error':{'message':'refused'}},
                     {'output': 'Script completed\nWall time 0.1 seconds\nOutput:\n'+json.dumps({'exit_code':2,'output':'bad args'})}]:
            self.assertEqual(result_state(item)['status'],'error',item)
        for text in ['error rate: 0', 'Documentation: apply_patch verification failed: example',
                     'Script completed\nWall time 0.1 seconds\nOutput:\nexample says Script failed',
                     'user rejected is a test fixture']:
            self.assertEqual(result_state({'output':text})['status'],'done')
        self.assertEqual(result_state({'output':PATCH_ERROR}, 'running')['status'],'running')

    def test_failed_command_output_stays_in_detail(self):
        log = 'Warning: truncated output\nTotal output lines: 11\n' + 'pytest failure trace\n' * 80
        wrapped = 'Script completed\nWall time 0.1 seconds\nOutput:\n' + json.dumps(
            {'exit_code': 1, 'output': log})
        item = {'id': 'call-log', 'type': 'mcpToolCall', 'server': 'functions',
                'tool': 'exec', 'status': 'completed',
                'result': {'content': [{'type': 'text', 'text': wrapped}]}}
        with mock.patch.object(self.ad, '_live_snap'):
            self.ad._item(self.ad._app_item(item), self.ctx, done=True)
        state = next(iter(self.ctx['parts'].values()))['state']
        self.assertEqual(state['status'], 'error')
        self.assertEqual(state['failure']['exit_code'], 1)
        self.assertEqual(state['failure']['message'], '工具执行失败（退出码 1）')
        self.assertIn('pytest failure trace', state['output'])
        for field in ('stderr', 'content', 'output'):
            failure = result_state({'isError': True, field: log})['failure']
            self.assertEqual(failure['message'], '工具执行失败')
        failure = result_state({'exit_code': 2, 'error': '权限不足', 'output': log})['failure']
        self.assertEqual(failure['message'], '权限不足')
        self.assertEqual(failure['exit_code'], 2)

    def test_metadata_and_empty_delta_do_not_mask_no_progress(self):
        self.ctx['_progress_at'] = time.time() - 1000
        for method, params in (
                ('thread/tokenUsage/updated', {'tokenUsage':{'total':{'totalTokens':7}}}),
                ('thread/status/changed', {'status':{'type':'active'}}),
                ('item/agentMessage/delta', {'itemId':'m', 'delta':''}),
                ('item/started', {'item':{'type':'reasoning','id':'r'}})):
            self.ad._app_event(method, params, self.ctx)
            a=self.ad.turn_state(self.sid)['activity']
            self.assertEqual(a['kind'],'no_progress', method)
            self.assertGreaterEqual(a['seconds'],900)
            self.assertGreater(a['last_event_at'],a['last_progress_at'])
        for params in (
                {'threadId':'thread-1', 'itemId':'m', 'delta':'missing turn'},
                {'turnId':'turn-1', 'itemId':'m', 'delta':'missing thread'},
                {'threadId':'other', 'turnId':'other-turn', 'itemId':'m', 'delta':'foreign'}):
            self.ad._app_event('item/agentMessage/delta', params, self.ctx)
            self.assertEqual(self.ad.turn_state(self.sid)['activity']['kind'],'no_progress')
        self.ad._app_event('item/agentMessage/delta', {
            'threadId':'thread-1', 'turnId':'turn-1', 'itemId':'m', 'delta':'字'}, self.ctx)
        self.assertEqual(self.ad.turn_state(self.sid)['activity']['kind'],'working')

    def test_total_only_usage_does_not_invent_input_output_detail(self):
        self.ad._app_event('thread/tokenUsage/updated', {
            'tokenUsage': {'total': {'totalTokens': 70, 'cachedInputTokens': 20}}
        }, self.ctx)
        self.assertEqual(self.ctx['_token_usage'], {'total': 70, 'cached': 20})

        self.ad._app_event('thread/tokenUsage/updated', {
            'tokenUsage': {'total': {'totalTokens': 70, 'inputTokens': 50,
                                      'outputTokens': 20, 'cachedInputTokens': 10}}
        }, self.ctx)
        self.assertEqual(self.ctx['_token_usage'], {
            'total': 70, 'input': 50, 'output': 20, 'cached': 10,
        })

    def test_cumulative_thread_usage_records_each_native_request_once(self):
        self.ad._app_event('thread/tokenUsage/updated', {'tokenUsage': {
            'total': {'totalTokens': 100, 'inputTokens': 80, 'outputTokens': 20,
                      'cachedInputTokens': 50},
            'last': {'totalTokens': 30, 'inputTokens': 20, 'outputTokens': 10,
                     'cachedInputTokens': 15}}}, self.ctx)
        self.ad._app_event('thread/tokenUsage/updated', {'tokenUsage': {
            'total': {'totalTokens': 160, 'inputTokens': 125, 'outputTokens': 35,
                      'cachedInputTokens': 80},
            'last': {'totalTokens': 60, 'inputTokens': 45, 'outputTokens': 15,
                     'cachedInputTokens': 30}}}, self.ctx)
        # A repeated terminal snapshot is a refinement/duplicate, not another charge.
        self.ad._app_event('thread/tokenUsage/updated', {'tokenUsage': {
            'total': {'totalTokens': 160, 'inputTokens': 125, 'outputTokens': 35,
                      'cachedInputTokens': 80},
            'last': {'totalTokens': 60, 'inputTokens': 45, 'outputTokens': 15,
                     'cachedInputTokens': 30}}}, self.ctx)
        self.assertEqual(self.ctx['_token_usage'], {
            'total': 90, 'input': 65, 'output': 25, 'cached': 45})

    def test_raw_response_echoes_and_unknown_items_are_not_progress(self):
        self.ctx['_progress_at'] = time.time() - 1000
        for item in (
                {'type':'message', 'role':'developer', 'content':[{'text':'rules'}]},
                {'type':'message', 'role':'user', 'content':[{'text':'question'}]},
                {'type':'message', 'role':'assistant', 'content':[]},
                {'type':'opaqueMetadata', 'value':'alive'}):
            self.ad._app_event('rawResponseItem/completed', {
                'threadId':'thread-1', 'turnId':'turn-1', 'item':item}, self.ctx)
            self.assertEqual(self.ad.turn_state(self.sid)['activity']['kind'], 'no_progress')
        self.ad._app_event('rawResponseItem/completed', {
            'threadId':'thread-1', 'turnId':'turn-1', 'item':{
                'type':'message', 'role':'assistant',
                'content':[{'type':'output_text', 'text':'historical answer'}]}}, self.ctx)
        self.assertEqual(self.ad.turn_state(self.sid)['activity']['kind'], 'no_progress')
        self.ad._app_event('item/completed', {
            'threadId':'thread-1', 'turnId':'turn-1',
            'item':{'type':'agentMessage', 'id':'m', 'text':'current answer'}}, self.ctx)
        self.assertEqual(self.ad.turn_state(self.sid)['activity']['kind'], 'working')

    def test_stream_recovery_requires_exact_current_turn_identity(self):
        self.ctx['stream_retrying'] = {'attempt': 2}
        with mock.patch.object(self.ad, '_stream_recovered') as recovered:
            for params in (
                    {'threadId':'thread-1', 'turn':{'status':'completed'}},
                    {'turnId':'turn-1', 'turn':{'id':'turn-1', 'status':'completed'}},
                    {'threadId':'other', 'turn':{'id':'other-turn', 'status':'completed'}}):
                self.ad._app_event('turn/completed', params, self.ctx)
                self.assertIn('stream_retrying', self.ctx)
            self.ad._app_event('turn/completed', {
                'threadId':'thread-1',
                'turn':{'id':'turn-1', 'status':'completed'}}, self.ctx)
            recovered.assert_called_once_with(self.ctx)

    def test_known_identified_tool_lifecycle_is_semantic_progress(self):
        self.ctx['_progress_at'] = time.time() - 1000
        self.ad._app_event('item/started', {
            'threadId':'thread-1', 'turnId':'turn-1',
            'item':{'type':'commandExecution', 'status':'inProgress'}}, self.ctx)
        self.assertEqual(self.ad.turn_state(self.sid)['activity']['kind'],'no_progress')
        self.ad._app_event('item/started', {
            'threadId':'thread-1', 'turnId':'turn-1',
            'item':{'type':'commandExecution', 'id':'cmd', 'status':'inProgress'}}, self.ctx)
        self.assertEqual(self.ad.turn_state(self.sid)['activity']['kind'],'tool')
        self.assertGreater(self.ctx['_progress_at'], time.time() - 5)

    def test_plan_and_unattributed_message_are_not_semantic_progress(self):
        self.ctx['_progress_at'] = time.time() - 1000
        for item in (
                {'type':'plan', 'id':'plan', 'text':'metadata plan'},
                {'type':'message', 'id':'message', 'content':[{'text':'missing role'}]}):
            self.ad._app_event('item/completed', {
                'threadId':'thread-1', 'turnId':'turn-1', 'item':item}, self.ctx)
            self.assertEqual(self.ad.turn_state(self.sid)['activity']['kind'],'no_progress')

    def test_all_parallel_tools_and_approval_have_priority(self):
        self.ctx['parts']={'a':{'type':'tool','state':{'status':'running'}},
                           'b':{'type':'tool','state':{'status':'done'}}}
        self.ctx['last_tool']='b'
        self.assertEqual(self.ad.turn_state(self.sid)['activity']['kind'],'tool')
        self.ctx['parts']['a']['state']['status']='error'
        self.ctx['_progress_at'] = time.time() - 1000
        self.assertEqual(self.ad.turn_state(self.sid)['activity']['kind'],'no_progress')
        with mock.patch('threading.Timer'):
            self.ad._approval_ask(self.sid,ASK,FakeConn())
        self.assertEqual(self.ad.turn_state(self.sid)['activity']['kind'],'approval')
        self.ctx['aborted']='interrupt'
        self.assertEqual(self.ad.turn_state(self.sid)['activity']['kind'],'cancelling')
        self.ad._clear_session_approvals(self.sid)

    def test_runner_soft_stall_keeps_running_tool_visible_without_terminal(self):
        self.ctx['parts']={'a':{'type':'tool','state':{'status':'running'}}}
        self.ctx['_runner_stall']={'since':time.time()-901,'active_tools':1}
        activity=self.ad.turn_state(self.sid)['activity']
        self.assertEqual(activity['kind'],'no_progress')
        self.assertTrue(activity['soft'])
        self.assertEqual(activity['running_tools'],1)
        self.assertGreaterEqual(activity['seconds'],900)
        self.assertNotIn('turn_failed',self.ctx)
        self.ctx.pop('_runner_stall')
        self.assertEqual(self.ad.turn_state(self.sid)['activity']['kind'],'tool')

    def test_timeout_resolves_once_and_snapshot_clears_asks(self):
        conn=FakeConn()
        with mock.patch('threading.Timer') as timer:
            self.ad._approval_ask(self.sid, ASK, conn)
            rid=self.ad.turn_state(self.sid)['asks'][0]['id']
            timer.call_args.args[1]()
        resolved=[e['properties'] for e in self.bus.events if e['type']=='permission.resolved']
        self.assertEqual(len(resolved),1)
        self.assertEqual(resolved[0]['resolution'],'timeout')
        self.assertEqual(resolved[0]['asks'],[])
        self.assertEqual(self.ad.turn_state(self.sid)['asks'],[])
        self.assertFalse(self.ad.respond_permission(rid,True))
        self.assertEqual(len(conn.answered),1)
        self.assertEqual(conn.answered[0][1],{'decision':'cancel'})

    def test_failed_approval_delivery_is_not_reported_approved(self):
        conn=mock.Mock();conn.answer_approval.side_effect=OSError('closed')
        with mock.patch('threading.Timer'):
            self.ad._approval_ask(self.sid,ASK,conn)
        rid=self.ad.turn_state(self.sid)['asks'][0]['id']
        self.assertFalse(self.ad.respond_permission(rid,True))
        event=self.bus.events[-1]
        self.assertEqual(event['properties']['resolution'],'connection_error')

    def test_ready_snapshot_has_no_stale_activity(self):
        self.ad._live.pop(self.sid)
        state=self.ad.turn_state(self.sid)
        self.assertEqual(state['asks'],[])
        self.assertEqual(state['activity']['kind'],'idle')


if __name__=='__main__': unittest.main()
