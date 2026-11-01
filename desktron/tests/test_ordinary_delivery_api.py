from io import BytesIO
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT=os.path.abspath(os.path.join(os.path.dirname(__file__),'..','..'))
LIB=os.path.join(ROOT,'desktron','lib');sys.path.insert(0,LIB)
_argv=sys.argv;sys.argv=['ui_server.py',os.path.join(ROOT,'frontend'),'7999','7998',ROOT]
try:
 import ui_contract
finally:sys.argv=_argv


class Adapter:
    name='pi'
    delivery=mock.Mock()
    def __init__(self):
        self.conflict = False
    def session_options(self,*_):return {}
    def prompt_async(self,*args,**kwargs):
        if self.conflict:
            from engine_adapters.delivery import DeliveryConflict
            raise DeliveryConflict('上一条请求可能已经执行；请先核验后再继续', code='delivery_uncertain')
        return {'request_id':kwargs['client_request_id'],'status':'accepted'}
    def turn_state(self,sid):
        return {'state':'blocked','generation':7,'delivery':{'request_id':'old','status':'uncertain'}}
    def request_status(self,sid,rid):return {'request_id':rid,'status':'uncertain'}
    def resolve_delivery(self,sid,rid):return {'request_id':rid,'status':'finished','reason':'user_acknowledged_unknown'}
    def cancel_unsent_delivery(self,sid,rid):return {'request_id':rid,'status':'cancelled'}
    def abort(self,sid,recall=False,client_request_id=''):
        return {'state':'cancelling','request_id':client_request_id,'recall':recall}


class Handler(ui_contract.UiContractMixin):
    def __init__(self,path,body=None):
        raw=json.dumps(body or {}).encode();self.path=path
        self.headers={'Content-Length':str(len(raw))};self.rfile=BytesIO(raw)
        self.code=None;self.out=None
    def send_response(self,code):self.code=code
    def end_headers(self):pass
    def _send_json(self,obj,code=200):self.out,self.code=obj,code;return obj


class OrdinaryDeliveryAPI(unittest.TestCase):
    def call(self,path,method='GET',body=None,ad=None):
        h=Handler(path,body);ad=ad or Adapter()
        with mock.patch.object(Handler,'_adapter_owning',return_value=ad), \
             mock.patch.object(ui_contract,'_adapter',return_value=ad), \
             mock.patch.object(ui_contract,'_engine_pref',return_value='pi'), \
             mock.patch.object(ui_contract,'_wb_state_read',return_value={}):
            h._adapter_route(method)
        return h

    def test_ordinary_prompt_returns_durable_receipt(self):
        got=self.call('/session/adp_s/prompt_async','POST',{
            'client_request_id':'prompt_1','parts':[{'type':'text','text':'hello'}]})
        self.assertEqual(got.code,202);self.assertEqual(got.out['status'],'accepted')

    def test_ordinary_abort_receives_request_identity(self):
        got=self.call('/session/adp_s/abort','POST',{
            'recall':True,'client_request_id':'prompt_1'})
        self.assertEqual(got.code,200)
        self.assertEqual(got.out['request_id'],'prompt_1')
        self.assertTrue(got.out['recall'])

    def test_delivery_conflict_prompt_response_carries_recovery_state(self):
        ad=Adapter();ad.conflict=True
        got=self.call('/session/adp_s/prompt_async','POST',{
            'client_request_id':'prompt_2','parts':[{'type':'text','text':'again'}]},ad=ad)
        self.assertEqual(got.code,409)
        self.assertEqual(got.out['code'],'delivery_uncertain')
        self.assertEqual(got.out['state'],'blocked')
        self.assertEqual(got.out['generation'],7)
        self.assertEqual(got.out['delivery']['request_id'],'old')

    def test_ordinary_receipt_query_and_unknown_resolution_are_routed(self):
        got=self.call('/session/adp_s/request/prompt_1')
        self.assertEqual(got.out['status'],'uncertain')
        got=self.call('/session/adp_s/request/prompt_1/accept-unknown','POST')
        self.assertEqual(got.out['status'],'finished')


if __name__=='__main__':unittest.main()
