"""信息卡的审批列表:卡片右缘与标题行的箭头对齐,滑块单独占右侧那条车道。

2026-09-10 用户实见:展开审批后,卡片右缘比标题行右端那枚箭头短一截。

容器本来就是为这件事设计的——它向右探出 18px(width: calc(100% + 18px) 配 margin-right: -18px),
再用同宽的 padding-right 把卡片收回分区内,那 18px 只当滑块车道。缺的是最后一步:
原生滚动条没关。系统设成「总是显示滚动条」时它不是浮层而是常驻自绘条,会从内容盒里
吃掉 15px,卡片右缘因此左移。

WebKit 探针实测(1680×1000,信息卡宽 309px,把伪元素定宽 15px 模拟常驻条):
    修前  箭头 1599–1613   卡片 1342–1598   容器占宽 15
    修后  箭头 1599–1613   卡片 1342–1613   容器占宽 0
不注入模拟(浮层条)时修前修后都是 1342–1613,所以这个缺口只在「总是显示滚动条」下出现,
也就是用户那台机器的设置。

关掉原生条之后车道会空着,所以把这个容器注册进全局那套自绘滑块(app-init.js 的
autoScrollbars,与侧栏、终端、会话正文同一套)。实测滑块落在 1621–1629:
在箭头(1613)右边、信息卡右缘(1632)左边,正好悬在那 18px 车道里,不压卡片。
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "frontend/style.css").read_text(encoding="utf-8")
INIT = (ROOT / "frontend/app-init.js").read_text(encoding="utf-8")


class ApprovalListGutter(unittest.TestCase):
    def _host_rule(self):
        m = re.search(r"\.hs-sit-approval-host:not\(:empty\) \{([^}]*)\}", CSS)
        self.assertIsNotNone(m)
        return m.group(1)

    def test_lane_is_carved_out_by_the_container_itself(self):
        # 三者是一组:探出 18px、拉回 18px、再用 18px 内距把卡片收回分区内。
        host = self._host_rule()
        self.assertIn("width: calc(100% + 18px)", host)
        self.assertIn("margin-right: -18px", host)
        self.assertIn("padding: 9px 18px 0 0", host)
        self.assertIn("box-sizing: border-box", host)

    def test_native_scrollbar_takes_no_layout_width(self):
        # 两条缺一不可:scrollbar-width 管 Firefox 口径,伪元素管 WebKit——桌面壳是 WebKit。
        self.assertIn("scrollbar-width: none", self._host_rule())
        self.assertIn(".hs-sit-approval-host::-webkit-scrollbar { display: none; }", CSS)
        # gutter 是上一版的做法,它并不阻止常驻条占宽,留着会让人以为已经处理过。
        self.assertNotIn("scrollbar-gutter", self._host_rule())

    def test_lane_is_filled_by_the_shared_thumb(self):
        # 关掉原生条却不给滑块,列表就没有任何可滚提示。注册进全局那套。
        sel = re.search(r"\(function autoScrollbars\(\) \{\s*const SEL = \[([\s\S]*?)\]\.join", INIT)
        self.assertIsNotNone(sel)
        self.assertIn('".hs-sit-approval-host"', sel.group(1))


if __name__ == "__main__":
    unittest.main()
