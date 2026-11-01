"""Execute the actual setup functions across two different page instances."""
from pathlib import Path
import subprocess
from frontend_i18n_js import NODE_PRELUDE

SOURCE = (Path(__file__).resolve().parents[2] / 'frontend/app-settings-setup.js').read_text(encoding='utf-8')
RUN = SOURCE[SOURCE.index('async function _setupRunInPanel('):SOURCE.index('function _setupTaskFeedback(')]
RESTORE = SOURCE[SOURCE.index('async function _setupFindTask('):SOURCE.index('// 首启浮层只自动出现一次')]


def test_reopened_page_reattaches_without_posting_command():
    js = r'''
const assert = require('assert');
globalThis.setTimeout = () => 0;
const element = () => ({hidden:false, textContent:'', innerHTML:'', style:{},
 classList:{toggle(){},add(){},remove(){},contains(){return false}}, replaceChildren(){}, appendChild(){}});
globalThis.document = {createElement: element};
const page = () => ({isConnected:true, querySelector(){return element();}});
let posts=0, attachments=[], task=null;
async function api(path, opts) {
 if (!opts) return {tasks:task ? [task] : []};
 posts++;
 const body=JSON.parse(opts.body);
 assert(body.setup_request_id);
 task={id:body.setup_request_id, terminal_id:'same-pty', title:'部署', remote:true, state:'running', attachable:true};
 return {id:'same-pty', task};
}
async function embedTerminal(host,id,kind,opts){attachments.push(id);return {dispose(){}};}
function _setupTaskFeedback(){}
async function _setupRefresh(){}
function _setupReceiveAuthLink(){}
RUN
RESTORE
(async()=>{
 const first=page();
 await _setupRunInPanel(first,'deploy fixed-target','部署',{remote:true});
 first.isConnected=false; first._setupTerm.dispose();
 const second=page();
 await _setupResumeTask(second);
 assert.equal(posts,1);
 assert.deepEqual(attachments,['same-pty','same-pty']);
 // Reopening the progress panel on the same page must replace a stale socket.
 await _setupResumeTask(second);
 assert.equal(posts,1);
 assert.equal(attachments.length,3);
 task={...task,state:'failed',attachable:false,output:'failed safely'};
 const third=page();
 await _setupResumeTask(third);
 assert.equal(posts,1);
 assert.equal(attachments.length,3);
})().catch(e=>{console.error(e);process.exit(1)});
'''.replace('RUN\n', RUN + '\n').replace('RESTORE\n', RESTORE + '\n')
    result = subprocess.run(['node', '-e', NODE_PRELUDE + js], capture_output=True, text=True, timeout=15, encoding='utf-8')
    assert result.returncode == 0, result.stderr
