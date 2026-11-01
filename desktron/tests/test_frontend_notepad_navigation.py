"""Notepad navigation uses synthetic notes, never the installed user's store."""
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "frontend/pages/notepad.html").read_text(encoding="utf-8")


def test_clicking_selected_note_still_opens_detail():
    handler = re.search(r"el\('noteList'\)\.addEventListener\('click', e => \{.*?\n\}\);", SOURCE, re.S).group()
    script = """
let activeId = 'demo', level = 'list', saves = 0;
const item = {dataset: {id: 'demo'}};
const el = () => ({addEventListener: (_, fn) => fn({target: {closest: () => item}})});
const save = () => saves++;
const renderList = () => {}, renderEditor = () => {};
const mGo = value => level = value;
""" + handler + "\nif (level !== 'note') throw Error('selected note did not open');"
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


@pytest.fixture
def page():
    pw_module = pytest.importorskip("playwright.sync_api")
    with pw_module.sync_playwright() as pw:
        browser = pw.webkit.launch()
        page = browser.new_page(viewport={"width": 700, "height": 900})
        writes = []

        def route(request):
            if "/workbench/notepad" in request.request.url:
                if request.request.method != "GET":
                    writes.append(request.request.post_data_json)
                request.fulfill(json={"notes": [
                    {"id": "demo", "content": "示例备忘录\n用于导航测试的正文", "updated": "2026-10-08T10:00:00Z"},
                    {"id": "other", "content": "另一篇示例\n第二篇正文", "updated": "2026-10-07T10:00:00Z"},
                ], "activeId": "demo", "folders": []})
            else:
                request.fulfill(body=SOURCE, content_type="text/html")

        page.route("http://notepad.test/**", route)
        page.writes = writes
        yield page
        browser.close()


def open_notepad(page, width=700, compact=False, stored=False):
    page.set_viewport_size({"width": width, "height": 900})
    if stored:
        page.add_init_script("""
localStorage.setItem('notepad_thinking_side_collapsed', '1');
localStorage.setItem('notepad_thinking_side_w', '290');
localStorage.setItem('notepad_thinking_list_w', '470');
""")
    page.goto("http://notepad.test/notepad")
    if compact:
        page.evaluate("window.postMessage({type:'desktron-layout',mode:'compact'}, location.origin)")
        page.wait_for_function("document.documentElement.dataset.layout === 'compact'")
    page.wait_for_function("document.querySelectorAll('.note-item').length === 2")


def assert_single_pane(page, selector):
    pane = page.locator(selector)
    assert pane.is_visible()
    rect = pane.bounding_box()
    assert rect["width"] > page.viewport_size["width"] - 2
    assert rect["x"] >= 0 and rect["x"] + rect["width"] <= page.viewport_size["width"] + 1
    for other in (".sidebar", ".list-pane", ".editor-pane"):
        if other != selector:
            assert not page.locator(other).is_visible()


@pytest.mark.desktop
@pytest.mark.parametrize("width,compact,stored", [(390, False, False), (700, True, False),
                                                 (900, True, False), (700, True, True)])
def test_selected_note_and_folder_collapse_never_blank(page, width, compact, stored):
    open_notepad(page, width, compact, stored)
    if page.locator(".editor-pane").is_visible():
        page.locator("#mBackList").click()
    assert_single_pane(page, ".list-pane")
    page.locator('.note-item[data-id="demo"]').click()
    assert_single_pane(page, ".editor-pane")
    assert "用于导航测试的正文" in page.locator("#editor").inner_text()
    page.locator("#mBackList").click()
    page.locator("#mBackFolders").click()
    assert_single_pane(page, ".sidebar")
    page.locator("#btnSideCollapse").click()
    assert_single_pane(page, ".list-pane")
    page.locator('.note-item[data-id="demo"]').click()
    assert_single_pane(page, ".editor-pane")


@pytest.mark.desktop
@pytest.mark.parametrize("stored", [False, True])
def test_folder_selection_with_saved_desktop_preferences(page, stored):
    open_notepad(page, 700, True, stored)
    if page.locator(".editor-pane").is_visible():
        page.locator("#mBackList").click()
    page.locator("#mBackFolders").click()
    page.locator('.side-row[data-view="folder"]').click()
    assert_single_pane(page, ".list-pane")
    page.locator('.note-item[data-id="other"]').click()
    assert_single_pane(page, ".editor-pane")
    assert "第二篇正文" in page.locator("#editor").inner_text()


@pytest.mark.desktop
def test_compact_layout_arrives_before_notes_load(page):
    pending = []
    page.route("**/workbench/notepad?*", lambda route: pending.append(route))
    page.goto("http://notepad.test/notepad", wait_until="domcontentloaded")
    page.evaluate("window.postMessage({type:'desktron-layout',mode:'compact'}, location.origin)")
    page.wait_for_function("document.documentElement.dataset.layout === 'compact'")
    assert len(pending) == 1
    pending[0].fulfill(json={"notes": [{"id": "demo", "content": "示例标题\n延迟加载正文",
                                     "updated": "2026-10-08T10:00:00Z"}], "folders": []})
    page.wait_for_function("document.querySelectorAll('.note-item').length === 1")
    assert_single_pane(page, ".editor-pane")
    assert "延迟加载正文" in page.locator("#editor").inner_text()


@pytest.mark.desktop
def test_resize_and_host_round_trip_preserve_selection(page):
    open_notepad(page, 1200, False, True)
    assert not page.locator(".sidebar").is_visible()
    assert page.locator(".list-pane").is_visible() and page.locator(".editor-pane").is_visible()
    page.locator("#btnSideExpand").click()
    assert page.locator(".sidebar").is_visible()
    page.locator("#btnSideCollapse").click()
    page.evaluate("window.postMessage({type:'desktron-layout',mode:'compact'}, location.origin)")
    page.wait_for_function("document.documentElement.dataset.layout === 'compact'")
    assert_single_pane(page, ".editor-pane")
    page.locator("#mBackList").click()
    page.locator("#mBackFolders").click()
    assert_single_pane(page, ".sidebar")
    page.evaluate("window.postMessage({type:'desktron-layout',mode:'main'}, location.origin)")
    page.wait_for_function("document.documentElement.dataset.layout === 'main'")
    assert not page.locator(".sidebar").is_visible()
    assert page.locator(".editor-pane").is_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_function("document.querySelector('.app').classList.contains('stack-layout')")
    assert_single_pane(page, ".sidebar")
    page.locator("#btnSideCollapse").click()
    assert_single_pane(page, ".list-pane")
    page.locator('.note-item[data-id="demo"]').click()
    assert_single_pane(page, ".editor-pane")
    assert "用于导航测试的正文" in page.locator("#editor").inner_text()


@pytest.mark.desktop
def test_navigation_does_not_change_note_content(page):
    open_notepad(page)
    page.locator('.note-item[data-id="other"]').click()
    page.locator("#mBackList").click()
    page.locator("#mBackFolders").click()
    page.locator("#btnSideCollapse").click()
    page.wait_for_timeout(300)
    assert all([n["content"] for n in write["notes"]] ==
               ["示例备忘录\n用于导航测试的正文", "另一篇示例\n第二篇正文"] for write in page.writes)
