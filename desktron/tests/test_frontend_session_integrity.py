"""执行真实列表入口：不完整响应、后端切换与迟到请求。"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def run(case):
    source = (ROOT / 'frontend/app-panel-terms.js').read_text(encoding='utf-8')
    source = source[source.index('let activeTermId'):source.index('function taskRunTermId')]
    harness = r'''
const assert=require('assert');
const cache=new Map();
globalThis.localStorage={getItem:k=>cache.get(k)||null,setItem:(k,v)=>cache.set(k,v)};
globalThis.document={cookie:'',querySelector(){return null}};
globalThis.wbState={taskRuns:{}};
globalThis.saveState=()=>{};
const good=(sessions,source='A')=>({sessions,source,complete:true,issues:[]});
const partial=(sessions,source='A')=>({sessions,source,complete:false,issues:[{source:'pi',code:'index_unavailable'}]});
const row=(id,engine='pi',extra={})=>({id,engine,title:id,...extra});
'''
    result = subprocess.run(['node', '-e', harness + source + '\n(async()=>{' + case + '\n})().catch(e=>{console.error(e);process.exit(1)})'],
                            capture_output=True, text=True, timeout=10, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_partial_response_keeps_missing_rows_without_overwriting_complete_cache():
    run(r'''
seedTerms(good([row('old','pi',{busy:true}),row('alias','codex',{session_id:'adp_1'})]));
const stored=cache.get('cd_terms');
seedTerms(partial([row('new'),row('engine','codex',{session_id:'adp_1'})]));
assert.deepEqual(_termsList.map(r=>r.id).sort(),['engine','new','old']);
assert(_termsList.find(r=>r.id==='old').list_stale);
assert.equal(cache.get('cd_terms'),stored);
seedTerms(good([]));
assert.deepEqual(_termsList,[]); // 完整空表才能确认删除/关闭
''')


def test_identity_does_not_merge_different_engines_or_different_sources():
    run(r'''
seedTerms(good([row('same','claude')]));
seedTerms(partial([row('same','pi')]));
assert.equal(_termsList.length,2);
seedTerms(partial([row('other')],'B'));
assert.deepEqual(_termsList.map(r=>r.id),['other']);
''')


def test_malformed_payload_preserves_current_state_and_reports_failure():
    run(r'''
seedTerms(good([row('kept')]));
assert.throws(()=>seedTerms({sessions:[],source:'A'}));
assert.throws(()=>seedTerms({sessions:[null],source:'A',complete:true}));
assert.equal(_termsList[0].id,'kept');
api=async()=>{throw Error('offline')};
await refreshTerms({maxAge:0});
assert.equal(_termsList[0].id,'kept');
assert(_termsList[0].list_stale);
''')


def test_cache_is_scoped_and_late_response_cannot_restore_previous_backend():
    run(r'''
seedTerms(good([row('local')]));
let release; api=()=>new Promise(r=>release=r);
const old=refreshTerms({maxAge:0});
await Promise.resolve();
document.cookie='cd_backend=server';
api=async()=>good([row('remote')],'B');
await refreshTerms({maxAge:0});
release(good([row('late-local')]));
await old;
assert.deepEqual(_termsList.map(r=>r.id),['remote']);
document.cookie='';
seedTerms(JSON.parse(cache.get('cd_terms_snapshot')),{cached:true});
assert.deepEqual(_termsList,[]); // 不把服务器缓存当本机
''')


def test_cached_list_is_verified_silently_without_creating_retry_notice():
    run(r'''
seedTerms(good([row('cached')]));
const stored=JSON.parse(cache.get('cd_terms_snapshot'));
let created=0;
document.querySelector=s=>s==='#panel-list'?{parentNode:{insertBefore(){}}}:null;
document.createElement=()=>{created++;return {style:{},querySelector(){return {}}}};
seedTerms(stored,{cached:true});
assert.equal(created,0);
assert(_termsList[0].list_stale);
assert.equal(_termsAt,0); // 静默不等于跳过后台核验
''')


def test_synchronous_network_failure_keeps_rows_and_retry_remains_available():
    run(r'''
let notice;
const message={textContent:''},button={};
document.querySelector=s=>s==='#panel-list'?{parentNode:{insertBefore(n){notice=n}}}:notice;
document.createElement=()=>({style:{},querySelector:s=>s==='button'?button:message});
seedTerms(good([row('kept')]));
api=()=>{throw Error('network failed before promise')};
await refreshTerms({maxAge:0});
assert(_termsList[0].list_stale);
assert(!notice.hidden && message.textContent.includes('重试'));
let rendered=0;globalThis._rerenderSessionsHost=()=>rendered++;
api=async()=>good([row('recovered')]);
await button.onclick();
assert(notice.hidden);
assert.equal(_termsList[0].id,'recovered');
assert.equal(rendered,1);
''')


def test_legacy_arrays_remain_readable_but_cannot_downgrade_known_protocol():
    run(r'''
seedTerms([row('legacy')]);
assert.equal(_termsList.length,1);
seedTerms(good([row('known')]));
assert.throws(()=>seedTerms([])); // 同页不能被旧协议迟到空表覆盖
assert.equal(_termsList[0].id,'known');
''')


def test_reload_checks_cached_source_and_failed_storage_keeps_atomic_snapshot():
    run(r'''
seedTerms(good([row('cached')]));
const stored=cache.get('cd_terms_snapshot');
seedTerms(JSON.parse(stored),{cached:true});
assert(_termsList[0].list_stale);
assert.equal(_termsAt,0);
api=async()=>good([row('fresh')]);
await refreshTerms();
assert.equal(_termsList[0].id,'fresh');
localStorage.setItem=()=>{throw Error('quota')};
const before=cache.get('cd_terms_snapshot');
seedTerms(good([row('another-device')],'B'));
assert.equal(cache.get('cd_terms_snapshot'),before);
assert.equal(JSON.parse(before).source,'A');
assert.equal(JSON.parse(before).sessions[0].id,'fresh');
''')
