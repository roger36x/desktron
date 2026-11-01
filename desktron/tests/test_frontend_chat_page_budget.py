"""会话页的 DOM 规模要按「同时看得见几页」封顶,不按「开过几页」。

刷新时 restorePages 会把留着的会话页一次全建出来,而建页立刻灌整段历史。开得多了,不在看的
那些也各自把上万个节点铺进 DOM——2026-08-15 在本机窗口上探到 50 页、23.3 万个节点,当前那一页
只占 301 个。此后任何一次全局的样式失效或几何读取都要在这 23 万个节点上走一遍:点刻度、
悬停、点侧栏都没有反应,整页冻住十秒,而滚动照常(那条路在合成器上,不经主线程)。

修法两条,都在这里守:
  1. 盖住的会话页只建空壳,历史等它露面再灌(hsBuild 里按 hsHidden 分流,showOverlay 里补灌);
  2. 同时留着正文的会话页有上限,超出的按「最久没看」清空正文,再次露面时重灌。

清的只是正文:视图对象、草稿、队列、引擎档位都留着——那些没有后端副本,清掉就真丢了。
在跑的那一页也不清,它正在收流式帧。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
PAGES = open(os.path.join(ROOT, "frontend", "app-pages.js"), encoding="utf-8").read()


def fn(src, name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), src, re.S)
    assert m, "%s 不见了" % name
    return m.group(0)


class ChatPageBudget(unittest.TestCase):
    def test_盖住的会话页不灌历史(self):
        seg = fn(CHAT, "hsBuild")
        hidden = re.search(r"else if \(hsHidden\(v\)\) \{[\s\S]+?\} else \{", seg).group(0)
        self.assertIn("v.needLoad = true", hidden)
        self.assertNotIn("hsLoad(v)", hidden)
        self.assertIn("else hsLoad(v).then(", seg)

    def test_盖住的会话页不取元数据(self):
        """目标、任务、环境三条与历史同一口径:建页时盖着就延后。2026-09-05 实测九页刷新
        开机 20 秒内 goal 与 situation 各 61 次,把当前页的 /message 挤到 init 完成 5 秒后。"""
        seg = fn(CHAT, "hsBuild")
        hidden = re.search(r"else if \(hsHidden\(v\)\) \{[\s\S]+?\} else \{", seg).group(0)
        self.assertIn("v.needMeta = true", hidden)
        for call in ("hsGoalSync(v)", "hsTasksSync(v)", "hsSituationSync(v)"):
            self.assertNotIn(call, hidden, call + " 不该在盖住时发")
        wire = fn(CHAT, "hsSituationWire")
        self.assertIn("!hsHidden(v)) hsSituationSync(v)", wire)
        shown = fn(CHAT, "hsShown")
        self.assertIn("hsSituationSync(v)", shown)
        self.assertIn("if (v.needMeta)", shown)
        self.assertIn("hsGoalSync(v)", shown)
        self.assertIn("hsTasksSync(v)", shown)

    def test_露面时补灌且不等能力位(self):
        seg = fn(CHAT, "hsShown")
        self.assertIn("v.needLoad", seg)
        self.assertIn("hsLoad(v)", seg)
        self.assertNotIn("v.capsReady", seg)
        self.assertIn("hsEvictColdSoon(v)", seg)

    def test_能力与历史并行(self):
        seg = fn(CHAT, "hsBuild")
        self.assertIn("hsEngineSync(v);", seg)
        self.assertIn("else hsLoad(v).then(", seg)
        self.assertNotIn("capsReady.then(() => hsLoad(v)", seg)

    def test_showOverlay_对会话页调_hsShown(self):
        seg = fn(PAGES, "showOverlay")
        self.assertIn('String(p.url || "").startsWith("chat:")', seg)
        self.assertIn("hsShown(", seg)

    def test_有上限且是小数(self):
        m = re.search(r"const HS_WARM_MAX = (\d+);", CHAT)
        self.assertIsNotNone(m, "没有上限就等于没封顶")
        self.assertLessEqual(int(m.group(1)), 8)

    def test_清正文不销毁视图(self):
        seg = fn(CHAT, "hsEvictCold")
        self.assertIn('v.stream.innerHTML = ""', seg)
        self.assertIn("v.turns = []", seg)
        self.assertIn("v.needLoad = true", seg)
        # 草稿、队列、引擎档位挂在视图上,没有后端副本
        for gone in ("_hsViews.delete", "v.el.remove()", "v.queue = []", "v.inp.textContent"):
            self.assertNotIn(gone, seg, "清冷页时不该动 " + gone)

    def test_在跑的和正在看的不清(self):
        """正在看的整段跳过;在跑的不清正文——v.turns 一空,它正在收的那一轮就没有落点了。"""
        seg = fn(CHAT, "hsEvictCold")
        self.assertIn("v === keep || !hsHidden(v)", seg)
        self.assertLess(seg.index("if (v.busy)"), seg.index("v.turns = []"),
                        "整页清的那几行跑在了 busy 判断前面")

    def test_在跑的那页仍要裁到上限(self):
        """原来 busy 一律整段跳过。密集并行时每一段都在跑,这道闸于是一段都收不掉
        (2026-08-24 实测六段全 busy,界面进程物理足迹 5.79GB)。退一档:裁到 HS_DOM_TURNS。"""
        seg = fn(CHAT, "hsEvictCold")
        self.assertIn("if (v.busy) { hsTrimDom(v); continue; }", seg)

    def test_淘汰冷页不阻塞点击切换(self):
        shown = fn(CHAT, "hsShown")
        soon = fn(CHAT, "hsEvictColdSoon")
        self.assertNotIn("hsEvictCold(v)", shown)
        self.assertIn("requestIdleCallback", soon)
        self.assertIn("setTimeout(run, 80)", soon)

    def test_清完把翻页与刻度的状态一并还原(self):
        # 翻页按 v.turns.length 算 have,刻度带着 turn 引用;不还原就会错位或指向已摘除的节点
        seg = fn(CHAT, "hsEvictCold")
        for k in ("v.paged = false", "v.moreDone = false", "v.tkOn = null", "v.pendRest = null"):
            self.assertIn(k, seg)

    def test_清完停掉这一页的轮询(self):
        seg = fn(CHAT, "hsEvictCold")
        self.assertIn("clearInterval(v.followTimer)", seg)
        self.assertIn("clearInterval(v.statusTick)", seg)



class NoSelfPagination(unittest.TestCase):
    """打开会话页不该自己往更早翻。

    延后灌历史之后暴露出来的:灌完那一刻 scrollTop 本来就是 0(内容不满一屏,或定型前的
    中间态),而滚动处理里「scrollTop < 120 就翻页」于是立刻自己触发一次,页面刚打开就无端
    多出三十个更早的回合,刻度那一列跟着长出一截(2026-08-15 用户实见)。
    往更早翻是一个用户动作,判据要带上人的意图,不只看位置。

    第一版的意图判据是「人滚过 && scrollTop < 120」,而那个「滚过」一次置真、终生有效,
    于是这一页只要被人滚过一次,此后任何程序性的滚动只要路过顶端就翻页。点刻度跳转走的是
    scrollIntoView({behavior:"smooth"}),跳到靠前的回合时必然落进 120px 以内;翻页又往上
    插三十个回合,那次平滑滚动仍朝原来的数值目标走,插完之后目标又落回顶端——一次点击能
    连着灌进好几段历史(2026-08-17 用户实见:什么都没做,刻度自己长出一大截)。
    受控复现:同样点一次刻度,旧判据开闸 30 次,新判据 0 次。

    改法是让手势自己发起翻页:滚轮向上、上翻键、手指往下拖各自带着方向,程序性的滚动不产生
    这些事件。拖滚动条不发手势事件,只能从 scroll 认,那一路要求指针正按着。

    同一处还守首屏两批铺时刻度只画一次:先按前几个回合画、再跳成整段,读起来同样是
    「不知从哪儿冒出来一截」。
    """
    def test_翻页不再挂在scroll事件上(self):
        seg = fn(CHAT, "hsBuild")
        self.assertNotIn("v.userScroll", seg)
        # scroll 里剩下的翻页只有拖滚动条那一路,且要求正按着
        self.assertIn("const dragUp = v.dragging &&", seg)
        self.assertIn("if (dragUp) { hsUnstick(v); pageBack(); }", seg)

    def test_三种手势各自带方向(self):
        seg = fn(CHAT, "hsBuild")
        self.assertIn("if (e.deltaY < 0) { hsUnstick(v); pageBack(); }", seg)      # 滚轮向上
        self.assertIn("if (v.touchTop !== undefined && top < v.touchTop) { hsUnstick(v); pageBack(); }", seg)
        self.assertIn('if (/^(PageUp|Home|ArrowUp)$/.test(e.key)) { hsUnstick(v); pageBack(); }', seg)
        # 往下的那几个键不该翻页:End / PageDown 走的是回到最新
        self.assertNotIn("PageDown", seg)

    def test_位置判据只此一处(self):
        """三种手势加拖拽都经 pageBack,阈值不散落成四份。"""
        seg = fn(CHAT, "hsBuild")
        self.assertIn("const pageBack = () => { if (!v.historyLoading && v.body.scrollTop < 120) hsMore(v); };", seg)
        code = "\n".join(ln for ln in seg.splitlines() if not ln.lstrip().startswith("//"))
        self.assertEqual(code.count("scrollTop < 120"), 1)

    def test_重灌时把方向判据清零(self):
        """留着上一份的位置,重灌后第一次 scroll 会读成「正在往上滚」。"""
        seg = fn(CHAT, "hsLoad")
        for k in ("v.lastTop = undefined", "v.touchTop = undefined", "v.dragging = false"):
            self.assertIn(k, seg)
        self.assertLess(seg.index("v.lastTop = undefined"), seg.index("hsPourFast"))

    def test_第二批还欠着时不画刻度(self):
        seg = fn(CHAT, "hsLoad")
        self.assertIn("if (!v.pendRest) hsIndex(v);", seg)
if __name__ == "__main__":
    unittest.main()


class FileViewerHasWayOut(unittest.TestCase):
    """从会话里点开的文件要有来处。

    这一页不进标签条(会话与应用都不进),页头那对「收起 / 关闭」由 opts.app 决定
    (showOverlay 里 `classList.toggle("app-page", !!p.app)`,显隐规则在 style.css 的
    `#overlay.app-page #ov-min, #overlay.app-page #ov-close`)。hsOpenFile 当初漏了这一项,
    于是打开一份文件之后既关不掉也收不起,只能切到别的页才走得掉(2026-08-15 用户两次实见)。
    终端、文件、备忘几个页面本来都带着它。
    """
    def test_点开文件按应用页打开(self):
        seg = fn(CHAT, "hsOpenFile")
        self.assertIn('openContentPage(key, name, "≡"', seg)
        self.assertIn("{ app: true }", seg)


class IdeHandoff(unittest.TestCase):
    """文件查看页要能交接给编辑器。

    那一页只管读:markdown 按文档渲染、`:line` 定位并高亮。要改、要在同一个仓里跳来跳去,
    该换到文件应用(随包发的 Monaco)。但它的源码不在这个仓里,URL 上只认 domain / root,
    也没有应用级的消息通道——唯一的入口是它启动时按 `cd_files_app_<domain>` 恢复上次的标签
    (tabs 逐个打开,last 最后打开并激活)。它与我们同源,那份 localStorage 就在手边,
    所以交接的做法是:先把「该开哪个文件」写进它的恢复态,再让它启动,不改那份打包产物。

    行号带不过去:它只持久化标签,不持久化光标位置。
    """
    FILES = open(os.path.join(ROOT, "frontend", "app-files.js"), encoding="utf-8").read()

    def test_按它的恢复态交接_不改打包产物(self):
        seg = fn(self.FILES, "openFileInIde")
        self.assertIn('"cd_files_app_" + id', seg)
        self.assertIn("st.last = path", seg)
        self.assertIn("st.tabs", seg)
        self.assertIn("st.expanded", seg)          # 树要展开到那一层,不然人还得自己点开一路

    def test_同一个root要重走一遍启动(self):
        # iframe 已经启动过,src 不变就不会重读恢复态,那样点了没反应
        seg = fn(self.FILES, "openFileInIde")
        self.assertIn("filesCurrent && filesCurrent.id === id", seg)
        self.assertIn("location.replace(filesPageSrc(id))", seg)

    def test_文件页出交接钮_找不到路径时不出(self):
        seg = fn(CHAT, "hsOpenFile")
        self.assertIn("hs-fp-ide", seg)
        self.assertIn("openFileInIde(loc.root, loc.rel)", seg)
        # 这一段排在 `if (!loc)` 的提前返回之后
        self.assertLess(seg.index('if (!loc) {'), seg.index("openFileInIde(loc.root"))


class ResizeUsesNativeLayout(unittest.TestCase):
    """尺寸变化由统一观察器在同一绘制周期收口，横向内容轨仍由 CSS 自然重排。"""

    def test_不再有延迟几何复量(self):
        seg = fn(CHAT, "hsBuild")
        self.assertNotIn("alignSettled", seg)
        self.assertNotIn("visualViewport.addEventListener", seg)
        self.assertNotIn("matchMedia(`(resolution:", seg)
        self.assertNotIn("hsAlign", CHAT)

    def test_窗口变化只维持原有贴底语义(self):
        seg = fn(CHAT, "hsBuild")
        self.assertIn("hsObserveLayout(v)", seg)
        observe = fn(CHAT, "hsObserveLayout")
        self.assertIn("new ResizeObserver", observe)
        self.assertIn("[v.stream, composer, v.body, v.el]", observe)

    def test_热页面露面无需补写行内位置(self):
        seg = fn(CHAT, "hsShown")
        self.assertNotIn("alignLater", seg)
        self.assertNotIn("alignDirty", seg)
