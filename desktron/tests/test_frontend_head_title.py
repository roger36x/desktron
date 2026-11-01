"""顶栏标题与侧栏品牌字同色。

它们是窗口最上沿并排的两处标识——左边「Desktron」,右边当前会话/应用的名字。一边深一边
浅会读成两块界面(2026-08-21 用户实见:品牌 #414141、顶栏 #292929)。

约束范围变过一次。2026-08-21 只管浅色:那次的病灶是浅色顶栏取 --text-strong=#0d0d0d,
配 17px/600 让整条顶栏成了页面上最重的一处;深色当时被判为「白字压深条不是一件事」而
留在 --text-strong。2026-08-27 用户要求深色也对齐——深色下品牌字 #c8c8c8、顶栏 #fbfbfb,
并排看差得明显。于是取值收进 --head-title,而它落在分主题的色阶上,一条规则同时管住两边,
原先那条只管浅色的覆盖随之撤掉。

同一天用户又说「有点太暗」,于是从 --ink-72 挪到 --ink-77。色阶数字是亮度百分比、浅色
那套翻面,所以往上挪一档在两个主题里都是「更立得住」:深色 #c8c8c8→#d1d1d1 变亮,
浅色 #414141→#393939 变深。两处一起挪——只提亮顶栏会当场破掉刚对齐的那件事。

两处规则隔着两千多行,一处在 .side-brand .brand、一处在 #overlay-head,靠注释互相提醒
挡不住改动。改成共用一个 token 之后,比对的是「用没用同一个 token」,比比对字面取值更结实:
以后调明暗只动 token 一处,两边不可能再岔开。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


def token_value():
    """--head-title 定义处的取值。"""
    m = re.search(r"--head-title:\s*([^;]+);", CSS)
    assert m, "没找到 --head-title"
    return m.group(1).strip()


def color_of(head):
    """取以 head 开头那条规则里的 color(规则可能跨行)。"""
    i = CSS.index(head)
    body = CSS[i:CSS.index("}", i)]
    m = re.search(r"color:\s*([^;}]+)", body)
    assert m, "%s 那条规则里没有 color" % head
    return m.group(1).strip()


class 顶栏与品牌字同色(unittest.TestCase):
    def test_三处都走同一个token(self):
        """比对 token 而不是字面取值:调明暗时只动 token,两边不可能岔开。"""
        for where in (".side-brand .brand { font-size: 19px", "\n#overlay-head {", "\n#term-head {"):
            self.assertEqual(color_of(where), "var(--head-title)", "%s 没走 --head-title" % where.strip())

    def test_不是纯黑也不是纯白那一档(self):
        """--text-strong 两头都太满:浅色 #0d0d0d 近纯黑、深色 #fbfbfb 近纯白,
        配 17px 的 600 字重会让整条顶栏成为页面上最重的一处。"""
        self.assertNotIn("--text-strong", token_value())


class 一个token管两个主题(unittest.TestCase):
    def test_token落在分主题的色阶上(self):
        """--head-title 本身只定义一次;它指到的那档色阶两套主题各有一份,
        所以不需要再写一条只管浅色的覆盖——那就又回到「两处要一起改」的老问题。"""
        self.assertEqual(len(re.findall(r"--head-title:", CSS)), 1)
        m = re.search(r"var\(--(ink-\d+)\)", token_value())
        self.assertTrue(m, "--head-title 没指向色阶:%s" % token_value())
        self.assertEqual(len(re.findall(r"--%s:\s*#" % m.group(1), CSS)), 2,
                         "%s 在两套色阶里没有各一份" % m.group(1))

    def test_不再单开浅色覆盖(self):
        self.assertIsNone(
            re.search(r'html\[data-theme="light"\]\s+#overlay-head[^{]*\{[^}]*color:', CSS),
            "浅色又单开了一条顶栏色覆盖")


class 手机顶栏图标与标题中线(unittest.TestCase):
    def test_只在手机端上移图形而不移动按钮(self):
        marker = "/* 手机 3× 屏上 SVG 墨迹的视觉中心比 17px 标题低约 1px。只移动图形，不移动按钮盒，"
        mobile = CSS[CSS.index(marker):]
        self.assertIn("#top-nav button svg,", mobile)
        self.assertIn("#overlay.hs-head #overlay-head .head-actions > button > svg { transform: translateY(-1px); }", mobile)
        desktop = CSS[:CSS.index(marker)]
        self.assertNotIn("#top-nav button svg { transform:", desktop)
        self.assertNotIn("#overlay.hs-head #overlay-head .head-actions > button > svg { transform:", desktop)


if __name__ == "__main__":
    unittest.main()
