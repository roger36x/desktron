"""设置中心信息架构回归：真实配置节点切页，不复制一套假表单。"""
import os
import re
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
JS = open(os.path.join(ROOT, "frontend", "app-settings-core.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


class 设置路由(unittest.TestCase):
    def test_所有既有能力都有入口(self):
        for view in ("appearance", "engines", "browser", "intranet", "setup", "stats", "debug"):
            self.assertIn('"%s"' % view, re.search(r"const _SM_NAV_MODEL =[\s\S]+?\n\];", JS).group(0))

    def test_概览已移除且更新入口落在目录底部(self):
        nav_model = re.search(r"const _SM_NAV_MODEL = \[[\s\S]+?\n\];", JS).group(0)
        self.assertNotIn('id: "overview"', nav_model)
        self.assertNotIn("_buildSettingsOverview", JS)
        self.assertIn('update.className = "sm-nav-update"', JS)
        self.assertIn('id="smp-update-status"', JS)
        self.assertIn('class="sm-update-icon"', JS)
        self.assertIn('M12 3v11m-4-4 4 4 4-4', JS)
        self.assertNotIn('<span>检查更新</span>', JS)
        self.assertIn('#smp-update.has-update .sm-update-icon::after', CSS)

    def test_引擎列表把真实配置节点移入弹窗而不复制表单(self):
        for engine in ("claude", "codex", "pi"):
            self.assertIn('engine: "%s"' % engine, JS)
        self.assertIn('row.className = "sm-engine-entry"', JS)
        self.assertIn('modal.querySelector(".sm-engine-dialog-body").appendChild(section)', JS)
        self.assertIn("_openSettingsEngineDialog(modal)", JS)
        self.assertNotIn("_smActiveEngine", JS)

    def test_切页不再调用scrollIntoView(self):
        nav = re.search(r"function _renderSettingsNav\(\)[\s\S]+?\n}", JS).group(0)
        self.assertNotIn("scrollIntoView", nav)

    def test_AI引擎目录只显示标题(self):
        self.assertIn('id: "engines", title: "AI 引擎"', JS)
        nav = re.search(r"function _renderSettingsNav\(\)[\s\S]+?\n}", JS).group(0)
        self.assertNotIn("item.badge", nav)
        self.assertNotIn("<em>", nav)
        self.assertNotIn("item.icon", nav)
        self.assertNotIn("sm-nav-ic", nav)


class 响应式布局(unittest.TestCase):
    def test_关闭按钮共用尺寸且正文滚动区预留固定空间(self):
        close = re.search(r"#sm-close, \.resource-close \{([^}]*)\}", CSS).group(1)
        self.assertIn("width: 28px; height: 28px", close)
        self.assertIn("top: 6px; right: 8px", close)
        self.assertIn("#sm-close svg, .resource-close svg", CSS)
        content = re.search(r"\.settings-center-live \.sm-settings-content \{([^}]*)\}", CSS).group(1)
        self.assertIn("padding-top: 32px", content)
        self.assertIn("box-sizing: border-box", content)
        mobile = CSS[CSS.index("@media (max-width: 820px), (max-height: 520px)"):]
        self.assertIn(".settings-center-live .sm-settings-content { padding-top: 0; }", mobile)
        self.assertIn(".resource-close { display: none; }", mobile)
        self.assertIn("#sm-close { display: none; }", mobile)

    def test_关闭按钮聚焦只提亮不画外框(self):
        focus = re.search(r"#sm-close:focus-visible, \.resource-close:focus-visible \{([^}]*)\}", CSS).group(1)
        self.assertIn("outline: none", focus)
        self.assertIn("color: var(--text-strong)", focus)

    def test_资源页关闭按钮使用矢量图标而非文本乘号(self):
        with open(os.path.join(ROOT, "frontend", "app-rail-nav.js"), encoding="utf-8") as f:
            nav_js = f.read()
        self.assertIn('class="resource-close" aria-label="关闭" type="button"><svg', nav_js)
        self.assertNotIn('type="button">×</button>', nav_js)

    def test_宽屏使用固定上限的双栏子页面(self):
        self.assertIn("width: min(800px,calc(100% - 72px))", CSS)
        self.assertIn("height: min(620px,calc(100% - 88px))", CSS)
        self.assertIn("grid-template-columns: var(--sm-nav-width) minmax(0,1fr)", CSS)
        page = re.search(r"\.settings-center-live \.sm-page \{([^}]*)\}", CSS).group(1)
        self.assertIn("max-width: none", page)
        self.assertIn("width: min(744px,calc(100% - 40px))", CSS)
        self.assertIn("overflow-y: auto; overscroll-behavior: contain", CSS)

    def test_目录自身承担贯穿全高的单像素分隔线(self):
        nav = re.search(r"\.settings-center-live \.sm-inner-nav \{([^}]*)\}", CSS).group(1)
        self.assertIn("height: 100%", re.search(r"\.settings-center-live \.sm-page \{([^}]*)\}", CSS).group(1))
        self.assertIn("border-right: 1px solid var(--lift-055)", nav)
        self.assertNotIn(".settings-center-live .sm-page::before", CSS)

    def test_中等宽度仍让正文在剩余主区居中(self):
        medium = CSS[CSS.index("@media (max-width: 1180px) and (min-width: 821px)"):]
        self.assertIn("--sm-nav-width: clamp(168px,19vw,184px)", medium)
        self.assertIn("width: min(720px,calc(100% - 40px))", medium)

    def test_选中背景与设置标题共用左边缘(self):
        title = re.search(r"\.settings-center-live \.sm-nav-title \{([^}]*)\}", CSS).group(1)
        self.assertIn("margin: 0 10px 18px", title)

    def test_手机子页面铺满且目录收成顶部横向导航(self):
        mobile = CSS[CSS.index("@media (max-width: 820px), (max-height: 520px)"):]
        self.assertIn("#settings-modal { padding: 0; }", mobile)
        self.assertIn(".sm-panel { width: 100%; height: 100%; border: 0; border-radius: 0; }", mobile)
        self.assertIn("grid-template-rows: auto minmax(0,1fr)", mobile)
        self.assertIn("overflow-x: auto", mobile)
        self.assertIn("border-right: 0; border-bottom: 1px solid var(--lift-055)", mobile)
        self.assertIn(".settings-center-live .sm-nav-title { display: none; }", mobile)

    def test_引擎页使用单卡列表且窄屏收成三列(self):
        self.assertIn(".settings-center-live .sm-engine-list", CSS)
        engine_list = re.search(r"\.settings-center-live \.sm-engine-list \{([^}]*)\}", CSS).group(1)
        self.assertIn("box-shadow: none", engine_list)
        self.assertIn(".sm-engine-entry:not(:last-child)::after", CSS)
        self.assertIn("left: 62px; right: 14px", CSS)
        self.assertIn("grid-template-columns: 36px minmax(132px,1fr) minmax(104px,190px) 52px", CSS)
        mobile = CSS[CSS.rfind("@media (max-width: 480px)"):]
        self.assertIn("grid-template-columns: 32px minmax(0,1fr) 50px", mobile)

    def test_设置内二级弹窗高于设置子页面(self):
        self.assertIn("#settings-modal { position: fixed; inset: 0; z-index: 700", CSS)
        self.assertIn(".sm-engine-modal { z-index: 720; }", CSS)
        self.assertIn(".settings-dialog-open .auth-tui-back", CSS)


class 主题适配(unittest.TestCase):
    def test_设置选中态与工坊共用灰白层级(self):
        self.assertIn(".sm-inner-nav .sm-nav-row.active { background: var(--surface-active);", CSS)
        hover = re.search(r"\.settings-center-live \.sm-engine-entry:hover \{([^}]*)\}", CSS).group(1)
        self.assertIn("background: var(--lift-035)", hover)
        self.assertNotIn("rgba(112,78,157", hover)

    def test_设置选中态由主题变量自然适配深浅色(self):
        self.assertNotIn('html[data-theme="light"] .sm-inner-nav .sm-nav-row.active', CSS)
        self.assertNotIn('html[data-theme="light"] .settings-center-live .sm-engine-entry', CSS)
        self.assertIn('html[data-theme="light"] .setup-system-node.selected', CSS)


class 文案与层级(unittest.TestCase):
    def test_设置作为浮层打开且目录不显示偏好副标题(self):
        opened = re.search(r"function openSettings\(\) \{[\s\S]+?\n\}", JS).group(0)
        self.assertIn('const modal = $("#settings-modal")', opened)
        self.assertIn("modal.hidden = false", opened)
        self.assertNotIn('showView("asset")', opened)
        self.assertNotIn("navPush", opened)
        nav_model = re.search(r"const _SM_NAV_MODEL = \[[\s\S]+?\n\];", JS).group(0)
        self.assertNotIn('title: "偏好"', nav_model)

    def test_页内目录标题只显示设置文字(self):
        nav = re.search(r"function _renderSettingsNav\(\)[\s\S]+?\n}", JS).group(0)
        self.assertIn('title.textContent = "设置"', nav)
        self.assertNotIn('class="sm-nav-title-icon"', nav)
        self.assertNotIn('$("#settings-btn")', nav)
        self.assertNotIn("sm-head-icon", JS)
        self.assertNotIn(".settings-center-live .sm-nav-title-icon", CSS)

    def test_目录兼容隐藏旧版数量胶囊(self):
        self.assertIn(".settings-center-live .sm-inner-nav .sm-nav-row em { display: none !important; }", CSS)

    def test_目录选中项加长且文字左侧只突出六像素(self):
        row = re.search(r"\.sm-inner-nav \.sm-nav-row \{([^}]*)\}", CSS).group(1)
        self.assertIn("width: calc(100% - 8px)", row)
        self.assertIn("margin: 1px 4px", row)
        self.assertIn("padding: 6px", row)
        self.assertIn("max-width: 100%", row)
        self.assertIn("gap: 0", row)
        self.assertNotRegex(row, r"(?:^|[;\s])width:\s*100%")

    def test_连接页说明保持精简(self):
        self.assertIn('intranet: ["工作工具", "管理 lark-cli 的登录状态。"]', JS)
        self.assertIn('smSection("登录状态", "", ICONS.lock)', JS)
        self.assertNotIn("命令不含任何凭证", JS)

    def test_页头不再重复显示旧分类名(self):
        self.assertNotIn('sm-view-kicker', JS)

    def test_设置中心字阶不会让表单控件冒充标题(self):
        self.assertRegex(CSS, r"\.settings-center-live \.sm-view-head h1 \{[^}]*font-size: 28px")
        self.assertRegex(CSS, r"\.settings-center-live \.sm-section > \.sm-title \{[^}]*font-size: 16px")
        self.assertRegex(CSS, r"\.settings-center-live \.sm-input,[^}]*font-size: 13\.5px")
        self.assertRegex(CSS, r"\.settings-center-live \.sm-save,[^}]*font-size: 13px")

    def test_触屏窄屏输入框保留防缩放字号(self):
        self.assertRegex(
            CSS,
            r"@media \(max-width: 820px\) and \(pointer: coarse\) \{[^}]*\.settings-center-live \.sm-input,[^}]*font-size: 16px",
        )

    def test_引擎页主层只显示概览列表(self):
        finish = re.search(r"function _finishSettingsCenter\(host, settings\)[\s\S]+?\n}", JS).group(0)
        self.assertNotIn("<em>", finish)
        self.assertIn("可用引擎", finish)
        self.assertIn(".sm-engine-config > .sm-title, .sm-engine-config > .sm-hint { display: none; }", CSS)
        self.assertIn("订阅登录或 API key，二选一。", JS)

    def test_引擎列表按品牌画布留白校准图标(self):
        self.assertIn(".settings-center-live .sm-engine-entry-brand .brand-pi", CSS)
        self.assertIn("width: 23px; height: 23px", CSS)

    def test_引擎配置弹窗可由遮罩关闭并支持_escape(self):
        self.assertIn('modal.className = "nw-modal sm-engine-modal"', JS)
        self.assertIn('modal.querySelector(".nw-backdrop").onclick = _closeSettingsEngineDialog', JS)
        self.assertIn('event.key === "Escape"', JS)
        self.assertIn(".sm-engine-modal[hidden] { display: none; }", CSS)

    def test_常态页面不展示实现机制长文案(self):
        for text in (
            "走可访问性树、非像素点击",
            "部署脚本是幂等的",
            "payload 比对指纹",
            "命令不含任何凭证",
        ):
            self.assertNotIn(text, JS)


if __name__ == "__main__":
    unittest.main()
