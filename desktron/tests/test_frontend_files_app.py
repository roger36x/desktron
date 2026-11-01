"""文件查看成为一个应用(界面模型改造第三步)。

文件查看原先每个工作流就在全局标签条里多一张 files: 页。现在它们收进「文件」这个应用:
应用只有一个页面,顶栏是打开着的项目目录的标签条(2026-08-19 起,形制与浏览器/终端共用),
切目录就在这一页里换 iframe 的地址,不为每个目录各留一份 Monaco。与终端应用同构,差别都源于
内容是一张同源页面(pages/files.html,Monaco 编辑器)而不是 xterm——没有 PTY 要收。
"""
import json
import os
import re
import shutil
import subprocess
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FRONT = os.path.join(ROOT, "frontend")
FILES = open(os.path.join(FRONT, "app-files.js"), encoding="utf-8").read()
PAGES = open(os.path.join(FRONT, "app-pages.js"), encoding="utf-8").read()
SETTINGS = _srcmap.settings_src()
PANEL = _srcmap.panel_src()
INDEX = open(os.path.join(FRONT, "index.html"), encoding="utf-8").read()
CSS = open(os.path.join(FRONT, "style.css"), encoding="utf-8").read()


def _fn(src, name):
    m = re.search(rf"\n(?:async )?function {name}\(.*?\n}}\n", src, re.S)
    assert m, name
    return m.group(0)


class Wiring(unittest.TestCase):
    def test_script_is_loaded_after_pages(self):
        self.assertIn('<script src="/app-files.js" defer></script>', INDEX)
        self.assertLess(INDEX.index("/app-pages.js"), INDEX.index("/app-files.js"))

    def test_one_page_per_app(self):
        # 页 key 固定,整个应用只有一个页面:再点别的工作流是在这一页里换,不是多开一页
        self.assertIn('const FILES_PAGE_KEY = "files";', FILES)
        self.assertIn("pages.find((x) => x.url === FILES_PAGE_KEY)", FILES)

    def test_app_page_is_registered_as_app_with_uid(self):
        # 应用页(不进标签条,页头带收起/关闭,程序坞按 appid 认运行态),与终端应用同一套宿主
        body = _fn(FILES, "filesEnsurePage")
        self.assertIn('openContentPage(FILES_PAGE_KEY, "文件"', body)
        self.assertIn("{ app: true, appid: filesAppUid() }", body)
        self.assertIn("p.files = true", body)

    def test_head_is_the_app_own_top_bar(self):
        build = _fn(FILES, "_filesBuild")
        self.assertIn("host._headNode = head", build)   # 页头插槽:住进 #overlay-title 的位置
        self.assertIn("fa-tabs", build)      # 顶栏装的是目录标签条
        self.assertNotIn("fa-name", build)   # 单个名字那一版已经撤了
        self.assertIn(".overlay-content.files-host { padding: 0; overflow: hidden; }", CSS)
        self.assertIn("#overlay-title:has(.fa-head)", CSS)

    def test_entry_points_keep_their_signature(self):
        # 实现从 app-settings.js 搬进应用,各处入口(面板的文件夹钮、右键菜单、会话 cwd)照旧
        self.assertNotIn("function openFilesPage", SETTINGS)
        self.assertIn("function openFilesPage(project, show = true) {", FILES)
        self.assertIn("openFilesPage({ id: p.id, name: p.name })", PANEL)        # 工作流的右键菜单
        self.assertIn('openFilesPage({ id: "abs:" + s.cwd', PANEL)              # 会话 cwd 那一路

    def test_page_loads_only_once_it_is_visible(self):
        show = re.search(r"function showOverlay\(p\).*?\n}\n", PAGES, re.S).group(0)
        self.assertIn('if (p.files && typeof filesShown === "function") filesShown();', show)
        body = _fn(FILES, "filesShown")
        self.assertIn('if ((f.getAttribute("src") || "") !== want) f.setAttribute("src", want);', body)

    def test_closing_the_app_forgets_the_current_workflow(self):
        self.assertIn('if (p.files && typeof filesPageClosed === "function") filesPageClosed();',
                      _fn(PAGES, "closePage"))
        self.assertIn('localStorage.removeItem("cd_files")', _fn(FILES, "filesPageClosed"))


class Persistence(unittest.TestCase):
    def test_current_workflow_persists_apart_from_the_global_stack(self):
        # 应用页不进 cd_pages(persistPages 对 app 页返回 null),当前工作流自己存一份
        self.assertIn('localStorage.setItem("cd_files"', _fn(FILES, "persistFilesPage"))
        self.assertIn('localStorage.getItem("cd_files")', _fn(FILES, "restoreFilesPage"))
        self.assertIn('if (typeof restoreFilesPage === "function") restoreFilesPage();',
                      _fn(PAGES, "restorePagesAndBrowser"))

    def test_old_files_tabs_migrate_into_the_app(self):
        # 旧版本存在 cd_pages 里的 files 条目转交给应用,收拢成一个页面
        self.assertIn('if (typeof filesAdoptLegacy === "function") filesAdoptLegacy(items);',
                      _fn(PAGES, "restoreFilesPages"))
        self.assertIn("restoreFilesPages(files)", _fn(PAGES, "restorePages"))

    def test_files_pages_no_longer_occupy_a_global_tab(self):
        # 全局标签条那一侧不再产出 files: 这种页:既不落盘也不按它重建
        self.assertNotIn("files", _fn(PAGES, "persistPages"))
        self.assertNotIn("files", _fn(PAGES, "_tombstone"))
        self.assertNotIn("openFilesPage", PAGES)


HARNESS = r"""
const store = {};
const localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
const srcLog = [];
const frame = {
  src: "",
  getAttribute() { return this.src; },
  setAttribute(k, v) { this.src = v; srcLog.push(v); },
};
// 标签条:只需记住挂上去的那几个标签,够快照读名字
const strip = {
  kids: [],
  set innerHTML(_v) { this.kids = []; },
  appendChild(x) { this.kids.push(x); },
};
const escapeHtml = (x) => String(x);
const ICONS = { close: "×", plus: "+" };
function openCtxMenu() {}
// 标签条的拖动改位(app-pages.js)在这份桩里不参与:它只挂事件,不影响次序与落盘
function psTabsBindDrag() {}
function closePage(p) { pages = pages.filter((x) => x !== p); filesPageClosed(); }
const document = {
  createElement: () => ({
    className: "", innerHTML: "", title: "", dataset: {}, _name: "",
    set onclick(_f) {}, appendChild() {},
    querySelector: (s) => (s === ".fa-tabs" ? strip : { set onclick(_f) {} }),
  }),
};
const domains = [{ id: "sre", name: "事故应急" }, { id: "kb", name: "知识管理" }];
const desktopApps = [];
let pages = [], pActive = null, overlayOpen = false;
function openContentPage(key, title, glyph, build, show, opts) {
  const el = {
    classList: { add() {} }, innerHTML: "", _headNode: null,
    querySelector: (s) => (s === ".fa-frame" ? frame : null),
  };
  pages.push({ url: key, title, content: true, app: !!opts.app, appid: opts.appid, el });
  build(el);
}
function showOverlay(p) { pActive = p; overlayOpen = true; filesShown(); }
SRC
const out = [];
const snap = (name) => out.push([name, {
  pages: pages.length, src: frame.src,
  tabs: filesRoots.map((x) => x.name), cur: filesCurrent ? filesCurrent.name : "",
  saved: store.cd_files || "", loads: srcLog.length,
}]);
openFilesPage({ id: "sre", name: "事故应急" });
snap("open-sre");
openFilesPage({ id: "kb", name: "旧名" });
snap("switch-kb");
openFilesPage({ id: "kb", name: "知识管理" });
snap("same-again");
filesActivateRoot("sre");
snap("back-sre");
filesCloseRoot("sre");
snap("closed-sre");
filesPageClosed();
snap("closed");
// 刷新:cd_files 已随关闭清掉,旧版本留在 cd_pages 里的两条 files 条目迁移过来
pages = []; pActive = null; overlayOpen = false; frame.src = ""; srcLog.length = 0;
filesRoots = []; filesCurrent = null;
filesAdoptLegacy([{ kind: "files", id: "sre", name: "事故应急" }, { kind: "files", id: "kb", name: "知识管理" }]);
restoreFilesPage();
snap("migrated");
showOverlay(pages[0]);
snap("migrated-shown");
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class RootTabsRuntime(unittest.TestCase):
    def setUp(self):
        js = HARNESS.replace("SRC", FILES)
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.steps = dict(json.loads(out.stdout))

    def test_switching_workflow_stays_in_the_same_page(self):
        self.assertEqual(self.steps["open-sre"]["pages"], 1)
        self.assertEqual(self.steps["switch-kb"]["pages"], 1)   # 不是再开一页
        self.assertIn("domain=sre", self.steps["open-sre"]["src"])
        self.assertIn("domain=kb", self.steps["switch-kb"]["src"])

    def test_head_shows_the_open_roots(self):
        self.assertEqual(self.steps["open-sre"]["tabs"], ["事故应急"])
        # 名字以工作流列表为准:调用方给的是旧名,标签上显示的仍是现在的名字
        self.assertEqual(self.steps["switch-kb"]["tabs"], ["事故应急", "知识管理"])
        self.assertEqual(self.steps["switch-kb"]["cur"], "知识管理")

    def test_reopening_does_not_duplicate_a_tab(self):
        self.assertEqual(self.steps["same-again"]["tabs"], ["事故应急", "知识管理"])

    def test_switching_back_reloads_that_root(self):
        self.assertEqual(self.steps["back-sre"]["cur"], "事故应急")
        self.assertIn("domain=sre", self.steps["back-sre"]["src"])

    def test_closing_one_falls_back_to_a_neighbour(self):
        self.assertEqual(self.steps["closed-sre"]["tabs"], ["知识管理"])
        self.assertEqual(self.steps["closed-sre"]["cur"], "知识管理")

    def test_reopening_the_same_workflow_does_not_reload(self):
        self.assertEqual(self.steps["switch-kb"]["loads"], self.steps["same-again"]["loads"])

    def test_open_roots_are_saved_and_cleared_with_the_app(self):
        self.assertEqual(json.loads(self.steps["switch-kb"]["saved"]),
                         {"roots": [{"id": "sre", "name": "事故应急"},
                                    {"id": "kb", "name": "知识管理"}], "cur": "kb"})
        self.assertEqual(self.steps["closed"]["saved"], "")

    def test_old_entries_become_tabs(self):
        # 旧条目每个工作流各一条,现在各成一个标签,仍然只有一页
        self.assertEqual(self.steps["migrated"]["pages"], 1)
        self.assertEqual(self.steps["migrated"]["tabs"], ["事故应急", "知识管理"])
        # 恢复时页面是隐藏的:这一页先不装载,打开它那一刻才拉起来
        self.assertEqual(self.steps["migrated"]["src"], "")
        self.assertIn("domain=kb", self.steps["migrated-shown"]["src"])


if __name__ == "__main__":
    unittest.main()
