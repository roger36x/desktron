"""Synthetic WebKit rail: compare visible ink, not just flex rectangles."""
import base64
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / 'frontend/style.css').read_text(encoding='utf-8')
RULE = 'html:not(.os-windows) .hs-page .hs-context-item > .hs-context-icon { transform: translateY(.5px); }'


def test_optical_offset_is_scoped_to_non_windows_rail_buttons():
    assert RULE in CSS
    assert '.hs-context-status-popover .hs-context-icon { transform:' not in CSS


@pytest.mark.desktop
@pytest.mark.parametrize('width', [390, 760, 1200])
@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_rail_visible_ink_centers_and_unchanged_layout(width, theme):
    from PIL import Image
    from playwright.sync_api import sync_playwright

    css = CSS
    for icon in ('task', 'arrangement', 'status'):
        data = base64.b64encode((ROOT / f'frontend/icons/{icon}.png').read_bytes()).decode()
        css = css.replace(f'/icons/{icon}.png', f'data:image/png;base64,{data}')
    columns = []
    for wrapper, icon, label, meta in [
        ('hs-tasks', 'task', 'Shepherd 新会话继承上次模型', ''),
        ('hs-arrangements', 'arrangements', '安排', '暂无安排'),
        ('hs-context-status', 'status', '状态', '引擎已就绪'),
    ]:
        columns.append(f'<div class="{wrapper}"><button class="hs-context-item'
                       + (' hs-tasks-bar' if icon == 'task' else '') + '">'
                       + f'<span class="hs-context-icon hs-context-{icon}-icon"></span>'
                       + f'<span class="hs-context-label">{label}</span>'
                       + (f'<span class="hs-context-meta">{meta}</span>' if meta else '')
                       + '</button></div>')
    with sync_playwright() as pw:
        browser = pw.webkit.launch()
        try:
            page = browser.new_page(viewport={'width': width, 'height': 150}, device_scale_factor=2)
            page.set_content(f'<html data-theme="{theme}"><main class="hs-page" '
                             f'style="position:relative;container-name:hs-chat;container-type:inline-size;width:{width}px">'
                             '<div class="hs-context"><div class="hs-context-rail">'
                             + ''.join(columns) + '</div></div></main></html>')
            page.add_style_tag(content=css)
            before = page.locator('.hs-context-rail').bounding_box()
            image = Image.open(io.BytesIO(page.screenshot())).convert('RGB')
            for wrapper in ('hs-tasks', 'hs-arrangements', 'hs-context-status'):
                centers = []
                for kind in ('hs-context-icon', 'hs-context-label'):
                    box = page.locator(f'.{wrapper} .{kind}').bounding_box()
                    # Isolate this element's x-range in the full screenshot. Locator
                    # screenshots round fractional y offsets and hide half-pixel bugs.
                    crop = image.crop((round(box['x'] * 2), 0,
                                       round((box['x'] + box['width']) * 2), 80))
                    background = crop.getpixel((0, 0))
                    rows = [y for y in range(crop.height) for x in range(crop.width)
                            if max(abs(a - b) for a, b in zip(crop.getpixel((x, y)), background)) > 40]
                    assert rows, (width, theme, wrapper, kind)
                    centers.append((min(rows) + max(rows) + 1) / 4)
                # Mixed Latin descenders and CJK have different ink bounds;
                # permit one physical pixel at 2x, not one CSS pixel.
                assert abs(centers[0] - centers[1]) <= .5, (width, theme, wrapper, centers)
            page.evaluate("document.documentElement.classList.add('os-windows')")
            assert page.locator('.hs-context-icon').first.evaluate(
                'e=>getComputedStyle(e).transform') == 'none'
            assert page.locator('.hs-context-rail').bounding_box() == before
        finally:
            browser.close()
