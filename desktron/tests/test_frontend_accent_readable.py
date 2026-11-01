"""强调色要和正文拉开亮度差,不只是和背景拉开对比度。

深色下 #b0b9f9 被报「有点淡,不容易看清」。查下来根子不在对底的对比度——那一档是 9.2:1,
比正文 --ink-71 对底的 10.1:1 只低一点,远在门槛之上。问题在它与正文的相对亮度几乎相同
(0.508 对 0.558,差 0.051):深底上人先读亮度,亮度差近零时那点色相差撑不起层次,
读起来就是「另一种灰」。压深到 #97a3f7 之后差是 0.163,对底仍有 7.4:1。
(先落到 #8d9af6 / 差 0.204,看着偏深,回浅了两个百分点——那一档离 0.15 的门槛还剩 0.013。)

这条判据反直觉——「对比度够」并不等于「看得出这是个强调」,不写下来下次还会照着对比度调,
再把它调回一档洗白的淡紫。所以这里守两侧:对底不低于 AA 门槛,对正文的亮度差不低于 0.15。

浅色主题不在这条卡点里:它是同一个毛病(#4f46b8 与 #444444 差 0.037),但方向相反、
且被白底的 4.5:1 锁着,最多拉到 0.125 的差,得拿对底的对比度换,取舍另算。
现状用 test_浅色那一档的现状 记着,数值一变就会失败,提醒回来重看。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


def _lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def lum(hexv):
    h = hexv.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a, b):
    lo, hi = sorted((lum(a), lum(b)))
    return (hi + 0.05) / (lo + 0.05)


def token(name, light=False):
    """深色是基础规则,浅色写在 html[data-theme="light"] 里,同名 token 各出现一次。"""
    hits = re.findall(r"--%s:\s*(#[0-9a-fA-F]{6})" % name, CSS)
    assert len(hits) == 2, "%s 应当深浅各一份,现在有 %d 份" % (name, len(hits))
    return hits[1 if light else 0]


class 深色下的强调色(unittest.TestCase):
    def test_与正文的亮度差够(self):
        d = abs(lum(token("accent")) - lum(token("ink-71")))
        self.assertGreaterEqual(round(d, 3), 0.15,
                                "强调色与正文亮度太近(差 %.3f),读起来会像另一种灰" % d)

    def test_对底仍在AA门槛之上(self):
        c = contrast(token("accent"), token("ink-10"))
        self.assertGreaterEqual(round(c, 1), 4.5, "压深过头,对底只剩 %.1f:1" % c)

    def test_不比正文更亮(self):
        """比正文还亮的话,一行里最跳的就成了链接而不是内容。"""
        self.assertLess(lum(token("accent")), lum(token("ink-71")))

    def test_accent_soft跟着accent走(self):
        """两处各写一份的话,底纹和文字迟早对不上——上一次淡紫就是这么散开的。"""
        h = token("accent").lstrip("#")
        rgb = ",".join(str(int(h[i:i + 2], 16)) for i in (0, 2, 4))
        self.assertIn("--accent-soft: rgba(%s," % rgb, CSS)


class 浅色那一档的现状(unittest.TestCase):
    def test_已知未修_数值一变就回来重看(self):
        d = abs(lum(token("accent", True)) - lum(token("ink-71", True)))
        self.assertAlmostEqual(d, 0.037, places=2,
                               msg="浅色的强调色动过了,顺带确认它与正文的亮度差是不是有意为之")


class 不再各处手写一份(unittest.TestCase):
    def test_旧的淡紫不再作为颜色值出现(self):
        """#b0b9f9 / #b1b9f9 只该留在注释里当来历,不该还是某条规则的取值。"""
        code = re.sub(r"/\*[\s\S]*?\*/", "", CSS)      # 注释块整体剥掉,只看真生效的规则
        for bad in ("#b0b9f9", "#b1b9f9"):
            self.assertNotIn(bad, code, "%s 还在被当成取值用" % bad)


if __name__ == "__main__":
    unittest.main()
