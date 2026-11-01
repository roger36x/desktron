"""首屏分批铺设不得把一次交换劈成两批(2026-09-07 用户报「会话页向上跳」)。

首屏为了不卡界面分批铺:hsPourFast 先铺末尾几轮,hsPourRest 每 32ms 往前补一批。
批次边界原来落在用户消息「之后」——那条提问留在前一批,它的回答分到后一批,而后一批
先铺:回答找不到自己的用户回合,hsTurnFor 只好另开一个没有提问的回合;前一批随后补上,
又留下一个没有回答的回合。同一次交换于是被拆成两轮。

一次性铺同一份记录的 hsPour 不拆。两种铺法结构不同,此后任何一次对账重建都要重排整页,
而视口锚点按回合顶边复位,复位到的已经不是同一条线——表现为正文跳到更早的内容。
探针实测(会话 adp_218f…):载入 31 轮,重建成 25 轮,6 对拆分正好对应 6 个批次边界;
最坏的一个滚动位上,重建让参照段落位移 917px。

这里钉的是边界本身:任何一批都不能以用户消息结尾(那意味着它的回答在别的批里)。
"""
import json
import os
import re
import shutil
import subprocess
import unittest

import _srcmap

CHAT = _srcmap.chat_src()


def fn(name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), CHAT, re.S)
    assert m, name + " 不见了"
    return m.group(0)


HARNESS = r"""
const HS_FIRST_TURNS = 6;
const batches = [];
// 只记每批铺了哪些消息:这条测试问的是「怎么分批」,不是「怎么渲染」
function hsPour(v, msgs) { batches.push(msgs.map((m) => m.info.role)); }
function hsPrepend(v, msgs) { batches.unshift(msgs.map((m) => m.info.role)); }
function hsPourRestSoon(v) { while (v.pendRest && v.pendRest.length) hsPourRest(v); }
function hsHistoryReveal() {}
__FNS__
// 一段普通会话:提问、回答交替,末尾再补一条正在跑的助手消息
const msgs = [];
for (let i = 0; i < 20; i++) {
  msgs.push({ info: { role: "user", id: "u" + i } });
  msgs.push({ info: { role: "assistant", id: "a" + i } });
}
const v = { pendRest: null, restTimer: 0, el: { isConnected: true }, turns: [] };
hsPourFast(v, msgs);
const flat = [].concat(...batches);
console.log(JSON.stringify({
  batches,
  // 铺设顺序必须还原成原始顺序:一条也不能丢、不能重
  完整: flat.length === msgs.length && flat.every((r, i) => r === msgs[i].info.role),
  // 任何一批都不能以提问结尾——那说明它的回答被分到了别的批
  劈开的批: batches.filter((b) => b.length && b[b.length - 1] === "user").length,
  空批: batches.filter((b) => !b.length).length,
}));
"""


def run():
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("没有 node")
    js = HARNESS.replace("__FNS__", fn("hsHistoryTurnStart") + "\n" + fn("hsPourFast") + "\n" + fn("hsPourRest")
                         + "\n" + fn("hsReconcileAfterLoad")
                         + "\nfunction hsHidden(){ return false; }")
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


class 首屏分批(unittest.TestCase):
    def test_一次交换不跨批(self):
        r = run()
        self.assertEqual(r["劈开的批"], 0,
                         "有批次以提问结尾,它的回答会落到另一批里,回合因此被拆成两轮:%r" % (r["batches"],))

    def test_分批之后内容不增不减(self):
        r = run()
        self.assertTrue(r["完整"], "分批铺设改变了消息顺序或数量:%r" % (r["batches"],))
        self.assertEqual(r["空批"], 0)

    def test_确实分了批(self):
        """退化成一批就谈不上首屏加速,这条防止把问题「修」成不分批。"""
        self.assertGreater(len(run()["batches"]), 1)


class 贴底时只提交底部(unittest.TestCase):
    """插入更早的回合时,贴底态的不变量是「留在底部」,不是「留在同一段文字上」。
    按高度差算绝对位置只要差几像素(图片迟到、审批回执晚一拍),视口就停在离底不远处;
    页面随后静置、没有新的尺寸变化,也就没有人再把它拉回去。"""

    def test_插入更早回合时贴底走提交(self):
        seg = fn("hsPrepend")
        self.assertIn("return hsViewportChange(v, prepend)", seg)
        self.assertIn("if (v.stick) hsScrollCommit(v, true)", fn("hsViewportChange"))

    def test_冷页隐藏构建不逐批补偿视口(self):
        seg = fn("hsPrepend")
        self.assertIn("if (v.historyLoading) { prepend(); return; }", seg)
        self.assertLess(seg.index("if (v.historyLoading)"), seg.index("return hsViewportChange"))

    def test_挂回裁掉的回合仍按高度差补偿(self):
        """hsRemount 的两个调用点(hsUnstick / hsShown)都先确认了脱钩,
        所以它不需要贴底那一支;写成两支反而是死代码。"""
        seg = fn("hsRemount")
        self.assertNotIn("hsScrollCommit", seg)
        self.assertIn("hsViewportChange(v,", seg)
        self.assertIn("hsRestoreViewportAnchor(v, anchor, replace)", fn("hsViewportChange"))


class 重建锚点下钻到块(unittest.TestCase):
    """一个回合可以有几千像素高,重建前后它的内部高度会变(工具卡数量、markdown 重排)。
    只钉回合顶边的话,回合内部的内容整体位移那个高度差。"""

    def test_锚点记录块路径(self):
        seg = fn("hsViewportAnchor")
        self.assertIn("blockPath", seg)
        self.assertIn("hsBlockPath(turn.el, br.top)", seg)

    def test_复位先按块再退回回合(self):
        seg = fn("hsViewportApplyAnchor")
        self.assertIn("hsBlockAt(turn.el, a.blockPath)", seg)
        self.assertIn("turn.el.getBoundingClientRect().top - base - a.bodyOffset", seg)


if __name__ == "__main__":
    unittest.main()
