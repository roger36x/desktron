"""子代理那一层与主会话共用同一列宽。

主会话正文是 760px 居中的一列(.hs-stream);子代理面板原来是整幅宽加 84px 左右内距。
窗口不大时两者看着差不多,全屏之后差别就出来了:主会话仍是居中的一列,而这一层摊到整幅
屏宽,同一段会话的两处读起来像两个应用(2026-08-19 用户实见,两张全屏截图对照)。

分隔线不跟着收:它是页头与正文的分界,不是那一列的边框。所以居中的是头里那一层
(.hs-agv-hin),头本身仍横贯整幅。

无头浏览器实测(窗口 1720px):主会话 480~1240、子代理正文块 480~1240、头内层 480~1240,
分隔线 0~1720。

2026-09-09 续:那次只做到「同样是 760px 的一列」,居中方式仍是各写各的 margin: 0 auto。
此后正文列不再居中——有信息卡时按黄金分割偏左(--hs-col-ml / --hs-col-mr),
于是全屏后子代理比正文与输入框右移 141px(1680×1000 实测:正文列 443–1203,
子代理 584–1344,用户实见)。改成两处共用同一组变量,四档宽度复测全部重合:

    1680×1000   正文 443–1203   输入框 443–1203   子代理 443–1203
    1400×900    正文 444–1204   输入框 444–1204   子代理 444–1204
    1100×800    正文 332–1016   输入框 332–1016   子代理 332–1016
     820×700    正文  34–794    子代理  34–794

820px 那档输入框是 26–802:窄屏下 .hs-box 显式 max-width: none,让输入框占满屏宽,
是那条断点自己的选择(见 style.css「手机上屏幕本身就是上限」),不属于这里说的对齐。

下面 ColumnTrackIsGlobal 把这件事变成强约束:凡是收进 --hs-col 的会话内容,
左右外距只能取 --hs-col-ml / --hs-col-mr,写 auto 就是又开一个漂移口子。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
INIT = open(os.path.join(ROOT, "frontend", "app-init.js"), encoding="utf-8").read()
CONTRACT = open(os.path.join(ROOT, "desktron", "lib", "ui_contract.py"), encoding="utf-8").read()

# 列宽在 CSS 里是一个 token(--hs-col,定义在 .overlay-content.hs-page 上)。
# 这里比对 token 而不是那个数:主会话、子代理、输入框那一列全按它排,
# 谁改回字面量谁就在给漂移留口子(见 test_frontend_composer_align)。
COL = "var(--hs-col)"


def rule(sel):
    m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", CSS)
    assert m, "没有这条规则:" + sel
    return m.group(1)


class ColumnTrackIsGlobal(unittest.TestCase):
    """会话内容列的位置只有一处定义,谁都不许另起炉灶。

    这条约束是用户 2026-09-09 提的:「我觉得这个得做成全局强约束(会话内容和会话框对齐)」。
    背景是同一个错犯了两次——子代理那一层先在 2026-08-19 漏掉了列宽,改成 760px 居中之后,
    正文列又因为信息卡改成了偏左的黄金分割,它再一次落单。两次都是「另写一份居中」造成的。

    所以规则定死:凡是把自己收进 --hs-col 的会话内容,左右外距只能取 --hs-col-ml /
    --hs-col-mr。写 auto、写死像素、只写一边,都在这里失败。轨道本身
    (.hs-page .hs-col)与那些整幅宽的容器不在约束内——它们根本不声明 --hs-col。
    """

    #: --hs-col 的持有者。前三个是 .hs-col 类本身与它的两处等价写法,其余一律要过检。
    TRACK_VARS = ("var(--hs-col-ml)", "var(--hs-col-mr)")

    def _column_rules(self):
        """返回 [(选择器, 声明)],覆盖每一条声明了 max-width: var(--hs-col) 的规则。"""
        out = []
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
            sel, decl = m.group(1).strip(), m.group(2)
            if "max-width: " + COL in decl:
                out.append((" ".join(sel.split()), decl))
        return out

    def test_at_least_the_known_columns_are_covered(self):
        # 约束靠扫描生效,扫不到东西的话它就是一条空规则,先确认样本在。
        sels = [sel for sel, _ in self._column_rules()]
        self.assertGreaterEqual(len(sels), 3, sels)
        joined = " | ".join(sels)
        self.assertIn(".hs-agv-hin", joined)
        self.assertIn(".hs-agv-turn", joined)

    def test_every_column_uses_the_shared_offsets(self):
        for sel, decl in self._column_rules():
            with self.subTest(sel=sel):
                # .hs-env 用的是 margin 简写(margin: 0 var(--hs-col-mr) 5px var(--hs-col-ml)),
                # 同样把两个变量都写进来了;所以只查变量在不在,不查具体属性名。
                for var in self.TRACK_VARS:
                    self.assertIn(var, decl, f"{sel} 没有用 {var}")
                self.assertNotIn("margin: 0 auto", decl, sel)
                self.assertNotIn("margin-left: auto", decl, sel)
                self.assertNotIn("margin-right: auto", decl, sel)


class SameColumnAsMain(unittest.TestCase):
    def test_主会话那一列的宽度(self):
        self.assertIn("max-width: " + COL, rule(".hs-page .hs-col"))
        self.assertIn('class="hs-body"><div class="hs-stream hs-col"', CHAT)

    def test_子代理正文收进同一列(self):
        body = rule(".hs-page .hs-agv-turn")
        self.assertIn("max-width: " + COL, body)
        self.assertIn("margin-left: var(--hs-col-ml)", body)
        self.assertIn("margin-right: var(--hs-col-mr)", body)

    def test_子代理不再另定正文和工具排版(self):
        """切进只读页仍应使用主线的字号、颜色和段距。"""
        self.assertNotIn(".hs-page .hs-agv-p {", CSS)
        self.assertNotIn(".hs-page .hs-agv-r {", CSS)
        self.assertNotIn(".hs-page .hs-agv-s {", CSS)

    def test_头里那一层居中_头本身仍横贯(self):
        self.assertIn("max-width: " + COL, rule(".hs-page .hs-agv-hin"))
        hin = rule(".hs-page .hs-agv-hin")
        self.assertIn("margin-left: var(--hs-col-ml)", hin)
        self.assertIn("margin-right: var(--hs-col-mr)", hin)
        h = rule(".hs-page .hs-agv-h")
        self.assertNotIn("max-width", h)          # 分隔线要横贯整幅
        self.assertIn("border-bottom", h)
        self.assertNotIn("84px", h)               # 原来那个左右内距已经撤了
        # 内距同样取自轨道变量:写死 24px 时,窄屏那档(26/18px)和宽屏那档都对不上正文。
        for block in (h, rule(".hs-page .hs-agv-b")):
            self.assertIn("var(--hs-pad-l)", block)
            self.assertIn("var(--hs-pad-r)", block)

    def test_头的结构里确实多了一层(self):
        seg = re.search(r"async function hsAgentOpen\(v, a\)[\s\S]+?hs-agv-b\"></div>';", CHAT).group(0)
        self.assertIn('<div class="hs-agv-h"><div class="hs-agv-hin">', seg)
        self.assertIn("</div></div>", seg)

    def test_返回主线保留主线文字但不用文字箭头(self):
        seg = re.search(r"async function hsAgentOpen\(v, a\)[\s\S]+?hs-agv-b\"></div>';", CHAT).group(0)
        self.assertIn('DeskI18n.t("agent.backToMain")', seg)
        self.assertIn('class="ui-arrow-icon is-left"', seg)
        self.assertIn('hsEsc(DeskI18n.t("ctx.main"))', seg)
        self.assertNotIn('←', seg)

    def test_子代理滚动条不占列宽(self):
        """宽屏轨道含百分比外距；原生滚动条占宽会让它按更窄盒子重算并整体左移。"""
        self.assertRegex(CSS, r"\.hs-body,\s*\.hs-agv-b(?:,\s*[^{}]+)?\s*\{\s*scrollbar-width:\s*none;\s*\}")
        self.assertRegex(CSS, r"\.hs-body::-webkit-scrollbar,\s*\.hs-agv-b::-webkit-scrollbar(?:,\s*[^{}]+)?\s*\{\s*display:\s*none;\s*\}")
        sel = re.search(r"\(function autoScrollbars\(\) \{\s*const SEL = \[([\s\S]*?)\]\.join", INIT)
        self.assertIsNotNone(sel)
        self.assertIn('".hs-body", ".hs-agv-b"', sel.group(1))

    def test_工具摘要复用主会话详情渲染器(self):
        """接口已经返回完整 part；子代理页不能再把它降成不可展开的一行纯文本。"""
        seg = re.search(r"async function hsAgentOpen\(v, a\)[\s\S]+?\n}\n\nfunction hsAgentClose", CHAT).group(0)
        self.assertIn("hsPaintTool(v, part, proc.body)", seg)
        # 2026-08-24 多了第二个参数(视图):展开时要按 v.sid 去取被截断的工具输出全文
        self.assertIn("hsStepDetailMount(part, v)", seg)
        self.assertNotIn('chev.className = "hs-chev"', seg)
        self.assertIn("v.agentExpanded.add(key)", seg)
        self.assertIn("if (follow) hsSetScrollBottom(body)", seg)
        self.assertIn("else body.scrollTop = oldTop", seg)
        self.assertNotIn("d.textContent = hsToolVerb", seg)

    def test_正文和用户输入复用主线结构(self):
        seg = re.search(r"async function hsAgentOpen\(v, a\)[\s\S]+?\n}\n\nfunction hsAgentClose", CHAT).group(0)
        self.assertIn('className = "hs-text md hs-agv-p"', seg)
        self.assertIn('<div class="hs-bubble"><div class="hs-u-body md">', seg)

    def test_初始prompt提升到过程输出之前(self):
        seg = re.search(r"async function hsAgentOpen\(v, a\)[\s\S]+?\n}\n\nfunction hsAgentClose", CHAT).group(0)
        self.assertLess(seg.index("hsAgentPromptTurn(body, prompt.text)"),
                        seg.index("for (const [mi, m] of (msgs || []).entries())"))
        self.assertIn("prompt && prompt.mi === mi && prompt.pi === pi", seg)
        self.assertIn("if (!parts.length) continue", seg)
        self.assertIn("hsAgentPromptEcho(q.text || \"\", prompt)", seg)

    def test_子代理输出留在执行区底部(self):
        seg = re.search(r"function hsAgentProcess\(v, turn, key\)[\s\S]+?\n}\n\nfunction hsAgentResult[\s\S]+?\n}", CHAT).group(0)
        self.assertIn('hs-agv-result-rule', seg)
        self.assertIn('hs-agv-result', seg)
        self.assertIn('proc.result.appendChild(node)', seg)
        open_seg = re.search(r"async function hsAgentOpen\(v, a\)[\s\S]+?\n}\n\nfunction hsAgentClose", CHAT).group(0)
        self.assertIn('hsAgentResult(proc, d)', open_seg)
        self.assertIn('.hs-page .hs-agv-result', CSS)
        self.assertIn('.hs-page .hs-agv-result-rule', CSS)

    def test_移动端请求和渲染都限量(self):
        seg = re.search(r"async function hsAgentOpen\(v, a\)[\s\S]+?\n}\n\nfunction hsAgentClose", CHAT).group(0)
        self.assertIn('tail_bytes=262144&limit=60', seg)
        self.assertIn('totalProcessParts - 60', seg)
        self.assertIn('移动端仅展示最近 60 条处理过程', seg)
        self.assertIn('if (mobileLog && processSeen++ < processSkip) continue', seg)

    def test_同一子代理旧响应不能覆盖新帧(self):
        seg = re.search(r"async function hsAgentOpen\(v, a\)[\s\S]+?\n}\n\nfunction hsAgentClose", CHAT).group(0)
        self.assertIn("const requestSeq = (v.agentLogSeq || 0) + 1", seg)
        self.assertIn("v.agentOpen !== a.id || v.agentLogSeq !== requestSeq", seg)
        close = re.search(r"function hsAgentClose\(v\) \{[\s\S]+?\n}", CHAT).group(0)
        self.assertIn("v.agentLogSeq = (v.agentLogSeq || 0) + 1", close)

    def test_Shepherd公开子代理ID在路由层解码且不吞实现异常(self):
        self.assertIn("agent_id = urllib.parse.unquote(seg[4])", CONTRACT)
        route = CONTRACT[CONTRACT.index('seg[3] == "agents"'):]
        route = route[:route.index('seg[3] == "pulse"')]
        self.assertNotIn("except TypeError", route)

    def test_初始prompt优先识别Task信封并复用来信气泡(self):
        helpers = re.search(r"function hsAgentPrompt\(msgs, agent\)[\s\S]+?\n}\n\nfunction hsAgentPromptEcho[\s\S]+?\n}\n\nfunction hsAgentPromptTurn[\s\S]+?\n}", CHAT).group(0)
        self.assertIn(r"/^\s*Task\s*[:：]/i", helpers)
        self.assertIn("agent.task", helpers)
        self.assertIn("hsAgentTaskText", helpers)
        self.assertIn('className = "hs-u hs-u-peer hs-agv-prompt"', helpers)
        self.assertNotIn('hs-agv-prompt-head', helpers)
        self.assertIn(".hs-page .hs-agv-prompt .hs-bubble", CSS)

    def test_思考和工具合并进主线过程区(self):
        helpers = re.search(r"function hsAgentProcess\(v, turn, key\)[\s\S]+?\n}\n\nfunction hsAgentResult", CHAT).group(0)
        seg = re.search(r"async function hsAgentOpen\(v, a\)[\s\S]+?\n}\n\nfunction hsAgentClose", CHAT).group(0)
        self.assertIn('className = "hs-proc hs-agv-proc has-body has-steps"', helpers)
        self.assertIn('className = "hs-think hs-agv-r"', seg)
        self.assertIn('class="hs-think-body md"', seg)
        self.assertIn("md.render(q.text)", seg)
        self.assertNotIn("d.textContent = q.text", seg)

    def test_主工具渲染器允许只读外部容器(self):
        seg = re.search(r"function hsPaintTool\(v, part, host\)[\s\S]+?\n}\n", CHAT).group(0)
        self.assertIn("const target = host || hsProcHost(part.turn)", seg)
        self.assertIn("if (host) return", seg)


if __name__ == "__main__":
    unittest.main()


def _narrow_blocks():
    """窄屏媒体查询的块体。括号要配平地找块尾——按下一个 `}` 截会在第一条规则处断开。"""
    out, i = [], 0
    while True:
        i = CSS.find("@media", i)
        if i < 0:
            return out
        head_end = CSS.index("{", i)
        if "max-width: 820px" not in CSS[i:head_end]:
            i = head_end + 1
            continue
        depth, j = 1, head_end + 1
        while depth and j < len(CSS):
            if CSS[j] == "{":
                depth += 1
            elif CSS[j] == "}":
                depth -= 1
            j += 1
        out.append(CSS[head_end + 1:j - 1])
        i = j


class NarrowScreenDoesNotPan(unittest.TestCase):
    """子代理只读页在手机上不该能左右拖。

    它与主会话是两套容器:长串兜底那条只盖 .hs-turn,这一页的正文(.hs-agv-p /
    .hs-agv-r / .hs-agv-s)不在它下面,路径与标识符照旧把整页撑出去。而 .hs-agv-b
    只写了 overflow-y: auto,按规范另一轴从 visible 计算成 auto——撑出去就能拖
    (2026-08-25 手机实见)。
    """

    def setUp(self):
        self.narrow = "\n".join(_narrow_blocks())

    def test_正文允许在任意字符处断(self):
        self.assertIn(".hs-page .hs-agv-b, .hs-page .hs-agv-b * { overflow-wrap: anywhere; }",
                      self.narrow)

    def test_路径片窄屏放开断行(self):
        """桌面上路径不断行更好读,390px 上一条绝对路径就能把整段推出屏幕。"""
        self.assertIn(".hs-page .hs-agv-b code.hs-path { white-space: normal; }", self.narrow)

    def test_容器那一轴关掉(self):
        """只让内容断行还不够:真有断不开的东西时,容器仍会变成可拖的。"""
        self.assertIn(".hs-page .hs-agv-b { overflow-x: hidden; }", self.narrow)

    def test_代码块仍旧自己横滚(self):
        """关的是页面这一层,不是代码块。它们各自带 overflow-x: auto,该横滚的照旧。"""
        self.assertIn("overflow-x: auto", rule(".hs-page .hs-text pre"))
        self.assertIn("overflow-x: auto", rule(".md pre.hljs"))


class StatusLineKeepsTheDot(unittest.TestCase):
    """状态行的圆点不该被文字挤到单独一行。

    .hs-status 是可换行的 flex(.hs-st-next 要占一整行),而弹性盒先按「本来多宽」
    决定放不放得下、放不下就整项换行,之后才谈收缩。文字那一项原来基准是内容宽度
    (max-width: 100%),一旦比「整行减去圆点和间距」还长就整项被挤到下一行,圆点
    孤零零留在上面——手机上一条子代理返回的标题就够长(2026-08-25 用户实见)。
    """

    def test_文字项的基准宽度取零(self):
        r = rule(".hs-page .hs-status .hs-st-t")
        self.assertIn("flex: 1 1 0", r)
        self.assertNotIn("max-width: 100%", r, "基准仍按内容宽算,照旧会整项换行")

    def test_省略号那套还在(self):
        """基准取 0 之后靠 min-width: 0 才收得住,否则 nowrap 的文字会把行撑开。"""
        r = rule(".hs-page .hs-status .hs-st-t")
        for want in ("min-width: 0", "overflow: hidden", "text-overflow: ellipsis", "white-space: nowrap"):
            self.assertIn(want, r)

    def test_下一项仍占整行(self):
        """它是预告不是现状,本来就该另起一行——别把这条一起改掉。"""
        self.assertIn("flex: 1 0 100%", rule(".hs-page .hs-st-next"))


class CompactProgressKeepsPercentageTogether(unittest.TestCase):
    """窄屏可以收阶段文案,但不能把百分比压成逐字符换行。"""

    def test_进度条紧跟压缩文案(self):
        r = rule(".hs-page .hs-status:has(.hs-cbar) .hs-st-t")
        self.assertIn("flex: 0 1 auto", r)

    def test_进度条作为整体不收缩(self):
        self.assertIn("flex: 0 0 auto", rule(".hs-page .hs-cbar"))

    def test_百分比不收缩也不换行(self):
        r = rule(".hs-page .hs-cbar-n")
        self.assertIn("flex: 0 0 auto", r)
        self.assertIn("white-space: nowrap", r)
