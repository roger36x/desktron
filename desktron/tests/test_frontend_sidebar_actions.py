"""Sidebar creation and chat actions, using production functions in isolated WebKit."""
from pathlib import Path
import re

import pytest

pytestmark = pytest.mark.desktop

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / 'frontend'
CSS = (FRONT / 'style.css').read_text(encoding='utf-8')


def fn(file, name):
    source = (FRONT / file).read_text(encoding='utf-8')
    match = re.search(r'(?:async )?function '+name+r'\([^\n]*\)[\s\S]*?\n}\n', source)
    assert match, name
    return match.group()


@pytest.fixture
def page():
    pw = pytest.importorskip('playwright.sync_api')
    with pw.sync_playwright() as p:
        browser = p.webkit.launch()
        page = browser.new_page()
        page.route('https://desk.test/**', lambda route: route.fulfill(
            content_type='text/html; charset=utf-8', body='''
            <meta charset="utf-8">
            <button id="fx-new"></button><button id="fx-apps"></button><button id="fx-tasks"></button>
            <div id="engine-menu" hidden></div><button id="side-search-btn"></button>
            <div id="side-search" hidden><input id="side-search-inp"></div>
            <div class="side-sec hov" id="proj-head"><span>工作流</span>
              <button id="proj-add">+</button><button id="proj-fold-all">⌄</button></div>
            <main id="panel-list"></main>'''))
        page.goto('https://desk.test/')
        page.add_style_tag(content=CSS)
        page.add_script_tag(content='''
const $=s=>document.querySelector(s), DESKTOP='_desktop'; let curEngine='pi', shepherdMode=false;
let _termsList=[], _sessionBucketsCache=null;
const hiddenDomains=[], wbState={sideProjOrder:[]};
const domains=[]; const ICONS={close:'<svg></svg>',enter:'<svg></svg>'}; const WF_MENU_ICONS={conversation:'<svg></svg>'};
const expandedProjects=new Set(); let persisted=0;
function persistExpanded(){persisted++;} function renderProjSessions(host){host.textContent='工作流会话';}
function openDraftChat(){selected.push(['draft']);}
let _panelGen=0; const nav={type:'domain',id:DESKTOP};
window.calls=[]; window.created=[]; window.selected=[]; window.alerts=[];
window.fail=false; window.hold=false; window.confirmResult=true; window.deleted=[];
window.confirm=text=>{window.confirmText=text;return confirmResult;};
window.alert=text=>alerts.push(text); function alertLike(text){alerts.push(text);}
async function api(path,opts={}) {
 calls.push({path,method:opts.method,body:opts.body});
 if(hold) await new Promise(resolve=>window.release=resolve);
 if(fail) throw Error('409 session_busy'); return {ok:true};
}
async function reloadDomains() {} function selectNav(type,id){selected.push([type,id]);}
async function newTerminal(engine,domain){created.push([engine,domain]);}
function renderTaskDock(){} function sideProjects(){return domains;}
function renderLooseSessions(host){host.textContent='聊天会话';} function makeReorderable(){} function refreshFoldCountsEnsure(){refreshFoldCounts();}
function startTermPoll(){} function _sideHighlight(){} function applySideFilter(){}
function disposeTermPane(id){deleted.push(id);document.getElementById('session-row')?.remove();}
async function refreshTerms(){} function _rerenderSessionsHost(){}
function termInfoNode(){return document.createElement('div');}
const RP_RENAME_ICO='',RP_MOVE_ICO='',RP_TRASH_ICO='';
function renameTermSession(){} function moveTermSession(){} function releaseTermResident(){}
function escapeHtml(s){const el=document.createElement('span');el.textContent=String(s);return el.innerHTML;}
''')
        for file,name in [('app-rail-nav.js','bindSidebarSectionToggle'),
                          ('app-rail-nav.js','applyOrder'),
                          ('app-events.js','sessionVisibleInCurrentMode'),
                          ('app-panel-terms.js','sessionBuckets'),
                          ('app-panel-terms.js','invalidateSessionBuckets'),
                          ('app-panel-terms.js','setSidebarFoldCount'),
                          ('app-panel-terms.js','refreshFoldCounts'),
                          ('app-panel-sidebar.js','sideProjects'),
                          ('app-assets-term-ui.js','placePop'),
                          ('app-assets-create.js','createWorkspaceByName'),
                          ('app-assets-create.js','openNewWorkspace'),
                          ('app-panel-terms.js','openCtxMenu'),
                          ('app-panel-sidebar.js','_openLooseChatMenu'),
                          ('app-panel-sidebar.js','renderSessionPanel'),
                          ('app-panel-sidebar.js','projectFold'),
                          ('app-panel-sidebar.js','closeTermSession'),
                          ('app-panel-terms.js','openTermCtxMenu')]:
            page.add_script_tag(content=fn(file,name))
        nav_source = (FRONT / 'app-rail-nav.js').read_text(encoding='utf-8')
        start = nav_source.index('(function initSidebar()')
        end = nav_source.index('\n})();', start) + len('\n})();')
        page.add_script_tag(content=nav_source[start:end])
        yield page
        browser.close()


@pytest.mark.parametrize('theme',['cyan','light'])
@pytest.mark.parametrize('width',[320,1280])
def test_workspace_popup_is_compact_without_import_or_heavy_shadow(page,theme,width):
    page.set_viewport_size({'width':width,'height':600})
    page.evaluate('(t)=>document.documentElement.dataset.theme=t',theme)
    page.evaluate('openNewWorkspace()')
    inp = page.locator('.nw-input')
    inp.wait_for()
    page.wait_for_function('document.activeElement.classList.contains("nw-input")')
    assert page.evaluate('calls') == []  # no orphan directory requests
    assert page.locator('.nw-orphans').count() == 0
    assert page.locator('.nw-pop').inner_text() == ''
    style = page.locator('.nw-pop').evaluate('(e)=>{let s=getComputedStyle(e);return [s.boxShadow,s.borderRadius]}')
    assert style == ['none','10px']
    assert inp.evaluate('(e)=>getComputedStyle(e).height') == '32px'
    box = page.locator('.nw-pop').bounding_box()
    assert box['x'] >= 8 and box['x']+box['width'] <= width-8
    inp.fill('测试工作流')
    inp.press('Enter')
    page.wait_for_function('selected.length===1')
    assert len(page.evaluate('calls')) == 1
    assert page.evaluate('calls[0].path') == '/workbench/domains'
    assert page.evaluate('JSON.parse(calls[0].body).name') == '测试工作流'
    assert page.locator('.nw-pop').count() == 0


def test_creation_guards_ime_and_double_enter_and_preserves_failed_draft(page):
    page.evaluate('openNewWorkspace();fail=true;')
    inp = page.locator('.nw-input')
    inp.fill('draft')
    inp.dispatch_event('keydown',{'key':'Enter','isComposing':True})
    assert page.evaluate('calls.length') == 0
    inp.press('Enter')
    page.wait_for_function('alerts.length===1')
    assert inp.input_value() == 'draft' and inp.is_enabled()
    assert page.evaluate('selected.length') == 0
    page.evaluate('fail=false;hold=true;')
    inp.press('Enter')
    page.wait_for_function('typeof release==="function"')
    inp.dispatch_event('keydown',{'key':'Enter'})
    assert page.evaluate('calls.length') == 2
    page.evaluate('hold=false;release()')
    page.wait_for_function('selected.length===1')
    assert page.evaluate('calls.length') == 2


@pytest.mark.parametrize('action',['escape','outside','drawer','toggle'])
def test_creation_popup_closes_and_can_reopen(page,action):
    page.evaluate('openNewWorkspace()')
    page.wait_for_function('document.activeElement.classList.contains("nw-input")')
    if action == 'escape':
        page.keyboard.press('Escape')
        assert page.locator('#proj-add').evaluate('(e)=>document.activeElement===e')
    elif action == 'outside':
        page.locator('#panel-list').dispatch_event('pointerdown')
    elif action == 'drawer':
        page.evaluate('dispatchEvent(new Event("cd-drawer-closed"))')
    else:
        page.evaluate('openNewWorkspace()')
    assert page.locator('.nw-pop').count() == 0
    page.evaluate('openNewWorkspace()')
    assert page.locator('.nw-pop').count() == 1


@pytest.mark.parametrize('entry',['button','contextmenu'])
@pytest.mark.parametrize('theme',['cyan','light'])
@pytest.mark.parametrize('width',[320,1280])
def test_chat_menu_only_creates_unassigned_session_using_current_engine(page,entry,theme,width):
    page.set_viewport_size({'width':width,'height':600})
    page.evaluate('(t)=>document.documentElement.dataset.theme=t',theme)
    page.evaluate('renderSessionPanel()')
    header = page.locator('.loose-head')
    assert header.locator('.sec-more').get_attribute('title') == '聊天操作'
    if entry == 'button':
        header.evaluate('(el)=>el.classList.add("hov")')
        header.locator('.sec-more').click()
    else:
        header.click(button='right',position={'x':20,'y':10})
    assert page.locator('.rp-ctx .rp-row').all_text_contents() == ['新建会话']
    assert page.locator('.rp-ctx').evaluate('(e)=>getComputedStyle(e).boxShadow') == 'none'
    assert page.evaluate('calls') == []
    box = page.locator('.rp-ctx').bounding_box()
    assert box['x'] >= 8 and box['x']+box['width'] <= width-8
    page.locator('.rp-row').click()
    assert page.evaluate('created') == [['pi','_desktop']]


@pytest.mark.parametrize('alive',[False,True])
def test_loose_chat_context_delete_is_permanent_even_when_process_alive(page,alive):
    page.evaluate('document.body.insertAdjacentHTML("beforeend",\'<div id="session-row">会话</div>\')')
    page.evaluate('(alive)=>openTermCtxMenu(30,80,{id:"adp_test",title:"会话",domain:DESKTOP,engine:"shepherd",alive},document.getElementById("session-row"),DESKTOP)',alive)
    page.locator('.rp-row').filter(has_text='删除会话').click()
    page.wait_for_function('deleted.length===1')
    assert page.evaluate('calls') == [{'path':'/workbench/terminals/adp_test','method':'DELETE','body':None}]
    assert '物理删除' in page.evaluate('confirmText')
    assert '不可恢复' in page.evaluate('confirmText')


def test_creation_and_chat_menus_replace_each_other_and_escape_closes(page):
    page.evaluate('openNewWorkspace()')
    page.wait_for_function('document.activeElement.classList.contains("nw-input")')
    page.evaluate('_openLooseChatMenu(40,60)')
    assert page.locator('.nw-pop').count() == 0
    assert page.locator('.rp-ctx').count() == 1
    page.keyboard.press('Escape')
    assert page.locator('.rp-ctx').count() == 0
    page.evaluate('_openLooseChatMenu(40,60);openNewWorkspace()')
    assert page.locator('.rp-ctx').count() == 1
    assert page.locator('.nw-pop').count() == 1
    page.keyboard.press('Escape')
    assert page.locator('.rp-ctx').count() == 0


def test_cancel_or_busy_delete_does_not_remove_chat_row(page):
    page.evaluate('document.body.insertAdjacentHTML("beforeend",\'<div id="session-row">会话</div>\');confirmResult=false;')
    page.evaluate('closeTermSession({id:"adp_test",title:"会话"},true)')
    assert page.evaluate('calls') == []
    page.evaluate('confirmResult=true;fail=true;closeTermSession({id:"adp_test",title:"会话"},true)')
    page.wait_for_function('alerts.length===1')
    assert page.locator('#session-row').count() == 1
    assert page.evaluate('deleted') == []
    assert '409' in page.evaluate('alerts[0]')


@pytest.mark.parametrize('group', ['projects', 'chat'])
@pytest.mark.parametrize('theme', ['cyan', 'light'])
@pytest.mark.parametrize('width', [320, 1280])
def test_group_header_text_blank_arrow_and_keyboard_toggle_once(page, group, theme, width):
    page.set_viewport_size({'width': width, 'height': 600})
    page.evaluate('(t)=>document.documentElement.dataset.theme=t', theme)
    page.evaluate('domains.push({id:"wf-test",name:"测试工作流"});localStorage.setItem("cd_loose_collapsed","0");renderSessionPanel()')
    header = page.locator('#proj-head' if group == 'projects' else '.loose-head')
    fold = header.locator('#proj-fold-all' if group == 'projects' else '.sec-fold:not(.sec-more)')
    storage = 'cd_projects_collapsed' if group == 'projects' else 'cd_loose_collapsed'

    def assert_open(expected):
        assert header.get_attribute('aria-expanded') == str(expected).lower()
        collapsed = page.locator('#panel-list').evaluate('(e)=>e.classList.contains("projects-collapsed")') if group == 'projects' else page.locator('.loose-sessions').evaluate('(e)=>e.hidden')
        assert collapsed == (not expected)
        assert page.evaluate('(key)=>localStorage.getItem(key)', storage) == ('0' if expected else '1')

    header.locator('span').first.click()
    assert_open(False)
    header.click(position={'x': header.bounding_box()['width'] / 2, 'y': 18})
    assert_open(True)
    header.evaluate('(e)=>e.classList.add("hov")')
    fold.click()
    assert_open(False)  # button click must not toggle twice via the parent
    header.focus()
    page.keyboard.press('Enter')
    assert_open(True)
    page.keyboard.press('Space')
    assert_open(False)
    page.evaluate('renderSessionPanel()')  # polling repaint preserves collapsed state
    assert_open(False)
    assert page.evaluate('selected') == []
    assert page.evaluate('calls') == []


def test_workflow_group_fold_preserves_child_expansion_and_chat_state(page):
    page.evaluate('domains.push({id:"wf-test",name:"测试工作流"});localStorage.setItem("cd_loose_collapsed","0");renderSessionPanel()')
    summary = page.locator('.proj-fold > summary')
    summary.locator('.pf-name').click()
    page.wait_for_function('expandedProjects.has("wf-test")')
    assert page.locator('.proj-fold').get_attribute('open') is not None
    page.locator('#proj-head > span').first.click()
    assert not summary.is_visible()
    assert page.locator('.loose-sessions').is_visible()
    page.locator('.loose-head > span').click()
    assert not page.locator('.loose-sessions').is_visible()
    page.locator('#proj-head').click(position={'x': 150, 'y': 18})
    assert summary.is_visible()
    assert page.locator('.proj-fold').get_attribute('open') is not None
    assert page.locator('.proj-sessions').inner_text() == '工作流会话'
    assert not page.locator('.loose-sessions').is_visible()
    page.evaluate('renderSessionPanel()')
    assert page.locator('.proj-fold').get_attribute('open') is not None
    assert page.evaluate('selected') == []


@pytest.mark.parametrize('theme', ['cyan', 'light'])
@pytest.mark.parametrize('width', [320, 390])
def test_mobile_workflow_fold_keeps_drawer_open_until_session_navigation(page, theme, width):
    page.set_viewport_size({'width': width, 'height': 700})
    page.evaluate('''(theme) => {
      document.documentElement.dataset.theme=theme;
      const panel=document.createElement('aside');panel.id='panel';
      panel.append(...document.body.children);
      const app=document.createElement('div');app.id='app';app.append(panel);
      document.body.append(app);
      const top=document.createElement('div');top.id='top-nav';
      top.innerHTML='<button id="tn-side">侧栏</button><button id="tn-back"></button><button id="tn-fwd"></button>';
      document.body.append(top);
      window.drawerClosed=0;window.addEventListener('cd-drawer-closed',()=>drawerClosed++);
      domains.push({id:'wf-test',name:'测试工作流'});renderSessionPanel();
    }''', theme)
    source = (FRONT / 'app-rail-topnav.js').read_text(encoding='utf-8')
    start = source.index('(function initTopNav()')
    end = source.index('\n})();', start) + len('\n})();')
    page.add_script_tag(content=source[start:end])
    page.locator('#tn-side').click()
    summary = page.locator('.proj-fold > summary')
    summary.wait_for(state='visible')
    for selector in ('.pf-name', '.pf-ico', None):
        target = summary.locator(selector) if selector else summary
        kwargs = {} if selector else {'position': {'x': 140, 'y': 16}}
        target.click(**kwargs)
        page.wait_for_function('expandedProjects.has("wf-test")')
        assert page.locator('.proj-fold').get_attribute('open') is not None
        assert page.evaluate('document.body.classList.contains("mo-drawer")')
        assert summary.is_visible()
        target.click(**kwargs)
        page.wait_for_function('!expandedProjects.has("wf-test")')
        assert page.locator('.proj-fold').get_attribute('open') is None
        assert page.evaluate('document.body.classList.contains("mo-drawer")')
    assert page.evaluate('drawerClosed') == 0
    assert page.evaluate('selected') == []
    summary.locator('.pf-name').click()
    page.wait_for_function('expandedProjects.has("wf-test")')
    page.evaluate('''() => {
      const row=document.createElement('div');row.className='term-item';
      row.innerHTML='<span class="ti-label">测试会话</span>';
      row.onclick=()=>selected.push(['session']);
      document.querySelector('.proj-sessions').append(row);
    }''')
    page.locator('.term-item .ti-label').click()
    assert not page.evaluate('document.body.classList.contains("mo-drawer")')
    assert page.evaluate('drawerClosed') == 1
    assert page.evaluate('selected') == [['session']]


def test_header_action_buttons_and_context_menu_do_not_collapse(page):
    page.evaluate('localStorage.setItem("cd_loose_collapsed","0");renderSessionPanel()')
    page.locator('#proj-add').click()
    assert page.locator('.nw-pop').count() == 1
    assert page.locator('#proj-head').get_attribute('aria-expanded') == 'true'
    page.keyboard.press('Escape')
    header = page.locator('.loose-head')
    header.evaluate('(e)=>e.classList.add("hov")')
    header.locator('.sec-more').click()
    assert header.get_attribute('aria-expanded') == 'true'
    page.keyboard.press('Escape')
    header.click(button='right', position={'x': 150, 'y': 18})
    assert header.get_attribute('aria-expanded') == 'true'
    assert page.locator('.rp-row').all_text_contents() == ['新建会话']


@pytest.mark.parametrize('theme', ['cyan', 'light'])
@pytest.mark.parametrize('width', [320, 1280])
def test_collapsed_counts_are_gray_capsules_next_to_titles_not_row_end(page, theme, width):
    page.set_viewport_size({'width': width, 'height': 600})
    page.evaluate('(t)=>document.documentElement.dataset.theme=t', theme)
    page.evaluate('''() => {
      domains.push({id:'wf-test',name:'测试工作流'}, {id:'hidden',name:'隐藏工作流'});
      hiddenDomains.push('hidden');
      _termsList = [
        ...Array.from({length:7},(_,i)=>({id:'wf'+i,engine:'pi',domain:'wf-test'})),
        ...Array.from({length:7},(_,i)=>({id:'chat'+i,engine:'pi',domain:DESKTOP,refs:i===0?['wf-test']:[]})),
        {id:'other',engine:'claude',domain:'wf-test'},
        {id:'task',engine:'pi',domain:'wf-test',task:true},
        {id:'shell',engine:'shell',domain:DESKTOP}
      ];
      renderSessionPanel();
    }''')
    page.locator('#proj-head > span').first.click()  # collapse the workflow group
    assert page.locator('#proj-head > span').first.inner_text() == '工作流'
    assert page.locator('#proj-head > .sidebar-fold-count').inner_text() == '1'
    assert page.locator('.loose-head > .sidebar-fold-count').inner_text() == '6'

    def assert_compact_optical_alignment(head, label):
        title_box = head.locator(label).bounding_box()
        badge_box = head.locator('.sidebar-fold-count').bounding_box()
        # Match the sidebar controls' optical center, not just the taller text line box.
        title_center = title_box['y'] + title_box['height'] / 2
        badge_center = badge_box['y'] + badge_box['height'] / 2
        assert badge_center - title_center == pytest.approx(1.5, abs=.1)
        assert badge_box['height'] == 14
        assert badge_box['width'] == 18

    assert_compact_optical_alignment(page.locator('#proj-head'), ':scope > span:first-child')
    page.locator('#proj-head > .sidebar-fold-count').click()  # same action as title
    assert not page.locator('#proj-head > .sidebar-fold-count').is_visible()
    summary = page.locator('.proj-fold > summary')
    badge = summary.locator('.sidebar-fold-count')
    assert badge.inner_text() == '8'  # all owned + referenced sessions, not only first five
    for head, label in [(page.locator('.loose-head'), ':scope > span:first-child'), (summary, '.pf-name')]:
        capsule = head.locator('.sidebar-fold-count')
        title_box = head.locator(label).bounding_box()
        badge_box = capsule.bounding_box()
        assert 0 <= badge_box['x'] - title_box['x'] - title_box['width'] <= 7
        style = capsule.evaluate('(e)=>{let s=getComputedStyle(e);return [s.height,s.borderRadius,s.fontWeight,s.backgroundColor,s.color]}')
        assert style[:3] == ['14px', '999px', '400']
        assert_compact_optical_alignment(head, label)
        assert style[3:] == (['rgb(232, 232, 235)', 'rgb(104, 104, 111)'] if theme == 'light' else ['rgba(255, 255, 255, 0.11)', 'rgb(213, 213, 216)'])
    badge.click()
    assert page.locator('.proj-fold').get_attribute('open') is not None
    assert not badge.is_visible()
    summary.locator('.pf-name').click()
    assert badge.is_visible()
    page.evaluate('_termsList=_termsList.filter(t=>t.id!=="wf0");invalidateSessionBuckets();refreshFoldCounts()')
    assert badge.inner_text() == '7'
    page.evaluate('renderSessionPanel()')
    assert page.locator('.proj-fold > summary > .sidebar-fold-count').inner_text() == '7'
    assert page.evaluate('selected') == []


def test_empty_groups_hide_counts_and_long_names_leave_room_for_capsule(page):
    page.set_viewport_size({'width':320,'height':600})
    page.evaluate('renderSessionPanel()')
    assert page.locator('.sidebar-fold-count').count() == 0
    page.evaluate('domains.push({id:"long",name:"非常长的工作流名称".repeat(15)});_termsList=Array.from({length:123},(_,i)=>({id:String(i),engine:"pi",domain:"long"}));renderSessionPanel()')
    badge = page.locator('.proj-fold > summary > .sidebar-fold-count')
    assert badge.inner_text() == '123'
    label = page.locator('.proj-fold .pf-name')
    assert label.evaluate('(e)=>e.scrollWidth>e.clientWidth')
    assert badge.bounding_box()['x'] + badge.bounding_box()['width'] <= 320
    page.evaluate('_termsList=[];invalidateSessionBuckets();refreshFoldCounts()')
    assert badge.count() == 0
    assert page.locator('.loose-head > .sidebar-fold-count').count() == 0
