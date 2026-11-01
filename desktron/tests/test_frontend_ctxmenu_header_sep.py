"""右键菜单:分组标题下面那条分隔线不画。

2026-09-10 用户实见:「Remote Control · 已连接」这个标题与它下面那条开关之间有一条线。
标题本身已经把这一组开了口,再画一条线是把标题和它所标注的内容切开——读起来像标题
属于上一组,而不是下面那条的抬头。

线画在哪由调用方逐条传 sep,所以同一个错在两处都会犯(app-rail-topnav.js 里
needs_attention 与常态两个分支各有一条紧跟标题的 sep),修在渲染器里一次覆盖所有菜单。

WebKit 探针实测(四条菜单:普通项 / 带 sep 的标题 / 带 sep 的动作 / 另一组带 sep 的动作):
    切到服务器              rp-row rp-hasico              无线
    服务器 · 已连接          rp-row rp-ctx-head rp-sep     有线   ← 标题上方的线保留
    断开服务器              rp-row rp-hasico              无线   ← 紧跟标题,不画
    另一组的第一条            rp-row rp-sep rp-hasico       有线   ← 再下一组照常画
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
PANEL = (ROOT / "frontend/app-panel-terms.js").read_text(encoding="utf-8")


class HeaderOpensTheGroup(unittest.TestCase):
    def _menu_src(self):
        m = re.search(r"function openCtxMenu\(x, y, items, cls\) \{[\s\S]+?\n  document\.body\.appendChild\(m\);", PANEL)
        self.assertIsNotNone(m)
        return m.group(0)

    def test_row_right_after_a_header_drops_its_separator(self):
        src = self._menu_src()
        self.assertIn("if (it.sep && !afterHeader) r.classList.add(\"rp-sep\");", src)
        self.assertIn("afterHeader = true;", src)

    def test_the_flag_is_cleared_by_every_other_row(self):
        # 只在标题那一支置位,别的分支都要清掉;漏一处就会连着吞掉后面几条的线。
        src = self._menu_src()
        self.assertIn("let afterHeader = false;", src)
        self.assertIn("m.appendChild(it.node); afterHeader = false;", src)   # 自定义节点这一支
        # 普通行这一支:清在 sep 判断之后,否则这一行自己就被当成「紧跟标题」。
        self.assertLess(src.index('if (it.sep && !afterHeader)'),
                        src.rindex("afterHeader = false;"))

    def test_header_keeps_the_line_above_itself(self):
        # 标题上方那条线是分组的起点,照旧由标题自己的 sep 画。
        self.assertIn('h.className = "rp-row rp-ctx-head" + (it.sep ? " rp-sep" : "");', PANEL)


if __name__ == "__main__":
    unittest.main()
