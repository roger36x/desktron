"""Execute real login UI/theme code with offline stubs; never spawn a CLI or log in."""
import subprocess

from test_frontend_recall import fn
from frontend_i18n_js import NODE_PRELUDE


HARNESS = r'''
const assert = require('node:assert/strict');
class El {
  constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.listeners={};this.textContent='';}
  append(...items){this.children.push(...items);}
  appendChild(item){this.append(item);return item;}
  setAttribute(key,value){this.attrs[key]=value;}
  addEventListener(key,fn){this.listeners[key]=fn;}
  remove(){this.removed=true;}
}
const document={body:new El('body'),visibilityState:'visible',createElement:tag=>new El(tag),
  addEventListener(){},removeEventListener(){},getElementById(){return null;}};
let calls=[],listAlive=true,attachments=[],failEmbed=false,listError=false,_smCache={},alerts=[];
const alert=text=>alerts.push(text);
async function api(path,opts){calls.push([path,opts]);if(path==='/workbench/terminals'){
 if(listError)throw Error('offline');return [{id:'fixture',alive:listAlive}];}return {};}
async function embedTerminal(host,id,engine){
  host.textContent='synthetic command output';
  if(failEmbed)return null;
  const ws={readyState:1,listeners:{},addEventListener(key,fn){this.listeners[key]=fn;}};
  const inst={id,ws,dispose(){this.disposed=true;ws._intentional=true;}};
  attachments.push(inst);return inst;
}
const all=(el)=>[el,...el.children.flatMap(all)];
const find=cls=>all(document.body).find(el=>el.className?.split(' ').includes(cls));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
'''


def run(script):
    source = fn('app-settings-sections.js', '_authTuiModal')
    result = subprocess.run(['node', '-e', NODE_PRELUDE + source + '\n' + HARNESS + '\n' + script],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_only_task_title_terminal_close_and_original_return_action():
    run(r'''
(async()=>{
 await _authTuiModal('fixture','Claude 登录 · claude auth login','shell');
 assert.equal(find('auth-tui-title').children.length,1);
 assert.equal(find('auth-tui-title').children[0].textContent,'Claude 登录');
 for(const cls of ['auth-tui-details','auth-tui-connection','auth-tui-actions'])assert.equal(find(cls),undefined);
 assert.equal(find('auth-tui-foot').children.length,1);
 assert.equal(find('auth-tui-done').textContent,'完成并返回');
 assert.equal(find('auth-tui-host').textContent,'synthetic command output');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


def test_original_confirmed_exit_closes_and_rechecks_without_claiming_success():
    run(r'''
(async()=>{
 let refreshed=0;await _authTuiModal('fixture','登录','shell',()=>refreshed++);
 listAlive=false;attachments[0].ws.listeners.close();await tick();
 assert.equal(find('auth-tui-back').removed,true);assert.equal(refreshed,1);
 assert.ok(calls.some(([path,opts])=>path.endsWith('/fixture')&&opts.method==='DELETE'));
 assert.ok(calls.some(([path])=>path==='/workbench/setup'));assert.equal(_smCache,null);
 assert.equal(alerts.length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


def test_network_drop_reattaches_same_terminal_without_rerunning_command():
    run(r'''
(async()=>{
 await _authTuiModal('fixture','登录','shell');
 attachments[0].ws.readyState=3;attachments[0].ws.listeners.close();await tick();
 assert.equal(attachments.length,2);assert.ok(attachments.every(i=>i.id==='fixture'));
 assert.ok(calls.every(([path,opts])=>path==='/workbench/terminals'&&!opts?.method));
 assert.equal(find('auth-tui-back').removed,undefined);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


def test_liveness_query_error_does_not_delete_terminal():
    run(r'''
(async()=>{
 await _authTuiModal('fixture','登录','shell');listError=true;
 attachments[0].ws.listeners.close();await tick();
 assert.equal(attachments.length,2);assert.equal(find('auth-tui-back').removed,undefined);
 assert.ok(calls.every(([path,opts])=>!opts?.method));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


def test_manual_return_rechecks_once_and_intentional_close_does_not_reconnect():
    run(r'''
(async()=>{
 await _authTuiModal('fixture','登录','shell');
 find('auth-tui-done').onclick();find('auth-tui-x').onclick();await tick();
 attachments[0].ws.listeners.close();await tick();
 assert.equal(calls.filter(([path])=>path==='/workbench/setup').length,1);
 assert.equal(calls.filter(([path])=>path==='/workbench/terminals').length,0);
 assert.equal(find('auth-tui-back').removed,true);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


def test_embed_failure_is_reported_before_original_return_flow():
    run(r'''
(async()=>{
 failEmbed=true;await _authTuiModal('fixture','登录','shell');await tick();
 assert.equal(alerts.length,1);assert.match(alerts[0],/无法显示终端/);
 assert.equal(find('auth-tui-back').removed,true);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


def test_local_background_matches_xterm_and_survives_theme_switch():
    source = '\n'.join(fn('app-assets-term.js', name) for name in ('termThemeId', 'termTheme', 'watchTermTheme'))
    script = r'''
const assert=require('node:assert/strict');
const document={documentElement:{dataset:{theme:'dark'}}};
const TERM_THEMES={dark:{background:'#191919',foreground:'#d1d1d1'},light:{background:'#fbfbfb',foreground:'#343434'}};
const host={};
const getComputedStyle=el=>({getPropertyValue:key=>document.documentElement.dataset.theme==='light'?'#eeeeee':'#111111'});
let changed;class MutationObserver{constructor(cb){changed=cb;}observe(){}}
assert.equal(termTheme().background,'#191919');assert.equal(termTheme(host).background,'#111111');
const term={options:{theme:termTheme(host)}};watchTermTheme(term,host);
document.documentElement.dataset.theme='light';changed();
assert.equal(term.options.theme.background,'#eeeeee');assert.equal(term.options.theme.foreground,'#343434');
assert.equal(termTheme().background,'#fbfbfb');
'''
    result = subprocess.run(['node', '-e', NODE_PRELUDE + source + '\n' + script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
