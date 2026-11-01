"""文件浏览器紧凑布局默认收树，手动展开不被重复通知覆盖。"""
from pathlib import Path
import unittest
import pytest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

PAGE = Path(__file__).resolve().parents[2] / "frontend/pages/file-browser.html"


@unittest.skipUnless(sync_playwright, "需要 Playwright 与 WebKit")
@pytest.mark.desktop
class FileBrowserCompactTree(unittest.TestCase):
    def setUp(self):
        self.pw = sync_playwright().start()
        self.browser = self.pw.webkit.launch()
        self.page = self.browser.new_page(viewport={"width": 1200, "height": 800})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

        def route(request):
            url = request.request.url
            if '/file-browser' in url:
                request.fulfill(content_type='text/html', body=PAGE.read_text())
            elif '/workbench/fs' in url:
                request.fulfill(content_type='application/json', body='{"items":[]}')
            elif '/vendor/' in url:
                request.fulfill(content_type='text/javascript', body='')
            else:
                mode = 'compact' if 'compact' in url else 'main'
                request.fulfill(content_type='text/html', body=(
                    f'<div data-layout="{mode}"><iframe src="/file-browser" '
                    'style="width:800px;height:600px"></iframe></div>'))
        self.page.route('http://files.test/**', route)

    def tearDown(self):
        self.browser.close()
        self.pw.stop()

    def frame(self, mode):
        self.page.goto('http://files.test/' + mode)
        frame = self.page.frames[1]
        frame.wait_for_selector('#side-toggle')
        return frame

    def layout(self, mode):
        self.page.evaluate('''mode => {
            const f = document.querySelector('iframe');
            f.parentElement.dataset.layout = mode;
            f.contentDocument.documentElement.dataset.layout = mode;
            f.contentWindow.postMessage({type:'desktron-layout', mode}, location.origin);
        }''', mode)
        self.page.wait_for_timeout(40)

    def test_compact_default_and_manual_expansion(self):
        frame = self.frame('compact')
        self.assertFalse(frame.locator('#side').is_visible())
        self.assertFalse(frame.locator('#side-divider').is_visible())
        self.assertEqual(frame.locator('#side-toggle').get_attribute('aria-expanded'), 'false')
        frame.locator('#side-toggle').click()
        self.layout('compact')
        self.assertTrue(frame.locator('#side').is_visible())
        self.page.evaluate("document.querySelector('iframe').contentWindow.postMessage({type:'file-browser-open', root:'_desktop', path:''}, location.origin)")
        self.page.wait_for_timeout(40)
        self.assertTrue(frame.locator('#side').is_visible())
        self.layout('main')
        self.layout('compact')
        self.assertTrue(frame.locator('#side').is_visible())
        self.assertEqual(self.errors, [])

    def test_main_default_and_return_from_compact(self):
        frame = self.frame('main')
        self.assertTrue(frame.locator('#side').is_visible())
        self.layout('compact')
        self.assertFalse(frame.locator('#side').is_visible())
        self.layout('main')
        self.assertTrue(frame.locator('#side').is_visible())
        frame.locator('#side-toggle').click()
        self.layout('compact')
        self.layout('main')
        self.assertFalse(frame.locator('#side').is_visible())
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
