"""生产模型菜单：十行限高、固定参数、视口收缩与二级菜单。"""
from pathlib import Path
import json
import re
import shutil
import subprocess
import unittest
import pytest

from frontend_i18n_js import BROWSER_SCRIPT, NODE_PRELUDE

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[2]


def function(path, name):
    source = (ROOT / "frontend" / path).read_text(encoding='utf-8')
    return re.search(r"(?:async )?function " + name + r"\(.*?\n}\n", source, re.S).group()


@unittest.skipUnless(sync_playwright, "需要 Playwright WebKit")
@pytest.mark.desktop
class ModelMenuLimit(unittest.TestCase):
    def setUp(self):
        self.pw = sync_playwright().start()
        self.browser = self.pw.webkit.launch()
        self.page = self.browser.new_page(viewport={"width": 1000, "height": 800})
        self.page.set_content('<button id="anchor" style="position:fixed;bottom:12px;right:12px">模型</button>')
        self.page.add_style_tag(content=(ROOT / "frontend/style.css").read_text(encoding='utf-8'))
        self.page.add_script_tag(content=BROWSER_SCRIPT + function("app-chat-opts.js", "hsOptLabel") + r'''
const RP_CHECK='•', RP_CHEV='›';
function escapeHtml(s){return String(s);}
const v={opts:{model:'model-20'}};
let picked=null;
function hsUI(){return {};}
function hsModel(){return {id:v.opts.model};}
function hsModelRoute(){return hsModel();}
function hsModelRoutePatch(model){return {model:model.id};}
function hsVariants(){return [];}
function hsModels(){return Array.from({length:40},(_,i)=>({id:'model-'+i,label:'Model '+i+(i===20?' · 负载 8% · 额度 10%':'')}));}
function hsAutocompacts(){return [{id:'',label:'自动'}];}
function hsEfforts(){return [{id:'high',label:'High'}];}
function hsEffortDefault(){return 'high';}
function hsSetOpt(v,o){picked=o;}
function hsEngineLabel(){return 'Pi Coding';}
function hsShepherdEngines(){return [{id:'pi',label:'Pi Coding',available:true},{id:'claude',label:'Claude',available:true}];}
function hsSwitchShepherdEngine(v,engine){v.engine=engine;}
function hsPickEffort(){openCtxMenu(20,20,[{label:'High',fn:()=>{}}],'rp-wide');}
function hsPickAutocompact(){}
''' + function("app-panel-terms.js", "openCtxMenu")
            + 'openCtxMenu.anchorNext = el => { openCtxMenu._anchor = el || null; };'
            + function("app-chat-opts.js", "hsMenuAnchor")
            + function("app-chat-opts.js", "hsPickShepherdEngine")
            + function("app-chat-opts.js", "hsRefreshDynamicModels")
            + function("app-chat-opts.js", "hsModelItem")
            + function("app-chat-opts.js", "hsModelsByProvider")
            + function("app-chat-opts.js", "hsShowModelMenu")
            + function("app-chat-opts.js", "hsPickModel"))

    def tearDown(self):
        self.browser.close()
        self.pw.stop()

    def open(self):
        self.page.evaluate("hsPickModel(v,document.querySelector('#anchor'))")

    def test_ten_rows_selected_visible_and_last_model_selectable(self):
        self.open()
        state = self.page.evaluate('''() => {
          const m=document.querySelector('.rp-model-list'), r=m.getBoundingClientRect();
          const selected=m.children[20].getBoundingClientRect();
          return {height:r.height,row:m.children[0].getBoundingClientRect().height,
            scroll:m.scrollHeight>m.clientHeight,
            selected:selected.top>=r.top && selected.bottom<=r.bottom};
        }''')
        self.assertAlmostEqual(state['height'], state['row'] * 10, delta=1)
        self.assertTrue(state['scroll'])
        self.assertTrue(state['selected'])
        selected = self.page.locator('.rp-model-list .rp-row').nth(20)
        self.assertEqual(selected.locator('.rp-title').inner_text(), 'Model 20 · 负载 8% · 额度 10%')
        self.assertEqual(selected.locator('.rp-value').count(), 0)
        before = self.page.locator('.rp-model > .rp-row').first.bounding_box()
        self.page.evaluate("document.querySelector('.rp-model-list').scrollTop=100000")
        after = self.page.locator('.rp-model > .rp-row').first.bounding_box()
        self.assertEqual(before, after, '滚动模型不能移动固定参数区')
        self.assertEqual(self.page.evaluate("document.querySelector('.rp-model').scrollTop"), 0)
        self.page.locator('.rp-model .rp-row').nth(39).scroll_into_view_if_needed()
        self.page.locator('.rp-model .rp-row').nth(39).click()
        self.assertEqual(self.page.evaluate('picked.model'), 'model-39')
        self.assertEqual(self.page.locator('.rp-ctx').count(), 0)

    def test_small_viewport_resize_and_effort(self):
        self.open()
        self.page.set_viewport_size({'width':320,'height':240})
        self.page.wait_for_timeout(300)
        r = self.page.locator('.rp-model').bounding_box()
        self.assertGreaterEqual(r['x'], 7)
        self.assertGreaterEqual(r['y'], 7)
        self.assertLessEqual(r['x']+r['width'],313)
        self.assertLessEqual(r['y']+r['height'],233)
        # 无须滚到列表底部，Effort 与压缩窗口始终位于菜单内。
        for row in self.page.locator('.rp-model > .rp-row').all():
            box = row.bounding_box()
            self.assertGreaterEqual(box['y'], r['y'])
            self.assertLessEqual(box['y']+box['height'], r['y']+r['height'])
        self.page.locator('.rp-model .rp-row').nth(40).click()
        self.assertEqual(self.page.locator('.rp-ctx').inner_text(), 'High')
        self.assertLess(self.page.locator('.rp-ctx').bounding_box()['height'],80)

    def test_short_list_not_stretched(self):
        self.page.evaluate("hsModels=()=>[{id:'model-20',label:'Current'}]")
        self.open()
        self.assertEqual(self.page.locator('.rp-model .rp-row').count(),3)
        self.assertLess(self.page.locator('.rp-model').bounding_box()['height'],130)

    def dynamic(self):
        self.page.evaluate('''() => {
          v.engine='pi';v.sid='one';
          v.catalog={models:hsModels().map(m=>({...m,family:'example-gw/test'}))};
          hsModels=()=>v.catalog.models;
          window.refreshes=[];
          window.api=()=>new Promise(resolve=>refreshes.push(resolve));
        }''')

    def test_overlapping_pi_refreshes_keep_nodes_scroll_and_anchor(self):
        self.dynamic()
        self.page.evaluate('''() => {
          const anchor=document.querySelector('#anchor');
          window.firstOpen=hsPickModel(v,anchor);window.secondOpen=hsPickModel(v,anchor);
          window.menuBefore=document.querySelector('.rp-model');
          window.rowBefore=document.querySelector('.rp-model-list').children[20];
          document.querySelector('.rp-model-list').scrollTop=700;
          window.scrollBefore=document.querySelector('.rp-model-list').scrollTop;
        }''')
        self.assertEqual(self.page.locator('.rp-model-list .rp-row').count(),40)
        self.assertEqual(self.page.evaluate('refreshes.length'),1)
        self.page.evaluate('''async () => {
          refreshes[0]({engine:'pi',options:{models:v.catalog.models.map(m=>({...m,label:m.label+' · 负载 12%'}))}});
          await Promise.all([firstOpen,secondOpen]);
        }''')
        self.assertTrue(self.page.evaluate('''() =>
          menuBefore===document.querySelector('.rp-model') &&
          rowBefore===document.querySelector('.rp-model-list').children[20] &&
          scrollBefore===document.querySelector('.rp-model-list').scrollTop
        '''))
        self.assertIn('负载 12%',self.page.locator('.rp-model-list .rp-row').nth(20).inner_text())
        menu=self.page.locator('.rp-model').bounding_box()
        anchor=self.page.locator('#anchor').bounding_box()
        self.assertAlmostEqual(anchor['y']-menu['y']-menu['height'],6,delta=1)

    def test_slow_refresh_does_not_delay_selection_or_reopen_closed_menu(self):
        self.dynamic()
        self.page.evaluate("() => { window.pending=hsPickModel(v,document.querySelector('#anchor')); }")
        self.page.locator('.rp-model-list .rp-row').nth(20).click()
        self.assertEqual(self.page.evaluate('picked.model'),'model-20')
        self.assertEqual(self.page.locator('.rp-ctx').count(),0)
        self.page.evaluate("async()=>{refreshes[0]({engine:'pi',options:v.catalog});await pending;}")
        self.assertEqual(self.page.locator('.rp-ctx').count(),0)

    def test_late_refresh_does_not_replace_effort_submenu(self):
        self.dynamic()
        self.page.evaluate("() => { window.pending=hsPickModel(v,document.querySelector('#anchor')); }")
        self.page.locator('.rp-model > .rp-row').first.click()
        self.assertEqual(self.page.locator('.rp-ctx').inner_text(),'High')
        self.page.evaluate("async()=>{refreshes[0]({engine:'pi',options:v.catalog});await pending;}")
        self.assertEqual(self.page.locator('.rp-ctx').inner_text(),'High')

    def test_empty_catalog_displays_loading_before_network_reply(self):
        self.dynamic()
        self.page.evaluate("() => { v.catalog.models=[];window.pending=hsPickModel(v,document.querySelector('#anchor')); }")
        self.assertIn('正在加载模型',self.page.locator('.rp-model').inner_text())
        self.page.evaluate("async()=>{refreshes[0]({engine:'pi',options:{models:[{id:'new',label:'New'}]}});await pending;}")
        self.assertEqual(self.page.locator('.rp-model-list .rp-row').inner_text(),'New')

    def test_scrollbar_container_reaches_right_inner_border(self):
        self.open()
        geometry = self.page.evaluate('''() => {
          const menu=document.querySelector('.rp-model'), list=menu.querySelector('.rp-model-list');
          const border=parseFloat(getComputedStyle(menu).borderRightWidth);
          return {gap:menu.getBoundingClientRect().right-list.getBoundingClientRect().right,
            border, padding:parseFloat(getComputedStyle(list).paddingRight)};
        }''')
        self.assertAlmostEqual(geometry['gap'],geometry['border'],delta=0.5)
        self.assertEqual(geometry['padding'],3)

    def test_engine_submenu_stays_at_composer(self):
        self.page.evaluate("v.engine='pi'; v.shepherdMode=true")
        self.open()
        self.page.locator('.rp-model > .rp-row').first.click()
        menu = self.page.locator('.rp-ctx').bounding_box()
        anchor = self.page.locator('#anchor').bounding_box()
        self.assertAlmostEqual(anchor['y'] - menu['y'] - menu['height'], 6, delta=1)
        self.page.locator('.rp-ctx .rp-row').nth(1).click()
        self.assertEqual(self.page.evaluate('v.engine'), 'claude')

    def grouped(self):
        self.page.evaluate('''() => {
          v.engine='pi';
          v.opts.model='openai/model-15';
          hsModels=()=>['deepseek','openai','third'].flatMap(provider=>
            Array.from({length:25},(_,i)=>({id:provider+'/model-'+i,
              label:provider+' Model '+i,provider,
              providerLabel:{deepseek:'DeepSeek',openai:'OpenAI',third:'Third'}[provider]})));
        }''')

    def test_provider_sticky_title_is_bounded_to_its_group(self):
        self.grouped()
        self.open()
        state = self.page.evaluate('''() => {
          const list=document.querySelector('.rp-model-list'), groups=list.children;
          const head=groups[1].firstElementChild;
          list.scrollTop=groups[1].offsetTop-list.offsetTop+180;
          const top=list.getBoundingClientRect().top;
          return {position:getComputedStyle(head).position,
            head:head.getBoundingClientRect().top-top,
            oldBottom:groups[0].firstElementChild.getBoundingClientRect().bottom-top,
            title:document.elementFromPoint(list.getBoundingClientRect().left+25,top+10).textContent};
        }''')
        self.assertEqual(state['position'],'sticky', '带分隔线的第二组标题也必须吸顶')
        self.assertAlmostEqual(state['head'],0,delta=1)
        self.assertLessEqual(state['oldBottom'],0, 'DeepSeek 标题不能覆盖 OpenAI 模型')
        self.assertEqual(state['title'],'OpenAI')
        # 下一组尚未完全进入时，旧标题应被该组边界推走，而不是相互叠压。
        state = self.page.evaluate('''() => {
          const list=document.querySelector('.rp-model-list'), group=list.children[2];
          list.scrollTop=group.offsetTop-list.offsetTop-10;
          return {oldBottom:list.children[1].firstElementChild.getBoundingClientRect().bottom,
            nextTop:group.getBoundingClientRect().top};
        }''')
        self.assertLessEqual(state['oldBottom'],state['nextTop']+1)
        self.page.evaluate('''() => {
          const list=document.querySelector('.rp-model-list');
          list.scrollTop=list.children[2].offsetTop-list.offsetTop+100;
        }''')
        state = self.page.evaluate('''() => {
          const list=document.querySelector('.rp-model-list');
          return document.elementFromPoint(list.getBoundingClientRect().left+25,
            list.getBoundingClientRect().top+10).textContent;
        }''')
        self.assertEqual(state,'Third')

    def assert_provider_titles_are_undecorated(self, theme, mobile=False):
        self.page.evaluate('(theme) => document.documentElement.dataset.theme=theme', theme)
        if mobile:
            self.page.set_viewport_size({'width':390,'height':844})
        self.grouped()
        self.open()
        # 第一组无分隔线；后续组有分隔线，两类标题都不能出现底板或选中块。
        for index in (0, 1):
            header = self.page.locator('.rp-model-group-toggle').nth(index)
            header.scroll_into_view_if_needed()
            # WebKit 后台实例不保证启用 :hover/:active；样式规则另有静态回归。
            header.hover()
            for phase in ('hover', 'active', 'collapsed', 'keyboard'):
                if phase == 'active':
                    self.page.mouse.down()
                elif phase == 'collapsed':
                    self.page.mouse.up()
                    self.assertEqual(header.get_attribute('aria-expanded'), 'false')
                elif phase == 'keyboard':
                    header.focus()
                    header.press('Space')
                    self.assertEqual(header.get_attribute('aria-expanded'), 'true')
                style = header.evaluate('''el => {
                  const s=getComputedStyle(el);
                  return {background:s.backgroundColor,image:s.backgroundImage,
                    shadow:s.boxShadow,textShadow:s.textShadow,radius:s.borderRadius,
                    outline:s.outlineStyle,position:s.position};
                }''')
                self.assertEqual(style, {
                    'background':'rgba(0, 0, 0, 0)', 'image':'none', 'shadow':'none',
                    'textShadow':'none', 'radius':'0px', 'outline':'none',
                    'position':'sticky'}, (theme, mobile, index, phase))
        # 仅服务商标题取消装饰；当前模型标记仍保留。
        model = self.page.locator('.rp-model-group').nth(1).locator('.rp-row:not(.rp-ctx-head)').nth(15)
        self.assertEqual(model.locator('.rp-check').inner_text(), '•')

    def test_provider_titles_are_plain_in_desktop_dark_and_light(self):
        for theme in ('dark', 'light'):
            with self.subTest(theme=theme):
                self.assert_provider_titles_are_undecorated(theme)
                self.page.evaluate('openCtxMenu.close()')

    def test_provider_titles_are_plain_in_phone_dark_and_light(self):
        for theme in ('dark', 'light'):
            with self.subTest(theme=theme):
                self.assert_provider_titles_are_undecorated(theme, mobile=True)
                self.page.evaluate('openCtxMenu.close()')

    def test_click_provider_title_folds_only_its_models_without_closing_menu(self):
        self.grouped()
        self.open()
        header=self.page.locator('.rp-model-group-toggle').nth(1)
        self.assertEqual(header.inner_text(),'OpenAI')
        self.assertEqual(header.locator('.rp-chev,.rp-check').count(),0)
        self.page.evaluate("window.originalMenu=document.querySelector('.rp-model')")
        header.click()
        self.assertTrue(self.page.evaluate("originalMenu===document.querySelector('.rp-model')"))
        self.assertEqual(header.get_attribute('aria-expanded'),'false')
        self.assertEqual(self.page.locator('.rp-model-group').nth(1).locator('.rp-row:not(.rp-ctx-head):visible').count(),0)
        self.assertEqual(self.page.locator('.rp-model-group').nth(0).locator('.rp-row:not(.rp-ctx-head):visible').count(),25)
        self.assertIsNone(self.page.evaluate('picked'))
        header.click()
        self.assertEqual(header.get_attribute('aria-expanded'),'true')
        self.assertEqual(self.page.locator('.rp-model-group').nth(1).locator('.rp-row:not(.rp-ctx-head):visible').count(),25)
        self.page.locator('.rp-model-group').nth(1).locator('.rp-row').nth(3).click()
        self.assertEqual(self.page.evaluate('picked.model'),'openai/model-2')
        self.assertEqual(self.page.locator('.rp-ctx').count(),0)

    def test_keyboard_fold_and_reopen_preserves_current_group_state(self):
        self.grouped()
        self.open()
        header=self.page.locator('.rp-model-group-toggle').nth(1)
        header.focus()
        header.press('Enter')
        self.assertEqual(header.get_attribute('aria-expanded'),'false')
        self.page.evaluate('openCtxMenu.close()')
        self.open()
        header=self.page.locator('.rp-model-group-toggle').nth(1)
        self.assertEqual(header.get_attribute('aria-expanded'),'false')
        self.assertTrue(header.is_visible())
        header.focus()
        header.press('Space')
        self.assertEqual(header.get_attribute('aria-expanded'),'true')
        # 同名 provider 在其他引擎的折叠状态不互相影响。
        header.press('Enter')
        self.page.evaluate("v.engine='other'")
        self.open()
        self.assertEqual(self.page.locator('.rp-model-group-toggle').nth(1).get_attribute('aria-expanded'),'true')

    def test_menu_escape_closes_before_busy_session_capture_and_restores_focus(self):
        source=(ROOT / 'frontend/app-chat-page.js').read_text(encoding='utf-8')
        doc_key=re.search(r'  v\.docKey = \(ev\) => \{.*?\n  };',source,re.S).group()
        self.page.add_script_tag(content='''
          v.el=document.createElement('div');document.body.appendChild(v.el);
          v.inp=document.createElement('input');v.el.appendChild(v.inp);
          const pActive={el:v.el};v.busy=true;
          window.interrupts=0;
          function hsEscape(){interrupts++;}
        '''+doc_key+'\ndocument.addEventListener("keydown",v.docKey,true);')
        self.grouped()
        self.open()
        header=self.page.locator('.rp-model-group-toggle').nth(1)
        header.focus()
        header.press('Enter')
        header.press('Escape')
        self.assertEqual(self.page.locator('.rp-ctx').count(),0)
        self.assertEqual(self.page.evaluate('interrupts'),0)
        self.assertTrue(self.page.evaluate("document.activeElement===document.querySelector('#anchor')"))
        # 关闭后必须卸载菜单捕获监听，不能永久吞掉会话 Escape。
        self.page.locator('#anchor').press('Escape')
        self.assertEqual(self.page.evaluate('interrupts'),1)

    def test_all_groups_folded_fit_small_viewport_and_fixed_settings_remain(self):
        self.grouped()
        self.open()
        for i in range(3):
            self.page.locator('.rp-model-group-toggle').nth(i).click()
        self.assertEqual(self.page.locator('.rp-model-list .rp-row:not(.rp-ctx-head):visible').count(),0)
        self.assertEqual(self.page.locator('.rp-model-group-toggle:visible').count(),3)
        self.page.set_viewport_size({'width':320,'height':240})
        self.page.wait_for_timeout(300)
        box=self.page.locator('.rp-model').bounding_box()
        self.assertLessEqual(box['y']+box['height'],233)
        self.assertGreaterEqual(box['y'],7)
        self.assertEqual(self.page.locator('.rp-model > .rp-row:visible').count(),2)
        self.page.locator('.rp-model > .rp-row').first.click()
        self.assertEqual(self.page.locator('.rp-ctx').inner_text(),'High')

    def test_folded_group_async_labels_update_without_expanding_or_replacing_nodes(self):
        self.grouped()
        self.open()
        self.page.evaluate('''() => {
          openCtxMenu.close();
          window.menuHandle=hsShowModelMenu(v,document.querySelector('#anchor'));
          window.savedGroup=document.querySelectorAll('.rp-model-group')[1];
          window.savedRow=savedGroup.children[5];
        }''')
        self.page.locator('.rp-model-group-toggle').nth(1).click()
        self.page.evaluate("menuHandle.setLabels(new Map([['openai/model-4','Updated · 负载 12%']]))")
        self.assertTrue(self.page.evaluate("savedGroup===document.querySelectorAll('.rp-model-group')[1] && savedRow===savedGroup.children[5]"))
        self.assertEqual(self.page.locator('.rp-model-group-toggle').nth(1).get_attribute('aria-expanded'),'false')
        self.page.locator('.rp-model-group-toggle').nth(1).click()
        self.assertEqual(self.page.evaluate('savedRow.querySelector(".rp-title").textContent'),'Updated · 负载 12%')

    def test_only_models_scroll_with_three_fixed_settings(self):
        self.page.evaluate("hsUI=()=>({ultracode:true})")
        self.open()
        self.assertEqual(self.page.locator('.rp-model-list .rp-row').count(),40)
        self.assertEqual(self.page.locator('.rp-model > .rp-row').count(),3)
        self.page.set_viewport_size({'width':390,'height':300})
        self.page.wait_for_timeout(300)
        footer = [row.bounding_box() for row in self.page.locator('.rp-model > .rp-row').all()]
        self.page.evaluate("document.querySelector('.rp-model-list').scrollTop=100000")
        self.assertEqual(footer, [row.bounding_box() for row in self.page.locator('.rp-model > .rp-row').all()])
        self.assertLessEqual(footer[-1]['y']+footer[-1]['height'],293)



@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class ModelMenuProviderGroups(unittest.TestCase):
    """Pi 同时登录多个服务商时，模型按服务商分组，单一服务商保持平铺。"""

    def items(self, models):
        script = NODE_PRELUDE + function("app-chat-opts.js", "hsOptLabel") + r'''
const RP_CHECK='•', RP_CHEV='›';
let captured=null;
function openCtxMenu(x,y,items){captured=items;return {isOpen:()=>true,setLabels(){}};}
openCtxMenu.anchorNext=()=>{};
const v={opts:{model:'openai/gpt-6'}};
function hsUI(){return {};}
function hsModel(){return {id:v.opts.model};}
function hsModelRoute(){return null;}
function hsModelRoutePatch(m){return {model:m.id};}
function hsVariants(){return [];}
function hsAutocompacts(){return [];}
function hsEfforts(){return [];}
function hsSetOpt(){}
function hsMenuAnchor(){return {x:0,y:0};}
let MODELS=[];
function hsModels(){return MODELS;}
''' + function("app-chat-opts.js", "hsModelItem") + function("app-chat-opts.js", "hsModelsByProvider") \
            + function("app-chat-opts.js", "hsShowModelMenu") + r'''
MODELS = JSON.parse(process.argv[1]);
hsShowModelMenu(v, {getBoundingClientRect(){return {left:0,top:0,right:0,bottom:0};}});
console.log(JSON.stringify(captured.map(it => it.header ? '#' + it.header + (it.sep ? '|sep' : '') : it.label)));
'''
        out = subprocess.run(["node", "-e", script, json.dumps(models)],
                             capture_output=True, text=True, check=True, encoding='utf-8')
        return json.loads(out.stdout)

    def test_multiple_providers_get_headers_in_first_seen_order(self):
        models = [{"id": "deepseek/a", "label": "DS A", "provider": "deepseek", "providerLabel": "DeepSeek"},
                  {"id": "openai/gpt-6", "label": "GPT-6", "provider": "openai", "providerLabel": "OpenAI"},
                  {"id": "deepseek/b", "label": "DS B", "provider": "deepseek", "providerLabel": "DeepSeek"}]
        self.assertEqual(self.items(models), ["#DeepSeek", "DS A", "DS B", "#OpenAI|sep", "GPT-6"])

    def test_provider_title_style_has_no_plate_or_hover_selection(self):
        css = (ROOT / 'frontend/style.css').read_text(encoding='utf-8')
        sticky = re.search(r'\.rp-ctx\.rp-model \.rp-model-list \.rp-ctx-head\s*\{([^}]+)\}', css).group(1)
        self.assertIn('background: none;', sticky)
        self.assertIn('box-shadow: none;', sticky)
        self.assertIn('border-radius: 0;', sticky)
        hover = re.search(r'\.rp-ctx \.rp-ctx-head\.rp-model-group-toggle:hover\s*\{([^}]+)\}', css).group(1)
        self.assertIn('background: none;', hover)
        focus = re.search(r'\.rp-model-group-toggle:focus-visible\s*\{([^}]+)\}', css).group(1)
        self.assertIn('outline: none;', focus)
        self.assertIn('.rp-row:hover { background: var(--surface-hover); }', css)

    def test_single_provider_stays_flat(self):
        models = [{"id": "openai/a", "label": "A", "provider": "openai", "providerLabel": "OpenAI"},
                  {"id": "openai/b", "label": "B", "provider": "openai", "providerLabel": "OpenAI"}]
        self.assertEqual(self.items(models), ["A", "B"])


if __name__ == '__main__':
    unittest.main()
