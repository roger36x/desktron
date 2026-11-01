"""差异比的是 git 版本,不是磁盘;编辑不再是一种模式。

**差异。** 原来比「当前缓冲区」与「磁盘上的那一份」。改了没存时它说得出话,存下之后两边
一模一样,一句也说不出——而后一种恰恰是平时打开差异的时刻。改成比 git HEAD:回答的是
「相对上次提交,这份文件改了什么」(2026-08-19 用户提)。不在仓里、或那个版本里还没有这份
文件时,左边给空、整份读作新增,并把缘由说一句。

**编辑。** VS Code 里文件本来就是可编辑的,双击那一下管的是「标签从预览转常驻」,不是解锁
编辑;它没有「编辑模式」,自然也没有「什么时候退出」。原来这里打开即只读、点「编辑」才解锁,
于是多出一个按钮和一个说不清的问题。现在直接可编辑:改动不落盘就不生效,脏标记摆在标签与
面包屑上,保存走 Cmd+S 或那枚「保存」,关掉带改动的标签会拦一次。

注:vendor/ 那份打包产物不入 git,这里钉的是源码 src/entry.js 与后端。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENTRY = open(os.path.join(ROOT, "frontend", "app-assets", "files", "src", "entry.js"),
             encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "frontend", "pages", "files.html"), encoding="utf-8").read()
ASSETS = open(os.path.join(ROOT, "desktron", "lib", "assets.py"), encoding="utf-8").read()
CORE = open(os.path.join(ROOT, "desktron", "lib", "core", "api.py"), encoding="utf-8").read()


class GitDiff(unittest.TestCase):
    def test_前端取的是_git_那一份(self):
        seg = re.search(r"async function diffOpen\(\)[\s\S]+?\n}", ENTRY).group(0)
        self.assertIn("&git=HEAD", seg)
        self.assertNotIn("&file=1", seg)   # 磁盘那一份已经不比了

    def test_取不到版本时说一句(self):
        seg = re.search(r"async function diffOpen\(\)[\s\S]+?\n}", ENTRY).group(0)
        self.assertIn("if (note) msg(note);", seg)

    def test_后端按仓根解析路径(self):
        seg = re.search(r"def _fs_git_file\([\s\S]+?\n\ndef ", ASSETS).group(0)
        self.assertIn('git("rev-parse", "--show-toplevel")', seg)
        self.assertIn("os.path.relpath(full, top)", seg)

    def test_版本表达式要过滤(self):
        """rev 从客户端来,不能原样拼进命令行。"""
        seg = re.search(r"def _fs_git_file\([\s\S]+?\n\ndef ", ASSETS).group(0)
        self.assertIn('re.fullmatch(r"[\\w./^~@{}-]{1,64}", rev)', seg)

    def test_没有可比版本时给空正文加说明(self):
        seg = re.search(r"def _fs_git_file\([\s\S]+?\n\ndef ", ASSETS).group(0)
        self.assertIn('return "", "这个目录不在 git 仓里"', seg)
        self.assertIn("里还没有这份文件", seg)

    def test_端点接上了(self):
        self.assertIn('if (params.get("git") or [""])[0]:', CORE)
        self.assertIn("_fs_git_file(root_id, rel, rev)", CORE)
        self.assertIn("_fs_git_file", CORE.split("\n")[7])   # 导入那一行


class NoEditMode(unittest.TestCase):
    def test_编辑钮已撤(self):
        self.assertNotIn("btn-edit", HTML)
        self.assertNotIn("btn-edit", ENTRY)

    def test_编辑器不再只读(self):
        seg = re.search(r"function initEditor\(\)[\s\S]+?\n}", ENTRY).group(0)
        self.assertNotIn("readOnly: true", seg)

    def test_切标签不再带只读(self):
        seg = re.search(r"function activate\(rel\)[\s\S]+?\n}", ENTRY).group(0)
        self.assertNotIn("readOnly", seg)
        self.assertIn("unicodeHighlight: unicodeOpt(", seg)   # 那一项还在

    def test_存完不回到只读(self):
        seg = re.search(r"async function doSave\(\)[\s\S]+?\n}", ENTRY).group(0)
        self.assertNotIn("readOnly", seg)

    def test_保存取消只在有改动时出现(self):
        seg = re.search(r"function paintBar\(\)[\s\S]+?\n}", ENTRY).group(0)
        self.assertIn('$("#btn-save").hidden = !current || !isDirty()', seg)
        self.assertIn('$("#btn-cancel").hidden = !current || !isDirty()', seg)

    def test_带改动关标签仍要拦一次(self):
        """没有编辑模式之后,唯一挡住误删改动的就是这一问。"""
        self.assertIn("还有未保存的改动,关掉就丢了", ENTRY)


if __name__ == "__main__":
    unittest.main()
