"""自动遍历不进要系统授权的位置(macOS TCC)。

文稿、下载、桌面、音乐、照片、~/Library、外置卷这些位置,进去一次就弹一个系统授权框,
而框上署的是 Desk 的名字——Desk 起的子进程都算在它头上。目录树展开时会挨个进子目录探
有没有内容,根一旦落在家目录上,一次展开就连弹好几个(2026-09-09 用户实见)。

跑:python3 -m pytest tests/test_fs_protected_dirs.py
"""
import os, sys, tempfile, shutil, unittest

sys.argv = sys.argv if len(sys.argv) >= 4 else ["test", tempfile.mkdtemp(), "0", "0"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import assets                        # noqa: E402
import deskcfg                       # noqa: E402
import protected_paths               # noqa: E402


class ProtectedList(unittest.TestCase):
    def test_the_positions_macos_asks_about_are_all_listed(self):
        want = (["desktop", "documents", "downloads", "music", "pictures", "videos", "appdata", "onedrive"]
                if os.name == 'nt' else
                ["Desktop", "Documents", "Downloads", "Music", "Pictures", "Movies", "Library"])
        tail = [os.path.basename(d) for d in deskcfg.PROTECTED_DIRS]
        for n in want:
            self.assertIn(n, tail, n)
        if os.name != 'nt':
            self.assertIn("/Volumes", deskcfg.PROTECTED_DIRS)

    def test_macos_agent_list_covers_explicit_file_tool_roots(self):
        home = os.path.join(tempfile.gettempdir(), "desk-protected-home")
        broad = protected_paths.protected_dirs(platform="darwin", home=home)
        self.assertIn(os.path.join(home, "Library"), broad)
        self.assertIn(os.path.join(home, "Downloads"), broad)
        self.assertIn(os.path.join(home, "Desktop"), broad)

    def test_membership_covers_the_things_inside_them(self):
        self.assertTrue(deskcfg.is_protected_dir("~/Music"))
        self.assertTrue(deskcfg.is_protected_dir("~/Music/某个资料库"))
        self.assertTrue(deskcfg.is_protected_dir(
            "~/AppData/Local/com.x.y" if os.name == 'nt' else "~/Library/Containers/com.x.y"))
        self.assertFalse(deskcfg.is_protected_dir("~/work/desktron"))
        # 名字前缀相同但不是同一个目录,不能误判
        self.assertFalse(deskcfg.is_protected_dir(os.path.expanduser("~/Documents-old")))
        self.assertFalse(deskcfg.is_protected_dir(""))


class Walking(unittest.TestCase):
    """真实家目录不能进测试,所以把 tmp 下的一个子目录当成受保护位置。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="fsprot-")
        self.guard = os.path.join(self.base, "音乐")
        os.makedirs(os.path.join(self.guard, "内层"), exist_ok=True)
        os.makedirs(os.path.join(self.base, "工作"), exist_ok=True)
        with open(os.path.join(self.guard, "内层", "命中.txt"), "w", encoding='utf-8') as f:
            f.write("关键词\n")
        with open(os.path.join(self.base, "工作", "命中.txt"), "w", encoding='utf-8') as f:
            f.write("关键词\n")
        self._orig = assets.is_protected_dir
        assets.is_protected_dir = lambda p: os.path.abspath(str(p)).startswith(self.guard)

    def tearDown(self):
        assets.is_protected_dir = self._orig
        shutil.rmtree(self.base, ignore_errors=True)

    def test_name_search_does_not_descend_into_them(self):
        got = [x["path"] for x in assets._fs_search("abs:" + self.base, "命中")["items"]]
        self.assertEqual(got, [os.path.join("工作", "命中.txt")])

    def test_content_search_does_not_descend_into_them(self):
        got = assets._fs_grep("abs:" + self.base, "关键词")
        self.assertEqual([x["path"] for x in got["items"]], [os.path.join("工作", "命中.txt")])

    def test_the_tree_still_offers_them_but_does_not_open_them(self):
        opened = []
        orig = os.scandir
        os.scandir = lambda p, *a, **k: (opened.append(p), orig(p, *a, **k))[1]
        try:
            self.assertTrue(assets._fs_has_visible_children(self.guard))
            self.assertEqual(opened, [])          # 探一下就等于打开它,系统就是在这里弹框
            self.assertTrue(assets._fs_has_visible_children(os.path.join(self.base, "工作")))
            self.assertEqual(len(opened), 1)
        finally:
            os.scandir = orig

    def test_a_root_the_user_opened_there_himself_still_searches(self):
        # 用户自己把文件应用开在受保护目录上:权限已经问过,再绕就成了「打开的目录搜不到东西」
        got = [x["path"] for x in assets._fs_search("abs:" + self.guard, "命中")["items"]]
        self.assertEqual(got, [os.path.join("内层", "命中.txt")])


if __name__ == "__main__":
    unittest.main()
