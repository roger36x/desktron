"""审阅面板顶上那枚「在 IDE 打开」。

这一栏本来就是逐个文件读 diff,读到想动手的那份得能直接接到编辑器上,否则要回去自己找路径。
所以它开的是**此刻正在看的那一份**,不是整个仓库:onclick 里现取 v.rv.files[v.rv.cur],
不把路径闭包进去——面板不重开、只在左边换一份文件的情况比重开常见得多。

路径是绝对的,经 hsResolveFile 顺带换成仓库根 + 仓内相对路径(hsRepoRoot 那一步)。
给编辑器文件所在目录是不行的:那一页按 root 建标签,同一个仓每换一层就多开一页。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
FILES = open(os.path.join(ROOT, "frontend", "app-files.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
OPEN = re.search(r"function hsRvOpen\(v, t, path\)[\s\S]+?\n}", CHAT).group(0)
CLOSE = re.search(r"function hsRvClose\(v\)[\s\S]+?\n}", CHAT).group(0)
WIRE = re.search(r"function hsRvWire\(v\)[\s\S]+?\n}", CHAT).group(0)


class 位置与形制(unittest.TestCase):
    def test_排在改动量之后弹性空白之前(self):
        """挨着「+0 -23」,与撤销、关闭之间隔着 .hs-rv-sp,不挤到右端那两枚里去。"""
        head = re.search(r'<div class="hs-rv-h">[\s\S]+?hs-rv-x[^;]*?;', CHAT).group(0)
        self.assertLess(head.index("hs-rv-s"), head.index("hs-rv-ide"))
        self.assertLess(head.index("hs-rv-ide"), head.index("hs-rv-sp"))

    def test_与撤销那枚同形(self):
        """两枚并排,一枚圆角胶囊一枚方的会读成两种东西。"""
        for sel in (".hs-rv-undo, .hs-rv-ide, .hs-rv-x {", ".hs-rv-undo, .hs-rv-ide {"):
            self.assertIn(sel, CSS)
        self.assertIn(".hs-rv-undo:hover, .hs-rv-ide:hover", CSS)

    def test_隐藏时不占位(self):
        """hidden 属性对 display:inline-flex 不生效,要显式收掉。"""
        self.assertIn(".hs-rv-ide[hidden] { display: none; }", CSS)

    def test_顶栏文字共用同一中线盒(self):
        self.assertIn(".hs-rv-h { height: 44px; box-sizing: border-box;", CSS)
        self.assertIn(".hs-rv-t, .hs-rv-s { height: 28px; display: inline-flex; align-items: center; line-height: 1; }", CSS)
        self.assertIn(".hs-rv-undo, .hs-rv-ide { height: 28px;", CSS)


class 审阅栏与会话列同帧布局(unittest.TestCase):
    def test_开关审阅栏不再触发几何复量(self):
        self.assertIn('v.el.classList.add("rv-on")', OPEN)
        self.assertIn('v.el.classList.remove("rv-on")', CLOSE)
        self.assertNotIn("alignLater", OPEN + CLOSE)

    def test_拖宽只更新侧栏宽度变量(self):
        self.assertIn('el.style.setProperty("--rv-w", nw + "px")', WIRE)
        self.assertNotIn("rvAlignRaf", WIRE)
        self.assertNotIn("hsAlign", WIRE)


class 手机端返回会话(unittest.TestCase):
    def test_顶栏有明确的返回入口(self):
        self.assertIn('class="hs-rv-back" aria-label="返回会话"', CHAT)
        self.assertIn("HS_ICONS.qprev + '<span>返回会话</span>", CHAT)

    def test_返回入口与桌面关闭钮走同一关闭逻辑(self):
        wire = re.search(r"function hsRvWire\(v\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn('querySelectorAll(".hs-rv-x, .hs-rv-back")', wire)
        self.assertIn("b.onclick = () => hsRvClose(v)", wire)

    def test_手机审阅是全屏且不再显示拖宽条(self):
        self.assertIn(".hs-page.rv-on { padding-right: 0; }", CSS)
        self.assertIn(".hs-page .hs-rv { inset: 0; width: 100%;", CSS)
        self.assertIn(".hs-page .hs-rv-dv { display: none !important; }", CSS)

    def test_手机显示返回并隐藏桌面关闭钮(self):
        self.assertIn(".hs-page .hs-rv-back { display: inline-flex;", CSS)
        self.assertIn(".hs-page .hs-rv-x, .hs-page .hs-rv-ide { display: none; }", CSS)


class 开的是当前那一份(unittest.TestCase):
    def test_点的时候才取当前文件(self):
        seg = OPEN[OPEN.index('querySelector(".hs-rv-ide")'):]
        self.assertIn("v.rv.files[v.rv.cur]", seg, "路径要现取,换文件之后才跟得上")

    def test_经hsResolveFile换成仓库根(self):
        seg = OPEN[OPEN.index('querySelector(".hs-rv-ide")'):]
        self.assertIn("hsResolveFile(f.path, v.el)", seg)
        self.assertIn("openFileInIde(loc.root, loc.rel)", seg)

    def test_解析失败不炸(self):
        seg = OPEN[OPEN.index('querySelector(".hs-rv-ide")'):]
        self.assertIn("catch", seg)
        self.assertIn("if (loc && loc.root && loc.rel)", seg)

    def test_没有那个函数时整枚不出现(self):
        """app-files.js 没加载的话给个点不动的钮,不如不给。"""
        self.assertIn('ide.hidden = typeof openFileInIde !== "function";', OPEN)


class 对面那一侧(unittest.TestCase):
    def test_openFileInIde_收的是root和仓内相对路径(self):
        seg = re.search(r"function openFileInIde\(root, rel\)[\s\S]+?\n}", FILES).group(0)
        self.assertIn('const key = "cd_files_app_" + id;', seg)
        self.assertIn("st.last = path;", seg)

    def test_它会把沿路目录展开(self):
        """否则树是收着的,交接过去还得自己点开一路。"""
        seg = re.search(r"function openFileInIde\(root, rel\)[\s\S]+?\n}", FILES).group(0)
        self.assertIn("st.expanded", seg)


if __name__ == "__main__":
    unittest.main()
