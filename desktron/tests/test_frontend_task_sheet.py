"""任务详情改成浮层,不再就地展开。

原来点一条任务是在行下面展开:流程图加整篇备忘把列表顶出去半屏,上下两条任务被推得很远,
想对着看两条就得来回滚(2026-08-16 按用户要求改)。现在点一行弹一张浮层,列表本身不动。

详情正文恢复为单一 Markdown 阅读面：结构化账本只在展示层合成 Markdown，人工补充仍从
原备忘录读取；详情暂时只读，不在这里暴露 Markdown 源码编辑器。
"""
import os
import re
import unittest
import pytest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PANEL = _srcmap.panel_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


def fn(name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), PANEL, re.S)
    assert m, "%s 不见了" % name
    return m.group(0)


class OpensSheet(unittest.TestCase):
    def test_任务详情把账本与补充收进单一Markdown阅读面(self):
        seg = fn("wfOpenTaskSheet")
        ledger = fn("_wfTaskLedgerMarkdown")
        mount = fn("_wfTaskMarkdownMount")
        self.assertIn('_wfTaskMarkdownMount(area, domId, t', seg)
        for field in ('ledger.done', 'ledger.next', 'ledger.blocker', 'ledger.contract',
                      'ledger.criterion_status', 'ledger.sessions'):
            self.assertIn(field, ledger)
        self.assertIn('还没有进展记录', ledger)
        self.assertIn('md.render(detail.markdown)', mount)
        self.assertIn('_wfMemoPost(domId, t, "memo-get")', mount)
        self.assertNotIn('_wfNoteEd(', mount)
        self.assertNotIn('editButton', mount)
        self.assertNotIn('wf-task-edit', seg)
        self.assertNotIn('memo-set', seg)

    def test_全局任务页点行直接弹浮层(self):
        seg = fn("openTasksPage")
        self.assertIn("wfOpenTaskSheet(task, el.dataset.dom)", seg)
        self.assertNotIn("_wfFocusTask", seg)
        self.assertNotIn('selectNav("domain"', seg)

    def test_详情不再直接铺流程图_任务流编辑仍在操作菜单(self):
        seg = fn("wfOpenTaskSheet")
        self.assertNotIn("_wfTaskFlowMount(area, t, domId)", seg)
        self.assertNotIn("_wfTaskNoteMount(area, domId, t)", seg)
        self.assertIn("_wfTaskMarkdownMount(area, domId, t", seg)
        self.assertIn("_wfFlowBuilder(sheet, t, domId, stages)", seg)

    def test_同一时刻只开一张(self):
        # 壳一进门先关掉当前那张(任务详情与记忆卡共用,见 test_frontend_mem_sheet.py)
        seg = fn("openFloatSheet")
        self.assertIn("wfCloseTaskSheet();", seg)
        self.assertIn("_wfSheet = back;", seg)

    def test_两条关闭路径(self):
        seg = fn("openFloatSheet")
        self.assertIn("!sheet.contains(ev.target)", seg)   # 点卡片以外
        self.assertIn('ev.key === "Escape"', seg)    # Esc
        close = fn("wfCloseTaskSheet")
        self.assertIn("removeEventListener", close)  # 关掉要摘掉键盘监听,不然越开越多


class CoversMainOnly(unittest.TestCase):
    """浮层只盖主区,不盖整窗。

    盖住侧栏读起来像是整个应用被挡住,而侧栏此刻仍然可点(2026-08-16 用户实见)。位置按
    #main 的矩形量出来写进行内样式;用 fixed 而不是往 #asset-view 里塞 absolute——那个容器
    是滚动的,absolute 会跟着内容滚走。
    """
    def test_按主区矩形定位(self):
        seg = fn("_wfSheetFit")
        self.assertIn('getElementById("main")', seg)
        for k in ("left", "top", "width", "height"):
            self.assertIn("back.style.%s" % k, seg)

    def test_会话中详情卡按正文列居中(self):
        seg = fn("_wfSheetFit")
        self.assertIn('pActive.el.classList.contains("hs-page")', seg)
        self.assertIn('page.querySelector(".hs-stream")', seg)
        self.assertIn('--wf-sheet-shift-x', seg)
        self.assertIn('transform: translateX(var(--wf-sheet-shift-x))', CSS)

    def test_不再整窗铺满(self):
        # inset:0 会盖住侧栏
        m = re.search(r"\.wf-sheet-back \{[^}]+\}", CSS, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn("inset: 0", m.group(0))
        self.assertIn("position: fixed", m.group(0))

    def test_窗口尺寸变了重新量(self):
        seg = fn("openFloatSheet")
        self.assertIn('window.addEventListener("resize", back._fit)', seg)
        self.assertIn("window.removeEventListener(\"resize\", el._fit)", fn("wfCloseTaskSheet"))

    def test_点侧栏也关得掉(self):
        # 遮罩够不着侧栏了,判据要挂在 document 上;否则点侧栏走开时这张卡会留在那儿
        seg = fn("openFloatSheet")
        self.assertIn('document.addEventListener("mousedown", back._out, true)', seg)
        self.assertIn("!sheet.contains(ev.target)", seg)


class MenuAboveSheet(unittest.TestCase):
    """光标菜单要压在浮层之上。

    它是从任何地方唤出来的一层——会话行、任务行、图表、以及浮层里的「…」都用它。层级压在
    浮层之下的话,在浮层里点「…」菜单就被盖住(2026-08-16 实见)。
    """
    def _z(self, sel):
        m = re.search(re.escape(sel) + r"\s*\{[^}]*z-index:\s*(\d+)", CSS, re.S)
        self.assertIsNotNone(m, "没找到 %s 的 z-index" % sel)
        return int(m.group(1))

    def test_菜单高于浮层(self):
        self.assertGreater(self._z(".rp-ctx"), self._z(".wf-sheet-back"))

    def test_菜单也高于应用浮层那一档(self):
        # #overlay 一类的应用浮层在 80~90;菜单同样可能从它们里面唤出
        self.assertGreater(self._z(".rp-ctx"), 90)


class NoLeftovers(unittest.TestCase):
    def test_展开态集合退休(self):
        # 详情不再占页面位置,「哪条展开着」这件事不复存在
        self.assertNotIn("_wfFlowOpen", PANEL)

    def test_浮层以视口三分之二限高自滚(self):
        # 备忘可以很长,卡片本身不能跟着长出屏幕
        self.assertIn(".wf-sheet-body { flex: 1 1 auto; min-height: 0; overflow: auto;", CSS)
        self.assertIn("--wf-sheet-max-h: min(66vh, 980px)", CSS)
        self.assertIn("max-height: min(var(--wf-sheet-max-h), 100%)", CSS)

    def test_手机端改为近全宽居中面板(self):
        start = CSS.index("/* 手机上的任务详情与项目记忆共用同一张居中面板。")
        mobile = CSS[start:CSS.index(".wf-chart-tip {", start)]
        self.assertIn("align-items: center", mobile)
        self.assertNotIn("align-items: flex-end", mobile)
        self.assertIn("padding: 10px 10px calc(10px + env(safe-area-inset-bottom))", mobile)
        self.assertIn(".wf-sheet {", mobile)
        self.assertIn("width: 100%", mobile)
        self.assertIn("max-height: calc(100% - 8px)", mobile)
        self.assertIn("transform: none", mobile)
        self.assertIn("-webkit-overflow-scrolling: touch", mobile)
        self.assertIn("font-size: 13.5px", mobile)
        self.assertNotIn("wf-task-edit", mobile)


try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, "需要 Playwright WebKit")
@pytest.mark.desktop
class TaskDetailWebKit(unittest.TestCase):
    def test_详情只渲染Markdown_不再暴露源码编辑(self):
        with open(os.path.join(ROOT, 'frontend', 'app-panel-task-memo.js'), encoding='utf-8') as stream:
            source = stream.read()
        with sync_playwright() as pw:
            browser = pw.webkit.launch()
            try:
                for width in (390, 1440):
                    page = browser.new_page(viewport={"width":width, "height":844})
                    page.set_content(
                        '<main id="main" style="position:fixed;inset:0">'
                        '<section class="hs-page"><div class="hs-stream" '
                        'style="position:fixed;left:80px;width:280px;height:600px"></div></section>'
                        '</main>'
                    )
                    page.add_style_tag(content=CSS)
                    page.add_script_tag(path=os.path.join(ROOT, 'frontend', 'vendor', 'markdown-it.min.js'))
                    page.add_script_tag(content='const md=window.markdownit({html:false,linkify:true});')
                    page.add_script_tag(content=source)
                    page.add_script_tag(content='''
                        window.pActive={el:document.querySelector('.hs-page')};
                        function _wfFlowStages(){return []}
                        function taskCbSvg(){return ''}
                        _wfMemoPost=async()=>({ok:true,note:'note-1',text:'交付\\n\\n## 补充说明\\n\\n请按发布清单验收。'});
                        wfOpenTaskSheet({title:'交付',state:'doing',note:'note-1',ledger:{
                          done:'代码已完成',next:'等待验收',blocker:'等待授权',
                          sessions:[{id:'s1',engine:'pi',title:'实现会话'}],
                          contract:{criteria:[{id:'test',summary:'相关测试通过'}]},
                          criterion_status:{test:'passed'}}}, 'alpha');
                    ''')
                    for text in ('代码已完成', '等待验收', '等待授权', '实现会话', '相关测试通过'):
                        self.assertEqual(page.get_by_text(text, exact=True).count(), 1)
                    page.get_by_text('补充说明', exact=True).wait_for()
                    self.assertNotIn('## 当前进展', page.locator('.wf-task-markdown').inner_text())
                    self.assertEqual(page.locator('.wf-task-markdown h2').count(), 2)
                    self.assertTrue(page.get_by_text('补充说明', exact=True).is_visible())
                    self.assertEqual(page.locator('.wf-task-edit,.wf-task-md-edit,.wf-note-ed').count(), 0)
                    if width == 390:
                        box = page.locator('.wf-sheet').bounding_box()
                        self.assertAlmostEqual(box['x'], width - box['x'] - box['width'], delta=1)
                    page.evaluate('wfCloseTaskSheet()')
                    page.evaluate("wfOpenTaskSheet({title:'新任务',state:'todo',ledger:{}}, 'alpha')")
                    self.assertEqual(page.get_by_text('还没有进展记录', exact=True).count(), 1)
                    self.assertEqual(page.locator('.wf-task-markdown').count(), 1)
                    self.assertEqual(page.locator('.wf-task-edit,.wf-task-md-edit,.wf-note-ed').count(), 0)
                    page.evaluate('wfCloseTaskSheet()')
                    page.close()
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
