"""主区页统一下压一档:页名不再贴着窗口顶。

2026-09-05 用户看安排 / 应用 / 工坊三页:「不要这么占据在顶部,整体高度下来一点」。

WebKit 探针实测(1728×1026 窗口),改之前页名距窗口顶只有 60 / 70 / 64px,而下方
空着 568 / 166 / 430px——整块被顶在上沿。改后统一到 120 / 130 / 124px。

同一轮先试过另一条路并已撤回:把这几页的居中基准从主区改成窗口(往左让出半个侧栏)。
量是量准了——三页在主区里左右留白严格相等,中线恒定落在窗口中线右侧、偏移量正好是
侧栏宽的一半——但用户看过实物后说要的不是横向对齐,是纵向。那一版整段回退,不留残余。

两处上沿各自的来路不同,所以是两处加同一个量,不是一处:
  · 安排与工作流页:#asset-view .md.wf-page 的 padding-top;
  · 应用:.store-host 的 padding-top(.apps-page 是它里面的居中列)。

下压量挂 min(60px, 6vh):矮窗口按视口收,700 高时只压 42px(实测页名落在 102/112/106),
不至于把本来就不多的可视高度再吃掉一截。手机档整档归零,那边顶部另有自己的让位。
"""
import json
import os
import re
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


class 下压量(unittest.TestCase):
    def test_只有一个旋钮(self):
        """三页共用一个数。各调各的迟早调出三种高度,那正是改之前的样子(60/70/64)。"""
        self.assertIn(":root { --page-drop: min(60px, 6vh); }", CSS)

    def test_矮窗口按视口收(self):
        """写死 60px 的话,700 高的窗口要拿掉近一成可视高度去做留白。"""
        m = re.search(r"--page-drop:\s*min\(([^)]+)\)", CSS)
        self.assertIsNotNone(m)
        self.assertIn("vh", m.group(1))

    def test_手机档归零(self):
        """手机顶部另有状态栏与浮钮的让位,再叠一层就是双份。
        写 :root 不写 body —— :root 是伪类(0,1,0),body 是类型选择器(0,0,1),压不过。"""
        mob = re.search(r"@media \(max-width: 820px\)[\s\S]{0,1200}", CSS).group(0)
        self.assertIn(":root { --page-drop: 0px; }", mob)


class 两处上沿都吃这个量(unittest.TestCase):
    def test_安排与工作流页(self):
        self.assertRegex(CSS, r"#asset-view \.md\.wf-page \{[^}]*padding: calc\(40px \+ var\(--page-drop\)\)")

    def test_应用(self):
        """.apps-page 是 .store-host 里的居中列,上沿由外层给。"""
        self.assertIn(".store-host { padding: calc(24px + var(--page-drop))", CSS)


class 横向那一版确已撤净(unittest.TestCase):
    """撤回的判断和结论一样重要:留下半截会与这一版打架(两个方向同时挪)。"""

    def test_没有残余的让位变量(self):
        for name in ("--page-nudge", "--page-col", "--page-gap-min"):
            self.assertNotIn(name, CSS, f"{name} 是横向那一版的东西,应当已随整段回退")

    def test_几处居中仍是原样(self):
        self.assertIn(".apps-page { max-width: 680px; margin: 0 auto;", CSS)
        self.assertIn("#asset-view .md.wf-page { width: 100%; max-width: 1040px; margin: 0 auto;", CSS)


class 页头样式(unittest.TestCase):
    def test_应用复用设置式标题层级(self):
        with open(os.path.join(ROOT, "frontend", "app-assets-apps.js"), encoding="utf-8") as source:
            apps = source.read()
        self.assertIn('_resourceHeader(titles[st.pool]', apps)
        self.assertIn('all: "全部应用", official: "官方应用", local: "本地应用"', apps)
        self.assertIn('.settings-center-live .sm-view-head h1', CSS)
        self.assertNotIn('class="ap-title"', apps)

    def test_页头创建按钮共用应用样式并保留手机点击区(self):
        self.assertIn('.ap-create, .arr-head .arr-primary, .arr-head .ws-import {', CSS)
        self.assertIn('.ap-create:hover, .arr-head .arr-primary:hover, .arr-head .ws-import:hover {', CSS)
        self.assertIn('.arr-head .arr-primary { min-height: 38px;', CSS)
        self.assertNotIn('.arr-head .arr-primary span { display: none;', CSS)
        self.assertIn('.arr-primary { min-height: 38px;', CSS)

    def test_新建入口保留向下箭头和同尺寸(self):
        with open(os.path.join(ROOT, "frontend", 'app-assets-apps.js'), encoding="utf-8") as source:
            self.assertIn('<polyline points="6 9 12 15 18 9"/>', source.read())
        with open(os.path.join(ROOT, "frontend", 'app-panel-arrangements.js'), encoding="utf-8") as source:
            arr = source.read()
        match = re.search(r'^  down: (".*"),$', arr, re.M)
        root = ET.fromstring(json.loads(match.group(1)))
        self.assertEqual(root.attrib['width'], '12')
        self.assertEqual(root.attrib['height'], '12')
        self.assertEqual(root.find('.//{http://www.w3.org/2000/svg}path').attrib['d'],
                         'm6.8 11.8 8.3 8.8a1.25 1.25 0 0 0 1.8 0l8.3-8.8')
        self.assertIn("+ _ARR_ICONS.down +", arr)
        self.assertIn('.ap-create svg, .arr-head .arr-primary svg { display: block; width: 12px; height: 12px;', CSS)


if __name__ == "__main__":
    unittest.main()
