"""Visible engine update feedback; no real engine installation or network calls."""
from pathlib import Path

from frontend_i18n_js import NODE_PRELUDE
from test_frontend_recall import RecallUI

ROOT = Path(__file__).resolve().parents[2]
FUNCTIONS = [
    ('app-chat-core.js', 'hsEnvFull'),
    ('app-chat-core.js', 'hsEngineUpdate'),
    ('app-chat-tasksbar.js', 'hsContextStatusSync'),
    ('app-chat-tasksbar.js', 'hsContextStatusAction'),
]
HARNESS = NODE_PRELUDE + r'''
const assert = require('node:assert/strict');
const _hsUpdateMuted = new Set();
function node() { return {attrs:{},dataset:{},textContent:'',hidden:false,disabled:false,
  setAttribute(k,v){this.attrs[k]=v;},getAttribute(k){return this.attrs[k]||'';},
  removeAttribute(k){delete this.attrs[k];}}; }
const nodes={};
const host={querySelector:s=>nodes[s]||(nodes[s]=node())};
const button=nodes['.hs-context-status-action']=Object.assign(node(),{
  querySelector:s=>nodes[s]||(nodes[s]=node())});
let legacyClicks=0;
const legacy={hidden:false,click(){legacyClicks++;}};
const v={engine:'pi',updateKey:'pi@2.0.0',envCurrent:{state:'update',stage:'version',
 label:'pi 有新版本：当前 1.0.0，最新 2.0.0',details:['npm install example'],
 action:{kind:'engine-update',engine:'pi',label:'更新'}},
 el:{querySelector:()=>host},envEl:{querySelector:()=>legacy}};
const title=()=>nodes['.hs-context-status-title'].textContent;
const detail=()=>nodes['.hs-context-status-detail'].textContent;
const label=()=>nodes['.hs-context-status-action-label'].textContent;
const event=()=>({stopPropagation(){},currentTarget:button});
function run(task){ task().catch(e=>{console.error(e);process.exitCode=1;}); }
'''


def test_update_uses_existing_reviewed_icon_and_respects_reduced_motion():
    css = (ROOT/'frontend/style.css').read_text(encoding='utf-8')
    assert '/icons/reviewed-remaining/_BR_ICON_RELOAD.svg' in css
    assert '.hs-context-status-action-icon::after' not in css
    assert 'hs-engine-update-spin' in css
    assert '@media (prefers-reduced-motion: reduce)' in css
    assert '.hs-context-status-action:disabled' in css
    assert (ROOT/'frontend/icons/reviewed-remaining/_BR_ICON_RELOAD.svg').is_file()


def test_visible_button_shows_progress_immediately_blocks_duplicates_and_keeps_success():
    RecallUI().run_js(FUNCTIONS, HARNESS + r'''
let release,calls=0;
function api(path,options){calls++;assert.equal(path,'/workbench/engine-update');
 assert.deepEqual(JSON.parse(options.body),{engine:'pi'});
 return new Promise(resolve=>release=resolve);}
run(async()=>{
 hsContextStatusSync(v);assert.match(title(),/有新版本/);
 const pending=hsContextStatusAction(v,event());
 assert.equal(legacyClicks,0);assert.equal(calls,1);
 assert.equal(title(),'正在更新 pi');assert.equal(label(),'更新中…');
 assert.equal(button.disabled,true);assert.equal(button.attrs['aria-busy'],'true');
 assert.match(detail(),/下载并安装/);
 await hsContextStatusAction(v,event());assert.equal(calls,1);
 v.envCurrent=null;hsContextStatusSync(v);assert.equal(title(),'正在更新 pi');
 release({ok:true,out:'installed'});await pending;
 assert.equal(title(),'pi 已更新');assert.match(detail(),/下次启动引擎/);
 assert.match(detail(),/installed/);assert.equal(button.disabled,false);assert.equal(button.hidden,true);
 assert.equal(button.attrs['aria-busy'],'false');assert.ok(_hsUpdateMuted.has(v.updateKey));
 hsContextStatusSync(v);assert.equal(title(),'pi 已更新');
});
''')


def test_failure_is_visible_with_output_and_does_not_claim_ready():
    RecallUI().run_js(FUNCTIONS, HARNESS + r'''
async function api(){return {ok:false,out:'npm EACCES'};}
run(async()=>{
 await hsContextStatusAction(v,event());
 assert.equal(title(),'pi 更新失败');assert.match(detail(),/npm EACCES/);
 assert.equal(host.className,'hs-context-status error');assert.equal(button.hidden,true);
 assert.equal(_hsUpdateMuted.size,0);
});
''')


def test_lost_or_invalid_receipt_stays_unknown_without_automatic_retry():
    RecallUI().run_js(FUNCTIONS, HARNESS + r'''
let calls=0;
async function api(){calls++;throw new Error('network interrupted');}
run(async()=>{
 await hsContextStatusAction(v,event());
 assert.equal(title(),'pi 更新结果未确认');assert.match(detail(),/安装可能仍在执行/);
 assert.match(detail(),/network interrupted/);assert.equal(button.hidden,true);
 await hsEngineUpdate(v,'pi',button);assert.equal(calls,1);
 v.engineUpdateNotice=null;
 api=async()=>{calls++;return '<html>unexpected response</html>';};
 await hsContextStatusAction(v,event());
 assert.equal(title(),'pi 更新结果未确认');assert.equal(calls,2);
});
''')


def test_old_endpoint_and_other_engine_issues_remain_visible():
    RecallUI().run_js(FUNCTIONS, HARNESS + r'''
async function api(){throw Object.assign(new Error('HTTP 404'),{status:404});}
run(async()=>{
 await hsContextStatusAction(v,event());assert.equal(title(),'pi 更新失败');
 assert.match(detail(),/后端还在跑旧代码/);
 v.envCurrent={stage:'auth',state:'warning',label:'需要登录',action:{kind:'terminal',label:'登录'}};
 hsContextStatusSync(v);assert.equal(title(),'需要登录');assert.equal(label(),'登录');
 await hsContextStatusAction(v,event());assert.equal(legacyClicks,1);
 v.envCurrent={stage:'mcp',state:'error',label:'MCP failed'};
 hsContextStatusSync(v);assert.equal(title(),'MCP failed');
 v.engine='codex';v.envCurrent=null;hsContextStatusSync(v);
 assert.equal(title(),'引擎已就绪');assert.equal(detail(),'codex 可正常使用');
});
''')


def test_update_feedback_uses_english_interface_copy():
    RecallUI().run_js(FUNCTIONS, HARNESS + r'''
DeskI18n.setLocale('en');
async function api(){return {ok:true};}
run(async()=>{
 await hsContextStatusAction(v,event());assert.equal(title(),'pi updated');
 assert.match(detail(),/will not restart automatically/);
});
''')
