"""会话页右侧信息卡的静态契约。"""
import os
import re
import unittest

import _srcmap


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
UI = _srcmap.ui_server_src()
RECAP = open(os.path.join(ROOT, "desktron", "lib", "engine_adapters", "recap.py"),
             encoding="utf-8").read()


class SessionSituationCard(unittest.TestCase):
    def test_四项平铺且无总标题与箭头(self):
        self.assertIn('class="hs-sit"', CHAT)
        for title in (">项目<", ">制品<", ">回顾<", ">变更<"):
            self.assertIn(title, CHAT)
        # 审批和项目同级，不再额外插入「会话信息」分组标题。
        self.assertNotIn('hs-sit-context-label', CHAT)
        self.assertNotIn("<h2>会话信息</h2>", CHAT)
        block = CHAT[CHAT.index("function hsSituationMarkup"):CHAT.index("function hsSituationAgo")]
        # 箭头只属于能折叠的分区:待审批(2026-09-04)与制品(2026-09-10 用户提,它会堆多)。
        # 项目、回顾、变更三区不折叠,也就不该有箭头——有箭头意味着可以按。
        rest = block[block.index("hs-sit-projects"):block.index("hs-sit-artifacts")] \
            + block[block.index("hs-sit-recap"):]
        self.assertNotIn("chev", rest)
        self.assertNotIn("svg", rest)
        self.assertEqual(block.count("appr-chev"), 2)

    def test_项目默认三条且可展开收起(self):
        self.assertIn("HS_SIT_PROJECT_LIMIT = 3", CHAT)
        self.assertIn("'sit.moreProjects'", CHAT)
        self.assertIn("projects.classList.toggle('open')", CHAT)

    def test_项目可从卡片添加(self):
        self.assertIn('class="hs-sit-project-add"', CHAT)
        self.assertIn("await _wfRepoPick(null, domain)", CHAT)
        self.assertIn("await hsSituationSync(v)", CHAT)

    def test_项目点击后在侧栏IDE打开(self):
        self.assertIn("function hsSituationOpenProject(project)", CHAT)
        self.assertIn("openFilesPage({ id, name }, false)", CHAT)
        self.assertIn("showAppSide(page)", CHAT)
        self.assertIn('data-project-index="', CHAT)

    def test_变更开既有审阅侧栏(self):
        self.assertNotIn("hs-sit-modal-card", CHAT)
        self.assertIn("hsRvOpen(v, row._turn, '')", CHAT)
        self.assertIn("row._turn._rvOpts = { title: '会话变更', session: true }", CHAT)

    def test_记忆已从这张卡上撤下(self):
        """2026-09-10 用户定:这张卡上换成「制品」。记忆本身没有取消——工作流页页底
        与设置里的记忆卡都还在,撤的是会话卡上这一处入口和它带的那张编辑浮层。"""
        for gone in ("hs-sit-memory", "hsSituationOpenMemory", "'/workbench/memory/edit'",
                     "hs-sit-private"):
            self.assertNotIn(gone, CHAT)
        self.assertNotIn("hs-sit-memory", CSS)

    def test_无变更也统一显示零增删(self):
        self.assertIn('<span class="hs-cadd">+0</span><span class="hs-cdel">-0</span>', CHAT)
        self.assertNotIn("stat.innerHTML = files.length", CHAT)
        self.assertIn("font-size: 11.5px", CSS)

    def test_recap_截断时悬停显示全文(self):
        self.assertIn("function hsSituationTipBind(el, text, truncatedOnly = false)", CHAT)
        self.assertIn("function hsSituationTruncatedTitle(el, text)", CHAT)
        self.assertIn("el.scrollHeight > el.clientHeight + 1", CHAT)
        self.assertNotIn("el.title = full", CHAT)
        self.assertIn("el.setAttribute('aria-label', full)", CHAT)
        self.assertIn("hsSituationTipShow(el, full)", CHAT)
        self.assertIn("className = 'hs-sit-tooltip'", CHAT)
        self.assertIn("const useAbove =", CHAT)
        self.assertIn(".hs-sit-tooltip {", CSS)
        self.assertIn("_hsSituationTip.style.width = 'max-content'", CHAT)
        self.assertIn("max-width: min(360px, calc(100vw - 24px))", CSS)
        self.assertNotIn("_hsSituationTip.style.width = Math.min(360", CHAT)
        tooltip = CSS.split(".hs-sit-tooltip {", 1)[1].split("}", 1)[0]
        self.assertIn("border-radius: var(--hover-float-radius)", tooltip)
        self.assertIn("hsSituationTruncatedTitle(recapEl, recapText)", CHAT)

    def test_信息卡操作提示与回顾复用同一浮层(self):
        self.assertNotIn('title="在 IDE 中打开"', CHAT)
        self.assertNotIn('title="添加项目"', CHAT)
        self.assertIn("hsSituationTipBind(row, DeskI18n.t('sit.openInIde'))", CHAT)
        self.assertIn("hsSituationTipBind(projectAdd, DeskI18n.t('sit.addProject'))", CHAT)
        # 制品行的提示是它自己的说明或路径:标题被截断时这一句就是全文
        self.assertIn("hsSituationTipBind(row, item && item.note ? item.note : "
                      "(item ? item.ref : ''))", CHAT)

    def test_边框与分隔均为一像素(self):
        self.assertIn("--hs-sit-line: 1px", CSS)
        self.assertIn("--hs-sit-line: .5px", CSS)  # Retina 上一枚物理像素
        # 原来这里钉的是「仅自己」那枚胶囊的描边;它随「记忆」一起撤下(2026-09-10),
        # 改钉卡片自己的外框——同一条口径,而且它是这张卡上唯一的外框。
        self.assertIn("border: var(--hs-sit-line) solid var(--border-weak-base)", CSS)
        self.assertIn("border-bottom: var(--hs-sit-line) solid var(--border-weak-base)", CSS)
        self.assertIn("left: 18px; right: 18px; bottom: 0", CSS)

    def test_深色信息栏采用无阴影提亮炭灰且文本色仅限卡片(self):
        dark = re.search(r'html:not\(\[data-theme="light"\]\) \.hs-sit \{([^}]+)\}', CSS).group(1)
        self.assertIn("background-color: #1e1e1e", dark)
        self.assertIn("rgba(80,80,80,.045)", dark)
        self.assertIn("box-shadow: none", dark)
        self.assertNotIn("repeating-linear-gradient", dark)
        self.assertNotIn("inset", dark)
        for token, color in (("89", "#dedfe0"), ("66", "#cacbce"),
                             ("55", "#cacbce"), ("42", "#b2b5b9")):
            self.assertIn(f"--ink-{token}: {color}", dark)
        # 辅助文本使用独立 token，不提亮审批按钮描边或审批高亮内圈。
        self.assertIn("--hs-sit-muted-text: #b2b5b9", dark)
        self.assertNotIn("--ink-35:", dark)
        self.assertNotIn("--ink-48:", dark)
        # 不修改外框及内部细线的主题 token。
        self.assertNotIn("--border-weak-base:", dark)
        self.assertNotIn("border:", dark)

    def test_毛玻璃能力检测与浅色主题保持独立(self):
        start = CSS.index('@supports ((-webkit-backdrop-filter: blur(1px)) or (backdrop-filter: blur(1px)))',
                          CSS.index('/* 深色信息栏：'))
        support = CSS[start:CSS.index('@media (min-resolution: 2dppx)', start)]
        self.assertIn('html:not([data-theme="light"]) .hs-sit', support)
        self.assertIn("background-color: rgba(32,32,32,.72)", support)
        self.assertIn("-webkit-backdrop-filter: blur(20px) saturate(.7)", support)
        self.assertIn("backdrop-filter: blur(20px) saturate(.7)", support)
        light = re.search(r'html\[data-theme="light"\] \.hs-sit \{([^}]+)\}', CSS).group(1)
        self.assertIn("background-color: #fafaf9", light)
        self.assertNotIn("backdrop-filter", light)
        self.assertNotIn("--ink-", light)

    def test_卡片按主区宽度响应并避让程序坞(self):
        self.assertIn("container-name: hs-chat", CSS)
        self.assertIn("right: 48px", CSS)
        self.assertIn("body.dock-open .hs-sit { right: 92px; }", CSS)
        self.assertIn("width: clamp(280px, calc(16.6667cqw + 70px), 320px)", CSS)
        self.assertIn("--hs-pad-r: clamp(404px, calc(16.6667cqw + 194px), 444px)", CSS)
        self.assertNotIn("@container hs-chat (min-width: 1261px) and (max-width: 1500px)", CSS)
        self.assertIn("@container hs-chat (max-width: 1260px)", CSS)
        self.assertIn(".hs-page:not(.rv-on) .hs-wrap,", CSS)
        self.assertIn("--hs-pad-r: 444px", CSS)
        self.assertNotIn("body.dock-open .hs-page:not(.rv-on) .hs-body", CSS)

    def test_空间足够时正文与卡片空白按黄金分割(self):
        golden = CSS[CSS.index("@container hs-chat (min-width: 1261px)") :]
        self.assertIn("--hs-col-ml: calc(61.8% - 454.8px)", golden)
        self.assertIn("--hs-col-mr: calc(38.2% - 305.2px)", golden)
        self.assertNotIn("@container hs-chat (min-width: 1650px)", CSS)

    def test_小版面默认隐藏但可由页头打开同一信息栏(self):
        self.assertNotIn("if (!hsSituationEnabled()) return ''", CHAT)
        self.assertIn('class="hs-sit-scrim"', CHAT)
        self.assertIn("function hsSituationToggleActive()", CHAT)
        self.assertIn("function hsSituationCornerState()", CHAT)
        self.assertIn("(hover: none) and (pointer: coarse)", CHAT)
        self.assertIn("navigator.maxTouchPoints", CHAT)
        self.assertIn('classList.toggle("hs-sit-disabled", !hsSituationEnabled())', CHAT)
        mobile = CSS[CSS.index("@media (hover: none) and (pointer: coarse)") :]
        self.assertIn(".hs-sit { --hs-sit-inline: 0; display: none !important; }", mobile)
        self.assertIn(".hs-page.hs-sit-open:not(.rv-on) .hs-sit", CSS)
        self.assertIn("display: block !important", CSS)
        self.assertIn(".hs-page:not(.rv-on) .hs-composer { --hs-pad-r: var(--hs-mobile-pad-r, 18px)", mobile)
        self.assertIn(".hs-page.hs-sit-disabled:not(.rv-on) .hs-composer {", CSS)
        self.assertEqual(CSS.count("--hs-pad-r: var(--hs-mobile-pad-r, 18px)"), 2)

    def test_窄窗浮层与宽屏共用无阴影材质且遮罩不压暗背景(self):
        overlay = re.search(r'\.hs-page\.hs-sit-open:not\(\.rv-on\) \.hs-sit \{([^}]+)\}', CSS).group(1)
        scrim = re.search(r'\.hs-page\.hs-sit-open:not\(\.rv-on\) \.hs-sit-scrim \{([^}]+)\}', CSS).group(1)
        self.assertIn("box-shadow: none", overlay)
        self.assertNotIn("background", overlay)
        self.assertNotIn("backdrop-filter", overlay)
        self.assertIn("background: transparent", scrim)
        self.assertIn("z-index: 10", scrim)
        self.assertIn("z-index: 11", overlay)
        self.assertIn("el.querySelector('.hs-sit-scrim').onclick", CHAT)
        self.assertIn("el.classList.remove('hs-sit-open')", CHAT)

    def test_手机浮层与审批列表分别按可视高度限高(self):
        block = CSS[CSS.index("/* 手机信息浮层不能按桌面列表高度铺满整屏。"):]
        block = block[:block.index("/* 右上通知:")]
        self.assertIn("(max-width: 820px), (hover: none) and (pointer: coarse)", block)
        self.assertIn(".hs-page.hs-sit-open:not(.rv-on) .hs-sit", block)
        self.assertIn("min(560px, calc(var(--vvh, 100dvh) * .7)", block)
        self.assertIn("calc(100% - 24px - env(safe-area-inset-bottom))", block)
        self.assertIn("overflow-y: auto; overscroll-behavior: contain", block)
        self.assertIn("scrollbar-width: thin", block)
        self.assertIn(".hs-sit-approval-host:not(:empty)", block)
        self.assertIn("min(200px, calc(var(--vvh, 100dvh) * .28))", block)

    def test_旧后端期间项目走现有接口回退(self):
        """前端资源可热更新,后端接口要等进程重启。跨版本那一小段里卡片不能整块失效。
        记忆那一路随卡片一起撤了,制品另有自己的接口(见 test_frontend_situation_artifacts)。"""
        self.assertIn("const sessions = await api('/session'", CHAT)
        self.assertIn("'/repos'", CHAT)
        self.assertIn("data = { domain: workspace, projects, recap: null }", CHAT)

    def test_会话接口按作用域返回数据(self):
        self.assertIn('seg[3] == "situation"', UI)
        self.assertIn("_repo_items(workspace)", UI)
        self.assertIn("_git_branch_for_cwd", UI)
        self.assertNotIn("from term_titles import _git_branch_for_cwd", UI)
        self.assertIn("_artifacts.list_items(workspace)", UI)
        self.assertIn('(ad.index.get(sid) or {}).get("recap")', UI)

    def test_recap_保存到当前会话索引(self):
        self.assertIn('row["recap"] = {"text": text', RECAP)
        self.assertIn("self._save_index()", RECAP)


if __name__ == "__main__":
    unittest.main()
