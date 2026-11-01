"""开机不再一个等一个,而且首屏不等网络(2026-08-24 用户实报手机首启与刷新都非常慢)。

经隧道时每个请求固定一秒多,与响应大小无关。原来 init 里七个 await 排成一串,串起来
就是七八秒,而它们本可以一起走——只有「把本地应用并进 domains」这一步有先后,取数没有。

再往前一步:上回的结果落盘,下次开机先用它把侧栏画出来,真数据回来再对账。首屏因此不等
网络。手法照 lark-events 那处「落盘垫底:下次 app 冷启秒开」。
"""
import json
import os
import re
import shutil
import subprocess
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
INIT = open(os.path.join(ROOT, "frontend", "app-init.js"), encoding="utf-8").read()
CORE = open(os.path.join(ROOT, "frontend", "app-core.js"), encoding="utf-8").read()
EVENTS = open(os.path.join(ROOT, "frontend", "app-events.js"), encoding="utf-8").read()
PANEL = _srcmap.rail_src()


def init_body():
    at = INIT.index("async function init() {")
    return INIT[at:INIT.index("\ninit()", at)]


class InteractiveMilestone(unittest.TestCase):
    """首屏完成以当前页正文和输入框都可用为准，不拿导航完成冒充。"""

    def test_历史会话等待正文成功或明确失败(self):
        self.assertIn("!v.historyReady && !v.loadFailEl", INIT)
        self.assertIn('deskStartupMark("interactive"', INIT)

    def test_可交互前等待浏览器完成两帧布局(self):
        self.assertIn("requestAnimationFrame(() => requestAnimationFrame(resolve))", INIT)
        self.assertIn("v.inp.isContentEditable", INIT)


class OneWaveNotSeven(unittest.TestCase):
    """开机那几样一起发。"""

    def test_取数收在一波里(self):
        body = init_body()
        self.assertIn("Promise.all([", body, "还是一个等一个")

    def test_七样都在那一波里(self):
        body = init_body()
        seg = body[body.index("Promise.all(["):]
        seg = seg[:seg.index("]).then")]
        for need in ("loadDeskRole()", "initEnginePick()", "loadHidden()",
                     "/workbench.json", "/workbench/domains", "/workbench/apps",
                     "/workbench/state"):
            self.assertIn(need, seg, need + " 没进那一波")

    def test_串行的那几个await没了(self):
        """留一个在外面就白省了——那一个照样要单独等一趟。"""
        body = init_body()
        head = body[:body.index("renderRail()")]
        for gone in ("await loadDeskRole()", "await initEnginePick()", "await loadHidden()"):
            self.assertNotIn(gone, head, "还留着串行的 " + gone)

    def test_合并那一步仍在取数之后(self):
        """本地应用要并进 domains,domains 得先拼好。这一步有先后,不能一起做。"""
        body = init_body()
        self.assertLess(body.index("domains = mergeDomains("),
                        body.index("mergeLocalApps("), "先合并后拼 domains,顺序反了")

    def test_合并不再自己取一趟(self):
        """那份 /workbench/apps 已经在上面那一波里拿回来了。"""
        body = init_body()
        self.assertIn("mergeLocalApps(localApps)", body, "没有把取回的那份传进去")
        seg = re.search(r"async function mergeLocalApps\(pre\) \{.*?\n\}", CORE, re.S)
        self.assertIsNotNone(seg, "mergeLocalApps 没有收 pre")
        self.assertIn("if (pre && typeof pre === \"object\") local = pre;", seg.group(0))
        self.assertIn("else try { local = await api(", seg.group(0), "不传时要能自己取")


class PaintBeforeNetwork(unittest.TestCase):
    """先用落盘的画,再对账。"""

    def test_开机先读缓存(self):
        body = init_body()
        self.assertIn('localStorage.getItem(_BOOT_CACHE)', body)
        self.assertLess(body.index("_BOOT_CACHE"), body.index("Promise.all(["),
                        "读缓存排在取数之后,那就还是要等网络")

    def test_垫底之后先画一次(self):
        body = init_body()
        seg = body[body.index("_BOOT_CACHE"):body.index("Promise.all([")]
        self.assertIn("renderRail()", seg, "读了缓存却没画")

    def test_真数据回来再画一次(self):
        """对账:不一致的地方静默换掉。"""
        body = init_body()
        after = body[body.index("Promise.all(["):]
        self.assertIn("renderRail()", after, "拿到真数据没有重画")

    def test_拿到真数据要落盘(self):
        body = init_body()
        self.assertIn("localStorage.setItem(_BOOT_CACHE", body, "没有为下次开机留一份")

    def test_缓存里不放票据与正文(self):
        """落盘的只该是画侧栏所需的那几样。"""
        body = init_body()
        seg = body[body.index("localStorage.setItem(_BOOT_CACHE"):]
        seg = seg[:seg.index("}")]
        for bad in ("token", "message", "transcript"):
            self.assertNotIn(bad, seg, "缓存里放了不该放的 " + bad)

    def test_缓存坏了不挡开机(self):
        """localStorage 里那份可能是上一个版本写的,形状对不上也得能起来。"""
        body = init_body()
        seg = body[body.index("_BOOT_CACHE"):body.index("Promise.all([")]
        self.assertIn("catch", seg, "垫底那段没有兜住异常")

    def test_空缓存不画(self):
        """第一次开机没有缓存,不能画出一个空侧栏再闪一下。"""
        body = init_body()
        seg = body[body.index("_BOOT_CACHE"):body.index("Promise.all([")]
        self.assertIn("c.domains.length", seg, "没判空就画")


class MergedEndpointFirst(unittest.TestCase):
    """七样一趟拿全;拿不到退回并行那一波。"""

    def test_先用首页内联的那份(self):
        """首页本来就每次都取,顺路捎上的那份不必再花一个往返。"""
        body = init_body()
        self.assertIn("window.__BOOT__", body)
        self.assertLess(body.index("window.__BOOT__"), body.index('api("/workbench/boot")'),
                        "先去请求了才看内联的那份,那就白内联了")

    def test_没有内联就去请求(self):
        """老后端不内联。新前端不该挑后端版本。"""
        body = init_body()
        seg = body[body.index("window.__BOOT__"):body.index("} else {")]
        self.assertIn("if (!boot)", seg)
        self.assertIn('api("/workbench/boot")', seg)

    def test_先试合并端点(self):
        body = init_body()
        self.assertIn('api("/workbench/boot")', body)
        self.assertLess(body.index('api("/workbench/boot")'), body.index("Promise.all(["))

    def test_缺哪项补哪项(self):
        """一样缺就整份不用的话,已经拿到的那六样白丢了。2026-08-25 实见:workbench.json
        是静态文件、不归任何 /workbench/* 路由,扇出取不到,整份被判作不可用。"""
        body = init_body()
        seg = body[body.index('api("/workbench/boot")'):body.index("} else {")]
        self.assertIn("const need = async (k) =>", seg, "没有按项回落")
        self.assertIn("boot[k] != null ? boot[k] : api(k)", seg)
        self.assertNotIn('boot["/workbench.json"]) {', seg, "还在拿单独一项判整份可不可用")

    def test_拿不到要能退回去(self):
        """老后端没有这条路。新前端不该挑后端版本。"""
        body = init_body()
        self.assertIn("} else {", body[body.index('api("/workbench/boot")'):])
        seg = body[body.index('api("/workbench/boot")'):]
        self.assertIn("Promise.all([", seg, "没有回落那一支")

    def test_取数与应用拆开了(self):
        """合并端点带回来的那份要能直接用,不必再打一趟。"""
        self.assertIn("function applyDeskRole(r)", CORE)
        self.assertIn("async function applyEnginePick(e)", EVENTS)
        seg = re.search(r"async function loadDeskRole\(\) \{.*?\n\}", CORE, re.S).group(0)
        self.assertIn("applyDeskRole(", seg, "取数那一半没有调应用那一半")
        seg2 = re.search(r"async function initEnginePick\(\) \{.*?\n\}", EVENTS, re.S).group(0)
        self.assertIn("applyEnginePick(e)", seg2)

    def test_会话列表先从本地缓存种上(self):
        """冷扫不进首页；上一轮列表先画，后台再对账。"""
        body = init_body()
        self.assertIn('localStorage.getItem("cd_terms_snapshot")', body)
        self.assertNotIn('await need("/workbench/terminals")', body)
        self.assertIn("function seedTerms(payload, opts = {})", PANEL, "没有种进去的入口")

    def test_种上之后不再重复取(self):
        """网络快照记新鲜度；磁盘垫底不是本轮读取成功，仍需核对。"""
        seg = re.search(r"function seedTerms\(payload, opts = \{\}\) \{.*?\n\}", PANEL, re.S)
        self.assertIsNotNone(seg, "seedTerms 不见了")
        self.assertIn("_applyTermsList(rows)", seg.group(0))
        self.assertIn("_termsAt = 0", seg.group(0))
        self.assertIn("_termsAt = Date.now()", seg.group(0), "没记时间,新鲜度判据会当它是陈的")

    def test_真列表变化后更新本地缓存(self):
        seg = re.search(r"function seedTerms\(payload, opts = \{\}\) \{.*?\n\}", PANEL, re.S).group(0)
        self.assertIn('localStorage.setItem("cd_terms_snapshot"', seg)
        self.assertLess(seg.index("if (!complete) return"), seg.index('localStorage.setItem("cd_terms_snapshot"'))

    def test_种的不是数组就不动(self):
        """开机那份里这一项可能是 null(取不到),不能拿它把列表冲成空。"""
        seg = re.search(r"function seedTerms\(payload, opts = \{\}\) \{.*?\n\}", PANEL, re.S).group(0)
        self.assertIn("Array.isArray(list)", seg)

    def test_合并那一支也把这两样应用上(self):
        body = init_body()
        seg = body[body.index("const need = async (k) =>"):body.index("} else {")]
        self.assertIn("applyDeskRole(", seg)
        self.assertIn("applyEnginePick(", seg)
        self.assertIn("hiddenDomains", seg, "隐藏的工作区没从合并结果里取")


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class MergeLocalAppsBehaviour(unittest.TestCase):
    def _run(self, body):
        src = re.search(r"async function mergeLocalApps\(pre\) \{.*?\n\}", CORE, re.S).group(0)
        js = """
let domains = [{id:"d1", apps:[{id:"a"}]}];
let desktopApps = [{id:"z"}];
let fetched = 0;
async function api(){ fetched++; return {d1:[{id:"b"}], _desktop:[{id:"y"}]}; }
%s
(async () => { %s })();
""" % (src, body)
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_传了就不再取(self):
        got = self._run("""
await mergeLocalApps({d1:[{id:"c"}]});
console.log(JSON.stringify({fetched, d1: domains[0].apps.map((a)=>a.id)}));
""")
        self.assertEqual(got["fetched"], 0, "传了那份还去打了一趟")
        self.assertIn("c", got["d1"])

    def test_不传照旧自己取(self):
        """别处的调用点一个字都没改,那一路必须照常工作。"""
        got = self._run("""
await mergeLocalApps();
console.log(JSON.stringify({fetched, d1: domains[0].apps.map((a)=>a.id)}));
""")
        self.assertEqual(got["fetched"], 1)
        self.assertIn("b", got["d1"])


class OpenSessionRoundtrips(unittest.TestCase):
    """开一次会话打几个往返。刷新会自动恢复上次那一页,所以这条路每次刷新都要走。

    2026-08-25 实测原来是四个请求:/workbench/engine、/config、/message、/session/x/state,
    而其中两对是串行的。经隧道时一个往返一两秒,串起来就是双份。
    """

    CHAT = _srcmap.chat_src()

    def _fn(self, name):
        m = re.search(r"\n(?:async )?function %s\(.*?\n}\n" % re.escape(name), self.CHAT, re.S)
        self.assertIsNotNone(m, name + " 不见了")
        return m.group(0)

    def test_状态那一趟与消息并行(self):
        """它不依赖消息,只是应用要等正文铺好。发可以提前,等留在原处。"""
        seg = self._fn("hsLoad")
        self.assertIn("const statePre = api(", seg, "没有提前把状态那一趟发出去")
        # 锚在真正的调用上,不是「/message」这个词——上面那段注释里也提到它,
        # 按词找会命中注释,断言就成了废话(刚踩过)。
        self.assertLess(seg.index("const statePre"), seg.index("}/message`)"),
                        "发得比消息还晚,那就没并行")
        self.assertIn("hsStateSync(v, statePre)", seg, "没有把飞行中的那个收回来")

    def test_状态那一趟失败不带走整段加载(self):
        """它只是状态同步,挂了不该让历史也灌不进来。"""
        seg = self._fn("hsLoad")
        i = seg.index("const statePre")
        self.assertIn(".catch(", seg[i:i + 200], "没有兜住,失败会把整段加载带走")

    def test_应用仍在正文之后(self):
        """hsTurnReady 认的是已经存在的回合,提前应用会落空。"""
        seg = self._fn("hsLoad")
        self.assertLess(seg.index("hsPourFast(v, msgs)"), seg.index("hsStateSync(v, statePre)"))

    def test_引擎那份不再重复取(self):
        """开机已经取过并存进 engineInfo,而开机数据现在还直接内联在首页里。"""
        seg = self._fn("hsEngineSync")
        self.assertIn("engineInfo", seg, "没有复用开机拿到的那份")
        i = seg.index("engineInfo")
        j = seg.index('api("/workbench/engine")')
        self.assertLess(i, j, "先去请求了才看已有的那份")
        self.assertIn("if (!st)", seg, "没有「拿不到才现取」这一支")

    def test_收下来的那份仍当引擎级档位用(self):
        """会话没存过档位时要回落到引擎级那一份,换了来源不能把这条丢了。

        取数在 hsEngineSync、绘制在 hsOptsPaint(2026-08-25 拆开),两头各看一半:
        前者要把这份存成 engineSnap,后者才拿得到——否则 SSE 那条路上重画时没有退路。
        """
        self.assertIn("v.engineSnap = st ||", self._fn("hsEngineSync"))
        self.assertIn("(st || {}).opts", self._fn("hsOptsPaint"))
