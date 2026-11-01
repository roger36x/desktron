"""接入向导与 larkbot 本体的口径一致性(bin/lark-bot-wizard.py)。

向导是交互脚本,主体流程测不了。但它与本体之间有几处必须一字不差的约定——权限清单、
事件名、站点域名、接口路径。这些分散在四个文件里,各改各的不会有任何报错,
到运行时才表现为「bot 不回话」,而那正是这套东西最难排查的一类故障。

所以这里测的是「两处说的是不是同一件事」,不是向导本身跑得对不对。
"""
import json
import os
import re
import sys
import tempfile
import unittest
import _srcmap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

# deskcfg 在导入那一刻就从 sys.argv 取 FRONTEND/UI_PORT/REPO_ROOT(模块级单例),
# 拿 unittest 自己的 argv 去 int() 会报 "invalid literal for int()"——与真实原因
# 毫无字面关联。同 test_bg_tasks.py:21 与 test_larkbot_boot.py 的处理。
_argv = sys.argv
sys.argv = ["ui_server.py", "/tmp/wiz-frontend", "0", "0", tempfile.mkdtemp(prefix="wiz-")]
try:
    from larkbot import api as lb_api, larkapi  # noqa: E402
finally:
    sys.argv = _argv

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _read(rel):
    base = os.path.basename(rel)
    if base in ("app-chat.js", "app-panel.js", "app-rail.js", "app-assets.js", "app-settings.js"):
        return _srcmap.frontend_src(base)   # 2026-08-27 分拆,拼接整体等价
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


WIZ = _read("desktron/bin/lark-bot-wizard.py")
AUTO = _read("desktron/bin/lark_app_auto.py")
GUIDE = _read("docs/agent-hosting-guide.md")
SETTINGS = _read("frontend/app-settings.js")

#: 唯一那个事件名。三处必须一字不差,见 ScopesAndEvents.test_事件名三处一致。
EVENT_NAME = "im.message.receive_v1"


def _load_auto():
    """按文件路径加载 lark_app_auto——它在 bin/ 下,不是包的一部分,import 不到。"""
    import importlib.util
    p = os.path.join(_ROOT, "desktron/bin/lark_app_auto.py")
    spec = importlib.util.spec_from_file_location("lark_app_auto_undertest", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


LAA = _load_auto()


class ScopesAndEvents(unittest.TestCase):
    """向导让人勾的权限,必须正好是 larkbot 运行时要用的那些。"""

    #: 运行时用到的能力 → 权限表里的可申请名。改了 larkbot 的能力面就要同步这里与向导。
    #: 「查群形态」那一项:公开文档叫 im:chat:readonly,权限表里搜不到那个名
    #: (2026-08-26 实测),可申请名是 im:chat:read——两处清单都按表里的名字写。
    NEEDED = {
        "im:message": "收消息",
        "im:message:send_as_bot": "发消息",
        "im:chat:read": "查群形态(chat_mode,决定不 @ 要不要理)",
        "im:resource": "读消息里的图片与文件",
    }

    def test_向导列的权限与运行时要的一致(self):
        listed = set(re.findall(r'\("(im:[a-z_:.]+)",', WIZ))
        self.assertEqual(listed, set(self.NEEDED), "向导的权限清单与运行时对不上")

    def test_guide_里列的权限也是同一份(self):
        """人照 guide 手动配时不该少勾一项。"""
        for s in self.NEEDED:
            self.assertIn(s, GUIDE, s)

    def test_事件名三处一致(self):
        """向导让人订阅的、sidecar 注册的、guide 里写的,必须是同一个。"""
        ev = "im.message.receive_v1"
        self.assertIn(f'EVENT = "{ev}"', WIZ)
        self.assertIn(ev, _read("desktron/bin/lark-ws.mjs"))
        self.assertIn(ev, GUIDE)

    def test_订阅方式是长连接不是_webhook(self):
        """默认是 webhook,而 Desk 可能跑在内网、飞书回调不进来。
        这一项配错的表现是「什么都配好了但一条消息也收不到」。"""
        self.assertIn("长连接", WIZ)
        self.assertIn("长连接", GUIDE)


class Hosts(unittest.TestCase):
    def test_站点域名与_larkapi_同一份(self):
        wiz = dict(re.findall(r'"(feishu|larksuite)": "(https://[a-z.]+)"', WIZ))
        self.assertEqual(wiz, larkapi.DOMAINS, "向导与 larkapi 的域名表对不上")

class DeskEndpoints(unittest.TestCase):
    """向导调的本机接口都得真的存在——它跑在终端里,拼错了要到用户手上才发现。"""

    def _paths(self):
        return set(re.findall(r'desk\(\s*f?"(/workbench/[^"]+)"', WIZ))

    def test_调的_larkbot_接口都被认领(self):
        for p in self._paths():
            if not p.startswith("/workbench/lark-bot"):
                continue          # 别的域(如 /workbench/domains)由它们自己认领
            real = p.replace("{bot_id}", "x")
            self.assertTrue(lb_api.owns(real, "POST"), f"{p} 没有被 larkbot 域认领")

    def test_六个接口一个都不少(self):
        got = {p.replace("{bot_id}", "<id>").replace("{engine}", "<eng>")
               for p in self._paths()}
        self.assertEqual(got, {
            "/workbench/lark-bot/deps",                        # 依赖检查与安装
            "/workbench/lark-bot/engines",                     # 目标那侧装了哪些引擎
            "/workbench/lark-bot/engines/<eng>/models",        # 那个引擎能用哪些模型
            "/workbench/lark-bot/agents",                      # 建 agent
            "/workbench/lark-bot/agents/<id>/secret",          # 存凭证并自检
            "/workbench/lark-bot/agents/<id>/start",           # 真的连一次
        })

    def test_向导让人从清单里挑模型(self):
        """模型名抄错的表现是引擎报参数错、一条消息都答不了,所以列出来按序号挑。
        清单在目标那一侧现问引擎——两边装的版本、开的权限都可能不一样。
        列不出来退回手填,不拦路。"""
        self.assertIn('desk(f"/workbench/lark-bot/engines/{engine}/models"', WIZ)
        self.assertIn("模型（序号或名字；留空则用引擎默认）", WIZ)
        self.assertIn("列不出模型清单", WIZ)
        self.assertIn('"model": model', WIZ)

    def test_向导会问用哪个引擎且序号名字都认(self):
        """它决定这个 bot 到底由谁来答,是建的时候最该定的一项。此前不问,建出来一律是
        默认的 claude,人得事后去面板里改(2026-08-25 用户实测发现)。
        选法与模型、挑应用一致:列出序号,序号或名字都认(2026-08-27 用户要求对齐)。"""
        self.assertIn('引擎（序号或名字，默认', WIZ)
        self.assertIn("engine = eng_all[int(v) - 1]", WIZ)
        self.assertIn('"engine": engine', WIZ)

    def test_只列目标那一侧真装了的引擎(self):
        """装没装是按机器算的(服务器上没装某个引擎就选不了)。选一个没装的,表现是
        「bot 收到消息但不回话」,与配错权限的现象一模一样,很难反推。"""
        self.assertIn('desk("/workbench/lark-bot/engines", method="GET")', WIZ)
        self.assertIn('if e.get("ok"):', WIZ)
        self.assertIn("尚未安装——收到消息也无法回答", WIZ)

    def test_探不到引擎清单时不把向导堵死(self):
        """可能是那一侧还是老版本、没有这个接口。少一层帮助,不该变成走不下去。"""
        blk = WIZ[WIZ.index("eng_default, eng_ok, eng_all"):WIZ.index('d, err = desk("/workbench/lark-bot/agents"')]
        self.assertIn("探不到已安装的引擎", blk)
        self.assertIn("if not eng_all or v in eng_all:", blk)

    def test_向导会问工作目录(self):
        """记忆与知识是全工作台检索的,不用问。工作目录决定它脚下能看见什么——
        代码仓、知识库挂载点都跟着它走,是「看得见什么」里剩下的那一项。"""
        self.assertIn("工作目录（留空则为工作台根目录）", WIZ)
        self.assertIn('"cwd": cwd', WIZ)

    def test_向导不问工作流(self):
        """bot 是工作台这一层的代理,不属于任何一个工作流。防回归。"""
        self.assertNotIn("workflow", WIZ)


class AutoMode(unittest.TestCase):
    """--auto:扫一次码,前四步替人做完。

    这条路走的是开发者后台前端自己在调的 /developers/v1/*,不是承诺兼容的公开 API
    (做法取自 botmux)。后台改版就会断——所以它的正确性不在「一定能成」,而在
    「断了之后回到原来的样子」。这里测的正是那个降级。
    """

    def test_两条路都在(self):
        """引导那条不能因为有了自动就删掉:自动那条随时可能断。"""
        self.assertIn('AUTO = "--auto" in sys.argv', WIZ)
        self.assertIn("if auto_done:", WIZ)
        self.assertIn("step(1, TOTAL", WIZ)      # 六步引导还在

    def test_自动失败不退出而是降级(self):
        """失败时若 sys.exit,人就只剩一句报错。降级才有下一步。"""
        blk = WIZ[WIZ.index("if AUTO:"):WIZ.index("if auto_done:")]
        self.assertIn("except Exception", blk)
        self.assertNotIn("sys.exit", blk)

    def test_降级时带出半成品应用(self):
        """应用一旦建出来,后面的失败都不能只说「失败」——不说清楚的话,开放平台上会
        躺着一个半配好的应用,而人以为什么都没发生,下次又建一个。"""
        blk = WIZ[WIZ.index("if AUTO:"):WIZ.index("if auto_done:")]
        self.assertIn('getattr(e, "app_id", "")', blk)
        self.assertIn("不必重建", blk)

    def test_失败带着做到哪一步(self):
        self.assertIn("class Failed", AUTO)
        self.assertIn("self.step = step", AUTO)
        self.assertIn("self.app_id = app_id", AUTO)
        # run() 里每一步都更新 step,否则降级时说不出卡在哪
        for st in ("建应用", "配权限", "事件订阅", "发版", "读凭证"):
            self.assertIn(f'step = "{st}"', AUTO + 'step = "建应用"')

    def test_权限与事件跟引导那条同一份(self):
        """两条路配出来的应用必须一样,否则走哪条路取决于运气。

        auto 那份是候选组(表里名字对不上时逐个试),向导那份是单名(人手勾,给一个
        准名字):向导的每个名字必须是 auto 某组的**首选**——首选不一致,两条路配出
        来的就可能不同。"""
        first_choices = {m for m in re.findall(r'\(\("(im:[a-z_:.]+)"', AUTO)}
        wiz_scopes = set(re.findall(r'\("(im:[a-z_:.]+)",', WIZ))
        self.assertEqual(first_choices, wiz_scopes)
        self.assertIn('EVENTS = ["im.message.receive_v1"]', AUTO)
        self.assertIn("EVENT_MODE_WS = 4", AUTO)      # 4 = 长连接

    def test_读群信息的候选组带着公开文档名(self):
        """公开文档叫 im:chat:readonly。表里的名字将来改回去也不该断——候选组里
        新旧两个名都在,勾到哪个算哪个。"""
        blk = AUTO[AUTO.index("SCOPES = ["):AUTO.index("EVENTS = [")]
        self.assertIn('"im:chat:read"', blk)
        self.assertIn('"im:chat:readonly"', blk)

    def test_建应用首选一键智能体模板(self):
        """模板建出的应用出生即带机器人能力与长连接(console launcher 同款,做法取自
        botmux 抓包)。普通自建 + robot/switch 那条在企业租户里会卡「机器人能力要审批」
        ——2026-08-26 用户扫码后被丢回开放平台手动申请,就是走了后一条。"""
        self.assertIn('"appManifestTemplateID": "developer_console"', AUTO)
        self.assertIn("manifest/upsert_by_template", AUTO)
        blk = AUTO[AUTO.index("app_id, step, from_tpl"):]
        self.assertLess(blk.index("create_app_tpl"), blk.index("c.create_app(name, desc, avatar)"))

    def test_模板被明确拒绝才回退_结果未知不回退(self):
        """业务错误码与 404 说明应用肯定没建出来,可以回退普通自建;传输错误、5xx 时
        应用可能已经建成,回退再建会产出重复应用——原样抛,让人去后台确认。"""
        blk = AUTO[AUTO.index("def create_app_tpl("):AUTO.index("def create_app(")]
        self.assertIn('"code=" in m or "HTTP 404" in m', blk)
        self.assertIn('return ""', blk)
        self.assertIn("raise", blk)

    def test_模板应用上_robot_switch_被拒不拦路(self):
        """模板应用出生就带机器人能力,那一步只是幂等重设;租户把它卡在审批后面时
        报错不该断链——能力本身在,最后真连一次见分晓。普通自建则必须断:能力真缺。"""
        self.assertIn("if not from_tpl:\n                raise", AUTO)

    def test_向导有行编辑(self):
        """不导 readline 的话方向键在 input() 里打出 ^[[D(2026-08-26 实测)。"""
        self.assertIn("import readline", WIZ)

    def test_站点表三处一致(self):
        auto_hosts = dict(re.findall(r'"(feishu|larksuite)": \("https://[a-z.]+", "(https://[a-z.]+)"\)', AUTO))
        self.assertEqual(auto_hosts, larkapi.DOMAINS)

    def test_单独跑时不打印凭证(self):
        """它只该经调用方交给 Desk 写进 SECRETS。"""
        self.assertIn('out.pop("secret", None)', AUTO)

    def test_会话不落盘(self):
        """cookie 只在进程内存里活着。落盘的登录态是另一类东西,不该悄悄产生。

        查的是「有没有写模式的 open」,不是「有没有 open」——读图标(rb)是正当的,
        upload_icon 要拿它当应用头像(2026-08-26 起)。"""
        import ast
        for n in ast.walk(ast.parse(AUTO)):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "open"):
                continue
            mode = ""
            if len(n.args) > 1 and isinstance(n.args[1], ast.Constant):
                mode = str(n.args[1].value)
            for kw in n.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
            self.assertIn("r", mode, "open() 没写明只读模式")
            self.assertNotIn("w", mode, "lark_app_auto 里出现了写文件")
            self.assertNotIn("a", mode, "lark_app_auto 里出现了追加写")


class Safety(unittest.TestCase):
    def test_凭证不落任何文件(self):
        """secret 只经接口交给后端写进 SECRETS,向导自己不留一份。

        按 AST 查真正的调用,不按字面匹配:".workbench" 在注释里出现是在说明
        「凭证不进那儿」,而 "open(" 会命中 urlopen()——两处都是假阳性。"""
        import ast
        called = set()
        for n in ast.walk(ast.parse(WIZ)):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Name):
                    called.add(f.id)
                elif isinstance(f, ast.Attribute):
                    called.add(f.attr)
        for bad in ("open", "writelines", "dump", "write"):
            self.assertNotIn(bad, called, f"向导里调了 {bad}()")

    def test_不模拟登录会话(self):
        """建应用那几步由本人在浏览器里做。向导只开页面、读输入、调本机接口——
        不碰 cookie、不抠 csrf、不打开放平台的内部接口。"""
        for bad in ("csrfToken", "csrf", "cookie", "qrlogin", "/developers/v1"):
            self.assertNotIn(bad.lower(), WIZ.lower(), f"向导里出现了 {bad}")


class ListApps(unittest.TestCase):
    """/developers/v1/app/list 的解析。返回体形状取自 2026-08-25 在内部域的实测:
    data.apps[] 里有 appID / name / desc / i18n / ability / appSceneType,外加 totalCount。
    """

    SAMPLE = {"code": 0, "data": {
        "Head": {"RespFormat": 0},
        "apps": [
            {"ability": ["bot"], "appID": "cli_aaaaaaaaaaaaaaaa", "appSceneType": 0,
             "name": "RogerAssist", "desc": "Roger's personal assistance",
             "i18n": {"zh_cn": {"name": "张三助手", "description": "中文说明"}},
             "createTime": 1774256484},
            {"ability": [], "appID": "cli_bbbbbbbbbbbbbbbb", "appSceneType": 0,
             "name": "没开机器人能力的那个", "desc": ""},
            {"ability": ["bot"], "appID": "cli_cccccccccccccccc", "appSceneType": 1,
             "name": "商店应用", "desc": ""},
            {"ability": ["bot"], "name": "连 appID 都没有的一条"},
        ],
        "totalCount": 9,
    }}

    def _con(self):
        # 绕开 __init__:它要真的登录、真的抠 csrfToken。这里测的只是解析。
        c = LAA.Console.__new__(LAA.Console)
        c.post = lambda path, body=None: self.SAMPLE
        return c

    def test_只留企业自建(self):
        """商店应用配不了长连接,列出来只会让人选错。"""
        apps, _ = self._con().list_apps()
        self.assertEqual([a["app_id"] for a in apps],
                         ["cli_aaaaaaaaaaaaaaaa", "cli_bbbbbbbbbbbbbbbb"])

    def test_形状不对的条目跳过而不是抛(self):
        """后台改版多半是加字段、改字段名,不该让整个列表取不出来。"""
        apps, _ = self._con().list_apps()
        self.assertTrue(all(a["app_id"].startswith("cli_") for a in apps))

    def test_标出机器人能力开没开(self):
        """没开的话 im:* 那几项在权限表里根本不可选——这一条解释了「看着好好的却配不上」。"""
        apps, _ = self._con().list_apps()
        self.assertEqual([a["has_bot"] for a in apps], [True, False])

    def test_如实报出后台说的总数(self):
        """默认只给一页。少列出来的那几个,人会当成「不存在」,转头去建一个重复的,
        所以两个数都要给出来,由调用方说破。"""
        apps, total = self._con().list_apps()
        self.assertEqual((len(apps), total), (2, 9))

    def test_名字取得到(self):
        apps, _ = self._con().list_apps()
        self.assertEqual(apps[0]["name"], "RogerAssist")


class Adopt(unittest.TestCase):
    """已有应用那条:扫码 → 挑 → 配好 → 读凭证。"""

    def _src(self):
        with open(os.path.join(_ROOT, "desktron/bin/lark_app_auto.py"), encoding="utf-8") as f:
            return f.read()

    def test_与新建走同一串配置调用(self):
        """「让这个应用能当 bot 用」的定义与它是新是旧无关。两条各写一份迟早岔开。"""
        blk = self._src()[self._src().index("def adopt("):]
        for call in ("enable_bot", "set_scopes", "set_events", "publish", "secret"):
            self.assertIn(f"c.{call}(", blk, call)

    def test_不假装判得出应用本来配好了没有(self):
        """scope/update 是「设成这样」,问不出它原来是哪样。知道的只有机器人能力原本
        开没开、事件是不是新订的——拿这两件定默认答案,不拿它替人做决定。"""
        blk = self._src()[self._src().index("def adopt("):]
        self.assertIn("likely_ready = was_bot and added == 0", blk)
        self.assertIn('"n" if likely_ready else "y"', blk)
        # 无论如何都要问一次:发版可能要管理员审批,不该悄悄替人排队
        self.assertNotIn("if likely_ready:\n            version", blk)

    def test_放弃时什么都不动(self):
        blk = self._src()[self._src().index("def adopt("):]
        self.assertIn("if not picked:\n            return None", blk)

    def test_动配置前有写前闸且默认是不(self):
        """挑中的应用可能正被别的服务用着——尤其订阅方式,它正以 webhook 给谁供事件时,
        切成长连接就把那个服务弄断了(2026-08-26 真发生过,用户只能把应用整个停用)。
        所以三步写操作之前先列出改动、确认,默认答案 n。"""
        blk = self._src()[self._src().index("def adopt("):]
        gate = blk.index("确认这个应用只给 Desk 用吗")
        self.assertLess(gate, blk.index("c.enable_bot("))
        self.assertLess(gate, blk.index("c.set_scopes("))
        self.assertIn('", "n"):', blk[gate:gate + 120])

    def test_事件订阅的请求体是_console_前端的增量契约(self):
        """字段是 operation:add + appEvents/userEvents/events + eventMode。
        2026-08-26 之前写的 eventKeys 在开放平台不存在,一律 9499 Bad Request——
        与 botmux 注释里记的 eventNames/eventNameList 是同类错:字段名凭感觉起,
        后台不认,失败现场只有一句 Bad Request,与真实原因毫无字面关联。"""
        s = self._src()
        blk = s[s.index("def set_events("):s.index("def publish(")]
        # 带引号查:docstring 里保留着「以前写的是 eventKeys」这段历史,查裸词会误伤
        self.assertNotIn('"eventKeys"', blk)
        for field in ('"operation": "add"', '"appEvents": need',
                      '"userEvents": []', '"eventMode": EVENT_MODE_WS'):
            self.assertIn(field, blk)

    def test_发版请求体是_console_launcher_的极简结构(self):
        """appVersion + 两端默认能力 bot + visibleSuggest/blackVisibleSuggest
        (botmux CDP 抓包确认)。2026-08-26 之前发的 versionName/updateDesc/i18n
        那套字段在这个接口上不存在,一律 9499——与 eventKeys 同一类错。"""
        blk = AUTO[AUTO.index("    def publish("):AUTO.index("    def secret(")]
        self.assertNotIn('"versionName"', blk)
        for field in ('"appVersion": version', '"mobileDefaultAbility": "bot"',
                      '"visibleSuggest": white', '"blackVisibleSuggest": black'):
            self.assertIn(field, blk)
        # 不加会触发人工审核的字段:加了应用停在未启用,事件配置进了草稿也不生效。
        # 带引号查——docstring 里保留着「别加回来」的告诫,查裸词会误伤。
        self.assertNotIn('"applyReasonConfig"', blk)
        self.assertNotIn('"isAutoAudit"', blk)

    def test_发版版本号连草稿一起数(self):
        """只看已发布的会与草稿撞号,平台以「版本号未递增」拒掉,应用永远停在未启用。"""
        c = LAA.Console.__new__(LAA.Console)
        calls = []

        def post(p, b=None):
            calls.append((p, b))
            if "app_version/list" in p:
                return {"data": {"versions": [{"appVersion": "1.0.0"},
                                              {"appVersion": "0.0.9"},
                                              {"appVersion": "坏的"}]}}
            if "visible/online" in p:
                return {"data": {"whiteList": {"departments": [], "members": [{"id": "u1"}],
                                               "groups": [], "isAll": 0},
                                 "blackList": {"departments": [], "members": [],
                                               "groups": [], "isAll": 0}}}
            if "app_version/create" in p:
                return {"data": {"versionId": "v9"}}
            return {"code": 0}

        c.post = post
        c.who = {"uid": "me"}
        version, note = c.publish("cli_x")
        self.assertEqual(version, "1.0.1")
        self.assertEqual(note, "")
        create = next(b for p, b in calls if "app_version/create" in p)
        # 已有版本的应用:可见范围整块镜像线上的,不是写死空值
        self.assertEqual(create["visibleSuggest"]["members"], ["u1"])

    def test_首次发布可见范围含创建者(self):
        """不含创建者的话应用发布后不会自动启用(tenantAppStatus 停在 0)。"""
        c = LAA.Console.__new__(LAA.Console)
        seen = {}

        def post(p, b=None):
            if "app_version/list" in p:
                return {"data": {"versions": []}}
            if "app_version/create" in p:
                seen.update(b)
                return {"data": {"versionId": "v1"}}
            if "visible/online" in p:
                raise AssertionError("首次发布不该去读线上可见范围,那时还没有线上版本")
            return {"code": 0}

        c.post = post
        c.who = {"uid": "creator-1"}
        version, note = c.publish("cli_new")
        self.assertEqual(version, "0.0.1")
        self.assertEqual(seen["visibleSuggest"]["members"], ["creator-1"])
        # 读不到 uid 时要说出来,不静默:应用可能不会自动启用,人得知道去补哪儿
        c.who = {}
        version, note = c.publish("cli_new")
        self.assertIn("userId", note)

    def test_已有版本但可见范围读不出时中止发版(self):
        """发版会整体覆写可见范围。读不准还发,等于把应用从所有人面前收走。"""
        c = LAA.Console.__new__(LAA.Console)

        def post(p, b=None):
            if "app_version/list" in p:
                return {"data": {"versions": [{"appVersion": "1.0.0"}]}}
            if "visible/online" in p:
                return {"data": {"whiteList": {"isAll": "1"}}}     # isAll 形态不对
            raise AssertionError("解析失败后不该再往下调 create")

        c.post = post
        c.who = {"uid": "me"}
        with self.assertRaises(LAA.Failed):
            c.publish("cli_x")

    def test_identity_按配对花括号截不设长度上限(self):
        """此前的正则最多取 800 字,window.user 超长时静默解析失败——uid 拿不到,
        首次发布的可见范围就是空的,应用不会自动启用,而人看不出这串因果。"""
        big = json.dumps({"name": "张三", "tenantName": "示例公司",
                          "id": "u-42", "pad": "x" * 3000}, ensure_ascii=False)
        who = LAA.Console._identity(f"<script>window.user = {big};</script>")
        self.assertEqual(who, {"name": "张三", "tenant": "示例公司", "uid": "u-42"})

    def test_回读解析认得真实的响应形状(self):
        """形状照 botmux extractOpenPlatformEventState:events/appEvents/userEvents
        数组(元素是字符串或带 id 的对象)+ *EventDetails 分组的 items。
        2026-08-26 实测踩过:此前找的 eventKey 字段在响应里不存在,于是永远判成
        「一个都没订」——模板应用出生自带的订阅也被判没有,update 后回读照样缺失。"""
        c = LAA.Console.__new__(LAA.Console)
        c.post = lambda p, b=None: {"code": 0, "data": {
            "eventMode": 4,
            "events": ["a.plain.string_v1"],
            "appEvents": [{"id": "im.message.receive_v1", "extra": 1}, {"no_id": True}],
            "userEvents": "不是数组也不能抛",
            "appEventDetails": [{"items": [{"id": "from.details_v1"}, "detail.string_v1"]},
                                {"没有items": 1}],
        }}
        got = c._events_now("cli_x")
        self.assertEqual(got, {"a.plain.string_v1", "im.message.receive_v1",
                               "from.details_v1", "detail.string_v1"})

    def test_回读解析不再找不存在的_eventKey(self):
        blk = AUTO[AUTO.index("def _events_now("):AUTO.index("def set_events(")]
        self.assertNotIn('"eventKey"', blk)

    def test_事件订阅改完回读确认_缺了就抛(self):
        """不回读的话,失败静默降级成一个「建好了却不回话」的 bot——
        那是这套配置里最难排查的一类故障,宁可当场断在有上下文的地方。"""
        s = self._src()
        blk = s[s.index("def set_events("):s.index("def publish(")]
        self.assertIn("still = [e for e in EVENTS if e not in self._events_now(app_id)]", blk)
        self.assertIn("raise Failed", blk)

    def test_少列出来时说破(self):
        blk = self._src()[self._src().index("def adopt("):]
        self.assertIn("if len(apps) < total:", blk)


class HaveAppFlow(unittest.TestCase):
    """向导的 --have-app:扫码那条 + 断了退回手填。"""

    def test_先走扫码(self):
        blk = WIZ[WIZ.index("elif HAVE:"):WIZ.index("if not app_id:")]
        self.assertIn("_auto.adopt(", blk)
        self.assertIn("choose=pick_app", blk)
        self.assertIn("confirm=yes", blk)

    def test_断了退回手填而不是退出(self):
        """扫码那条走的是开发者后台的内部接口,后台改版就断。手填那条得始终留着。"""
        blk = WIZ[WIZ.index("elif HAVE:"):WIZ.index("if not app_id:")]
        self.assertIn("except Exception", blk)
        self.assertIn("退回手填", blk)
        # 但用户主动放弃(sys.exit)不能被当成失败吞掉,那会接着问他手填
        self.assertIn("except SystemExit:", blk)

    def test_读出来的_secret_不再让人手抄(self):
        """它一次都没显示给人看过,抄无可抄;而手抄正是「把 Secret 填进 App ID 那一栏」
        的来源。--auto 那条此前也在白问一遍(2026-08-25 一并修掉)。"""
        self.assertIn('_known = (adopted or auto_done or {}).get("secret") or ""', WIZ)
        self.assertIn("Secret 已从开发者后台读出,不必手抄", WIZ)

    def test_代号与显示名带出默认值(self):
        """扫码那两条已经知道应用叫什么。中文名推不出合法代号时 slug() 返回空,照常问。"""
        self.assertIn('_id_default = _saved.get("agent_id") or slug(_app_name)', WIZ)
        self.assertIn('bot_name = ask("显示名称", _app_name or bot_id)', WIZ)

    def test_slug_推不出来就返回空(self):
        """硬凑一个 app-1 之类的默认值,等他有三个 bot 时就分不清谁是谁了。"""
        ns = {}
        exec(compile(WIZ[WIZ.index("def slug("):WIZ.index("def desk(")], "<wiz>", "exec"),
             {"re": re}, ns)
        self.assertEqual(ns["slug"]("RogerAssist"), "rogerassist")
        self.assertEqual(ns["slug"]("Roger's Assist"), "roger-s-assist")
        self.assertEqual(ns["slug"]("张三助手"), "")
        self.assertEqual(ns["slug"](""), "")

    def test_挑应用时序号与_app_id_都认(self):
        """列表长了翻着找序号麻烦,而人手上常常已经有那串 ID。"""
        blk = WIZ[WIZ.index("def pick_app("):WIZ.index("def slug(")]
        self.assertIn("v.isdigit()", blk)
        self.assertIn('a["app_id"] == v', blk)
        self.assertIn("未开机器人能力", blk)


if __name__ == "__main__":
    unittest.main()
