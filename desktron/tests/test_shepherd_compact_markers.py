"""Automatic compaction is durable UI output, not a replayable summary."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from engine_adapters.bus import EventBus
from engine_adapters.lifecycle import compact_update
from engine_adapters.shepherd import ShepherdAdapter
from engine_adapters.shepherd_events import EventProjection
from test_shepherd_product import FakeService


class CompactMarkers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workspace = root / 'workspace'
        self.workspace.mkdir()
        self.data = root / 'data'
        self.adapter = ShepherdAdapter(str(self.data), str(self.workspace), EventBus())
        self.adapter.service = FakeService(str(root / 'state'))
        self.adapter.service.coordinator = mock.Mock()
        self.adapter.service.coordinator.state.return_value = None
        self.counter = 0
        promotion = mock.patch.object(ShepherdAdapter, '_schedule_storage_promotion')
        promotion.start()
        self.addCleanup(promotion.stop)
        self.sid = self.adapter.create_session('fixture', opts={'active_engine':'pi'})['id']

    def context(self, text='question'):
        self.counter += 1
        ctx = {'sid':self.sid, 'msg_id':f'msg_fixture_{self.counter}', 'seq':self.counter, 'text':text,
               'parts':{}, 'order':[], 'last_tool':None, 't0':int(time.time()*1000)}
        self.adapter._live[self.sid] = ctx
        return ctx

    def part(self, ctx, pid, kind='reasoning'):
        self.adapter._ensure_part(ctx, pid, kind, text=pid)

    def markers(self, ctx):
        return [ctx['parts'][pid] for pid in ctx['order']
                if ctx['parts'][pid]['type'] == 'compact']

    def test_real_engine_events_persist_in_start_order_through_reload_and_settlement(self):
        for engine in ('pi', 'claude', 'codex'):
            with self.subTest(engine=engine):
                ctx = self.context()
                self.part(ctx, 'before')
                worker = self.adapter._worker(engine)
                warnings = []
                projection = EventProjection(engine, worker, ctx, warnings.append)
                if engine == 'pi':
                    start = ({'type':'compaction_start', 'reason':'auto'},)
                    end = ({'type':'compaction_end', 'reason':'auto', 'status':'completed',
                            'result':{'summary':'PRIVATE SUMMARY', 'tokensBefore':1234}},)
                elif engine == 'claude':
                    start = ({'type':'system', 'subtype':'status', 'status':'compacting'},)
                    end = ({'type':'system', 'subtype':'status', 'status':'requesting',
                            'compact_result':'success'},)
                else:
                    start = ('item/started', {'item':{'id':'compact', 'type':'contextCompaction'}})
                    end = ('item/completed', {'item':{'id':'compact', 'type':'contextCompaction'}})
                native = copy.deepcopy((start, end))
                projection(*start)
                projection(*start)  # repeated status must not move the start anchor
                self.assertEqual(self.markers(ctx), [])
                self.part(ctx, 'after', 'text')
                projection(*end)
                projection(*end)
                self.assertFalse(warnings)
                self.assertEqual((start, end), native)
                marker, = self.markers(ctx)
                self.assertEqual(marker['compact'], {'trigger':'auto'})
                self.assertEqual(ctx['order'], ['before', marker['id'], 'after'])
                self.assertNotIn('PRIVATE SUMMARY', json.dumps(ctx['parts']))
                self.assertEqual(self.adapter.live_message(self.sid)['parts'],
                                 [ctx['parts'][pid] for pid in ctx['order']])
                checkpoint = self.adapter._read_live_checkpoint(self.sid)
                self.assertEqual(checkpoint['parts'], self.adapter.live_message(self.sid)['parts'])
                # A fresh adapter reads the same checkpoint after losing memory.
                restored = ShepherdAdapter(str(self.data), str(self.workspace), EventBus())
                self.assertEqual(restored.live_message(self.sid)['parts'], checkpoint['parts'])
                # Exercise the production settlement writer, not a fixture append.
                self.adapter._finish_turn(self.sid, ctx, None)
                self.assertEqual(self.adapter.messages(self.sid)[-1]['parts'], checkpoint['parts'])
                restored = ShepherdAdapter(str(self.data), str(self.workspace), EventBus())
                self.assertEqual(restored.messages(self.sid)[-1]['parts'], checkpoint['parts'])

    def test_failure_cancel_noop_retry_and_unknown_do_not_create_boundaries(self):
        for result in ('failed', 'cancelled', 'noop', 'retrying', 'unknown'):
            with self.subTest(result=result):
                ctx = self.context()
                compact_update(self.adapter, ctx, {'running':True, 'trigger':'auto'})
                compact_update(self.adapter, ctx, {'running':False, 'trigger':'auto', 'result':result})
                self.assertEqual(self.markers(ctx), [])
        ctx = self.context()
        compact_update(self.adapter, ctx, {'running':True})
        compact_update(self.adapter, ctx, {'running':False, 'trigger':'auto',
                                          'result':'success', 'error':'failed'})
        self.assertEqual(self.markers(ctx), [])

    def test_unpaired_terminals_and_manual_operation_remain_separate(self):
        ctx = self.context()
        compact_update(self.adapter, ctx, {'running':False, 'trigger':'auto', 'result':'success'})
        self.assertEqual(self.markers(ctx), [])
        ctx = self.context('/compact')
        compact_update(self.adapter, ctx, {'running':True, 'trigger':'auto'})
        compact_update(self.adapter, ctx, {'running':False, 'trigger':'auto', 'result':'success'})
        self.assertEqual(self.markers(ctx), [])

    def test_repeated_successes_keep_unique_ordered_markers_and_metadata(self):
        ctx = self.context()
        publish = mock.patch.object(self.adapter.bus, 'publish')
        events = publish.start()
        self.addCleanup(publish.stop)
        for i in range(2):
            self.part(ctx, f'before{i}')
            compact_update(self.adapter, ctx, {'running':True, 'trigger':'auto'})
            self.part(ctx, f'after{i}')
            compact_update(self.adapter, ctx, {'running':False, 'trigger':'auto',
                                              'pre':50000, 'post':10000, 'ms':12})
        markers = self.markers(ctx)
        self.assertEqual(len(markers), 2)
        self.assertNotEqual(markers[0]['id'], markers[1]['id'])
        self.assertEqual(ctx['order'], ['before0', markers[0]['id'], 'after0',
                                        'before1', markers[1]['id'], 'after1'])
        self.assertEqual(markers[0]['compact'], {'trigger':'auto', 'pre':50000, 'post':10000, 'ms':12})
        # The persisted part is intentionally not emitted as a second live row.
        self.assertFalse(any(call.args[0]['type'] == 'message.part.updated'
                             for call in events.call_args_list))

    def test_late_callback_does_not_mutate_closed_turn(self):
        ctx = self.context()
        compact_update(self.adapter, ctx, {'running':True, 'trigger':'auto'})
        ctx['_lifecycle_closed'] = True
        self.assertFalse(compact_update(self.adapter, ctx, {'running':False, 'trigger':'auto'}))
        self.assertEqual(self.markers(ctx), [])
