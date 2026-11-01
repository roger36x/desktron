"""手机文件打开走完整宿主；收起保留页面并回到会话。"""
from pathlib import Path
import re
import subprocess
import unittest
import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / 'frontend'


def function(source, name):
    match = re.search(r'(?:async )?function ' + name + r'\([^\n]*\).*?\n}\n', source, re.S)
    assert match, name
    return match.group()


class FullscreenBehavior(unittest.TestCase):
    def run_js(self, code):
        result = subprocess.run(['node', '-e', code], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_chat_file_keeps_singleton_path_line_and_full_host(self):
        files = (FRONT / 'app-files.js').read_text(encoding='utf-8')
        refs = (FRONT / 'app-chat-refs.js').read_text(encoding='utf-8')
        code = '''
const assert = require('assert');
let pages = [], shown = [], messages = [], attrs = {}, fileBrowserCurrent;
const FILE_BROWSER_PAGE_KEY = 'file-browser', FILE_BROWSER_STATE_KEY = 'state';
const localStorage = {setItem(){}};
const frame = {getAttribute:k=>attrs[k], setAttribute:(k,v)=>attrs[k]=v,
  contentWindow:{postMessage:m=>messages.push(m)}};
function openContentPage(url,label,icon,build,show,opts) {
  pages.push({url,app:opts.app,el:{querySelector:()=>frame}});
}
function _fileBrowserBuild() {}
function showOverlay(p) { shown.push(p); }
async function hsResolveFile(path) { return {root:'wf',rel:path}; }
'''
        for name in ['fileBrowserAppDef', 'fileBrowserPage', 'fileBrowserFrame', 'fileBrowserSrc',
                     'fileBrowserRemember', 'fileBrowserEnsurePage', 'openFileBrowser']:
            code += function(files, name)
        code += function(refs, 'hsOpenFile')
        code += '''
(async () => {
  await hsOpenFile('docs/first.md', '23');
  assert.equal(pages.length,1); assert.equal(shown[0],pages[0]);
  const url = new URL(attrs.src,'http://desk.test');
  assert.equal(url.searchParams.get('root'),'wf');
  assert.equal(url.searchParams.get('path'),'docs/first.md');
  assert.equal(url.searchParams.get('line'),'23');
  assert.equal(url.searchParams.has('compact'),false);
  await hsOpenFile('docs/second.md','');
  assert.equal(pages.length,1); assert.equal(shown[1],shown[0]);
  assert.equal(messages[0].path,'docs/second.md');
})().catch(e=>{console.error(e);process.exit(1)});
'''
        self.run_js(code)

    def test_mobile_minimizes_and_desktop_returns_to_sidebar(self):
        source = (FRONT / 'app-pages.js').read_text(encoding='utf-8')
        code = '''
const assert = require('assert');
const app = {app:true}, chat = {url:'chat:original'}, otherChat = {url:'chat:other'};
let mobile = true, pActive = app, sideAppReturnState, sides = [], shown = [];
let lastSideAppPage, lastSideAppPages = [], overlayOpen = true;
const pages = [app,chat,otherChat];
const isMobileView = () => mobile;
const appPageInSide = p => sides.includes(p);
function hideAppSide() { sides = []; }
function overlayHomePage() { return otherChat; }
function showOverlay(p) { pActive = p; shown.push(p); }
function showAppSide(p,home) { sides.push(p); pActive = home || otherChat; }
'''
        for name in ['collapseMainAppToSide','minimizeMainApp','hideOverlay']:
            code += function(source, name)
        code += '''
for (const action of [minimizeMainApp, collapseMainAppToSide]) {
  pActive = app; sides = [{app:true}];
  sideAppReturnState = {app,page:chat,sides:[app]};
  action();
  assert.equal(pActive,chat); // 精确返回，不受全局最近会话变化影响
  assert.equal(sides.length,0);
  assert.equal(lastSideAppPage,app);
  assert.deepEqual(lastSideAppPages,[app]);
  assert(pages.includes(app));
  assert.equal(sideAppReturnState,null);
}
mobile = false; pActive = app;
sideAppReturnState = {app,page:chat,sides:[app]};
assert.equal(collapseMainAppToSide(),true);
assert.deepEqual(sides,[app]); assert.equal(pActive,chat);
'''
        self.run_js(code)


try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, '需要 Playwright 与 WebKit')
@pytest.mark.desktop
class MobileControls(unittest.TestCase):
    def test_phone_directory_and_minimize_controls(self):
        with sync_playwright() as pw:
            browser = pw.webkit.launch()
            page = browser.new_page(viewport={'width':390,'height':844}, is_mobile=True, has_touch=True)
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            def route(request):
                url = request.request.url
                if '/file-browser' in url:
                    request.fulfill(content_type='text/html', body=(FRONT/'pages/file-browser.html').read_text(encoding='utf-8'))
                elif '/style.css' in url:
                    request.fulfill(content_type='text/css', body=(FRONT/'style.css').read_text(encoding='utf-8'))
                elif '/workbench/fs' in url:
                    request.fulfill(content_type='application/json', body='{"items":[]}')
                elif '/vendor/' in url or '/page-theme.js' in url:
                    request.fulfill(content_type='text/javascript', body='')
                elif '/page-theme.css' in url:
                    request.fulfill(content_type='text/css', body='')
                else:
                    request.fulfill(content_type='text/html', body='''
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/style.css">
<div id="overlay" class="app-page"><header id="overlay-head">
<button id="ov-min">收起</button><button id="ov-toside">收到侧边栏</button>
</header><div id="overlay-body"><iframe src="/file-browser" class="overlay-frame"></iframe></div></div>
<style>html,body{margin:0;width:100%;height:100%}#overlay{height:100%}</style>''')
            page.route('http://files.test/**', route)
            page.goto('http://files.test/')
            frame = page.frames[1]
            frame.wait_for_selector('#side-toggle')
            self.assertTrue(page.locator('#ov-min').is_visible())
            self.assertFalse(page.locator('#ov-toside').is_visible())
            self.assertEqual(page.locator('#overlay').bounding_box()['height'],844)
            frame.locator('#side-toggle').click()
            self.assertEqual(frame.locator('#side-toggle').get_attribute('aria-expanded'),'true')
            frame.locator('#side-close').click()
            self.assertEqual(frame.locator('#side-toggle').get_attribute('aria-expanded'),'false')
            page.set_viewport_size({'width':844,'height':390})
            self.assertFalse(page.locator('#ov-toside').is_visible())
            self.assertTrue(page.locator('#ov-min').is_visible())
            self.assertEqual(errors,[])
            browser.close()


if __name__ == '__main__':
    unittest.main()
