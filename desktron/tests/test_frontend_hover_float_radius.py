"""悬停提示共用圆角；不改点击菜单、常驻卡片及浮层尺寸。"""
from pathlib import Path
import re

import pytest

CSS = (Path(__file__).resolve().parents[2] / "frontend/style.css").read_text(encoding='utf-8')
CASES = [
    (".hs-sit-tooltip", '<div class="hs-sit-tooltip show">回顾全文提示</div>', None, False),
    ("#app-dock .dk-item:hover::before", '<div id="app-dock"><div class="dk-item" data-name="文件浏览器">应用</div></div>', "::before", True),
    (".ap-inst:hover::after", '<div class="ap-inst" data-name="文件浏览器">应用</div>', "::after", True),
    ("#dock-intro-tip", '<div id="dock-intro-tip">程序坞使用说明</div>', None, False),
    (".wf-chart-tip", '<div class="wf-chart-tip">任务图表提示</div>', None, False),
    (".hs-page .hs-peek", '<div class="hs-page"><div class="hs-peek on">刻度内容预览</div></div>', None, False),
    (".hs-page [data-tip]::after", '<div class="hs-page"><button data-tip="发送">发送</button></div>', "::after", True),
    (".wf-toc.ticks:hover", '<div class="wf-toc ticks"><div>工作流目录</div></div>', None, True),
]


def rule(selector):
    return re.search(r"^" + re.escape(selector) + r"\s*\{([^}]*)\}", CSS, re.M).group(1)


@pytest.mark.parametrize("selector,html,pseudo,hover", CASES)
def test_hover_float_uses_shared_radius(selector, html, pseudo, hover):
    assert "border-radius: var(--hover-float-radius)" in rule(selector)


def test_shared_radius_is_16px():
    assert "--hover-float-radius: 16px" in rule(":root")


def test_click_popovers_and_situation_card_are_unchanged():
    for selector in (".hs-page .hs-context-popover", ".hs-sit", ".rp-ctx"):
        assert "--hover-float-radius" not in rule(selector)


@pytest.mark.desktop
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_webkit_hover_radius_and_geometry(theme):
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        browser = p.webkit.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        for selector, html, pseudo, hover in CASES:
            page.set_content(html)
            page.add_style_tag(content=CSS)
            page.add_style_tag(content='''
              body { margin: 0; }
              .hs-page { width: 100%; height: 700px; }
              #app-dock { transform: none; opacity: 1; pointer-events: auto; }
              .wf-chart-tip { display: block; }
              .wf-toc { left: 50px; }
              .ap-inst { position: absolute; top: 100px; left: 300px; width: 50px; height: 40px; }
              .hs-page [data-tip] { position: absolute; top: 120px; left: 500px; }
            ''')
            page.evaluate("t => document.documentElement.dataset.theme=t", theme)
            target = re.sub(r"::(?:before|after)|:hover", "", selector)
            node = page.locator(target)
            if hover:
                node.hover()
                page.wait_for_timeout(350)
            metrics = '''(el,pseudo)=>{const s=getComputedStyle(el,pseudo);return {
              radius:s.borderTopLeftRadius,width:s.width,height:s.height,padding:s.padding,
              shadow:s.boxShadow,border:s.borderWidth,background:s.backgroundColor,
              visibility:s.visibility,content:s.content};}'''
            after = node.evaluate(metrics, pseudo)
            assert after["radius"] == "16px", selector
            if pseudo:
                assert after["content"] not in ("none", '""'), selector
                assert after["visibility"] == "visible", selector
            page.evaluate("document.documentElement.style.setProperty('--hover-float-radius','8px')")
            before = node.evaluate(metrics, pseudo)
            assert before["radius"] == "8px", selector
            assert {k: v for k, v in after.items() if k != "radius"} == {
                k: v for k, v in before.items() if k != "radius"
            }, selector
            page.evaluate("document.documentElement.style.removeProperty('--hover-float-radius')")
        browser.close()
