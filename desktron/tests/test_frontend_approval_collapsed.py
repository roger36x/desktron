"""信息卡审批区收起后不留任何正文行。

2026-09-09 用户实见:没有审批时把「最近审批」收起,标题行下面仍留着一行「暂无审批记录」。
收起的语义是这一区只剩标题,留一行状态文字等于没收起。

根因是结构与样式脱节。标题栏 .hs-sit-approval-bar 之下有两个兄弟节点:
列表容器 .hs-sit-approval-host 写在 app-chat-situation.js 的标记里,
状态行 .appr-load-state 由 app-approval.js 在首次需要时补挂到 section 上。
收起规则只挡住了前者——写规则时后者还不存在。

WebKit 探针实测(380px 容器,section 只含标题栏 + 空列表 + 状态行):
  收起  状态行 display:none,section 高 48px(只剩标题行)
  展开  状态行 display:block,占 342×33,section 高 89px
优先级不靠书写顺序:.hs-sit-approvals.collapsed .appr-load-state 是 (0,3,0),
压得住后面那条 .appr-load-state { display: block }(0,1,0)。
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "frontend/style.css").read_text(encoding="utf-8")
SITUATION = (ROOT / "frontend/app-chat-situation.js").read_text(encoding="utf-8")
APPROVAL = (ROOT / "frontend/app-approval.js").read_text(encoding="utf-8")


class CollapsedApprovalSectionHidesEverything(unittest.TestCase):
    def test_collapsed_hides_both_the_list_and_the_state_line(self):
        m = re.search(r"\.hs-sit-approvals\.collapsed[^{]*\{\s*display:\s*none", CSS)
        self.assertIsNotNone(m, "收起规则不见了")
        block = CSS[CSS.index(".hs-sit-approvals.collapsed .hs-sit-approval-host"):]
        block = block[:block.index("}") + 1]
        self.assertIn(".hs-sit-approval-host", block)
        self.assertIn(".appr-load-state", block)
        self.assertIn("display: none", block)

    def test_state_line_is_a_sibling_of_the_list_not_inside_it(self):
        # 规则要挡两个节点,前提是它们确实是两个节点;哪天状态行改挂进 host,这条会提醒回来简化。
        self.assertIn("sec.appendChild(loadState)", APPROVAL)
        self.assertIn('<div class="hs-sit-approval-host"></div>', SITUATION)

    def test_header_row_itself_stays_visible_when_collapsed(self):
        # 收起不是隐藏整区:标题、计数、箭头都还在,否则没有地方可以点开。
        for sel in (".hs-sit-approvals.collapsed .hs-sit-approval-bar",
                    ".hs-sit-approvals.collapsed .hs-sit-approval-head {",
                    ".hs-sit-approvals.collapsed .hs-sit-approval-count"):
            self.assertNotIn(sel + " { display: none", CSS)
        self.assertIn(".hs-sit-approvals.collapsed .hs-sit-approval-head .appr-chev", CSS)


if __name__ == "__main__":
    unittest.main()
