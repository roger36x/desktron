"""单侧边栏生产 DOM 与 WebKit 交互；不连接真实会话。"""
import json
from pathlib import Path
import re
import unittest
import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend"
SIDEBAR = (FRONT / "app-panel-sidebar.js").read_text(encoding='utf-8')
TERMS = (FRONT / "app-panel-terms.js").read_text(encoding='utf-8')


class RemovedSidebarContract(unittest.TestCase):
    def test_no_runtime_entry_or_asset_remains(self):
        for name in ('app-multi-sidebar.js', 'multi-sidebar.css'):
            self.assertFalse((FRONT / name).exists())
            self.assertNotIn(name, (FRONT / 'index.html').read_text(encoding='utf-8'))
            self.assertNotIn(name, (ROOT / 'desktron/lib/frontend_bundle.py').read_text(encoding='utf-8'))
        for file in FRONT.glob('*'):
            if file.suffix in ('.js', '.css', '.html'):
                source = file.read_text(encoding='utf-8')
                for token in ('multiSidebar', '_sideScope', 'multi-side-', '新增侧边栏'):
                    self.assertNotIn(token, source, str(file))

    def test_only_legacy_layout_key_is_retired(self):
        source = (FRONT / 'app-events.js').read_text(encoding='utf-8')
        self.assertIn('localStorage.removeItem("cd_multi_sidebar_v1")', source)
        self.assertNotIn('localStorage.clear()', source)

    def test_sidebar_does_not_reference_removed_kb_domain(self):
        self.assertNotIn('KB_DOMAIN', SIDEBAR)


def function(source, name):
    found = re.search(r"(?:async )?function " + name + r"\([^\n]*\).*?\n}\n", source, re.S)
    assert found, name
    return found.group()


DATA = [dict(id=f"{engine}-{i}", engine=engine, title=f"{engine} 会话 {i}",
             domain="wf" if i < 9 else "_desktop", alive=True)
        for engine in ("claude", "codex", "pi") for i in range(12)]
BASE = r'''
let curEngine = 'claude', activeTermId = null, _termsList = DATA;
let _sessionBucketsCache = null;
const DESKTOP = '_desktop', hiddenDomains = [];
const domains = [{id:'wf', name:'Desk 迭代'}], expandedProjects = new Set(['wf']);
const wbState = {sessionOrder:{}};
function saveState() {} function _pinSave() {}
function applyOrder(items, order, key) {
  return items.slice().sort((a,b)=>(order||[]).indexOf(key(a))-(order||[]).indexOf(key(b)));
}
'''.replace("DATA", json.dumps(DATA, ensure_ascii=False))


try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, "需要 Playwright 和 WebKit")
@pytest.mark.desktop
class SingleSidebarWebKit(unittest.TestCase):
    def setUp(self):
        self.pw = sync_playwright().start()
        self.browser = self.pw.webkit.launch()
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 1000})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("http://sidebar.test/**", lambda route: route.fulfill(
            content_type="text/html", body='<html><body><div id="app"><aside id="panel"><div class="side-brand">Desktron</div><div id="panel-list"></div><footer class="side-foot"><button id="engine-pick">Claude Code</button></footer></aside><main id="main">CURRENT CONVERSATION</main></div><div id="engine-menu" hidden></div></body></html>'))
        self.page.goto("http://sidebar.test/")
        self.page.add_style_tag(content=(FRONT / "style.css").read_text(encoding='utf-8'))
        self.page.add_script_tag(content=BASE + r'''
const $ = s => document.querySelector(s);
const ICONS = {
  enter:'<svg viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg>',
  check:'<svg viewBox="0 0 24 24"><path d="m5 12 4 4 10-10"/></svg>',
  sidebar:'<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2.2"/><path d="M9.5 4v16"/></svg>'
}, ENGINE_ICON={};
const BRAND_ICONS = {};
let engineInfo={engines:{claude:{label:'Claude Code'},codex:{label:'Codex'},pi:{label:'Pi Coding'}},available:{claude:true,codex:true,pi:true}};
const nav={type:'domain',id:'wf'};
const _moreOpen = new Set(), SESS_VISIBLE = 5;
let _termPoll=null, _termHeadSid=null;
function escapeHtml(s){return String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;')}
function taskViewRegister(){} function makeReorderable(){} function persistExpanded(){}
function stripTitleGlyph(s){return s} function termBranchHTML(){return ''} function termRecordHTML(){return ''}
function termStatHTML(){return '<span class="ti-stat st-idle"></span>'}
function attachTerminal(id,engine){window.clicked={id,engine}}
function setTermHead(){} function navPush(){} function _sideHighlight(){}
function renderTaskDock(){} function refreshFoldCountsEnsure(){} function applySideFilter(){}
function refreshFoldCounts(){} function updateTermRow(row,t){row.querySelector('.ti-label').textContent=t.title}
async function refreshTerms(){return _termsList} function _applyTermsList(list){_termsList=list}
function isLocalView(){return true}
window.setInterval = fn => {window.poll=fn;return 1};
''')
        self.page.add_script_tag(content="\n".join(function(TERMS, n) for n in (
            "invalidateSessionBuckets", "sessionBuckets", "termIdentityIds", "termOrderId",
            "termMatchesId", "termRowMatchesId", "bindTermRowIdentity", "startTermPoll")))
        self.page.add_script_tag(content="\n".join(function(SIDEBAR, n) for n in (
            "sideProjects", "projectFold", "_pinSessionOrder", "visibleSessionsFor",
            "renderProjSessions", "looseSessions", "renderLooseSessions", "termPathHTML", "termRow")))
        self.page.add_script_tag(content='''
setTermHead = () => {};
function renderSidebar() {
  const host=document.getElementById('panel-list'); host.replaceChildren();
  for(const p of sideProjects()) host.appendChild(projectFold(p));
  const loose=document.createElement('div'); loose.className='loose-sessions';
  host.appendChild(loose); renderLooseSessions(loose);
}
startTermPoll();
''')

    def tearDown(self):
        self.browser.close()
        self.pw.stop()

    def install_engine_menu(self):
        self.page.add_script_tag(content='''
let engineManifest={}, engineOpts={};
const ENGINE_DESC={}, ENGINE_ACCENT={}, RP_CHECK='✓';
window.statusCalls=[];
function setStatus(ok){statusCalls.push(ok)}
localStorage.setItem('cd_multi_sidebar_v1',JSON.stringify({width:300,panes:[{id:'old',engine:'codex'}]}));
localStorage.setItem('cd_pages','keep-current-conversation');
localStorage.setItem('cd_loose_collapsed','0');
''')
        self.page.add_script_tag(content=function((FRONT / 'app-events.js').read_text(encoding='utf-8'), 'applyEnginePick'))
        self.page.evaluate("applyEnginePick({...engineInfo,engine:'claude'})")

    def test_legacy_layout_is_removed_without_touching_other_state(self):
        self.install_engine_menu()
        self.assertIsNone(self.page.evaluate("localStorage.getItem('cd_multi_sidebar_v1')"))
        self.assertEqual(self.page.evaluate("localStorage.getItem('cd_pages')"), 'keep-current-conversation')
        self.assertEqual(self.page.evaluate("localStorage.getItem('cd_loose_collapsed')"), '0')
        for width, height in ((1440, 1000), (390, 844), (1440, 1000)):
            self.page.set_viewport_size(dict(width=width, height=height))
            self.page.evaluate("applyEnginePick({...engineInfo,engine:'claude'})")
            self.assertEqual(self.page.locator('#multi-sidebar').count(), 0)
            self.assertEqual(self.page.locator('#main').inner_text(), 'CURRENT CONVERSATION')
        self.assertNotIn(False, self.page.evaluate('statusCalls'))
        self.assertEqual(self.errors, [])

    def test_menu_only_switches_engines_and_survives_blocked_storage(self):
        self.install_engine_menu()
        self.page.locator('#engine-pick').click()
        self.assertEqual(self.page.locator('#engine-menu .fm-head').all_text_contents(),
                         ['独立引擎', '联合引擎'])
        self.assertEqual(self.page.locator('#engine-menu .fm-title').all_text_contents(), ['Claude Code','Codex','Pi Coding'])
        self.assertEqual(self.page.locator('#engine-menu .fm-row.on').count(), 1)
        self.page.locator('#engine-menu .fm-row.on').click()
        self.assertTrue(self.page.locator('#engine-menu').is_hidden())
        self.page.evaluate("() => { Storage.prototype.removeItem=()=>{throw Error('storage denied')}; }")
        self.page.evaluate("applyEnginePick({...engineInfo,engine:'claude'})")
        self.assertNotIn(False, self.page.evaluate('statusCalls'))
        self.assertEqual(self.errors, [])

    def test_engine_partition_expansion_polling_and_open(self):
        page = self.page
        for engine in ('claude', 'codex', 'pi'):
            page.evaluate('''engine => {
curEngine=engine; _moreOpen.clear();
_termsList=Array.from({length:12},(_,i)=>({id:engine+'-'+i,engine,title:'会话 '+i,domain:i<9?'wf':'_desktop'}));
renderSidebar();
}''', engine)
            page.wait_for_timeout(30)
            self.assertEqual(page.locator('.pf-sess .term-item').count(), 5)
            self.assertEqual(page.locator('.loose-sessions .term-item').count(), 3)
            page.locator('.sess-more').click()
            self.assertEqual(page.locator('.pf-sess .term-item').count(), 9)
            page.locator('.pf-sess .term-item').first.click()
            self.assertEqual(page.evaluate('clicked.engine'), engine)
            page.evaluate("_termsList[9].domain='wf'; poll()")
            self.assertEqual(page.locator('.loose-sessions .term-item').count(), 2)
            page.evaluate("_termsList[10].refs=['wf']; poll()")
            self.assertEqual(page.locator('.loose-sessions .term-item').count(), 1)
            page.evaluate("_termsList=[]; poll()")
            self.assertEqual(page.locator('#panel-list .term-item').count(), 0)
        self.assertEqual(self.errors, [])

    def test_main_engine_switch_keeps_existing_request_and_reload(self):
        self.install_engine_menu()
        requests = []

        def switch(route):
            requests.append((route.request.method, route.request.post_data_json))
            route.fulfill(content_type='application/json', body='{}')

        self.page.route('http://sidebar.test/workbench/engine', switch)
        self.page.add_script_tag(content='''
async function api(url, opts) { return (await fetch(url, opts)).json(); }
function rememberEngineView(engine) { localStorage.setItem('remembered-engine',engine); }
''')
        self.page.locator('#engine-pick').click()
        with self.page.expect_navigation():
            self.page.locator('#engine-menu .fm-row').filter(has_text='Codex').click()
        self.assertEqual(requests, [('PUT', {'engine': 'codex'})])
        self.assertEqual(self.page.evaluate("localStorage.getItem('remembered-engine')"), 'claude')
        self.assertEqual(self.errors, [])
