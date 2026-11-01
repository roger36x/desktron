"""Raw Code Mode is data: preserve its kind, never evaluate it on catch-up."""
import copy
import json
from pathlib import Path
from unittest import mock

from test_shepherd_pi import PiFixture
from shepherd import ReadProjector, SwitchRunner, Conflict
from shepherd.coordinator import digest
from shepherd.pi_codec import encode_round, CodecError
from shepherd.codex_codec import encode_round as codex_encode
from shepherd.source_codec import pi_public, codex_public
from shepherd.store import InvalidState


def public(raw='const x = await tools.exec_command({cmd: "never execute"});\ntext(x);'):
    return [
        {'type':'message','role':'user','content':[{'type':'input_text','text':'question'}]},
        {'type':'custom_tool_call','call_id':'custom_1','name':'exec','input':raw},
        {'type':'custom_tool_call_output','call_id':'custom_1','output':'historical result'},
        {'type':'message','role':'assistant','content':[{'type':'output_text','text':'done'}]},
    ]


def entries(messages):
    return [m if m.get('type') == 'custom' else {'type':'message','message':m}
            for m in messages]


class CustomTools(PiFixture):
    def test_raw_input_round_trips_without_json_interpretation(self):
        for raw in ('', '{}', '"a"', 'null', 'await tools.exec_command({cmd: "echo hi"});\ntext("好");'):
            with self.subTest(raw=raw):
                original=public(raw)
                messages=encode_round(self.round(original))
                call=next(m for m in messages if m.get('role') == 'assistant')['content'][0]
                self.assertEqual(call['arguments'], {'input':raw})
                self.assertEqual(set(call), {'type','id','name','arguments'})
                restored=pi_public(entries(messages))
                self.assertEqual(restored, original)
                self.assertEqual(codex_public(codex_encode(self.round(restored))), original)

    def test_function_call_keeps_strict_json_and_does_not_infer_custom(self):
        original=public()
        original[1]={'type':'function_call','call_id':'custom_1','name':'exec','arguments':'{"input":"raw"}'}
        original[2]['type']='function_call_output'
        self.assertEqual(pi_public(entries(encode_round(self.round(original)))), original)
        for raw in ('not JSON', '[]', 'null'):
            original[1]['arguments']=raw
            with self.assertRaises(CodecError): encode_round(self.round(original))
        wrong=public(); wrong[2]['type']='function_call_output'
        with self.assertRaises(CodecError): encode_round(self.round(wrong))

    def test_marker_tampering_and_orphans_fail_closed(self):
        native=entries(encode_round(self.round(public())))
        marker=next(i for i,row in enumerate(native) if row.get('type')=='custom')
        variants=[]
        changed=copy.deepcopy(native);changed[marker]['data']['name']='other';variants.append(changed)
        changed=copy.deepcopy(native);changed.insert(marker,copy.deepcopy(changed[marker]));variants.append(changed)
        changed=copy.deepcopy(native);changed[marker+1]['message']['content'][0]['arguments']['extra']=True;variants.append(changed)
        variants.append(native[:marker+2])
        variants.append(native[:marker]+native[marker+1:]+[native[marker]])
        for changed in variants:
            with self.assertRaises(InvalidState): pi_public(changed)

    def test_native_sdk_excludes_markers_and_appends_offsets_two_to_nine(self):
        connector=self.connector()
        rows=[self.round(public(f'await neverExecute({n});'),n) for n in range(1,10)]
        revision=connector.append(rows[:1], '')['native_revision']
        before=Path(connector.path).read_bytes()
        result=connector.append(rows[1:], revision)
        self.assertEqual(connector.append(rows[1:], revision), result)
        self.assertTrue(Path(connector.path).read_bytes().startswith(before))
        self.assertEqual(connector.verify()['roles'], ['user','assistant','toolResult','assistant']*9)
        for row in rows:
            ref={'kind':'native_round','engine':'pi','session_id':self.sid,'entry_id':row['round_id'],
                 'source_key':f'pi:{self.sid}:{row["round_id"]}'}
            self.assertEqual(connector.resolve_source(ref)['public'],row['public'])

    def test_resume_existing_switch_from_verified_offset_one_only(self):
        old=self.coordinator.bind(self.cid,'codex','native_codex',native_cwd=str(self.cwd),
            connector_version='codex-v1',binding_id='binding_codex',ownership_nonce='nonce_codex')
        lease=self.coordinator.acquire(self.cid,old,ownership_nonce='nonce_codex')
        source_rows={}
        for n in range(1,10):
            native={'fixture':n};items=public(f'await neverExecute({n});')
            ref={'kind':'native_round','engine':'codex','session_id':'native_codex',
                 'entry_id':str(n),'source_key':f'codex:native_codex:{n}'}
            source_rows[str(n)]={'source':native,'public':items}
            self.coordinator.commit(self.cid,lease['lease_id'],lease['generation'],
                source=ref,source_hash=digest(native),public_hash=digest(items),estimated_tokens=20)
        class Resolver:
            def resolve(self,ref): return source_rows[ref['entry_id']]
        projector=ReadProjector(self.store,Resolver());connector=self.connector()
        pending=self.coordinator.prepare_switch(self.cid,self.binding_id,ownership_nonce='nonce_pi',
            lease_id=lease['lease_id'],generation=lease['generation'])
        first=projector.project(self.cid,1,1)
        rev=connector.append(first,'')['native_revision']
        self.coordinator.advance(self.cid,self.binding_id,1,first[0]['entry_hash'],rev,ownership_nonce='nonce_pi')
        with mock.patch.object(connector,'append',wraps=connector.append) as append:
            restored=SwitchRunner(self.coordinator,projector,connector).resume_switch(
                self.cid,pending['switch_id'],ownership_nonce='nonce_pi')
        self.assertEqual([r['offset'] for r in append.call_args.args[0]],list(range(2,10)))
        state=self.coordinator.state(self.cid)
        self.assertEqual(restored['binding_id'],self.binding_id)
        self.assertGreater(restored['generation'],lease['generation'])
        self.assertEqual(state['bindings'][self.binding_id]['applied_offset'],9)
        self.assertIsNone(state['pending_switch'])
        self.assertEqual(connector.verify()['messages'],36)
