"""首屏立即核验缓存工作态；不等待轮询，不把旧 busy 当实时状态。"""
import re

import pytest
from test_frontend_session_integrity import ROOT, run


def check(case, local=True):
    source = (ROOT / 'frontend/app-panel-terms.js').read_text(encoding='utf-8')
    functions = '\n'.join(re.search(r'function ' + name + r'\([^\n]*\).*?\n}', source, re.S).group()
                          for name in ('termState', 'termStatTip', 'updateTermRow', 'startTermPoll'))
    setup = r'''
let _termPoll=null, timer, intervalCount=0, requests=0, resolveRequest;
let editing=false, dragging=false, painted=0;
globalThis.termState=termState;
globalThis.termStatTip=termStatTip;
globalThis.setInterval=(fn,delay)=>{timer={fn,delay};intervalCount++;return 1};
globalThis.isLocalView=()=>LOCAL;
document.hidden=false;
document.body={classList:{contains:()=>dragging}};
document.querySelector=selector=>selector==='.ti-rename' && editing ? {} : null;
const stat={className:'ti-stat st-unknown',title:''}, label={textContent:'session'};
const item={dataset:{sid:'session',orderId:'session'},classList:{toggle(){}},
  querySelector:selector=>selector==='.ti-stat'?stat:selector==='.ti-label'?label:null};
const host={querySelectorAll:()=>[item],closest:()=>({dataset:{pid:'wf'}})};
document.querySelectorAll=selector=>selector==='details.pf-work[open] .pf-sess'?[host]:[];
globalThis.refreshFoldCounts=()=>{};
globalThis.visibleSessionsFor=()=>({shown:_termsList});
globalThis.applySideFilter=()=>painted++;
globalThis.stripTitleGlyph=s=>s;
globalThis.compactBranchName=()=>'';
globalThis.renderProjSessions=()=>{throw Error('unchanged rows must not be rebuilt')};
globalThis.api=()=>{requests++;return new Promise(resolve=>resolveRequest=resolve)};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
seedTerms(good([row('session','codex',{busy:true})]));
seedTerms(JSON.parse(cache.get('cd_terms_snapshot')),{cached:true});
assert.equal(termState(_termsList[0]),'unknown');
'''.replace('LOCAL', 'true' if local else 'false')
    run(functions + setup + case)


@pytest.mark.parametrize('local', [True, False])
def test_immediate_fetch_paints_busy_without_any_timer_tick(local):
    check(r'''
startTermPoll();
startTermPoll(); // 重渲不能再起一份首轮请求/轮询。
await flush();
assert.equal(intervalCount,1);
assert.equal(timer.delay,isLocalView()?1500:5000);
assert.equal(requests,1);
assert.equal(painted,0); // 请求未返回时不伪造工作态。
assert.equal(stat.className,'ti-stat st-unknown');
resolveRequest(good([row('session','codex',{busy:true})]));
await flush();
assert.equal(stat.className,'ti-stat st-working');
assert.equal(painted,1); // 未运行任何定时器，回包已经落到 DOM。
assert.equal(requests,1);
''', local)


def test_startup_reuses_an_existing_request_and_paints_its_result():
    check(r'''
const existing=refreshTerms();
await flush();
startTermPoll();
await flush();
assert.equal(requests,1);
resolveRequest(good([row('session','codex',{busy:true})]));
await existing; await flush();
assert.equal(stat.className,'ti-stat st-working');
assert.equal(painted,1);
''')


def test_empty_cached_list_also_triggers_immediate_fetch():
    check(r'''
seedTerms(null,{cached:true});
startTermPoll(); await flush();
assert.equal(requests,1);
resolveRequest(good([row('session','codex',{busy:true})]));
await flush();
assert.equal(stat.className,'ti-stat st-working');
''')


@pytest.mark.parametrize('guard', ['document.hidden', 'editing', 'dragging'])
def test_inactive_or_editing_page_does_not_fetch(guard):
    check(guard + r'''=true;
startTermPoll(); await flush();
assert.equal(requests,0);
assert.equal(painted,0);
''')


@pytest.mark.parametrize('guard', ['document.hidden', 'editing', 'dragging'])
def test_guard_entered_during_fetch_defers_paint_until_safe(guard):
    check(r'''
startTermPoll(); await flush();
''' + guard + r'''=true;
resolveRequest(good([row('session','codex',{busy:true})]));
await flush();
assert.equal(painted,0);
assert.equal(stat.className,'ti-stat st-unknown');
''' + guard + r'''=false;
await timer.fn();
assert.equal(stat.className,'ti-stat st-working');
assert.equal(painted,1);
assert.equal(requests,1); // 定时器复用刚返回的新鲜结果。
''')


def test_unconfirmed_result_does_not_flash_a_cached_busy_session():
    check(r'''
startTermPoll(); await flush();
resolveRequest(partial([]));
await flush();
assert.equal(stat.className,'ti-stat st-unknown');
assert(_termsList[0].list_stale);
''')
