"""设置页整体审阅(2026-08-24)落地的三处改动。

1. 三家引擎小节的标题前挂上品牌图标(取自 BRAND_ICONS,与引擎选择器同一份),不是重画一套。
2. Remote Control 原来把接口失败(catch{} 静默吞掉)与「真的没配置」
   渲染成同一种界面——用户看到「未配置」/「未部署」,会被引去点「一键部署」,而真实原因
   可能只是后端没重启。改成显式区分:接口失败单独给一行「读取…失败」,不冒充「未配置」。
3. .sm-switch 关闭态的轨道原来只靠一层半透明叠色,合成到卡片底色后测得深色 1.67:1、
   浅色 1.28:1,浏览器操控里禁用的开关在浅色下基本看不见。改用 outline 描一圈边
   (--text-weaker,实色,深色 4.18:1 / 浅色 3.07:1),且不影响 ::after 那颗圆点的坐标——
   outline 不占布局,border 会把坐标系收进去几像素。
"""
import os
import re
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
JS = _srcmap.settings_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


class 引擎图标(unittest.TestCase):
    def test_smSection收icon参数(self):
        sec = re.search(r"function smSection\(title, hint, icon\)[\s\S]+?\n}", JS)
        self.assertTrue(sec, "smSection 没有 icon 形参")
        self.assertIn("sm-title-ic", sec.group(0))

    def test_三家引擎都传了品牌图标(self):
        for name, eng in (("Claude Code", "claude"), ('"Codex"', "codex"),
                          ("Pi Coding", "pi")):
            m = re.search(r'smSection\("%s"[\s\S]{0,400}?BRAND_ICONS\.%s\)' % (re.escape(name.strip('"')), eng), JS)
            self.assertTrue(m, "%s 没有挂 BRAND_ICONS.%s" % (name, eng))

    def test_没传icon时不留空的图标位(self):
        """这一节原本守的是「其余 7 节保持纯文字」。2026-08-24 目录改版之后 11 节全配了
        图标(见 test_frontend_settings_nav),那条前提不再成立,改守模板本身:
        没传 icon 时不该渲染一个空的 .sm-title-ic 把标题推出去。"""
        sec = re.search(r"function smSection\(title, hint, icon\)[\s\S]+?\n}", JS).group(0)
        self.assertIn('icon ? `<span class="sm-title-ic">', sec)


class 开关轨道可见(unittest.TestCase):
    """头一版用 outline 描边解决对比度,实机看效果是一圈多余的灰框,把整颗开关的边界
    撑大了一圈(2026-08-24 用户实见,撤掉)。改直接用实色轨道(--ink-36),不需要另画
    一圈边:深色 4.18:1、浅色 3.5:1,比描边方案(4.18:1/3.07:1)还略高一点。"""

    def _rule(self, sel):
        # 同一个选择器在文件里出现不止一次(通用组件基础样式 + 设置页覆盖),要的是后写的
        # 那份——CSS 里后者覆盖前者,和界面上实际生效的是同一份。
        hits = re.findall(r"\n%s \{[^}]*\}" % re.escape(sel), CSS)
        self.assertTrue(hits, "找不到 %s" % sel)
        return hits[-1]

    def test_关闭态是实色不是透明叠加(self):
        r = self._rule(".sm-switch")
        self.assertIn("background: var(--ink-36)", r)

    def test_不再画额外的描边(self):
        """outline 试过又撤了,两个状态都不该再留痕迹。"""
        for sel in (".sm-switch", ".sm-switch.on"):
            r = self._rule(sel)
            self.assertNotIn("outline", r)

    def test_圆点坐标不受影响(self):
        r = self._rule(".sm-switch::after")
        self.assertIn("top: 2px; left: 2px", r)


if __name__ == "__main__":
    unittest.main()
