"""终端成为一个应用(界面模型改造第二步)。

shell 原先每开一个就在全局标签条里多一张 term: 页。现在它们收进「终端」这个应用:
应用只有一个页面,内部一排标签,一个标签一个 shell,新建/切换/关闭都在应用内部。
与浏览器应用同构,差别都源于终端是本地进程——关标签要真的结束后端 PTY,内容是 xterm
(对容器尺寸敏感、切标签要重新 fit、pane 有保活上限),标签名跟着当前目录走。
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
TERMINAL = open(os.path.join(FRONT, "app-terminal.js"), encoding="utf-8").read()
PAGES = open(os.path.join(FRONT, "app-pages.js"), encoding="utf-8").read()
ASSETS = _srcmap.assets_src()
PANEL = _srcmap.panel_src()
INDEX = open(os.path.join(FRONT, "index.html"), encoding="utf-8").read()
CSS = open(os.path.join(FRONT, "style.css"), encoding="utf-8").read()


def _fn(src, name):
    m = re.search(rf"\n(?:async )?function {name}\(.*?\n}}\n", src, re.S)
    assert m, name
    return m.group(0)


class Wiring(unittest.TestCase):
    def test_script_is_loaded_after_pages(self):
        self.assertIn('<script src="/app-terminal.js" defer></script>', INDEX)
        self.assertLess(INDEX.index("/app-pages.js"), INDEX.index("/app-terminal.js"))

    def test_app_entry_opens_the_terminal_app(self):
        # 终端不是一张 HTML 页(没有 pages/shell.html),两个入口都走应用:开着就切回去
        self.assertIn('if (a.page === "shell") opened = openTerminalApp({ title: a.label, show: !compact });', ASSETS)
        self.assertIn("d.onclick = () => openTerminalApp();", PANEL)

    def test_app_page_is_registered_as_app_with_uid(self):
        # 应用页(不进标签条,页头带收起/关闭,程序坞按 appid 认运行态),与浏览器应用同一套宿主
        body = _fn(TERMINAL, "terminalEnsurePage")
        self.assertIn('openContentPage(TERMINAL_PAGE_KEY, "终端"', body)
        self.assertIn("{ app: true, appid: terminalAppUid() }", body)
        self.assertIn("p.terminal = true", body)

    def test_iterm式窗口标题与会话标签分层(self):
        build = _fn(TERMINAL, "_terminalBuild")
        self.assertIn("host._headNode = head", build)   # 页头插槽只承载窗口标题
        self.assertIn("tm-window-title", build)
        self.assertIn("tm-tabbar", build)
        self.assertIn("tm-tabs", build)
        self.assertLess(build.index("tm-tabbar"), build.index("tm-view"))
        # 终端没有浏览器地址栏，也不复制 macOS 左上角交通灯。
        self.assertNotIn("tm-url", TERMINAL)
        self.assertNotIn("traffic-light", TERMINAL)

    def test_window_title_follows_the_active_tab(self):
        body = _fn(TERMINAL, "termAppRenderTabs")
        self.assertIn('querySelector(".tm-window-title")', body)
        self.assertIn('(active && active.title) || "终端"', body)

    @unittest.skipUnless(shutil.which("node"), "需要 node 加载前端脚本")
    def test_script_loads_before_the_assets_bundle(self):
        """app-terminal.js 先于 /app-assets.js 加载，顶层不能调用那一包里的函数。

        2026-10-16 实见：顶层调用 _appTilePlain 在页面加载时报 ReferenceError，本文件其后的
        常量全部无法初始化，openTerminalApp() 抛出 “Cannot access '_TM_ICON_TAB' before initialization”。
        """
        index = open(os.path.join(FRONT, "index.html"), encoding="utf-8").read()
        self.assertLess(index.index('src="/app-terminal.js"'), index.index('src="/app-assets.js"'))
        script = r"""
const fs = require("fs"), vm = require("vm");
const ctx = vm.createContext({ document: { addEventListener() {} }, navigator: {}, console,
  localStorage: { getItem() { return null; }, setItem() {} } });
ctx.window = ctx;
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);   // 不载入 /app-assets.js
ctx._appTilePlain = (gid, art) => `<svg data-gid="${gid}">${art}</svg>`;
ctx.TERMINAL_ART = "<image/>";
const first = vm.runInContext("_tmIconTabHtml()", ctx);
if (first !== '<svg data-gid="shell"><image/></svg>') throw new Error(first);
ctx._appTilePlain = () => "changed";
if (vm.runInContext("_tmIconTabHtml()", ctx) !== first) throw new Error("not cached");
"""
        result = subprocess.run(["node", "-e", script, os.path.join(FRONT, "app-terminal.js")],
                                capture_output=True, text=True, timeout=20, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tab_uses_the_shared_square_terminal_icon(self):
        # 标签、应用列表和程序坞共用原创正方形素材，不额外套底板或染色。
        self.assertIn('_tmIconTab || (_tmIconTab = _appTilePlain("shell", TERMINAL_ART))', TERMINAL)
        self.assertIn('const TERMINAL_ART = \'<image href="/icons/app-terminal-3b65e9edf830.png" width="24" height="24"/>\';', ASSETS)
        self.assertIn('[/终端|terminal|shell/i, _appTilePlain("shell", TERMINAL_ART)]', ASSETS)
        self.assertIn('${_tmIconTabHtml()}</span>', TERMINAL)
        self.assertIn('openContentPage(TERMINAL_PAGE_KEY, "终端", _tmIconTabHtml()', TERMINAL)
        self.assertNotIn('openContentPage(TERMINAL_PAGE_KEY, "终端", ">_"', TERMINAL)
        # 图标仍保留在 DOM 供语义与后续紧凑布局使用；iTerm 形制下默认隐藏，避免重复标识。
        self.assertIn(".tm-tab .ps-ic { display: none; }", CSS)
        self.assertIn(".tm-tab .ps-ic svg { transform: translateY(1.5px); }", CSS)

    def test_terminal_artifact_and_manifest_share_fingerprinted_asset(self):
        import hashlib
        from pathlib import Path
        from PIL import Image
        icon_path = "/icons/app-terminal-3b65e9edf830.png"
        icon = Path(FRONT) / icon_path.lstrip("/")
        data = icon.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         "3b65e9edf830e3eb909b24ad7ec0cca9f133aad95435866d37f6d5a58d103d6d")
        self.assertEqual(data, (Path(FRONT) / "icons/app-terminal.png").read_bytes())
        with Image.open(icon) as image:
            self.assertEqual(image.size, (512, 512))
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getchannel("A").getextrema(), (0, 255))
        apps = json.loads((Path(FRONT) / "workbench.json").read_text(encoding='utf-8'))["desktopApps"]
        self.assertEqual(next(app for app in apps if app["id"] == "shell")["icon"], icon_path)
        self.assertIn(icon_path.lstrip("/"), (Path(FRONT) / "vendor/ICONS-LICENSE.txt").read_text(encoding='utf-8'))
        self.assertIn('.ap-ic.img img[src^="/icons/app-terminal"], .ap-ic.tile svg.app-terminal-icon { filter: none; }', CSS)

    def test_one_page_per_app(self):
        # 页 key 固定,整个应用只有一个页面;「运行中」退化成「这个应用的页面在不在」
        self.assertIn('const TERMINAL_PAGE_KEY = "terminal";', TERMINAL)
        self.assertIn('pages.find((x) => x.url === TERMINAL_PAGE_KEY)', TERMINAL)

    def test_pane_stack_has_a_measurable_box(self):
        # FitAddon 按父元素的计算高度算行数:pane 必须有确定的盒子,否则开屏几何是错的
        self.assertIn(".tm-pane { position: absolute; inset: 0;", CSS)
        self.assertIn(".overlay-content.terminal-host { padding: 0; overflow: hidden; }", CSS)
        self.assertIn(".tm-tabbar { flex: 0 0 38px;", CSS)
        self.assertIn("background: var(--terminal-tabbar)", CSS)


class EntryPoints(unittest.TestCase):
    def test_open_shell_terminal_moved_into_the_app(self):
        # 外部调用方的签名不变,实现从 app-pages.js 搬进终端应用
        self.assertNotIn("function openShellTerminal", PAGES)
        body = _fn(TERMINAL, "openShellTerminal")
        self.assertIn('api("/workbench/shell"', body)
        self.assertIn("_termAppAddTab(r.id, opts.title", body)

    def test_existing_shell_sessions_attach_as_tabs(self):
        # 会话列表里点一行临时终端:回到它那张标签,不再起一个新的 PTY
        self.assertIn('openShellTab(s.id, stripTitleGlyph(s.title) || "终端");', PANEL)
        self.assertIn("_termAppAddTab(sessionId, title", _fn(TERMINAL, "openShellTab"))

    def test_cmd_w_closes_the_inner_tab_first(self):
        # 2026-09-03:判据从 pActive 换成 focusedAppPage() 的返回值。终端可以摆在侧栏,
        # 那时 pActive 是主区的会话页,⌘W 整个落空(用户实见)。语义不变,取页的方式变了,
        # 完整口径见 test_frontend_app_shortcuts.py。
        self.assertIn('if (p.terminal && typeof termAppCloseActiveTab === "function"'
                      " && termAppCloseActiveTab()) return;", PAGES)


class PtyLifecycle(unittest.TestCase):
    def test_closing_a_tab_ends_that_pty(self):
        body = _fn(TERMINAL, "termAppCloseTab")
        self.assertIn('api(`/workbench/terminals/${encodeURIComponent(t.id)}`, { method: "DELETE" })', body)
        self.assertIn("_termAppUnmount(t)", body)   # 本地 xterm/WS 也断掉,不留孤儿连接

    def test_closing_the_app_ends_every_pty(self):
        self.assertIn('if (p.terminal && typeof terminalPageClosed === "function") terminalPageClosed();',
                      _fn(PAGES, "closePage"))
        closed = _fn(TERMINAL, "terminalPageClosed")
        self.assertIn('api(`/workbench/terminals/${encodeURIComponent(t.id)}`, { method: "DELETE" })', closed)
        self.assertIn("if (t.cwdTimer) clearInterval(t.cwdTimer)", closed)

    def test_closing_from_the_session_list_only_drops_the_tab(self):
        # 侧栏那一路已经发过删除,标签这边不再重复发,否则第二次删的是别人
        self.assertIn('if (typeof termAppDropTab === "function") termAppDropTab(s.id);', PANEL)
        self.assertIn("function termAppDropTab(sessionId) { termAppCloseTab(sessionId, false); }", TERMINAL)

    def test_last_tab_closes_the_app(self):
        self.assertIn("if (p) closePage(p);", _fn(TERMINAL, "termAppCloseTab"))


class KeepAliveAndFit(unittest.TestCase):
    def test_switching_tabs_only_hides_the_pane(self):
        # 保活:切标签不销毁 xterm 与 WS,切回来无需重放
        body = _fn(TERMINAL, "termAppActivateTab")
        self.assertIn('x.host.style.display = x === t ? "" : "none"', body)
        self.assertNotIn("dispose", body)

    def test_pane_cap_is_owned_by_the_terminal_app(self):
        # 上限原来沿用主区会话那一档(_TERM_PANE_CAP)。主区那套 pane 随 TUI 退场撤了
        # (2026-08-20),常量搬进终端应用自己拿着。超出仍按最近最少用回收,被回收的后端
        # 仍活着,切回来重连重放。
        self.assertIn("const TERM_APP_PANE_CAP = 16;", TERMINAL)
        self.assertNotIn("_TERM_PANE_CAP", ASSETS)
        self.assertIn("const cap = TERM_APP_PANE_CAP;", _fn(TERMINAL, "_termAppEvict"))
        self.assertIn("_termAppMount(t)", _fn(TERMINAL, "termAppShown"))

    def test_embed_exposes_an_explicit_refit(self):
        body = re.search(r"async function embedTerminal\(.*?\n  const refit = .*?\n  \};\n", ASSETS, re.S)
        self.assertIsNotNone(body)
        body = body.group(0)
        # 容器还没有尺寸(仍隐藏)就不动:按 0 算几何会把 PTY 的 winsize 打坏
        self.assertIn("if (disposed || !hostEl.clientWidth || !hostEl.clientHeight) return;", body)
        self.assertIn("return { term, ws, refit, dispose:", ASSETS)

    def test_refit_runs_after_the_page_is_actually_visible(self):
        show = re.search(r"function showOverlay\(p\).*?\n}\n", PAGES, re.S).group(0)
        self.assertIn('if (p.terminal && typeof termAppShown === "function") termAppShown();', show)
        # 必须在 ov.classList.add("open") 之后:之前页面还没有盒子,量出来是 0
        self.assertLess(show.index('ov.classList.add("open")'), show.index("termAppShown()"))
        self.assertIn("requestAnimationFrame", _fn(TERMINAL, "termAppRefit"))

    def test_resize_bursts_are_still_coalesced(self):
        # 拖窗口时 ResizeObserver 连发几十次,合并成停手后一次干净的 fit+resize(否则 TUI 每帧 SIGWINCH 互相打断)
        self.assertIn("function debouncedFit(fit, sendResize, ms = 250)", ASSETS)
        self.assertIn("new ResizeObserver(debouncedFit(fit, () => {", ASSETS)


class TitleAndPersistence(unittest.TestCase):
    def test_tab_title_follows_the_working_directory(self):
        body = _fn(TERMINAL, "termAppCwdPoll")
        self.assertIn("/workbench/term/cwd?id=", body)
        self.assertIn("setInterval(tick, 2000)", body)
        self.assertIn("termAppRenderTabs(); persistTerminalTabs();", body)

    def test_tabs_persist_apart_from_the_global_stack(self):
        # 应用页不进 cd_pages(persistPages 对 app 页返回 null),标签自己存一份
        self.assertIn('localStorage.setItem("cd_terminal"', _fn(TERMINAL, "persistTerminalTabs"))
        self.assertIn('localStorage.getItem("cd_terminal")', _fn(TERMINAL, "restoreTerminalTabs"))
        self.assertIn('if (typeof restoreTerminalTabs === "function") restoreTerminalTabs();',
                      _fn(PAGES, "restorePagesAndBrowser"))

    def test_old_term_tabs_migrate_into_the_app(self):
        # 旧版本存在 cd_pages 里的 term 条目转交给终端应用,按会话 id 与 cd_terminal 去重
        self.assertIn('if (typeof termAppAdoptLegacy === "function") termAppAdoptLegacy(items);',
                      _fn(PAGES, "restoreTermPages"))
        body = _fn(TERMINAL, "restoreTerminalTabs")
        self.assertIn("saved.concat(_termAppLegacy)", body)
        self.assertIn("seen.has(id)", body)
        # app 重启会杀光 PTY:死会话不留死标签
        self.assertIn("filter((t) => t.alive)", body)


EVICT_HARNESS = r"""
const TERM_APP_PANE_CAP = 3;
let _termAppLRU = [];
let termAppTabs = [];
let termAppActiveId = "";
const disposed = [];
SRC
for (const id of ["a", "b", "c", "d", "e"]) {
  termAppTabs.push({ id, embed: { dispose: () => disposed.push(id) }, host: { innerHTML: "x" } });
  termAppActiveId = id;
  _termAppTouch(id);
  _termAppEvict();
}
// 回到还留着的那张最久未用的:它成为最近用,下一次回收轮不到它
termAppActiveId = "c";
_termAppTouch("c");
_termAppEvict();
console.log(JSON.stringify({
  disposed,
  live: termAppTabs.filter((t) => t.embed).map((t) => t.id),
  lru: _termAppLRU,
}));
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class PaneRecycling(unittest.TestCase):
    def test_least_recently_used_panes_are_recycled_and_the_active_one_is_kept(self):
        src = "\n".join(_fn(TERMINAL, n) for n in ("_termAppTouch", "_termAppUnmount", "_termAppEvict"))
        out = subprocess.run(["node", "-e", EVICT_HARNESS.replace("SRC", src)],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        d = json.loads(out.stdout)
        self.assertEqual(d["disposed"], ["a", "b"])   # 最久未用的先走,当前那张始终留着
        self.assertEqual(d["live"], ["c", "d", "e"])
        self.assertEqual(d["lru"], ["d", "e", "c"])


CLOSE_HARNESS = r"""
const calls = [];
function api(u, o) { calls.push(((o && o.method) || "GET") + " " + u); return Promise.resolve({}); }
let _termAppLRU = [];
let termAppTabs = [];
let termAppActiveId = "";
let closedPage = "";
function terminalPage() { return { url: "terminal" }; }
function closePage(p) { closedPage = p.url; }
function persistTerminalTabs() {}
function termAppRenderTabs() {}
function termAppActivateTab(id) { termAppActiveId = id; }
SRC
const mk = (id) => ({ id, embed: null, cwdTimer: 0, host: { remove() {}, innerHTML: "" } });
termAppTabs = [mk("a"), mk("b")];
termAppActiveId = "a";
const out = [];
const step = (name) => out.push([name, {
  calls: calls.splice(0), tabs: termAppTabs.map((t) => t.id), active: termAppActiveId, closedPage,
}]);
termAppCloseTab("a");  step("close-a");
termAppDropTab("b");   step("drop-b");
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class CloseBehaviour(unittest.TestCase):
    def setUp(self):
        src = "\n".join(_fn(TERMINAL, n) for n in ("_termAppUnmount", "termAppCloseTab")) \
            + "\nfunction termAppDropTab(sessionId) { termAppCloseTab(sessionId, false); }\n"
        out = subprocess.run(["node", "-e", CLOSE_HARNESS.replace("SRC", src)],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.steps = dict(json.loads(out.stdout))

    def test_closing_a_tab_deletes_its_session_and_moves_on(self):
        s = self.steps["close-a"]
        self.assertEqual(s["calls"], ["DELETE /workbench/terminals/a"])
        self.assertEqual(s["tabs"], ["b"])
        self.assertEqual(s["active"], "b")
        self.assertEqual(s["closedPage"], "")   # 还有标签在,应用不关

    def test_dropping_the_last_tab_closes_the_app_without_a_second_delete(self):
        s = self.steps["drop-b"]
        self.assertEqual(s["calls"], [])        # 会话已在别处结束,不重复发删除
        self.assertEqual(s["tabs"], [])
        self.assertEqual(s["closedPage"], "terminal")


if __name__ == "__main__":
    unittest.main()
