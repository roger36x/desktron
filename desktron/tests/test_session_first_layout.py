"""会话回到第一位:任务与会话解绑,会话回侧栏。

2026-08-10 的信息架构调整(docs/session-first-restructure.md)。实测里任务行的点击率远低于
会话,而当时的结构把任务放在第一位:侧栏工作流下只列任务,会话要从工作流页进,且会话按任务
归属分组。改完之后会话回到侧栏、按工作流分组;任务不再绑定会话(`s:` 注记整体下线),
每段会话默认可读到本工作流全部任务的备忘。

这里钉三件事:`s:` 那条路后端前端都没有残留;侧栏折叠块下任务与会话并列;
行首图标按类型分形状(会话圆点、任务勾选框)。
"""
import os
import re
import sys
import tempfile
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
sys.path.insert(0, LIB)

FRONTEND = os.path.join(ROOT, "frontend")


def _read(*parts):
    if parts[-1] in ("app-chat.js", "app-panel.js", "app-rail.js", "app-assets.js", "app-settings.js"):
        return _srcmap.frontend_src(parts[-1])   # 2026-08-27 分拆,拼接整体等价
    if parts[-1] == "term.py":
        return _srcmap.term_src()
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


PANEL = _read(FRONTEND, "app-rail.js") + "\n" + _read(FRONTEND, "app-panel.js")
TASKS_JS = _read(FRONTEND, "app-tasks.js")
ASSETS = _read(FRONTEND, "app-assets.js")
CSS = _read(FRONTEND, "style.css")
TASKS_PY = _read(LIB, "tasks.py")
TASK_API = _read(LIB, "task", "api.py")
TERM_PY = _read(LIB, "term.py")


class SessionBindingIsGone(unittest.TestCase):
    """绑定这条路整体下线:留一半会让 TASKS.md 继续长出 `s:`,迁移就白做了。"""

    def test_后端没有绑定函数与路由分支(self):
        for name in ("task_session_bind", "task_session_migrate"):
            self.assertNotIn(name, TASKS_PY)
            self.assertNotIn(name, TERM_PY)
        self.assertNotIn("sess-bind", TASK_API)
        self.assertNotIn("sess-unbind", TASK_API)

    def test_注记键表不再解析_s(self):
        m = re.search(r'for k in \("t", "c", "d",.*?\):', TASKS_PY)
        self.assertTrue(m)
        self.assertNotIn('"s"', m.group(0))

    def test_注入给_agent_的字段词表不再交代_s(self):
        self.assertNotIn("已归入本任务的会话 id", TASKS_PY)

    def test_前端没有绑定调用与按任务过滤(self):
        for src in (PANEL, TASKS_JS, ASSETS):
            self.assertNotIn("sess-bind", src)
            self.assertNotIn("taskPeer", src)
            self.assertNotIn("_taskSids", src)
            self.assertNotIn("_wfBoundSids", src)

    def test_从任务新建会话只写多对多账本关联(self):
        launch = re.search(r"async function _wfTaskNewSession\(.*?\n}\n", PANEL, re.S).group(0)
        self.assertIn('op: "ledger"', launch)
        self.assertIn('intent: "claim"', launch)
        self.assertIn("task_id: t.t", launch)
        self.assertIn("revision: t.ledger && t.ledger.revision", launch)
        self.assertNotIn("sess-bind", launch)
        self.assertNotIn('key: "s"', launch)

    def test_读取仍然容忍存量注记(self):
        """迁移脚本漏掉的、或 agent 手写回去的 `s:` 不该让整行解析不出来。"""
        import tasks

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "TASKS.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write("- [~] 甲   <!-- c:2026-08-01T10:00 s:abc,def note:n1 -->\n")
            items = tasks.read_tasks(path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "甲")
        self.assertEqual(items[0]["note"], "n1")
        self.assertNotIn("s", items[0])   # 不再进条目,前端也就拿不到它


class SidebarListsSessions(unittest.TestCase):
    def test_折叠块下只列会话(self):
        fold = re.search(r"function projectFold\(p(?:, [^)]*)?\)[\s\S]+?\n}\n", PANEL).group(0)
        self.assertIn('sessHost.className = "pf-sess"', fold)
        self.assertIn("renderProjSessions(sessHost, p.id)", fold)

    def test_侧栏不再有任务清单(self):
        """一列里两种东西并排谁都不突出;任务改在会话视图输入框上沿那一条看。"""
        for name in ("renderWfTasks", "_wfTaskRow", "_wfSideMore", "pf-tasks"):
            self.assertNotIn(name, PANEL)

    def test_会话块进保鲜轮询(self):
        self.assertIn('details.pf-work[open] .pf-sess', PANEL)

    def test_聊天组默认折叠(self):
        # 归在「聊天」的会话占多数,默认铺开会把工作流那几段挤出视野
        self.assertIn('localStorage.getItem("cd_loose_collapsed") !== "0"', PANEL)

    def test_单击工作流名称只切换展开折叠(self):
        fold = re.search(r"function projectFold\(p(?:, [^)]*)?\)[\s\S]+?\n}\n", PANEL).group(0)
        click = re.search(r"summary\.onclick = \(ev\) => \{([\s\S]+?)\n  \};", fold).group(1)
        self.assertIn("det.open = !det.open", click)
        self.assertNotIn("selectNav", click)
        self.assertNotIn("closest", click)   # 名称与箭头不再分成两套单击行为

    def test_工作流项目管理从行操作菜单进入(self):
        fold = re.search(r"function projectFold\(p(?:, [^)]*)?\)[\s\S]+?\n}\n", PANEL).group(0)
        menu = re.search(r"async function _openWorkflowMenu\(.*?\n}\n", PANEL, re.S).group(0)
        self.assertIn('header: DeskI18n.t("wf.projects")', menu)
        self.assertIn('label: DeskI18n.t("wf.addProject")', menu)
        self.assertIn('label: DeskI18n.t("wf.delete")', menu)
        self.assertNotIn("工作流详情", menu)
        self.assertIn("_openWorkflowMenu(p, r.right, r.bottom)", fold)
        self.assertIn("_openWorkflowMenu(p, ev.clientX, ev.clientY)", fold)


class RowGlyphs(unittest.TestCase):
    def test_会话行首是圆点不是引擎图标(self):
        stat = re.search(r"function termStatHTML\(s\)[\s\S]+?\n}\n", PANEL).group(0)
        self.assertIn('<i class="ti-pt">', stat)
        self.assertNotIn("BRAND_ICONS", stat)
        # 多侧栏的引擎选择器可以用品牌图标；会话行的首渲与就地更新仍须用圆点。
        for name in ("updateTermRow", "renderProjSessions", "renderLooseSessions"):
            body = re.search(r"(?:async )?function " + name + r"\([^)]*\)[\s\S]+?\n}\n", PANEL).group(0)
            self.assertNotIn("BRAND_ICONS", body)
        self.assertIn(".term-item .ti-stat .ti-pt", CSS)

    def test_在跑的是外圈那层晕在呼吸(self):
        # 呼吸挂在晕上,不挂在点本体:点是「这段会话在这儿」,它忽明忽暗读起来像信号不稳。
        # 晕因此得单独成一层(::after),box-shadow 跟着元素的 opacity 一起变,没法只让它动。
        m = re.search(r"\.ti-stat\.st-working \.ti-pt::after \{([^}]+)\}", CSS)
        self.assertTrue(m)
        self.assertIn("animation:", m.group(1))
        pt = re.search(r"\.ti-stat \.ti-pt \{([^}]+)\}", CSS).group(1)
        self.assertNotIn("box-shadow", pt)
        self.assertNotIn("animation", pt)   # 点本体不动

    def test_就地刷新给出的状态与首渲同口径(self):
        # 轮询走 updateTermRow,首渲走 termStatHTML;两处都要认 dormant,否则一段会话
        # 关掉之后那颗点还停在「空闲」。各态具体怎么画由视觉那一侧调,这里只锁口径。
        m = re.search(r"const cls = \"ti-stat st-\" \+ ([^;]+);", PANEL)
        self.assertTrue(m)
        self.assertIn("termState(t)", m.group(1))

    def test_任务行首是勾选框(self):
        # 外框按 unchecked 那枚空框的比例,三态只换框内内容。
        # 「进行中」原来另画一圈填充的晕(.wt-cb-halo,不走描边),2026-08-25 改成三态共用
        # 同一个描边外框——那一版的外缘跟「未开始」长得不一样,读成了两种控件(用户实见)。
        # 细节口径见 test_frontend_task_cb_doing.py,这里只锁「确实是同一个外框」。
        svg = re.search(r"function taskCbSvg\(state\)[\s\S]+?\n}\n", PANEL).group(0)
        self.assertIn('class="wt-cb-box" x="1" y="1" width="14" height="14"', svg)
        self.assertNotIn('class="wt-cb-halo"', svg)   # 旧画法,已撤
        self.assertIn('viewBox="0 0 16 16"', svg)   # 视口收成 16,方框填满盒子(具体尺寸归视觉那一侧调)
        self.assertIn("polyline", svg)             # 已完成:框内打勾
        self.assertIn('class="wt-cb-in"', svg)     # 进行中:框内实心块
        self.assertIn(".wt-cb-in {", CSS)
        self.assertNotIn(".wt-cb.live", CSS)     # 任务不再有「下面有会话在跑」这层状态
        self.assertNotIn("data-sids", PANEL)

    def test_详情浮层勾选框与标题垂直居中(self):
        m = re.search(r"\.wf-sheet-title-group \{([^}]+)\}", CSS)
        self.assertTrue(m)
        self.assertIn("align-items: center", m.group(1))

    def test_任务写回只有一份实现(self):
        """SoT 是 domains/<id>/TASKS.md,写它的入口有四个(侧栏清单、工作流页、会话输入框、
        TaskPaper 应用)。原来各写各的:改名的 prompt 抄了两份,删除有的问有的不问,而且在
        一处改完另一处不跟着变。现在写回统一走 app-core 的 taskWrite,视图把自己的刷新
        登记进 taskViewRegister。这条盯住的是「别又回到各写各的」。"""
        core = _read(FRONTEND, "app-core.js")
        for fn in ("function taskWrite", "function taskSetState", "function taskRename",
                   "function taskDelete", "function taskAdd", "function taskAnnot",
                   "function taskViewRegister"):
            self.assertIn(fn, core)
        # 写回的地址只在共用那一份里出现;各视图不再自己拼这个 URL
        url = "/tasks`"
        for name, src in (("app-tasks.js", TASKS_JS), ("app-chat.js", _read(FRONTEND, "app-chat.js"))):
            self.assertNotIn("method: \"POST\"", src.split(url)[0][-400:] if url in src else "",
                             f"{name} 不该再自己发任务写回")
        # 两个常驻视图都登记了刷新,任何入口写完它们都会跟着重绘
        self.assertIn("taskViewRegister(", TASKS_JS)
        self.assertIn("taskViewRegister(", PANEL)

    def test_画任务状态的地方都用同一枚勾选框(self):
        # 现在是全局任务页与任务详情浮层的标题前导;
        # 漏掉哪处就会在那一处留下旧的 CSS 圆点
        self.assertEqual(PANEL.count("taskCbSvg(t.state)"), 2)


CHAT = _read(FRONTEND, "app-chat.js")


class TaskBar(unittest.TestCase):
    """会话视图输入框上沿的任务栏:会话不再归属任务之后,「这个工作流在办什么」的落点。"""

    def test_任务栏在输入框之上_与目标条同一段(self):
        build = re.search(r"function hsBuild\(el, sid, title\)[\s\S]+?const v =", CHAT).group(0)
        # 任务现已进入会话上下文托盘；托盘仍与目标、输入框共用 hs-col 内容轨道，
        # 并保持在两者之间。任务只是托盘内的一项，不再自己占据整条内容轨道。
        self.assertIn('class="hs-tasks"', build)
        self.assertLess(build.index('class="hs-goal hs-col"'), build.index('class="hs-context hs-col"'))
        self.assertLess(build.index('class="hs-context hs-col"'), build.index('class="hs-box hs-col"'))

    def test_归属口径与侧栏一致(self):
        fn = re.search(r"function hsRowDomain\(row\)[\s\S]+?\n}\n", CHAT).group(0)
        self.assertIn("row.domain || DESKTOP", fn)
        self.assertIn("(row.refs || [])[0]", fn)   # 归属在聊天、被工作流引用的,任务栏跟着那个工作流

    def test_归属直接问会话记录不绕引擎侧id(self):
        # engine_sid 要跑过一个回合才有,不落引擎侧 id 的适配器一直没有;
        # 归属写在 /session 那一行自己身上,绕道会话表反查对它恒不通。
        fn = re.search(r"async function hsSessionDomain\(v\)[\s\S]+?\n}\n", CHAT).group(0)
        self.assertIn('api("/session")', fn)
        self.assertIn("x.id === v.sid", fn)
        sync = re.search(r"async function hsTasksSync\(v\)[\s\S]+?\n}\n", CHAT).group(0)
        self.assertNotIn("hsEngineSid", sync)
        self.assertNotIn("_termsList", sync)

    def test_任务栏不显示内部诊断(self):
        # 这一栏是给人看任务的。查不到归属就静默收起,不把 sid / esid / 表内行数摆在用户面前。
        # 0443d72 起退路分成两类:确定无归属走 hsTasksHide 收起;
        # 取数失败(查归属失败 / 拉任务失败)改走 hsTasksBail——保留上一份、5s 重试,
        # 不再把"暂时取不到"当成"没有任务"。诊断字样只进 deskLog,页面上仍然什么都不写。
        self.assertNotIn("_hsTasksWhy", CHAT)
        self.assertNotIn("诊断:", CHAT)
        sync = re.search(r"async function hsTasksSync\(v\)[\s\S]+?\n}\n", CHAT).group(0)
        self.assertEqual(sync.count("hsTasksHide(v, box)"), 1)   # 只有无归属才收起；零任务仍常驻
        self.assertEqual(sync.count("hsTasksBail(v, box"), 2)    # 查归属失败 / 拉任务失败:保留重试

    def test_只列在办的任务(self):
        fn = re.search(r"async function hsTasksSync\(v\)[\s\S]+?\n}\n", CHAT).group(0)
        self.assertIn('t.state !== "done"', fn)

    def test_不属于工作流的会话不出现任务栏(self):
        # 查不到归属就不往下走(具体是直接藏还是留一句诊断,由那一侧决定),
        # 但绝不能带着空的 dom 去 _fetchTasks——那会打到 /workbench/domains//tasks
        fn = re.search(r"async function hsTasksSync\(v\)[\s\S]+?\n}\n", CHAT).group(0)
        self.assertIn("if (!dom)", fn)
        self.assertLess(fn.index("if (!dom)"), fn.index("_fetchTasks(dom)"))

    def test_回合边界与建页各取一次(self):
        self.assertIn("hsGoalSync(v); hsAgentsSync(v); hsTasksSync(v);", CHAT)
        # 真实会话建页各取一次；尚未发送的聊天空页没有后端归属，明确跳过。
        tail = re.search(r"if \(v\.ephemeral\)[\s\S]+?else \{[\s\S]+?"
                         r"hsGoalSync\(v\);\n    hsTasksSync\(v\);\n"
                         r"    hsArrangementsSync\(v\);\n  \}\n  return v;", CHAT)
        self.assertTrue(tail)
        # 盖住的页不在建页时取,露面时补:两处合起来仍是「各取一次」
        hidden = re.search(r"else if \(hsHidden\(v\)\) \{[\s\S]+?v\.needMeta = true;", CHAT)
        self.assertTrue(hidden)
        shown = re.search(r"function hsShown\(v\)[\s\S]+?if \(v\.needMeta\)[\s\S]+?"
                          r"hsGoalSync\(v\);\n    hsTasksSync\(v\);\n"
                          r"    hsArrangementsSync\(v\);", CHAT)
        self.assertTrue(shown)

    def test_任务列表显示任务ID和名称但不重复勾选框(self):
        sync = re.search(r"async function hsTasksSync\(v\)[\s\S]+?\n}\n", CHAT).group(0)
        self.assertIn("row.innerHTML = '<span class=\"hs-task-id\"></span><span class=\"hs-task-t\"></span>'", sync)
        self.assertIn('const taskID = String(t.t || "").trim()', sync)
        self.assertIn('id.classList.toggle("empty", !taskID)', sync)
        self.assertNotIn("taskCbSvg(t.state)", sync)

    def test_展开后可以就地加任务(self):
        self.assertIn('class="hs-tasks-add"', CHAT)
        fn = re.search(r"const tasksAdd = el\.querySelector[\s\S]+?\n  \};", CHAT).group(0)
        # 四个入口共用 app-core 的那一份(taskAdd),不各写各的:文案、确认与写完之后的重绘
        # 都收在那里,这里只负责把标题递过去
        self.assertIn("taskAdd(v.taskDom, title)", fn)
        self.assertIn("ev.isComposing", fn)   # 输入法合成态的回车是选字,不是提交
        paint = re.search(r"function hsTasksPaint\(v, open\)[\s\S]+?\n}\n", CHAT).group(0)
        self.assertIn('.hs-tasks-popover").hidden = !open', paint)   # 输入框随整个弹层一起显隐

    def test_加任务那一行与任务名同左缘(self):
        # 27 = 6 内距 + 13 勾选框 + 8 间隙;输入框自带 1px 边框,所以内距少给 1
        cb = re.search(r"\.hs-page \.hs-task-cb \{([^}]+)\}", CSS).group(1)
        self.assertIn("13px", cb)
        self.assertIn(".hs-page .hs-task-cb svg { display: block; }", CSS)   # 行内 svg 会按基线摆,整枚浮高
        add = re.search(r"\.hs-page \.hs-tasks-add \{([^}]+)\}", CSS).group(1)
        self.assertIn("27px", add)   # 与任务名同一左缘;无边框,所以不必扣那 1px

    def test_任务栏复用工作流任务详情(self):
        fn = re.search(r"function hsTasksSync\(v\)[\s\S]+?\n}\n", CHAT).group(0)
        self.assertIn("wfOpenTaskSheet(t, dom)", fn)
        self.assertNotIn("hsTaskMemo", CHAT)
        self.assertNotIn(".hs-memo", CSS)


if __name__ == "__main__":
    unittest.main()
