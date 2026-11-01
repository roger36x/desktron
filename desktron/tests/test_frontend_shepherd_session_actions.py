"""Shepherd 会话操作：新建进入、引擎文案与契约路由回归。"""
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
PAGE = (ROOT / "frontend/app-chat-page.js").read_text(encoding='utf-8')
PAGES = (ROOT / "frontend/app-pages.js").read_text(encoding='utf-8')
TOPNAV = (ROOT / "frontend/app-rail-topnav.js").read_text(encoding='utf-8')
OPTS = (ROOT / "frontend/app-chat-opts.js").read_text(encoding='utf-8')
CORE = (ROOT / "frontend/app-chat-core.js").read_text(encoding='utf-8')


def function(name):
    start = PAGE.index("async function " + name + "(")
    brace = PAGE.index("{", start)
    depth = 0
    for pos in range(brace, len(PAGE)):
        if PAGE[pos] == "{": depth += 1
        elif PAGE[pos] == "}":
            depth -= 1
            if depth == 0: return PAGE[start:pos + 1]
    raise AssertionError(name + " not closed")


@unittest.skipUnless(shutil.which("node"), "需要 node")
class NewSessionActivation(unittest.TestCase):
    def test_late_restore_cannot_overwrite_created_session(self):
        script = r'''
const assert=require('node:assert/strict');
let pages=[],shown='',curEngine='codex',shepherdMode=true,epoch=0;
function curDomainId(){return 'desk-iter';}
function HS_PAGE(sid){return 'chat:'+sid;}
function claimMainNavigation(){epoch+=1;return epoch;}
function ownsMainNavigation(candidate){return candidate===undefined||candidate===null||candidate===epoch;}
function showOverlay(page,candidate){if(!ownsMainNavigation(candidate))return false;shown=page.url;return true;}
async function api(){return {id:'adp_new'};}
async function openHtmlSession(sid,_title,_show,opts){
 const page={url:HS_PAGE(sid)};pages.push(page);showOverlay(page,opts.navigationEpoch);
}
FN
(async()=>{
 const restoreEpoch=epoch;
 const got=await newHtmlSession({engine:'codex'});
 assert.equal(got.id,'adp_new');
 assert.equal(shown,'chat:adp_new');
 assert.equal(showOverlay({url:'chat:old-snapshot'},restoreEpoch),false);
 assert.equal(shown,'chat:adp_new');
 // 后续显式导航仍可推进代际，不会被新会话永久锁住。
 const later=claimMainNavigation();
 assert.equal(showOverlay({url:'chat:later'},later),true);
 assert.equal(shown,'chat:later');
})().catch(e=>{console.error(e);process.exit(1)});
'''.replace("FN", function("newHtmlSession"))
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_explicit_overlay_navigation_claims_new_epoch(self):
        script = r'''
const assert=require('node:assert/strict');
let epoch=0,shown='';
function claimMainNavigation(){epoch+=1;return epoch;}
function ownsMainNavigation(candidate){return candidate===undefined||candidate===null||candidate===epoch;}
function body(page,candidate){
  if (candidate===undefined) candidate=claimMainNavigation();
  if (!ownsMainNavigation(candidate)) return false;
  shown=page.url;return true;
}
const restoreEpoch=epoch;
assert.equal(body({url:'chat-draft'}),true); // openDraftChat 走无 epoch 的显式入口
assert.equal(shown,'chat-draft');
assert.equal(body({url:'chat:old'},restoreEpoch),false);
assert.equal(shown,'chat-draft');
assert.equal(body({url:'browser'}),true); // 其他显式页面也推进代际
assert.equal(shown,'browser');
'''
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_restore_paths_carry_navigation_epoch(self):
        self.assertIn("function showOverlay(p)", PAGES)
        self.assertIn("arguments.length > 1 ? arguments[1] : undefined", PAGES)
        self.assertIn('navigationEpoch === undefined', PAGES)
        self.assertIn('navigationEpoch = claimMainNavigation()', PAGES)
        self.assertIn('!ownsMainNavigation(navigationEpoch)) return false', PAGES)
        self.assertIn("showOverlay(back, navigationEpoch)", PAGES)
        self.assertIn('typeof currentMainNavigation === "function"', TOPNAV)
        self.assertIn("? currentMainNavigation() : null", TOPNAV)
        self.assertIn("showOverlay(p, navigationEpoch)", TOPNAV)
        self.assertIn("claimMainNavigation()", PAGE)
        self.assertIn('{ navigationEpoch, domain: sessionDomain, cwd: s.cwd || "" }', PAGE)


class ReconciliationAction(unittest.TestCase):
    def test_config_surfaces_evidence_preserving_action(self):
        self.assertIn('const reconciliation = (v.execution || {}).reconciliation', OPTS)
        self.assertIn('label: "隔离无法验证内容并继续"', OPTS)
        self.assertIn('attemptID: reconciliation.attempt_id', OPTS)
        self.assertIn('reconciliation.reason === "source_shape_unsupported"', OPTS)
        self.assertIn('当前 Desktron 无法识别 Claude 的新记录格式', OPTS)
        self.assertIn('请升级 Desktron 后重新打开会话', OPTS)

    def test_missing_reconciliation_clears_stale_recovery_banner(self):
        self.assertIn('if (v.envStage === "recovery")', OPTS)
        self.assertIn('hsEnvironmentHide(v, true)', OPTS)
        self.assertLess(OPTS.index('if (v.envStage === "recovery")'),
                        OPTS.index('hsEnvironmentSnapshot(v, cf.environment, cf.environment_revision)'))

    def test_action_is_confirmed_and_attempt_scoped(self):
        self.assertIn('async function hsShepherdReconcile', CORE)
        self.assertIn('confirm("隔离这轮无法验证的 Shepherd 内容？', CORE)
        self.assertIn('/reconcile`', CORE)
        self.assertIn('action: "omit_and_continue", attempt_id: attemptID', CORE)
        self.assertIn('kind === "shepherd-reconcile"', CORE)


class EngineCopy(unittest.TestCase):
    def test_menu_uses_short_engine_label(self):
        self.assertIn('label: DeskI18n.t("model.engine")', OPTS)
        self.assertNotIn('label: "执行引擎"', OPTS)


if __name__ == "__main__": unittest.main()
