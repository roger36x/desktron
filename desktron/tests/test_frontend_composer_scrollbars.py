"""Composer controls remain scrollable without native or custom scrollbar chrome."""
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / 'frontend/app-init.js').read_text(encoding='utf-8')
CSS = (ROOT / 'frontend/style.css').read_text(encoding='utf-8')
SCROLLBARS = re.search(r'\(function autoScrollbars\(\) \{.*?\n\}\)\(\);', SOURCE, re.S).group()


def test_composer_controls_opt_out_before_overflow_auto_detection():
    function = re.search(r'  function scrollable\(key\) \{.*?\n  }', SCROLLBARS, re.S).group()
    script = r'''
const assert = require('node:assert/strict');
const SEL = '.hs-body, .rp-model-list';
let computed = 0;
const getComputedStyle = () => { computed++; return {overflowX:'auto', overflowY:'hidden'}; };
const key = cls => ({closest: () => null,
  matches: selector => selector.split(',').some(s => s.trim() === '.'+cls)});
'''+function+r'''
for (const cls of ['hs-row', 'frw-composer-row']) {
  assert.equal(scrollable(key(cls)), false);
  assert.equal(computed, 0, 'opt out must precede automatic overflow registration');
}
assert.equal(scrollable(key('hs-body')), true);
assert.equal(scrollable(key('other-horizontal')), true, 'other horizontal containers still register');
'''
    subprocess.run(['node', '-'], input=script, check=True, capture_output=True, text=True, encoding='utf-8')


def test_mobile_input_horizontal_overlay_is_suppressed_even_with_editing_overflow():
    layout = re.search(r'  function layoutHorizontal\(t, g\) \{.*?\n  }', SCROLLBARS, re.S).group()
    script = r'''
const assert = require('node:assert/strict');
let mobile = true, created = 0;
global.window = {matchMedia: query => {
  assert.equal(query, '(max-width: 820px), (max-height: 520px) and (pointer: coarse)');
  return {matches:mobile};
}};
global.document = {createElement: () => {created++; throw Error('unexpected horizontal overlay');}};
const el = {scrollWidth:900,clientWidth:300,scrollLeft:12,
  scrollHeight:600,clientHeight:100,scrollTop:40,
  matches: selectors => selectors.includes('.'+el.cls)};
const g = {el,width:300,visible:true};
let removed=0;
const t={_x:{classList:{remove: cls=>{assert.equal(cls,'sb-show');removed++;}}}};
'''+layout+r'''
for (el.cls of ['hs-inp','frw-input']) {
  assert.equal(layoutHorizontal(t,g),false);
}
assert.equal(created,0);assert.equal(removed,2,'existing horizontal overlays are hidden');
assert.equal(el.scrollTop,40);assert.equal(el.scrollLeft,12,'do not alter selection or scroll offsets');
// Desktop inputs and unrelated mobile code/table containers still take the existing layout path.
for (const [isMobile,cls] of [[false,'hs-inp'],[true,'code-output']]) {
  mobile=isMobile;el.cls=cls;
  assert.throws(()=>layoutHorizontal({},g),/unexpected horizontal overlay/);
}
assert.equal(created,2);
'''
    subprocess.run(['node', '-'], input=script, check=True, capture_output=True, text=True, encoding='utf-8')


@pytest.fixture
def page():
    pw_module = pytest.importorskip('playwright.sync_api')
    with pw_module.sync_playwright() as pw:
        browser = pw.webkit.launch()
        page = browser.new_page(viewport={'width': 1100, 'height': 800})
        page.set_content('<main class="hs-page" style="position:fixed;inset:0;display:block"></main>')
        page.add_style_tag(content=CSS)
        page.add_script_tag(content=SCROLLBARS)
        yield page
        browser.close()


@pytest.mark.desktop
@pytest.mark.parametrize('input_class', ['hs-inp', 'frw-input'])
def test_mobile_editing_overflow_has_no_horizontal_thumb_but_keeps_vertical_scroll(page, input_class):
    page.set_viewport_size({'width': 390, 'height': 844})
    page.evaluate('''cls => {
      document.querySelector('main').innerHTML='<div class="hs-box" style="width:300px;margin:30px">'
        +'<div class="'+cls+'" contenteditable="true" style="height:80px;max-height:80px">'
        +'<div style="width:1000px;height:600px">合成输入草稿</div></div></div>';
    }''', input_class)
    inp = page.locator('.'+input_class)
    inp.hover()
    inp.evaluate('node=>node.dispatchEvent(new CustomEvent("desk-scrollbar-refresh",{bubbles:true}))')
    page.wait_for_function('''cls=>[...document.querySelectorAll('.sb-thumb:not(.sb-horizontal)')]
      .some(t=>t._key===document.querySelector('.'+cls))''', arg=input_class)
    assert inp.evaluate('node=>node.scrollWidth>node.clientWidth+1')
    assert inp.evaluate('node=>getComputedStyle(node).overflowX') == 'hidden'
    assert not page.evaluate('''cls=>[...document.querySelectorAll('.sb-horizontal.sb-show')]
      .some(t=>t._key===document.querySelector('.'+cls))''', input_class)
    inp.evaluate('node=>{node.scrollTop=60;node.dispatchEvent(new Event("scroll",{bubbles:true}));}')
    assert inp.evaluate('node=>node.scrollTop') == 60
    assert inp.inner_text() == '合成输入草稿'
    assert page.evaluate('''cls=>[...document.querySelectorAll('.sb-thumb.sb-show:not(.sb-horizontal)')]
      .some(t=>t._key===document.querySelector('.'+cls))''', input_class)


@pytest.mark.desktop
@pytest.mark.parametrize('theme,row_class', [('dark', 'hs-row'), ('light', 'hs-row'),
                                            ('dark', 'frw-composer-row'), ('light', 'frw-composer-row')])
def test_controls_no_scrollbar_under_overflow_hover_and_resize(page, theme, row_class):
    page.evaluate('''({theme,rowClass}) => {
      document.documentElement.dataset.theme=theme;
      const host=document.querySelector('main');
      host.innerHTML='<div class="hs-box" style="width:340px;margin:30px">'
        +'<div class="hs-inp" contenteditable="true">示例草稿</div>'
        +'<div class="'+rowClass+'">'
        +'<button class="hs-clip" data-tip="附件" style="flex:0 0 auto">附件</button>'
        +'<span class="hs-mode" style="flex:0 0 auto">bypass permissions mode</span>'
        +'<span class="hs-model" style="flex:0 0 auto"><span class="hs-m1">Pi Coding Synthetic Model</span></span>'
        +'<button class="hs-send" data-tip="发送">↑</button></div></div>';
    }''', {'theme': theme, 'rowClass': row_class})
    row = page.locator('.'+row_class)
    page.wait_for_function('(cls)=>document.querySelector("."+cls).scrollWidth>document.querySelector("."+cls).clientWidth+1', arg=row_class)
    row.hover()
    row.evaluate('node => node.dispatchEvent(new CustomEvent("desk-scrollbar-refresh", {bubbles:true}))')
    page.wait_for_timeout(80)
    assert row.evaluate('node=>getComputedStyle(node).overflowX') == 'auto'
    assert row.evaluate('node=>getComputedStyle(node).scrollbarWidth') == 'none'
    assert row.evaluate('node=>getComputedStyle(node,"::-webkit-scrollbar").display') == 'none'
    assert not page.evaluate('''cls=>[...document.querySelectorAll('.sb-thumb')]
        .some(t=>t._key===document.querySelector('.'+cls))''', row_class)
    # Native wheel delivery still reaches the overflowing controls; scrollbar removal is visual only.
    page.mouse.wheel(160, 0)
    page.wait_for_function('(cls)=>document.querySelector("."+cls).scrollLeft>0', arg=row_class)
    assert page.locator('.hs-inp').inner_text() == '示例草稿'
    send = row.locator('.hs-send').bounding_box()
    bounds = row.bounding_box()
    assert send['x'] >= bounds['x']-1 and send['x']+send['width'] <= bounds['x']+bounds['width']+1
    # Dynamic content, mobile sizing, and widening must never recreate an overlay for the row.
    page.set_viewport_size({'width': 390, 'height': 844})
    row.evaluate('node=>{const span=document.createElement("span"); span.textContent="追加选项"; node.appendChild(span);}')
    page.locator('.hs-box').evaluate('node=>node.style.width="300px"')
    page.wait_for_timeout(100)
    row.hover()
    page.locator('.hs-box').evaluate('node=>node.style.width="360px"')
    page.wait_for_timeout(100)
    assert not page.evaluate('''cls=>[...document.querySelectorAll('.sb-thumb')]
        .some(t=>t._key===document.querySelector('.'+cls))''', row_class)
    # Code/table-style horizontal containers still retain their normal custom scrollbar.
    page.evaluate('''() => {
      const other=document.createElement('div'); other.id='other-scroll';
      other.style.cssText='position:fixed;top:300px;left:30px;width:260px;height:100px;overflow:auto';
      other.innerHTML='<div style="width:1400px;height:40px">合成横向内容</div>';
      document.querySelector('main').appendChild(other);
    }''')
    page.wait_for_function('''()=>[...document.querySelectorAll('.sb-horizontal.sb-show')]
        .some(t=>t._key===document.getElementById('other-scroll'))''')
