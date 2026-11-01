"""会话框架的碎片不进正文。

harness 记账用的标签偶尔会被引擎当成正文吐出来——2026-08-16 实见一条正常回复的末尾跟着
`system<total_tokens>15000000 tokens left</total_tokens>`。它不是谁说的话,也不是要人处理的
系统信息(那类走输入框上方那条窄条,见 hsEnvironment)。

与 isInjected 的分工:那个判「整条都是注入」,整块丢掉;这里剥的是挂在正常回复尾巴上的一小段,
整条丢会把回复也丢掉,所以只能剥。剥在渲染这一侧,存储里那份不动——它是引擎记录的投影,
改它等于篡改原始记录,而且下一次以记录为准重建又会回来。
"""
import json
import os
import re
import shutil
import subprocess
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CORE = open(os.path.join(ROOT, "frontend", "app-core.js"), encoding="utf-8").read()
CHAT = _srcmap.chat_src()


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class StripFraming(unittest.TestCase):
    def _run(self, cases):
        m = re.search(r"const FRAMING_RE[\s\S]*?\nfunction stripFraming\([\s\S]*?\n}\n", CORE)
        self.assertIsNotNone(m, "stripFraming 不见了")
        js = m.group(0) + "\nconsole.log(JSON.stringify(%s.map(stripFraming)));" % json.dumps(
            cases, ensure_ascii=False)
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_剥掉尾巴保住正文(self):
        got = self._run(["正文。\n\nurl请继续\n\nsystem<total_tokens>15000000 tokens left</total_tokens>"])
        self.assertEqual(got, ["正文。\n\nurl请继续"])

    def test_整段就是碎片时剩空(self):
        self.assertEqual(self._run(["<total_tokens>9 left</total_tokens>"]), [""])

    def test_夹在中间也剥_不粘连上下文(self):
        self.assertEqual(self._run(["前\n<total_tokens>9</total_tokens>\n后"]), ["前\n\n后"])

    def test_半截标签不动(self):
        # 流式期间标签会分两片到达,认半截会把还没说完的正文误伤
        s = "正文里提到 <total_tokens 半截"
        self.assertEqual(self._run([s]), [s])

    def test_无关正文原样(self):
        for s in ["普通正文", "讲 token 预算的一段话", ""]:
            self.assertEqual(self._run([s]), [s])

    def test_shepherd_history引用不进入可见正文(self):
        got = self._run([
            "正文。\n[Shepherd history: adp_1932c67a282842#84]",
            "[Shepherd history: adp_1932c67a282842#84]",
            "正文里提到 Shepherd history 这个词",
        ])
        self.assertEqual(got, ["正文。", "", "正文里提到 Shepherd history 这个词"])


class WiredIntoRendering(unittest.TestCase):
    def test_三条渲染路径都过一遍(self):
        # 终态、流式、思考块统一经过 hsNarrativePieces；流式底层保留直接调用时的兜底。
        pieces = re.search(r"function hsNarrativePieces\(.*?\n}", CHAT, re.S).group(0)
        paint = re.search(r"function hsPaint\(.*?\n}", CHAT, re.S).group(0)
        self.assertIn("const txt = stripFraming(part.text).trim();", pieces)
        self.assertEqual(paint.count("hsNarrativePieces(part,"), 3)
        self.assertEqual(CHAT.count("stripFraming(part.text)"), 2)

    def test_复制也过一遍(self):
        m = re.search(r"function hsCopy\(text\) \{[\s\S]*?\n}", CHAT)
        self.assertIsNotNone(m)
        self.assertIn("text = stripFraming(text);", m.group(0))
        self.assertLess(m.group(0).index("stripFraming"), m.group(0).index("clipboard"))

    def test_不动存储(self):
        # 存储是引擎记录的投影,改它等于篡改原始记录,下一次重建又会回来
        self.assertNotIn("stripFraming", _srcmap.engine_adapters_src())


if __name__ == "__main__":
    unittest.main()
