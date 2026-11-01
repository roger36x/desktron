"""多选的问答卡片要有一处说得出「就这些了」的入口。

单选是选中即走(hsAskPick → hsAskNext),多选只是就地开关,推进靠别的按钮。而问题只有一个时
翻页钮是禁用的(st.i >= qs.length - 1),于是唯一按得动的是那颗写着「跳过」的——语义正好
相反(2026-08-23 用户实报:选完之后找不到提交的地方)。

两处一起改才闭合:
  1. 多选且真的选中了东西时,在选项之后补一行「提交 / 下一问」,带已选计数;
     一个都没选时不出——那种情形本来就该走「跳过」。
  2. 「跳过」先清掉这一问的选中态再走。原来它直接调 hsAskNext,而那一步会把已经点上的
     多选项结算并交出去,按钮说的和做的相反。
"""
import json
import os
import re
import subprocess
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT_PATH = _srcmap.chat_src_file()   # 2026-08-27 拆片:node 侧读拼接整体
CHAT = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
PAINT = re.search(r"function hsAskPaint\(v\)[\s\S]+?\n}", CHAT).group(0)
PICK = re.search(r"function hsAskPick\(v, label, multi, free\)[\s\S]+?\n}", CHAT).group(0)
NEXT = re.search(r"function hsAskNext\(v\)[\s\S]+?\n}", CHAT).group(0)

# hsAskNext / hsAskPick 只碰 v.ask 与两个画界面的函数,换成空壳就能在 node 里跑
HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.env.CHAT_JS, "utf8");
for (const re of [/function hsAskPick\(v, label, multi, free\)[\s\S]+?\n}/,
                  /function hsAskNext\(v\)[\s\S]+?\n}/,
                  /function hsAskGo\(v, i\)[\s\S]+?\n}/]) eval(src.match(re)[0]);
let sent = null;
function hsAskPaint() {}
function hsAskSend(v, ans) { sent = JSON.parse(JSON.stringify(ans)); }

const mk = (multi) => ({ ask: { i: 0, qs: [{ question: "选哪个?", multiSelect: multi }],
                                sel: [new Set()], ans: {} } });

// 多选:点两项之后交上去
const a = mk(true);
hsAskPick(a, "甲", true); hsAskPick(a, "乙", true);
hsAskNext(a);
const 提交结果 = sent;

// 多选:点了两项之后走「跳过」那条路(清空再走)
const b = mk(true);
hsAskPick(b, "甲", true); hsAskPick(b, "乙", true);
b.ask.sel[0].clear(); delete b.ask.ans["选哪个?"];
hsAskNext(b);
const 跳过结果 = sent;

// 单选:点一下就该直接交
const c = mk(false);
sent = null;
hsAskPick(c, "丙", false);
const 单选结果 = sent;

console.log(JSON.stringify({ 提交结果, 跳过结果, 单选结果 }));
"""


def _run():
    env = dict(os.environ, CHAT_JS=CHAT_PATH)
    out = subprocess.run(["node", "-e", HARNESS], env=env,
                         capture_output=True, text=True, timeout=60, encoding='utf-8')
    if out.returncode:
        raise AssertionError(out.stderr[-800:])
    return json.loads(out.stdout.strip().splitlines()[-1])


class 推进的语义(unittest.TestCase):
    def test_多选交上去的是选中的那几项(self):
        self.assertEqual(_run()["提交结果"], {"选哪个?": ["甲", "乙"]})

    def test_跳过之后什么都不带(self):
        """跳过 = 不选上面那些;已经点上的也不算数。"""
        self.assertEqual(_run()["跳过结果"], {})

    def test_单选仍是选中即交(self):
        self.assertEqual(_run()["单选结果"], {"选哪个?": "丙"})


class 提交行(unittest.TestCase):
    def test_只在多选时出(self):
        self.assertIn("if (q.multiSelect && st.sel[st.i].size) {", PAINT)
        self.assertIn('done.className = "hs-qp-r hs-qp-done"', PAINT)

    def test_一个都没选时不出(self):
        """那种情形本来就该走「跳过」,再摆一个提交只会让人以为交了空的。"""
        m = re.search(r"if \(q\.multiSelect && st\.sel\[st\.i\]\.size\) \{[\s\S]+?\n  \}", PAINT)
        self.assertTrue(m, "提交行没有挂在「有选中」这个判断里")
        self.assertIn("hsAskNext(v)", m.group(0))

    def test_最后一问说提交_中间说下一问(self):
        self.assertIn('last ? "提交" : "下一问"', PAINT)
        self.assertIn("const last = st.i >= st.qs.length - 1;", PAINT)

    def test_带上已选计数(self):
        self.assertIn("已选 ${st.sel[st.i].size} 项", PAINT)

    def test_样式与普通选项分得开(self):
        """选项是「选什么」,这一行是「就这些了」,图标那一格常态就点亮。"""
        self.assertIn(".hs-page .hs-qp-done .hs-qp-i", CSS)


class 跳过真的跳过(unittest.TestCase):
    def test_先清选中再走(self):
        m = re.search(r'own\.querySelector\("\.hs-qp-skip"\)\.onclick[\s\S]+?\n  \};', PAINT)
        self.assertTrue(m, "找不到跳过的处理")
        body = m.group(0)
        self.assertIn("st.sel[st.i].clear();", body)
        self.assertIn("delete st.ans[q.question];", body)
        self.assertLess(body.index("clear()"), body.index("hsAskNext(v)"))


if __name__ == "__main__":
    unittest.main()
