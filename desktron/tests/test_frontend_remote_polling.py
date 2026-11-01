"""远端那一页的常驻轮询(2026-08-25 实测手机经隧道时的持续消耗)。

页面起来之后并没有停,几条 setInterval 一直在打:33 秒里 /dev/token 去了 19 次、
/workbench/terminals 去了 9 次、tasks 在同一时刻发出去 11 个。本机看不出来——直连
后端 0~1ms;经隧道每一个都是跨区往返,复用连接时中位 242ms,新建连接时一半超过
600ms、最慢 1540 ms(同日实测,两端服务端各自打自己都是零尖峰,慢在 Mac↔服务器 那段
建连路径上)。于是「用着用着变卡」不是错觉:真正要发的请求得跟这些抢连接。

三处各有各的毛病,判据也各不相同:

  一、/dev/token 那条问的是「服务端是不是开发态」。可 dev 与否是服务端一侧的属性,
      而代价落在客户端:手机连进来看到的是同一份服务端,于是跟着一起轮询。那一侧既
      没人在改前端文件,重载还会把正在看的页面冲掉——判据该看客户端在哪。

  二、会话列表那条没有 document.hidden,标签页切到后台照样打。间隔 1.5 秒也是照本机
      定的,而经隧道一轮本身就要 0.5~1.3 秒,等于连着不停。

  三、tpTaskRows 是 for + await 一个等一个,五个工作流串起来最坏九秒多;任务页与设置页
      又各调一次,同一批请求发了两遍。
"""
import os
import re
import subprocess
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def read(name):
    return _srcmap.frontend_src(name)   # 2026-08-27 分拆:被拆的名给拼接整体


def code_only(src):
    """剥掉整行注释。检查「某种写法还在不在」时必须先剥:注释里往往正写着那个写法
    (本文件的注释就提到 keys.map(fetchTaskStats)),照全文查会锚到注释上,判据就废了。"""
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))


CORE = read("app-core.js")
INIT = read("app-init.js")
PANEL = read("app-rail.js") + "\n" + read("app-panel.js")
INDEX = read("index.html")


class LocalViewProbe(unittest.TestCase):
    """判断这一页是不是本机在看。"""

    def _fn(self):
        m = re.search(r"function isLocalView\(\) \{.*?\n\}", CORE, re.S)
        self.assertIsNotNone(m, "isLocalView 不见了")
        return m.group(0)

    def test_按客户端所在判断(self):
        self.assertIn("location.hostname", self._fn())

    def test_认全本机的几种写法(self):
        """IPv6 的写法漏一个,那一路进来的就被当成远端,自动重载在开发机上失灵。"""
        fn = self._fn()
        for h in ("127.0.0.1", "localhost", "::1"):
            self.assertIn(h, fn, h + " 没算作本机")

    def test_不拿服务端属性当判据(self):
        """dev / role / backend 都是服务端一侧的事,与谁在看这一页无关。"""
        fn = self._fn()
        for bad in ("dev", "deskRole", "backend"):
            self.assertNotIn(bad, fn, "判据混进了服务端属性 " + bad)

    def test_定义排在用它的脚本之前(self):
        """app-core.js 得先于 app-init.js / app-panel.js 加载,否则调用时它还不存在。

        位置按 script 标签算,不按文件名在正文里第一次出现算:index.html 的注释里也提到
        这几个文件名(第 39 行那段就写着 app-panel.js),照全文查找会锚到注释上。
        """
        at = {}
        for name in ("app-core.js", "app-init.js", "app-panel.js"):
            m = re.search(r'<script src="/%s' % re.escape(name), INDEX)
            self.assertIsNotNone(m, name + " 没在 index.html 里")
            at[name] = m.start()
        self.assertLess(at["app-core.js"], at["app-init.js"])
        self.assertLess(at["app-core.js"], at["app-panel.js"])


class DevReloadLocalOnly(unittest.TestCase):
    """开发态自动重载只在本机开。"""

    def _fn(self):
        m = re.search(r"\(async function devAutoReload\(\) \{.*?\n\}\)\(\);", INIT, re.S)
        self.assertIsNotNone(m, "devAutoReload 不见了")
        return m.group(0)

    def test_远端直接不轮询(self):
        self.assertIn("if (!isLocalView()) return;", self._fn())

    def test_这一关排在第一个请求之前(self):
        """放在探过 /dev/token 之后的话,远端仍白跑一趟,而且那一趟正卡在开机路上。"""
        fn = self._fn()
        self.assertLess(fn.index("isLocalView()"), fn.index('api("/dev/token")'),
                        "先去问了服务端才判断客户端在哪")

    def test_本机那条路没被改掉(self):
        """远端关掉是目的,顺手把开发机的热重载也关了就是另一回事了。"""
        fn = self._fn()
        self.assertIn("location.reload()", fn)
        self.assertIn("1500", fn, "本机的节律被动了")


class TermPollQuietWhenUnseen(unittest.TestCase):
    """会话列表轮询:看不见就不打,远端放慢。"""

    def _fn(self):
        m = re.search(r"function startTermPoll\(\) \{.*?\n\}", PANEL, re.S)
        self.assertIsNotNone(m, "startTermPoll 不见了")
        return m.group(0)

    def test_后台标签页不打(self):
        self.assertIn("document.hidden", self._fn())

    def test_这一关排在取数之前(self):
        """排在 refreshTerms 之后的话,请求已经发出去了,省下的只是重绘。"""
        fn = self._fn()
        self.assertLess(fn.index("document.hidden"), fn.index("refreshTerms()"),
                        "先取了数才判断页面在不在前台")

    def test_远端的节律放慢(self):
        fn = self._fn()
        self.assertIn("isLocalView() ? 1500 : 5000", fn)

    def test_本机的节律不变(self):
        """1.5 秒是照本机(直连 0~1ms)定的,那一侧没有理由跟着放慢。"""
        self.assertIn("1500", self._fn())


class TaskFetchDedup(unittest.TestCase):
    """挡重复的闸做在唯一入口上。

    2026-08-25 实测:先只给 tpTaskRows 加去重,经隧道一测 tasks 仍是 11 个——侧栏折叠计数
    (app-panel 的 refreshFoldCounts 那路)与容器子区聚合各自遍历全部工作流,走的都是
    _fetchTasks,绕不到 tpTaskRows 那道闸。闸要落在数据的入口,不是落在某一个调用者上。
    """

    TASKS = read("app-tasks.js")

    def _fn(self):
        m = re.search(r"function _fetchTasks\(key\) \{.*?\n\}", self.TASKS, re.S)
        self.assertIsNotNone(m, "_fetchTasks 不见了")
        return m.group(0)

    def test_同一个key在飞时共用(self):
        fn = self._fn()
        self.assertIn("if (_tasksFlight[key]) return _tasksFlight[key];", fn)

    def test_落地后撤掉记号(self):
        """不撤的话第二次就永远拿的是第一次那份,agent 改完 TASKS.md 页面再也不更新。"""
        fn = self._fn()
        self.assertIn("delete _tasksFlight[key]", fn)

    def test_失败也要撤掉自己的记号(self):
        """只在成功那一支撤会永久缓存拒绝；无条件删又可能误删失效后发起的新请求。"""
        fn = self._fn()
        self.assertIn("if (_tasksFlight[key] === p) delete _tasksFlight[key]", fn)

    def test_事件期间的旧响应不会覆盖新快照(self):
        start = self.TASKS.index("const _taskStats")
        end = self.TASKS.index("async function fetchTaskStats", start)
        source = self.TASKS[start:end]
        harness = r'''
const pending=[];
const merged={};
global.window={__BOOT__:null};
global.api=(url)=>{
  if(merged[url]) return merged[url];
  merged[url]=new Promise(resolve=>pending.push({url,resolve}));
  return merged[url];
};
(async()=>{
  const old=_fetchTasks('alpha');
  invalidateTasks('alpha');
  const fresh=_fetchTasks('alpha');
  if(pending.length!==2||pending[0].url===pending[1].url) throw new Error('refresh reused merged GET');
  pending[0].resolve({done:0,doing:1,total:1,items:[{title:'old'}]});
  await Promise.resolve();
  pending[1].resolve({done:0,doing:1,total:1,items:[{title:'new'}]});
  const [a,b]=await Promise.all([old,fresh]);
  if(a.items[0].title!=='new'||b.items[0].title!=='new'||_taskItems.alpha[0].title!=='new')
    throw new Error('stale task response won');
})().catch(e=>{console.error(e);process.exit(1)});
'''
        got = subprocess.run(["node", "-e", source + "\n" + harness],
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(got.returncode, 0, got.stderr)

    def test_不做时间缓存(self):
        """任务由 agent 直接改 TASKS.md,过期的数比多一次请求麻烦。"""
        fn = self._fn()
        self.assertNotIn("Date.now()", fn, "掺进了时间缓存")


class TaskRowsBundled(unittest.TestCase):
    """各工作流的任务清单:一次取全。

    这一条改了三版才对,前两版都被实测推翻,值得钉住:
      一、只给 tpTaskRows 加去重 → tasks 仍是 11 个。侧栏折叠计数与容器聚合走的是
          _fetchTasks,绕不到那道闸。
      二、改成 Promise.all 并行 → 多开 3 条连接,每个请求 1.06~1.84 秒。
      三、合并成一条 /workbench/tasks → 五次往返变一次。
    症结从来不是「并行还是串行」「去重与否」,是请求数本身:服务端一个只要 1~3 毫秒,
    而经隧道握手中位 384ms、四分之一的新连接还要多付整整一秒。
    """

    def _fn(self):
        m = re.search(r"async function tpTaskRows\(\) \{.*?\n\}", PANEL, re.S)
        self.assertIsNotNone(m, "tpTaskRows 不见了")
        return m.group(0)

    def test_走合并那条(self):
        fn = self._fn()
        self.assertIn("fetchTasksBundle(", fn)

    def test_不再一个工作流一个请求(self):
        fn = self._fn()
        self.assertNotIn("_fetchTasks(d.id)", fn, "还在逐个取")
        self.assertNotIn("api(`/workbench/domains/", fn, "还在自己另打一份")

    def test_单个工作流读失败不连累整页(self):
        """合并之后单个失败表现为那一项是 null,不再是抛异常。"""
        fn = self._fn()
        self.assertIn("if (!data) continue;", fn)


class TaskBundleFrontend(unittest.TestCase):
    """一次取多个的那条前端入口。"""

    TASKS = read("app-tasks.js")

    def _fn(self):
        m = re.search(r"async function fetchTasksBundle\(keys\) \{.*?\n\}", self.TASKS, re.S)
        self.assertIsNotNone(m, "fetchTasksBundle 不见了")
        return m.group(0)

    def test_一次一个请求(self):
        fn = self._fn()
        self.assertIn("/workbench/tasks?ids=", fn)
        self.assertIn("encodeURIComponent", fn, "id 没转义,带斜杠或中文的会拼坏 URL")

    def test_去重后再发(self):
        """同一个 key 传两遍就白取一遍。"""
        self.assertIn("[...new Set(keys)]", self._fn())

    def test_填的是同一份缓存(self):
        """另存一份的话,单个取那条路与这条各看各的,页面上就会出现两个不一样的数。"""
        fn = self._fn()
        self.assertIn("_takeTasks(k, one)", fn)

    def test_老后端没有这条路时退回逐个取(self):
        """新前端不该挑后端版本:装了新前端旧后端的那一台,任务页不能整个空掉。"""
        fn = self._fn()
        self.assertIn("catch", fn)
        self.assertIn("_fetchTasks(k)", fn, "没有退回单个取那条路")

    def test_撞过一次就记住(self):
        """三处调用各撞一次 404 的话,老后端下反而比改之前多三个请求——2026-08-25 实见:
        前端已换、后端还没重启的那阵,bundle 3 个全 404,逐个 11 个照旧。"""
        fn = self._fn()
        self.assertIn("if (_bundleOff) return", fn, "没有记住,每次都要再撞一遍")
        self.assertIn("_bundleOff = true", fn)
        self.assertIn("let _bundleOff = false", self.TASKS)

    def test_只有那条路不通才记(self):
        """记号一旦立起来就不再试合并端点了。只该由那一个请求本身失败来立,
        不能因为某个工作流的数据有问题就把整条路判死。"""
        fn = self._fn()
        at = fn.index("_bundleOff = true")
        head = fn[:at]
        self.assertIn("api(`/workbench/tasks?ids=", head, "记号不是那条请求失败时立的")

    def test_扇出的那两处都改了(self):
        """漏一处就还是一个 key 一个请求,合并端点白加。"""
        for src, where in ((PANEL, "app-panel-sidebar.js"), (self.TASKS, "app-tasks.js")):
            self.assertNotIn("map(fetchTaskStats)", code_only(src),
                             where + " 里还留着按 key 扇出的写法")


if __name__ == "__main__":
    unittest.main()
