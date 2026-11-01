"""Retired backend data is transferred once; normal sends have no backend queue."""
import ast
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'desktron/lib'))
from engine_adapters import queue_recovery as recovery
import durable_json


class QueueRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.receipts={}
        self.ad=SimpleNamespace(dir=self.tmp.name,name='shepherd',index={'s':{}},_lock=threading.Lock(),
            requests=SimpleNamespace(get=lambda sid,rid:self.receipts.get(rid,{'status':'not_found'})))
        self.path=Path(self.tmp.name)/'message_queue/s/queue.json'
        self.data={'items':{},'revision':1}

    def add(self,i,state='queued',**extra):
        self.data['items'][i]={'item_id':i,'state':state,'text':i,'attachments':[],
            'request_id':'r_'+i,'client_request_id':'r_'+i,'revision':1,'position':len(self.data['items']),**extra}
        snapshot=durable_json.read(str(self.path));snapshot.update(self.data)
        durable_json.write(str(self.path),snapshot)

    def test_claim_ack_lost_reply_and_other_browser(self):
        self.add('a');self.add('b','editing')
        first=recovery.recover(self.ad,'s','browser1')
        self.assertEqual([r['id'] for r in first['items']],['a','b'])
        self.assertEqual(recovery.recover(self.ad,'s','browser1'),first)
        with self.assertRaises(ValueError):recovery.recover(self.ad,'s','browser2')
        self.assertTrue(recovery.recover(self.ad,'s','browser1',first['token'])['ok'])
        self.assertTrue(recovery.recover(self.ad,'s','browser1',first['token'])['ok'])
        self.assertEqual(recovery.recover(self.ad,'s','browser2')['items'],[])
        self.assertEqual(recovery.request_status(self.ad,'s','r_a')['status'],'cancelled')
        self.assertEqual(durable_json.read_value(str(self.path))['items']['a']['text'],'a')

    def test_unknown_execution_is_not_exported(self):
        for i in ['unknown','finished','never_started','no_receipt']:self.add(i,'dispatching')
        self.receipts={'r_unknown':{'status':'uncertain'},'r_finished':{'status':'finished'},
            'r_never_started':{'status':'finished','reason':'failed_before_dispatch'}}
        result=recovery.recover(self.ad,'s','browser')
        self.assertEqual({r['id'] for r in result['items']},{'never_started','no_receipt'})
        self.assertEqual(result['blocked'],1)
        self.assertTrue(recovery.available(self.ad,'s'))
        self.receipts={'r_finished':{'status':'finished'}}
        for key in ('unknown','never_started','no_receipt'):
            self.data['items'].pop(key, None)
        snapshot=durable_json.read(str(self.path));snapshot.update(self.data)
        durable_json.write(str(self.path),snapshot)
        self.assertFalse(recovery.available(self.ad,'s'))

    def test_attachment_reference_survives_ack_and_rejects_foreign_blob(self):
        self.add('a',attachments=[{'attachment_id':'att_x','name':'a.png','type':'image','media_type':'image/png'}])
        blob=self.path.parent/'blobs/att_x.bin';blob.parent.mkdir();blob.write_bytes(b'png')
        result=recovery.recover(self.ad,'s','browser')
        self.assertEqual(result['items'][0]['atts'][0]['url'],'/session/s/queue-recovery/a/att_x')
        recovery.recover(self.ad,'s','browser',result['token'])
        self.assertEqual(Path(recovery.attachment(self.ad,'s','a','att_x')).read_bytes(),b'png')
        self.assertEqual(recovery.attachment(self.ad,'s','a','att_other'),'')

    def test_empty_session_does_not_create_a_queue(self):
        self.assertFalse(recovery.available(self.ad,'s'))
        self.assertEqual(recovery.recover(self.ad,'s','browser')['items'],[])
        self.assertFalse(self.path.exists())

    def test_real_http_handler_reaches_transfer_and_ack_routes(self):
        # Extract classes/functions, avoiding ui_server startup and real adapters.
        source=(ROOT/'desktron/lib/ui_server.py').read_text(encoding='utf-8')
        cls=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=='Handler')
        contract=(ROOT/'desktron/lib/ui_contract.py').read_text(encoding='utf-8')
        route=next(n for n in ast.walk(ast.parse(contract)) if isinstance(n,ast.FunctionDef) and n.name=='_adapter_route')
        cls.bases=[ast.parse('http.server.SimpleHTTPRequestHandler',mode='eval').body]
        cls.body.append(route)
        import socket
        env={'http':http,'time':time,'re':re,'os':os,'json':json,'socket':socket,
             'FRONTEND':self.tmp.name,
             'app_control_peer_pid':lambda r:None,'PROCESS_STARTED':0,'STARTUP_TRACE_S':0,
             '_adapter':lambda e:self.ad,'_engine_pref':lambda:'shepherd','GUI_ENGINES':('pi',)}
        # Handler 收尾请求正文时依赖模块级的 _RequestBody 与 drain 常量。
        body_nodes=[n for n in ast.parse(source).body
                    if (isinstance(n,ast.ClassDef) and n.name=='_RequestBody')
                    or (isinstance(n,ast.Assign) and any(
                        getattr(t,'id','') in ('BODY_DRAIN_MAX','BODY_DRAIN_TIMEOUT','BODY_READ_TIMEOUT')
                        for t in n.targets))]
        exec(compile(ast.fix_missing_locations(ast.Module(body=body_nodes+[cls],type_ignores=[])),'ui_server.py','exec'),env)
        handler=env['Handler']
        handler._gate=lambda h:True;handler._peer=lambda h:False
        handler._is_workbench=lambda h:False;handler._is_contract=lambda h:True
        handler._adapter_owning=lambda h,sid:self.ad
        def send(h,obj,code=200):
            raw=json.dumps(obj).encode();h.send_response(code);h.send_header('Content-Length',str(len(raw)));h.end_headers();h.wfile.write(raw)
        handler._send_json=send
        self.add('a')
        # Only lifecycle admission is replaced; real do_GET/do_POST and route run over TCP.
        with patch.dict(sys.modules,{'remote_auth':SimpleNamespace(PROBE_PATH='/probe'),
             'lifecycle':SimpleNamespace(begin=lambda **kw:True,finish=lambda **kw:None)}):
            with http.server.ThreadingHTTPServer(('127.0.0.1',0),handler) as server:
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    def request(method,body=None):
                        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                        conn.request(method,'/session/s/queue-recovery',json.dumps(body) if body else None)
                        res=conn.getresponse();raw=res.read();conn.close()
                        self.assertEqual(res.status,200,raw)
                        return json.loads(raw)
                    self.assertTrue(request('GET')['available'])
                    claim=request('POST',{'owner':'browser'})
                    self.assertEqual(claim['items'][0]['text'],'a')
                    self.assertTrue(request('POST',{'owner':'browser','ack':claim['token']})['ok'])
                    self.assertFalse(request('GET')['available'])
                finally:server.shutdown();thread.join()
