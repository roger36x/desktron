"""会话里点开一条绝对路径,不能把开头的斜杠弄丢。

链接路由(app-core.js)在交给 hsOpenFile 之前要剥掉相对前缀。原来写的是 `/^\\.?\\//`——
那个点是可选的,于是绝对路径的根斜杠也被一起吃掉:`/Users/…/orientation.md` 变成
`Users/…`,hsResolveFile 认不出它是绝对路径,退去仓内找一个叫 `Users` 的顶层目录,
自然找不到,页面上就是「没在已登记的代码仓里找到这个路径」(2026-08-19 用户实见,
页头显示的路径正好少了那个斜杠)。

只剥 `./` 又会碰到另一种写法:文档里的 `/docs/x.md` 说的是仓内的根相对位置,不是磁盘根。
两种都要能开,所以判据放在 hsResolveFile:按绝对路径先验一次,磁盘上没有再剥掉斜杠
按仓内路径找。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CORE = open(os.path.join(ROOT, "frontend", "app-core.js"), encoding="utf-8").read()
CHAT = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


class KeepsLeadingSlash(unittest.TestCase):
    def test_路由只剥点斜杠(self):
        m = re.search(r"hsOpenFile\(href\.replace\((/\^[^,]+)/, \"\"\)", CORE)
        self.assertTrue(m, "链接路由那一行不见了")
        self.assertIn(r"/^\.\//", m.group(0))
        # 可选的那个点正是缺陷本身。只看代码行,注释里为了讲清缘由照写了原样的那个正则。
        code = "\n".join(ln for ln in CORE.splitlines() if not ln.lstrip().startswith("//"))
        self.assertNotIn(r"/^\.?\//", code)

    def _seg(self):
        return re.search(r"async function hsResolveFile\(path, source\)[\s\S]+?\n  const top", CHAT).group(0)

    def test_绝对路径先验一次(self):
        seg = self._seg()
        self.assertIn('path.startsWith("/")', seg)
        self.assertIn("/workbench/fs?root=", seg)
        # 验过存在之后再把根升到 .git 那一层(见 test_frontend_repo_root_open.py)
        self.assertIn("return await hsRepoRoot(abs);", seg)

    def test_家目录写法同样按绝对路径处理(self):
        """`~/…` 只可能是家目录。原来只认斜杠开头,它会被当成仓内相对路径去拼,
        于是明明存在的文件报找不到(2026-08-19 实见)。后端两侧本来都 expanduser。"""
        seg = self._seg()
        self.assertIn('path.startsWith("~/")', seg)
        # 退回仓内那一步要剥掉 "~/" 两个字符;按 slice(1) 剥会留下 "/work/…",两头都找不到
        self.assertIn('path.startsWith("~/") ? path.slice(2)', seg)

    def test_不存在就退回仓内再找(self):
        """/docs/x.md 这种根相对写法不能因为磁盘上没有 /docs 就直接判死。"""
        seg = self._seg()
        self.assertIn("path.slice(1)", seg)
        self.assertLess(seg.index("return await hsRepoRoot(abs);"), seg.rindex("path.slice(1)"))

    def test_markdown链接的行号不混进文件名(self):
        """可点击文件链接使用 `/abs/file.html:10`，路由应把 10 作为行号传递。"""
        self.assertIn("function hsOpenFileHref(href, source)", CHAT)
        self.assertRegex(CHAT, r"suffix\s*=\s*path\.match\(")
        self.assertIn("path = suffix[1]; line = suffix[2];", CHAT)
        self.assertIn("hsOpenFileHref(href, a)", CORE)

    def test_绝对路径不要求登记到工作流(self):
        """任意已存在的本地绝对路径直接以其父目录作为文件根。"""
        seg = self._seg()
        self.assertIn('const abs = { root: "abs:" + path.slice(0, i)', seg)
        self.assertIn("return await hsRepoRoot(abs);", seg)
        self.assertNotIn("/workbench/domains", seg)


class 整条路径不被推出可视区(unittest.TestCase):
    """把一条绝对路径整个交给人时,它要能被看全、被复制全。

    路径片默认 `white-space: nowrap`,理由是句中的引用连着读更清楚。但整条绝对路径的
    宽度本来就超过正文那一列——无头浏览器在 720px 的正文列里实测,截图里那条路径渲染
    出 937px,右边 217px 落在可视区外(2026-09-10 用户实见:行尾停在 `…2026-09-10.m`)。
    这种情形下不断行并没有换来「连着读」,只是让人看不见也拿不全。

    所以按用途分,而不是按长度设阈值:整条交出去的(hsFileLink 的 full 支、以 / 或 ~
    开头的行内码路径)放开断行,夹在句子里的相对引用仍不断行。改后同一条路径实测溢出 0。
    """

    def test_整条交出去的那种放开断行(self):
        self.assertIn('a.className = "hs-file" + (full ? " hs-path-full" : "");', CHAT)
        self.assertIn('if (/^[~/]/.test(txt)) c.classList.add("hs-path-full");', CHAT)
        self.assertIn(".hs-page .hs-file.hs-path-full,\n"
                      ".hs-page .hs-text.md code.hs-path.hs-path-full "
                      "{ white-space: normal; overflow-wrap: anywhere; }", CSS)

    def test_句中的相对引用仍连着读(self):
        """放开断行的代价是路径会在斜杠与连字符处折行。短引用本来就放得下,不必付这个代价。"""
        self.assertIn(".hs-page .hs-file { color: var(--accent); text-decoration: none; "
                      "white-space: nowrap; }", CSS)
        self.assertIn("white-space: nowrap; }", CSS[CSS.index("code.hs-path {"):][:220])


if __name__ == "__main__":
    unittest.main()
