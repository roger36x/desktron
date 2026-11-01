"""会话右上角应用图标：在隔离 WebKit 中核对真实级联与可见轮廓。"""
from pathlib import Path
import re

import pytest

pytestmark = pytest.mark.desktop
ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend"
CSS = (FRONT / "style.css").read_text(encoding='utf-8')
SOURCE = (FRONT / "app-assets-apps.js").read_text(encoding='utf-8')
ICON_SOURCE = SOURCE[SOURCE.index("const _APP_COLORS"):SOURCE.index("// 最近打开过的应用。")]
RENDER_SOURCE = re.search(r"\nfunction renderHeadRecent\(\)[\s\S]*?\n}\n", SOURCE).group()
APPS = [
    {"key": "file-browser", "a": {"label": "文件浏览器", "icon": "/icons/app-file-browser-original-7710531b884e.png"}},
    {"key": "files", "a": {"label": "Monaco IDE"}},
    {"key": "terminal", "a": {"label": "终端", "icon": "/icons/app-terminal.png"}},
    {"key": "browser", "a": {"label": "浏览器"}},
]


@pytest.fixture
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        browser = p.webkit.launch()
        yield browser
        browser.close()


def setup_page(browser, theme="dark", dpr=1, width=1280):
    page = browser.new_page(viewport={"width": width, "height": 800}, device_scale_factor=dpr)

    def route(req):
        name = req.request.url.split("/")[-1]
        if name.endswith(".png"):
            req.fulfill(path=str(FRONT / "icons" / name), content_type="image/png")
        else:
            req.fulfill(content_type="text/html", body='<meta charset="utf-8">'
                        '<div id="overlay" class="hs-head"><span id="ov-recent"></span></div>'
                        '<div id="panel-icons"></div>')

    page.route("https://desk.test/**", route)
    page.goto("https://desk.test/")
    page.add_style_tag(content=CSS)
    page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
    page.add_script_tag(content="const escapeHtml = s => String(s || '');\n" + ICON_SOURCE)
    page.evaluate("apps => { window.testApps = apps; window.activations = []; }", APPS)
    page.add_script_tag(content='''
const _APP_RECENT_SHOW = 4;
const _allApps = () => testApps;
const _appRecent = () => testApps.map(a => a.key);
const _dockApps = () => testApps;
const _dockRunningPage = x => ({id: x.key});
const showAppSide = p => activations.push(p.id);
''' + RENDER_SOURCE + '''
renderHeadRecent();
document.getElementById('panel-icons').innerHTML = testApps.map(x =>
  _appIconHTML(x.a.label, x.key, '', '', x.a.icon)).join('');
''')
    page.wait_for_function("Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)")
    page.evaluate('''async () => {
      await Promise.all(Array.from(document.querySelectorAll('svg image')).map(el =>
        new Promise((resolve,reject) => {
          const image=new Image(); image.onload=resolve; image.onerror=reject;
          image.src=el.getAttribute('href');
        })));
    }''')
    return page


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("dpr", [1, 2])
def test_visible_icon_bounds_and_centers(browser, theme, dpr):
    page = setup_page(browser, theme, dpr)
    metrics = page.evaluate('''() => Array.from(document.querySelectorAll('#ov-recent .ov-rc')).map(button => {
      const icon = button.querySelector('.ap-ic'), art = icon.querySelector('img,svg');
      const frame = icon.getBoundingClientRect(), rect = art.getBoundingClientRect();
      let visible;
      if (art.tagName === 'IMG') {
        const canvas = document.createElement('canvas');
        canvas.width = art.naturalWidth; canvas.height = art.naturalHeight;
        const ctx = canvas.getContext('2d'); ctx.drawImage(art, 0, 0);
        const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
        let left = canvas.width, top = canvas.height, right = 0, bottom = 0;
        for (let y=0; y<canvas.height; y++) for (let x=0; x<canvas.width; x++) {
          if (pixels[(y*canvas.width+x)*4+3] <= 32) continue;
          left=Math.min(left,x); top=Math.min(top,y);
          right=Math.max(right,x+1); bottom=Math.max(bottom,y+1);
        }
        visible = {left: rect.left+left/canvas.width*rect.width,
          top: rect.top+top/canvas.height*rect.height,
          width: (right-left)/canvas.width*rect.width,
          height: (bottom-top)/canvas.height*rect.height};
      } else {
        const outline = art.querySelector('path,image');
        const box = outline.getBBox(), matrix = outline.getScreenCTM();
        const a = new DOMPoint(box.x,box.y).matrixTransform(matrix);
        const b = new DOMPoint(box.x+box.width,box.y+box.height).matrixTransform(matrix);
        visible = {left:a.x,top:a.y,width:b.x-a.x,height:b.y-a.y};
      }
      const hit=button.getBoundingClientRect();
      return {name:button.title, frameWidth:frame.width, hitWidth:hit.width, hitHeight:hit.height,
        width:visible.width,height:visible.height,
        centerX:visible.left+visible.width/2-(hit.left+hit.width/2),
        centerY:visible.top+visible.height/2-(hit.top+hit.height/2),
        filter:getComputedStyle(art).filter, shadow:getComputedStyle(icon).boxShadow};
    })''')
    assert len(metrics) == 4
    for m in metrics:
        assert m["frameWidth"] == 17, m
        assert (m["hitWidth"], m["hitHeight"]) == (23, 23), m
        assert abs(m["width"] - 17) < .05, m
        assert abs(m["height"] - 17) < .05, m
        assert abs(m["centerX"]) < .02, m
        assert abs(m["centerY"]) < .02, m
        assert m["filter"] == "none", m
        assert m["shadow"] == "none", m
    # 面板图标继续保留原始画布与投影，不接受顶栏的缩放或中线补差。
    outside = page.locator('#panel-icons .ap-ic img').evaluate_all('''nodes => nodes.map(i => {
      const s=getComputedStyle(i);return [i.getBoundingClientRect().width,s.transform,s.filter];
    })''')
    assert all(width == 40 and transform == "none" and shadow != "none"
               for width, transform, shadow in outside)
    for i, app in enumerate(APPS):
        button = page.locator('#ov-recent .ov-rc').nth(i)
        button.hover()
        button.focus()
        assert button.evaluate("b => document.activeElement === b")
        button.press("Enter")
        assert page.evaluate("activations") == [a["key"] for a in APPS[:i+1]]
    page.close()


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width", [390, 1280])
def test_browser_globe_is_white_in_panel_and_recent_icons(browser, theme, width):
    from io import BytesIO
    from PIL import Image
    page = setup_page(browser, theme, dpr=2, width=width)
    selectors = ['#panel-icons .ap-ic']
    if width > 820:
        selectors.append('#ov-recent .ov-rc .ap-ic')
    else:
        assert page.locator('#ov-recent').is_hidden()
    for selector in selectors:
        icon = page.locator(selector).nth(3)
        assert icon.locator('svg image').get_attribute('href') == '/icons/app-browser-original-5d5d9b828321.png'
        image = Image.open(BytesIO(icon.screenshot())).convert('RGB')
        # 双纬线在中心留出紫底，验证球体区域仍有白线，而非要求中心白色。
        w, h = image.size
        globe = image.crop((int(w*.22), int(h*.22), int(w*.78), int(h*.78)))
        white = sum(min(pixel) >= 190 and max(pixel)-min(pixel) <= 35
                    for pixel in globe.getdata())
        assert white > globe.width * globe.height * .12, (theme, selector, white)
    page.close()


def test_browser_outline_matches_original_v4_rounded_rectangle(browser):
    page = setup_page(browser)
    difference = page.evaluate('''async () => {
      const image = new Image();
      image.src = document.querySelector('#panel-icons .ap-ic:last-child svg image').getAttribute('href');
      await image.decode();
      const actual = document.createElement('canvas'), expected = document.createElement('canvas');
      actual.width = expected.width = actual.height = expected.height = 512;
      const a = actual.getContext('2d'), b = expected.getContext('2d');
      a.drawImage(image, 0, 0);
      b.scale(16, 16);
      b.beginPath(); b.roundRect(1, 1, 30, 30, 9); b.fill();
      const ap = a.getImageData(0, 0, 512, 512).data, bp = b.getImageData(0, 0, 512, 512).data;
      let difference = 0;
      for (let i = 3; i < ap.length; i += 4) difference += Math.abs(ap[i] - bp[i]);
      return difference / (512 * 512);
    }''')
    # 仅容许 PNG 超采样与 WebKit 路径光栅化在抗锯齿边缘的微小差别。
    assert difference < .5, difference
    page.close()


def test_mobile_still_hides_recent_app_tray(browser):
    page = setup_page(browser, width=390, dpr=3)
    assert page.locator('#ov-recent').is_hidden()
    page.close()
