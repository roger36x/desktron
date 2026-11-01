"""浏览器成为一个应用(界面模型改造第一步)。

网页原先各占一张全局标签,地址栏与前进/后退挂在浮层页头上,是全局的。现在它们收进
「浏览器」这个应用:应用自己带一排内部标签、自己的地址栏与导航钮,外部仍只有 openPage
一个入口。最容易出错的是原生面板——外链在 Swift 壳里走 WKWebView,面板盖在主 webview
之上、靠前端上报矩形定位,切标签/切页/收起时必须显式 open / rect / hide,否则会出现
「切走了但原生面板还盖在上面」。
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
BROWSER = open(os.path.join(FRONT, "app-browser.js"), encoding="utf-8").read()
CSS = open(os.path.join(FRONT, "style.css"), encoding="utf-8").read()
PAGES = open(os.path.join(FRONT, "app-pages.js"), encoding="utf-8").read()
ASSETS = _srcmap.assets_src()
INDEX = open(os.path.join(FRONT, "index.html"), encoding="utf-8").read()
STYLE = open(os.path.join(FRONT, "style.css"), encoding="utf-8").read()
CORE = open(os.path.join(FRONT, "app-core.js"), encoding="utf-8").read()
SETTINGS = open(os.path.join(FRONT, "app-settings-core.js"), encoding="utf-8").read()
SWIFT = open(os.path.join(ROOT, "desktop", "native", "main.swift"), encoding="utf-8").read()


def _fn(src, name):
    m = re.search(rf"\nfunction {name}\(.*?\n}}\n", src, re.S)
    assert m, name
    return m.group(0)


class Wiring(unittest.TestCase):
    def test_browser_leaves_page_appearance_to_the_site(self):
        """Desk 只画浏览器外壳，不再注入反色或提供独立网页外观开关。"""
        for marker in ("applyBrowserAppearance", "__desk_auto_dark_style", "data-desk-auto-dark"):
            self.assertNotIn(marker, SWIFT)
        self.assertNotIn("cd_browser_appearance", CORE)
        self.assertNotIn('smSection("浏览器外观"', SETTINGS)

    def test_browser_uses_original_v4_globe_with_purple_background(self):
        with open(os.path.join(FRONT, "workbench.json"), encoding="utf-8") as f:
            data = json.load(f)
        app = next(a for a in data["desktopApps"] if a["id"] == "browser")
        self.assertNotIn("icon", app)
        self.assertEqual(app["uid"], "desktron.browser.0001")
        self.assertIn('const BROWSER_ART =', ASSETS)
        self.assertIn('[/浏览器|browser/i, _appTilePlain("browser", BROWSER_ART, 16 / 15)]', ASSETS)
        self.assertIn('/icons/app-browser-original-5d5d9b828321.png', ASSETS)
        from pathlib import Path
        import hashlib
        manifest = json.loads(Path(FRONT, 'icons/supplemental-original-manifest.json').read_text(encoding='utf-8'))
        icon = next(i for i in manifest['icons'] if i['name'] == 'browser')
        png = Path(FRONT, icon['png']).read_bytes()
        self.assertEqual(hashlib.sha256(png).hexdigest(), icon['png_sha256'])
        license_text = Path(FRONT, 'vendor/ICONS-LICENSE.txt').read_text(encoding='utf-8')
        for expected in ('Desktron 补充自绘图标', 'V4 定稿', 'Apache-2.0'):
            self.assertIn(expected, license_text)
        self.assertFalse(os.path.exists(os.path.join(FRONT, "icons", "app-browser.png")))

    def test_browser_v4_has_two_closer_latitudes_and_transparent_margin(self):
        from pathlib import Path
        from PIL import Image
        png = Image.open(os.path.join(FRONT, 'icons/app-browser-original-5d5d9b828321.png')).convert('RGBA')
        self.assertEqual(png.size, (512, 512))
        self.assertEqual(png.getchannel('A').getbbox(), (16, 16, 496, 496))
        self.assertEqual(png.getpixel((256, 256)), (98, 86, 222, 255))
        for y in (202, 310):
            self.assertGreaterEqual(min(png.getpixel((256, y))[:3]), 248)
        svg = Path(FRONT, 'icons/app-browser-original-5d5d9b828321.svg').read_text(encoding='utf-8')
        self.assertIn('M7.236 12.6h17.528M7.236 19.4h17.528', svg)
        self.assertIn('rx="4.2" ry="9.4"', svg)
        self.assertEqual(png.getpixel((0, 0))[3], 0)

    def test_browser_requested_icon_renders_without_added_tile(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node unavailable")
        art = re.search(r"^const BROWSER_ART = .*?;$", ASSETS, re.M).group(0)
        script = ("const escapeHtml = s => s;\n" + art + _fn(ASSETS, "_appTilePlain")
                  + '\nconst _APP_GLYPHS = [[/浏览器|browser/i, _appTilePlain("browser", BROWSER_ART)]];\n'
                  + _fn(ASSETS, "_appIconHTML") + '''
for (const extra of ["", "sm"]) {
  const html = _appIconHTML("浏览器", "desktron.browser.0001", extra, "", undefined);
  if (!html.includes('<svg') || !html.includes('/icons/app-browser-original-5d5d9b828321.png')
      || html.includes('scale(1.09)') || html.includes('<rect') || html.includes('<img'))
    throw new Error(html);
}
''')
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True, encoding='utf-8')

    def test_script_is_loaded_after_pages(self):
        self.assertIn('<script src="/app-browser.js" defer></script>', INDEX)
        self.assertLess(INDEX.index("/app-pages.js"), INDEX.index("/app-browser.js"))

    def test_agent_owned_tabs_are_not_persisted_or_reopened(self):
        self.assertIn('browserTabs.filter((t) => !t.agent)', BROWSER)
        self.assertIn('act && !act.agent', BROWSER)
        self.assertIn('source !== "agent" && typeof _closedTabs', BROWSER)
        self.assertIn('browserTabs.find((x) => !x.agent && x.url === url)', BROWSER)

    def test_app_entry_opens_browser_app(self):
        # 与终端那条同理:它不是一张 HTML 页,没有 pages/browser.html 可加载
        self.assertIn('else if (a.page === "browser") opened = openBrowserApp(!compact);', ASSETS)

    def test_opening_the_app_creates_a_visible_blank_tab(self):
        body = _fn(BROWSER, "openBrowserApp")
        self.assertIn("if (p && !browserTabs.length) browserNewTab(show);", body)

    def test_cmd_t_opens_a_blank_browser_tab_not_a_floating_prompt(self):
        branch = re.search(r'if \(!ev\.shiftKey && k === "t"\) \{.*?\n  \}', PAGES, re.S)
        self.assertIsNotNone(branch)
        self.assertIn('browserNewTab(true)', branch.group(0))
        self.assertNotIn('openAddBox()', branch.group(0))
        self.assertNotIn('function openAddBox()', PAGES)

    def test_app_page_is_registered_as_app_with_uid(self):
        # 应用页(不进标签条,页头带收起/关闭,程序坞按 appid 认运行态),备忘录/Timeline 同款宿主
        body = _fn(BROWSER, "browserEnsurePage")
        self.assertIn('openContentPage(BROWSER_PAGE_KEY, "浏览器"', body)
        self.assertIn("{ app: true, appid: browserAppUid() }", body)
        self.assertIn("p.browser = true", body)

    def test_nav_and_url_bar_live_in_the_app_head(self):
        build = _fn(BROWSER, "_browserBuild")
        for cls in ("br-back", "br-fwd", "br-reload", "br-url"):
            self.assertIn(cls, build)
        self.assertIn("host._headNode = head", build)   # 页头插槽:住进 #overlay-title 的位置

    def test_blank_page_has_one_input_and_a_claude_style_empty_state(self):
        build = _fn(BROWSER, "_browserBuild")
        self.assertEqual(build.count('class="ov-url br-url"'), 1)
        self.assertNotIn("br-np-q", build)              # 页面中央不再复制第二个搜索框
        for cls in ("br-empty-mark", "br-empty-title", "br-empty-desc", "br-empty-local"):
            self.assertIn(cls, build)

    @unittest.skipUnless(shutil.which("node"), "需要 node 运行前端函数")
    def test_entering_a_url_in_a_blank_tab_hides_the_empty_state(self):
        """标签集合不变时标签条走就地更新；空态也必须在这条路上收起。

        手机与纯浏览器没有原生面板，网页是 iframe，空态绝对定位盖在它上面。2026-10-16 手机实见：
        空白标签输入网址后网页能显示，但空态文字叠在上面并挡住全部点击。
        """
        script = _fn(BROWSER, "browserRenderTabs") + _fn(BROWSER, "browserRenderEmpty") + r"""
const assert = require("node:assert/strict");
const node = (cls) => ({ className: cls, dataset: {}, hidden: false, children: [], innerHTML: "",
  title: "", textContent: "", querySelector: () => node(""), appendChild(c) { this.children.push(c); } });
const empty = node("br-empty"), strip = node("br-tabs");
strip.querySelector = () => null;
const pageEl = { _headNode: { querySelector: (s) => (s === ".br-tabs" ? strip : null) },
  querySelector: (s) => (s === ".br-empty" ? empty : null) };
globalThis.document = { createElement: () => node("") };
globalThis.CSS = { escape: (x) => x };
let browserTabs = [{ id: "browser:1", url: "", curUrl: "", title: "新标签页" }], browserActiveId = "browser:1", focused = 0;
const browserPage = () => ({ el: pageEl });
const browserActiveTab = () => browserTabs.find((t) => t.id === browserActiveId) || null;
const browserFocusUrl = () => { focused++; };
const escapeHtml = String, ICONS = { close: "" }, _TAB_WINDOW = "", _BR_ICON_PLUS = "";
const _tabFav = () => {}, psTabsBindDrag = () => {}, persistBrowserTabs = () => {};
empty.hidden = true;
browserRenderTabs();                                   // 新建空白标签：整条重建，显示空态
assert.equal(empty.hidden, false); assert.equal(focused, 1);
browserTabs[0].url = "https://example.com"; browserTabs[0].curUrl = "https://example.com";
browserRenderTabs();                                   // 同一张标签输入网址：就地更新
assert.equal(empty.hidden, true);
browserTabs[0].url = ""; browserTabs[0].curUrl = "";
browserRenderTabs();                                   // 回到空白：再次显示并聚焦地址栏
assert.equal(empty.hidden, false); assert.equal(focused, 2);
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_browser_url_bar_resets_the_global_absolute_pill(self):
        rule = re.search(r"\.br-bar \.br-url \{.*?\n\}", STYLE, re.S)
        self.assertIsNotNone(rule)
        for declaration in ("position: static", "transform: none", "text-align: left"):
            self.assertIn(declaration, rule.group(0))
        self.assertIn("border-color: var(--accent)", STYLE)   # 品牌紫色键盘焦点环
        self.assertIn("box-shadow: 0 0 0 1px var(--accent-soft)", STYLE)

    def test_url_bar_displays_url_instead_of_page_title(self):
        nav = _fn(BROWSER, "browserRenderNav")
        self.assertIn("t ? (t.curUrl || t.url) :", nav)
        self.assertNotIn("t.title || t.curUrl", nav)

    def test_loading_progress_covers_native_and_iframe_navigation(self):
        build = _fn(BROWSER, "_browserBuild")
        self.assertIn('class="br-progress"', build)
        self.assertIn(".br-progress > span", CSS)
        self.assertIn("background: var(--accent)", CSS)
        self.assertIn('browserSetLoading(t, "start")', _fn(BROWSER, "browserNavAction"))
        self.assertIn('browserSetLoading(t, "start")', _fn(BROWSER, "browserSubmitUrl"))
        self.assertIn("browserBindFrame(t, f)", BROWSER)
        apply_event = _fn(BROWSER, "browserApplyEvent")
        self.assertIn('e.phase !== "state"', apply_event)
        self.assertIn('else if (!e.phase && t.loading) browserSetLoading(t, "finish")', apply_event)
        for phase in ('phase: "start"', 'phase: "commit"', 'phase: "finish"', 'phase: "fail"'):
            self.assertIn(phase, SWIFT)

    def test_agent_control_requires_explicit_browser_button(self):
        build = _fn(BROWSER, "_browserBuild")
        self.assertIn("br-agent", build)
        self.assertIn("browserToggleAgentControl()", build)
        toggle = _fn(BROWSER, "browserToggleAgentControl")
        self.assertIn('"agentTakeover" : "agentDelegate"', toggle)
        self.assertIn("sessionId", toggle)

    def test_delegate_failure_is_visible(self):
        # 原生壳回 error 事件时按钮要露出失败与原因,否则人点了没反应、也没地方看为什么
        render = _fn(BROWSER, "browserRenderAgentControl")
        self.assertIn('state.status === "error"', render)
        self.assertIn('"失败"', render)
        self.assertIn('classList.toggle("err", failed)', render)
        self.assertIn(".br-nav button.br-agent.err", CSS)

    def test_paused_state_is_distinct_from_revoked(self):
        # 交还控制不再承诺直接续跑旧动作，须明确重新观察。
        render = _fn(BROWSER, "browserRenderAgentControl")
        self.assertIn('state.status === "paused"', render)
        self.assertIn('"已暂停"', render)
        self.assertIn("重新观察", render)
        self.assertIn('classList.toggle("paused", paused)', render)
        self.assertIn(".br-nav button.br-agent.paused", CSS)
        self.assertIn('event.type === "paused"', BROWSER)

    def test_tab_switch_steps_aside_navigation_revokes(self):
        # 切标签只是让开(切回来自动接续),导航才真收回:地址一换可能是另一个站点
        activate = _fn(BROWSER, "browserActivateTab")
        self.assertIn('browserAgentStepAside(previous, "tab_switched")', activate)
        self.assertIn("browserAgentStepBack(t)", activate)
        self.assertIn('"agentPause"', _fn(BROWSER, "browserAgentStepAside"))
        self.assertIn('"agentResume"', _fn(BROWSER, "browserAgentStepBack"))

    def test_agent_opened_tab_enters_tab_strip(self):
        # Agent 自开的标签要进标签条并置成已委托,否则界面上不存在、用户也关不掉
        self.assertIn("window.__browserAgentOpen", BROWSER)
        seg = BROWSER[BROWSER.index("window.__browserAgentOpen"):]
        seg = seg[:seg.index("window.__browserControlEvent")]
        self.assertIn("browserTabs.push", seg)
        self.assertIn("isolated: !!req.isolated", seg)
        self.assertIn('browserControlState.set(req.id, { status: "delegated"', seg)
        self.assertIn("isolated: !!t.isolated", BROWSER)   # 落到原生 open 消息里

    def test_hiding_side_browser_preserves_webview(self):
        body = _fn(BROWSER, "browserHideNativeForSide")
        self.assertIn('action: "hideOne"', body)
        self.assertNotIn('action: "close"', body)
        self.assertIn('action: "hideOne"', _fn(BROWSER, "browserSweepNative"))
        self.assertIn("browserTab: true", BROWSER)

    def test_agent_open_does_not_require_visible_layout(self):
        # 原生后台 open 自带布局尺寸，不必打开用户面板或反复同步可见标签。
        seg = BROWSER[BROWSER.index("window.__browserAgentOpen"):]
        seg = seg[:seg.index("window.__browserControlEvent")]
        self.assertNotIn("browserSyncNative()", seg)
        self.assertNotIn("requestAnimationFrame", seg)
        self.assertNotIn("browserActivateTab(req.id", seg)
        self.assertIn("browserEnsurePage(false)", seg)

    def test_agent_open_creates_background_view(self):
        # 不等可见:browserVisible 闸在人看着会话时恒为假,补多少次同步都建不出视图
        seg = BROWSER[BROWSER.index("window.__browserAgentOpen"):]
        seg = seg[:seg.index("window.__browserControlEvent")]
        self.assertIn("background: true", seg)
        self.assertIn('action: "open"', seg)
        self.assertIn("w: 1280", seg)

    def test_manual_navigation_and_tab_switch_take_control_back(self):
        self.assertIn('browserUserTakesControl(t, "user_navigation")',
                      _fn(BROWSER, "browserNavAction"))
        self.assertIn('browserUserTakesControl(t, "user_navigation")',
                      _fn(BROWSER, "browserSubmitUrl"))


class OpenPageCompat(unittest.TestCase):
    def test_open_page_routes_cross_origin_to_the_browser_app(self):
        body = _fn(PAGES, "openPage")
        self.assertIn('if (!opts.app && typeof browserHandlesUrl === "function" && browserHandlesUrl(url))', body)
        self.assertIn("browserOpenTab(url, title, opts, show);", body)
        # 调用方签名不变:仍是 openPage(url, title, opts, show)
        self.assertIn("function openPage(url, title, opts = {}, show = true)", body)

    def test_native_mode_options_are_carried_over_unchanged(self):
        # 飞书那几处的定制模式(nativeMode/chatId/chatName/tagName)两边共用同一份归一
        self.assertIn("function resolveNativeOpts(url, opts = {})", PAGES)
        self.assertIn("resolveNativeOpts(url, opts)", _fn(PAGES, "openPage"))
        tab = _fn(BROWSER, "browserOpenTab")
        self.assertIn("const nat = resolveNativeOpts(url, opts)", tab)
        for k in ("nativeMode", "chatId", "chatName", "tagName"):
            self.assertIn(f"{k}: nat.{k}", tab)
        # 原生面板开面板时把这几个参数原样发给壳
        sync = _fn(BROWSER, "browserSyncNative")
        for k in ("nativeMode", "chatId", "chatName", "tagName"):
            self.assertIn(f"{k}: t.{k}", sync)

    def test_reopen_closed_tab_still_goes_through_open_page(self):
        # ⌘⇧T 仍走 app-pages.js 的 reopenClosedTab,浏览器标签关掉时留同款摘要
        self.assertIn('kind: "page"', _fn(BROWSER, "browserCloseTab"))
        self.assertIn("_closedTabs.push", _fn(BROWSER, "browserCloseTab"))


class NativePanelLifecycle(unittest.TestCase):
    def test_side_resize_handle_stays_hittable_beneath_native_webview(self):
        # 原生网页盖在主 WebView 上；侧栏左缘必须命中穿透，否则只有网页上方能拖宽。
        rect = _fn(BROWSER, "browserRect")
        self.assertIn("passThroughLeft: sideResizeHit", rect)
        self.assertIn("final class BrowserWebView: WKWebView", SWIFT)
        self.assertIn("if passThroughLeft > 0", SWIFT)
        self.assertIn('(v as? BrowserWebView)?.passThroughLeft = num(r["passThroughLeft"])', SWIFT)

    def test_native_page_makes_room_for_the_approval_notice(self):
        """主 WebView 的通知盖不过独立 WKWebView，矩形必须在重叠时从通知下方开始。"""
        rect = _fn(BROWSER, "browserRect")
        self.assertIn('document.getElementById("appr-notice")', rect)
        self.assertIn("n.bottom + 8", rect)
        self.assertIn("bottom - top", rect)

    @unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
    def test_approval_notice_only_insets_an_overlapping_native_page(self):
        rect = _fn(BROWSER, "browserRect")
        js = r'''
let hidden = false;
const document = {
  body: { classList: { contains: () => false } },
  getElementById: () => ({ hidden, getBoundingClientRect: () =>
    ({ left: 640, right: 1000, top: 16, bottom: 300, width: 360, height: 284 }) })
};
function browserViewEl() { return { getBoundingClientRect: () =>
  ({ left: 200, right: 1000, top: 100, bottom: 700, width: 800, height: 600 }) }; }
function browserPage() { return {}; }
function appPageInSide() { return true; }
SRC
const shown = browserRect();
hidden = true;
const gone = browserRect();
console.log(JSON.stringify({shown, gone}));
'''.replace("SRC", rect)
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual((got["shown"]["y"], got["shown"]["h"]), (308, 392))
        self.assertEqual((got["gone"]["y"], got["gone"]["h"]), (100, 600))

    def test_show_overlay_does_not_blanket_hide_the_browser_app(self):
        body = re.search(r"function showOverlay\(p\).*?\n}\n", PAGES, re.S).group(0)
        self.assertIn("} else if (p.browser) {", body)
        self.assertIn("requestAnimationFrame(() => browserSyncNative(true));", body)

    def test_closing_the_app_destroys_every_panel(self):
        self.assertIn('if (p.browser && typeof browserPageClosed === "function") browserPageClosed();',
                      _fn(PAGES, "closePage"))
        closed = _fn(BROWSER, "browserPageClosed")
        self.assertIn('nb({ action: "close", id: t.id })', closed)
        self.assertIn('_browserShownNative = ""', closed)

    def test_a_sweep_catches_panels_left_behind(self):
        sweep = _fn(BROWSER, "browserSweepNative")
        self.assertIn("browserHideNative()", sweep)
        # hide 是全局的:别的页正开着内嵌面板时不去按它,只把自己的记号清掉
        self.assertIn("nativeEmbeds", sweep)
        self.assertIn("setInterval(browserSweepNative, 1000);", BROWSER)

    def test_resize_updates_the_rect(self):
        self.assertIn('window.addEventListener("resize", () => browserSyncNative());', BROWSER)
        # 回调改成了逐帧合并(拖窗时 ResizeObserver 连发几十次,每次都发一条 rect 会让 IPC 排队),
        # 所以不再是一行直调。只钉住「观察者存在且最终会同步原生面板」这两点。
        self.assertIn("new ResizeObserver(", BROWSER)
        self.assertIn("requestAnimationFrame(() => { raf = 0; browserSyncNative(); })", BROWSER)


HARNESS = r"""
let NATIVE_BROWSER = true;
const sent = [];
function nb(m) { sent.push(m); }
let _browserShownNative = "";
let VISIBLE = true;
let TAB = null;
let RECT = { x: 0, y: 40, w: 800, h: 600 };
function browserVisible() { return VISIBLE; }
function browserActiveTab() { return TAB; }
function browserRect() { return RECT; }
SRC
const A = { id: "browser:1", url: "https://a.example/", curUrl: "", native: true, nativeMode: "", chatId: "", chatName: "", tagName: "" };
const B = { id: "browser:2", url: "https://b.example/", curUrl: "", native: true, nativeMode: "", chatId: "", chatName: "", tagName: "" };
const LOCAL = { id: "browser:3", url: "/local", native: false };
const BLANK = { id: "browser:4", url: "", curUrl: "", native: true };
const out = [];
const step = (name) => out.push([name, sent.splice(0).map((m) => m.action + ":" + (m.id || ""))]);
TAB = A; browserSyncNative(true); step("show-a");
browserSyncNative();               step("resize-a");
TAB = B; browserSyncNative(true);  step("switch-b");
TAB = LOCAL; browserSyncNative(true); step("switch-local");
TAB = B; browserSyncNative(true);  step("back-to-b");
TAB = BLANK; browserSyncNative(true); step("switch-blank");
TAB = B; browserSyncNative(true);  step("back-from-blank");
VISIBLE = false; browserSyncNative(); step("hidden");
browserSyncNative();               step("hidden-again");
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class NativePanelBehaviour(unittest.TestCase):
    def setUp(self):
        src = "\n".join(_fn(BROWSER, n) for n in ("browserSyncNative", "browserHideNative"))
        js = HARNESS.replace("SRC", src)
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.steps = dict(json.loads(out.stdout))

    def test_showing_a_tab_opens_its_panel(self):
        self.assertEqual(self.steps["show-a"], ["open:browser:1"])

    def test_layout_change_only_moves_the_panel(self):
        # 尺寸变化发 rect:open 每次都把视图提到最前,当 resize 用太重
        self.assertEqual(self.steps["resize-a"], ["rect:browser:1"])

    def test_switching_tabs_opens_the_new_panel(self):
        # 原生那侧 open 会把其余面板一并隐藏,所以切标签只需发这一条
        self.assertEqual(self.steps["switch-b"], ["open:browser:2"])

    def test_leaving_the_app_hides_the_panel_once(self):
        self.assertEqual(self.steps["hidden"], ["hide:"])
        self.assertEqual(self.steps["hidden-again"], [])   # 已经收起就不再重复发,免得压掉别处的面板

    def test_switching_to_a_same_origin_tab_hides_the_panel(self):
        # 无原生桥的标签走 iframe:切过去要把上一张的原生面板收掉,否则它还盖在上面
        self.assertEqual(self.steps["switch-local"], ["hide:"])
        self.assertEqual(self.steps["back-to-b"], ["open:browser:2"])

    def test_blank_tab_uses_the_desk_empty_state_not_a_native_white_canvas(self):
        self.assertEqual(self.steps["switch-blank"], ["hide:"])
        self.assertEqual(self.steps["back-from-blank"], ["open:browser:2"])


URL_HARNESS = r"""
const location = { href: "http://127.0.0.1:8899/", origin: "http://127.0.0.1:8899" };
SRC
console.log(JSON.stringify(CASES.map(browserHandlesUrl)));
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class UrlRouting(unittest.TestCase):
    def test_only_cross_origin_addresses_go_to_the_browser_app(self):
        cases = [
            "https://www.feishu.cn/messenger/",          # 外链:归浏览器应用
            "http://127.0.0.1:8899/workbench/view?path=/a/b",   # 同源文件查看页:仍是 iframe 标签
            "/workbench/page?id=files&domain=x",         # 同源应用页
            "chat:abc",                                  # 会话页 key,不是地址
            "term:12",
            "files:desk-iter",
            "",
        ]
        js = (URL_HARNESS.replace("SRC", _fn(BROWSER, "browserHandlesUrl"))
              .replace("CASES", json.dumps(cases)))
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [True, False, False, False, False, False, False])


@unittest.skipUnless(shutil.which("node"), "需要 node 跑原生桥协调")
class NativeModalPresentation(unittest.TestCase):
    def test_modal_hides_and_restores_without_closing_or_losing_updates(self):
        bridge = PAGES[PAGES.index("const NATIVE_BROWSER ="):PAGES.index("// 通顶壳拖窗:")]
        js = r'''
const assert = require('assert');
const sent = [], classes = new Set();
let changed;
const body = {classList: {contains: name => classes.has(name)}};
const document = {body};
const bump = () => changed([{target: body, attributeName: 'class'}]);
const window = {webkit: {messageHandlers: {browser: {postMessage: m => sent.push(m)}}}};
class MutationObserver { constructor(fn) { changed = fn; } observe() {} }
SRC
sent.length = 0;
nb({action:'open',id:'tab-a',url:'https://example.test',rect:{w:800,h:600}});
classes.add('settings-dialog-open'); bump();
assert.equal(sent.at(-1).action, 'hide');
const hiddenCount = sent.length;
nb({action:'rect',id:'tab-a',rect:{w:900,h:700}});
assert.equal(sent.length, hiddenCount);
classes.add('resource-dialog-open'); bump();
classes.delete('settings-dialog-open'); bump();
assert.equal(sent.length, hiddenCount); // 另一层弹窗仍在，不能恢复
classes.delete('resource-dialog-open'); bump();
assert.deepEqual(sent.at(-1), {action:'open',id:'tab-a',url:'https://example.test',rect:{w:900,h:700}});
assert(!sent.some(m => m.action === 'close' || m.action === 'navigate'));
classes.add('settings-dialog-open'); bump();
nb({action:'open',id:'tab-b',url:'https://other.test',rect:{w:500,h:600}});
assert.equal(sent.at(-1).action, 'hide');
nb({action:'open',id:'agent-tab',url:'https://agent.test',background:true});
classes.delete('settings-dialog-open'); bump();
assert.equal(sent.at(-1).id, 'tab-b'); // 后台 Agent 页不抢前台
classes.add('resource-dialog-open'); bump();
nb({action:'close',id:'tab-b'});
const closedCount = sent.length;
classes.delete('resource-dialog-open'); bump();
assert.equal(sent.length, closedCount); // 弹窗期间关闭的页面不复活
'''.replace("SRC", bridge)
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)


@unittest.skipUnless(shutil.which("node"), "需要 node 跑地址同步")
class BrowserAddressSync(unittest.TestCase):
    def test_navigation_state_updates_address_copy_and_storage_without_finishing_load(self):
        js = r'''
const assert = require('assert');
const a = {id:'a',url:'https://example.test/old',curUrl:'https://example.test/old',loading:true};
const b = {id:'b',url:'https://example.test/background'};
const browserTabs = [a,b]; let browserActiveId = 'a';
const u = {value:a.url,selectionStart:0,selectionEnd:a.url.length,
  select(){this.selectionStart=0;this.selectionEnd=this.value.length;}};
let hasFocus = true;
const document = {activeElement:u,hasFocus:()=>hasFocus};
const buttons = {'.br-url':u,'.br-back':{},'.br-fwd':{},'.br-reload':{}};
function browserPage(){return {el:{querySelector:s=>buttons[s]}};}
function browserActiveTab(){return browserTabs.find(t=>t.id===browserActiveId);}
function browserRenderAgentControl(){} function browserRenderLoading(){}
function browserRenderTabs(){} let stored;
function persistBrowserTabs(){stored=browserTabs.map(t=>t.curUrl||t.url);}
const phases=[]; function browserSetLoading(t,phase){phases.push(phase);}
SRC
browserApplyEvent({id:'a',phase:'state',url:'https://example.test/route',title:'新页',canBack:true});
assert.equal(u.value, a.curUrl); assert.equal(browserExternalUrl(), u.value);
assert.equal(u.selectionEnd, u.value.length); // 全选复制拿到新地址
assert.equal(stored[0], u.value); assert.equal(a.title, '新页');
assert.equal(buttons['.br-back'].disabled, false);
assert.deepEqual(phases, []); assert.equal(a.loading, true);
u._editingTab='a'; u.value='https://my-draft.test';
browserApplyEvent({id:'a',phase:'state',url:'https://example.test/redirect'});
assert.equal(u.value,'https://my-draft.test'); // 真正输入的草稿不被覆盖
assert.equal(browserExternalUrl(),'https://example.test/redirect');
hasFocus=false; browserRenderNav(); assert.equal(u.value,a.curUrl); // 原生页获得焦点
hasFocus=true; u._editingTab='';
browserApplyEvent({id:'b',phase:'state',url:'https://example.test/bg-new'});
assert.equal(u.value,a.curUrl); assert.equal(stored[1],b.curUrl);
u._editingTab='a'; u.value='another draft'; browserActiveId='b'; browserRenderNav();
assert.equal(u.value,b.curUrl); // 换标签不能留下前一张草稿
browserApplyEvent({id:'a',phase:'finish'}); assert.deepEqual(phases,['finish']);
browserApplyEvent({id:'a'}); assert.deepEqual(phases,['finish','finish']); // 旧壳兼容
assert.equal(browserApplyEvent({id:'closed',phase:'state',url:'bad'}),false);
'''.replace('SRC', '\n'.join(_fn(BROWSER, name) for name in (
            'browserRenderNav', 'browserApplyEvent')) + '\n' +
            re.search(r'^function browserExternalUrl\(\).*$', BROWSER, re.M).group(0))
        out = subprocess.run(['node', '-e', js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
