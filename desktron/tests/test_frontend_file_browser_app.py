"""对话文件链接统一进入极简文件浏览器，并保留 Monaco IDE 作为编辑出口。"""
import hashlib
import os
import re
import shutil
import struct
import subprocess
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class FileBrowserAppContractTest(unittest.TestCase):
    def test_file_browser_is_shipped_as_an_official_app(self):
        import json
        data = json.loads(read("frontend/workbench.json"))
        app = next(a for a in data["desktopApps"] if a["id"] == "file-browser")
        self.assertEqual(app["uid"], "desktron.file-browser.93a69f")
        self.assertEqual(app["page"], "file-browser")
        self.assertEqual(app["icon"], "/icons/app-file-browser-original-7710531b884e.png")
        self.assertEqual(app["agent_operability"], "none")
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "frontend/pages/file-browser.html")))

    def test_file_browser_icon_matches_the_requested_original(self):
        with open(os.path.join(ROOT, "frontend/icons/app-file-browser-original-7710531b884e.png"), "rb") as f:
            png = f.read()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", png[16:24]), (512, 512))
        self.assertEqual(hashlib.sha256(png).hexdigest(),
                         "7710531b884eff6ff04a970f0ef51fd3dd36a6c0bcb386189e8834246e6b54fb")
        license_text = read("frontend/vendor/ICONS-LICENSE.txt")
        for text in ("Desktron 原创文件图标", "独立 SVG", "frontend/icons/app-file-browser-original-7710531b884e.png"):
            self.assertIn(text, license_text)

    def test_file_browser_icon_keeps_lowered_front_and_rounds_papers(self):
        import xml.etree.ElementTree as ET
        from PIL import Image

        base = ET.fromstring(read("frontend/icons/app-file-browser-original-d8f988ab8b2b.svg"))
        current = ET.fromstring(read("frontend/icons/app-file-browser-original-7710531b884e.svg"))
        before, after = list(base.iter()), list(current.iter())
        self.assertEqual(len(before), len(after))
        for old, new in zip(before, after):
            if old.tag.endswith("title") or old is before[-1]:
                continue
            expected = dict(old.attrib)
            if old.tag.endswith("rect"):
                expected["rx"] = "8"
            self.assertEqual((old.tag, expected), (new.tag, new.attrib))
        self.assertEqual(after[-1].attrib, {
            "d": "M0 56Q0 44 12 44H116Q128 44 128 56V128H0Z", "fill": "#FDBA21"})
        with Image.open(os.path.join(ROOT, "frontend/icons/app-file-browser-original-7710531b884e.png")) as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getpixel((0, 0))[3], 0)
            self.assertEqual(image.getpixel((256, 175))[:3], (255, 255, 255))
            self.assertEqual(image.getpixel((256, 177))[:3], (253, 186, 33))

    def test_file_browser_icon_has_no_added_tile_or_shrinking(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node unavailable")
        src = read("frontend/app-assets-apps.js")
        render = re.search(r"\nfunction _appIconHTML\(.*?\n}\n", src, re.S).group(0)
        script = "const escapeHtml = s => s;\n" + render + '''
for (const extra of ["", "sm"]) {
  const html = _appIconHTML("文件浏览器", "desktron.file-browser.93a69f", extra, "", "/icons/app-file-browser-original-7710531b884e.png");
  if (!html.includes('<img src="/icons/app-file-browser-original-7710531b884e.png" alt="">') || html.includes('fit-tile'))
    throw new Error(html);
}
'''
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
        css = read("frontend/style.css")
        self.assertNotIn("fit-tile", css)
        self.assertIn("object-fit: contain", css)
        # 应用列表、程序坞及最近应用共用清单的同一图片。
        self.assertIn('_appIconHTML(a.label, a.uid || a.id || a.label, "sm", a.desc, a.icon)', src)
        self.assertEqual(src.count('_appIconHTML(x.a.label, x.key, "", x.a.desc, x.a.icon)'), 2)

    def test_file_browser_is_a_singleton_app(self):
        src = read("frontend/app-files.js")
        self.assertIn('const FILE_BROWSER_PAGE_KEY = "file-browser"', src)
        self.assertIn('const FILE_BROWSER_STATE_KEY = "cd_file_browser"', src)
        self.assertIn('pages.find((x) => x.url === FILE_BROWSER_PAGE_KEY)', src)
        self.assertIn('postMessage({ type: "file-browser-open"', src)
        self.assertIn('appid: a.uid || a.id', src)
        self.assertIn('style="display:block;width:100%;height:100%;border:0"', src)
        self.assertIn('host.style.padding = "0"', src)

    def test_file_browser_restores_the_last_open_file_after_reload(self):
        host = read("frontend/app-files.js")
        page = read("frontend/pages/file-browser.html")
        self.assertIn("localStorage.getItem(FILE_BROWSER_STATE_KEY)", host)
        self.assertIn("localStorage.setItem(FILE_BROWSER_STATE_KEY", host)
        self.assertIn("if (fileBrowserCurrent.path)", host)
        self.assertIn("parent.fileBrowserRemember(root, current, currentLine)", page)

    def test_directory_target_is_persisted_and_revealed_in_tree(self):
        host = read("frontend/app-files.js")
        page = read("frontend/pages/file-browser.html")
        self.assertIn("if (x.dir) u += `&dir=1`", host)
        self.assertIn("function openFileBrowser(root, path = \"\", line = \"\", show = true, dir = false)", host)
        self.assertIn('let currentIsDir = q.get("dir") === "1"', page)
        self.assertIn('const selector = currentIsDir ? ".tree-row.dir" : ".tree-row.file"', page)
        self.assertIn("if (currentIsDir) openApps = []", page)

    def test_app_launcher_uses_file_browser_host(self):
        src = read("frontend/app-assets-apps.js")
        self.assertIn('a.page === "file-browser"', src)
        self.assertIn('openFileBrowserApp(!compact, domainId)', src)

    def test_chat_links_prefer_browser_and_keep_fallback(self):
        src = read("frontend/app-chat-refs.js")
        start = src.index("async function hsOpenFile")
        end = src.index("async function hsRewind", start)
        fn = src[start:end]
        self.assertIn('openFileBrowser(loc.root, loc.rel, line || "", true)', fn)
        self.assertNotIn("fileBrowserMobilePending", fn)
        self.assertIn("openContentPage(key, name", fn)
        self.assertLess(fn.index("openFileBrowser("), fn.index("openContentPage(key, name"))

    def test_open_with_menu_only_uses_declared_file_capabilities(self):
        import json
        data = json.loads(read("frontend/workbench.json"))
        apps = {a["id"]: a for a in data["desktopApps"]}
        self.assertEqual(apps["files"]["file_open"]["accept"], ["*"])
        self.assertIn("html", apps["browser"]["file_open"]["accept"])
        self.assertNotIn("file_open", apps["taskpaper"])

        host = read("frontend/app-files.js")
        self.assertIn("function fileOpenCandidates(path)", host)
        self.assertIn("const cap = a && a.file_open", host)
        self.assertIn('["ide", "browser"].includes(cap.handler)', host)
        self.assertIn("function openFileInApp(key, root, rel)", host)
        self.assertIn("fileOpenCandidates(rel).find", host)

    def test_open_with_menu_remembers_the_last_app_that_opened_a_file(self):
        host = read("frontend/app-files.js")
        page = read("frontend/pages/file-browser.html")
        self.assertIn('const FILE_OPEN_LAST_APP_KEY = "cd_file_open_last_app"', host)
        self.assertIn("localStorage.setItem(FILE_OPEN_LAST_APP_KEY, app.key)", host)
        self.assertIn('const lastOpenAppKey = "cd_file_open_last_app"', page)
        self.assertIn("localStorage.getItem(lastOpenAppKey)", page)
        self.assertIn('openApps.find((a) => a.handler === "ide")', page)
        self.assertIn('id="open-app-menu"', page)
        self.assertIn('id="open-app-list"', page)

    def test_open_with_image_icon_is_bounded_inside_the_iframe(self):
        page = read("frontend/pages/file-browser.html")
        self.assertIn("#open-app .ap-ic img, .open-app-option .ap-ic img", page)
        self.assertIn("max-width: 15px; max-height: 15px", page)
        self.assertIn("justify-content: center; overflow: hidden", page)

    def test_tree_heading_is_emphasized(self):
        page = read("frontend/pages/file-browser.html")
        self.assertIn("color: var(--text); font-size: 13px; font-weight: 600", page)

    def test_markdown_tables_match_conversation_reading_style(self):
        page = read("frontend/pages/file-browser.html")
        self.assertIn(".doc table {", page)
        self.assertIn("display: block; overflow-x: auto", page)
        self.assertIn("border: 0; border-bottom: 1px solid var(--line)", page)
        self.assertIn(".doc tr:last-child td { border-bottom: 0; }", page)
        self.assertIn(".doc th code, .doc td code", page)
        self.assertNotIn(".doc th, .doc td { padding: 7px 9px; border: 1px solid var(--line)", page)

    def test_compact_layout_contract_defaults_the_tree_closed(self):
        page = read("frontend/pages/file-browser.html")
        self.assertIn("const treeVisible = { full: true, compact: false };", page)
        self.assertIn('const next = mode === "compact" ? "compact" : "full";', page)
        self.assertIn('postMessage({ type: "desktron-layout", mode }', read("frontend/app-pages.js"))

    def test_mobile_links_use_the_full_file_page(self):
        host = read("frontend/app-files.js")
        page = read("frontend/pages/file-browser.html")
        css = read("frontend/style.css")
        self.assertNotIn("fileBrowserMobilePreview", host)
        self.assertNotIn("file-browser-preview", css)
        self.assertNotIn("compactPreview", page)
        self.assertIn("Promise.all([restoreTree(), renderFile()])", page)

    def test_initial_iframe_load_does_not_repeat_the_same_open_request(self):
        host = read("frontend/app-files.js")
        self.assertIn("if (!same) f.contentWindow.postMessage", host)

    def test_tree_width_can_be_dragged_and_restored(self):
        page = read("frontend/pages/file-browser.html")
        self.assertIn('id="side-divider" role="separator"', page)
        self.assertIn('const sideWidthKey = "cd_file_browser_side_width"', page)
        self.assertIn('sideDivider.addEventListener("pointerdown"', page)
        self.assertIn('sideDivider.addEventListener("pointermove"', page)
        self.assertIn('localStorage.setItem(sideWidthKey', page)
        self.assertIn('body.side-off #side-divider { display: none; }', page)

if __name__ == "__main__":
    unittest.main()
