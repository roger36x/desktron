"""image_transport execution-only derivations preserve durable evidence."""
import base64
import copy
import hashlib
import io
import json
import random
import struct
import zlib
from types import SimpleNamespace
from unittest import mock

import pytest
from PIL import Image
from shepherd import image_transport as policy
from shepherd.image_content import image_block, native_content
from shepherd.claude_codec import encode_rounds
from shepherd.source_codec import claude_public
from shepherd.turn_inputs import TurnInputs
from shepherd.coordinator import digest
from shepherd.unified_runners import UnifiedRunner
from shepherd.image_transport import ClaudeImageTransport, ImageTransportError
from shepherd.store import InvalidState, PromptNotStarted


def png(size=(64, 48), padding=0):
    out = io.BytesIO(); Image.new('RGB', size, (120, 30, 80)).save(out, format='PNG')
    raw = out.getvalue()
    if padding:
        body = b'padding\0' + b'a' * padding
        kind = b'tEXt'
        chunk = struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
        raw = raw[:-12] + chunk + raw[-12:]
    return raw


def native(raw, mime='image/png'):
    return {'type':'image', 'source':{'type':'base64', 'media_type':mime,
                                     'data':base64.b64encode(raw).decode()}}


def frame(raw):
    return {'type':'user', 'message':{'role':'user', 'content':[
        {'type':'text', 'text':'compare'}, native(raw), native(raw)]}}


@pytest.fixture(scope='module')
def large():
    return png(padding=8_000_000)


def test_two_10mb_encoded_images_are_losslessly_derived_and_originals_unchanged(large, tmp_path):
    original = frame(large); before = copy.deepcopy(original)
    public = [{'type':'message', 'role':'user', 'content':[
        {'type':'input_text','text':'compare'}, image_block(original['message']['content'][1]['source']['data'], 'image/png')]}]
    inputs = TurnInputs(tmp_path); ref = inputs.write('conversation', 'conversation:1', public)
    saved = list(tmp_path.rglob('*.json'))[0].read_bytes()
    transport = ClaudeImageTransport()
    with mock.patch.object(transport, '_compress', wraps=transport._compress) as compress:
        sent = transport.frame(original)
        assert compress.call_count == 1
    assert sent != original and original == before
    for b in sent['message']['content'][1:]:
        assert len(b['source']['data']) <= policy.MAX_ENCODED_IMAGE_BYTES
        with Image.open(io.BytesIO(base64.b64decode(b['source']['data']))) as im:
            assert im.convert('RGB').getpixel((0,0)) == (120,30,80)
    receipt = transport.receipt()
    assert len(receipt['derivations']) == 1
    r = receipt['derivations'][0]
    assert r['source_sha256'] == hashlib.sha256(large).hexdigest()
    assert r['variant_sha256'] == hashlib.sha256(base64.b64decode(sent['message']['content'][1]['source']['data'])).hexdigest()
    assert 'data' not in json.dumps(receipt) and r['jpeg_quality'] is None
    assert inputs.read(ref) == public
    assert list(tmp_path.rglob('*.json'))[0].read_bytes() == saved
    transport.validate_request([], sent)


def test_small_valid_images_are_byte_exact():
    f = frame(png()); t = ClaudeImageTransport()
    assert t.frame(f) == f
    assert not t.derivations


def test_lossy_fallback_is_bounded_and_records_quality():
    pixels = random.Random(5).randbytes(1300 * 1300 * 3)
    out = io.BytesIO(); Image.frombytes('RGB',(1300,1300),pixels).save(out,format='PNG')
    t = ClaudeImageTransport(); sent = t.image(native(out.getvalue()))
    assert sent['source']['media_type'] == 'image/jpeg'
    assert len(sent['source']['data']) <= policy.MAX_ENCODED_IMAGE_BYTES
    assert t.receipt()['derivations'][0]['jpeg_quality'] >= 80


def test_resize_preserves_aspect_and_does_not_upscale():
    t = ClaudeImageTransport(); sent = t.image(native(png((4000,1000))))
    assert t.receipt()['derivations'][0]['variant_size'] == [2000,500]
    with Image.open(io.BytesIO(base64.b64decode(sent['source']['data']))) as im:
        assert im.size == (2000,500)


@pytest.mark.parametrize('fmt,mime', [('JPEG','image/jpeg'),('WEBP','image/webp'),('GIF','image/gif')])
def test_supported_formats_can_be_resized(fmt, mime):
    out=io.BytesIO(); Image.new('RGB',(2100,100),(10,20,30)).save(out,format=fmt)
    t=ClaudeImageTransport(); t.image(native(out.getvalue(),mime))
    assert t.receipt()['derivations'][0]['variant_size'] == [2000,95]


def test_tool_images_and_checkpoint_are_processed_without_touching_tool_arguments(large):
    t=ClaudeImageTransport()
    image=native(large)
    entries=[{'type':'user','message':{'role':'user','content':[
        {'type':'tool_result','tool_use_id':'t1','content':[image]},
        {'type':'text','text':'keep text'}]}},
        {'type':'assistant','message':{'role':'assistant','content':[
            {'type':'tool_use','input':{'untrusted':image},'id':'t2','name':'read'}]}}]
    original=copy.deepcopy(entries); old_hash=digest(entries)
    sent=t.entries(entries)
    assert entries==original and digest(entries)==old_hash
    assert sent[0]['message']['content'][0]['content'][0] != image
    assert sent[1] == entries[1]
    assert len(t.entry_sources)==1


def test_old_codec_hash_and_import_retry_are_not_changed_by_execution_transform(large):
    public=[{'type':'message','role':'user','content':[
        image_block(base64.b64encode(large).decode(),'image/png')]}]
    binding={'binding_id':'test','native_session_id':'test','native_cwd':'/tmp'}
    row={'round_id':'conversation:1','offset':1,'entry_hash':'a'*64,'public':public}
    entries=encode_rounds(binding,[row]); original=copy.deepcopy(entries)
    sent=ClaudeImageTransport().entries(entries)
    assert claude_public(entries)==public
    assert encode_rounds(binding,[row])==original
    assert digest(entries)==digest(original)
    assert claude_public(sent)!=public


@pytest.mark.parametrize('source', [{'type':'url','url':'https://invalid/private'},
                                   {'type':'file','file_id':'private'}])
def test_no_remote_source_or_file_lookup(source):
    with pytest.raises(InvalidState):
        ClaudeImageTransport().image({'type':'image','source':source})


def test_decode_bomb_is_rejected_before_pillow_allocation():
    # A valid PNG container with falsified huge dimensions, not a real allocation.
    raw=png(); ihdr=struct.pack('>IIBBBBB',10000,10000,8,2,0,0,0)
    raw=raw[:16]+ihdr+struct.pack('>I',zlib.crc32(b'IHDR'+ihdr))+raw[33:]
    with mock.patch.object(policy,'_pillow',side_effect=AssertionError('must not decode')):
        with pytest.raises(ImageTransportError,match='3200'):
            ClaudeImageTransport().image(native(raw))


def test_dependency_missing_fails_instead_of_sending_large_original(large):
    with mock.patch.object(policy,'_pillow',side_effect=policy.ImageInputError('Pillow missing')):
        with pytest.raises(policy.ImageInputError,match='Pillow'):
            ClaudeImageTransport().frame(frame(large))


def test_corrupt_pixel_stream_is_rejected_when_transform_required():
    # CRC-valid IDAT is not a valid compressed pixel stream.
    raw=png((2100,2)); start=raw.index(b'IDAT')-4; end=start+12+int.from_bytes(raw[start:start+4],'big')
    bad=b'not deflate'; chunk=struct.pack('>I',len(bad))+b'IDAT'+bad+struct.pack('>I',zlib.crc32(b'IDAT'+bad))
    with pytest.raises(policy.ImageInputError,match='解码'):
        ClaudeImageTransport().image(native(raw[:start]+chunk+raw[end:]))


def test_legacy_private_history_is_fail_closed_not_rewritten(large):
    f=frame(large); original=copy.deepcopy(f)
    with pytest.raises(ImageTransportError,match='统一历史'):
        ClaudeImageTransport().entries([f],transform=False)
    assert f==original


def test_request_budget_counts_utf8_and_history_not_just_current_images():
    t=ClaudeImageTransport(); entries=[{'message':{'role':'user','content':[{'type':'text','text':'汉字'}]}}]
    n=t.validate_request(entries,None,system='system',grounding='grounding')
    with mock.patch.object(policy,'MAX_REQUEST_BYTES',n):
        assert t.validate_request(entries,None,system='system',grounding='grounding')==n
    with mock.patch.object(policy,'MAX_REQUEST_BYTES',n-1):
        with pytest.raises(ImageTransportError,match='整请求'):
            t.validate_request(entries,None,system='system',grounding='grounding')


def test_image_count_includes_nested_tools_and_current_turn():
    t=ClaudeImageTransport(); image=native(png())
    entries=[{'message':{'role':'user','content':[{'type':'tool_result','content':[image]*99}]}}]
    with pytest.raises(ImageTransportError,match='100'):
        t.validate_request(entries,frame(png()))


def test_cache_is_bounded_and_entry_sources_exclude_unrelated_current_images(large):
    t=ClaudeImageTransport(); t.frame(frame(large))
    t.entries([frame(png())]); assert not t.entry_sources
    for i in range(10):
        t.image(native(png((2101+i,10))))
    assert len(t.cache)==8


def test_unified_execution_view_covers_current_and_history_and_preserves_proofs(large, tmp_path):
    r=object.__new__(UnifiedRunner); r.connector=SimpleNamespace(engine='claude', store_root=tmp_path,
        binding={'conversation_id':'conversation'})
    r.user_frame=frame(large); r.system_text='system'; r.options={}
    entries=[frame(large)]; original=copy.deepcopy(entries)
    sent=r._claude_execution_entries(entries,'prompt')
    assert entries==original and sent!=entries
    assert r.user_frame!=frame(large)
    assert r.image_transport.receipt()['derivations']


def test_unified_whole_request_failure_is_before_process_creation(large, tmp_path):
    r=object.__new__(UnifiedRunner); r.connector=SimpleNamespace(engine='claude', store_root=tmp_path,
        binding={'conversation_id':'conversation'})
    r.user_frame=frame(png()); r.system_text='system'; r.options={}
    with mock.patch.object(policy,'MAX_REQUEST_BYTES',500):
        with pytest.raises(ImageTransportError):
            r._claude_execution_entries([frame(large)],'prompt')
    assert not hasattr(r,'process')


def test_two_turns_bridge_payload_commit_and_cold_replay_keep_original_hashes(large, tmp_path):
    from shepherd import Service, ExecutionHarness, ReadProjector
    from shepherd.source_resolver import SourceResolver
    from test_shepherd_unified_storage import JournalRunner
    service=Service(tmp_path,storage='unified'); service.create('conversation','claude',tmp_path)
    active=service.active('conversation'); c=service.connector(active['binding']); lease=active['lease']
    original_public=TurnInputs.public_input('old',[
        image_block(base64.b64encode(large).decode(),'image/png')])
    old=ExecutionHarness(service.coordinator,c,JournalRunner(c,original_public)).run(
        'conversation',lease_id=lease['lease_id'],generation=lease['generation'],prompt='old',
        input_public=original_public)
    old_source=c.resolve_source(old['source']); original_hash=digest(old_source)
    old_inputs={p:p.read_bytes() for p in tmp_path.glob('inputs/**/*.json')}
    payloads=[]

    def fake_native(runner):
        def spawn(command,cancel,env):
            payload=json.loads(__import__('pathlib').Path(command[-1]).read_text(encoding='utf-8')); payloads.append(payload)
            entries=payload['entries']; parent=entries[-1]['uuid'] if entries else ''
            start=len(entries); revision='a'*64
            prepared={'type':'prepared','bridgeProtocolVersion':3,'fallbackReceipt':payload.get('fallbackReceipt'),
                      'parentUuid':parent,'nativeRevision':revision,'startSeq':start,'storeSize':start,
                      'claudeVersion':'2.1.283','groundingUuid':''}
            user={**payload['userFrame'], 'uuid':runner.round_id+'-user','parentUuid':parent,
                  'sessionId':c.native_session_id,'cwd':c.native_cwd,'version':'2.1.283'}
            assistant={'type':'assistant','uuid':runner.round_id+'-answer','parentUuid':user['uuid'],
                'sessionId':c.native_session_id,'cwd':c.native_cwd,'version':'2.1.283',
                'message':{'role':'assistant','content':[{'type':'text','text':'done'}],
                           'id':'msg-test','model':'fixture','stop_reason':'end_turn','type':'message'}}
            raw=[json.dumps(e,separators=(',',':')) for e in (user,assistant)]
            result={'type':'result','startSeq':start,'endSeq':start+2,'groundingUuid':'',
                    'terminalState':'completed','nativeRevision':'b'*64,
                    'transitionRevision':runner._claude_transition_revision(revision,raw)}
            process=mock.Mock(proc=None)
            def sent(message):
                if message.get('type')=='start' and seq==2:
                    with c.archive.db() as db:
                        rows=db.execute('SELECT payload FROM events WHERE turn_id=?',
                                        (runner.round_id,)).fetchall()
                    assert any('image_transport' in json.loads(r['payload']) for r in rows)
            process.send.side_effect=sent
            process.events.return_value=iter([prepared,{'type':'archive','id':'batch',
                'entries':[user,assistant],'entryPayloads':raw},result])
            return process
        return spawn

    for seq in (2,3):
        current=frame(large) if seq==2 else frame(png())
        runner=UnifiedRunner(c,user_frame=current,system_text='system',options={},
            env={'DESK_SHEPHERD_CLAUDE_BINARY':'/fixture/claude','DESK_SHEPHERD_CLAUDE_SDK_ROOT':'/fixture/sdk'})
        history=ReadProjector(service.store,SourceResolver([c])).project('conversation')
        with mock.patch.object(c,'context',return_value=('test-execution',None,history)), \
             mock.patch('shepherd.resident_process.enabled',return_value=False), \
             mock.patch('shepherd.claude_connector.ClaudeConnector.availability',return_value={'available':True}), \
             mock.patch.object(runner,'_native_process',side_effect=fake_native(runner)):
            row=ExecutionHarness(service.coordinator,c,runner).run('conversation',
                lease_id=lease['lease_id'],generation=lease['generation'],prompt='compare',
                input_public=TurnInputs.public_input('compare',[
                    image_block(b['source']['data'],b['source']['media_type'])
                    for b in current['message']['content'] if b['type']=='image']))
        assert row['terminal_state']=='completed'
    for payload in payloads:
        t=ClaudeImageTransport(); t.entries(payload['entries'],transform=False)
        t.validate_request(payload['entries'],payload['userFrame'],system=payload['systemPrompt'])
    assert digest(c.resolve_source(old['source']))==original_hash
    assert all(p.read_bytes()==v for p,v in old_inputs.items())
    state=service.coordinator.state('conversation')
    projector=ReadProjector(service.store,SourceResolver([c]))
    public=projector.project('conversation')
    assert len(public)==3 and public[0]['public']==original_public
    assert projector.project('conversation')==public
    assert state['committed_offset']==3 and not state.get('inflight')
    # Commit-only recovery/projection does not need the variant cache or encoder.
    with mock.patch.object(policy,'_pillow',side_effect=AssertionError('no recovery re-encode')):
        assert projector.project('conversation')==public


def test_staged_derived_completion_recovery_never_reencodes_or_runs_again(large, tmp_path):
    from shepherd import Service, ExecutionHarness
    from test_shepherd_unified_storage import JournalRunner
    s=Service(tmp_path,storage='unified'); s.create('conversation','claude',tmp_path)
    active=s.active('conversation'); c=s.connector(active['binding']); lease=active['lease']
    original=TurnInputs.public_input('look',[image_block(base64.b64encode(large).decode(),'image/png')])
    sent=ClaudeImageTransport(tmp_path,'conversation').frame(frame(large))
    derived=[{'type':'message','role':'user','content':[
        image_block(b['source']['data'],b['source']['media_type'])
        for b in sent['message']['content'] if b['type']=='image']}]
    runner=JournalRunner(c,derived); runner.run=mock.Mock(wraps=runner.run)
    harness=ExecutionHarness(s.coordinator,c,runner)
    with mock.patch.object(harness,'recover',side_effect=InvalidState('staged only')):
        with pytest.raises(InvalidState,match='staged only'):
            harness.run('conversation',lease_id=lease['lease_id'],generation=lease['generation'],
                        prompt='look',input_public=original)
    saved={p:p.read_bytes() for p in tmp_path.glob('inputs/**/*.json')}
    # Variant files are execution caches, not authority for commit-only recovery.
    for p in (tmp_path/'image-variants'/'conversation').glob('*.json'): p.unlink()
    with mock.patch.object(policy,'_pillow',side_effect=AssertionError('no re-encode')):
        row=harness.recover('conversation',lease_id=lease['lease_id'],generation=lease['generation'])
    assert row['terminal_state']=='completed' and runner.run.call_count==1
    assert all(p.read_bytes()==v for p,v in saved.items())


def test_prepare_input_failure_is_before_turn_reservation(tmp_path):
    from shepherd import Service, ExecutionHarness
    s=Service(tmp_path,storage='unified'); s.create('conversation','claude',tmp_path)
    active=s.active('conversation'); c=s.connector(active['binding']); lease=active['lease']
    runner=SimpleNamespace(prepare_input=mock.Mock(side_effect=ImageTransportError('too large')),
                           run=mock.Mock())
    with mock.patch.object(s.coordinator,'begin_round',wraps=s.coordinator.begin_round) as reserve:
        with pytest.raises(PromptNotStarted):
            ExecutionHarness(s.coordinator,c,runner).run('conversation',lease_id=lease['lease_id'],
                generation=lease['generation'],prompt='x',input_public=TurnInputs.public_input('x'))
        reserve.assert_not_called(); runner.run.assert_not_called()
    assert not list(tmp_path.glob('inputs/**/*.json'))


# --- Whole-request budget: compress older history first, omit only what cannot fit.

def noisy(size, seed=1):
    # Incompressible pixels make sizes predictable and large without huge files.
    out=io.BytesIO(); Image.frombytes('RGB',size,random.Random(seed).randbytes(size[0]*size[1]*3)).save(out,format='PNG')
    return out.getvalue()


WIDE=(1200,400)  # below the per-image limits, above the history long edge


def history(*images):
    return [{'type':'user','message':{'role':'user','content':[
        {'type':'tool_result','tool_use_id':f't{i}','content':[native(raw)]}]}}
        for i,raw in enumerate(images)]


def sent_images(entries):
    return [b for e in entries for item in e['message']['content']
            for b in item['content'] if b['type']=='image']


def omitted_texts(entries):
    return [b['text'] for e in entries for item in e['message']['content']
            for b in item['content'] if b['type']=='text']


def fit(entries, current, budget, tmp_path=None):
    t=ClaudeImageTransport(tmp_path,'conversation') if tmp_path else ClaudeImageTransport()
    with mock.patch.object(policy,'MAX_REQUEST_BYTES',budget):
        return t, t.fit_entries(entries,current,prompt='p',system='s')


def test_history_within_budget_is_byte_exact_and_records_nothing():
    entries=history(noisy((300,200),1),noisy((300,200),2)); before=copy.deepcopy(entries)
    t,sent=fit(entries,frame(png()),policy.MAX_REQUEST_BYTES)
    assert sent==entries==before
    assert not t.derivations and not t.entry_sources and t.omitted==0 and t.history_compressed==0


def test_over_budget_compresses_oldest_first_and_keeps_newest_original(tmp_path):
    raws=[noisy(WIDE,i) for i in range(3)]
    entries=history(*raws); before=copy.deepcopy(entries)
    full=ClaudeImageTransport.request_size(entries,frame(png()),prompt='p',system='s')
    t,sent=fit(entries,frame(png()),full-1,tmp_path)
    assert entries==before                                      # archive view untouched
    images=sent_images(sent)
    assert len(images)==3 and t.omitted==0 and t.history_compressed==1
    assert images[0]!=before[0]['message']['content'][0]['content'][0]
    assert images[1:]==[e['message']['content'][0]['content'][0] for e in before[1:]]
    with Image.open(io.BytesIO(base64.b64decode(images[0]['source']['data']))) as im:
        assert max(im.size)==policy.HISTORY_LONG_EDGE
    r=t.receipt(); policy.validate_receipt(r)
    assert [d['policy'] for d in r['derivations']]==[policy.HISTORY_POLICY_VERSION]
    assert t.entry_sources=={'history:'+hashlib.sha256(raws[0]).hexdigest()}
    assert ClaudeImageTransport.request_size(sent,frame(png()),prompt='p',system='s')<=full-1


def test_images_that_cannot_fit_even_compressed_are_omitted_before_encoding():
    raws=[noisy(WIDE,i) for i in range(4)]
    entries=history(*raws)
    newest=ClaudeImageTransport.request_size(history(raws[-1]),frame(png()),prompt='p',system='s')
    t=ClaudeImageTransport()
    # A high estimate means an older image cannot fit beside the newest at any size.
    with mock.patch.object(policy,'HISTORY_ESTIMATE_BYTES_PER_PIXEL',50.0), \
         mock.patch.object(policy,'MAX_REQUEST_BYTES',newest+50_000), \
         mock.patch.object(t,'_compress_history',wraps=t._compress_history) as shrink:
        sent=t.fit_entries(entries,frame(png()),prompt='p',system='s')
    images=sent_images(sent)
    assert t.omitted==3 and t.history_compressed==0 and len(images)==1
    assert images[0]==entries[-1]['message']['content'][0]['content'][0]   # newest stays original
    assert shrink.call_count==0                                 # omitted ones were never encoded
    texts=omitted_texts(sent)
    assert len(texts)==3 and all('未随本次请求发送' in s and '1200×400' in s for s in texts)
    assert t.entry_sources=={'omitted:'+hashlib.sha256(r).hexdigest() for r in raws[:3]}


def test_exact_size_overrun_omits_oldest_after_compression_until_request_fits():
    raws=[noisy(WIDE,i) for i in range(8)]
    entries=history(*raws)
    budget=ClaudeImageTransport.request_size(history(raws[-1]),frame(png()),prompt='p',system='s')
    t=ClaudeImageTransport()
    # A low estimate keeps everything in planning; only exact sizes find the overrun.
    with mock.patch.object(policy,'HISTORY_ESTIMATE_BYTES_PER_PIXEL',0.001), \
         mock.patch.object(policy,'MAX_REQUEST_BYTES',budget):
        sent=t.fit_entries(entries,frame(png()),prompt='p',system='s')
        t.validate_request(sent,frame(png()),prompt='p',system='s')
    kinds=[e['message']['content'][0]['content'][0]['type'] for e in sent]
    assert 1<=t.omitted<8 and kinds==['text']*t.omitted+['image']*(8-t.omitted)
    assert t.history_compressed==8-t.omitted


def test_history_image_count_keeps_newest_within_limit():
    image=native(png())
    entries=[{'type':'user','message':{'role':'user','content':[
        {'type':'tool_result','tool_use_id':'t','content':[image]*99}]}}]
    t=ClaudeImageTransport(); sent=t.fit_entries(entries,frame(png()),prompt='p')
    assert t.omitted==1 and len(sent[0]['message']['content'][0]['content'])==99
    assert sent[0]['message']['content'][0]['content'][0]['type']=='text'


def test_current_turn_images_are_never_omitted():
    t=ClaudeImageTransport()
    current=frame(png())
    with mock.patch.object(policy,'MAX_REQUEST_BYTES',ClaudeImageTransport.request_size([],current,prompt='p')-1):
        with pytest.raises(ImageTransportError,match='省略全部历史图片后仍超出'):
            t.fit_entries(history(noisy((400,300))),current,prompt='p')
    assert current==frame(png())


def test_undecodable_oversized_history_image_is_omitted_not_fatal():
    raw=png(); ihdr=struct.pack('>IIBBBBB',10000,10000,8,2,0,0,0)
    bomb=raw[:16]+ihdr+struct.pack('>I',zlib.crc32(b'IHDR'+ihdr))+raw[33:]
    t=ClaudeImageTransport()
    with mock.patch.object(policy,'_pillow',side_effect=AssertionError('must not decode')):
        sent=t.fit_entries(history(bomb,png()),frame(png()),prompt='p')
    assert t.omitted==1 and len(sent_images(sent))==1


def test_history_variants_are_cached_separately_and_deterministic(tmp_path):
    raws=[noisy(WIDE,i) for i in range(2)]
    entries=history(*raws)
    budget=ClaudeImageTransport.request_size(entries,frame(png()),prompt='p',system='s')-1
    first_t,first=fit(entries,frame(png()),budget,tmp_path)
    assert first_t.history_compressed==1
    with mock.patch.object(policy,'_pillow',side_effect=AssertionError('cached, no re-encode')):
        _,second=fit(entries,frame(png()),budget,tmp_path)
    assert first==second
    # The per-image policy for the same source keeps its own cache entry.
    t=ClaudeImageTransport(tmp_path,'conversation'); big=noisy((2100,300),9)
    t.image(native(big))
    assert [d['policy'] for d in t.receipt()['derivations']]==[policy.POLICY_VERSION]
    assert len(list((tmp_path/'image-variants'/'conversation').glob('*.json')))==2


def test_old_receipts_remain_valid(tmp_path):
    t=ClaudeImageTransport(tmp_path,'conversation'); t.image(native(png((2100,100))))
    policy.validate_receipt(t.receipt())
    bad=copy.deepcopy(t.receipt()); bad['derivations'][0]['policy']='other'
    with pytest.raises(policy.ImageInputError):
        policy.validate_receipt(bad)


def test_turn_notice_reports_history_compression_and_omission():
    from engine_adapters.shepherd import image_transport_notice
    t=ClaudeImageTransport()
    assert image_transport_notice(None) is None and image_transport_notice(t) is None
    t.history_compressed=2; t.omitted=3
    notice=image_transport_notice(t)
    assert notice['stage']=='image_transport' and notice['scope']=='turn'
    assert any('压缩了 2 张' in d for d in notice['details'])
    assert any('3 张' in d and '未发送' in d for d in notice['details'])
    t.omitted=0; t.history_compressed=0; t.derivations[('p','image/png','a'*64)]={}
    assert len(image_transport_notice(t)['details'])==1
