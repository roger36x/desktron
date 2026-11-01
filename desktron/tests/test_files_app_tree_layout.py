"""IDE 桌面及手机文件树密度对齐文件浏览器，保留开合图标、Seti 与搜索触控高度。"""
from pathlib import Path
import mimetypes
from urllib.parse import parse_qs, urlsplit

import pytest

pytestmark = pytest.mark.desktop
ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend"
LONG_NAME = "很长的文件名称-用于检查省略而不撑宽侧栏.md"
ITEMS = {
    "": [
        {"name": ".claude", "path": ".claude", "type": "dir", "has_children": True},
        {"name": "docs", "path": "docs", "type": "dir", "has_children": True},
        {"name": "empty", "path": "empty", "type": "dir", "has_children": False},
        {"name": "README.md", "path": "README.md", "type": "file"},
        {"name": LONG_NAME, "path": LONG_NAME, "type": "file"},
    ],
    "docs": [
        {"name": "reference", "path": "docs/reference", "type": "dir", "has_children": True},
        {"name": "guide.md", "path": "docs/guide.md", "type": "file"},
    ],
    "docs/reference": [{"name": "intro.md", "path": "docs/reference/intro.md", "type": "file"}],
}


@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        instance = p.webkit.launch()
        yield instance
        instance.close()


def open_app(context, app):
    page = context.new_page()

    def route(request):
        url = urlsplit(request.request.url)
        query = parse_qs(url.query)
        if url.path == "/workbench/fs":
            if "file" in query:
                request.fulfill(content_type="text/plain", body="# Example\n")
            else:
                request.fulfill(json={"items": ITEMS.get(query.get("path", [""])[0], [])})
            return
        target = FRONT / url.path.lstrip("/")
        if target.is_file() and target.resolve().is_relative_to(FRONT):
            request.fulfill(path=str(target), content_type=mimetypes.guess_type(target.name)[0]
                            or "application/octet-stream")
        else:
            request.fulfill(status=404, body="Test resource not found")

    page.route("https://tree.test/**", route)
    page.goto(f"https://tree.test/pages/{app}.html?domain=fixture")
    page.wait_for_selector('#tree [data-path="docs"]', state="attached")
    return page


def row(page, path):
    return page.locator(f'#tree [data-path="{path}"]')


def metrics(locator):
    return locator.evaluate("""async el => {
      await Promise.all(el.getAnimations({subtree:true}).map(a => a.finished.catch(() => {})));
      const s = getComputedStyle(el), tree = el.closest('#tree'), t = getComputedStyle(tree);
      const arrow = el.querySelector('.tw svg'), a = arrow.getBoundingClientRect();
      const name = el.querySelector('.nm,.name'), n = getComputedStyle(name);
      return {height:el.getBoundingClientRect().height, font:s.fontFamily, size:s.fontSize,
        weight:s.fontWeight, line:s.lineHeight, gap:s.gap, indent:s.paddingLeft,
        radius:s.borderRadius, margin:s.marginLeft, treePadding:t.padding,
        nameLine:n.lineHeight, ellipsis:n.textOverflow, nowrap:n.whiteSpace,
        arrowWidth:Math.round(a.width * 1000) / 1000,
        arrowHeight:Math.round(a.height * 1000) / 1000, fill:arrow.getAttribute('fill'),
        stroke:arrow.getAttribute('stroke'), strokeWidth:arrow.getAttribute('stroke-width'),
        path:arrow.querySelector('path').getAttribute('d')};
    }""")


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width", [1280, 700])
def test_tree_metrics_match_file_browser_and_keep_icons(browser, theme, width):
    context = browser.new_context(viewport={"width": width, "height": 800}, color_scheme=theme)
    ide = open_app(context, "files")
    reference = open_app(context, "file-browser")
    assert ide.locator("html").get_attribute("data-theme") == theme
    assert reference.locator("html").get_attribute("data-theme") == theme
    assert metrics(row(ide, "docs")) == metrics(row(reference, "docs"))
    assert metrics(row(ide, "docs"))["height"] == 27
    assert metrics(row(ide, "docs"))["weight"] == "400"
    for page in (ide, reference):
        row(page, "docs").click()
        row(page, "docs/reference").wait_for(state="visible")
        row(page, "docs/reference").click()
        row(page, "docs/reference/intro.md").wait_for(state="visible")
    for path in ("docs/reference", "docs/reference/intro.md", LONG_NAME):
        assert metrics(row(ide, path)) == metrics(row(reference, path))
    assert metrics(row(ide, "docs/reference/intro.md"))["indent"] == "33px"
    # IDE 全部目录使用指定蓝色原图，Seti 文件图标仍保持原样。
    folder = row(ide, "docs").locator(".folder-icon")
    assert "is-open" in folder.get_attribute("class")
    assert folder.evaluate("el => getComputedStyle(el).width") == "15px"
    assert folder.evaluate("el => getComputedStyle(el).height") == "15px"
    assert folder.get_attribute("src") == "/icons/ide-folder-open-original-b95c700a1abe.png"
    assert folder.evaluate("async el => { await el.decode(); return el.naturalWidth; }") == 512
    assert folder.evaluate("el => getComputedStyle(el).filter") == "none"
    glyph = row(ide, "README.md").locator(".seti-file-icon")
    assert glyph.evaluate("el => getComputedStyle(el).fontFamily").strip('"') == 'Desk Seti'
    assert glyph.evaluate("el => getComputedStyle(el).fontSize") == "16px"
    assert row(ide, "empty").locator(".folder-icon.is-empty").count() == 1
    for page in (ide, reference):
        row(page, "docs").click()
        assert row(page, "docs/reference/intro.md").is_hidden()
        assert page.locator("#tree").evaluate("el => el.scrollWidth <= el.clientWidth")
    context.close()


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("size,touch", [((1280, 800), False), ((700, 800), False),
                                      ((320, 640), True), ((390, 844), True), ((844, 390), True)])
def test_search_field_visual_size_focus_and_search_behavior(browser, theme, size, touch):
    context = browser.new_context(viewport={"width": size[0], "height": size[1]},
                                  is_mobile=touch, has_touch=touch, color_scheme=theme)
    page = open_app(context, "files")
    if touch and not page.locator("body").evaluate("el => el.classList.contains('side-open')"):
        page.locator("#m-side").tap()
    field = page.locator("#search-field")
    field.wait_for(state="visible")
    q = page.get_by_role("searchbox", name="搜索文件与内容")
    assert field.bounding_box()["height"] == (36 if touch else 32)
    visual = q.evaluate("""el => {
      const s = getComputedStyle(el), r = el.getBoundingClientRect();
      return {font:parseFloat(s.fontSize), visualFont:parseFloat(s.fontSize)*r.width/el.offsetWidth,
        right:r.right, fieldRight:el.closest('label').getBoundingClientRect().right,
        overflow:document.documentElement.scrollWidth > innerWidth};
    }""")
    assert visual["font"] == (16 if touch else 13)
    assert visual["visualFont"] == pytest.approx(13, abs=.1)
    assert visual["right"] < visual["fieldRight"]
    assert not visual["overflow"]
    q.focus()
    # 用同一颜色变量的临时元素归一化 rgb / hex 表示。
    assert field.evaluate("""el => {
      const probe = document.createElement('span'); probe.style.color = 'var(--gh-fg-muted)';
      el.append(probe); const match = getComputedStyle(el).borderTopColor === getComputedStyle(probe).color;
      probe.remove(); return match;
    }""")
    assert q.evaluate("el => getComputedStyle(el).boxShadow") == "none"
    await_search = page.expect_request(lambda request: '/workbench/fs?' in request.url and 'q=guide' in request.url)
    with await_search:
        q.fill("guide")
    page.locator("#results .row").first.wait_for(state="visible")
    assert page.locator("#tree").is_hidden()
    if touch:
        assert page.locator("#results .row").first.bounding_box()["height"] >= 44
    q.fill("")
    assert page.locator("#results").is_hidden()
    assert row(page, "docs").is_visible()
    q.fill("用于检查右端文字及光标位置的很长搜索关键词-search-keyword")
    q.press("End")
    assert q.evaluate("el => el.scrollWidth >= el.clientWidth")
    assert not page.locator("#side").evaluate("el => el.scrollWidth > el.clientWidth")
    context.close()


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("size", [(390, 844), (844, 390)])
def test_touch_tree_density_matches_file_browser_and_keeps_tap_behavior(browser, theme, size):
    context = browser.new_context(viewport={"width": size[0], "height": size[1]}, is_mobile=True,
                                  has_touch=True, device_scale_factor=2, color_scheme=theme)
    page = open_app(context, "files")
    reference = open_app(context, "file-browser")
    if not page.locator("body").evaluate("el => el.classList.contains('side-open')"):
        page.locator("#m-side").tap()
    if not reference.locator("body").evaluate("el => el.classList.contains('side-open')"):
        reference.locator("#side-toggle").tap()
    item = row(page, "docs")
    assert item.is_visible()
    assert item.bounding_box()["height"] == 27
    assert metrics(item) == metrics(row(reference, "docs"))
    assert metrics(item)["path"] == "m4 2 4 4-4 4"
    assert page.locator("#search-field").bounding_box()["height"] == 36
    assert page.locator("#q").bounding_box()["height"] == pytest.approx(34, abs=.1)
    for target in (page, reference):
        row(target, "docs").tap()
        row(target, "docs/guide.md").wait_for(state="visible")
        row(target, "docs/reference").tap()
        row(target, "docs/reference/intro.md").wait_for(state="visible")
    for path in ("docs/reference", "docs/guide.md", "docs/reference/intro.md", "README.md", LONG_NAME):
        assert metrics(row(page, path)) == metrics(row(reference, path))
        assert row(page, path).bounding_box()["height"] == 27
    item.tap()
    assert row(page, "docs/guide.md").is_hidden()
    assert row(page, "README.md").locator(".seti-file-icon").count() == 1
    assert page.locator("#tree").evaluate("el => el.scrollWidth <= el.clientWidth")
    context.close()
