"""引子行归过程区。

模型动手前常写一句「Now the frontend removals:」,它引出的是紧接着的工具调用。
工具调用收在「处理中」的折叠里,引子留在正文就成了一句悬在半空的话——冒号之后什么都没有。
判据必须收紧:短、以冒号收尾、后面紧跟工具调用,三者同时成立才挪。
"""
import json
import os
import re
import shutil
import subprocess
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT_JS = os.path.join(ROOT, "frontend", "app-chat.js")

HARNESS = r"""
SRC
const cases = CASES;
const out = cases.map(([prev, kind]) => hsIsLead({ type: kind, text: prev }));
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class LeadLine(unittest.TestCase):
    def _src(self):
        return _srcmap.chat_src()

    def _lead(self, cases):
        src = self._src()
        parts = [re.search(r"^const HS_LEAD_MAX = .*$", src, re.M).group(0),
                 re.search(r"\nfunction hsIsLead\(p\) \{.*?\n}\n", src, re.S).group(0)]
        js = (HARNESS.replace("SRC", "\n".join(parts))
              .replace("CASES", json.dumps(cases, ensure_ascii=False)))
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_冒号收尾的短句算引子(self):
        got = self._lead([["Now the frontend removals:", "text"],
                          ["Now the AGENTS.md 字段词表:", "text"],
                          ["接下来改这几处：", "text"]])
        self.assertEqual(got, [True, True, True])

    def test_不以冒号收尾的不算(self):
        got = self._lead([["Now the backend removals.", "text"], ["好的", "text"]])
        self.assertEqual(got, [False, False])

    def test_长段落与多行不算(self):
        long_line = "这是一段很长的说明" * 20 + ":"
        got = self._lead([[long_line, "text"], ["第一行\n第二行:", "text"]])
        self.assertEqual(got, [False, False])

    def test_思考与工具不算(self):
        got = self._lead([["在想什么:", "reasoning"], ["", "text"]])
        self.assertEqual(got, [False, False])

    def test_已归过程区的引子重建后不退回正文(self):
        # 整片重建按序重画每一块;引子已在过程区,再按正文接回去就会重建一次退回一次。
        src = self._src()
        seg = re.search(r"\nfunction hsPaint\(v, part\) \{.*?\n}\n", src, re.S).group(0)
        self.assertIn("} else if (part.lead) {", seg)
        self.assertLess(seg.index("} else if (part.lead) {"),
                        seg.index("} else if (!part.turn.holder.contains(part.el)) {"))

    def test_不按位置回收正文(self):
        # 位置分不开两种中途叙述:结论会被推翻的阶段说明,和提前给出、后文不再重复的状态
        # 汇报。按位置回收会把后者藏进折叠的过程区。判据只保留收紧的引子形态。
        src = self._src()
        self.assertNotIn("hsNarrationBack", src)

    def test_挪动发生在工具行建立之前(self):
        # 顺序要求:引子先进过程区,紧跟着才是它引出的那一步,否则两行会颠倒。
        src = self._src()
        seg = re.search(r"function hsPaintTool\(v, part, host\) \{.*?\n  \}", src, re.S).group(0)
        self.assertLess(seg.index("hsLeadIn(part)"),
                        seg.index("target.appendChild(part.el)"))
