"""HTML 会话视图不再假定「GUI 就是 claude」。

这一层没有渲染环境可跑,只能钉源码上的几处硬编码——它们正是 2026-08-08 之前把
codex 挡在 GUI 之外的原因:档位存在 engine_opts.claude、切终端一律传 "claude"、
档位表只有 claude 那一套。改法是从 /config 取 engine 与 caps,按位分流。
"""
import json
import os
import subprocess

import _srcmap
import re
import unittest
from frontend_i18n_js import NODE_PRELUDE

FRONT = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
CHAT = _srcmap.chat_src()
PANEL = _srcmap.panel_src()
ASSETS = _srcmap.assets_src()
CSS = open(os.path.join(FRONT, "style.css"), encoding="utf-8").read()
EVENTS = open(os.path.join(FRONT, "app-events.js"), encoding="utf-8").read()
UI_SERVER = _srcmap.ui_server_src()
ADAPTERS = _srcmap.engine_adapters_src()
COMMANDS = open(os.path.join(os.path.dirname(__file__), "..", "lib", "engine_adapters",
                             "command_manifest.py"), encoding="utf-8").read()


class EngineMenuUI(unittest.TestCase):
    def test_独立与联合引擎分组都有标题(self):
        self.assertIn('menu.firstChild.textContent = DeskI18n.t("engineMenu.standalone")', EVENTS)
        self.assertIn('jointHead.textContent = DeskI18n.t("engineMenu.joint")', EVENTS)
        self.assertLess(EVENTS.index('separator.setAttribute("role", "separator")'),
                        EVENTS.index('jointHead.textContent = DeskI18n.t("engineMenu.joint")'))
        self.assertLess(EVENTS.index('jointHead.textContent = DeskI18n.t("engineMenu.joint")'),
                        EVENTS.index('shepherd.className = "fm-row fm-shepherd"'))

    def test_普通引擎与Shepherd书桌互斥(self):
        """左下只选择书桌；Shepherd 内部执行引擎不在这里显示或切换。"""
        self.assertIn('${currentEngine && !shepherdActive ? RP_CHECK : ""}</span>', EVENTS)
        self.assertIn('currentEngine && !shepherdActive ? " on" : ""', EVENTS)
        self.assertIn('body: JSON.stringify({ engine: id, shepherd_mode: false })', EVENTS)
        self.assertNotIn('fm-current', EVENTS)
        self.assertNotIn("ICONS.check", EVENTS)
        self.assertNotIn('<span class="fm-check">✓</span>', EVENTS)

    def test_菜单固定宽度不按名字收缩(self):
        """移动端不能用 width:auto，否则脚本清掉 right 后会按名字收缩。"""
        rules = re.findall(r"#engine-menu\s*\{[^}]*\}", CSS)
        self.assertIn("width: 208px", rules[-1])
        self.assertNotIn("width: auto", rules[-1])

    def test_标记用中性亮灰不用绿(self):
        # 这个选择器出现两次(另一处只调 padding),取带 color 的那一条
        rules = [x for x in re.findall(r"#engine-menu \.fm-check \{[^}]*\}", CSS) if "color:" in x]
        self.assertEqual(len(rules), 1, "带 color 的 .fm-check 规则应当只有一条")
        self.assertIn("color: var(--ink-91)", rules[0])
        self.assertNotIn("#30d158", CSS[CSS.index("#engine-menu .fm-check {"):][:400])

    def test_选中底改用中性抬升面(self):
        """紫在这套界面里不再承担「当前项」。--surface-active 是侧栏、设置导航共用的那一档,
        两套主题各有取值,所以浅色不必再单开一条覆盖。"""
        m = re.search(r"#engine-menu \.fm-row\.on \{[^}]*\}", CSS)
        self.assertTrue(m)
        self.assertIn("background: var(--surface-active)", m.group(0))
        self.assertNotIn("rgba(112,78,157", m.group(0))
        self.assertNotIn('html[data-theme="light"] #engine-menu .fm-row.on', CSS)

    def test_shepherd_all_visible_entries_share_size_and_optical_alignment(self):
        for selector in ('#engine-menu .fm-icon .brand-shepherd',
                         '.side-foot #engine-pick .fx-ic .brand-shepherd',
                         '.frw-engine-icon .brand-shepherd'):
            rule = re.search(re.escape(selector) + r'\s*\{([^}]+)\}', CSS).group(1)
            self.assertIn('width: 18px; height: 18px', rule)
            self.assertNotIn('transform:', rule)
        common = re.search(r'\.brand-shepherd\.img-ic\s*\{([^}]+)\}', CSS).group(1)
        self.assertIn('transform: none', common)
        self.assertIn('/icons/shepherd.png', common)
        slot = re.search(r"#engine-menu \.fm-icon\s*\{([^}]+)\}", CSS[CSS.index('/* 引擎菜单是切换器'):]).group(1)
        self.assertIn('width: 20px; height: 20px', slot)

    def test_菜单使用紧凑切换器几何(self):
        self.assertRegex(CSS, r"#engine-menu \{[^}]+width: 208px")
        self.assertRegex(CSS, r"#engine-menu \.fm-row \{[^}]+min-height: 36px")


class NoHardcodedEngine(unittest.TestCase):
    def test_档位存进当前引擎的键_不再写死_claude(self):
        # 键按当前引擎取。写的值从 patch 换成了合并后的 v.opts(2026-08-16 档位改成会话级:
        # 会话那一支先写、拿回合并结果,引擎级再存同一份做新会话的起点),键的口径没变。
        self.assertNotIn("opts: { claude:", CHAT)
        self.assertIn("{ opts: { [eng]: engineDefaults },", CHAT)
        self.assertIn('for (const key of ["desk_role", "shepherd_mode", "shepherd_trial", "active_engine"])', CHAT)
        self.assertIn("delete engineDefaults[key]", CHAT)

    def test_登录终端按会话自己的引擎(self):
        """原来这条锁的是「切回终端视图时用会话自己的引擎」。TUI 2026-08-20 退场后
        没有可切回去的视图了,同一个「别写死 claude」的约束落在登录终端上:
        /login 要跑被点的这段会话所属引擎的登录命令,不是当前偏好的那个。"""
        m = re.search(r"function hsLoginShell\(v, kind = \"login\", source = \{\}\)[\s\S]+?\n}", CHAT)
        self.assertTrue(m)
        self.assertIn("(v && v.engine)", m.group(0))
        self.assertNotIn('HS_AUTH_CMD["claude"]', m.group(0))

    def test_engine_从_config_取(self):
        """原来用 {0,3200} 截这个函数。那是个会过期的上限——函数一长就截不到,
        断言随之落空而不是变红(2026-08-25 补了失败兜底之后正好超了)。改成按结尾锚点取。"""
        m = re.search(r"async function hsEngineSync\(v\)[\s\S]*?\n}\n", CHAT)
        self.assertTrue(m)
        self.assertIn("v.engine = cf.engine", m.group(0))


def _entry(src, key):
    """档位表里某个引擎那一整条。非贪婪的正则会停在 models 数组里的第一个 `},`,
    截出来的只有开头两行,断言等于没断。这里按花括号配对取,长短不影响。"""
    i = src.index("  %s: {" % key) + len("  %s: " % key)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError("%s 那一条没有配对的花括号" % key)


class PerEngineTables(unittest.TestCase):
    def test_各引擎各有一张档位表(self):
        m = re.search(r"const HS_ENGINE_UI = \{[\s\S]+?\n\};", CHAT)
        self.assertTrue(m)
        for k in ("claude:", "codex:", "pi:"):
            self.assertIn(k, m.group(0))
        # OpenCode 于 2026-08-28 再次退役,档位表里不该再留它那一档
        self.assertNotIn("opencode", m.group(0))

    def test_codex_的mode与sandbox分开(self):
        self.assertIn('collaboration_mode', CHAT)
        self.assertIn('hsPickSandbox', CHAT)
        self.assertIn('class="hs-sandbox"', CHAT)
        for v in ("read-only", "workspace-write", "danger-full-access"):
            self.assertIn(v, CHAT)

    def test_codex模型与effort来自后端目录(self):
        self.assertIn('v.catalog = cf.options', CHAT)
        self.assertIn('function hsEfforts(v)', CHAT)
        self.assertIn('(hsModelRoute(v) || {}).efforts', CHAT)

    def test_plan默认effort优先于模型默认值(self):
        m = re.search(r"function hsEffortDefault\(v\)[\s\S]+?\n}", CHAT)
        self.assertTrue(m)
        self.assertLess(m.group(0).find("mode.defaultEffort"), m.group(0).find("model.defaultEffort"))

    def test_appserver目录未到时mode仍完整显示(self):
        self.assertIn('{ id: "default", label: "Default" }', CHAT)
        self.assertIn('{ id: "plan", label: "Plan" }', CHAT)
        self.assertIn("return live.length ? live : (hsUI(v).modes || [])", CHAT)

    def test_codex设置写当前会话(self):
        self.assertIn('/settings`', CHAT)
        self.assertIn('JSON.stringify({ opts: patch, engine: eng })', CHAT)
        # 键按当前引擎取:写死 codex 会把别的引擎的会话设置
        # 存到 codex 那一格去
        self.assertIn("opts: { [eng]: engineDefaults }", CHAT)

    def test_不给切模型的引擎不弹空菜单(self):
        m = re.search(r"function hsShowModelMenu\(v, anchor,.*?[\s\S]+?\n}", CHAT)
        self.assertIn("if (!items.length)", m.group(0))

    def test_档位表取不到不回落到某个具体引擎(self):
        # 回落 claude 正是这次缺陷的形态:deepseek 会话上列着 Opus 5 / Effort / ultracode
        m = re.search(r"function hsUI\(v\)[\s\S]+?\n}", CHAT).group(0)
        self.assertNotIn("HS_ENGINE_UI.claude", m)
        self.assertIn("HS_UI_NONE", m)

    def test_沙箱那一枚按有没有档位显隐_不按引擎名(self):
        m = re.search(r"const sb = v\.el\.querySelector\(\"\.hs-sandbox\"\);[\s\S]+?\n  \}", CHAT).group(0)
        self.assertIn("hsUiHidden(sb, !list.length)", m)
        self.assertNotIn("hsIsCodexLike", m)

    def test_没有模式的引擎不显示模式那一枚(self):
        m = re.search(r"const mo = v\.el\.querySelector\(\"\.hs-mode\"\);[\s\S]+?\n  \}", CHAT).group(0)
        self.assertIn("!hsModes(v).length", m)


class ComposerControlsAcrossEngines(unittest.TestCase):
    """底栏按实际正文列宽收缩,不按引擎或整窗宽度分叉。"""

    @staticmethod
    def rule(selector):
        m = re.search(r"\n%s \{[^}]*\}" % re.escape(selector), CSS)
        assert m, "找不到 %s" % selector
        return m.group(0)

    def test_公共底栏空间不足时横向滚动(self):
        row = self.rule(".hs-page .hs-row")
        self.assertIn("flex-wrap: nowrap", row)
        self.assertIn("overflow-x: auto", row)
        self.assertIn("overflow-y: hidden", row)
        self.assertIn("scrollbar-width: none", row)
        self.assertIn("flex: 0 0 auto", self.rule(".hs-page .hs-row > *"))

    def test_权限模式与模型标签不在控件内断行(self):
        modes = self.rule(".hs-page .hs-mode, .hs-page .hs-sandbox")
        model = self.rule(".hs-page .hs-model")
        name = self.rule(".hs-page .hs-model .hs-m1")
        self.assertIn("white-space: nowrap", modes)
        self.assertIn("white-space: nowrap", model)
        self.assertIn("text-overflow: ellipsis", name)
        self.assertIn("max-width: 11em", name)

    def test_发送钮留在可见区(self):
        send = self.rule(".hs-page .hs-send")
        self.assertIn("position: sticky", send)
        self.assertIn("right: 0", send)
        self.assertIn("var(--hs-box-bg)", send)

    def test_所有引擎复用同一套底栏(self):
        start = CHAT.index("'<div class=\"hs-row\">'")
        end = CHAT.index('class="hs-send"', start)
        markup = CHAT[start:end + len('class="hs-send"')]
        for cls in ("hs-mode", "hs-sandbox", "hs-agent", "hs-model", "hs-send"):
            self.assertIn(cls, markup)
        self.assertEqual(CHAT.count('class="hs-row"'), 1)


class SessionListingAcrossEngines(unittest.TestCase):
    """/session 列全部适配器的会话,而不只是当前偏好那一个。

    会话 id 全局唯一,前端拿着一个 id 来问「它的引擎侧 id 是什么、它属于哪个工作流」。
    只列偏好那个引擎的话,别的引擎下的会话根本不在返回里,前端据此判定「查不到」——
    2026-08-15 用户看到的那条诊断文字就是这么来的。
    """

    def test_不再只列偏好引擎(self):
        self.assertNotIn("self._send_json(ad.list_sessions())", UI_SERVER)
        self.assertIn("_sessions_listing", UI_SERVER)
        m = re.search(r"def _sessions_listing\(cls, only=\"\"\):[\s\S]+?\n        return rows", UI_SERVER)
        self.assertTrue(m)
        self.assertIn("for name in GUI_ENGINES:", m.group(0))
        self.assertIn('row["engine"] = name', m.group(0))   # 行里要标明自己属于哪个引擎

    def test_每行补齐归属(self):
        m = re.search(r"def _session_belonging\(row\):[\s\S]+?\n        return out", UI_SERVER)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn("_term_meta_read()", seg)      # 索引里没写归属的老会话退到会话表那份
        self.assertIn('out["refs"]', seg)            # 引用不是归属,但侧栏口径要它兜底

    def test_仍可单看一个引擎(self):
        self.assertIn('self._sessions_listing((q.get("engine") or [""])[0].strip())', UI_SERVER)


class CapsDriven(unittest.TestCase):
    def test_分支按能力位显隐(self):
        m = re.search(r"function hsCapsPaint\(v\)[\s\S]+?\n}", CHAT)
        self.assertIn("caps.fork", m.group(0))
        self.assertIn("caps.rewind", m.group(0))

    def test_每回合那一行也按位定_挂载前找不到自己(self):
        self.assertIn("fk.hidden = !(v.caps && v.caps.fork)", CHAT)

    def test_斜杠命令按能力位筛(self):
        self.assertIn("any(not caps.get(key)", COMMANDS)
        self.assertIn("'requires'", COMMANDS)

    def test_压缩命令按能力位显隐(self):
        self.assertIn('"compact": bool(getattr(ad, "compact_supported", False)', UI_SERVER)
        self.assertIn("command_manifest", UI_SERVER)
        self.assertIn("'/compact'", COMMANDS)


class SlashCommandIsolation(unittest.TestCase):
    def test_本地斜杠命令来自后端manifest(self):
        self.assertIn("function hsCommandRows(v)", CHAT)
        self.assertIn("manifest.version === 1", CHAT)
        self.assertIn('row.owner === "desk"', CHAT)
        self.assertNotIn("HS_SLASH_BY_ENGINE", CHAT)

    def test_pi本地命令覆盖真实能力(self):
        for command in ('/mode', '/model', '/effort', '/compact', '/clear'):
            self.assertIn("'%s'" % command, COMMANDS)
            self.assertIn('"%s"' % command, CHAT)

    def test_表里没有的引擎只给通用命令_不回落claude(self):
        m = re.search(r"function hsLocalSlash\(v\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn("hsCommandRows(v)", m)
        self.assertNotIn("v.engine", m)

    def test_codex拼接官方引擎slash并由本地同名优先(self):
        # Codex app-server 能处理 /goal、/status 等原生命令。前端只负责补全,
        # 同名的 Desk 本地动作仍优先,例如 /compact 走 GUI RPC。
        m = re.search(r"function hsSlashAll\(v\)[\s\S]+?\n}", CHAT)
        self.assertTrue(m)
        self.assertIn('rows.filter((row) => row.owner !== "desk")', m.group(0))
        self.assertIn("return local.concat(eng.filter((c) => !own.has(c.n)))", m.group(0))
        self.assertNotIn("hsIsCodexLike", m.group(0))
        seg = re.search(r"class CodexAdapter[\s\S]+?def slash_commands\(self\):[\s\S]+?\n        \]",
                        ADAPTERS)
        self.assertTrue(seg)
        self.assertIn('"n": "goal"', seg.group(0))
        self.assertIn('"n": "status"', seg.group(0))

    def test_直接输入本地命令先执行不发给模型(self):
        m = re.search(r"async function hsSend\(v\)[\s\S]+?\n}", CHAT)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn("if (v.ask && !v.ask.permission)", seg)
        self.assertLess(seg.index("const localCmd"), seg.index("if (v.ask && !v.ask.permission)"))
        self.assertIn("hsRunLocalSlash(v, localCmd.cmd, localCmd.args)", seg)
        self.assertIn("return;", seg[:seg.index("if (v.ask && !v.ask.permission)")])

    def test_shepherd未接管命令不进普通消息通道(self):
        m = re.search(r"async function hsSend\(v\)[\s\S]+?\n}", CHAT)
        self.assertTrue(m)
        seg = m.group(0)
        guard = 'v.shepherdMode && /^\\/[\\w:-]+(?=\\s|$)/.exec(text)'
        self.assertIn(guard, seg)
        self.assertLess(seg.index(guard), seg.index("if (v.ephemeral"))

    def test_compact走真实后端端点而不是普通prompt(self):
        self.assertIn('function hsCompact(v)', CHAT)
        self.assertIn('/compact`', CHAT)
        self.assertIn('timeout: 300000', CHAT)

    def test_compact先建用户命令回合再挂状态(self):
        m = re.search(r"async function hsCompact\(v\)[\s\S]+?\n}", CHAT)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn('hsNewTurn(v, "/compact"', seg)
        self.assertLess(seg.find('hsNewTurn(v, "/compact"'), seg.find('hsStatus(v, "working"'))
        self.assertIn("hsStatusClear(v)", seg)

    def test_compact无token也按trigger显示分隔线(self):
        m = re.search(r'if \(type === "session\.compact"\) \{[\s\S]+?return;\n  \}', CHAT)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn("if (p.running)", seg)
        self.assertIn("hsCompactOn(v, p.started || Date.now())", seg)
        # 条件后来放宽了:claude 的收尾帧三样都没有,只给 compact_result,
        # 画线那一支得认它(2026-08-26,见 test_compact_status_frame)。这里只钉住
        # 「trigger 仍是画线的依据之一」,不再断言整条条件的字面。
        self.assertIn("p.pre || p.post || p.trigger || p.result", seg)
        self.assertLess(seg.find('p.trigger'), seg.find('hsCompactRow'))


class CompactRowFollowsOutput(unittest.TestCase):
    """2026-09-12：手动与自动都紧跟压缩前输出，不再按命令或回合头定位。"""

    def _rebuild(self):
        at = CHAT.index("let pendGoal = null;")
        return CHAT[at:CHAT.index("v.pouring = wasPouring;", at)]

    def test_手动自动共用输出锚点(self):
        seg = self._rebuild()
        self.assertNotIn('info.compact.trigger === "manual"', seg)
        self.assertIn("hsCompactRow(v, txt, info.compact, undefined, info.id)", seg)

    def test_不再按消息时刻延后(self):
        seg = self._rebuild()
        self.assertNotIn("pendCmp", seg)
        self.assertNotIn("flushCmp", seg)

    def test_实时在开始时捕获锚点(self):
        self.assertIn("v.cmpAnchor = hsCompactAnchor(v)", CHAT)
        self.assertIn('hsCompactRow(v, "", p, v.cmpAnchor)', CHAT)

    def test_续写认领原过程区(self):
        self.assertIn("compactHost.compactMids ||= new Set()", self._rebuild())


class AdoptCarriesTheEngine(unittest.TestCase):
    """被点的那段会话未必属于当前偏好的引擎,引擎要一路带到底。

    2026-08-10 实见:偏好切到 codex 之后,点开一段 claude 会话是一片空白。自动分流那条路
    调 termToHtml 时没把 engine 带上,后端只能退回偏好 → 拿 codex 的读法去找一份不存在的
    rollout → 认领失败;失败后的回退又用 curEngine,于是再拿 codex 去 resume 一个 claude
    uuid,终端同样是空的。两处都要按会话自己的引擎走。
    """

    def test_自动分流把引擎一并送出(self):
        m = re.search(r"async function attachTerminal\(sessionId, engine, opts\)[\s\S]{0,2000}?termToHtml\([\s\S]{0,200}?\);", ASSETS)
        self.assertTrue(m)
        self.assertIn("engine: eng", m.group(0))

    def test_认领失败不再退回终端(self):
        """TUI 退场后没有可退回去的视图。自动分流那一路静默(刚建的会话本来就没有记录可读),
        用户显式点开的那一路仍要说一声。"""
        m = re.search(r"async function termToHtml\(sid, opts\)[\s\S]+?\n\}", CHAT)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertNotIn("attachTerminal(", seg)
        self.assertIn('if (!auto) alert("打不开:" + m);', seg)


class ProcessRendering(unittest.TestCase):
    def test_codex环境状态位于输入框上方(self):
        build = re.search(r"function hsBuild\(el, sid, title\)[\s\S]+?const v =", CHAT).group(0)
        self.assertLess(build.index('class="hs-env hs-col"'), build.index('class="hs-box hs-col"'))
        self.assertIn('type === "session.environment"', CHAT)
        self.assertIn('if (state === "ready")', CHAT)
        self.assertIn("hsEnvironmentSnapshot(v, cf.environment, cf.environment_revision)", CHAT)
        self.assertIn(".hs-page .hs-env.warning .hs-env-k { color: var(--ink-60); }", CSS)
        self.assertIn(".hs-page .hs-env.error .hs-env-k { color: #e08585; }", CSS)

    def test_codex环境状态用右箭头展开且可关闭(self):
        build = re.search(r"function hsBuild\(el, sid, title\)[\s\S]+?const v =", CHAT).group(0)
        self.assertIn('class="hs-env-chev" aria-label="展开详情">\' + HS_ICONS.chev', build)
        self.assertIn('class="hs-env-x" aria-label="关闭">×</button>', build)
        self.assertIn('querySelector(".hs-env-x").onclick', CHAT)
        self.assertIn("hsEnvironmentHide(v, true)", CHAT)
        self.assertIn("rotate(90deg)", CSS)
        self.assertIn(".hs-page .hs-env-chev svg", CSS)


    def test_回到前台立刻查一次记录(self):
        """在终端里聊天时 Desk 多半在后台,轮询整个不跑(document.hidden);
        切回来还要再等最多十秒——而「聊完切回 GUI 看」正是这条路最常见的用法。"""
        self.assertIn('document.addEventListener("visibilitychange"', CHAT)
        self.assertIn('window.addEventListener("focus"', CHAT)
        self.assertIn("function hsFollowTick(v, queueOnly = false)", CHAT)     # 轮询与回前台共用同一段判据
        m = re.search(r"function hsFollow\(v\)[\s\S]+?\n\}", CHAT)
        self.assertIn("hsFollowTick(v), 10000", m.group(0))


    def test_卡住的处理中能自己回来(self):
        """回合的收尾只经 SSE 发一遍,那两条(session.error / session.idle)一旦落在断线窗口里
        就永远收不到,页面停在「处理中」。而 hsFollowTick 原本「busy 就直接返回」,等于把唯一
        能纠正它的通道也关掉,只能靠人刷新(2026-08-10 实见:引擎以「会话正在别处使用」拒了
        这一回合,后端已收尾,界面还在转)。"""
        m = re.search(r"async function hsFollowTick\(v, queueOnly = false\)[\s\S]+?\n\}", CHAT)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn("if (v.busy) { hsBusyVerify(v); return; }", seg)
        self.assertNotIn("if (v.busy || document.hidden", seg)   # 原来的一刀切
        self.assertNotIn("document.hidden || (v.queue || []).length", seg)
        # 原来这后面还有一道「有队列就直接 return」,同样会把这条通道关掉。现在空闲时
        # 先问控制面，只有 ready 才发；busy 时仍旧只走对账。
        self.assertNotIn("if ((v.queue || []).length) return;", seg)
        self.assertIn("if (!v.busy && (v.queue || []).length) {", seg)
        self.assertIn("else await hsStateSync(v)", seg)
        r = re.search(r"async function hsBusyVerify\(v, opts\)[\s\S]+?\n\}", CHAT)
        self.assertTrue(r, "没有对账函数")
        rep = r.group(0)
        self.assertIn("HS_BUSY_QUIET_MS", rep)                   # 安静得反常时才问,不做常态轮询
        self.assertIn("HS_INTERRUPT_VERIFY_MS", CHAT)
        self.assertIn('/state`', rep)                             # 控制面状态是权威判据
        self.assertIn('hsApply(v, "session.state", state)', rep)
        self.assertIn('m.info || {}).running', rep)              # 旧后端的消息投影仅作兼容
        self.assertIn("hsBusy(v, false)", rep)
        self.assertIn("hsQFlush(v)", rep)
        self.assertIn('v.pulse = ""', rep)                       # 逼下一拍重建,把收尾内容接回来

    def test_翻过页也要能纠正处理中(self):
        """paged 拦的是「重建」,而核对只问一句控制面的状态,不动 DOM。两件事原来写在同一个
        return 里,于是往上翻过一次历史之后,这一页就再也纠正不了自己的「处理中」——手机上
        尤其容易踩到,那块屏幕本来就要翻(2026-08-25 用户实见:电脑那侧早已跑完,手机上还
        转着,而侧栏那个点已经不闪了——那一路走的是终端清单轮询,与这一页各走各的)。"""
        seg = re.search(r"async function hsFollowTick\(v, queueOnly = false\)[\s\S]+?\n\}", CHAT).group(0)
        self.assertNotIn("document.hidden || v.paged", seg, "核对又被翻页挡住了")
        self.assertLess(seg.index("hsBusyVerify(v)"), seg.index("if (v.paged) return;"),
                        "核对要排在 paged 那道守卫之前")

    def test_重建那一支自己认_paged(self):
        """核对提到 paged 之前以后,兜底那一支会真的重建,守卫得在它自己那儿补回来,
        否则翻出来的历史会被存储里最近那一段抹掉。"""
        rep = re.search(r"async function hsBusyVerify\(v, opts\)[\s\S]+?\n\}", CHAT).group(0)
        self.assertIn("if (!v.paged) hsReconcile(v);", rep)

    def test_等待态只认后端_stalled(self):
        self.assertNotIn("HS_SILENT_MS", CHAT)
        self.assertIn("const mute = v.stallSec || 0", CHAT)

    def test_相同投影不在回合末重建(self):
        self.assertIn("hsShapeSame(dbShape, domShape)", CHAT)
        self.assertIn("if (hsShapeSame(dbShape, domShape)) return", CHAT)
        self.assertIn("async function hsReconcileNow(v, opts)", CHAT)

    def test_回合展示收尾与队列解锁分属两个边界(self):
        settled = re.search(r"function hsTurnSettled\(v, p\)[\s\S]+?\n}", CHAT)
        ready = re.search(r"function hsTurnReady\(v, p\)[\s\S]+?\n}", CHAT)
        idle = re.search(r'if \(type === "session\.idle"\)[\s\S]+?'
                         r'if \(!p\.contract\) hsTurnReady\(v, p\);[\s\S]+?return;', CHAT)
        self.assertTrue(settled and ready and idle)
        self.assertIn("hsReconcile(v)", settled.group(0))
        self.assertNotIn("hsQFlush(v)", settled.group(0))
        self.assertIn("v.turnReady = true", ready.group(0))
        self.assertIn("else hsQFlush(v)", ready.group(0))
        self.assertIn("hsTurnSettled(v, p)", idle.group(0))
        self.assertIn("if (!p.contract) hsTurnReady(v, p)", idle.group(0))
        self.assertNotIn("hsDrainQueue", CHAT)
        self.assertNotIn("function hsQNext(v)", CHAT)

    def test_回答完成时强制收掉压缩估算条(self):
        """手机后台会漏掉 compact 收尾帧，idle/ready 仍须保证终态界面自洽。"""
        settled = re.search(r"function hsTurnSettled\(v, p\)[\s\S]+?\n}", CHAT)
        self.assertTrue(settled)
        seg = settled.group(0)
        self.assertIn("hsCompactOff(v)", seg)
        self.assertIn("v.cmpLive = false", seg)
        self.assertLess(seg.index("hsCompactOff(v)"), seg.index('hsStatus(v, "done"'))

    def test_异常终态也恢复过程分割线(self):
        """压缩 class 残留时,只要真实状态已不是工作中,分割线就该回来。"""
        self.assertIn(
            ".hs-page.hs-compacting:has(.hs-status.st-working .hs-cbar) .hs-rule { display: none; }",
            CSS)
        self.assertNotIn(".hs-page.hs-compacting .hs-rule { display: none; }", CSS)
        busy = re.search(r"function hsBusy\(v, on\)[\s\S]+?\n}", CHAT)
        self.assertTrue(busy)
        seg = busy.group(0)
        self.assertIn('v.el.classList.contains("hs-compacting")', seg)
        self.assertIn("hsCompactOff(v)", seg)
        self.assertIn("v.cmpLive = false", seg)

    def test_队列整体合并为一条消息且附件顺序保留(self):
        m = re.search(r"function hsQFlush\(v\)[\s\S]+?\n}", CHAT)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn('join("\\n\\n")', seg)
        self.assertIn("const atts = []", seg)
        self.assertIn("atts.push(...(q.atts || []))", seg)
        self.assertIn("hsDispatch(v, text, atts)", seg)

    def test_Esc不依赖输入框焦点(self):
        self.assertIn("function hsEscape(v, ev)", CHAT)
        self.assertIn('document.addEventListener("keydown", v.docKey, true)', CHAT)
        m = re.search(r"v\.docKey = \(ev\) => \{[\s\S]+?\n  \};", CHAT)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn('ev.key !== "Escape"', seg)
        self.assertIn("pActive && pActive.el === v.el", seg)
        self.assertIn("v.inp.contains(ev.target)", seg)
        self.assertIn("hsEscape(v, ev)", seg)

    def test_终端里的Esc不触发会话中断(self):
        m = re.search(r"v\.docKey = \(ev\) => \{[\s\S]+?\n  \};", CHAT)
        self.assertTrue(m)
        seg = m.group(0)
        guard = 'ev.target.closest(".xterm")'
        self.assertIn(guard, seg)
        self.assertLess(seg.index(guard), seg.index("hsEscape(v, ev)"))

    def test_Esc按约束不取回队列只作用当前回合(self):
        self.assertNotIn("function hsQRecall(v)", CHAT)
        m = re.search(r"async function hsInterrupt\(v, keepMessage = false\)[\s\S]+?\n}", CHAT)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertGreater(seg.index("} catch {"), seg.index("await api("))
        self.assertGreater(seg.index('hsQIssue(v, "state"'), seg.index("} catch {"))
        self.assertIn("const fresh = (v.caps || {}).recall !== false && !keepMessage && !!t && !t.parts.size && !(v.queue || []).length", seg)
        self.assertIn("if (fresh) v.noDrain = true", seg)
        self.assertIn("hsBusy(v, false)", seg)
        self.assertNotIn("hasQ", seg)
        self.assertNotIn("hsQRecall", seg)

    def test_非撤回Esc立即显示中断但不提前发队列(self):
        m = re.search(r"async function hsInterrupt\(v, keepMessage = false\)[\s\S]+?\n}", CHAT)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn("if (v.interrupted && v.interruptDrop) return true", seg)
        self.assertIn("if (!fresh) {", seg)
        self.assertLess(seg.find('hsStatus(v, "done", "已中断")'), seg.find("await api("))
        self.assertIn("if (!v.queuePaused && !v.busy && v.turnReady) hsQFlush(v)", seg)
        self.assertGreater(seg.index("hsQFlush(v)"), seg.index("await api("))
        self.assertIn('if (!fresh) hsStatus(v, "working", "处理中")', seg)
        self.assertIn("v.interruptAt = Date.now()", seg)
        self.assertIn("hsScheduleBusyVerify(v, HS_INTERRUPT_VERIFY_MS)", seg)

    def test_中断后队列等待后端ready再发出(self):
        self.assertIn("function hsScheduleBusyVerify(v, delay)", CHAT)
        m = re.search(r"async function hsBusyVerify\(v, opts\)[\s\S]+?\n\}", CHAT)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn("const interrupted = v.interrupted || v.interruptDrop", seg)
        self.assertIn("const wait = interrupted ? HS_INTERRUPT_VERIFY_MS : HS_BUSY_QUIET_MS", seg)
        self.assertIn("const HS_INTERRUPT_VERIFY_MS = 250", CHAT)
        self.assertIn('/state`', seg)
        self.assertIn('state.state !== "ready"', seg)
        ready = re.search(r"function hsTurnReady\(v, p\)[\s\S]+?\n}", CHAT).group(0)
        self.assertLess(ready.find("v.turnReady = true"), ready.find("hsQFlush(v)"))

    def test_撤回迟到idle不解除新回合忙碌(self):
        self.assertIn("v.recallIdle = (v.recallIdle || 0) + 1", CHAT)
        settled = re.search(r"function hsTurnSettled\(v, p\)[\s\S]+?\n}", CHAT)
        self.assertTrue(settled)
        seg = settled.group(0)
        # 2026-09-05:撤回从「计数」改成「按代认领」。撤回的那一轮未必等得到 ready
        # (按得够快时后端那一轮已经收尾,abort 找不到 live ctx,一帧状态都不发),
        # 只增不减的计数器会把后面某一轮真正的收尾吞掉,那一轮就停在「处理中」。
        self.assertIn("if (v.recallIdle && (!gen || !v.recallGen || gen === v.recallGen))", seg)
        self.assertIn("v.recallIdle = Math.max(0, (v.recallIdle || 0) - 1)", seg)
        self.assertIn("v.recallGen = 0", seg)
        self.assertIn("return;", seg)
        self.assertNotIn("hsQFlush(v)", seg)

    def test_中断终态即使无过程也保留在两轮之间(self):
        m = re.search(r"function hsStatus\(v, state, label(?:, \w+)?\)[\s\S]+?\n}", CHAT)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn('label !== "已中断"', seg)
        self.assertIn("hsProcTrim(lt)", seg)

    def test_新回合开始清理旧中断丢帧标志(self):
        m = re.search(r"async function hsDispatch\(v, text, atts\)[\s\S]+?hsStatusClear", CHAT)
        self.assertTrue(m)
        self.assertIn("v.interruptDrop = false", m.group(0))
        self.assertIn("v.turnGeneration = Number(v.turnGeneration || 0) + 1", m.group(0))
        self.assertNotRegex(m.group(0), r"v\.turnGeneration\s*=.*Date\.now\(\)")
        self.assertLess(m.group(0).find("v.turnGeneration = Number"),
                        m.group(0).find("v.turnReady = false"))

    def test_忙时发送按CC语义只入队(self):
        m = re.search(r"async function hsSend\(v\)[\s\S]+?\n}", CHAT)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn("if (v.busy || !v.turnReady || (v.queue || []).length) {", seg)
        self.assertIn("(v.queue = v.queue || []).push({ text, atts })", seg)
        self.assertNotIn("v.busy && v.interrupting", seg)
        self.assertNotIn("hsFill(v, text)", seg)

    def test_Esc后迟到的助手内容不再进入当前视图(self):
        # 撤回(fresh)那一支原来不设这个开关,理由是「还没出内容,没有迟到的帧可丢」。
        # 但打断未必立刻落地:常驻连接那类引擎在冷起窗口里按下的 Esc,引擎那侧仍在跑,跑完还会把过程
        # 灌回来——那条已经撤掉的回合于是重新转成「处理中」(2026-08-15 实见)。两支都要丢。
        self.assertIn("v.interruptDrop = true;", CHAT)
        self.assertIn('if (v.interruptDrop && info.role === "assistant") return;', CHAT)
        self.assertIn("if (v.interruptDrop) return;", CHAT)
        settled = re.search(r"function hsTurnSettled\(v, p\)[\s\S]+?\n}", CHAT)
        self.assertTrue(settled)
        seg = settled.group(0)
        self.assertIn("const wasInterrupted = v.interrupted || v.interruptDrop", seg)
        self.assertIn("hsReconcile(v)", seg)

    def test_后到part快照不覆盖已流式内容(self):
        self.assertIn("function hsMergePart(part, q)", CHAT)
        self.assertIn("const visible = oldText + oldBuf", CHAT)
        # 取整个分支:到下一个顶层 if (type === 为止。原来用第一个 hsScroll(v) 收尾,
        # 分支前段一插新代码(用户消息补气泡那一段)就提前截断,断在合并之前。
        i = CHAT.index('if (type === "message.part.updated")')
        j = CHAT.find('if (type === "', i + 10)
        seg = CHAT[i:j if j > i else len(CHAT)]
        self.assertIn("hsMergePart(part, q)", seg)
        self.assertNotIn("Object.assign(part, q)", seg)

    def test_工具完成分支也能读取工具摘要(self):
        m = re.search(r'if \(q\.type === "tool"\)[\s\S]+?hsTodoSync\(v, q\);', CHAT)
        self.assertTrue(m)
        self.assertLess(m.group(0).find("const o = hsToolObj(q)"),
                        m.group(0).find('if (st.status === "running")'))

    def test_重建使用离屏节点避免内容闪空(self):
        self.assertIn('const scratch = document.createElement("div")', CHAT)
        self.assertIn("real.replaceChildren(...scratch.childNodes)", CHAT)

    def test_历史耗时不被无数据的活状态覆盖(self):
        self.assertIn('status.state !== "done"', CHAT)

    def test_过程展开态跨刷新保存(self):
        self.assertIn("function hsProcOpenSave", CHAT)
        self.assertIn('localStorage.setItem("hs:proc:" + v.sid', CHAT)
        # 复用旧盒子的判据 2026-08-16 从 parentNode 换成 isConnected:整片重建之后旧盒子
        # 仍挂在脱离文档的旧节点上,parentNode 不为空,照它判会把内容画进看不见的盒子
        # (见 test_frontend_replay_reattach.py)。展开态的持久化本身没变。
        self.assertIn("t.procEl.isConnected", CHAT)

    def test_摘要中的文件路径单独使用紫色(self):
        self.assertIn("function hsToolTargetPaint", CHAT)
        self.assertIn('span.className = "hs-step-path"', CHAT)
        self.assertIn(".hs-step-a.txt .hs-step-path", CSS)

    def test_动作行机器标识符不随正文放大(self):
        rule = re.search(r"\.hs-page \.hs-step-a \{([^}]+)\}", CSS)
        self.assertTrue(rule)
        self.assertIn("font-family: var(--mono-action)", rule.group(1))
        self.assertIn("font-size: 13px", rule.group(1))
        self.assertNotIn("font-size: inherit", rule.group(1))

    def test_动作行机器标识符中的中文回到正文字体(self):
        self.assertIn("const HS_CJK_RUN", CHAT)
        self.assertIn("function hsMachineTextPaint", CHAT)
        self.assertIn('span.className = "hs-step-cjk"', CHAT)
        rule = re.search(r"\.hs-page \.hs-step-a \.hs-step-cjk \{([^}]+)\}", CSS)
        self.assertTrue(rule)
        self.assertIn("font-family: var(--font-family-sans)", rule.group(1))
        self.assertIn("font-size: var(--hs-step-font-size)", rule.group(1))

    def test_中英文件名实际拆成正文与等宽片段(self):
        pattern = re.search(r"const HS_CJK_RUN = .*?;", CHAT).group(0)
        painter = re.search(r"function hsMachineTextPaint\(el, text\) \{[\s\S]+?\n\}", CHAT).group(0)
        script = f"""
const assert = require("assert");
{pattern}
{painter}
function node(kind) {{
  return {{ kind, className: "", _text: "", children: [],
    set textContent(v) {{ this._text = String(v); this.children = []; }},
    get textContent() {{ return this.children.length
      ? this.children.map(x => x.textContent).join("") : this._text; }},
    appendChild(x) {{ this.children.push(x); }} }};
}}
global.document = {{ createTextNode: t => {{ const n = node("text"); n.textContent = t; return n; }},
                    createElement: t => node(t) }};
const root = node("root");
hsMachineTextPaint(root, {json.dumps("项目/训练脚本.py…")});
assert.equal(root.textContent, {json.dumps("项目/训练脚本.py…")});
assert.deepEqual(root.children.map(x => [x.className, x.textContent]), [
  ["hs-step-cjk", "项目"], ["", "/"], ["hs-step-cjk", "训练脚本"],
  ["", ".py"], ["hs-step-cjk", "…"]
]);
"""
        subprocess.run(["node", "-e", NODE_PRELUDE + script], check=True, capture_output=True, text=True, encoding='utf-8')

    def test_自然语言中的中文路径仍被识别(self):
        pattern = re.search(r"const HS_PATH_TOKEN = .*?;", CHAT).group(0)
        script = pattern + '\nconsole.log("读取 /项目/训练脚本.py".match(HS_PATH_TOKEN)?.[0] || "");'
        out = subprocess.run(["node", "-e", NODE_PRELUDE + script], check=True,
                             capture_output=True, text=True, encoding='utf-8').stdout.strip()
        self.assertEqual(out, "/项目/训练脚本.py")

    def test_MCP限定名整段使用标识符色(self):
        self.assertIn("const HS_QUALIFIED_ID", CHAT)
        self.assertIn("code: HS_QUALIFIED_ID.test(desc)", CHAT)

    def test_MCP调用不显示为调度(self):
        self.assertIn('mcp: "call"', CHAT)

    def test_过程盒子重建时接过旧内容(self):
        """盒子掉出文档时(重建走 replaceChildren)不能另起一个空的。

        每个步骤的 part.el 还指向旧 body,而 hsPaintTool 只在 part.el 为空时才新建挂载,
        丢掉旧盒子等于那些步骤永远回不来:盒子在、过程区空、点了没反应,而状态行仍能报出
        最后一个工具名(t.parts 还在)。刷新或 sync home 之后「等待中」展不开就是这么来的
        (2026-08-10 实见)。
        """
        m = re.search(r"function hsProcBox\(t\)[\s\S]+?\n\}", CHAT)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn("const old = t.procEl", seg)
        self.assertIn('take(".hs-proc-body")', seg)
        self.assertIn('take(".hs-proc-slot")', seg)
        # 搬过去之后 has-steps 要跟着对上,否则光标与点击判据仍按空盒子算
        self.assertIn('el.classList.add("has-steps")', seg)

    def test_过程行点了必有反应(self):
        """这一行只在有过程可看时才存在(空的由 hsProcTrim 撤掉),所以点它永远不该没反应。

        原来的写法是「过程区没有子节点就直接返回」。渲染一旦掉队——节点留在某个已经脱离
        文档的旧盒子里,而 hsPaintTool 只在 part.el 为空时才重建——这一行就成了死摆设:
        数据在 t.parts 里,状态行还能报出最后一个工具名,点下去却什么也不发生
        (2026-08-10 实见,切引擎重载之后必现;在隔离实例上人为清空过程区可复现)。
        """
        m = re.search(r"function hsProcBox\(t\)[\s\S]+?\n\}", CHAT)
        seg = m.group(0)
        self.assertIn("!body.children.length && !hsProcRepaint(t, body)", seg)
        self.assertNotIn("if (!el.querySelector(\".hs-proc-body\").children.length) return;", seg)
        r = re.search(r"function hsProcRepaint\(t, body\)[\s\S]+?\n\}", CHAT)
        self.assertTrue(r, "没有补画函数")
        rep = r.group(0)
        self.assertIn("q.el = null", rep)        # 旧节点已不在这儿,得让 hsPaint 重建
        self.assertIn("q.sig = null", rep)       # 签名去重会拦住重建
        self.assertIn('q.type !== "tool" && q.type !== "reasoning"', rep)   # 正文不属于过程区


    def test_重试状态显示次数与服务端等待(self):
        """Claude watchdog 会在同一请求里继续等 Retry-After，SDK 的 10 次普通预算不再是终点。
        普通模式仍可显示 N/M；watchdog 模式显示“第 N 次 + 剩余等待”，有内容即撤掉。"""
        self.assertIn('type === "session.retry"', CHAT)
        self.assertIn("连接不畅,等待重试", CHAT)
        self.assertIn("后重试", CHAT)
        m = re.search(r"function hsStatusPaint\(v\)[\s\S]+?\n\}", CHAT)
        seg = m.group(0)
        self.assertLess(seg.index("v.retry"), seg.index("const mute = v.stallSec"))
        self.assertIn("v.retry = null", CHAT)

    def test_流式增量只在阶段变化时刷新状态(self):
        """Pi 的思考增量可在几十毫秒内连续到达，不能按每个 token 重挂状态节点。"""
        i = CHAT.index('if (type === "message.part.delta")')
        j = CHAT.find('if (type === "', i + 10)
        seg = CHAT[i:j]
        self.assertIn('const phase = field === "reasoning" ? "正在思考" : "正在输出"', seg)
        self.assertIn('v.statusLabel !== phase', seg)
        self.assertIn('hsStatus(v, "working", phase)', seg)

    def test_运行状态文字和圆点保留原有呼吸(self):
        self.assertIn('.hs-status.st-working .hs-dot { animation: hs-breathe-text 1.4s', CSS)
        self.assertIn('.hs-status.st-warn .hs-dot { animation: hs-breathe-text 1.4s', CSS)
        self.assertRegex(CSS, r"\.hs-status\.st-working \.hs-st-t,\s*"
                             r"\.hs-page \.hs-status\.st-warn \.hs-st-t "
                             r"\{ animation: hs-breathe-text 1\.4s ease-in-out infinite;")

    def test_检索式不当路径涂色(self):
        """`查找 session/adopt|adopt_session|` 里的 session/adopt 被涂成了文件(2026-08-10 实见)。

        原来的判据是「凡带斜杠都算路径」。收紧成三条各带明确特征的分支,并且遇到正则元字符
        整段不涂——一句给人读的说明里不会出现它们。
        """
        self.assertIn("const HS_PATTERNISH", CHAT)
        self.assertIn("if (obj.code || HS_PATTERNISH.test(text))", CHAT)
        tok = re.search(r"const HS_PATH_TOKEN = /(.+)/g;", CHAT).group(1)
        self.assertTrue(tok.startswith("(?<![A-Za-z0-9_@.+-])"))   # 散文里的 /adopt 不算路径
        self.assertNotIn(r"(?:[A-Za-z0-9_@.+-]+\/)+[A-Za-z0-9_@.+-]+|", tok)   # 旧的「凡带斜杠」分支

    def test_codex结构化diff按增删行渲染(self):
        m = re.search(r"function hsDiffRows\(tool, input(?:, full)?\)[\s\S]+?\n}", CHAT)
        self.assertTrue(m)
        self.assertIn("input.changes", m.group(0))
        self.assertIn('line.startsWith("+")', m.group(0))
        self.assertIn('kind: "file"', m.group(0))

    def test_pi多段编辑进入统一审阅面板(self):
        m = re.search(r"function hsDiffRows\(tool, input(?:, full)?\)[\s\S]+?\n}", CHAT)
        self.assertTrue(m)
        segment = m.group(0)
        self.assertIn("Array.isArray(input.edits)", segment)
        self.assertIn("e.old_string", segment)
        self.assertIn("e.new_string", segment)
        self.assertIn("e.oldText", segment)
        self.assertIn("e.newText", segment)
        self.assertIn("input.path", segment)

        with open(os.path.join(FRONT, "app-chat-detail.js"), encoding="utf-8") as file:
            detail = file.read()
        review = re.search(r"if \(!host && hsRvIsEdit\(part\)\)[\s\S]+?rb\.onclick", detail)
        self.assertTrue(review)
        self.assertIn("inp0.path", review.group(0))

    def test_正文行内代码允许长命令换行(self):
        m = re.search(r"\.hs-page \.hs-text code \{[^}]+\}", CSS)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertNotIn("white-space: nowrap", seg)
        self.assertIn("overflow-wrap: anywhere", seg)
        self.assertIn("word-break: break-word", seg)
        self.assertIn(".hs-page .hs-text pre code { white-space: pre-wrap", CSS)

class WayIn(unittest.TestCase):
    """进 GUI 的门。此前只有一条:终端头部那枚钮走 adopt,而 adopt 只有 claude 有——
    切到别的引擎之后,界面上没有任何地方能开出一段 GUI 会话。"""

    def test_网页视图钮不再只对_claude_显示(self):
        m = re.search(r'const th = \$\("#term-tohtml"\);[\s\S]+?\n  \}', PANEL)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertNotIn("th.hidden = s.engine", seg)   # 原来:非 claude 一律藏起来
        self.assertIn("th.hidden = false", seg)
        # 能搬的搬这一段,不能搬的以 GUI 新开一段,两条路都留着。
        # 只钉调用本身,不钉参数:能不能搬现在按引擎自报的能力位判,搬迁请求要带上引擎,
        # 参数还会随之变。
        self.assertIn("termToHtml(target", seg)
        self.assertIn("newHtmlSession({ pref: true", seg)
        # 判据不再写死引擎名——加第四个引擎时这里应当零改动
        self.assertNotIn('s.engine === "claude"', seg)
        self.assertIn("adopt", seg)

    def test_新建会话只有_gui_一条路(self):
        """newTerminal 保留的是「在这个工作区开一段新会话」的语义,实现只剩 GUI。
        原来它先起 TUI、再靠 attachTerminal 自动 adopt,而新 Codex 还没有 thread id,
        adopt 必然失败并退回 TUI——那条路连同 TUI 一起没有了。"""
        m = re.search(r"async function newTerminal\(engine, domainId\)[\s\S]+?\n}", ASSETS)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn("newHtmlSession({ domain:", seg)
        self.assertNotIn("/workbench/terminals", seg)

    def test_Codex空终端可直接改建GUI(self):
        m = re.search(r'const th = \$\("#term-tohtml"\);[\s\S]+?\n  \}', PANEL)
        seg = m.group(0)
        self.assertIn("fresh && fresh.resume_id", seg)
        self.assertIn('method: "DELETE"', seg)
        self.assertIn("newHtmlSession({ pref: true, domain: s.domain })", seg)


class ToolTargetColor(unittest.TestCase):
    """工具卡的宾语分三档,配色各说各的事。

    淡紫在这套 UI 里表示「点得开、找得到」的对象。检索式不指向任何东西——
    `**/*writing*voice*/SKILL.md` 谁也打不开——着成路径色只会让人误读成「读了这个文件」,
    glob 又本来就长得像路径(2026-08-11 实见)。等宽保留(它是机器串),色退回正文灰。
    """

    def test_检索式单独标一档(self):
        m = re.search(r"if \(inp\.pattern\) return \{[^}]+\}", CHAT)
        self.assertTrue(m)
        self.assertIn("pat: true", m.group(0))
        self.assertIn("code: true", m.group(0))   # 仍是机器串,等宽不变

    def test_那一档只去色不去等宽(self):
        m = re.search(r"\.hs-page \.hs-step-a\.pat \{([^}]+)\}", CSS)
        self.assertTrue(m)
        self.assertIn("color:", m.group(1))
        self.assertNotIn("font-family", m.group(1))   # 等宽从 .hs-step-a 继承下来

    def test_渲染时按档位切类(self):
        fn = re.search(r"function hsToolTargetPaint\(el, obj, running\)[\s\S]+?\n}\n", CHAT)
        self.assertTrue(fn)
        self.assertIn('classList.toggle("pat"', fn.group(0))

    def test_site定位符与中文检索词使用各自字体和颜色(self):
        self.assertIn("const HS_WEB_QUERY_HEAD", CHAT)
        self.assertIn("webq: mixed", CHAT)
        self.assertIn('classList.toggle("webq"', CHAT)
        self.assertIn('span.className = "hs-step-locator"', CHAT)
        parent = re.search(r"\.hs-page \.hs-step-a\.txt\.webq \{([^}]+)\}", CSS)
        locator = re.search(r"\.hs-page \.hs-step-a\.txt\.webq \.hs-step-locator \{([^}]+)\}", CSS)
        self.assertTrue(parent and locator)
        self.assertIn("font-family: inherit", parent.group(1))
        self.assertNotIn("color:", parent.group(1))
        self.assertIn("font-family: var(--mono-action)", locator.group(1))
        self.assertIn("font-size: 13px", locator.group(1))
        self.assertIn("color: var(--accent)", locator.group(1))

    def test_site定位符与自然语言实际拆成两个节点(self):
        parts = []
        for name in ("HS_WEB_QUERY_HEAD", "HS_CJK_RUN", "HS_PATTERNISH", "HS_PATH_TOKEN"):
            parts.append(re.search(rf"const {name} = .*?;", CHAT).group(0))
        parts.append(re.search(
            r"function hsMachineTextPaint\(el, text\) \{[\s\S]+?\n\}", CHAT).group(0))
        parts.append(re.search(
            r"function hsToolTargetPaint\(el, obj, running\) \{[\s\S]+?\n\}", CHAT).group(0))
        script = "\n".join(parts) + r'''
const assert = require("assert");
function node(kind) {
  const classes = new Set();
  return {kind, className: "", _text: "", children: [],
    classList: {toggle(k, on) { if (on) classes.add(k); else classes.delete(k); }},
    set textContent(v) { this._text = String(v); this.children = []; },
    get textContent() { return this.children.length
      ? this.children.map(x => x.textContent).join("") : this._text; },
    appendChild(x) { this.children.push(x); }};
}
global.document = {createTextNode: t => { const n = node("text"); n.textContent = t; return n; },
                   createElement: t => node(t)};
const root = node("root");
hsToolTargetPaint(root, {t: "site:github.com 中文主题", webq: true, pat: true}, false);
assert.equal(root.textContent, "site:github.com 中文主题");
assert.equal(root.children.length, 2);
assert.equal(root.children[0].className, "hs-step-locator");
assert.equal(root.children[0].textContent, "site:github.com");
assert.equal(root.children[1].textContent, " 中文主题");
'''
        subprocess.run(["node", "-e", NODE_PRELUDE + script], check=True, capture_output=True, text=True, encoding='utf-8')


if __name__ == "__main__":
    unittest.main()

class SettledWaitsForUser(unittest.TestCase):
    """卡片等人回应时不做回合收尾(2026-09-02)。

    收尾会给回合挂上时间戳与复制/分支那一排,那是「已结束」的记号;而人一批准,回合
    还会继续吐字。后端那侧已经不在等人时收口,前端再守一道,两处独立成立。
    """

    def test_等待回应时不收尾(self):
        import re as _re
        src = _srcmap.chat_src()
        m = _re.search(r"\nfunction hsTurnSettled\(v, p\) \{(.*?)\n\}", src, _re.S)
        assert m, "hsTurnSettled 不见了"
        body = m.group(1)
        guard = body[:body.index("hsFinalize(v)")]
        self.assertIn("if (v.awaitAsk)", guard)      # 守卫必须在收尾之前
        self.assertIn("settledGeneration = gen", guard)   # 同一代不重复处理
