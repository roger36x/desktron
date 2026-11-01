"""信息栏宽屏、窄窗与触屏共用无阴影材质；用合成色块验证真实背景混合。"""
from io import BytesIO
from pathlib import Path

import pytest

pytestmark = pytest.mark.desktop
ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "frontend/style.css").read_text(encoding='utf-8')


@pytest.fixture
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        instance = p.webkit.launch(headless=True)
        yield instance
        instance.close()


def setup_page(browser, theme="dark", mode="narrow"):
    width = {"wide": 1600, "narrow": 1000, "mobile": 390}[mode]
    page = browser.new_page(viewport={"width": width, "height": 844},
                            is_mobile=mode == "mobile", has_touch=mode == "mobile")
    page.set_content(f'''<html data-theme="{theme}"><head>
      <meta name="viewport" content="width=device-width,initial-scale=1"></head><body>
      <div class="overlay-content hs-page {'hs-sit-open' if mode != 'wide' else ''}"
           style="height:844px;position:relative">
        <div class="hs-wrap"><div class="hs-body"><div class="hs-col">
          <p>示意正文，不读取实际会话</p></div></div></div>
        <button class="hs-sit-scrim" aria-label="收起信息栏"></button>
        <aside class="hs-sit" style="height:360px"><section class="hs-sit-sec">
          <h2>项目</h2><div class="hs-sit-recap-text">示意信息栏</div>
        </section></aside>
      </div></body></html>''')
    page.add_style_tag(content=CSS)
    return page


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("mode", ["wide", "narrow", "mobile"])
def test_same_material_without_shadow_or_backdrop_dimming(browser, theme, mode):
    page = setup_page(browser, theme, mode)
    styles = page.locator(".hs-sit").evaluate('''el => {
      const s=getComputedStyle(el), r=el.getBoundingClientRect();
      return {shadow:s.boxShadow,background:s.backgroundColor,radius:s.borderRadius,
        blur:s.backdropFilter||s.webkitBackdropFilter,visible:s.display !== 'none',
        left:r.left,right:r.right};
    }''')
    assert styles["visible"]
    assert styles["shadow"] == "none", styles
    assert styles["radius"] == "18px", styles
    assert styles["left"] >= 0 and styles["right"] <= page.viewport_size["width"]
    if theme == "dark":
        assert styles["background"] == "rgba(32, 32, 32, 0.72)", styles
        assert styles["blur"] == "blur(20px) saturate(0.7)", styles
    else:
        assert styles["background"] == "rgb(250, 250, 249)", styles
        assert styles["blur"] == "none", styles
    scrim = page.locator(".hs-sit-scrim")
    if mode != "wide":
        assert scrim.is_visible()
        assert scrim.evaluate("el => getComputedStyle(el).backgroundColor") == "rgba(0, 0, 0, 0)"
        # 遮罩仍拦截点击，但不使正文变暗；卡片内部不被遮罩挡住。
        page.evaluate('''() => {
          document.querySelector('.hs-sit-scrim').onclick = () =>
            document.querySelector('.hs-page').classList.remove('hs-sit-open');
        }''')
        assert page.locator(".hs-sit").evaluate('''el => {
          const r=el.getBoundingClientRect();
          return el.contains(document.elementFromPoint(r.left+50,r.top+50));
        }''')
        scrim.click(position={"x": 5, "y": 5})
        assert page.locator(".hs-sit").is_hidden()
    page.close()


def test_dark_glass_really_blends_the_background(browser):
    Image = pytest.importorskip("PIL.Image")
    page = setup_page(browser)
    # 背景放在真实正文层；信息栏采样处不含前景文字。
    page.locator(".hs-body").evaluate('''el => {
      el.style.background='linear-gradient(to bottom, rgb(0,0,0) 0 180px, rgb(255,255,255) 180px)';
    }''')
    rect = page.locator(".hs-sit").bounding_box()
    x = int(rect["x"] + rect["width"] / 2)
    def sample():
        image = Image.open(BytesIO(page.screenshot())).convert("RGB")
        return [image.getpixel((x, y))[0] for y in (110, 180, 290)]
    glass = sample()
    # 72% 底色不是不透明：背景由黑变白时卡片内部亮度也随之变化。
    assert glass[2] - glass[0] > 50, glass
    # 这里只验透明混合；模糊声明由上面的级联测试核对。
    # 本机无头 WebKit 在最小独立页面中也不渲染 backdrop blur，不能把声明当像素验收。
    assert 15 <= glass[0] <= 35 and 80 <= glass[2] <= 105, glass
    page.close()
