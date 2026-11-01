import os
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class FrontendSessionSearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def read(name):
            with open(os.path.join(ROOT, "frontend", name), encoding="utf-8") as fh:
                return fh.read()
        cls.rail = read("app-rail-nav.js")
        cls.chat = read("app-chat-page.js")
        cls.entry = read("app-assets-term-ui.js")

    def test_正文搜索走后端而不是遍历消息_dom(self):
        self.assertIn("/session/search?q=", self.rail)
        self.assertIn('const searchEngine = shepherdMode ? "shepherd" : curEngine', self.rail)
        self.assertIn("&engine=${encodeURIComponent(searchEngine)}", self.rail)
        self.assertIn("sideSearchPaint", self.rail)
        self.assertNotIn('querySelectorAll(".hs-turn")', self.rail)

    def test_shepherd_搜索走独立索引且不再硬编码空结果(self):
        self.assertIn('shepherdMode ? "shepherd" : curEngine', self.rail)
        self.assertNotIn("if (shepherdMode) {\n    sideSearchPaint(query, { results: []", self.rail)

    def test_侧栏保鲜不重复重建同一查询(self):
        self.assertIn("_sideSearchKey", self.rail)
        self.assertIn("if (key === _sideSearchKey) return", self.rail)

    def test_搜索文案面向用户(self):
        # 2026-09-04 起这一节的头带排序开关(第三个参数),分节与计数的意图不变。
        self.assertIn('section("搜索结果", results.length, sortBox())', self.rail)
        self.assertIn('`搜索到 ${item.count} 处相关内容`', self.rail)
        self.assertNotIn("正文命中", self.rail)
        self.assertNotIn("处命中", self.rail)

    def test_命中携带消息与回合锚点(self):
        self.assertIn("message_id: hit.message_id", self.rail)
        self.assertIn("turn_from_end: hit.turn_from_end", self.rail)
        self.assertIn("engine: item.engine || curEngine", self.rail)
        self.assertIn("openHtmlSession(sid, listedTitle, true, opts || {})", self.entry)
        self.assertIn("hsLoadAnchor(v, opts.anchor)", self.chat)

    def test_锚点按小窗加载且历史偏移参与继续翻页(self):
        self.assertIn("/around?", self.chat)
        self.assertIn("anchor.engine || v.engine", self.chat)
        self.assertIn("(v.historyNewer || 0) + v.turns.length", self.chat)
        self.assertIn("hs-search-target", self.chat)
        self.assertIn("if (v.historyNewer > 0)", self.chat)


if __name__ == "__main__":
    unittest.main()


class SortToggleAndExpand(unittest.TestCase):
    """节头的排序开关,与卡片尾部那一行的展开。

    那一行原来是 div,只报数、点不动:卡片写着「搜索到 37 处」,能看的只有两条
    (2026-09-04 用户实见)。
    """

    def setUp(self):
        self.src = open(os.path.join(ROOT, "frontend", "app-rail-nav.js"), encoding="utf-8").read()
        self.css = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()

    def test_更多那一行是按钮不是死字(self):
        self.assertIn('document.createElement("button"); more.className = "ss-more"', self.src)
        self.assertIn("rest.forEach", self.src)

    def test_默认只露两条_其余藏着(self):
        self.assertIn("if (i < 2) card.appendChild(row);", self.src)
        self.assertIn("row.hidden = true; rest.push(row)", self.src)

    def test_展开不再打接口(self):
        """片段本来就在这次结果里;经隧道一个往返一两秒,不值得为展开再花一趟。"""
        i = self.src.index("more.onclick")
        self.assertNotIn("api(", self.src[i:i + 400])

    def test_留存不足时说清其余在哪看(self):
        """后端每段最多留十条,更多的只报数。摊开却仍不全,不说明比不给展开更让人疑惑。"""
        self.assertIn("其余 ${left()} 处打开会话查看", self.src)

    def test_排序开关切换后重新问后端(self):
        """命中的先后由 SQL 定,前端手上只有前十条,拿它重排得出的是「前十条里最新的」。"""
        i = self.src.index("const sortBox")
        seg = self.src[i:i + 900]
        self.assertIn("_sideSearchSort = v", seg)
        self.assertIn("applySideFilter()", seg)
        self.assertIn('_sideSearchKey = ""', seg, "键不清的话查询没变会被拦住,切了没反应")

    def test_排序进请求也进缓存键(self):
        self.assertIn("&sort=${encodeURIComponent(_sideSearchSort)}", self.src)
        self.assertIn('${shepherdMode ? "shepherd" : curEngine}\\n${_sideSearchSort}\\n${q}', self.src)

    def test_偏好落盘(self):
        self.assertIn('localStorage.setItem("cd_side_search_sort"', self.src)
        self.assertIn('localStorage.getItem("cd_side_search_sort")', self.src)

    def test_两枚样式都在(self):
        for sel in (".ss-sort-b.on", ".ss-more:hover", ".ss-more:disabled"):
            self.assertIn(sel, self.css)
