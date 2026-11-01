"""长回复流式期间不能把主线程占满。

旧实现的每次流式重绘都会整段重设 innerHTML。现在已定型节点被保留，只替换活动尾部；
但长尾和 markdown 解析仍需按长度节流，避免一段两万字的回复占满主线程。
2026-08-15 实见的现象正是「左侧栏完全卡住、预览卡收不掉,但聊天能滚」。

整段提交的引擎最先撞上:不发增量,一次一整段,一块几千字连着排空。

修法是按正文长度给重绘拉开最小间隔(hsPaintGap),短正文照旧每帧。这里守住阈值单调、
两条流式入口都走节流,以及收尾时欠着的那次补绘会被还上。
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


class StreamPaintThrottle(unittest.TestCase):
    def setUp(self):
        self.src = _srcmap.chat_src()

    def _fn(self, name):
        m = re.search(r"\nfunction %s\(.*?\n}" % re.escape(name), self.src, re.S)
        self.assertIsNotNone(m, "%s 不见了" % name)
        return m.group(0)

    @unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
    def test_间隔随长度单调不减且短文不节流(self):
        js = self._fn("hsPaintGap") + "\nconsole.log(JSON.stringify(%s.map(hsPaintGap)));" % \
            json.dumps([0, 500, 3999, 4000, 11999, 12000, 39999, 40000, 200000])
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        gaps = json.loads(out.stdout)
        self.assertEqual(gaps[:3], [0, 0, 0], "四千字以内应当照旧每帧绘")
        self.assertTrue(all(b >= a for a, b in zip(gaps, gaps[1:])), gaps)
        self.assertGreaterEqual(gaps[-1], 200, "超长正文的间隔要拉得开")

    def test_打字机走节流入口(self):
        seg = self._fn("hsQueue")
        self.assertIn("hsPaintLive(v, part)", seg)
        self.assertNotIn("hsPaint(v, part)", seg)

    def test_part_updated_走节流入口(self):
        m = re.search(r'if \(type === "message\.part\.updated"\) \{.*?\n  \}', self.src, re.S)
        self.assertIsNotNone(m)
        self.assertIn("hsPaintLive(v, part)", m.group(0))

    def test_工具与终态不节流(self):
        seg = self._fn("hsPaintLive")
        # 工具行与终态都在算节流间隔之前就直接画掉:工具那一支只在真重建时多套一层布局事务
        self.assertLess(seg.index('if (part.type === "tool") {'), seg.index("hsPaintGap("))
        self.assertLess(seg.index("if (part.done || part.settled) { hsPaint(v, part); return; }"),
                        seg.index("hsPaintGap("))

    def test_收尾把欠的那次补绘还上(self):
        seg = self._fn("hsFlush")
        self.assertIn("hsPaintDone(part)", seg)
        self.assertIn("else if (pending) hsPaint(v, part)", seg)

    def test_终态渲染前先撤掉排着的补绘(self):
        seg = self._fn("hsFinalize")
        self.assertIn("hsPaintDone(part)", seg)
        self.assertLess(seg.index("hsPaintDone(part)"), seg.index("hsPaint(v, part)"))

    def test_流式绘制不再覆盖整块正文(self):
        paint = self._fn("hsPaint")
        self.assertIn("hsStreamPaint(part, part.el", paint)
        self.assertNotIn("part.el.innerHTML = hsStreamHtml", paint)
        patch = self._fn("hsStreamPatchTail")
        self.assertIn("mark.nextSibling", patch)

    @unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
    def test_切点按换行不是空行(self):
        """标题、列表在换行处即完整,不必等空行;未闭合围栏仍退回素排。"""
        cases = [["## 标题\n正文第一行", 5], ["- 项1\n- 项2", 4],
                 ["第一行\n第二行\n第三行", 7], ["```py\ncode line", 0]]
        js = (self._fn("hsSafeCut") + "\nconst cases = " + json.dumps(cases, ensure_ascii=False)
              + ";\nfor (const [c, want] of cases) { const got = hsSafeCut(c);"
              + " if (got !== want) { console.log(c + ' => ' + got + ' != ' + want); process.exit(1); } }"
              + "\nconsole.log('OK');")
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("OK", out.stdout)


if __name__ == "__main__":
    unittest.main()
