"""一次性打开的页也要有出口。

同源链接点开的渲染页(/workbench/view 之类)既不是应用页——拿不到 app-page 那一对
「收起 / 关闭」;也不归浏览器应用管——browserHandlesUrl 只接外部源。于是它不进标签条、
页头上只有一个标题,打开之后除了切到别的页没有别的走法(2026-08-19 用户实见)。
与当初文件查看页漏掉 app:true 是同一类问题:页有了,来处没了。

会话页与终端页除外:前者自带收起、页头是另一套,后者压根没有页头。
"""
import os
import re
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PAGES = open(os.path.join(ROOT, "frontend", "app-pages.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


class OnceOpenedPageHasAWayOut(unittest.TestCase):
    def test_按页的种类挂标记(self):
        self.assertIn('classList.toggle("once-page"', PAGES)
        seg = PAGES[PAGES.index('classList.toggle("once-page"'):][:260]
        self.assertIn("!p.app", seg)
        self.assertIn('classList.contains("hs-page")', seg)   # 会话页除外
        self.assertIn('p.url.startsWith("term:")', seg)       # 终端页除外

    def test_两枚钮跟应用页同一条规则(self):
        self.assertIn("#overlay.once-page #ov-min, #overlay.once-page #ov-close { display: flex; }", CSS)

    def test_应用页那一支没被改掉(self):
        self.assertIn("#overlay.app-page #ov-min, #overlay.app-page #ov-close,", CSS)

    def test_关闭与收起本来就通用(self):
        """一次性页面在手机和桌面都能收起，不应进入应用侧栏。"""
        self.assertIn("if (pActive) { closePage(pActive); return; }", PAGES)
        minimize = re.search(r"function minimizeMainApp\(\).*?\n}\n", PAGES, re.S).group()
        binding = re.search(r'\{ const m = \$\("#ov-min"\);[^\n]+', PAGES).group()
        code = r'''
const assert = require('assert');
let mobile = false, hidden = 0;
const pActive = {url:'/workbench/view?path=readme.md'}, button = {};
const isMobileView = () => mobile;
const $ = () => button;
function hideOverlay() { hidden++; }
function hideAppSide() { throw Error('一次性页面不应改变应用侧栏'); }
''' + minimize + binding + r'''
button.onclick();
assert.equal(hidden,1);
mobile = true;
button.onclick();
assert.equal(hidden,2);
'''
        result = subprocess.run(['node', '-e', code], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)



if __name__ == "__main__":
    unittest.main()
