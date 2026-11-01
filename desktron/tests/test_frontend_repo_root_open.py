"""从会话点开文件,IDE 以 .git 那一层为根。

原来取的是文件所在的那一层(hsResolveFile 的绝对路径分支:root = 目录,rel = 文件名)。
后果是左树上只有同目录那几个文件,跨文件跳不动;差异也无从谈起——它比的是 git 版本,
而那一层未必是仓根(2026-08-19 用户提)。

相对路径由后端算,不在前端拼:根可能写成 `abs:~/work/…`,而 git 报的是展开后的绝对路径,
两边字符串在前端对不上。问的是目录本身(path 传空),所以同一个目录下的第二个文件不用再问。
不在仓里(比如 /tmp 下的临时文件)原样用,不强求。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
ASSETS = open(os.path.join(ROOT, "desktron", "lib", "assets.py"), encoding="utf-8").read()
CORE = open(os.path.join(ROOT, "desktron", "lib", "core", "api.py"), encoding="utf-8").read()


def js(name):
    m = re.search(r"\nasync function %s\(.*?\n}" % re.escape(name), CHAT, re.S)
    assert m, name
    return m.group(0)


class OpensAtRepoRoot(unittest.TestCase):
    def test_解析到文件后升到仓根(self):
        seg = js("hsResolveFile")
        self.assertIn("return await hsRepoRoot(abs);", seg)

    def test_只问目录_同目录第二个文件不再问(self):
        seg = js("hsRepoRoot")
        self.assertIn("&path=&gitroot=1", seg)
        self.assertIn("_hsRepoRoot.has(loc.root)", seg)   # 按目录缓存

    def test_不在仓里就原样用(self):
        seg = js("hsRepoRoot")
        self.assertIn("if (!hit) return loc;", seg)

    def test_相对路径按目录相对仓根拼(self):
        seg = js("hsRepoRoot")
        self.assertIn('rel: hit.dir ? hit.dir + "/" + loc.rel : loc.rel', seg)


class BackendAnswers(unittest.TestCase):
    def test_端点接上了(self):
        self.assertIn('if (params.get("gitroot") or [""])[0] == "1":', CORE)
        self.assertIn("_fs_git_root(root_id, rel)", CORE)

    def test_后端算相对路径而不是只给仓根(self):
        seg = re.search(r"def _fs_git_root\([\s\S]+?\n\ndef ", ASSETS).group(0)
        self.assertIn("os.path.relpath(target, top)", seg)

    def test_不拿家目录与文件系统根当项目根(self):
        """git 的向上查找不限层数,一路能走到 /。家目录被人拿来版本化 dotfiles 是常见做法,
        那时 ~ 底下任何一份文件都会把根定在家目录上——左树要铺开 Library、缓存与全部下载,
        比原来「只看文件所在那一层」更糟。"""
        seg = re.search(r"def _fs_git_root\([\s\S]+?\n\ndef ", ASSETS).group(0)
        self.assertIn('if top in (os.path.expanduser("~").rstrip(os.sep), os.sep):', seg)

    def test_两侧比真实路径(self):
        """git 报的 toplevel 解析过软链,而 _fs_resolve 有意不解析(那是给访问控制用的)。
        直接比字符串的话,路径里有一段软链就对不上——macOS 的 /tmp 即 /private/tmp,
        升不到仓根却毫无提示。"""
        seg = re.search(r"def _fs_git_root\([\s\S]+?\n\ndef ", ASSETS).group(0)
        self.assertIn("target = os.path.realpath(full or base)", seg)
        self.assertIn("top = os.path.realpath(top)", seg)

    def test_仓外的目标不认(self):
        """软链之类可能把 base 指到仓外,那时 relpath 会算出一串 ..,不能当相对路径用。"""
        seg = re.search(r"def _fs_git_root\([\s\S]+?\n\ndef ", ASSETS).group(0)
        self.assertIn("not target.startswith(top + os.sep)", seg)


if __name__ == "__main__":
    unittest.main()
