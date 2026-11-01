"""查看器就地编辑(assets._wb_view_write + 页面编辑栏):可写范围、拒写类型、页面挂载。

写入范围与只读查看同源(_view_roots 限定),不额外放开;目录/图片/不存在一律拒写。
REPO_ROOT 逐用例 patch(不靠 argv):deskcfg 是进程级单例,全量跑时先 import 的测试文件说了算,
写死 argv 会在单跑通过、全量跑失败。
跑:python3 -m pytest tests/test_view_edit.py
"""
import os, sys, tempfile, shutil, unittest

# argv 只为「本文件先 import deskcfg」时能过初始化;真实根一律在 setUp 里 patch
sys.argv = sys.argv if len(sys.argv) >= 4 else ["test", tempfile.mkdtemp(), "0", "0"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import assets                        # noqa: E402


class _RootedCase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="viewedit-")
        self.repo = os.path.join(self.base, "repo_root")
        os.makedirs(self.repo, exist_ok=True)
        self._orig = assets.REPO_ROOT
        assets.REPO_ROOT = self.repo    # _view_roots / _wb_view_resolve 都在调用时取模块全局

    def tearDown(self):
        assets.REPO_ROOT = self._orig
        shutil.rmtree(self.base, ignore_errors=True)

    def w(self, rel, text="# 标题\n\n正文\n"):
        p = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p


class ViewWriteScope(_RootedCase):
    def test_writes_existing_text_file_in_workspace(self):
        self.w("docs/note.md")
        self.assertTrue(assets._wb_view_write("docs/note.md", "# 改过了\n"))
        with open(os.path.join(self.repo, "docs/note.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "# 改过了\n")

    def test_writes_plain_text_too(self):
        self.w("a.txt", "old\n")
        self.assertTrue(assets._wb_view_write("a.txt", "new\n"))
        with open(os.path.join(self.repo, "a.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "new\n")

    def test_远端形态拒绝工作区外的路径(self):
        """判据 2026-08-19 从「路径在不在某个根里」换成「这一份 Desk 是本机还是远端」。

        远端形态下接口经隧道对外,持令牌者可读可写的范围就是白名单放开的范围,
        ~/.ssh 与各类凭证都在其中,所以那一档必须继续拦。"""
        outside = os.path.join(self.base, "outside.md")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("原文\n")
        import remote_auth
        _orig = remote_auth.role
        remote_auth.role = lambda: "remote"
        try:
            self.assertFalse(assets._wb_view_write(outside, "篡改\n"))
        finally:
            remote_auth.role = _orig
        with open(outside, encoding="utf-8") as f:
            self.assertEqual(f.read(), "原文\n")

    def test_本机形态放开工作区外的路径(self):
        """本机是用户自己的机器、自己的文件,终端里 cat 一下就能看,查看器拦着只制造摩擦
        ——新 clone 且已被工作流引用的仓打不开,就是这么来的。"""
        outside = os.path.join(self.base, "outside2.md")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("原文\n")
        import remote_auth
        _orig = remote_auth.role
        remote_auth.role = lambda: "local"
        try:
            self.assertTrue(assets._wb_view_write(outside, "改过\n"))
        finally:
            remote_auth.role = _orig
        with open(outside, encoding="utf-8") as f:
            self.assertEqual(f.read(), "改过\n")

    def test_判不出角色时按远端处理(self):
        """宁可严一档:判据本身出问题时不该顺带把整盘放开。"""
        outside = os.path.join(self.base, "outside3.md")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("原文\n")
        import remote_auth
        _orig = remote_auth.role
        def _boom(): raise RuntimeError("判不出")
        remote_auth.role = _boom
        try:
            self.assertFalse(assets._wb_view_write(outside, "篡改\n"))
        finally:
            remote_auth.role = _orig

    def test_refuses_directory_and_missing(self):
        os.makedirs(os.path.join(self.repo, "adir"), exist_ok=True)
        self.assertFalse(assets._wb_view_write("adir", "x"))
        self.assertFalse(assets._wb_view_write("nope/missing.md", "x"))

    def test_refuses_image(self):
        # 图片视图不挂编辑栏,后端也拒写:防前端漏挡时把二进制写成文本
        p = self.w("pic.png", "")
        with open(p, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        self.assertFalse(assets._wb_view_write("pic.png", "文本"))

    def test_does_not_create_new_file(self):
        # 只改已打开的文件,不新建(与文件浏览器 _fs_write_file 的语义不同)
        self.assertFalse(assets._wb_view_write("docs/brand-new.md", "x"))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "docs/brand-new.md")))


class ViewPageMountsEditor(_RootedCase):
    def html(self, rel):
        res = assets._wb_view(rel)
        self.assertIsNotNone(res, f"{rel} 未渲染出页面")
        return res[1].decode("utf-8")

    def test_md_page_has_edit_bar_and_rerender_hook(self):
        self.w("docs/note.md")
        h = self.html("docs/note.md")
        for frag in ('id="vw-edit"', 'id="vw-save"', 'id="vw-ed"', "window.__vwRender"):
            self.assertIn(frag, h)
        self.assertIn('class="md vw-view"', h)   # 编辑态靠 body.editing 隐藏渲染区

    def test_buttons_sit_right_after_the_path(self):
        # 按钮紧跟路径末尾、随文字流走(inline),不是撑满一行贴到最右;中线靠 vertical-align 对齐
        self.w("docs/note.md")
        h = self.html("docs/note.md")
        head = h.split('<div class="vw-head">', 1)[1].split("</div>" + '<textarea', 1)[0]
        self.assertIn("fb-crumb", head)
        self.assertIn('id="vw-edit"', head)
        self.assertIn(".vw-head .fb-crumb,.vw-head .crumb{display:inline", h)
        self.assertIn("vertical-align:middle", h)
        self.assertNotIn(".vw-bar{flex:0 0 auto", h)   # 旧的「靠最右」布局不该回潮

    def test_editor_fills_viewport_height(self):
        # 编辑态 textarea 吃满剩余高度到底,不留大片空白
        self.w("docs/note.md")
        h = self.html("docs/note.md")
        self.assertIn("body.editing .wrap{display:flex", h)
        self.assertIn("body.editing #vw-ed{flex:1", h)

    def test_mobile_editor_keeps_real_viewport_without_focus_zoom(self):
        self.w("docs/note.md")
        h = self.html("docs/note.md")
        self.assertIn("name=viewport", h)
        self.assertIn("width=device-width,initial-scale=1,viewport-fit=cover", h)
        self.assertIn("#vw-ed{font-size:16px;line-height:1.65}", h)
        self.assertIn("body.editing .wrap{height:100dvh", h)

    def test_md_crumb_style_matches_rendered_class(self):
        # crumb() 产出的是 .fb-crumb;CSS 选择器要对得上,否则面包屑吃浏览器默认链接色
        self.w("docs/note.md")
        h = self.html("docs/note.md")
        self.assertIn(".fb-crumb a{color:", h)

    def test_text_page_has_edit_bar(self):
        self.w("a.txt", "hello\n")
        h = self.html("a.txt")
        self.assertIn('id="vw-edit"', h)
        self.assertIn('id="vw-pre"', h)
        self.assertIn("window.__vwRender", h)

    def test_dir_and_image_pages_have_no_editor(self):
        os.makedirs(os.path.join(self.repo, "adir"), exist_ok=True)
        self.w("adir/x.md")
        self.assertNotIn('id="vw-edit"', self.html("adir"))
        p = self.w("pic2.png", "")
        with open(p, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        self.assertNotIn('id="vw-edit"', self.html("pic2.png"))

    def test_directory_page_follows_host_theme_and_has_readable_light_colors(self):
        """目录页是同源 iframe；浅色不能继续沿用深色的 #eaeaea，变成白底白字。"""
        os.makedirs(os.path.join(self.repo, "adir"), exist_ok=True)
        self.w("adir/x.md")
        h = self.html("adir")
        self.assertIn("parent.document.documentElement.dataset.theme", h)
        self.assertIn(":root[data-theme='light']{--view-text:#333333", h)
        self.assertIn("color:var(--view-text)", h)
        self.assertIn("new MutationObserver", h)
        self.assertIn("attributeFilter:['data-theme']", h)
        self.assertLess(h.index("parent.document.documentElement.dataset.theme"), h.index("<style>"))

    def test_source_escapes_script_close(self):
        # 正文含 </script> 时不能截断页面(与只读渲染同一条防线)
        self.w("docs/x.md", "正文 </script><b>x</b>\n")
        self.assertNotIn("</script><b>x</b>", self.html("docs/x.md"))


if __name__ == "__main__":
    unittest.main()
