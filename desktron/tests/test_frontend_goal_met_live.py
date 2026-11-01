"""「目标已达成」那条分割线在达成的当下就出现,不用刷新。

这条线平时是从记录里画的:hsPour 见到消息上的 info.goal 就画一条。而记录要等对账重建之后
才读得到,于是达成之后界面上什么也不变,得刷新一次才看见(2026-08-20 用户实见)。

目标没有实时事件可听,只在建页与每轮收尾各取一次(hsGoalSync)。所以补在那里:这一次取回来
如果看到「由未达成变成达成」,当场往流末补一条。三处边界:
  · 首次同步不补——记录里本来就有,补了是重复;
  · 达成之后反复同步也不补,只认那一次跳变;
  · 之后的重建以记录为准整片重画,这条会被换掉,同样不重复；
  · 达成行归入上一回合末尾，不作为会话流里的游离节点。

受控验证(把 /goal 那一路换成桩):未达成 0 条 → 仍未达成 0 条 → 这一刻达成 1 条 →
再同步 1 条;另起一页「开机即达成」0 条。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
SYNC = re.search(r"async function hsGoalSync\(v\)[\s\S]+?\n}", CHAT).group(0)


class GoalMetShowsAtOnce(unittest.TestCase):
    def test_goal发出后先显示再等待权威回执(self):
        dispatch = re.search(r"async function hsDispatch\(v, text, atts\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn("v.goalPending = pending", dispatch)
        self.assertIn("hsGoalPaint(v, pending)", dispatch)
        self.assertIn("30000", dispatch)

    def test_空的旧接口不会抹掉待确认目标(self):
        self.assertIn("const pending = v.goalPending", SYNC)
        self.assertIn("g = pending", SYNC)

    def test_旧后端下从仍运行的goal回合恢复(self):
        seed = re.search(r"function hsGoalSeed\(v, msgs\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn('x.role === "assistant" && x.running', seed)
        self.assertIn("if (!match) return", seed)
        load = re.search(r"async function hsLoad\(v\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn("hsGoalSeed(v, msgs)", load)

    def test_同步时认跳变(self):
        self.assertIn('const sig = (g && g.condition) ? g.condition + "|" + (g.met ? "1" : "0") : "";', SYNC)
        self.assertIn("const prev = v.goalSig;", SYNC)
        self.assertIn("if (prev !== undefined && prev !== sig && g && g.met && g.condition) hsGoalRow(v, g, null);", SYNC)

    def test_首次同步不补(self):
        """prev 为 undefined 就是这一页的第一次同步:记录里已经画过了。"""
        self.assertIn("prev !== undefined", SYNC)

    def test_补的那条归入上一回合末尾(self):
        """它属于上一轮收尾，不能成为流里的游离节点。"""
        self.assertIn("hsGoalRow(v, g, null)", SYNC)
        row = re.search(r"function hsGoalRow\(v, g, turn\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn("const prev = v.turns[v.turns.length - 1]", row)
        self.assertIn("prev.el.appendChild(el)", row)

    def test_收尾时会同步一次(self):
        """没有实时事件可听,靠的就是回合边界这一次。"""
        self.assertIn("hsGoalSync(v); hsAgentsSync(v); hsTasksSync(v);", CHAT)


if __name__ == "__main__":
    unittest.main()
