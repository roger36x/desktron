"""近期应用右侧紧凑宿主的静态契约。"""
import os
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FRONT = os.path.join(ROOT, "frontend")

def read(rel):
    with open(os.path.join(FRONT, rel), encoding="utf-8") as f:
        return f.read()

INDEX = read("index.html")
PAGES = read("app-pages.js")
APPS = _srcmap.assets_src()
STYLE = read("style.css")
SITUATION = read("app-chat-situation.js")
BROWSER = read("app-browser.js")
TERMINAL = read("app-terminal.js")
FILES = read("app-files.js")
NOTEPAD = read("pages/notepad.html")
TASKPAPER = read("pages/taskpaper.html")
EXCALIDRAW = read("app-assets/excalidraw/src/entry.jsx")
FILE_ENTRY = read("app-assets/files/src/entry.js")


class HostContract(unittest.TestCase):
    def test_main_and_side_are_sibling_hosts(self):
        self.assertIn('id="main-stage"', INDEX)
        self.assertIn('id="app-side"', INDEX)
        self.assertIn('id="app-side-2"', INDEX)
        self.assertIn('id="app-side-resizer"', INDEX)
        self.assertIn('id="app-side-expand"', INDEX)
        self.assertIn('id="app-side-collapse"', INDEX)
        self.assertIn('id="app-side-body"', INDEX)
        self.assertIn('id="app-side-body-2"', INDEX)
        self.assertIn('id="ov-toside"', INDEX)

    def test_page_node_moves_instead_of_cloning(self):
        self.assertIn('body.appendChild(p.el)', PAGES)
        self.assertIn('$("#overlay-body").appendChild(p.el)', PAGES)
        self.assertNotIn('cloneNode', PAGES)
        self.assertIn('setAppPageLayout(p, "compact")', PAGES)
        self.assertIn('setAppPageLayout(p, "main")', PAGES)

    def test_recent_apps_use_side_but_dock_uses_main(self):
        self.assertIn('if (pg) showAppSide(pg); else _openDomainApp(x.a, x.domainId, "side")', APPS)
        self.assertIn('_openDomainApp(x.a, x.domainId, "main")', APPS)
        self.assertIn('function _openDomainApp(a, domainId, placement = "main")', APPS)

    def test_expand_promotes_the_same_page(self):
        self.assertIn('function expandSideApp(p = sideAppPage)', PAGES)
        self.assertIn('hideAppSide();\n  showOverlay(p);', PAGES)

    def test_fullscreen_app_can_return_to_side_without_rebuild(self):
        self.assertIn('function collapseMainAppToSide()', PAGES)
        seg = PAGES[PAGES.index('function collapseMainAppToSide()'):PAGES.index('function setOverlayTitle')]
        self.assertIn('const p = pActive', seg)
        self.assertIn('!p.app', seg)
        self.assertIn('showAppSide(p, home)', seg)
        self.assertNotIn('openPage(', seg)
        self.assertIn('{ const x = $("#ov-toside"); if (x) x.onclick = () => collapseMainAppToSide(); }', PAGES)
        self.assertIn('#overlay.app-page:not(.hs-head) #ov-toside { display: flex; }', STYLE)

    def test_fullscreen_side_restores_the_exact_previous_page(self):
        self.assertIn('let sideAppReturnState = null', PAGES)
        expand = PAGES[PAGES.index('function expandSideApp('):PAGES.index('function collapseMainAppToSide()')]
        self.assertIn('sideAppReturnState = {', expand)
        self.assertIn('page: overlayOpen && pActive && pActive !== p ? pActive : null', expand)
        show = PAGES[PAGES.index('function showAppSide('):PAGES.index('function toggleAppSide()')]
        self.assertIn('pages.includes(returnPage)', show)
        self.assertIn('const home = exact || overlayHomePage()', show)
        hide = PAGES[PAGES.index('function hideOverlay('):PAGES.index('const _closedTabs')]
        self.assertIn('sideAppReturnState.app === pActive', hide)
        self.assertIn('const home = exact || (toHome ? overlayHomePage() : null)', hide)

    def test_compact_header_has_collapse_and_uniform_icon_boxes(self):
        # 原来这里还断言侧栏里的标签要藏掉关闭 ×。2026-09-03 撤掉:藏了 × 侧栏就只能新开
        # 不能关,与「侧栏里也是一组真正的标签页」这条相冲,见下面 test_side_shows_all_tabs。
        self.assertIn('flex: 0 0 26px; width: 26px; height: 26px;', STYLE)
        self.assertIn('width: 15px; height: 15px; stroke-width: 1.8;', STYLE)
        self.assertIn('{ const x = $("#app-side-collapse"); if (x) x.onclick = () => hideAppSide(sideAppPage); }', PAGES)
        self.assertIn('{ const x = $("#app-side-collapse-2"); if (x) x.onclick = () => hideAppSide(sideAppPage2); }', PAGES)
        self.assertNotIn('id="app-side-close"', INDEX)

    def test_chat_header_has_separate_app_side_and_information_buttons(self):
        self.assertIn('class="ov-close-icon"', INDEX)
        self.assertIn('class="ov-side-icon"', INDEX)
        self.assertIn('id="ov-info"', INDEX)
        self.assertIn('#overlay.hs-head #ov-close .ov-close-icon { display: none; }', STYLE)
        self.assertIn('#overlay.hs-head #ov-close .ov-side-icon { display: block; }', STYLE)
        self.assertIn('#overlay.hs-head #ov-info,', STYLE)
        self.assertIn('classList.contains("hs-head")) { toggleAppSide(); return; }', PAGES)
        self.assertIn('{ const x = $("#ov-info"); if (x) x.onclick = () => hsSituationToggleActive(); }', PAGES)
        self.assertIn("function hsSituationToggleActive()", SITUATION)

    def test_collapsed_side_can_be_restored_from_the_chat_header(self):
        self.assertIn('let lastSideAppPage = null', PAGES)
        self.assertIn('lastSideAppPage = p;', PAGES)
        seg = PAGES[PAGES.index('function toggleAppSide()'):PAGES.index('function syncOverlayCornerAction()')]
        self.assertIn('pages.includes(lastSideAppPage)', seg)
        self.assertIn('showAppSide(lastSideAppPage)', seg)
        self.assertIn('document.querySelector("#ov-recent .ov-rc")', seg)

    def test_open_app_side_hides_only_duplicate_side_toggle(self):
        self.assertIn('body.app-side-open #overlay.hs-head #ov-close { display: none; }', STYLE)
        self.assertNotIn('body.app-side-open #overlay.hs-head #ov-info', STYLE)
        self.assertIn('document.body.classList.toggle("app-side-open", count > 0)', PAGES)

    def test_information_toggle_reads_css_layout_and_inline_state_is_disabled(self):
        seg = SITUATION[SITUATION.index('function hsSituationCornerState()'):
                        SITUATION.index('function hsSituationToggleActive()')]
        self.assertIn("getComputedStyle", seg)
        self.assertIn("--hs-sit-inline", seg)
        self.assertNotIn("innerWidth", seg)
        corner = PAGES[PAGES.index('function syncOverlayCornerAction()'):
                       PAGES.index('function expandSideApp(')]
        self.assertIn("infoBtn.disabled", corner)
        self.assertIn('"信息栏已显示"', corner)

    def test_side_shows_all_tabs_for_browser_terminal_and_files(self):
        """侧栏里三个应用都要露出全部标签。

        旧规则是 `.X-tab:not(.active) { display: none }`,只留当前那一张:别的标签在
        内存里活着,看起来却像不存在。浏览器那一路先单独修过,终端与文件应用当时没跟上,
        于是终端搬进侧栏就成了「不支持多标签」(2026-09-03 用户实见)。现在三者合用同一条
        紧凑尺寸规则,空间不足交给各自标签条的横向滚动。

        关闭 × 也一并留下——藏了它,侧栏里只能新开不能关。"""
        for head, tab in (("br", "br"), ("tm", "tm"), ("fa", "fa")):
            self.assertNotIn('.%s-head[data-layout="compact"] .%s-tab:not(.active)' % (head, tab), STYLE)
            self.assertNotIn('.%s-head[data-layout="compact"] .%s-tab .ps-x { display: none; }' % (head, tab), STYLE)
            self.assertIn('.%s-head[data-layout="compact"] .%s-tab' % (head, tab), STYLE)
        self.assertIn('flex: 0 1 150px; min-width: 72px; max-width: 160px;', STYLE)

    def test_native_browser_and_terminal_recognize_side_visibility(self):
        self.assertIn('return appPageVisible(p)', BROWSER)
        self.assertIn('const inSide = typeof appPageInSide', BROWSER)
        self.assertIn('const inSide = typeof appPageInSide', TERMINAL)
        self.assertIn('function browserHideNativeForSide()', BROWSER)
        self.assertIn('browserHideNativeForSide()', PAGES)
        self.assertIn('termAppRefit()', PAGES)

    def test_side_width_is_draggable_persisted_and_resettable(self):
        self.assertIn('function appSideResize()', PAGES)
        self.assertIn('const KEY = "cd_app_side_w"', PAGES)
        self.assertIn('bar.setPointerCapture(ev.pointerId)', PAGES)
        self.assertIn('w0 + x0 - e.clientX', PAGES)
        self.assertIn('localStorage.setItem(KEY, String(lastW))', PAGES)
        self.assertIn('localStorage.removeItem(KEY)', PAGES)
        self.assertIn('body.app-side-resizing iframe { pointer-events: none; }', STYLE)

    def test_mobile_side_stays_full_screen_without_resize_handle(self):
        self.assertIn('#app-side-resizer { display: none; }', STYLE)
        self.assertIn('width: 100%; max-width: none; flex-basis: 100%', STYLE)

    def test_two_sides_fill_the_main_area_and_hide_the_original_stage(self):
        self.assertIn('let sideAppPage2 = null', PAGES)
        self.assertIn('document.body.classList.toggle("app-side-double", count > 1)', PAGES)
        self.assertIn('body.app-side-double #main-stage { display: none; }', STYLE)
        self.assertIn('--app-side-w: 50%; flex: 1 1 50%; width: 50%;', STYLE)

    def test_third_side_request_replaces_only_the_second_slot(self):
        show = PAGES[PAGES.index('function showAppSide('):PAGES.index('function toggleAppSide()')]
        self.assertIn('if (sideAppPage && sideAppPage2) hideAppSide(sideAppPage2);', show)
        self.assertIn('const slot = sideAppPage ? 2 : 1;', show)

    def test_closing_first_side_promotes_the_second_to_single_side(self):
        promote = PAGES[PAGES.index('function _promoteSecondSide()'):PAGES.index('function hideAppSide(')]
        self.assertIn('sideAppPage = p;', promote)
        self.assertIn('sideAppPage2 = null;', promote)
        self.assertIn('_appSideParts(1).body.appendChild(p.el)', promote)


class CompactApps(unittest.TestCase):
    def test_host_native_apps_have_compact_rules(self):
        for marker in (
            '.browser-host[data-layout="compact"]',
            '.terminal-host[data-layout="compact"]',
            '.br-head[data-layout="compact"]',
            '.fa-head[data-layout="compact"]',
        ):
            self.assertIn(marker, STYLE)

    def test_notepad_enters_current_note(self):
        self.assertIn("e.data.type !== 'desktron-layout'", NOTEPAD)
        self.assertIn("if (next === 'compact' && notepadLoaded && !notepadNavigated)", NOTEPAD)
        self.assertIn("mGo(activeNote() ? 'note' : 'list', false)", NOTEPAD)
        self.assertIn("if (notepadLayout === 'compact' && !notepadNavigated)", NOTEPAD)
        self.assertIn("document.documentElement.dataset.layout === 'compact' || notepadNarrow.matches", NOTEPAD)
        self.assertIn("if (next === notepadLayout) return", NOTEPAD)

    def test_taskpaper_source_is_packaged_and_starts_in_document(self):
        self.assertIn('.app[data-m="doc"] .side { display: none; }', TASKPAPER)
        self.assertIn("mGo('doc');", TASKPAPER)
        self.assertIn("e.data.type !== 'desktron-layout'", TASKPAPER)
        self.assertIn("if (next === taskpaperLayout) return", TASKPAPER)

    def test_excalidraw_undocks_its_list_when_narrow(self):
        self.assertIn('window.matchMedia("(max-width: 820px)")', EXCALIDRAW)
        self.assertIn('if (mq.matches) setDocked(false)', EXCALIDRAW)

    def test_ide_uses_drawer_and_main_first_layout(self):
        self.assertIn('const mobileIDE = () => MOBILE_MEDIA.matches', FILE_ENTRY)
        self.assertIn('if (mobileIDE()) setSideVisible(false)', FILE_ENTRY)
        self.assertIn('else if (a.page === "files") opened = openFilesApp(!compact, domainId)', APPS)
        self.assertIn('function openFilesApp(show = true, domainId = "")', FILES)

    def test_ide_compact_layout_defaults_to_collapsed_without_overwriting_main(self):
        self.assertIn('const sideVisibleByLayout = { main: null, compact: false }', FILE_ENTRY)
        self.assertIn('if (!force && next === appLayoutMode) return', FILE_ENTRY)
        self.assertIn('e.data?.type !== "desktron-layout"', FILE_ENTRY)
        self.assertIn('next === "compact" ? sideVisibleByLayout.compact', FILE_ENTRY)
        self.assertIn('if (appLayoutMode === "compact") {\n    sideVisibleByLayout.compact = on;', FILE_ENTRY)
        self.assertIn('} else {\n    st.sideCollapsed = !on; save();\n  }', FILE_ENTRY)


class SurvivesReload(unittest.TestCase):
    """切引擎是整页重载,右侧栏开着就不该在重载后消失。"""

    def test_两栏都落盘_开合都写(self):
        self.assertIn('const _SIDE_APPS_KEY = "cd_side_apps"', PAGES)
        # 收起与打开各写一次:少任何一处,记录都会与眼前的界面对不上
        show, hide = PAGES.index("function showAppSide"), PAGES.index("function hideAppSide")
        for start in (show, hide):
            seg = PAGES[start:start + 2600]
            self.assertIn("persistAppSide();", seg)

    def test_恢复接在主区之后且跳过主区那一页(self):
        init = read("app-init.js")
        self.assertIn("await restoreEngineView(curEngine)", init)
        self.assertIn("await restoreAppSide()", init)
        self.assertLess(init.index("await restoreEngineView"), init.index("await restoreAppSide()"))
        self.assertIn("if (p && pActive === p) continue;", PAGES)

    def test_窄屏不恢复_那一档侧栏是盖满的临时视图(self):
        self.assertIn("function _appSideNarrow()", PAGES)
        self.assertIn("if (_appSideNarrow()) return false;", PAGES)


if __name__ == "__main__":
    unittest.main()
