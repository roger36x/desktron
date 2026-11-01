"""任务勾选框的「进行中」态,用的是 taskCbSvg(),不是 .td-mark——上一轮改错了组件。

用户截图里那份任务列表(七八月份 Desk MVP…)是会话内嵌的任务栏,由 app-chat.js 的
hsTasksSync 用 taskCbSvg(t.state) 渲染,类名是 .hs-task-cb;.td-mark 是任务侧栏
(app-tasks.js 的 _dockRow)另一份独立组件,截图里那处根本没有用到它,上一轮的修复
因此没有反映到用户看到的地方。

taskCbSvg 原来给「进行中」单独画一块填充满格的色块当外缘(.wt-cb-halo,不走描边)——
外缘跟「未开始」的描边框长得不一样,读成了两种控件。改成三态共用同一个描边方框,
内部标记缩小(9.5→5.5),边框与色块之间露出可辨的间隙。

改完发现一个连带的旧问题:.hs-task.doing .hs-task-cb 原来把 color 单独提到 --ink-93,
在旧画法下这条规则不起作用(填充画的外缘不读 currentColor),一直是条没有可见效果的
死规则。外框统一成描边方框之后它突然生效,框的颜色比「未开始」亮了一大截——正是用户
这次报的"外框颜色没对齐"。去掉这条,两态的外框颜色才真的一致。
"""
import os
import re
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PANEL = _srcmap.rail_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()

SVG_FN = re.search(r"function taskCbSvg\(state\)[\s\S]+?\n}", PANEL).group(0)


class 三态共用同一个外框(unittest.TestCase):
    def test_box不再按状态分叉(self):
        """box 曾经是三元表达式(doing 走填充色块、其余走描边框)。现在应当是一个常量,
        三态都是同一个 <rect>。"""
        self.assertIn("const box = '<rect class=\"wt-cb-box\"", SVG_FN)
        self.assertNotIn('wt-cb-halo', SVG_FN)

    def test_内块尺寸(self):
        """几轮来回:9.5(顶到边,读成两种控件)→5.5(嫌小)→6.5(「再略大一点」)→6.0
        (「改小一点」,6.5 又大了)→6.25(两端都被打回,取中)→5.5(2026-08-26 同一天
        又要求「改成 5.5 试试」,回到当初嫌小的那一档)。中心仍钉在 viewBox 中点
        (x/y = 8 - 边长/2);圆角另有一笔账,见下一条。5.5 曾经被排除又变回当前值,不能再用
        assertNotIn 挡它;其余试过又被打回的档继续挡。"""
        self.assertIn('width="5.5" height="5.5"', SVG_FN)
        self.assertNotIn('width="9.5" height="9.5"', SVG_FN)
        self.assertNotIn('width="6.5" height="6.5"', SVG_FN)
        self.assertNotIn('width="6" height="6"', SVG_FN)
        self.assertNotIn('width="6.25" height="6.25"', SVG_FN)

    def test_内块是直角(self):
        """那圈缝原来不等宽:直边 2.71 CSS px、四角 3.51 px,角上宽 29%(把图标放大 5 倍渲染、
        按角度逐点扫出来的;缝内底色两处都是 43,排除了明暗渐变造成错觉这一路)。

        成因是圆角比例:内块圆角占自身半宽 44%,外框内缘只占 36%,内块比外框更圆。一圈缝要
        等宽,内圈必须是外圈等距内缩的形状;而缝宽 3.5 已经超过外框内缘的圆角半径 2.25,
        内缩到那个距离时圆角早收没了,等宽解恰好是个直角方块。留 1.2 的圆角等于在四条对角线
        上又削掉一块,缝就在角上张开——沿直边平行、到转角张开的带子正是倒角的画法,内块因此
        看着像浮在台子上(2026-08-26 用户实见「缝隙有凸起的感觉」)。

        先收到 0.45(四角多余量 10.0%→3.7%),2026-08-26 用户要求索性试直角:去掉 rx。
        直角方块正是上面那道等宽解本身,垂直缝宽这下真的一圈相等。
        (按射线量的"径向"缝宽在 45° 仍比直边大 15% 上下,那是三角关系使然,不是形不对:
        同一条等宽带子,沿对角线穿过去本来就要走得更远。)

        要给它加回圆角先重算这笔账,别只按手感调。"""
        m = re.search(r'<rect class="wt-cb-in"[^>]*>', SVG_FN)
        self.assertTrue(m, "内块 rect 不见了")
        self.assertNotIn("rx=", m.group(0))

    def test_wt_cb_halo规则已清掉(self):
        """类名不再产出,留着规则也是死代码。注释里提历史缘由允许出现这个名字,
        只查它是不是还作为选择器生效。"""
        self.assertNotIn(".wt-cb-halo {", CSS)
        self.assertNotIn(".wt-cb-halo{", CSS)


class 会话内嵌任务栏不再单独提亮进行中的边框(unittest.TestCase):
    def test_hs_task_doing不再覆盖checkbox颜色(self):
        """这条规则在旧画法下是死的(填充色块不读 currentColor),外框统一成描边方框后
        突然生效,把「进行中」的边框比「未开始」提亮了一大截——就是这次用户报的问题。"""
        self.assertNotIn(".hs-page .hs-task.doing .hs-task-cb {", CSS)

    def test_两态的checkbox基础色是同一处来源(self):
        m = re.search(r"\n\.hs-page \.hs-task-cb \{[^}]*\}", CSS)
        self.assertTrue(m)
        self.assertIn("color: var(--ink-44)", m.group(0))

    def test_标题文字仍按进行中提亮_不影响checkbox(self):
        """.hs-task.doing 本身的 color 是给标题文字用的,这条不用动——受影响的只有
        checkbox 那一层的单独覆盖。"""
        self.assertIn(".hs-page .hs-task.doing { color: var(--ink-79); }", CSS)


class 内块使用品牌紫_勾选框与文字墨迹对齐(unittest.TestCase):
    """进行中内块使用品牌紫，勾选框继续保持原有墨迹对齐。"""

    def test_进行中内块使用品牌紫(self):
        """只给中央实心方块着品牌紫，外框仍保留中性色。"""
        m = re.search(r"\.wt-cb-in\s*\{([^}]*)\}", CSS)
        self.assertTrue(m)
        self.assertIn("fill: var(--accent)", m.group(1))

    def test_task_cb有墨迹对齐的位移(self):
        """CDP 实测:行盒、勾选框盒、文字行盒的几何中线完全重合(48.5px),align-items:center
        没有问题;错位出在文字的墨迹在行盒里天然偏下(中文字面框不居中),跟 .hs-tasks-chev
        那颗箭头是同一类问题。两组独立截图量出 0.875px 与 0.75px,取 0.8px 位移勾选框、
        不动文字。"""
        m = re.search(r"\n\.hs-page \.hs-task-cb \{[^}]*\}", CSS)
        self.assertTrue(m)
        self.assertIn("translateY(0.8px)", m.group(0))


class 外框内侧不再是镂空的黑(unittest.TestCase):
    def test_wt_cb_box有底色(self):
        """svg 根上 fill:none,外框内侧原来镂空,露出页面本底——深色主题下接近纯黑,
        跟描边、内块两层灰反差过硬(2026-08-25 用户实见「边框和实心方块中间的黑色…
        填充为不那么黑的灰色」)。先试过 --lift-14(合成色 27→59),用户嫌太显眼,
        压到 --lift-07(27→43)。三态共用同一个 <rect>,不单独为某一态开例外。"""
        self.assertIn(".wt-cb-box { fill: var(--lift-07); }", CSS)
        self.assertNotIn(".wt-cb-box { fill: var(--lift-14); }", CSS)

    def test_内块与底色使用不同语义色(self):
        """底色使用中性抬升面，内块使用品牌紫，避免两层糊成一片。"""
        m = re.search(r"\.wt-cb-in\s*\{([^}]*)\}", CSS)
        self.assertTrue(m)
        self.assertNotIn("lift-07", m.group(1))


if __name__ == "__main__":
    unittest.main()
