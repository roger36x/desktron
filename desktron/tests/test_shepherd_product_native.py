"""Pinned engines through the production Shepherd dispatcher and UI projection."""
import tempfile
import threading
import unittest
from contextlib import ExitStack
from unittest import mock

import pytest
from shepherd_product_fixture import ProductFixture


@pytest.mark.native
class ProductNative(unittest.TestCase):
    def test_pi_system_transcript_survives_reopen_and_following_turn(self):
        with tempfile.TemporaryDirectory() as root:
            product = ProductFixture(root)
            try:
                for index in range(3):
                    if index == 1:
                        product.reopen()
                    error, ctx = product.turn('pi', 'verify Pi transcript and continuation')
                    self.assertIsNone(error)
                    self.assertNotIn('_projection_failures', ctx)
                    self.assertTrue(ctx['parts'])
                state = product.adapter.service.coordinator.state(product.sid)
                self.assertIsNone(state.get('active_turn'))
                self.assertEqual([turn['outcome'] for turn in state['turns']], ['completed'] * 3)
            finally:
                product.close()

    def test_four_engines_send_compact_switch_and_continue(self):
        with tempfile.TemporaryDirectory() as root, ExitStack() as cleanup:
            product = ProductFixture(root)
            cleanup.callback(product.close)
            engines = ('claude','pi','codex','claude','pi')
            for index,engine in enumerate(engines):
                with self.subTest(engine=engine):
                    if index==4:
                        product.reopen()
                    error,ctx = product.turn(engine,'hello from product fixture' +
                        (' archived detail' * 16000 if engine in ('claude','pi') else ''))
                    self.assertIsNone(error)
                    self.assertNotIn('_projection_failures',ctx)
                    self.assertTrue(ctx['parts'])
                    if engine in ('claude','pi','codex'):
                        from engine_adapters.command_manifest import resolve
                        available=product.adapter.compact_available(product.sid)
                        self.assertTrue(available)
                        manifest=resolve(engine=engine,shepherd=True,caps={'compact':available})
                        self.assertIn('/compact',[c['name'] for c in manifest['commands']])
                        result,ctx = product.compact(engine)
                        self.assertTrue(result['ok'],result)
                        self.assertIsNotNone(ctx['_shepherd_runner'].checkpoint)
                        self.assertNotIn('_projection_failures',ctx)
                        error,ctx = product.turn(engine,'continue after compact')
                        self.assertIsNone(error)
                        self.assertNotIn('_projection_failures',ctx)
            state=product.adapter.service.coordinator.state(product.sid)
            self.assertIsNone(state.get('active_turn'))
            self.assertTrue(all(t['outcome']=='completed' for t in state['turns']))

    def test_display_failure_preserves_archive_terminal_and_following_turn(self):
        for engine in ('claude','pi','codex'):
            with self.subTest(engine=engine),tempfile.TemporaryDirectory() as root:
                product = ProductFixture(root)
                try:
                    ctx=product.context(engine,'hello with display failure')
                    worker=product.adapter._worker(engine)
                    method='_app_event' if engine == 'codex' else 'handle_event'
                    handler=getattr(worker,method)
                    failed=[]
                    def broken(*args):
                        event=args[0]
                        name=event if isinstance(event,str) else event['type']
                        if name in ('stream_event','message_update','item/started') and not failed:
                            failed.append(name)
                            raise AttributeError('synthetic display regression')
                        return handler(*args)
                    with mock.patch.object(worker,method,side_effect=broken):
                        with self.assertLogs('engine_adapters.shepherd_events',level='ERROR'):
                            self.assertIsNone(product.dispatch(engine,ctx))
                    self.assertTrue(failed)
                    self.assertEqual(ctx['_projection_failures'],1)
                    notice=product.adapter.session_environment(product.sid)
                    self.assertEqual(notice['state'],'warning')
                    self.assertEqual(notice['scope'],'turn')
                    state=product.adapter.service.coordinator.state(product.sid)
                    turn=state['turns'][-1]
                    self.assertEqual(turn['outcome'],'completed')
                    connector=product.adapter.service.connector(state['bindings'][turn['producer_binding_id']])
                    source=connector.resolve_source(turn['source_ref'])
                    self.assertIn('image-fixture-ok',str(source))
                    product.reopen()
                    self.assertEqual(product.adapter.session_environment(product.sid)['state'],'warning')
                    product.adapter._clear_turn_error_environment(product.sid)
                    self.assertIsNone(product.adapter.session_environment(product.sid))
                    error,_=product.turn(engine,'next turn remains usable')
                    self.assertIsNone(error)
                finally:product.close()

    def test_four_engines_cancel_and_continue_through_product_dispatch(self):
        for engine in ('claude','pi','codex'):
            with self.subTest(engine=engine),tempfile.TemporaryDirectory() as root:
                requested,release=threading.Event(),threading.Event()
                def wait(_):
                    requested.set();release.wait(30);return 100
                product=ProductFixture(root,input_tokens=wait)
                thread=None
                try:
                    ctx=product.context(engine,'wait before cancel')
                    results=[]
                    def run():
                        try:results.append(product.dispatch(engine,ctx))
                        except Exception as exc:results.append(exc)
                    thread=threading.Thread(target=run,daemon=True);thread.start()
                    self.assertTrue(requested.wait(20))
                    ctx['aborted']='interrupt'
                    self.assertTrue(product.adapter._soft_interrupt(product.sid,ctx.get('proc')))
                    thread.join(15)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(len(results),1)
                    self.assertNotIsInstance(results[0],Exception)
                    state=product.adapter.service.coordinator.state(product.sid)
                    self.assertEqual(state['turns'][-1]['outcome'],'interrupted')
                    self.assertNotIn('_projection_failures',ctx)
                    release.set()
                    error,ctx=product.turn(engine,'continue after cancellation')
                    self.assertIsNone(error)
                    self.assertNotIn('_projection_failures',ctx)
                finally:
                    release.set()
                    if thread:thread.join(20)
                    product.close()

    def test_four_engines_cancel_manual_compaction_then_continue(self):
        for engine in ('claude','pi','codex'):
            with self.subTest(engine=engine),tempfile.TemporaryDirectory() as root:
                product=ProductFixture(root)
                requested,release=threading.Event(),threading.Event()
                thread=None
                try:
                    # Remain within Claude's native window before the explicit
                    # compact; this case verifies cancellation, not overflow.
                    error,_=product.turn(engine,'previous context ' * 8000)
                    self.assertIsNone(error)
                    error,_=product.turn(engine,'second context ' * 8000)
                    self.assertIsNone(error)
                    result,_=product.compact(engine)
                    self.assertTrue(result['ok'], result)
                    error,_=product.turn(engine,'more context after checkpoint ' * 8000)
                    self.assertIsNone(error)
                    error,_=product.turn(engine,'next context after checkpoint ' * 8000)
                    self.assertIsNone(error)
                    active=product.adapter.service.active(product.sid)
                    archive=product.adapter.service.connector(active['binding']).archive
                    checkpoints=archive.checkpoint_candidates()
                    self.assertTrue(checkpoints)
                    def pause(_path):
                        requested.set();release.wait(30)
                    product.before_request=pause
                    results=[]
                    def run():
                        try:results.append(product.compact(engine))
                        except Exception as exc:results.append(exc)
                    thread=threading.Thread(target=run,daemon=True);thread.start()
                    self.assertTrue(requested.wait(20), str(results)[:1800])
                    ctx=product.adapter._live[product.sid]
                    ctx['aborted']='interrupt'
                    product.adapter._soft_interrupt(product.sid,ctx.get('proc'))
                    thread.join(15)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(len(results),1)
                    self.assertNotIsInstance(results[0],Exception)
                    if engine=='claude':
                        # 2.1.268 emits compact_result=failed + a successful
                        # local-command result, not an aborted terminal. Do
                        # not relabel a native failure from English wording.
                        self.assertEqual(results[0][0]['result'],'failed')
                        self.assertFalse(results[0][0]['ok'])
                    else:
                        self.assertEqual(results[0][0], {'ok':True,'result':'cancelled'},
                            ''.join(getattr(ctx['_shepherd_runner'].process,'errors',()))[-4000:])
                    state=product.adapter.service.coordinator.state(product.sid)
                    self.assertEqual(state['turns'][-1]['outcome'],
                                     'failed' if engine=='claude' else 'interrupted')
                    self.assertIsNone(ctx['_shepherd_runner'].checkpoint)
                    self.assertEqual(archive.checkpoint_candidates(), checkpoints)
                    finishes=[e['properties'] for e in product.bus.events
                              if e['type']=='session.compact' and not e['properties'].get('running')]
                    self.assertEqual(finishes[-1]['result'], 'failed' if engine=='claude' else 'cancelled')
                    self.assertEqual(bool(finishes[-1]['error']),engine=='claude')
                    release.set()
                    product.before_request=None
                    error,_=product.turn(engine,'continue after cancelled compaction')
                    self.assertIsNone(error)
                finally:
                    release.set()
                    if thread:thread.join(20)
                    product.close()
