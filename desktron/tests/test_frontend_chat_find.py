"""会话页的页内查找(⌘F)。

为什么不靠浏览器自带的查找:这一页的文档不完整,三层遮挡叠着——
1. 贴底时 hsTrimDom 会把靠上的回合从文档里摘走(节点仍挂在 v.turns 上);
2. 处理中区、思考正文、压缩摘要默认 display:none;
3. 更早的历史压根没铺,要往上翻页现取(后端一次只给最近 25 个回合)。
自己做能盖住前两层:开查找时先 hsUnstick(它内含 hsRemount)把摘走的挂回来,命中落在
折叠区就把祖先展开。第三层盖不住,所以占位符与说明里都写「已加载的内容」,不让人
以为搜的是整段会话。

盖不住的还有工具详情:命令全文、输出、diff 与子代理日志是展开那一刻才建节点的
(hsStepDetailMount / hsStepFull),没展开就不在文档里。这是刻意的取舍,不是漏。

高亮走 CSS 自定义高亮接口,与输入框那处命令着色同源(hsCmdPaint):只标区间、不动 DOM,
不会打散 markdown 渲染出来的结构。老内核没这个接口时静默降级成「只跳转不着色」。

CDP 实测(离线搭一个 hsBuild 出来的页,灌 12 段正文 + 1 个折叠区):
  搜 envoy → 25 处命中、计数「1/25」、高亮区间 25 个
  下一个/上一个/两头环绕 → 1、0、24、0(总数 25)
  跳到折叠区里那个命中 → 祖先加上了 .open,当前高亮区间 1 个
  ⌘F 开查找 → 是;输入框里的 Ctrl+F 没被抢 → 是;Esc 关查找且 docKey 放行 → 是
  无匹配 → 「无匹配」、上一个钮禁用;关闭 → 隐藏且高亮清空
"""
import os
import re
import unittest
import pytest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()

WIRE = re.search(r"function hsFindWire\(v\)[\s\S]+?\n}\n", CHAT).group(0)
SCAN = re.search(r"function hsFindScan\(v, q\)[\s\S]+?\n}\n", CHAT).group(0)
GO = re.search(r"function hsFindGo\(v, i\)[\s\S]+?\n}\n", CHAT).group(0)
OPEN = re.search(r"function hsFindOpen\(v\)[\s\S]+?\n}\n", CHAT).group(0)


class 键位不抢输入框(unittest.TestCase):
    def test_元F与CtrlF都认(self):
        self.assertIn("!ev.metaKey && !ev.ctrlKey", WIRE)

    def test_输入框里的CtrlF留给readline(self):
        """Ctrl+F 在输入框里是那套 emacs 键位的「光标右移一格」(见 hsEditKey),抢了会
        破坏手感。所以焦点在输入框时只认 ⌘;别处两个都认(Windows/Linux 没有 ⌘)。"""
        self.assertIn("!ev.metaKey && v.inp && v.inp.contains(ev.target)", WIRE)

    def test_只对当前活动的那一页生效(self):
        """键盘监听挂在 document 上,多开几段会话时不能每一页都响应。"""
        self.assertIn("pActive && pActive.el === v.el", WIRE)

    def test_页没了就自己摘掉监听(self):
        self.assertIn('document.removeEventListener("keydown", v.findKey, true)', WIRE)


class Esc不会顺手中止回合(unittest.TestCase):
    def test_查找条自己拦下Esc(self):
        self.assertIn("ev.preventDefault(); ev.stopPropagation();", WIRE)

    def test_docKey也放行查找条(self):
        """docKey 是捕获阶段的监听,跑在查找条自己的处理之前,光靠 stopPropagation 挡不住。"""
        m = re.search(r"v\.docKey = \(ev\) => \{[\s\S]+?\n  \};", CHAT)
        self.assertTrue(m, "docKey 不见了")
        self.assertIn('closest(".hs-find")', m.group(0))


class 搜索范围(unittest.TestCase):
    def test_折叠区照收(self):
        """处理中区、思考正文、压缩摘要默认 display:none,但要能搜到——跳过去时再展开。
        真正排除的只有不属于会话正文的那几块。"""
        self.assertIn("HS_FIND_SKIP", SCAN)
        skip = re.search(r'const HS_FIND_SKIP = "([^"]+)"', CHAT).group(1)
        for cls in (".hs-toc", ".hs-composer", ".hs-find", ".hs-rv"):
            self.assertIn(cls, skip)
        # 折叠区不在排除列表里
        for cls in (".hs-proc", ".hs-think", ".hs-cmp"):
            self.assertNotIn(cls + ",", skip)

    def test_查找会按需物化命中的折叠思考(self):
        """折叠 reasoning 平时不保留正文 DOM；查找时只重建确实含关键词的块。"""
        self.assertIn('part.type !== "reasoning"', SCAN)
        self.assertIn('includes(needle)', SCAN)
        self.assertIn('part.el.classList.add("open")', SCAN)
        self.assertIn('hsPaint(v, part)', SCAN)

    def test_命中落在折叠区就展开祖先(self):
        """range 落在 display:none 里量不出位置,不展开的话滚动等于滚到 0。"""
        for cls in ('".hs-proc"', '".hs-think"', '".hs-cmp"'):
            self.assertIn(cls, GO)
        self.assertIn('classList.add("open")', GO)

    def test_开查找先把裁掉的回合挂回来(self):
        """hsUnstick 内含 hsRemount;顺带停掉贴底,否则跳转会被拽回底部。"""
        self.assertIn("hsUnstick(v)", OPEN)

    def test_命中数有上限(self):
        """两千个之后再多也没人一个个翻,而 Range 对象不便宜。"""
        self.assertIn("hits.length >= 2000", SCAN)


class 高亮与样式(unittest.TestCase):
    def test_用自定义高亮不包标签(self):
        """正文是 markdown 渲染出来的,插 <mark> 会打散它的结构。"""
        self.assertIn('const HS_FIND_HL = "hs-find"', CHAT)
        self.assertIn('const HS_FIND_CUR = "hs-find-cur"', CHAT)
        self.assertIn("::highlight(hs-find)", CSS)
        self.assertIn("::highlight(hs-find-cur)", CSS)

    def test_老内核静默降级(self):
        """没有这个接口时只是没颜色,跳转照常——不能整个功能失效。"""
        m = re.search(r"function hsFindHl\(name\)[\s\S]+?\n}\n", CHAT)
        self.assertTrue(m)
        self.assertIn("if (!window.CSS || !CSS.highlights", m.group(0))

    def test_当前命中与其余不同色(self):
        """一屏十几个命中时,只靠"亮一点"分不出当前是哪个。"""
        cur = re.search(r"::highlight\(hs-find-cur\) \{([^}]*)\}", CSS).group(1)
        self.assertIn("color:", cur)   # 实底反白,不只是底色

    def test_查找条不再叠加浮层阴影(self):
        bar = re.search(r"\.hs-page \.hs-find \{([^}]*)\}", CSS).group(1)
        self.assertIn('box-shadow: none', bar)
        self.assertIn('height: 32px', bar)
        self.assertIn('border-radius: 10px', bar)
        self.assertNotIn('rgba(', bar)

    def test_搜索图标仅作装饰不重复朗读(self):
        self.assertIn('class="hs-find-icon" aria-hidden="true"', CHAT)
        self.assertIn('aria-label="查找"', CHAT)

    def test_上下钮共用一枚图标各转90度(self):
        """两个方向各画一枚,迟早会画得不一样粗。"""
        self.assertIn(".hs-find-prev svg { transform: rotate(-90deg); }", CSS)
        self.assertIn(".hs-find-next svg { transform: rotate(90deg); }", CSS)


class 左上定位与搜索交互(unittest.TestCase):
    @pytest.mark.desktop
    def test_webkit_left_corner_resizes_without_changing_find_controls(self):
        from playwright.sync_api import sync_playwright
        markup = re.search(r"('<div class=\"hs-find\" hidden>'[\s\S]+?\+ \"</div>\")", CHAT).group(1)
        with open(os.path.join(ROOT, 'frontend', 'app-chat-panels.js'), encoding='utf-8') as file:
            panels = file.read()
        find_js = panels[panels.index('const HS_FIND_HL ='):]
        with sync_playwright() as pw:
            browser = pw.webkit.launch()
            try:
                for width in (320, 390, 1280):
                    page = browser.new_page(viewport={'width': width, 'height': 800})
                    page.set_content('''<main style="display:flex;height:700px;padding-top:40px">
                      <aside id="sidebar" style="display:none;width:200px;flex:none"></aside>
                      <div id="chat" class="overlay-content hs-page" style="position:relative;flex:1;min-width:0">
                        <div class="hs-wrap"><div class="hs-body"><div class="hs-stream hs-col">
                          <p>kit one</p><p>kit two</p>
                          <div class="hs-think"><div class="hs-think-body">kit folded</div></div>
                          <div style="height:1600px"></div>
                        </div></div></div>
                        <div class="hs-composer"><div class="hs-box"><textarea id="draft"></textarea></div></div>
                      </div><aside id="info" style="display:none;width:240px;flex:none"></aside>
                    </main>''')
                    page.add_style_tag(content=CSS)
                    page.add_script_tag(content='const HS_ICONS = {chev:"<svg></svg>",qclose:"<svg></svg>"};'
                                        + 'document.querySelector(".hs-wrap").insertAdjacentHTML("afterbegin",' + markup + ');'
                                        + find_js + '''
                        function hsUnstick() {}
                        function hsViewportReveal(v, r) { r.startContainer.parentElement.scrollIntoView(); }
                        const v = {el:document.querySelector('#chat'),stream:document.querySelector('.hs-stream'),
                          body:document.querySelector('.hs-body'),inp:document.querySelector('#draft'),turns:[]};
                        const pActive = v; hsFindWire(v);
                    ''')
                    page.keyboard.press('Control+f')
                    self.assertTrue(page.locator('.hs-find').is_visible())
                    for theme in ('dark', 'light'):
                        page.evaluate('t => document.documentElement.dataset.theme=t', theme)
                        for sidebars in (False, True) if width == 1280 else (False,):
                            page.evaluate('on => { for (const id of ["sidebar","info"]) document.getElementById(id).style.display=on?"block":"none"; }', sidebars)
                            wrap = page.locator('.hs-wrap').bounding_box()
                            bar = page.locator('.hs-find').bounding_box()
                            self.assertAlmostEqual(bar['x'] - wrap['x'], 18, delta=0.5)
                            self.assertAlmostEqual(bar['y'] - wrap['y'], 10, delta=0.5)
                            self.assertLessEqual(bar['x'] + bar['width'], wrap['x'] + wrap['width'] - 17.5)
                            self.assertAlmostEqual(bar['height'], 32, delta=0.5)
                            self.assertLessEqual(bar['width'], 340.5)
                            icon = page.locator('.hs-find-icon svg').bounding_box()
                            self.assertAlmostEqual(icon['width'], 14, delta=0.5)
                            self.assertAlmostEqual(icon['y'] + icon['height'] / 2, bar['y'] + bar['height'] / 2, delta=0.5)
                            for focused in (False, True):
                                page.locator('.hs-find-q').evaluate('el => el.blur()')
                                if focused:
                                    page.locator('.hs-find-q').focus()
                                style = page.locator('.hs-find').evaluate('''el => {
                                  const s=getComputedStyle(el), q=getComputedStyle(el.querySelector('input'));
                                  return {shadow:s.boxShadow, blur:s.backdropFilter || s.webkitBackdropFilter,
                                    radius:s.borderRadius, border:s.borderTopWidth,
                                    background:s.backgroundColor, inputShadow:q.boxShadow, inputOutline:q.outlineStyle,
                                    fontSize:q.fontSize, composer:getComputedStyle(document.querySelector('.hs-box')).backgroundColor};
                                }''')
                                self.assertEqual(style['shadow'], 'none', (width, theme, focused))
                                self.assertEqual(style['blur'], 'none')
                                self.assertEqual(style['radius'], '10px')
                                self.assertEqual(style['border'], '1px')
                                self.assertEqual(style['background'], style['composer'])
                                self.assertEqual(style['inputShadow'], 'none')
                                self.assertEqual(style['inputOutline'], 'none')
                                self.assertEqual(style['fontSize'], '13.5px')
                            for selector in ('.hs-find-prev', '.hs-find-next', '.hs-find-x'):
                                button = page.locator(selector).bounding_box()
                                self.assertAlmostEqual(button['width'], 24, delta=0.5)
                                self.assertLessEqual(button['x'] + button['width'], bar['x'] + bar['width'])
                            page.locator('.hs-body').evaluate('el => el.scrollTop=400')
                            self.assertEqual(bar, page.locator('.hs-find').bounding_box())
                    page.locator('.hs-find-q').fill('kit')
                    page.wait_for_function('document.querySelector(".hs-find-n").textContent === "1/3"')
                    page.locator('.hs-find-next').click()
                    self.assertEqual(page.locator('.hs-find-n').inner_text(), '2/3')
                    page.locator('.hs-find-prev').click()
                    self.assertEqual(page.locator('.hs-find-n').inner_text(), '1/3')
                    page.locator('.hs-find-q').focus()
                    page.keyboard.press('Shift+Enter')
                    self.assertEqual(page.locator('.hs-find-n').inner_text(), '3/3')
                    self.assertEqual(page.locator('.hs-think.open').count(), 1)
                    page.keyboard.press('Escape')
                    self.assertTrue(page.locator('.hs-find').is_hidden())
                    self.assertTrue(page.locator('#draft').evaluate('el => el === document.activeElement'))
                    page.keyboard.press('Meta+f')
                    page.locator('.hs-find-q').fill('not-present')
                    page.wait_for_function('document.querySelector(".hs-find-n").textContent === "无匹配"')
                    self.assertTrue(page.locator('.hs-find-prev').is_disabled())
                    self.assertTrue(page.locator('.hs-find-next').is_disabled())
                    page.locator('.hs-find-x').click()
                    self.assertTrue(page.locator('.hs-find').is_hidden())
                    page.close()
            finally:
                browser.close()


class 说清楚搜的是什么(unittest.TestCase):
    def test_占位符写明已加载(self):
        """搜不到更早的历史与没展开的工具详情,占位符要先说,不能让人以为搜的是整段会话。"""
        self.assertIn('placeholder="在已加载的内容里找"', CHAT)

    def test_关闭按钮复用会话图标(self):
        self.assertIn("HS_ICONS.qclose", CHAT)
        self.assertNotIn('aria-label="关闭查找">×', CHAT)


if __name__ == "__main__":
    unittest.main()
