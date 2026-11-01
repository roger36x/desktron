"""Task detail safety: failed memo reads, portalled controls and invalidation."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
import pytest

ROOT = Path(__file__).resolve().parents[2]
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, 'Playwright required')
@pytest.mark.desktop
class DetailSafety(unittest.TestCase):
    def setUp(self):
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        self.browser = self.pw.webkit.launch()
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page()
        self.page.set_content('<main id="main" style="height:800px"></main>')
        self.page.add_script_tag(content=(ROOT/'frontend/app-panel-task-memo.js').read_text(encoding='utf-8'))

    def test_failed_memo_read_never_opens_blank_editor_and_retry_restores_body(self):
        self.page.evaluate('''async () => {
          window.task = {title:'Title',note:'note1'};
          window.host = document.createElement('div');document.body.append(host);
          window.calls = []; window.fail = true;
          window.api = async (_url, opts) => {
            const op = JSON.parse(opts.body).op; calls.push(op);
            if (fail) throw new Error('offline');
            return {text:'Title\\n\\nOriginal body'};
          };
          _wfNoteRender = (el, dom, t, text) => {
            const ed=document.createElement('div');ed.contentEditable='true';
            ed.textContent=text;el.replaceChildren(ed);
          };
          await _wfNoteMount(host, 'alpha', task);
        }''')
        self.assertEqual(self.page.locator('[contenteditable=true]').count(), 0)
        self.assertIn('读取失败', self.page.locator('.wf-note-err').inner_text())
        self.page.evaluate('fail=false')
        self.page.get_by_role('button', name='重试').click()
        self.assertEqual(self.page.locator('[contenteditable=true]').inner_text(), 'Original body')
        self.assertEqual(self.page.evaluate('calls'), ['memo-get', 'memo-get'])

    def test_error_payload_and_null_response_also_block_editing(self):
        for response in (None, {'error': 'denied'}, {}):
            self.page.evaluate('''async response => {
              const host=document.createElement('div');document.body.append(host);
              window.api=async()=>response;
              _wfNoteRender=()=>{throw new Error('must not render editor')};
              await _wfNoteMount(host,'alpha',{title:'Title',note:'note1'});
            }''', response)
        self.assertEqual(self.page.locator('[contenteditable=true]').count(), 0)
        self.assertEqual(self.page.locator('.wf-note-err').count(), 3)

    def test_portalled_menu_and_image_handle_do_not_close_sheet(self):
        self.page.evaluate('''() => {
          window.opened=openFloatSheet({title:'Task'});
          const menu=document.createElement('div');menu.className='rp-ctx';
          const b=document.createElement('button');b.textContent='Edit flow';
          b.onclick=()=>{window.attached=opened.sheet.isConnected};menu.append(b);document.body.append(menu);
          const ed=document.createElement('div'), img=document.createElement('img');
          img.style.width='100px';ed.append(img);opened.body.append(ed);_wfImgSelect(img, ed);
        }''')
        self.page.get_by_role('button', name='Edit flow').click()
        self.assertTrue(self.page.evaluate('attached'))
        self.page.evaluate('''() => {
          const img=opened.body.querySelector('img');_wfImgSelect(img,img.parentElement);
          _wfImgSel.h.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,clientX:100}));
          document.dispatchEvent(new MouseEvent('mousemove',{bubbles:true,clientX:140}));
          document.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
        }''')
        self.assertTrue(self.page.evaluate('opened.sheet.isConnected'))
        self.page.evaluate("document.body.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}))")
        self.assertFalse(self.page.evaluate('opened.sheet.isConnected'))


@unittest.skipUnless(shutil.which('node'), 'Node required')
class BundleInvalidation(unittest.TestCase):
    def test_bundle_after_invalidation_cannot_reuse_old_get(self):
        source = (ROOT/'frontend/app-tasks.js').read_text(encoding='utf-8').split('// 容器行的子区进度聚合:')[0]
        script = '''const vm=require('node:vm'), assert=require('node:assert/strict');
const flights=new Map(), pending=[];
const fresh={items:[{title:'fresh'}],total:1};
const ctx={window:{},api:url=>{
  if(flights.has(url)) return flights.get(url);
  if(url.startsWith('/workbench/domains/')) return Promise.resolve(fresh);
  let resolve;const p=new Promise(r=>{resolve=r});flights.set(url,p);pending.push({url,resolve});return p;
}};
vm.createContext(ctx);vm.runInContext(SOURCE,ctx);
(async()=>{
 const old=vm.runInContext("fetchTasksBundle(['b','a'])",ctx);
 vm.runInContext("invalidateTasks('a')",ctx);
 const recent=vm.runInContext("fetchTasksBundle(['a','b'])",ctx);
 assert.equal(pending.length,2,'fresh generation must not dedupe with old bundle');
 assert.notEqual(pending[0].url,pending[1].url);
 pending[1].resolve({a:fresh,b:fresh}); await recent;
 pending[0].resolve({a:{items:[{title:'stale'}]},b:fresh}); await old;
 assert.equal(vm.runInContext('_taskItems.a[0].title',ctx),'fresh');
})().catch(e=>{console.error(e);process.exitCode=1});
'''.replace('SOURCE', json.dumps(source))
        run = subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=20, encoding='utf-8')
        self.assertEqual(run.returncode,0,run.stderr)


if __name__ == '__main__':
    unittest.main()
