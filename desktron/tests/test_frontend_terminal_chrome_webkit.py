"""终端应用与登录/配置终端共用的外壳（Terminal Chrome）。

2026-09-21 起两处终端不再各画一套壳：窗口标题条 40px，下面一条会话标签条；弹框用同一组
色阶，保留右上角关闭与底部「完成」。不复制 macOS 左上角红黄绿。

2026-09-23 撤回当时的 iTerm 石墨配色（#303033 页头、直角标签、活动标签与正文连成一体）：
放进右栏后与浏览器、文件应用读成两套界面（用户实见）。现在页头不另涂色，标签条 38px、
与正文同底，标签改为与浏览器应用同形的圆角胶囊。标题条 40px 与两层结构保留。
这里先守 CSS 契约，再用真实 WebKit 在桌面与窄屏两种尺寸量几何。
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "frontend/style.css").read_text(encoding="utf-8")
SETTINGS = (ROOT / "frontend/app-settings-sections.js").read_text(encoding="utf-8")
PAGES = (ROOT / "frontend/app-pages.js").read_text(encoding="utf-8")

CLOSE_SVG = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
             '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>')
TERM_SVG = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">'
            '<rect x="3" y="4" width="18" height="16" rx="3"/></svg>')


def _tab(title, active):
    return ('<div class="ps-item tm-tab%s">' % (" active" if active else "")
            + '<span class="ps-ic">' + TERM_SVG + '</span>'
            + '<span class="ps-title">' + title + '</span>'
            + '<span class="ps-x" title="关闭">' + CLOSE_SVG + '</span></div>')


TERMINAL_APP = (
    '<div id="overlay" class="terminal-page"><header id="overlay-head">'
    '<span id="overlay-title"><span class="tm-head">'
    '<span class="tm-window-title">alice@server — ~/work/desktron</span></span></span>'
    '<span class="head-actions">'
    '<button id="ov-min" type="button">−</button>'
    '<button id="ov-close" type="button">×</button></span></header>'
    '<div class="overlay-content terminal-host"><div class="terminal-app">'
    '<div class="tm-tabbar"><span class="tm-tabs">'
    + _tab("~/work/desktron", True) + _tab("ssh server", False) + _tab("pytest", False)
    + '<button class="tm-new" type="button">+</button></span></div>'
    '<div class="tm-view"><div class="tm-pane"><div class="xterm"></div></div></div>'
    '</div></div></div>'
)

AUTH_TUI = (
    '<div class="auth-tui-back"><div class="auth-tui-panel auth-tui-terminal" role="dialog">'
    '<div class="auth-tui-head"><div class="auth-tui-copy"><div class="auth-tui-title">'
    '<strong>登录 Claude</strong></div></div>'
    '<div class="auth-tui-controls">'
    '<button class="auth-tui-x" type="button">×</button></div></div>'
    '<div class="auth-tui-stage"><div class="auth-tui-host"><div class="xterm"></div></div></div>'
    '<div class="auth-tui-foot"><button class="auth-tui-done" type="button">完成并返回</button>'
    '</div></div></div>'
)

SCRIPT = r'''
  const rect = el => el.getBoundingClientRect();
  const bg = el => getComputedStyle(el).backgroundColor;
  // 标签有 .12s 的 background 过渡（.ps-item）：切主题后取色可能读到过渡中间值，量出来
  // 还是上一套配色。这里量的是终态几何与配色，直接把过渡关掉，不靠等待撞运气。
  const freeze = document.createElement('style');
  freeze.textContent = '*,*::before,*::after{transition:none!important;animation:none!important}';
  document.head.appendChild(freeze);
  const settle = () => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)));
  window.collectTerminalChrome = async () => {
    const results = [];
    for (const theme of ['dark', 'light']) {
      document.documentElement.dataset.theme = theme;
      await settle();
      const head = document.querySelector('#overlay-head');
      const title = document.querySelector('.tm-window-title');
      const tabbar = document.querySelector('.tm-tabbar');
      const tabs = [...document.querySelectorAll('.tm-tab')];
      const active = document.querySelector('.tm-tab.active');
      const view = document.querySelector('.tm-view');
      const host = document.querySelector('.terminal-host');
      const panel = document.querySelector('.auth-tui-terminal');
      const pHead = panel.querySelector('.auth-tui-head');
      const pCopy = panel.querySelector('.auth-tui-copy');
      const pStage = panel.querySelector('.auth-tui-stage');
      const pFoot = panel.querySelector('.auth-tui-foot');
      const pClose = panel.querySelector('.auth-tui-x');
      const terminalHeadBg = bg(head);
      const terminalHeadBorder = parseFloat(getComputedStyle(head).borderBottomWidth);
      // 模拟终端收起并回到普通会话：terminal-host 作为隐藏的缓存页仍留在 overlay 内，
      // 但当前页状态类已撤掉，顶栏必须恢复普通 chrome，不能被后代节点污染。
      document.querySelector('.terminal-host').style.display = 'none';
      document.querySelector('#overlay').classList.remove('terminal-page');
      await settle();
      const normalProbe = document.createElement('i');
      normalProbe.style.cssText = 'position:absolute;background:var(--ink-9)';
      document.body.appendChild(normalProbe);
      const collapsedHeadBg = bg(head);
      const collapsedHeadBorder = parseFloat(getComputedStyle(head).borderBottomWidth);
      const normalHeadBg = bg(normalProbe);
      normalProbe.remove();
      document.querySelector('#overlay').classList.add('terminal-page');
      document.querySelector('.terminal-host').style.display = '';
      await settle();
      results.push({
        terminalHeadBg,
        terminalHeadBorder,
        collapsedHeadBg,
        collapsedHeadBorder,
        tabbarBorder: parseFloat(getComputedStyle(tabbar).borderBottomWidth),
        normalHeadBg,
        theme,
        pageWidth: document.documentElement.scrollWidth,
        viewportWidth: window.innerWidth,
        headHeight: rect(head).height,
        tabbarHeight: rect(tabbar).height,
        titleLines: title.getClientRects().length,
        titleInsideHead: rect(title).left >= rect(head).left && rect(title).right <= rect(head).right + 1,
        headPadLeft: parseFloat(getComputedStyle(head).paddingLeft),
        visibleTabs: tabs.filter(t => rect(t).width > 0).length,
        activeVisible: rect(active).width > 0,
        activeBg: bg(active),
        tabbarBg: bg(tabbar),
        viewBg: bg(view),
        headBg: bg(head),
        tabJoinsView: Math.abs(rect(active).bottom - rect(view).top),
        paneFillsHost: Math.abs(rect(document.querySelector('.tm-pane')).bottom - rect(host).bottom),
        iconHidden: getComputedStyle(active.querySelector('.ps-ic')).display === 'none',
        closeOnLeft: rect(active.querySelector('.ps-x')).left < rect(active.querySelector('.ps-title')).left,
        tabsFit: tabbar.scrollWidth <= tabbar.clientWidth + 1,
        activeInsideViewport: rect(active).left >= 0 && rect(active).right <= window.innerWidth + 1,
        panelHeadHeight: rect(pHead).height,
        panelTitleInside: rect(pCopy).left >= rect(pHead).left
                          && rect(pCopy).right <= rect(pClose).left,
        panelNoDiagnostics: !panel.querySelector('details, .auth-tui-connection, .auth-tui-foot p'),
        panelHostBg: bg(panel.querySelector('.auth-tui-host')),
        panelStageInset: rect(pStage).left - rect(panel).left,
        panelStageBg: bg(pStage),
        panelHeadBg: bg(pHead),
        panelFootHeight: rect(pFoot).height,
        panelCloseRight: rect(panel).right - rect(pClose).right,
        panelCloseAboveTitle: rect(pClose).left > rect(pCopy).left,
        panelFits: rect(panel).left >= 0 && rect(panel).right <= window.innerWidth + 1,
        trafficLights: document.querySelectorAll('.traffic-light, .auth-tui-lights').length,
      });
    }
    window.webkit.messageHandlers.result.postMessage({results});
  };
'''


class 样式契约(unittest.TestCase):
    def test_共用一组terminal_chrome色阶(self):
        for token in ("--terminal-chrome:", "--terminal-chrome-raised:", "--terminal-tabbar:",
                      "--terminal-divider:", "--terminal-label:", "--terminal-label-muted:"):
            self.assertEqual(CSS.count(token), 2, token)   # 深浅各一份

    def test_弹框终端与终端应用同源(self):
        self.assertIn(".auth-tui-terminal { width:", CSS)
        self.assertIn("auth-tui-panel auth-tui-terminal", SETTINGS)
        # 只有真正承载 xterm 的弹框吃这套壳，普通设置向导仍是 auth-tui-panel。
        self.assertNotIn('className = "auth-tui-panel auth-tui-terminal"\n', SETTINGS.replace(" ", ""))

    def test_不复制macos交通灯(self):
        self.assertNotIn("traffic-light", CSS)
        self.assertNotIn("auth-tui-lights", SETTINGS)

    def test_弹框去掉了任务卡片式眉标(self):
        self.assertNotIn("auth-tui-kicker", SETTINGS)

    def test_窗口标题条与标签条高度固定(self):
        self.assertIn("#overlay.terminal-page #overlay-head,\n", CSS)
        # 终端页收起后节点仍缓存于 overlay-body；不能以 :has 后代判断当前页，否则污染会话顶栏。
        self.assertNotIn("#overlay:has(.terminal-host)", CSS)
        self.assertIn('overlay.classList.toggle("terminal-page", !!p.terminal);', PAGES)
        self.assertIn('ov.classList.remove("app-page", "terminal-page");', PAGES)
        # 左内距不写进这条：窄屏 116px 让位与侧栏收起时的 118px 都要继续生效。
        self.assertNotIn("#overlay.terminal-page #overlay-head { height: 40px; padding-left", CSS)
        self.assertIn(".tm-tabbar { flex: 0 0 38px; min-height: 38px;", CSS)
        self.assertIn(".auth-tui-terminal .auth-tui-head { position: relative; min-height: 40px; height: 40px;", CSS)

    def test_仅去掉终端标题与标签之间的分隔线(self):
        self.assertIn('#app-side-2:has(.terminal-host) #app-side-head-2 { height: 40px; border-bottom: 0; }', CSS)
        self.assertIn('background: var(--terminal-tabbar); border-bottom: 1px solid var(--terminal-divider);', CSS)
        # 普通应用页头仍保留分隔线；终端规则不修改共用选择器。
        head = CSS[CSS.index('#overlay-head {'):CSS.index('#overlay-body {')]
        self.assertIn('border-bottom: 1px solid var(--border-weak-base);', head)

    def test_弹框保留原有层级只加深正文(self):
        block = CSS[CSS.index(".auth-tui-terminal .auth-tui-head {"):CSS.index("@media (max-width: 600px) {\n  .auth-tui-back")]
        self.assertIn("background: var(--ink-9); border-bottom-color: var(--border-weak-base);", block)
        self.assertIn("background: var(--terminal-background);", block)
        self.assertIn(".auth-tui-terminal .auth-tui-stage { margin: 0; border: 0;", block)
        self.assertIn(".auth-tui-terminal .auth-tui-host { padding: 12px 16px 8px; }", block)
        self.assertIn("--terminal-background: #111111;", CSS)
        self.assertIn('html[data-theme="light"] .auth-tui-terminal { --terminal-background: #eeeeee; }', CSS)
        self.assertNotIn("auth-tui-details", SETTINGS)
        self.assertNotIn("auth-tui-connection", SETTINGS)

    def test_标签条与正文同底_活动标签为胶囊(self):
        self.assertIn("--terminal-tabbar: #191919;", CSS)
        self.assertIn("--terminal-background: #191919;", CSS)
        self.assertIn(".tm-tab.active { background: var(--lift-06);", CSS)
        self.assertIn(".tm-view { flex: 1; min-height: 0; position: relative; background: var(--terminal-background); }", CSS)


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc")
                    or os.environ.get("DESK_WEBKIT_TESTS") != "1", reason="需启用 WebKit 验收")
@pytest.mark.parametrize("width,height", [(1280, 800), (390, 844)])
@pytest.mark.desktop
def test_原生webkit终端外壳几何(tmp_path, width, height):
    html = ('<!doctype html><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>html,body{margin:0;height:100%%}#overlay{height:%dpx}</style>'
            '<style>%s</style>%s%s<script>%s</script>'
            % (height, CSS, TERMINAL_APP, AUTH_TUI, SCRIPT))
    page = tmp_path / ("terminal-chrome-%d.html" % width)
    page.write_text(html, encoding="utf-8")
    binary = tmp_path / "terminal-chrome"
    fixture = Path(__file__).parent / "fixtures/terminal_chrome_webkit.swift"
    subprocess.run(["swiftc", str(fixture), "-o", str(binary)], capture_output=True, text=True,
                   check=True, timeout=120)
    result = subprocess.run([str(binary), str(page), str(width), str(height)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert len(data["results"]) == 2, data
    narrow = width <= 820
    for layout in data["results"]:
        assert layout["pageWidth"] <= width + 1, layout
        # 两层壳：窗口标题条 40、会话标签条 38，谁也不吃掉终端的行数。
        # 标题与标签之间不再有边框，窄屏 content-box 也保持 40px。
        assert layout["headHeight"] == 40, layout
        assert layout["terminalHeadBorder"] == 0, layout
        assert layout["collapsedHeadBorder"] == 1, layout
        assert layout["tabbarBorder"] == 1, layout
        assert layout["tabbarHeight"] == 38, layout
        assert layout["titleLines"] == 1 and layout["titleInsideHead"], layout
        # 窄屏左上角三枚 fixed 钮的让位不能被终端配色规则吃掉。
        assert layout["headPadLeft"] == (116 if narrow else 14), layout
        # 标签条与正文同底连成一片，活动标签靠胶囊高亮区分。
        assert layout["tabbarBg"] == layout["viewBg"], layout
        assert layout["activeBg"] != layout["tabbarBg"], layout
        # 页头不再另涂终端色：与普通顶栏同色，收起后也不残留。
        assert layout["terminalHeadBg"] == layout["headBg"], layout
        assert layout["terminalHeadBg"] == layout["normalHeadBg"], layout
        assert layout["collapsedHeadBg"] == layout["normalHeadBg"], layout
        assert layout["paneFillsHost"] <= 1, layout
        assert layout["iconHidden"] and layout["closeOnLeft"], layout
        # 窄屏标签只收窄不隐藏：它是终端应用内部唯一的会话切换入口。
        assert layout["visibleTabs"] == 3, layout
        assert layout["activeVisible"], layout
        assert layout["activeInsideViewport"], layout
        if not narrow:
            assert layout["tabsFit"], layout
        # 弹框保留原有居中标题，不展示诊断；正文独立加深，画布宿主同底。
        assert layout["panelHeadHeight"] == 40, layout
        assert layout["panelTitleInside"], layout
        assert layout["panelNoDiagnostics"], layout
        assert layout["panelStageBg"] == layout["panelHostBg"], layout
        assert layout["panelStageBg"] == ('rgb(17, 17, 17)' if layout['theme'] == 'dark' else 'rgb(238, 238, 238)'), layout
        assert layout["panelHeadBg"] == layout["normalHeadBg"], layout
        assert layout["panelFootHeight"] >= 40, layout
        assert 0 <= layout["panelCloseRight"] <= 14, layout
        assert layout["panelCloseAboveTitle"], layout
        assert layout["panelFits"], layout
        assert layout["trafficLights"] == 0, layout
        # 正文贴边铺满，只隔 1px 窗框；窄屏全屏无窗框。撤回 09-21 的 7px 内缩与两侧竖线。
        assert layout["panelStageInset"] == (0 if narrow else 1), layout


if __name__ == "__main__":
    unittest.main()
