"""设置目录只保留文字、分组、数字和提醒状态；正文标题仍可使用语义图标。"""
import os
import re
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CORE = open(os.path.join(ROOT, "frontend", "app-core.js"), encoding="utf-8").read()
JS = _srcmap.settings_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
RAIL = open(os.path.join(ROOT, "frontend", "app-rail-nav.js"), encoding="utf-8").read()
TOPNAV = open(os.path.join(ROOT, "frontend", "app-rail-topnav.js"), encoding="utf-8").read()
INIT = open(os.path.join(ROOT, "frontend", "app-init.js"), encoding="utf-8").read()
NAV = re.search(r"function _renderSettingsNav\(\)[\s\S]+?\n}", JS).group(0)


def _calls():
    """取每处 smSection(...) 调用的实参原文。按括号配平扫,不用正则——说明文字里带着
    分号和括号(比如带说明的引擎名那条),任何"扫到某个符号为止"的写法都会截错。
    函数定义那处是 smSection(title, ...),没有引号,不在此列。"""
    out = []
    for m in re.finditer(r'smSection\("', JS):
        i = m.end() - 2          # 停在左括号上
        depth, j = 0, i
        while j < len(JS):
            if JS[j] == "(":
                depth += 1
            elif JS[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(JS[i + 1:j])
    return out


class 每节都要挂载(unittest.TestCase):
    """smSection 只建节点、不挂 DOM(它重载 appendChild 让内容落进卡内,但自己要调用方挂)。
    漏了那一行的后果是「代码全在、端点正常、页面上没有」——从合包里 grep 得到、
    从接口取得到,唯独看不见(2026-09-01「首次设置」那一节就是这么漏的)。"""

    def test_每个小节都被_appendChild(self):
        import re as _re
        # 取每个 const <名> = smSection(...) 的变量名,再看有没有 g*.appendChild(<名>)
        names = _re.findall(r"const\s+(\w+)\s*=\s*smSection\(", JS)
        self.assertTrue(names, "一节都没找到,取法要跟着改")
        missing = [n for n in names
                   if not _re.search(r"\.appendChild\(\s*%s\s*\)" % _re.escape(n), JS)]
        self.assertEqual(missing, [], "这些小节建了但没挂进 DOM:" + ", ".join(missing))


class 每节都有图标(unittest.TestCase):
    def test_全部小节都传了icon(self):
        """少一节的话目录里那一行就缺一枚,一列图标中间空一格比全都没有更显眼。"""
        self.assertEqual(len(_calls()), 13, "小节数变了,这条要跟着核对")
        for c in _calls():
            self.assertTrue("ICONS." in c or "BRAND_ICONS." in c or "ARR_SYS_ICONS." in c,
                            "有一节没传图标:" + c[:40])

    def test_引擎小节用品牌图标(self):
        for eng in ("claude", "codex", "pi"):
            self.assertIn("BRAND_ICONS.%s" % eng, JS)

    def test_其余小节用线条图标(self):
        for ico in ("moon", "terminal", "browser", "lock", "chart", "laptop"):
            self.assertIn("ICONS.%s" % ico, JS)


class 目录不展示图标(unittest.TestCase):
    def test_目录行只渲染文字(self):
        self.assertNotIn('querySelector(".sm-title-ic")', NAV)
        self.assertNotIn('class="sm-nav-ic"', NAV)
        self.assertIn('<span class="ai-title">', NAV)
        self.assertNotIn("item.badge", NAV)
        self.assertNotIn("<em>", NAV)

    def test_目录模型不再声明图标(self):
        model = re.search(r"const _SM_NAV_MODEL = \[[\s\S]+?\n\];", JS).group(0)
        self.assertNotIn("icon:", model)
        self.assertNotIn("_SM_NAV_ICON_CROP", JS)
        self.assertNotIn("_fitSettingsNavIcon", JS)


class 设置目录归属(unittest.TestCase):
    def test_进入设置不再接管主区或应用侧栏(self):
        fn = re.search(r"function openSettings\(\) \{[\s\S]+?\n}", JS).group(0)
        self.assertIn('const modal = $("#settings-modal")', fn)
        self.assertIn("modal.hidden = false", fn)
        self.assertIn('id="sm-nav"', fn)
        self.assertIn('class="sm-inner-nav"', fn)
        self.assertNotIn("navPush", fn)
        self.assertNotIn("showView", fn)
        self.assertNotIn("_sideEnterTreeMode()", fn)
        self.assertNotIn("_sideEnterSettingsMode()", fn)

    def test_目录不等待设置接口返回(self):
        fn = re.search(r"function openSettings\(\) \{[\s\S]+?\n}", JS).group(0)
        self.assertLess(fn.index("_renderSettingsNav()"), fn.index("renderSettings().then"))

    def test_目录只写设置页自己的容器(self):
        self.assertIn('const el = $("#sm-nav")', NAV)
        self.assertNotIn('#panel-list', NAV)
        self.assertNotIn('#proj-head', NAV)


class 设置退出保持导航一致(unittest.TestCase):
    def test_设置按钮再次点击仍可退出(self):
        self.assertIn("_leaveSettings();", INIT)

    def test_关闭设置只移除浮层并保持原导航(self):
        fn = re.search(r"function _leaveSettings\(\) \{[\s\S]+?\n}", JS).group(0)
        self.assertNotIn("_sideEnterTreeMode()", fn)
        self.assertNotIn("renderSessionPanel()", fn)
        self.assertIn('$("#settings-modal").hidden = true', fn)
        self.assertNotIn("navGo", fn)
        self.assertNotIn("selectNav", fn)

    def test_页内目录不重复提供返回入口(self):
        self.assertNotIn("返回工作流", NAV)
        self.assertNotIn("side-back", NAV)

    def test_navGo把回放成败返回给调用方(self):
        fn = re.search(r"function navGo\(d\) \{[\s\S]+?\n}", TOPNAV).group(0)
        self.assertIn("const ok = _navReplay(e)", fn)
        self.assertIn("return ok", fn)


class 行的排布(unittest.TestCase):
    def _rule(self, sel):
        hits = re.findall(r"\n%s \{[^}]*\}" % re.escape(sel), CSS)
        self.assertTrue(hits, "找不到 %s" % sel)
        return hits[-1]

    def test_不再靠内距硬撑缩进(self):
        r = self._rule(".sm-nav-row")
        self.assertNotIn("padding-left: 32px", r)
        self.assertIn("display: flex", r)
        self.assertIn("align-items: center", r)

    def test_纯文字行不保留图标槽(self):
        row = self._rule(".sm-nav-row")
        self.assertIn("padding: 6px 10px", row)
        self.assertNotIn("gap:", row)
        self.assertNotIn(".sm-nav-ic", CSS)
        self.assertNotIn("--sm-nav-art-width", CSS)

    def test_浅色选中态复用主题中性表面(self):
        active = self._rule('.sm-inner-nav .sm-nav-row.active')
        self.assertIn("background: var(--surface-active)", active)
        self.assertNotIn('html[data-theme="light"] .sm-inner-nav .sm-nav-row.active', CSS)

    def test_深色选中态与引擎菜单共用中性表面(self):
        nav = self._rule('.sm-inner-nav .sm-nav-row.active')
        engine = self._rule('#engine-menu .fm-row.on')
        self.assertIn("background: var(--surface-active)", nav)
        self.assertIn("background: var(--surface-active)", engine)


class 目录选项平铺(unittest.TestCase):
    def test_不渲染不可点击的二级标题(self):
        self.assertNotIn('className = "sm-nav-group"', NAV)
        self.assertNotIn("gRow", NAV)
        self.assertNotIn(".sm-nav-group", CSS)

    def test_数据仍保留语义分组供搜索使用(self):
        self.assertIn("_SM_NAV_MODEL.flatMap", JS)


class 新增图标的画法(unittest.TestCase):
    @staticmethod
    def icon(name):
        import json
        import xml.etree.ElementTree as ET
        match = re.search(r'^  ' + re.escape(name) + r': (".*"),$', CORE, re.M)
        assert match, '缺少图标 ' + name
        return ET.fromstring(json.loads(match.group(1)))

    def test_设置图标使用已评审32格画布(self):
        for name in ('moon', 'terminal', 'browser', 'lock', 'chart'):
            icon = self.icon(name)
            self.assertEqual(icon.attrib['viewBox'], '0 0 32 32')
            self.assertEqual(icon.attrib['data-desk-icon'], 'ICONS.' + name)
            paint = list(icon)[0]
            self.assertEqual(paint.attrib['stroke-width'], '2.1')
            self.assertEqual(paint.attrib['fill'], 'none')

    def test_允许小圆点但不填满主体轮廓(self):
        for name in ('moon', 'terminal', 'browser', 'lock'):
            for node in self.icon(name).iter():
                if node.attrib.get('fill') == 'currentColor':
                    self.assertTrue(node.tag.endswith('circle'), name + ' 主体不应实心')

    def test_锁保留宽圆角锁体与锁梁(self):
        icon = self.icon('lock')
        rect = next(node for node in icon.iter() if node.tag.endswith('rect'))
        self.assertEqual((rect.attrib['width'], rect.attrib['height'], rect.attrib['rx']), ('21', '17', '5.2'))
        self.assertTrue(any(node.attrib.get('d') == 'M10 12V8.6a6 6 0 0 1 12 0V12' for node in icon.iter()))

    def test_换掉的两枚不再留名(self):
        for gone in ('key', 'contrast'):
            self.assertNotRegex(CORE, r'\n  ' + gone + r': ')
            self.assertNotIn('ICONS.' + gone, JS)

    def test_外观保留单条连续月牙(self):
        icon = self.icon('moon')
        paths = [node for node in icon.iter() if node.tag.endswith('path')]
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].attrib['d'].endswith('Z'))
        self.assertFalse(any(node.tag.endswith('line') for node in icon.iter()))

    def test_柱状图沿用本轮批准的三根圆端笔画(self):
        icon = self.icon('chart')
        paths = [node.attrib['d'] for node in icon.iter() if node.tag.endswith('path')]
        self.assertEqual(paths, ['M4.5 3.5v21c0 2.3 1.3 3.5 3.5 3.5h20M11 21v-7M18 21V9M25 21V4'])
        self.assertEqual(list(icon)[0].attrib['stroke-linecap'], 'round')


if __name__ == "__main__":
    unittest.main()
