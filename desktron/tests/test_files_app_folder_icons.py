"""IDE 文件树使用原创开合 PNG，保留展开、空目录与排版状态。"""
import os
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENTRY = open(os.path.join(ROOT, "frontend", "app-assets", "files", "src", "entry.js"),
             encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "frontend", "pages", "files.html"), encoding="utf-8").read()
ASSETS = open(os.path.join(ROOT, "desktron", "lib", "assets.py"), encoding="utf-8").read()


class FolderStateContract(unittest.TestCase):
    def test_后端返回目录是否有可见内容(self):
        self.assertIn("def _fs_has_visible_children(path):", ASSETS)
        self.assertIn('"has_children": _fs_has_visible_children(p)', ASSETS)
        self.assertIn("if entry.name in _FS_SKIP:", ASSETS)
        self.assertIn("if entry.is_dir() or entry.is_file():", ASSETS)

    def test_前端组合打开关闭与有内容空目录四态(self):
        self.assertIn('open ? "is-open" : "is-closed"', ENTRY)
        self.assertIn('hasChildren ? "has-children" : "is-empty"', ENTRY)
        self.assertIn('it.has_children !== false', ENTRY)
        self.assertIn("function twistyIcon(type, open = false, hasChildren = false)", ENTRY)
        self.assertIn("const isOpen = expanded.has(it.path)", ENTRY)
        self.assertIn("icon(it.type, it.name, isOpen, hasChildren)", ENTRY)
        self.assertIn('icon("dir", it.name, !on, hasChildren)', ENTRY)

    def test_开合切换独立素材且保留空目录状态(self):
        import json
        import re
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node unavailable")
        fn = re.search(r"function folderIcon\(.*?\n}\n", ENTRY, re.S).group(0)
        script = fn + "console.log(JSON.stringify([folderIcon(false,true),folderIcon(true,true),folderIcon(false,false),folderIcon(true,false)]));"
        result = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
        rendered = json.loads(result.stdout)
        for index, markup in enumerate(rendered):
            expected = "ide-folder-open-original-b95c700a1abe.png" if index % 2 else "ide-folder-closed-original-905d2778fcb2.png"
            self.assertIn(expected, markup)
            self.assertIn("has-children" if index < 2 else "is-empty", markup)
        self.assertNotEqual(rendered[0], rendered[1])



class SpecifiedFolderAppearance(unittest.TestCase):
    def test_彩色原图等比呈现_不残留旧绘制样式(self):
        self.assertIn('width: 15px; height: 15px; object-fit: contain; filter: none', HTML)
        self.assertNotIn('--folder-back:', HTML)
        self.assertNotIn('.folder-front', HTML)

    def test_原创资源与可编辑源图(self):
        import hashlib
        from pathlib import Path
        icons = {'ide-folder-closed-original-905d2778fcb2.png': '905d2778fcb2f600152afd35134aa34fe0a5653ef63373c4bfa9b2c8d3daadf2', 'ide-folder-open-original-b95c700a1abe.png': 'b95c700a1abe9dcaf1d8d7c0a650f3ceb0963ae4a6da98a5416d9d557c0f70e0'}
        license_text = Path(ROOT, "frontend/vendor/ICONS-LICENSE.txt").read_text(encoding="utf-8")
        for name, digest in icons.items():
            path = Path(ROOT, "frontend/icons", name)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            self.assertTrue(path.with_suffix(".svg").is_file())
            self.assertIn(name, license_text)



class TreeStructure(unittest.TestCase):
    def test_树行有独立折叠箭头槽(self):
        self.assertIn('${twistyIcon(it.type, isOpen, hasChildren)}<span class="ic">', ENTRY)
        self.assertIn('row.querySelector(".tw").outerHTML = twistyIcon("dir", !on, hasChildren)', ENTRY)
        self.assertIn(".row .tw {", HTML)
        self.assertIn(".row .tw.open svg { transform: rotate(90deg); }", HTML)

    def test_桌面和手机文件树都对齐文件浏览器密度(self):
        self.assertIn("row.style.paddingLeft = 7 + depth * 13 + \"px\"", ENTRY)
        self.assertIn("gap: 5px; min-height: 27px; padding: 0 7px", HTML)
        self.assertIn('font: 400 13px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif', HTML)
        self.assertIn('<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5">', ENTRY)
        self.assertIn('<path d="m4 2 4 4-4 4"/>', ENTRY)
        self.assertIn(".folder-icon { display: block; width: 15px; height: 15px", HTML)
        self.assertIn(".seti-file-icon {", HTML)
        self.assertIn('font: normal 16px/1 "Desk Seti"', HTML)
        self.assertNotIn("\n    .row { min-height: 44px", HTML)
        self.assertIn("#results .row { min-height: 44px", HTML)


if __name__ == "__main__":
    unittest.main()
