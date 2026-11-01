"""生产删除/恢复函数的 WebKit 交互，不连接用户会话。"""
from pathlib import Path
import re
import unittest
import pytest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

FRONT = Path(__file__).resolve().parents[2] / "frontend"


@unittest.skipUnless(sync_playwright, "需要 Playwright 和 WebKit")
@pytest.mark.desktop
class SessionDeleteWebKit(unittest.TestCase):
    def setUp(self):
        self.pw = sync_playwright().start()
        self.browser = self.pw.webkit.launch()
        self.page = self.browser.new_page()
        self.page.set_content('<div id="row">保留的会话</div>')
        self.page.add_script_tag(content='''
window.calls=[]; window.alerts=[]; window.opened=[]; window.fail=false;
const expandedProjects=new Set(); const curEngine='codex';
window.confirm=()=>true;
async function api(path,opts) { calls.push([path,opts.method]); if(fail) throw Error('409 session_busy'); }
function alertLike(text) { alerts.push(text); }
function disposeTermPane(id) { document.getElementById('row')?.remove(); }
async function refreshTerms() {} function _rerenderSessionsHost() {}
function persistExpanded() {} function renderSessionPanel() {}
async function openHtmlSession(id) { opened.push(id); }
async function attachTerminal(id) { opened.push(id); }
''')
        for file, name in (("app-panel-sidebar.js", "closeTermSession"),
                           ("app-assets-create.js", "resumeSession")):
            source = (FRONT / file).read_text()
            fn = re.search(r"async function " + name + r"\([^\n]*\)[\s\S]*?\n}\n", source)
            self.assertIsNotNone(fn)
            self.page.add_script_tag(content=fn.group())

    def tearDown(self):
        self.browser.close()
        self.pw.stop()

    def test_delete_409_keeps_row_and_allows_retry(self):
        self.page.evaluate("fail=true; closeTermSession({id:'adp_1',title:'会话'},true)")
        self.assertEqual(self.page.locator('#row').count(), 1)
        self.assertIn('409', self.page.evaluate('alerts[0]'))
        self.page.evaluate("fail=false; closeTermSession({id:'adp_1',title:'会话'},true)")
        self.assertEqual(self.page.locator('#row').count(), 0)
        self.assertEqual(self.page.evaluate('calls'), [
            ['/workbench/terminals/adp_1', 'DELETE'], ['/workbench/terminals/adp_1', 'DELETE']])

    def test_close_uses_keep_instead_of_permanent_delete(self):
        self.page.evaluate("closeTermSession({id:'native-1',title:'会话'},false)")
        self.assertEqual(self.page.evaluate('calls'), [['/workbench/terminals/native-1?keep=1', 'DELETE']])

    def test_resume_restores_both_id_routes_before_opening(self):
        for sid in ('adp_1', 'native-1'):
            self.page.evaluate("sid=>resumeSession(sid,'work','codex')", sid)
        self.assertEqual(self.page.evaluate('calls'), [
            ['/workbench/terminals/adp_1/reopen', 'POST'], ['/workbench/terminals/native-1/reopen', 'POST']])
        self.assertEqual(self.page.evaluate('opened'), ['adp_1', 'native-1'])

    def test_resume_failure_does_not_open_a_hidden_session(self):
        self.page.evaluate("fail=true; resumeSession('adp_1','work','codex')")
        self.assertEqual(self.page.evaluate('opened'), [])
        self.assertTrue(self.page.evaluate('alerts[0].startsWith("恢复失败")'))
