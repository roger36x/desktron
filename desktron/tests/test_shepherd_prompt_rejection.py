"""Pre-turn rejection is correlated with the request, not message text."""
import subprocess
import unittest
from unittest import mock

from test_frontend_recall import ROOT, fn
from test_peer_message_route import Adapter, Handler, ui_contract


class PromptRejection(unittest.TestCase):
    def test_invalid_image_returns_explicit_unaccepted_error(self):
        from shepherd.image_content import ImageInputError
        from engine_adapters.attachment_context import AttachmentInputError
        for error in (ImageInputError('图片格式无效'), AttachmentInputError('附件无法保存')):
            with self.subTest(code=error.code):
                ad = Adapter()
                ad.prompt_async = mock.Mock(side_effect=error)
                h = Handler('public', {'parts':[{'type':'text','text':'hello'},
                                              {'type':'image','data_b64':'bad','media_type':'image/png'}]})
                with mock.patch.object(ui_contract, '_adapter', return_value=ad), \
                     mock.patch.object(ui_contract, '_engine_pref', return_value='pi'), \
                     mock.patch.object(ui_contract, '_wb_state_read', return_value={}), \
                     mock.patch.object(Handler, '_adapter_owning', return_value=ad):
                    h._adapter_route('POST')
                self.assertEqual(h.code,400)
                self.assertEqual(h.out,{'error':str(error),'code':error.code})

    def test_aggregate_input_rejection_returns_413_without_generic_failure(self):
        from shepherd.turn_inputs import InputTooLarge
        ad = Adapter(); ad.name = 'shepherd'
        ad.prompt_async = mock.Mock(side_effect=InputTooLarge('本轮输入过大'))
        h = Handler('public', {'parts':[{'type':'text','text':'hello'}]})
        with mock.patch.object(ui_contract, '_adapter', return_value=ad), \
             mock.patch.object(ui_contract, '_engine_pref', return_value='pi'), \
             mock.patch.object(ui_contract, '_wb_state_read', return_value={}), \
             mock.patch.object(Handler, '_adapter_owning', return_value=ad):
            h._adapter_route('POST')
        self.assertEqual(h.code, 413)
        self.assertEqual(h.out, {'error':'本轮输入过大','code':'turn_input_too_large'})

    def test_image_transport_rejection_returns_413_without_dispatch(self):
        from shepherd.image_transport import ImageTransportError
        ad = Adapter(); ad.name = 'shepherd'
        ad.prompt_async = mock.Mock(side_effect=ImageTransportError('图片传输过大'))
        h = Handler('public', {'parts':[{'type':'text','text':'hello'}]})
        with mock.patch.object(ui_contract, '_adapter', return_value=ad), \
             mock.patch.object(ui_contract, '_engine_pref', return_value='pi'), \
             mock.patch.object(ui_contract, '_wb_state_read', return_value={}), \
             mock.patch.object(Handler, '_adapter_owning', return_value=ad):
            h._adapter_route('POST')
        self.assertEqual(h.code, 413)
        self.assertEqual(h.out['code'], 'image_transport_too_large')

    def test_oversized_transport_rejects_before_body_read_on_all_prompt_aliases(self):
        from shepherd.turn_inputs import MAX_PROMPT_BODY_BYTES
        for alias in ('prompt_async','prompt','message'):
            with self.subTest(alias=alias):
                ad = Adapter(); ad.name = 'shepherd'
                ad.prompt_async = mock.Mock()
                h = Handler('public', {})
                h.path = '/session/public/' + alias
                h.headers['Content-Length'] = str(MAX_PROMPT_BODY_BYTES + 1)
                h.rfile = mock.Mock()
                h.rfile.read.side_effect = AssertionError('oversized body must not be read')
                with mock.patch.object(ui_contract, '_adapter', return_value=ad), \
                     mock.patch.object(ui_contract, '_engine_pref', return_value='pi'), \
                     mock.patch.object(Handler, '_adapter_owning', return_value=ad):
                    h._adapter_route('POST')
                self.assertEqual(h.code, 413)
                self.assertEqual(h.out['code'], 'prompt_body_too_large')
                self.assertTrue(h.close_connection)
                ad.prompt_async.assert_not_called()
                h.rfile.read.assert_not_called()

    def test_real_http_oversize_closes_without_waiting_for_body_or_501(self):
        import http.client
        import json
        import socket
        import socketserver
        import threading
        from test_http_body_drain import _Handler as BodyHandler
        from shepherd.turn_inputs import MAX_PROMPT_BODY_BYTES
        ad = Adapter(); ad.name = 'shepherd'
        ad.prompt_async = mock.Mock()

        class QuotaHandler(ui_contract.UiContractMixin, BodyHandler):
            def _adapter_owning(self, _sid):
                return ad

            def do_POST(self):
                self._adapter_route('POST')

            def _send_json(self, obj, code=200):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header('Content-Length',str(len(raw)))
                if self.close_connection:
                    self.send_header('Connection','close')
                self.end_headers(); self.wfile.write(raw)

        server = socketserver.ThreadingTCPServer(('127.0.0.1',0),QuotaHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            with mock.patch.object(ui_contract, '_adapter', return_value=ad), \
                 mock.patch.object(ui_contract, '_engine_pref', return_value='pi'):
                with socket.create_connection(server.server_address,timeout=2) as sock:
                    sock.settimeout(2)
                    sock.sendall((f'POST /session/public/prompt_async HTTP/1.1\r\n'
                        f'Host: localhost\r\nContent-Length: {MAX_PROMPT_BODY_BYTES + 1}\r\n'
                        f'Connection: keep-alive\r\n\r\nx'
                        f'GET /after HTTP/1.1\r\nHost: localhost\r\n\r\n').encode())
                    chunks = []
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk: break
                        chunks.append(chunk)
                    reply = b''.join(chunks)
                    self.assertIn(b' 413 ',reply)
                    self.assertIn(b'Connection: close',reply)
                    self.assertNotIn(b' 501 ',reply)
                    self.assertNotIn(b' 200 ',reply)
                conn = http.client.HTTPConnection(*server.server_address,timeout=2)
                try:
                    conn.request('GET','/after'); response = conn.getresponse(); response.read()
                    self.assertEqual(response.status,200)
                finally:
                    conn.close()
                ad.prompt_async.assert_not_called()
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_transport_exact_boundary_reaches_adapter(self):
        from shepherd import turn_inputs
        ad = Adapter(); ad.name = 'shepherd'
        h = Handler('public', {'parts':[{'type':'text','text':'hello'}]})
        with mock.patch.object(turn_inputs, 'MAX_PROMPT_BODY_BYTES', int(h.headers['Content-Length'])), \
             mock.patch.object(ui_contract, '_adapter', return_value=ad), \
             mock.patch.object(ui_contract, '_engine_pref', return_value='pi'), \
             mock.patch.object(ui_contract, '_wb_state_read', return_value={}), \
             mock.patch.object(Handler, '_adapter_owning', return_value=ad):
            h._adapter_route('POST')
        self.assertEqual(h.code, 204)
        self.assertEqual(len(ad.calls), 1)

    def test_prompt_carries_latest_shepherd_engine_before_dispatch(self):
        ad = Adapter(); ad.name = 'shepherd'
        ad.switch_engine = mock.Mock(return_value={'ok':True})
        ad.execution_name = mock.Mock(return_value='claude')
        ad.session_engine = mock.Mock(return_value='claude')
        ad.execution_options = mock.Mock(return_value={'model':'opus'})
        h = Handler('public', {'engine':'claude', 'client_request_id':'client-2',
                              'parts':[{'type':'text','text':'hello'}]})
        with mock.patch.object(ui_contract, '_adapter', return_value=ad), \
             mock.patch.object(ui_contract, '_engine_pref', return_value='pi'), \
             mock.patch.object(ui_contract, '_wb_state_read', return_value={}), \
             mock.patch.object(Handler, '_adapter_owning', return_value=ad):
            h._adapter_route('POST')
        self.assertEqual(h.code,204)
        ad.switch_engine.assert_not_called()  # 请求去重必须先于切换副作用。
        self.assertEqual(ad.calls[0][1]['requested_engine'], 'claude')
        ad.execution_options.assert_called()
        self.assertEqual(ad.calls[0][0][2], {'model':'opus'})

    def test_frontend_prompt_includes_current_shepherd_selection(self):
        # 按测试文件定位仓根，不按进程 cwd；否则从 desktron/ 子目录跑就找不到文件。
        source = (ROOT / "frontend/app-chat-composer.js").read_text(encoding="utf-8")
        self.assertIn('client_request_id: attempt.id', source)
        self.assertIn('...(v.shepherdMode ? { engine: v.engine } : {})', source)

    def test_route_passes_client_correlation_separately_from_prompt(self):
        ad = Adapter()
        h = Handler('public', {'client_request_id': 'client-1',
                              'parts': [{'type': 'text', 'text': 'hello'}]})
        with mock.patch.object(ui_contract, '_adapter', return_value=ad), \
             mock.patch.object(ui_contract, '_engine_pref', return_value='codex'), \
             mock.patch.object(ui_contract, '_wb_state_read', return_value={}), \
             mock.patch.object(Handler, '_adapter_owning', return_value=ad):
            h._adapter_route('POST')
        self.assertEqual(h.code, 204)
        self.assertEqual(ad.calls[0][0][1], 'hello')
        self.assertEqual(ad.calls[0][1]['client_request_id'], 'client-1')

    def run_ui(self, scenario):
        code = '\n'.join(fn('app-chat-composer.js', name) for name in
                         ('hsQIssue','hsQRetry','hsDispatch', 'hsPromptRejected', 'hsRestoreUnsent', 'hsInterrupt'))
        code += r'''
const assert = require('node:assert/strict');
let removed=0, requests=[], ack;
const old={uid:'old',el:{}};
const v={sid:'public',shepherdMode:true,turns:[old],turnGeneration:42,
  inp:{textContent:'new draft'},atts:[],queue:[{text:'queued'}],turnReady:true};
for (const name of ['hsStatusClear','hsSetStick','hsTouch','hsStatus','hsScroll','hsEnvironment',
                    'hsDraftSave','hsIndex','hsCmdPaint','hsAttPaint','hsQPaint']) globalThis[name]=()=>{};
function hsInpText(inp){return inp.textContent;}
function hsBusy(v,b){v.busy=b;}
function hsNewTurn(v){const t={el:{remove(){removed++;}}};v.turns.push(t);return t;}
function hsSentOrNot(){throw new Error('must not guess rejected delivery');}
function api(path,opts){requests.push(JSON.parse(opts.body));
  return new Promise((resolve,reject)=>{ack={resolve,reject};});}
function rejected(id){return {requestID:id,error:{code:'prompt_not_started',message:'catch-up failed'}};}
function checkRestored(){
  assert.equal(v.inp.textContent,'hello\n\nnew draft');
  assert.equal(v.atts.length,1);assert.equal(removed,1);
  assert.deepEqual(v.turns,[old]);assert.equal(v.sending,null);
  assert.equal(v.busy,false);assert.equal(v.turnReady,true);
  assert.equal(v.turnGeneration,42);assert.equal(v.noDrain,true);
  assert.equal(v.queuePaused,true);
  assert.deepEqual(v.queue,[{text:'queued'}]);
}
(async()=>{
''' + scenario + r'''
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code], capture_output=True, text=True, timeout=10, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_transient_delivery_failure_queues_only_with_cancellation_proof(self):
        self.run_ui(r'''
hsSentOrNot=async (v,text,proof)=>{proof.retryable=true;return 'not-sent'};
const sent=hsDispatch(v,'hello',[{data_b64:'image'}]);
ack.reject(new Error('offline'));
assert.equal(await sent,true);assert.equal(removed,1);assert.equal(v.turns.length,1);
assert.equal(v.queue[0].text,'hello');assert.equal(v.queue[1].text,'queued');
assert.equal(v.queue[0].atts[0].data_b64,'image');assert.equal(v.inp.textContent,'new draft');
assert.equal(v.queuePaused,false);assert.equal(v.turnReady,false);assert.equal(v.queueRetries,1);
''')

    def test_uncertain_delivery_never_returns_to_queue(self):
        self.run_ui(r'''
hsSentOrNot=async()=> 'unknown';
const sent=hsDispatch(v,'hello',[]);ack.reject(new Error('offline'));await sent;
assert.equal(removed,0);assert.equal(v.queue.length,1);
assert.equal(v.pendingSend.text,'hello');assert.equal(v.sending.requestID,v.pendingSend.requestID);
assert.equal(v.inp.textContent,'new draft');
''')

    def test_late_cancel_during_receipt_lookup_cannot_retry(self):
        self.run_ui(r'''
let release;hsSentOrNot=()=>new Promise(r=>release=r);
const sent=hsDispatch(v,'hello',[]);ack.reject(new Error('offline'));
await new Promise(r=>setImmediate(r));v.promptDispatch.recalled=true;release('not-sent');
assert.equal(await sent,true);assert.equal(v.queue.length,1);assert.equal(v.queueRetries,undefined);
''')

    def test_escape_before_late_ack_does_not_recreate_pending_send(self):
        self.run_ui(r'''
const sending=hsDispatch(v,'hello',[]);const post=ack;
v.queue=[];const turn=v.turns[v.turns.length-1];turn.parts=new Map();turn.user='hello';
globalThis.hsFill=(v,text)=>{v.inp.textContent=text};
globalThis.hsStateGeneration=()=>43;
const stopped=hsInterrupt(v);const stop=ack;
stop.resolve({state:'ready',generation:43,request:{status:'cancelled'}});await stopped;
assert.equal(v.inp.textContent,'hello');assert.equal(v.sending,null);
post.resolve({status:'cancelled'});await sending;
assert.equal(v.pendingSend == null,true);assert.equal(v.inp.textContent,'hello');
hsPromptRejected(v,rejected(requests[0].client_request_id));
assert.equal(v.inp.textContent,'hello');assert.equal(removed,1);
''')

    def test_rejection_before_http_ack_is_restored_once(self):
        self.run_ui(r'''
const sent=hsDispatch(v,'hello',[{name:'file'}]);
const id=requests[0].client_request_id;
assert.ok(id);hsPromptRejected(v,rejected(id));checkRestored();
hsPromptRejected(v,rejected(id));checkRestored();
ack.resolve();assert.equal(await sent,true);checkRestored();
''')

    def test_invalid_image_http_error_restores_without_delivery_guess(self):
        self.run_ui(r'''
const attachments=[{type:'image',data_b64:'bad',media_type:'image/png'}];
const sent=hsDispatch(v,'hello',attachments);
ack.reject(Object.assign(new Error('bad image'),{code:'invalid_image',status:400}));
assert.equal(await sent,false);
// hsSend/hsDrain owns restoring content for a rejected HTTP request.
hsRestoreUnsent(v,'hello',attachments);checkRestored();
assert.equal(requests.length,1);
''')

    def test_input_quota_http_errors_restore_without_receipt_guess_or_auto_retry(self):
        for code, status in (('turn_input_too_large',413),('prompt_body_too_large',413),
                             ('image_transport_too_large',413),('invalid_attachment',400)):
            with self.subTest(code=code):
                self.run_ui(r'''
const attachments=[{type:'image',data_b64:'large',media_type:'image/png'}];
const sent=hsDispatch(v,'hello',attachments);
ack.reject(Object.assign(new Error('input rejected'),{code:CODE,status:STATUS}));
assert.equal(await sent,false);
hsRestoreUnsent(v,'hello',attachments);checkRestored();
assert.equal(v.noDrain,true);assert.equal(requests.length,1);
'''.replace('CODE', repr(code)).replace('STATUS', str(status)))

    def test_rejection_after_http_ack_restores_message(self):
        self.run_ui(r'''
const sent=hsDispatch(v,'hello',[{name:'file'}]);
const id=requests[0].client_request_id;ack.resolve();await sent;
hsPromptRejected(v,rejected(id));checkRestored();
''')

    def test_lost_http_ack_after_rejection_does_not_restore_twice(self):
        self.run_ui(r'''
const sent=hsDispatch(v,'hello',[{name:'file'}]);
hsPromptRejected(v,rejected(requests[0].client_request_id));
ack.reject(new Error('network'));assert.equal(await sent,true);checkRestored();
''')

    def test_foreign_or_stale_rejection_does_not_remove_new_turn(self):
        self.run_ui(r'''
const first=hsDispatch(v,'hello',[]);const id=requests[0].client_request_id;
assert.equal(hsPromptRejected(v,rejected('another-client')),true);
assert.equal(removed,0);ack.resolve();await first;
const second=hsDispatch(v,'hello',[]);
hsPromptRejected(v,rejected(id));assert.equal(removed,0);
assert.equal(v.turns.length,3);assert.equal(v.busy,true);ack.resolve();await second;
''')

    def test_persisted_turn_and_ordinary_errors_are_not_recalled(self):
        self.run_ui(r'''
const sent=hsDispatch(v,'hello',[]);const id=requests[0].client_request_id;
v.promptDispatch.turn.uid='native-user';
hsPromptRejected(v,rejected(id));assert.equal(removed,0);
assert.equal(hsPromptRejected(v,{error:{message:'execution failed'}}),false);
v.shepherdMode=false;assert.equal(hsPromptRejected(v,rejected('ordinary-other')),true);
assert.equal(removed,0);
ack.resolve();await sent;
''')
