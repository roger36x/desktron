"""侧栏搜索结果:每条命中带上「这条消息自己的时间」。

2026-09-03 用户要的是给搜出来的消息加时间。落点有两个候选:卡片头(会话级)与命中行
(消息级)。选后者,因为会话级会说谎——同一次搜索的真实数据:

    desktron ui        会话更新 今天 17:39   命中 今天 16:19 / 7月30日
    Desktron Delivery  会话更新 今天 17:22   命中 8月10日 / 8月23日

会话是长期滚动的,更新时间几乎总是"今天",而人想知道的恰恰是那句话什么时候说的。
同一张卡里的两条命中按相关度排,先后也不按时间,不标出来看不出它们跨了三周。

时间写在片段前面,不另起一行:片段本来就窄(侧栏约 250px),多一行会把每张卡片都撑高
一截;前缀的左缘固定,一列扫下来能直接比出哪条更早。

时间戳来自后端已有的 hit.at,没有改后端,也不必重启。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
NAV = open(os.path.join(ROOT, "frontend", "app-rail-nav.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()

AT_FN = re.search(r"\nfunction sideSearchAt\(ms\)[\s\S]+?\n}", NAV).group(0)
PAINT = re.search(r"\nfunction sideSearchPaint\([\s\S]+?\n}\n", NAV).group(0)


class 时间挂在命中而不是卡片(unittest.TestCase):
    def test_取的是命中自己的时间戳(self):
        """用 item.updated(会话更新时间)的话每张卡片都会写「今天」,见文件头的实测对照。"""
        self.assertIn("sideSearchAt(hit.at)", PAINT)
        self.assertNotIn("sideSearchAt(item.updated)", PAINT)

    def test_时间排在片段之前(self):
        """前缀左缘固定才好扫;放在片段之后位置随文长浮动,反而看不出先后。"""
        i_stamp = PAINT.index('stamp.className = "ss-hit-at"')
        i_snip = PAINT.index("sideSearchMark(row, hit.snippet")
        self.assertLess(i_stamp, i_snip)

    def test_没有时间戳时不占位(self):
        """老索引里可能没有 at,不能留一个空的前缀把片段推开。"""
        self.assertIn("if (at) {", PAINT)

    def test_不另起一行(self):
        """.ss-hit 是 display:block,时间用行内 span 跟在同一行,不额外占高。"""
        m = re.search(r"\.ss-hit-at \{[^}]*\}", CSS)
        self.assertTrue(m, "缺 .ss-hit-at 样式")
        self.assertNotIn("display: block", m.group(0))
        self.assertIn("color: var(--text-weaker)", m.group(0))

    def test_字号跟着片段走且加粗(self):
        """2026-09-03 用户要求:日期加粗,字号与后面的内容对齐。字号写 inherit 而不是
        复写 12px——两者本来就该是同一行里的同一档,.ss-hit 将来改字号时不必记得回来改
        这里。加粗让它读成一枚标签而不是正文的一部分;颜色仍比正文暗一档,不抢视线。
        实测:日期 12px/600/rgb(152,152,152),片段 12px/400/rgb(213,213,213)。"""
        m = re.search(r"\.ss-hit-at \{[^}]*\}", CSS)
        self.assertIn("font-size: inherit", m.group(0))
        self.assertIn("font-weight: 550", m.group(0))


class 时间粒度按距今远近分档(unittest.TestCase):
    """实测各档(以当天为基准):
    20 秒前→刚刚,25 分钟前→25 分钟前,5 小时前→12:42,30 小时前→昨天,
    今年 7 月→7月30日,去年 8 月→2025年8月,0→空串。"""

    def test_一小时内报相对量(self):
        self.assertIn('if (sec < 60) return "刚刚";', AT_FN)
        self.assertIn("Math.floor(sec / 60)} 分钟前", AT_FN)

    def test_当天报钟点而不是N小时前(self):
        """「3 小时前」要人自己做减法;钟点能直接和这一天的记忆对上号。"""
        self.assertIn("d.getHours()", AT_FN)
        # 只查它有没有作为返回值出现;注释里拿这个词说明取舍是允许的。
        self.assertIsNone(re.search(r"\}\s*小时前", AT_FN))

    def test_跨天按自然日算而不是按86400秒(self):
        """按秒差算的话,今天 00:30 与昨天 23:30 只差一小时却要显示成同一天。"""
        self.assertIn("new Date(x.getFullYear(), x.getMonth(), x.getDate())", AT_FN)

    def test_跨月落到具体日期跨年落到年月(self):
        """会话历史动辄跨月,「N 天前」到这个尺度就没有意义了——这也是没有复用
        _arrAgo / _agpRel 的原因之一(另一个是它们的入参是秒,而 at 是毫秒)。"""
        self.assertIn("d.getMonth() + 1}月${d.getDate()}日", AT_FN)
        self.assertIn("d.getFullYear()}年${d.getMonth() + 1}月", AT_FN)
        self.assertIsNone(re.search(r"\}\s*天前", AT_FN))

    def test_没有时间戳返回空串(self):
        self.assertIn('if (!t) return "";', AT_FN)
