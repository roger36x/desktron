"""Mobile chat gutter and two-thirds ticks share one layout; synthetic data only."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / 'frontend/style.css').read_text(encoding='utf-8')


def test_mobile_composer_uses_twenty_pixel_bottom_gap_without_changing_desktop():
    mobile = CSS[CSS.index('/* ---- 会话页的手机布局'):]
    assert '.hs-page .hs-composer { padding-bottom: calc(20px + env(safe-area-inset-bottom)); }' in mobile
    assert 'padding: 4px var(--hs-pad-r) 20px var(--hs-pad-l)' in CSS


def test_mobile_ticks_gain_two_pixels_of_edge_space_without_shrinking_hit_area():
    mobile = CSS[CSS.index('/* ---- 会话页的手机布局'):]
    assert '.hs-page .hs-toc { left: 2px; width: 18px; padding-left: 2px; max-height: 66%; }' in mobile
    assert '* { box-sizing: border-box; }' in CSS


def test_mobile_gutter_and_touch_restore_share_wider_right_margin():
    assert '.hs-page .hs-wrap, .hs-page .hs-composer { --hs-pad-l: 22px; --hs-pad-r: 20px; }' in CSS
    assert '.hs-page { --hs-mobile-pad-r: 20px; }' in CSS
    assert CSS.count('--hs-pad-r: var(--hs-mobile-pad-r, 18px)') == 2


def test_mobile_box_and_context_rail_share_rounder_corners():
    desktop, mobile = CSS.split('/* ---- 会话页的手机布局', 1)
    assert '.hs-page .hs-box { padding: 12px 13px 8px; border-radius: 22px; }' in mobile
    assert '.hs-page .hs-context-rail { border-radius: 22px 22px 0 0; }' in mobile
    assert 'background: var(--hs-box-bg); border-radius: 20px;' in desktop
    assert 'border-radius: 20px 20px 0 0;' in desktop


@pytest.mark.desktop
@pytest.mark.parametrize('sit_disabled', [False, True])
@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('viewport', [{'width':320, 'height':800}, {'width':390, 'height':844},
                                     {'width':900, 'height':390}])
def test_mobile_body_composer_and_preview_gutter(theme, viewport, sit_disabled):
    pw_module = pytest.importorskip('playwright.sync_api')
    with pw_module.sync_playwright() as pw:
        browser = pw.webkit.launch()
        try:
            page = browser.new_page(viewport=viewport, has_touch=True)
            page.set_content('<main class="overlay-content hs-page' + (' hs-sit-disabled' if sit_disabled else '') + '" style="position:fixed;inset:0">'
                             '<div class="hs-toc"><div class="hs-tk"></div><div class="hs-tk on"></div></div>'
                             '<div class="hs-peek">合成预览</div>'
                             '<div class="hs-wrap"><div class="hs-body"><div class="hs-stream hs-col">'
                             '<p class="hs-text md">合成正文，不读取真实会话</p></div></div></div>'
                             '<div class="hs-composer"><div class="hs-context hs-col"><div class="hs-context-rail">合成任务</div></div>'
                             '<div class="hs-box hs-col"><div class="hs-inp" contenteditable="true">合成草稿</div>'
                             '<div class="hs-row"><button class="hs-send">↑</button></div></div></div></main>')
            page.add_style_tag(content=CSS)
            page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
            for height in [viewport['height'], min(viewport['height'], 400)]:
                page.set_viewport_size({'width':viewport['width'], 'height':height})
                page.locator('.hs-inp').focus()
                metrics = page.evaluate('''() => {
                  const rect=s=>{const r=document.querySelector(s).getBoundingClientRect();
                    return {left:r.left,right:r.right,width:r.width};};
                  return {body:rect('.hs-body'),stream:rect('.hs-stream'),box:rect('.hs-box'),
                    context:rect('.hs-context'),toc:rect('.hs-toc'),peek:rect('.hs-peek'),
                    normal:rect('.hs-tk'),active:rect('.hs-tk.on'),
                    bodyLeft:getComputedStyle(document.querySelector('.hs-body')).paddingLeft,
                    composerLeft:getComputedStyle(document.querySelector('.hs-composer')).paddingLeft,
                    bodyRight:getComputedStyle(document.querySelector('.hs-body')).paddingRight,
                    composerRight:getComputedStyle(document.querySelector('.hs-composer')).paddingRight,
                    composerBottom:getComputedStyle(document.querySelector('.hs-composer')).paddingBottom,
                    boxRadius:getComputedStyle(document.querySelector('.hs-box')).borderTopLeftRadius,
                    railTopRadius:getComputedStyle(document.querySelector('.hs-context-rail')).borderTopLeftRadius,
                    railBottomRadius:getComputedStyle(document.querySelector('.hs-context-rail')).borderBottomLeftRadius,
                    overflow:document.documentElement.scrollWidth>innerWidth};
                }''')
                assert metrics['bodyLeft'] == metrics['composerLeft'] == '22px'
                assert metrics['bodyRight'] == metrics['composerRight'] == '20px'
                assert metrics['composerBottom'] == '20px'
                assert metrics['boxRadius'] == metrics['railTopRadius'] == '22px'
                assert metrics['railBottomRadius'] == '0px'
                assert abs(metrics['normal']['width'] - 10 * 2 / 3) < .02
                assert metrics['active']['width'] == 12
                assert metrics['toc']['left'] == 2 and metrics['toc']['right'] == 20
                assert metrics['normal']['left'] == metrics['active']['left'] == 4
                assert metrics['peek']['left'] == 22
                assert metrics['stream']['left'] == metrics['box']['left'] == metrics['context']['left']
                assert metrics['stream']['right'] == metrics['box']['right'] == metrics['context']['right']
                assert metrics['toc']['right'] <= metrics['stream']['left']
                assert metrics['box']['right'] <= viewport['width'] - 20
                assert not metrics['overflow']
                assert page.locator('.hs-inp').inner_text() == '合成草稿'
        finally:
            browser.close()
