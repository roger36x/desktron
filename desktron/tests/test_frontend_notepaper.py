"""TaskNote renames TaskPaper without migrating task data; Timeline leaves the catalog."""
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / 'frontend'


def test_official_catalog_renames_in_place_and_removes_timeline():
    apps = json.loads((FRONT / 'workbench.json').read_text(encoding='utf-8'))['desktopApps']
    assert len(apps) == 7
    assert not any(a.get('page') == 'lark-events' or a.get('label') == 'Timeline' for a in apps)
    note = next(a for a in apps if a['id'] == 'taskpaper')
    assert note['label'] == 'TaskNote'
    assert note['uid'] == 'desktron.taskpaper.31b13a'
    assert note['page'] == 'taskpaper'
    assert note['glass'] and note['chromeless']
    assert note['targets'] == ['_desktop']


def test_page_fallback_names_change_but_registry_and_task_routes_do_not():
    page = (FRONT / 'pages/taskpaper.html').read_text(encoding='utf-8')
    assert 'title="双击改名">TaskNote</div>' in page
    assert 'id="dtitle">TaskNote</div>' in page
    assert "APP.label || 'TaskNote'" in page
    assert "a.page === 'taskpaper'" in page
    assert "'/workbench/domains/'" in page
    assert "'/tasks'" in page
    assert "{ ...APP, label: v }" in page
    assert '>TaskPaper<' not in page


def test_new_and_legacy_names_use_adopted_small_red_checks_without_red_t():
    source = (FRONT / 'app-assets-apps.js').read_text(encoding='utf-8')
    snippet = source[source.index('const _APP_COLORS'):source.index('const _APP_RECENT_MAX')]
    code = "const escapeHtml = s => String(s);\n" + snippet + '''
const cases = [
 ['TaskNote', 'custom', ''],
 ['NotePaper', 'desktron.taskpaper.31b13a', ''],
 ['TaskPaper', 'desktron.taskpaper.31b13a', 'sm'],
 ['待办清单', 'custom', ''],
 ['自定义名称', 'desktron.taskpaper.31b13a', '']
];
console.log(JSON.stringify(cases.map(([name, key, size]) => _appIconHTML(name, key, size, '', 'list'))));
'''
    proc = subprocess.run(['node', '-e', code], text=True, encoding='utf-8', capture_output=True, check=True)
    for html in json.loads(proc.stdout):
        assert 'apg-notepaper' in html
        assert '#fbfaf8' in html and '#f1efe9' in html
        assert '#ef5350' in html
        assert '<circle' not in html
        for y in (14, 24, 34):
            assert f'translate(12.5 {y}) scale(.85)' in html
            assert f'M8.6 {y}l2.6 2.7 5.1-5.5' in html
        assert '#e03b3b' not in html
        assert 'M20.65 24.44' not in html
    assert '[/timeline/i' not in source


@pytest.mark.desktop
@pytest.mark.parametrize('width', [390, 820, 1280])
@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_renamed_page_still_loads_existing_task_route(width, theme):
    from playwright.sync_api import sync_playwright

    html = (FRONT / 'pages/taskpaper.html').read_text(encoding='utf-8')
    with sync_playwright() as pw:
        browser = pw.webkit.launch()
        try:
            page = browser.new_page(viewport={'width': width, 'height': 600})
            errors, requests = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def route(request):
                url = request.request.url
                requests.append((request.request.method, url))
                if url.endswith('/pages/taskpaper.html'):
                    request.fulfill(content_type='text/html', body=html)
                elif url.endswith('/workbench/apps'):
                    request.fulfill(json={})
                elif url.endswith('/workbench/domains'):
                    request.fulfill(json=[{'id': 'example', 'name': '示例工作流'}])
                elif url.endswith('/workbench/domains/example/tasks'):
                    request.fulfill(json={'items': [{'line': 1, 'title': '示例任务', 'state': 'todo'}], 'total': 1, 'done': 0})
                else:
                    request.fulfill(status=404, body='Unexpected test request')

            page.route('https://desk.test/**', route)
            page.goto('https://desk.test/pages/taskpaper.html')
            if theme == 'light':
                page.evaluate("document.body.classList.add('light')")
            page.locator('.item .txt').wait_for()
            assert page.locator('#appname').text_content() == 'TaskNote'
            assert page.locator('.item .txt').text_content() == '示例任务'
            assert any(url.endswith('/workbench/domains/example/tasks') for _, url in requests)
            assert all(method == 'GET' for method, _ in requests)
            assert not errors
        finally:
            browser.close()
