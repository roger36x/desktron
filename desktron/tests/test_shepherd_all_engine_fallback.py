"""Shared fallback selection remains verified, bounded, inert and codec-specific."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest
from shepherd.service import Service
from shepherd.harness import ExecutionHarness
from shepherd.store import Conflict
from shepherd.context_fallback import native_items, text_receipt
from test_shepherd_unified_storage import ENGINES, JournalRunner


@pytest.mark.parametrize('source', ENGINES)
@pytest.mark.parametrize('target', ENGINES)
@pytest.mark.parametrize('in_system', [False, True])
def test_every_source_target_pair_uses_verified_bounded_text(tmp_path, source, target, in_system):
    service=Service(tmp_path,storage='unified');service.create('pair',source,tmp_path)
    active=service.active('pair');c=service.connector(active['binding']);lease=active['lease']
    public=[{'type':'function_call','call_id':'old','name':'read','arguments':'{"key":"RAW_SECRET"}'},
            {'type':'function_call_output','call_id':'old','output':'FORBIDDEN_OUTPUT '*12000},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'Keep purple widgets.'}]}]
    ExecutionHarness(service.coordinator,c,JournalRunner(c,public)).run('pair',
        lease_id=lease['lease_id'],generation=lease['generation'],prompt='prior',
        input_public=[{'type':'message','role':'user','content':[{'type':'input_text','text':'Prior decision?'}]}])
    service.switch('pair',target,tmp_path);c=service.connector(service.active('pair')['binding'])
    key,checkpoint,tail=c.context({'model':'fixture','_require_context_budget':True,
                                 '_context_window':12000,'_history_hint':'Search verified history.',
                                 '_history_hint_in_system':in_system})
    assert tail==[] and checkpoint['through_seq']==1
    native=checkpoint['payload']['native'];items=native_items(checkpoint)
    assert native['estimated_tokens']<=12000-8192
    text=json.dumps(items)
    assert 'Keep purple widgets.' in text and 'Prior decision?' in text
    assert 'FORBIDDEN_OUTPUT' not in text and 'RAW_SECRET' not in text
    if in_system:
        assert 'Search verified history.' not in text
        assert 'retrieval instructions in the system prompt' in text
    else:
        assert 'Search verified history.' in text
    if target!='codex':
        assert 'portable_checkpoint_id' not in native
        assert 'internal_chat_message_metadata_passthrough' not in text
    message=items[0].get('message',items[0])
    body=message['content']
    value=body if isinstance(body,str) else ''.join(b['text'] for b in body)
    assert text_receipt(checkpoint)['text_sha256']==hashlib.sha256(value.encode()).hexdigest()
    with pytest.raises(Conflict):
        c.context({'model':'fixture','_require_context_budget':True,'_context_window':8200})
    assert len(service.coordinator.state('pair')['turns'])==1


def test_native_receipt_rejects_missing_changed_duplicate_and_wrong_role():
    module=(Path(__file__).parents[1]/'lib/shepherd/fallback_receipt.mjs').as_uri()
    script="""
import {verifyFallback} from MODULE;
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
const receipt={checkpoint_id:'degraded_fixture',text_sha256:createHash('sha256').update('verified').digest('hex')};
const m={role:'user',content:[{type:'text',text:'verified'}]};
assert.deepEqual(verifyFallback([m],receipt),receipt);
assert.deepEqual(verifyFallback([{message:m}],receipt),receipt);
for(const messages of [[],[m,m],[{...m,role:'assistant'}],[{...m,content:'changed'}]]) {
  assert.throws(()=>verifyFallback(messages,receipt));
}
""".replace('MODULE',json.dumps(module))
    subprocess.run(['node','--input-type=module','-e',script],check=True,capture_output=True,text=True)


@pytest.mark.parametrize('engine', ENGINES)
@pytest.mark.parametrize('budget', [0, 12000])
def test_runner_uses_explicit_budget_without_guessing_unknown_native_window(tmp_path,engine,budget):
    from unittest import mock
    from shepherd.unified_runners import UnifiedRunner
    service=Service(tmp_path,storage='unified');service.create('budget',engine,tmp_path)
    c=service.connector(service.active('budget')['binding'])
    runner=UnifiedRunner(c,options={'model':'fixture','autocompact':budget})
    with mock.patch.object(c,'context',side_effect=Conflict('capture')) as context:
        with pytest.raises(Conflict,match='capture'):
            runner.run(prompt='hello',round_id='budget:1',expected_offset=1,on_dispatched=lambda:None)
    options=context.call_args.args[0]
    assert options['_context_window']==budget
    assert options['_require_context_budget']==(budget>0)


@pytest.mark.parametrize('engine', ENGINES)
def test_fallback_drops_old_resident_before_native_preparation(tmp_path,engine):
    from unittest import mock
    from shepherd.unified_runners import UnifiedRunner
    service=Service(tmp_path,storage='unified');service.create('resident',engine,tmp_path)
    c=service.connector(service.active('resident')['binding'])
    runner=UnifiedRunner(c,options={'model':'fixture','_context_window':12000})
    checkpoint={'payload':{'native':{'degraded_context':{'through_seq':1},
        'degraded_checkpoint_id':'degraded_fixture','text_sha256':'a'*64,'items':[]}}}
    events=[]
    method='_claude' if engine=='claude' else '_pi' if engine=='pi' else '_codex'
    def prepare(*args):
        assert events==['resident','appserver']
        raise Conflict('stop before dispatch')
    with mock.patch.object(c,'context',return_value=('key',checkpoint,[])), \
         mock.patch('shepherd.resident_process.drop',side_effect=lambda *a:events.append('resident')) as native_drop, \
         mock.patch('codex_appserver.drop_session',side_effect=lambda *a:events.append('appserver')) as app_drop, \
         mock.patch.object(runner,method,side_effect=prepare):
        with pytest.raises(Conflict,match='stop before dispatch'):
            runner.run(prompt='hello',round_id='resident:1',expected_offset=1,on_dispatched=lambda:pytest.fail('dispatched'))
    native_drop.assert_called_once_with(c.binding['binding_id'])
    app_drop.assert_called_once_with('shepherd:'+c.binding['binding_id'])
