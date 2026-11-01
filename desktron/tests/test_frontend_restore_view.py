"""刷新之后回到刷新前看的那一页(2026-08-24 用户在手机上实见:刷新落在工坊,而人是在会话里刷的)。

persistPages 一直在写 open(浮层当时露着没有)与 active(看的哪一页),而 restorePages
只把页建出来(show=false)、这两个字段从来没人读。于是刷新后落在哪,由侧栏 nav(cd_nav)
说了算——上次点过工坊就停在工坊。

同一处还留着一个失效的补偿:init 里 selectNav 传 noTermSync:true,理由写的是
「下面 restoreTerminal 按 cd_term 单独恢复」,而那个函数随 TUI 退场一起删了。
补偿的人没了,闸还关着。
"""
import json
import os
import re
import shutil
import subprocess
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PAGES = open(os.path.join(ROOT, "frontend", "app-pages.js"), encoding="utf-8").read()
INIT = open(os.path.join(ROOT, "frontend", "app-init.js"), encoding="utf-8").read()
EVENTS = open(os.path.join(ROOT, "frontend", "app-events.js"), encoding="utf-8").read()
TOPNAV = open(os.path.join(ROOT, "frontend", "app-rail-topnav.js"), encoding="utf-8").read()


def restore_src():
    """取 restorePages 的函数体,连同它读 open/active 那一段。"""
    m = re.search(r"\nfunction restorePages\(\) \{.*?\n\}\n", PAGES, re.S)
    assert m, "restorePages 不见了"
    return m.group(0)


HARNESS = r"""
let shown = null, built = [];
let shepherdMode = MODE;
const pages = [];
function openHtmlSession(id, title, show){ const p = {url: "chat:" + id, title}; pages.push(p); built.push([id, show]); }
function openPage(){}
function browserHandlesUrl(){ return false; }
function restoreTermPages(){}
function restoreFilesPages(){}
function showOverlay(p){ shown = p.url; }
function persistPages(){}
let _restoringPages = false;
const localStorage = { getItem: (k) => (k === "cd_pages" ? DATA : null) };
__RESTORE__
restorePages();
console.log(JSON.stringify({ shown, built }));
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class RestoreVisiblePage(unittest.TestCase):
    def _run(self, data, shepherd=False):
        js = HARNESS.replace("__RESTORE__", restore_src()).replace(
            "DATA", json.dumps(json.dumps(data, ensure_ascii=False))).replace("MODE", json.dumps(shepherd))
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_刷新回到当时看的那段会话(self):
        got = self._run({"list": [{"kind": "chat", "id": "adp_a", "title": "甲"},
                                  {"kind": "chat", "id": "adp_b", "title": "乙"}],
                         "open": True, "active": "chat:adp_b"})
        self.assertEqual(got["shown"], "chat:adp_b", "没回到刷新前那一页")

    def test_不是最后建的那一页(self):
        """页是按打开顺序建的,而人最后看的未必是最后打开的。照顺序取末尾就会回错。"""
        got = self._run({"list": [{"kind": "chat", "id": "adp_a", "title": "甲"},
                                  {"kind": "chat", "id": "adp_b", "title": "乙"}],
                         "open": True, "active": "chat:adp_a"})
        self.assertEqual(got["shown"], "chat:adp_a")

    def test_当时浮层没开就不抢(self):
        """人正看着工坊、会话只是留在后台没关,那就该停在工坊。"""
        got = self._run({"list": [{"kind": "chat", "id": "adp_a", "title": "甲"}],
                         "open": False, "active": "chat:adp_a"})
        self.assertIsNone(got["shown"], "浮层当时没开却把它翻了出来")

    def test_记的那一页已经不在就不露面(self):
        """会话删了/建不出来时,active 指向一个没有的地址,不能拿它去 showOverlay。"""
        got = self._run({"list": [{"kind": "chat", "id": "adp_a", "title": "甲"}],
                         "open": True, "active": "chat:adp_gone"})
        self.assertIsNone(got["shown"])

    def test_恢复出来的页都不抢先露面(self):
        """建的时候一律 show=false:露面只有一次,由 open/active 决定是哪一页。"""
        got = self._run({"list": [{"kind": "chat", "id": "adp_a", "title": "甲"},
                                  {"kind": "chat", "id": "adp_b", "title": "乙"}],
                         "open": True, "active": "chat:adp_b"})
        self.assertEqual([b[1] for b in got["built"]], [False, False])

    def test_没有记录就什么都不做(self):
        got = self._run({"list": [], "open": True, "active": "chat:adp_a"})
        self.assertIsNone(got["shown"])

    def test_两种模式仅恢复自己的会话标签(self):
        data = {"list": [{"kind": "chat", "id": "ordinary", "engine": "pi"},
                          {"kind": "chat", "id": "sheep", "engine": "claude", "shepherd_mode": True}],
                "open": True, "active": "chat:sheep"}
        ordinary = self._run(data)
        self.assertEqual(ordinary["built"], [["ordinary", False]])
        self.assertIsNone(ordinary["shown"])
        shepherd = self._run(data, shepherd=True)
        self.assertEqual(shepherd["built"], [["sheep", False]])
        self.assertEqual(shepherd["shown"], "chat:sheep")


class StaleCompensationCalledOut(unittest.TestCase):
    """noTermSync 的理由曾指向一个已经删掉的函数。留着不改,下一个人会照它推断而想错。"""

    def test_init_不再说是_restoreTerminal_负责(self):
        """旧说法可以留,但只能以「原来写的是」的身份留——记下被推翻的判断是这仓库的惯例,
        它挡住重复论证;当成现行说明留着才是害人。"""
        seg = INIT[INIT.index("let startNav"):INIT.index("initDock()")]
        self.assertIn("noTermSync: true", seg)
        self.assertIn("cd_pages", seg, "没说清现在由谁恢复")
        stale = "restoreTerminal 按 cd_term 单独恢复"
        if stale in seg:
            before = seg[:seg.index(stale)]
            self.assertIn("原来写的是", before[-40:],
                          "旧说法还摆在现行说明的位置上,没标成已推翻")

    def test_恢复那一步在_selectNav_之后(self):
        """顺序要紧:先按 cd_nav 摆好侧栏,再把当时看的那一页盖上来。"""
        self.assertLess(INIT.index("selectNav(startNav.type"),
                        INIT.index("restorePagesAndBrowser()"))


class RestoreSidebarHighlight(unittest.TestCase):
    """选中态也要跟着回来(2026-08-25 用户实见:刷新之后主区开着会话,侧栏没有一行是亮的)。

    上面那条把会话页恢复回来了,却只恢复了一半:activeTermId 原来由 restoreTerminal
    按 cd_term 恢复,那个函数随 TUI 退场删了,补偿的人没了——侧栏据以上色的正是这个变量。
    """

    PANEL = _srcmap.panel_src()

    def _fn(self):
        m = re.search(r"function restoreActiveTerm\(\) \{.*?\n\}", self.PANEL, re.S)
        self.assertIsNotNone(m, "restoreActiveTerm 不见了")
        return m.group(0)

    def test_开机时调它(self):
        self.assertIn("restoreActiveTerm()", INIT)

    def test_排在页面恢复之后(self):
        """它要看主区恢复出来的是不是会话页,早于那一步就什么都判不出来。"""
        self.assertLess(INIT.index("restorePagesAndBrowser()"),
                        INIT.index("restoreActiveTerm()"))

    def test_从主区当前页取而不是另读旧键(self):
        """正文页才是权威态；另读 cd_term 会重新制造正文 A、边栏 B。"""
        fn = self._fn()
        self.assertIn('const sid = cur.slice(5)', fn)
        self.assertNotIn('localStorage.getItem("cd_term")', fn)
        self.assertIn('localStorage.setItem("cd_term", sid)', fn)

    def test_设的是变量而不只是加class(self):
        """侧栏每一轮重渲都按 activeTermId 重建 class,只加 class 会被下一拍冲掉。"""
        fn = self._fn()
        self.assertIn("activeTermId = sid", fn)
        at = fn.index("activeTermId = sid")
        self.assertLess(at, fn.index("classList.toggle"), "先上色后设变量,顺序反了")

    def test_主区不是会话页就不认(self):
        """上次看的是工坊或某个应用的话,不该有哪一行亮着。"""
        fn = self._fn()
        self.assertIn('cur.startsWith("chat:")', fn)

    def test_最近会话已收起也不认(self):
        """hideOverlay 会保留 pActive 供返回使用；是否真在前台要再看 overlayOpen。"""
        self.assertIn("!overlayOpen", self._fn())

    def test_已有高亮也要按正文纠偏(self):
        """activeTermId 可能来自旧键，不能因为它非空就保留错误的 B。"""
        self.assertNotIn("if (activeTermId) return;", self._fn())

    def test_两种取行id的写法都认(self):
        """行匹配走统一助手；它会同时识别引擎锚点和会话页的 adp_ id。"""
        self.assertIn("termRowMatchesId(x, sid)", self._fn())

    def test_恢复期间也立即同步可见会话的选中态(self):
        """恢复闸只阻止中间页面反写引擎快照,不能阻止最终可见页给侧栏上色。

        冷启动末尾虽有 restoreActiveTerm 兜底,但页面恢复还有别的调用路径；把会话 id
        同步包在 _restoringEngineViewState 里,就会出现正文已打开、行却没有高亮。
        """
        show = PAGES[PAGES.index("function showOverlay"):PAGES.index("function overlayHomePage")]
        sync = show.index('if (String(p.url || "").startsWith("chat:"))')
        guard = show.index('if (!_restoringEngineViewState && typeof curEngine === "string")')
        self.assertLess(sync, guard)
        self.assertLess(sync, show.index('if (String(p.url || "").startsWith("chat:") && typeof hsShown'))
        self.assertIn("activeTermId = sid", show[sync:guard])
        self.assertIn("classList.toggle(\"active\"", show[sync:guard])


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class SidebarSessionIdentityAliases(unittest.TestCase):
    """页面和侧栏持有不同 id 时仍指向同一段会话。"""

    def test_引擎id与Desk会话id都能命中(self):
        panel = _srcmap.panel_src()
        names = ("termIdentityIds", "termMatchesId", "termRowMatchesId")
        funcs = []
        for name in names:
            m = re.search(rf"function {name}\([^)]*\) \{{.*?\n\}}", panel, re.S)
            self.assertIsNotNone(m, name + " 不见了")
            funcs.append(m.group(0))
        js = "\n".join(funcs) + r'''
const term = {id: "thread-1", resume_id: "resume-1", session_id: "adp_ui"};
const row = {dataset: {sid: "thread-1", resumeId: "resume-1", sessionId: "adp_ui"}, title: ""};
console.log(JSON.stringify([
  termMatchesId(term, "thread-1"), termMatchesId(term, "adp_ui"),
  termRowMatchesId(row, "thread-1"), termRowMatchesId(row, "adp_ui"),
  termMatchesId(term, "other"), termRowMatchesId(row, "other")
]));
'''
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [True, True, True, True, False, False])


class EngineSwitchDropsLastPage(unittest.TestCase):
    """切引擎之后,上一个引擎的最后一段会话不该还摆在主区(2026-08-25 用户实见)。

    恢复那侧本来就按引擎筛过一道(f3596f7),但它挡不住这一页:persistPages 只在视图已经
    解析出 engine 时才写这个字段,而恢复出来没打开过的冷页不跑 hsEngineSync、永远解析不出来
    ——实见 63 条记录里 45 条没有 engine,筛选对它们一律放行。
    """

    EVENTS = EVENTS

    def _switch(self):
        at = self.EVENTS.index('body: JSON.stringify({ engine: id, shepherd_mode: false })')
        return self.EVENTS[at:self.EVENTS.index("location.reload()", at) + 20]

    def test_给那一页补上归属而不是抹掉(self):
        """此刻正看着它,它当然属于切换前那个引擎。补完之后恢复那侧自己就筛得掉。"""
        seg = self._switch()
        self.assertIn("row.engine = curEngine", seg)

    def test_不抹掉active(self):
        """抹掉的话切回这个引擎时也回不到它了,而那正是人想要的
        (2026-08-25 实测:补归属之后,切到 codex 主区落空态、切回 claude 又回到原会话)。"""
        seg = self._switch()
        self.assertNotIn("d.active = null", seg)
        self.assertNotIn("removeItem", seg, "把整份记录删了")

    def test_已经写过归属的不动(self):
        """视图解析出来的那个才是准的,别拿当前偏好去覆盖。"""
        self.assertIn("!row.engine", self._switch())

    def test_补在重载之前(self):
        seg = self._switch()
        self.assertLess(seg.index("row.engine = curEngine"), seg.index("location.reload()"))

    def test_记下过的引擎要接回视图(self):
        """不接回的话,上一次记下的 engine 在下一次落盘时被丢掉,记录越用越空,
        按引擎筛那道闸也就越来越形同虚设。"""
        seg = restore_src()   # 只看 restorePages 的函数体:别处也有同名的建页分支
        self.assertIn("if (v && d.engine && !v.engine) v.engine = d.engine;", seg)
        self.assertLess(seg.index("openHtmlSession(d.id, d.title, false)"),
                        seg.index("v.engine = d.engine"), "在页建出来之前就去接,拿不到视图")


class EngineScopedLastView(unittest.TestCase):
    """每个引擎各自记住切走前的主区位置，而不是争用一个 cd_pages.active。"""

    def _switch(self):
        at = EVENTS.index('body: JSON.stringify({ engine: id, shepherd_mode: false })')
        return EVENTS[EVENTS.rfind("row.onclick", 0, at):EVENTS.index("location.reload()", at) + 20]

    def test_切换前先保存旧引擎当前位置(self):
        seg = self._switch()
        self.assertIn("rememberEngineView(curEngine)", seg)
        self.assertLess(seg.index("rememberEngineView(curEngine)"), seg.index('api("/workbench/engine"'))

    def test_位置按引擎_id_分开存(self):
        self.assertIn('const _ENGINE_VIEWS_KEY = "cd_engine_views"', TOPNAV)
        self.assertIn("all[id] = view", TOPNAV)
        self.assertIn("_engineViewsRead()[engineId]", TOPNAV)

    def test_会话_功能页_欢迎页三种位置都能表达(self):
        self.assertIn('view = { kind: "chat"', TOPNAV)
        self.assertIn('view = { kind: "nav"', TOPNAV)
        self.assertIn('view = { kind: "welcome" }', TOPNAV)

    def test_应用页也按_appid_恢复(self):
        self.assertIn('view = { kind: "app", appid:', TOPNAV)
        self.assertIn("x.appid === view.appid", TOPNAV)
        self.assertIn("_openDomainApp(found.a, found.domainId)", TOPNAV)

    def test_当前引擎视图在页面建好后恢复(self):
        call = "await restoreEngineView(curEngine)"
        self.assertIn(call, INIT)
        self.assertLess(INIT.index("restorePagesAndBrowser()"), INIT.index(call))
        self.assertLess(INIT.index(call), INIT.index("restoreActiveTerm()"))

    def test_有引擎快照时不再强制欢迎页(self):
        tail = INIT[INIT.index("let engineViewRestored"):INIT.index("\n  // 这里原来还有一句")]
        self.assertIn("if (!engineViewRestored)", tail)
        self.assertLess(tail.index("if (!engineViewRestored)"),
                        tail.index('if (typeof pActive === "undefined" || !pActive)'))

    def test_旧数据首次启动后种下当前引擎位置(self):
        tail = INIT[INIT.index("let engineViewRestored"):INIT.index("\n  // 这里原来还有一句")]
        self.assertIn('if (typeof rememberEngineView === "function") rememberEngineView(curEngine)', tail)

    def test_日常切页即时更新而非只在切引擎时保存(self):
        self.assertIn("rememberEngineView(curEngine)", PAGES[PAGES.index("function showOverlay"):PAGES.index("function overlayHomePage")])
        nav = TOPNAV[TOPNAV.index("function navPush"):TOPNAV.index("function updateNavBtns")]
        self.assertIn("rememberEngineView(curEngine)", nav)

    def test_恢复建页期间禁止旧全局页覆盖快照(self):
        seg = INIT[INIT.index("let engineViewRestored"):INIT.index("restoreActiveTerm()")]
        self.assertIn("_restoringEngineViewState = true", seg)
        self.assertIn("_restoringEngineViewState = false", seg)
        self.assertLess(seg.index("_restoringEngineViewState = true"), seg.index("restorePagesAndBrowser()"))

    def test_初始导航也处于恢复保护中(self):
        """selectNav(startNav) 早于 restorePages；保护若默认关闭，会先把全局工坊页写进目标引擎。"""
        self.assertIn("let _restoringEngineViewState = true;", TOPNAV)
        self.assertLess(INIT.index("selectNav(startNav.type"), INIT.index("restorePagesAndBrowser()"))

    def test_旧版会话快照用分引擎会话键迁移一次(self):
        self.assertIn("view.rev !== _ENGINE_VIEW_REV", TOPNAV)
        self.assertIn('localStorage.getItem("cd_term:" + engineId)', TOPNAV)
        self.assertIn("view.rev = _ENGINE_VIEW_REV", TOPNAV)

    def test_会话恢复按该引擎记住的_id_找页(self):
        self.assertIn('pages.find((x) => x.url === "chat:" + view.id)', TOPNAV)
        self.assertIn("showOverlay(p, navigationEpoch)", TOPNAV)

    def test_功能页复用现有导航重放器(self):
        self.assertIn("const ok = _navReplay(view.entry)", TOPNAV)
        self.assertIn('_navHist.stack = [view.entry]', TOPNAV)


class NoBlankOverlayWhenNothingRestored(unittest.TestCase):
    """一页都没露面时,主区不能是一整块空白浮层(2026-08-25 用户实见:主区空、侧栏在)。

    #overlay 在 index.html 里不带 hidden,初始就是显示的,而只有 showView(非 overlay) 会去藏它
    ——init 这条路带着 noTermSync 走,不经过那里。
    """

    def _tail(self):
        return INIT[INIT.index("restorePagesAndBrowser()"):INIT.index("\ninit()")]

    def test_没有页时收掉浮层(self):
        seg = self._tail()
        self.assertIn("if (typeof pActive === \"undefined\" || !pActive)", seg)
        self.assertIn('showView("terminal")', seg)

    def test_摆回欢迎页(self):
        """只收浮层的话,露出来的是一个空的终端视图,一样是白的。"""
        self.assertIn("_showWatermark()", self._tail())

    def test_摘掉终端在场那个标(self):
        """空态是终端视图却没有终端,留着它手机端会凭空多出一条附加键栏。"""
        self.assertIn('classList.remove("term-on")', self._tail())

    def test_不走clearTerminalView(self):
        """它会顺手删掉 cd_term,而那一项是侧栏高亮的唯一来源,删了下次就找不回
        (见 RestoreSidebarHighlight)。"""
        self.assertNotIn("clearTerminalView()", self._tail())

    def test_排在页面恢复之后(self):
        """早于那一步就判不出到底有没有页露面。"""
        self.assertLess(INIT.index("restorePagesAndBrowser()"),
                        INIT.index("if (typeof pActive === \"undefined\" || !pActive)"))


class OneFailureIsNotPermanent(unittest.TestCase):
    """恢复失败只该影响这一次,不能把记录本身毁掉(2026-08-25 用户实见:刷新总是欢迎页)。

    restorePages 末尾要落一次盘(旧的 term/files 条目转交完就不该再留在记录里),而
    _restoringPages 已经在那之前放开了,所以那是真写。此刻的 pActive 反映的是「恢复成功没有」,
    不是人的意图:active 指的那一页要是没建出来(被引擎筛掉、会话已删、建的时候抛了),
    pActive 就是空,照写便把 active 抹成 null——而这份记录是唯一的来源,抹一次就再也回不去。
    """

    def test_收尾那次写回不动open和active(self):
        seg = restore_src()
        self.assertIn("persistPages({ keep: { open: data.open, active: data.active } })", seg)

    def test_persistPages认这个参数(self):
        m = re.search(r"function persistPages\(opts\) \{.*?\n\}", PAGES, re.S)
        self.assertIsNotNone(m, "persistPages 不收参数了")
        seg = m.group(0)
        self.assertIn("const keep = opts && opts.keep", seg)
        self.assertIn("open: keep ? !!keep.open : overlayOpen", seg)
        self.assertIn("active: keep ? (keep.active || null) : (pActive ? pActive.url : null)", seg)

    def test_平时照旧按当前状态写(self):
        """只有恢复收尾那一次特殊。关页、开页这些正常路径仍要如实记下当前看的是哪一页,
        否则关掉最后一页之后,记录还指着它。"""
        m = re.search(r"function persistPages\(opts\) \{.*?\n\}", PAGES, re.S).group(0)
        self.assertIn("pActive ? pActive.url : null", m, "不传 keep 时不再按当前状态写了")

    def test_别处调用不受影响(self):
        """persistPages 有很多调用点,加参数不能要求它们全改。"""
        n = len(re.findall(r"persistPages\(\)", PAGES))
        self.assertGreater(n, 1, "别处的无参调用都被改掉了,说明这个参数不是可选的")
