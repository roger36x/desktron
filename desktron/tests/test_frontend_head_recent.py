"""会话页顶栏上的「最近打开的应用」。

顶栏那几枚图标,点一下回到那个应用。三处判据要守住:

1. 只给会话页。应用页自己的页头上摆一排别的应用,读起来像是这个应用的功能;而这一排的用处
   是「看会话时顺手回到刚用过的那个」,属于会话那一侧。判据取 #overlay 的 hs-head——
   showOverlay 就是按「这一页是不是会话页」切的这个类,不另立一套。
2. 记录挂在唯一的打开入口 _openDomainApp 上。各处入口(程序坞、应用页、右键菜单、快捷键)
   都经过它,记在别处必然漏。
3. 不足数拿程序坞钉住的补齐:只按「最近」摆的话,头一次用是空的、用过一次是孤零零一枚,
   而这一排的价值恰恰在这时候(2026-08-16 用户实见「没出现」,就是空态)。
"""
import os
import re
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _read(*p):
    if p and p[-1] in ("app-chat.js", "app-panel.js", "app-rail.js", "app-assets.js", "app-settings.js"):
        return _srcmap.frontend_src(p[-1])   # 2026-08-27 分拆,拼接整体等价
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


ASSETS = _read("frontend", "app-assets.js")
PAGES = _read("frontend", "app-pages.js")
INDEX = _read("frontend", "index.html")
CSS = _read("frontend", "style.css")


def fn(src, name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), src, re.S)
    assert m, "%s 不见了" % name
    return m.group(0)


class HeadRecent(unittest.TestCase):
    def test_插槽在页头动作区里(self):
        self.assertIn('<span id="ov-recent" hidden></span>', INDEX)
        i, j = INDEX.index('id="ov-recent"'), INDEX.index('id="ov-close"')
        self.assertLess(i, j)                       # 排在收起/关闭之前

    def test_只给会话页(self):
        seg = fn(ASSETS, "renderHeadRecent")
        self.assertIn('classList.contains("hs-head")', seg)
        self.assertIn("host.hidden = true", seg)
        # 判据要排在渲染之前,别先画一排再藏
        self.assertLess(seg.index('classList.contains("hs-head")'), seg.index("_allApps()"))

    def test_按哪个应用页被显示来记(self):
        """完整口径在 showOverlay,不在 _openDomainApp。

        文件、终端、浏览器三个内置应用各有自己的入口(openFilesPage / openTerminalApp /
        openBrowserApp),工作流面板的文件夹钮、会话的目录、右键菜单都直接调它们,不经过
        _openDomainApp;而切回一个已经开着的应用同样是「刚用过」。只记在打开入口上,
        这几条路一条都记不到,TOP3 就与实际用的对不上(2026-08-16 用户实见)。
        """
        seg = fn(PAGES, "showOverlay")
        self.assertIn("if (p.app && p.appid && typeof _appRecentPush === \"function\")", seg)
        # 要排在 renderHeadRecent 之前,否则这一次切页画的还是上一次的次序
        self.assertLess(seg.index("_appRecentPush(p.appid)"), seg.index("renderHeadRecent()"))

    def test_agentic_详情页仍走打开入口(self):
        # 那一类不建页对象(openAgenticApp 里 pActive = null),showOverlay 覆盖不到
        seg = fn(ASSETS, "_openDomainApp")
        self.assertIn("_appRecentPush(_dockAppKey(a))", seg)
        self.assertLess(seg.index("_appRecentPush"), seg.index("openTerminalApp"))

    def test_不足数用钉住的补齐(self):
        seg = fn(ASSETS, "renderHeadRecent")
        self.assertIn("_dockApps()", seg)
        self.assertIn("rows.length >= _APP_RECENT_SHOW", seg)
        self.assertIn("has.has(x.key)", seg)        # 补的不能与最近的重复

    def test_摆几枚只有一个口径(self):
        # 取最近那一段与补齐那一段读同一个常量,改一处即可;写死两个数必然漂
        self.assertIn("const _APP_RECENT_SHOW = ", ASSETS)
        seg = fn(ASSETS, "renderHeadRecent")
        self.assertEqual(seg.count("_APP_RECENT_SHOW"), 2)
        self.assertNotIn("slice(0, 3)", seg)

    def test_已经开着的迁进侧栏_不新开(self):
        seg = fn(ASSETS, "renderHeadRecent")
        self.assertIn("_dockRunningPage(x)", seg)
        self.assertIn("showAppSide(pg)", seg)

    def test_切页时重画(self):
        seg = fn(PAGES, "showOverlay")
        self.assertIn("renderHeadRecent()", seg)
        # 要排在 hs-head 那个类切完之后,否则判据读到的是上一页的状态
        self.assertLess(seg.index('"hs-head"'), seg.index("renderHeadRecent()"))

    def test_顶栏点击框居中且投影覆盖优先于全局瓷贴(self):
        button = re.search(r"\.ov-rc \{([^}]+)\}", CSS).group(1)
        for rule in ("width: 23px", "height: 23px", "padding: 0",
                     "display: inline-grid", "place-items: center"):
            self.assertIn(rule, button)
        self.assertIn("#ov-recent .ov-rc .ap-ic svg", CSS)
        self.assertIn("#ov-recent .ov-rc .ap-ic.img img", CSS)
        icon = re.search(r"\.ov-rc \.ap-ic \{([^}]+)\}", CSS).group(1)
        self.assertIn("box-shadow: none", icon)
        self.assertIn("text-shadow: none", icon)

    def test_透明留白补差仅限顶栏不改原图(self):
        for path, transform in (
            ("app-terminal.png", "translateY(.09px) scale(1.02128)"),
        ):
            selector = '#ov-recent .ov-rc img[src="/icons/' + path + '"]'
            self.assertIn(selector + " { transform: " + transform + "; }", CSS)
        self.assertIn('width: 17px; height: 17px', CSS)

    def test_图标沿用应用面板那一套(self):
        seg = fn(ASSETS, "renderHeadRecent")
        self.assertIn("_appIconHTML(", seg)
        self.assertIn(".ov-rc .ap-ic {", CSS)       # 尺寸在这里收,不改面板那份
