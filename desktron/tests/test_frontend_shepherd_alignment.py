"""Shepherd must be enlarged in every entry, with visible ink aligned to its label."""
import base64
import io
from pathlib import Path

import pytest

FRONT = Path(__file__).resolve().parents[2] / 'frontend'
ICON = '<span class="brand-shepherd img-ic"></span>'
BODY = f'''
<div class="side-foot" style="position:absolute;left:12px;top:130px;width:230px">
 <div class="foot-row"><button id="engine-pick" class="side-fx">
 <span class="fx-ic">{ICON}</span><span>Shepherd</span></button></div></div>
<div id="engine-menu" class="float-menu" style="display:block;position:absolute;left:12px;top:12px;bottom:auto">
 <div class="fm-head">联合引擎</div><div class="fm-row fm-shepherd on">
 <span class="fm-icon">{ICON}</span><span class="fm-main"><span class="fm-title">Shepherd</span></span>
 <span class="fm-check">●</span></div></div>
<div style="position:absolute;left:12px;top:200px"><button class="frw-engine">
 <span class="frw-engine-icon">{ICON}</span><span class="frw-engine-label">Shepherd</span></button></div>
<div style="position:absolute;left:12px;top:260px"><button class="frw-engine-item selected">
 <span class="frw-engine-icon">{ICON}</span><span>Shepherd</span><span class="frw-engine-check"></span></button></div>
'''
USES = [('#engine-menu .fm-row', '.fm-title'),
        ('#engine-pick', ':scope > span:last-child'),
        ('.frw-engine', '.frw-engine-label'),
        ('.frw-engine-item', ':scope > span:nth-child(2)')]


@pytest.mark.desktop
@pytest.mark.parametrize('width', [390, 820, 1280])
@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_all_four_entries_align_visible_ink_not_only_element_boxes(width, theme):
    from PIL import Image
    from playwright.sync_api import sync_playwright

    data = base64.b64encode((FRONT / 'icons/shepherd.png').read_bytes()).decode()
    css = (FRONT / 'style.css').read_text(encoding='utf-8').replace(
        '/icons/shepherd.png', f'data:image/png;base64,{data}')
    with sync_playwright() as pw:
        browser = pw.webkit.launch()
        try:
            page = browser.new_page(viewport={'width': width, 'height': 340}, device_scale_factor=4)
            page.set_content(f'<html data-theme="{theme}"><meta charset="utf-8"><body>{BODY}</body></html>')
            page.add_style_tag(content=css)
            page.evaluate('async()=>{await document.fonts.ready}')
            for root, label in USES:
                row = page.locator(root)
                icon = row.locator('.brand-shepherd')
                assert icon.evaluate('e=>[e.clientWidth,e.clientHeight]') == [18, 18]
                assert icon.evaluate('e=>getComputedStyle(e).transform') == 'none'
                boxes = [icon.bounding_box(), row.locator(label).bounding_box()]
                row.evaluate("e=>e.dataset.inkTest='1'")
                style = page.add_style_tag(content='''
                  [data-ink-test], [data-ink-test] * {background-color:#000!important;
                    color:#fff!important;border-color:#000!important;box-shadow:none!important;text-shadow:none!important}
                  [data-ink-test] .brand-shepherd {background-color:#fff!important}
                ''')
                origin = row.bounding_box()
                image = Image.open(io.BytesIO(row.screenshot())).convert('RGB')
                centers = []
                for index, box in enumerate(boxes):
                    left = max(0, round((box['x'] - origin['x']) * 4))
                    top = max(0, round((box['y'] - origin['y']) * 4))
                    right = min(image.width, round((box['x'] + box['width'] - origin['x']) * 4))
                    bottom = min(image.height, round((box['y'] + box['height'] - origin['y']) * 4))
                    ink = [(y, image.getpixel((x, y))[0]) for y in range(top, bottom)
                           for x in range(left, right) if image.getpixel((x, y))[0] > 64]
                    assert ink, (width, theme, root, index)
                    # Narrow ears are not the silhouette's optical center. Weight
                    # the icon by visible ink; compare with the label's glyph bounds.
                    center = (sum((y + .5) * v for y, v in ink) / sum(v for y, v in ink)
                              if index == 0 else (min(y for y, _ in ink) + max(y for y, _ in ink) + 1) / 2)
                    centers.append(center / 4)
                assert abs(centers[0] - centers[1]) <= .25, (width, theme, root, centers)
                style.evaluate('e=>e.remove()')
                row.evaluate('e=>delete e.dataset.inkTest')
        finally:
            browser.close()
