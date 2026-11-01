"""回复分支使用原创图；兼容原路径并保留能力开关、点击行为与主题颜色。"""
from pathlib import Path
import hashlib
import struct

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend"
ICON = "chat-branch-4626029.png"


def test_original_fork_asset_and_license():
    data = (FRONT / "icons" / ICON).read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", data[16:24]) == (512, 512)
    assert hashlib.sha256(data).hexdigest() == "2ac938814b5ea1a97bf00b5a6cad8717f2063cf716d96cae77d108247196bd63"
    license_text = (FRONT / "vendor/ICONS-LICENSE.txt").read_text(encoding='utf-8')
    for text in ("chat-branch-4626029", "Desktron 原创圆润界面图标", "当前内容不再是Flaticon git_4626029"):
        assert text in license_text
    tools = (FRONT / "app-chat-tools.js").read_text(encoding='utf-8')
    assert 'fork: \'<span class="hs-fork-icon" aria-hidden="true"></span>\'' in tools
    css = (FRONT / "style.css").read_text(encoding='utf-8')
    assert f'-webkit-mask: url("/icons/{ICON}") center / contain no-repeat' in css
    assert f'  mask: url("/icons/{ICON}") center / contain no-repeat' in css


def preview_html(theme):
    return f'''<!doctype html><html data-theme="{theme}"><head><meta charset="utf-8">
<link rel="stylesheet" href="/style.css"></head><body>
<div class="hs-page" style="position:static;display:block;padding:24px;min-height:180px">
<div class="hs-turn" style="pointer-events:auto"><div class="hs-a"><p>回复分支图标</p>
<div id="holder"></div></div></div></div>
<script src="/app-chat-tools.js"></script><script src="/app-chat-refs.js"></script>
<script>
window.hsWhen = () => '10月16日 01:46';
window.hsGenPaint = () => {{}};
window.hsFork = (v, t) => window.forked = [v.sid, t.msgId];
window.view = {{sid:'preview-session', caps:{{fork:true, rewind:false}}}};
window.turn = {{msgId:'preview-message', at:1, parts:new Map(), holder:document.querySelector('#holder')}};
hsFoot(view, turn);
</script></body></html>'''


@pytest.mark.desktop
@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width", [1280, 390])
def test_fork_mask_theme_geometry_and_action(theme, width):
    from playwright.sync_api import sync_playwright
    from urllib.parse import urlparse

    with sync_playwright() as pw:
        browser = pw.webkit.launch()
        page = browser.new_page(viewport={"width": width, "height": 300})
        page.set_default_timeout(5000)
        loaded = []
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(req):
            path = urlparse(req.request.url).path
            if path == "/":
                req.fulfill(content_type="text/html", body=preview_html(theme))
            elif path in ("/style.css", "/app-chat-tools.js", "/app-chat-refs.js"):
                req.fulfill(content_type="text/css" if path.endswith(".css") else "text/javascript",
                            body=(FRONT / path.lstrip("/")).read_bytes())
            elif path == "/icons/" + ICON:
                loaded.append(path)
                req.fulfill(content_type="image/png", body=(FRONT / "icons" / ICON).read_bytes())
            else:
                req.abort()

        page.route("http://fork.test/**", route)
        page.goto("http://fork.test/")
        page.locator(".hs-turn p").hover()
        button = page.locator('[data-a="fork"]')
        icon = button.locator(".hs-fork-icon")
        button.wait_for(state="visible")
        style = icon.evaluate('''el => { const s=getComputedStyle(el), r=el.getBoundingClientRect();
          return {w:r.width,h:r.height,mask:s.webkitMaskImage,bg:s.backgroundColor,color:s.color}; }''')
        assert style["w"] == style["h"] == 12.25
        assert style["mask"].endswith(ICON + '\")')
        assert style["bg"] == style["color"] != "rgba(0, 0, 0, 0)"
        assert loaded
        before = button.bounding_box()
        button.hover()
        assert icon.evaluate("el => getComputedStyle(el).backgroundColor") == button.evaluate("el => getComputedStyle(el).color")
        assert before == button.bounding_box()
        button.click()
        assert page.evaluate("window.forked") == ["preview-session", "preview-message"]
        page.evaluate('''() => { turn.footEl.remove(); turn.footEl=null;
          view.caps.fork=false; hsFoot(view,turn); }''')
        assert not page.locator('[data-a="fork"]').is_visible()
        assert page.locator('[data-a="copy"]').is_visible()
        assert not errors
        browser.close()
