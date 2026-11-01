"""刻度尺寸与命中：隔离 WebKit 验证，不读取真实会话。"""
from pathlib import Path
import re

import pytest

pytestmark = pytest.mark.desktop
ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "frontend/style.css").read_text(encoding='utf-8')
SOURCE = (ROOT / "frontend/app-chat-overlays.js").read_text(encoding='utf-8')


def fn(name):
    return re.search(r"function " + name + r"\([^\n]*\)[\s\S]*?\n}", SOURCE).group()


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width", [1280, 390])
def test_short_phone_ticks_keep_pitch_and_container_hit_area(theme, width):
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        browser = p.webkit.launch()
        page = browser.new_page(viewport={"width": width, "height": 800})
        page.set_content('<div class="hs-page" style="width:100%;height:600px;--hs-comp-empty:0px">'
                         '<div class="hs-toc">' + '<div class="hs-tk"></div>' * 15 + '</div></div>')
        page.add_style_tag(content=CSS)
        page.evaluate("t => document.documentElement.dataset.theme=t", theme)
        page.add_script_tag(content='''
const v={toc:document.querySelector('.hs-toc'),el:document.querySelector('.hs-page')};
let jumps=0, previews=0;
Array.from(v.toc.children).forEach((tick,i)=>tick._turn={el:tick,id:i});
function hsPeek(){previews++}
function hsPeekHide(){}
function hsPeekLater(){}
function hsUnstick(){}
function hsViewportReveal(){jumps++}
''' + fn("hsTocHit") + fn("hsTocBind") + "\nhsTocBind(v);")

        def metrics():
            return page.locator('.hs-tk').evaluate_all('''nodes=>nodes.map(el=>{
              const r=el.getBoundingClientRect();return {width:r.width,height:r.height,cy:r.top+r.height/2};
            })''')

        before = metrics()
        expected_width = 6.5 if width == 1280 else 10 * 2 / 3
        assert all(abs(m['width'] - expected_width) < .02 and m['height'] == 1.75 for m in before)
        assert all(abs(before[i+1]['cy'] - before[i]['cy'] - 9) < .02 for i in range(14))
        frame = page.locator('.hs-toc').bounding_box()
        assert frame['width'] == (26 if width == 1280 else 18)
        # 指针位于横杠右侧及两杠之间，仍命中最近项；交互范围仍按整列判定。
        page.mouse.move(frame['x'] + frame['width'] - 1, before[6]['cy'] + 3)
        active = page.locator('.hs-tk.on')
        assert active.count() == 1
        assert active.evaluate("el => el._turn.id") == 6
        page.wait_for_timeout(200)  # 等待既有宽度过渡完成
        after = metrics()
        assert after[6]['width'] == (15 if width == 1280 else 12)
        assert after[6]['height'] == 2.25
        assert all(abs(a['cy'] - b['cy']) < .02 for a, b in zip(before, after))
        assert page.locator('.hs-toc').bounding_box() == frame
        page.mouse.click(frame['x'] + frame['width'] - 1, before[6]['cy'] + 3)
        assert page.evaluate('jumps') == 1
        assert page.evaluate('previews') >= 1
        browser.close()
