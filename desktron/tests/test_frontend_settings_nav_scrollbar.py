"""Mobile settings navigation scrolls without a native or overlay scrollbar."""
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / 'frontend/app-init.js').read_text(encoding='utf-8')
CSS = (ROOT / 'frontend/style.css').read_text(encoding='utf-8')
SCROLLBARS = re.search(r'\(function autoScrollbars\(\) \{.*?\n\}\)\(\);', SOURCE, re.S).group()


def test_settings_navigation_opt_out_only_in_mobile_layout():
    function = re.search(r'  function scrollable\(key\) \{.*?\n  }', SCROLLBARS, re.S).group()
    script = r'''
const assert = require('node:assert/strict');
const SEL = '.sm-inner-nav, #sm-settings-scroll';
let mobile = true, computed = 0;
const window = {matchMedia: query => {
  assert.equal(query, '(max-width: 820px), (max-height: 520px) and (pointer: coarse)');
  return {matches:mobile};
}};
const getComputedStyle = () => { computed++; return {overflowX:'auto',overflowY:'hidden'}; };
const key = selector => ({closest: () => null,
  matches: s => s.split(',').some(v => v.trim() === selector ||
    (selector === '.settings-center-live .sm-inner-nav' && v.trim() === '.sm-inner-nav'))});
''' + function + r'''
assert.equal(scrollable(key('.settings-center-live .sm-inner-nav')), false);
assert.equal(computed, 0, 'excluded before automatic registration');
assert.equal(scrollable(key('#sm-settings-scroll')), true, 'settings content retains scrollbar');
assert.equal(scrollable(key('.other-horizontal')), true, 'other horizontal containers retain scrollbar');
mobile = false;
assert.equal(scrollable(key('.settings-center-live .sm-inner-nav')), true, 'desktop nav retains scrollbar');
assert.equal(scrollable(key('.sm-inner-nav')), true, 'unrelated nav retains scrollbar');
'''
    subprocess.run(['node', '-'], input=script, check=True, capture_output=True,
                   text=True, encoding='utf-8', timeout=15)


@pytest.mark.desktop
@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('viewport', [{'width':390, 'height':844}, {'width':844, 'height':390}])
def test_mobile_settings_navigation_scroll_without_thumb(theme, viewport):
    pw_module = pytest.importorskip('playwright.sync_api')
    with pw_module.sync_playwright() as pw:
        browser = pw.webkit.launch()
        try:
            page = browser.new_page(viewport=viewport, has_touch=True, is_mobile=True)
            page.set_content('<main class="settings-center-live" style="position:fixed;inset:0">'
                             '<div class="sm-page"><nav class="sm-inner-nav">'
                             '<button id="sm-back">返回</button>'
                             + ''.join(f'<button class="sm-nav-row" data-index="{n}">合成设置分类{n}</button>'
                                       for n in range(14))
                             + '</nav><section class="sm-settings-content" id="sm-settings-scroll" '
                             'style="overflow:auto"><div style="height:2000px">合成设置内容</div>'
                             '</section></div></main>')
            page.add_style_tag(content=CSS)
            page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
            page.add_script_tag(content=SCROLLBARS)
            nav = page.locator('.sm-inner-nav')
            page.wait_for_function('()=>{const n=document.querySelector(".sm-inner-nav");return n.scrollWidth>n.clientWidth+1}')
            nav.hover()
            nav.evaluate('n=>{n.focus();n.dispatchEvent(new CustomEvent("desk-scrollbar-refresh",{bubbles:true}));}')
            page.wait_for_timeout(100)
            assert nav.evaluate('n=>getComputedStyle(n).scrollbarWidth') == 'none'
            assert nav.evaluate('n=>getComputedStyle(n,"::-webkit-scrollbar").display') == 'none'
            assert nav.evaluate('n=>getComputedStyle(n).overflowX') == 'auto'
            page.mouse.wheel(240, 0)
            page.wait_for_function('()=>document.querySelector(".sm-inner-nav").scrollLeft>0')
            nav.evaluate('n=>{n.scrollLeft=n.scrollWidth;n.dispatchEvent(new Event("scroll"));}')
            page.evaluate('()=>document.querySelector(".sm-inner-nav").addEventListener("click",e=>{'
                          'if(e.target.dataset.index) document.body.dataset.selected=e.target.dataset.index;})')
            page.locator('[data-index="13"]').click()
            assert page.locator('body').get_attribute('data-selected') == '13'
            page.wait_for_timeout(100)
            assert not page.evaluate('()=>[...document.querySelectorAll(".sb-thumb")].some('
                                    't=>t._key===document.querySelector(".sm-inner-nav"))')
            page.locator('#sm-settings-scroll').hover()
            page.wait_for_function('()=>[...document.querySelectorAll(".sb-thumb.sb-show")].some('
                                   't=>t._key===document.getElementById("sm-settings-scroll"))')
            # Desktop retains the vertical nav overlay after resizing the same page.
            page.set_viewport_size({'width':1100, 'height':900})
            nav.evaluate('n=>{n.style.height="180px";n.dispatchEvent(new CustomEvent("desk-scrollbar-refresh",{bubbles:true}));}')
            page.wait_for_function('()=>[...document.querySelectorAll(".sb-thumb.sb-show:not(.sb-horizontal)")].some('
                                   't=>t._key===document.querySelector(".sm-inner-nav"))')
        finally:
            browser.close()
