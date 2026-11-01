"""按 ↑ 翻历史之前手上那段草稿要留住,按 ↓ 翻到底是回到它,不是清空。

原来 hsHistory 翻到底一律填空串:人写了一半,按一下 ↑ 想看看上一条指令,再按 ↓ 想回来,
写的那段就没了,而 ↑ 本身没有任何提示说它会吃掉当前输入(2026-08-21 用户实报)。

两处一起改才闭合:
  1. 头一次翻进历史时把当前正文存进 v.histDraft,翻到底时填回去;
  2. hsSend 里清掉 histIdx / histDraft——那一段已经发走,不再是「翻历史之前手上那段」,
     不清的话发完直接按 ↑ 再按 ↓,会把早就发出去的草稿又填回来。

取值走 hsInpText 而不是 textContent:contenteditable 的换行有三种形态(文本节点里的 \\n、
<br>、块级边界),textContent 只认第一种,多行草稿存出来会连成一整行。
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
HIST = re.search(r"function hsHistory\(v, step\)[\s\S]+?\n}", CHAT).group(0)
SEND = re.search(r"async function hsSend\(v\)[\s\S]+?\n}\n", CHAT).group(0)

# hsHistory 只碰 v 与几个画界面的函数,把那几个换成空壳就能在 node 里直接跑。
HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.env.CHAT_JS, "utf8");
const fn = src.match(/function hsHistory\(v, step\)[\s\S]+?\n}/)[0];
function hsInpText(el) { return el.textContent; }
function hsCmdPaint() {}
function hsSlashHide() {}
globalThis.getSelection = () => ({ removeAllRanges() {}, addRange() {} });
globalThis.document = { createRange: () => ({ selectNodeContents() {}, collapse() {} }) };
eval(fn);

const v = { turns: [{ user: "第一条" }, { user: "第二条" }], inp: { textContent: "写了一半的草稿" } };
const seen = [];
hsHistory(v, -1); seen.push(v.inp.textContent);   // ↑ 最近一条
hsHistory(v, -1); seen.push(v.inp.textContent);   // ↑ 再上一条
hsHistory(v, 1);  seen.push(v.inp.textContent);   // ↓
hsHistory(v, 1);  seen.push(v.inp.textContent);   // ↓ 翻到底,该回到草稿
hsHistory(v, 1);  seen.push(v.inp.textContent);   // 已经到底,不动

// 空输入框上翻,翻到底应当仍是空
const e = { turns: [{ user: "唯一一条" }], inp: { textContent: "" } };
hsHistory(e, -1); const eUp = e.inp.textContent;
hsHistory(e, 1);  const eBack = e.inp.textContent;

console.log(JSON.stringify({ seen, eUp, eBack }));
"""


def _run():
    env = dict(os.environ, CHAT_JS=CHAT_PATH)
    out = subprocess.run(["node", "-e", HARNESS], env=env,
                         capture_output=True, text=True, timeout=60, encoding='utf-8')
    if out.returncode:
        raise AssertionError(out.stderr[-800:])
    return json.loads(out.stdout.strip().splitlines()[-1])


class 翻历史留住草稿(unittest.TestCase):
    def test_上翻再下翻草稿回得来(self):
        r = _run()
        self.assertEqual(r["seen"], ["第二条", "第一条", "第二条",
                                     "写了一半的草稿", "写了一半的草稿"])

    def test_到底之后再按下键不动(self):
        """翻到底就是回到草稿这一格,再按不该越过去变成空。"""
        r = _run()
        self.assertEqual(r["seen"][-1], r["seen"][-2])

    def test_本来就空的仍是空(self):
        r = _run()
        self.assertEqual(r["eUp"], "唯一一条")
        self.assertEqual(r["eBack"], "")


class 存取的口径(unittest.TestCase):
    def test_存草稿用hsInpText不用textContent(self):
        """textContent 只认三种换行里的一种,多行草稿存出来会连成一行。"""
        self.assertIn("v.histDraft = hsInpText(v.inp);", HIST)

    def test_只在头一次进历史时存(self):
        """每翻一格都存的话,存下来的就是上一条历史,不是草稿。"""
        self.assertEqual(HIST.count("v.histDraft = hsInpText"), 1)
        m = re.search(r"if \(v\.histIdx === undefined \|\| v\.histIdx < 0\) \{[\s\S]+?\n  \}", HIST)
        self.assertTrue(m, "存草稿要挂在「还没进历史」这个判断里")
        self.assertIn("v.histDraft = hsInpText(v.inp);", m.group(0))

    def test_翻到底填草稿而不是空串(self):
        self.assertIn('next === list.length ? (v.histDraft || "") : list[next]', HIST)
        self.assertNotIn('next === list.length ? "" :', HIST)

    def test_发出去之后草稿作废(self):
        """三条分支(本地命令 / 回答面板 / 正常发送)都要过这一句,所以它在分流之前。"""
        self.assertIn('v.histIdx = -1; v.histDraft = "";', SEND)
        head = SEND[:SEND.index("const localCmd")]
        self.assertIn('v.histDraft = "";', head)


if __name__ == "__main__":
    unittest.main()
