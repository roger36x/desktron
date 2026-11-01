"""设置页在手机宽度下的适配。

根子是一条从未生效的规则。那几行的 class 是 "sm-prov-head it-login-row",而
`.sm-card .sm-prov-head { display: flex }` 有两个类、特指度高过只写一个类的
`.it-login-row { display: grid }`——三列共享列线的效果其实是 flex 加 margin-left:auto
凑出来的假象。桌面宽度下看不出,390px 上列被挤窄,五枚按钮里的字折成两行、钮撑到两倍高
(2026-08-24 用户实见)。

三处一起改才闭合:
  1. 选择器带上 .sm-card,让那条 grid 真的生效;
  2. grid 里把 margin-left 清零——自动外距的优先级高于 justify-self,stretch 会被它吃掉,
     钮退回内容宽、各行左缘参差(实测 1029 / 1057 两档);
  3. .sm-link-btn 一律 nowrap:钮的宽度由外面的布局决定,被挤窄时宁可顶出来被发现,
     也不要闷声折行变形。

手机断点单列一套:390px 下三列放不开(名称那列只剩 76px,装不下较长的工具名
与说明),改成名称独占一行、状态与按钮排下一行、按钮贴右。

无头浏览器实测:
    390px  折行按钮 5 → 0,无横向溢出
    1300px 五行按钮宽度全 104、左缘全 1013(改前 88/60/60/88/88,左缘两档)
"""
import os
import re
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
CORE = open(os.path.join(ROOT, "frontend", "app-settings-core.js"), encoding="utf-8").read()

MOBILE_Q = '@media (max-width: 820px), (max-height: 520px) and (pointer: coarse)'


def rule(sel):
    hits = re.findall(r"\n%s \{[^}]*\}" % re.escape(sel), CSS)
    assert hits, "找不到 %s" % sel
    return hits[-1]


class 三列布局真的生效(unittest.TestCase):
    def test_选择器压得过sm_prov_head(self):
        """只写 .it-login-row 的话,会被 .sm-card .sm-prov-head 的 display:flex 盖掉。"""
        self.assertIn("display: grid", rule(".sm-card .it-login-row"))
        base = re.search(r"\n\.it-login-row \{[^}]*\}", CSS)
        self.assertIsNone(base, "还留着那条特指度不够的旧规则")

    def test_按钮清掉自动外距(self):
        """grid 里自动外距优先于 justify-self,不清零 stretch 就不生效。"""
        r = rule(".sm-card .it-login-row .sm-link-btn")
        self.assertIn("justify-self: stretch", r)
        self.assertIn("margin-left: 0", r)


class 按钮不折行(unittest.TestCase):
    def test_link_btn一律nowrap(self):
        self.assertIn("white-space: nowrap", rule(".sm-link-btn"))


class 手机断点(unittest.TestCase):
    def _mobile_block(self):
        i = CSS.index(".sm-card .it-login-row {")
        seg = CSS[i:i + 1400]
        j = seg.find(MOBILE_Q)
        self.assertGreater(j, -1, "三列规则后面没有跟手机断点")
        return seg[j:seg.index("}\n}", j) + 3]

    def test_窄屏改成换行的一列(self):
        b = self._mobile_block()
        self.assertIn("display: flex", b)
        self.assertIn("flex-wrap: wrap", b)

    def test_名称独占一行(self):
        self.assertIn("flex: 1 1 100%", self._mobile_block())

    def test_按钮仍贴右(self):
        self.assertIn("margin-left: auto", self._mobile_block())

    def test_断点与全站其余手机规则同一个(self):
        """全站手机规则都用这一条 media query,新开一个不同的阈值会出现两套断点。"""
        self.assertGreaterEqual(CSS.count(MOBILE_Q), 6)


class 手机远程访问页(unittest.TestCase):
    def test_顶部保留原有横向分类栏(self):
        self.assertNotIn('mobilePicker.className = "sm-mobile-picker"', CORE)
        mobile = CSS[CSS.index(MOBILE_Q):]
        self.assertIn('overflow-x: auto', mobile)
        self.assertIn('min-width: max-content', mobile)

@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc")
                    or os.environ.get("DESK_WEBKIT_TESTS") != "1", reason="需启用 WebKit 验收")
@pytest.mark.desktop
def test_390px原生webkit远程页保持紧凑(tmp_path):
    rows = ''.join(
        '<div class="sm-prov-head it-login-row">'
        '<span class="it-name-cell"><span class="pv-name">%s</span></span>'
        '<span class="pv-state off">%s</span>%s</div>' % (name, state, button)
        for name, state, button in (
            ('Remote Control', '已连接', '<button class="sm-link-btn">重新部署</button>'),
            ('SSH 备用连接', '已关闭', '<button class="sm-link-btn">启动备用连接</button>'),
            ('飞书通知', '待发送', ''),
        )
    )
    nav_rows = ''.join('<button class="sm-nav-row"><span class="ai-title">%s</span></button>' % title
                       for title in ('外观与行为', 'AI 引擎', '浏览器操控', '内网工具', '远程访问', '首次设置'))
    script = '''
      const rect = el => el.getBoundingClientRect();
      window.collectSettingsLayout = () => {
        const results = [];
        for (const theme of ['dark', 'light']) {
          document.documentElement.dataset.theme = theme;
          const nav = document.querySelector('.sm-inner-nav');
          const list = [...document.querySelectorAll('.it-login-row')];
          const diag = document.querySelector('.sm-remote-diagnostics');
          results.push({
            theme,
            pageWidth: document.documentElement.scrollWidth,
            navWidth: nav.scrollWidth,
            navClient: nav.clientWidth,
            visibleTabs: [...document.querySelectorAll('.sm-nav-row')].every(el => getComputedStyle(el).display !== 'none'),
            navScrollable: nav.scrollWidth > nav.clientWidth,
            overflowX: getComputedStyle(nav).overflowX,
            titleHidden: getComputedStyle(document.querySelector('.sm-remote-section > .sm-title')).display === 'none',
            rowHeights: list.map(el => rect(el).height),
            rowsInside: list.every(el => rect(el).left >= 0 && rect(el).right <= 390),
            diagBelow: rect(diag).top >= rect(list[list.length - 1]).bottom,
          });
        }
        window.webkit.messageHandlers.result.postMessage({results});
      };
    '''
    html = '''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>%s</style>
      <div id="settings-modal"><div class="sm-panel"><div class="settings-center-live"><div class="sm-page">
        <nav id="sm-nav" class="sm-inner-nav"><button id="sm-back"><span>返回</span></button>
          %s<div class="sm-nav-update"><button id="smp-update"><span class="sm-update-icon"></span><small id="smp-update-status">开发版</small></button></div>
        </nav><section class="sm-settings-content"><div id="sm-settings-scroll"><div id="sm-host">
          <header class="sm-view-head"><div><h1>远程访问</h1><p></p></div></header>
          <div class="sm-section sm-remote-section" data-settings-view="remote"><div class="sm-title">Remote Control</div>
            <div class="sm-card"><button class="sm-link-btn sm-remote-diagnostics">导出部署诊断</button>%s</div>
          </div></div></div></section>
      </div></div></div></div><script>%s</script>''' % (CSS, nav_rows, rows, script)
    page = tmp_path / "settings-mobile.html"
    page.write_text(html, encoding="utf-8")
    binary = tmp_path / "settings-mobile"
    fixture = Path(__file__).parent / "fixtures/settings_mobile_webkit.swift"
    subprocess.run(["swiftc", str(fixture), "-o", str(binary)], capture_output=True, text=True,
                   check=True, timeout=90)
    result = subprocess.run([str(binary), str(page)], capture_output=True, text=True, timeout=50)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "error" not in data, data
    for layout in data["results"]:
        assert layout["pageWidth"] <= 390, layout
        assert layout["navScrollable"] and layout["overflowX"] == "auto", layout
        assert layout["visibleTabs"], layout
        assert layout["titleHidden"] and layout["rowsInside"], layout
        assert all(height <= 60 for height in layout["rowHeights"]), layout
        assert layout["diagBelow"], layout


if __name__ == "__main__":
    unittest.main()
