"""Monaco 文件应用在 Desk 紧凑侧栏中默认收树，并分开保存两种布局的手动状态。"""
from pathlib import Path
import mimetypes
import unittest
import pytest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "frontend/pages/files.html"
VENDOR = ROOT / "frontend/app-assets/files/vendor"


@unittest.skipUnless(sync_playwright, "需要 Playwright 与 WebKit")
@pytest.mark.desktop
class FilesAppCompactSidebar(unittest.TestCase):
    def setUp(self):
        self.pw = sync_playwright().start()
        self.browser = self.pw.webkit.launch()
        self.page = self.browser.new_page(viewport={"width": 1200, "height": 800})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

        def route(request):
            url = request.request.url
            if "/pages/files.html" in url:
                request.fulfill(content_type="text/html", body=PAGE.read_text())
                return
            if "/app-assets/files/vendor/" in url:
                name = url.split("/app-assets/files/vendor/", 1)[1].split("?", 1)[0]
                target = VENDOR / name
                if target.is_file():
                    request.fulfill(
                        content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
                        body=target.read_bytes(),
                    )
                else:
                    request.abort()
                return
            if "/vendor/fonts/" in url:
                request.abort()
                return
            if "/workbench/fs" in url:
                request.fulfill(content_type="application/json", body='{"items":[]}')
                return
            mode = "compact" if "compact" in url else "main"
            request.fulfill(content_type="text/html", body=(
                f'<div data-layout="{mode}"><iframe src="/pages/files.html?domain=_desktop" '
                'style="width:700px;height:600px"></iframe></div>'
            ))

        self.page.route("http://files.test/**", route)

    def tearDown(self):
        self.browser.close()
        self.pw.stop()

    def frame(self, mode):
        self.page.goto("http://files.test/" + mode)
        frame = self.page.frames[1]
        frame.wait_for_selector("#m-side")
        frame.wait_for_function("document.body.classList.contains('side-collapsed') === (document.documentElement.dataset.layout === 'compact')")
        return frame

    def layout(self, mode):
        self.page.evaluate("""mode => {
            const f = document.querySelector('iframe');
            f.parentElement.dataset.layout = mode;
            f.contentDocument.documentElement.dataset.layout = mode;
            f.contentWindow.postMessage({type:'desktron-layout', mode}, location.origin);
        }""", mode)
        self.page.wait_for_timeout(40)

    def test_layouts_keep_independent_sidebar_state(self):
        frame = self.frame("compact")
        side = frame.locator("#side")
        toggle = frame.locator("#m-side")

        self.assertFalse(side.is_visible())
        self.assertEqual(toggle.get_attribute("aria-expanded"), "false")

        toggle.click()
        self.layout("compact")
        self.assertTrue(side.is_visible())

        self.layout("main")
        self.assertTrue(side.is_visible())
        toggle.click()
        self.assertFalse(side.is_visible())

        self.layout("compact")
        self.assertTrue(side.is_visible())
        self.layout("main")
        self.assertFalse(side.is_visible())
        # WebKit 的无权限剪贴板与页面卸载时取消 worker 是 Monaco 自身的测试环境噪声。
        unexpected = [e for e in self.errors if "navigator.clipboard.write" not in e and e != "Canceled: Canceled"]
        self.assertEqual(unexpected, [])


if __name__ == "__main__":
    unittest.main()
