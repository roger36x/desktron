"""刻度列钉在「标题栏下沿到输入框上沿」的正中,不跟着输入框走。

它原来挂在 .hs-wrap 里,而 wrap 是 flex:1——输入框一长,wrap 就被挤矮,定位点
跟着上移(2026-08-20 用户实见)。刻度量的是整段会话在哪儿,与输入框多高无关,所以定位的
参照物该是整页。

.hs-page 本来就是 position: relative,把它移到页一级即可;.hs-wrap 的 relative 不能撤——
子代理那一层(.hs-agview)靠它铺满正文区,不是铺满整页。

定位点原先取页高的 38.2%——那是按整页切的一刀,与这一列所标注的正文没有关系。
2026-09-10 改成正文区的正中:定位基准仍是 .hs-page,但它的下沿是窗口底而不是输入框上沿
(输入区也在这个盒子里),所以要把输入区那一截减掉,写成 calc(50% - 输入区高/2)。

减的是「输入框为空、任务清单收起时」的输入区高度(--hs-comp-empty,由
app-chat-overlays.js 的 ResizeObserver 写入),不是当前高度:按当前高度算的话,每敲一行
或展开任务时刻度都会往上挪。空态行高按 line-height 与 min-height 取大者算,不写死
24px——手机那档字号 16px,行高已经超过这个下限。

WebKit 探针实测(三档窗口,标题栏下沿 44,输入区空态 149):

    1680×1000   输入框上沿 851   应在 447   刻度 448
    1400×900    输入框上沿 751   应在 397   刻度 398
    1100×760    输入框上沿 611   应在 327   刻度 328

差 1px 是 calc 的取整。三档都把输入框顶到 200px(增高 176)复测,刻度位移均为 0。
"""
import os
import re
import subprocess
import unittest

import _srcmap
from test_frontend_recall import fn

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


class TocSitsOnThePage(unittest.TestCase):
    def test_刻度列是页的直接子节点(self):
        seg = CHAT[CHAT.index('el.classList.add("hs-page");'):][:600]
        i, j = seg.index('<div class="hs-toc">'), seg.index('<div class="hs-wrap">')
        self.assertLess(i, j, "刻度列要排在 .hs-wrap 之前,不在它里面")

    def test_页是定位参照物(self):
        self.assertIn("position: relative", re.search(r"\n\.hs-page \{([^}]*)\}", CSS).group(1))

    def test_wrap_的相对定位不能撤(self):
        """子代理那一层靠它铺满正文区(.hs-agview 的 inset:0),不是铺满整页。"""
        self.assertIn("position: relative", re.search(r"\.hs-page \.hs-wrap \{([^}]*)\}", CSS).group(1))
        self.assertIn("position: absolute; inset: 0", re.search(r"\.hs-page \.hs-agview \{([^}]*)\}", CSS).group(1))

    def test_刻度中线在正文区的正中(self):
        r = re.search(r"\.hs-page \.hs-toc \{([^}]*)\}", CSS).group(1)
        self.assertIn("top: calc(50% - var(--hs-comp-empty, 0px) / 2)", r)
        self.assertNotIn("38.2%", r)   # 黄金分割那一刀已经撤了
        self.assertIn("translateY(-50%)", r)
        self.assertIn("max-height: 66%", r)

    def test_手机刻度缩至三分之二桌面粗细和步距不变(self):
        normal = re.search(r"\.hs-page \.hs-tk \{([^}]*)\}", CSS).group(1)
        active = re.search(r"\n\.hs-page \.hs-tk\.on \{([^}]*)\}", CSS).group(1)
        for rule in ("width: 6.5px", "height: 1.75px", "margin: .125px 0"):
            self.assertIn(rule, normal)
        for rule in ("width: 15px", "height: 2.25px", "margin: -.125px 0"):
            self.assertIn(rule, active)
        toc = re.search(r"\.hs-page \.hs-toc \{([^}]*)\}", CSS).group(1)
        for rule in ("width: 26px", "gap: 7px", "padding: 6px 0"):
            self.assertIn(rule, toc)
        self.assertIn(".hs-page .hs-tk { width: calc(20px / 3); }", CSS)
        self.assertIn(".hs-page .hs-tk.on { width: 12px; }", CSS)
        self.assertIn(".hs-page .hs-toc { left: 2px; width: 18px; padding-left: 2px; max-height: 66%; }", CSS)

    def test_手机正文输入区和刻度预览共用收窄后的左沟槽(self):
        mobile = CSS[CSS.index('/* ---- 会话页的手机布局'):CSS.index('/* 档位行:', CSS.index('/* ---- 会话页的手机布局'))]
        self.assertIn('@media (max-width: 820px), (max-height: 520px) and (pointer: coarse)', mobile)
        self.assertIn('.hs-page .hs-wrap, .hs-page .hs-composer { --hs-pad-l: 22px; --hs-pad-r: 20px; }', mobile)
        self.assertIn('.hs-page .hs-peek { left: 22px; width: auto; max-width: none; right: 14px; }', mobile)
        self.assertIn('padding: 0 var(--hs-pad-r) 0 var(--hs-pad-l)', CSS)
        self.assertIn('padding: 4px var(--hs-pad-r) 20px var(--hs-pad-l)', CSS)

    def test_减的是输入框为空时的高度(self):
        """按当前高度算的话,每敲一行刻度就往上挪一截——它标的是正文,不该被打字牵动。"""
        overlays = open(os.path.join(ROOT, "frontend", "app-chat-overlays.js"), encoding="utf-8").read()
        seg = re.search(r"function hsTocAnchor\(v\) \{[\s\S]+?\n\}", overlays)
        self.assertIsNotNone(seg)
        seg = seg.group(0)
        # 空态 = 当前输入区高 - 输入框比一行多出来的部分 - 任务展开新增的部分
        self.assertIn("const grow = Math.max(0, inp.getBoundingClientRect().height - line);", seg)
        self.assertIn('composer.querySelector(".hs-tasks.open")', seg)
        self.assertIn('querySelectorAll(".hs-tasks-list, .hs-tasks-add")', seg)
        self.assertIn("composer.getBoundingClientRect().height - grow - taskGrow", seg)
        self.assertIn('v.el.style.setProperty("--hs-comp-empty", empty + "px")', seg)
        # 行高不能写死:手机那档字号 16px,行高已经超过 min-height 的 24px。
        self.assertIn("Math.max(parseFloat(cs.lineHeight) || 0, parseFloat(cs.minHeight) || 0)", seg)
        # 每敲一个字都会触发观察,值没变不要碰样式
        self.assertIn("if (v.tocCompEmpty === empty) return;", seg)

    def test_输入框变化时会重算(self):
        """ResizeObserver 已经在看 .hs-composer;刻度锚点挂在同一个回调里,不另起一套观察。"""
        overlays = open(os.path.join(ROOT, "frontend", "app-chat-overlays.js"), encoding="utf-8").read()
        seg = re.search(r"function hsObserveLayout\(v\) \{[\s\S]+?\n\}", overlays).group(0)
        # 经 globalThis 可选调用:回调段被 test_frontend_viewport_controller 单独截出来 eval,
        # 同文件的函数在那个上下文里并不存在,直呼名字会 ReferenceError。
        self.assertIn("globalThis.hsTocAnchor?.(v);", seg)
        self.assertIn('v.el.querySelector(".hs-composer")', seg)

    def test_任务展开不改变空态高度(self):
        """任务清单摊开只让输入区临时变高,不该改写刻度的锚点。"""
        script = r'''
const assert = require('node:assert/strict');
function getComputedStyle() { return {lineHeight: '22px', minHeight: '24px'}; }
class El {
  constructor(height) { this.height = height; this.style = {writes: [], setProperty: (k, v) => this.style.writes.push([k, v])}; }
  getBoundingClientRect() { return {height: this.height}; }
  querySelector(sel) { return this.nodes && this.nodes[sel] || null; }
  querySelectorAll(sel) { return this.all && this.all[sel] || []; }
  get isConnected() { return true; }
}
const inp = new El(24), composer = new El(149), page = new El(0);
page.nodes = {'.hs-composer': composer};
const v = {inp, el: page};
hsTocAnchor(v);
assert.equal(v.tocCompEmpty, 149);
assert.deepEqual(page.style.writes.at(-1), ['--hs-comp-empty', '149px']);
// 输入框长高 50px,composer 同步长高 50px；空态高度不应变。
inp.height = 74; composer.height = 199;
hsTocAnchor(v);
assert.equal(v.tocCompEmpty, 149);
assert.equal(page.style.writes.length, 1);
// 任务列表展开 100px,composer 同步长高 100px；空态高度仍不应变。
const tasks = new El(0), list = new El(80), add = new El(20);
tasks.all = {'.hs-tasks-list, .hs-tasks-add': [list, add]};
composer.nodes = {'.hs-tasks.open': tasks};
composer.height = 299;
hsTocAnchor(v);
assert.equal(v.tocCompEmpty, 149);
assert.equal(page.style.writes.length, 1);
'''
        result = subprocess.run(["node", "-e", fn("app-chat-overlays.js", "hsTocAnchor") + script],
                                capture_output=True, text=True, timeout=10, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
