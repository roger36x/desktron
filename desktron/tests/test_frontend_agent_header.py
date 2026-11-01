"""子代理这一层的头:标题与正文左对齐,右侧那枚钮说清楚它只是收起、不停子代理。

对齐:头内层(.hs-agv-hin)与正文各块本来就都收在 --hs-col 这一列里,两者的左缘实测
都是 544。错位来自列内的第一个 flex 项——状态点排在标题前面,把标题推到了 560,
比正文右 16px(2026-08-24 用户实见)。点是这一行的标记不是内容,用负 margin 挂到列外的
空白里(量 = 点宽 6px + 行 gap 10px),标题就落回 544。用负 margin 不用绝对定位,
是为了保住 flex 那套垂直居中。

那枚钮:hsAgentClose 里没有任何停止调用(清轮询、摘掉浮层、重算入口就完了),子代理
继续在后台跑。原来画的是一个 ×,读起来像「终止」——同一个位置在提问面板与审阅侧栏
确实是关掉一件事,这里却不是。现在保留「主线」文字,方向提示用图标而不是文字箭头。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
JS = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
CLOSE = re.search(r"function hsAgentClose\(v\)[\s\S]+?\n}", JS).group(0)


class 标题与正文同一列(unittest.TestCase):
    def test_状态点挂到列外(self):
        m = re.search(r"\n\.hs-page \.hs-agv-h \.rp-dot \{[^}]*\}", CSS)
        self.assertTrue(m, "找不到头里状态点的规则")
        self.assertIn("margin-left: -16px", m.group(0))

    def test_负margin的量对得上点宽加gap(self):
        """点宽 6px(.rp-dot)+ 行 gap 10px(.hs-agv-hin)= 16px。任一处改了这里要跟着改。"""
        dot = re.findall(r"\n\.rp-dot \{[^}]*\}", CSS)[-1]
        self.assertIn("width: 6px", dot)
        hin = re.search(r"\n\.hs-page \.hs-agv-hin \{[^}]*\}", CSS).group(0)
        self.assertIn("gap: 10px", hin)

    def test_头与正文取同一个列宽token(self):
        hin = re.search(r"\n\.hs-page \.hs-agv-hin \{[^}]*\}", CSS).group(0)
        self.assertIn("max-width: var(--hs-col)", hin)
        body = re.search(r"\.hs-page \.hs-agv-turn \{[^}]*\}", CSS).group(0)
        self.assertIn("max-width: var(--hs-col)", body)


class 返回钮的语义(unittest.TestCase):
    def test_关闭不停子代理(self):
        """判据落在实现上:这个函数里不该出现任何停止/中断类的调用。"""
        for bad in ("api(", "stop", "abort", "kill", "DELETE"):
            self.assertNotIn(bad, CLOSE, "hsAgentClose 里不该有 %s" % bad)

    def test_钮上写明是返回主线(self):
        m = re.search(r'<button class="hs-agv-x"[^>]*>[\s\S]{0,200}?</button>', JS)
        self.assertTrue(m, "找不到那枚钮的模板")
        self.assertIn('DeskI18n.t("agent.backToMain")', m.group(0))
        self.assertIn('class="ui-arrow-icon is-left"', m.group(0))
        self.assertIn('hsEsc(DeskI18n.t("ctx.main"))', m.group(0))
        self.assertNotIn('←', m.group(0))

    def test_不再是一个叉(self):
        """× 在别处(提问面板/审阅侧栏)是「关掉一件事」,这里语义不同,不该共用同一个形。"""
        m = re.search(r'<button class="hs-agv-x"[^>]*>[\s\S]{0,200}?</button>', JS).group(0)
        self.assertNotIn("qclose", m)

    def test_有提示说明子代理还在跑(self):
        m = re.search(r'<button class="hs-agv-x"([^>]*)>', JS).group(1)
        self.assertIn("title=", m)

    def test_切换菜单里那条主线用同一个函数(self):
        """两处都是「回主线」,不该一处收起一处另做一套。"""
        pick = re.search(r"function hsAgentPick\(v, anchor\)[\s\S]+?\n}", JS).group(0)
        self.assertIn('label: DeskI18n.t("ctx.main")', pick)
        self.assertIn("hsAgentClose(v)", pick)


if __name__ == "__main__":
    unittest.main()
