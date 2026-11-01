"""点开一段会话,落在最新那几句上。

hsPin 自己带一道 `if (!v.stick) return`——那是给跑动期间准备的:人往上翻之后不该被流式输出
一帧帧拽回底部。但 stick 一旦被 hsUnstick 置假就一直是假,于是再打开这一页时,那道守卫把
「打开即钉底」也一并挡掉,停在上次读到的地方(2026-08-20 用户实见)。openHtmlSession 本来
就在调 hsPin,只是那一调被自己的守卫吃掉了。

所以在明确「打开这一页」时把 stick 重新打开再钉。刷新恢复走的是 show=false,不经过这一支,
位置照旧——那一路要的正是「回到上次读到的地方」。

受控复现(无头浏览器,真实会话):
    开 A → 离底 0;在 A 里往上翻 → 离底 14932;切到 B → 0;切回 A → 0;
    show=false 恢复 → 保持原位。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()


def fn(name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), CHAT, re.S)
    assert m, name
    return m.group(0)


class OpenLandsAtBottom(unittest.TestCase):
    def test_打开时先把钉底打开(self):
        seg = fn("openHtmlSession")
        self.assertIn("hsSetStick(v, true);", seg)
        self.assertLess(seg.index("hsSetStick(v, true);"), seg.index("hsPin(v, 600)"))

    def test_只在真的要显示时重置(self):
        """刷新恢复(show=false)不能被拽到底部:那一路要的是回到上次读到的地方。"""
        seg = fn("openHtmlSession")
        i = seg.index("hsSetStick(v, true);")
        self.assertIn("if (show && v) {", seg[:i])

    def test_钉底那道守卫还在(self):
        """守卫本身是对的:跑动期间人往上翻,不该被一帧帧拽回去。这次只是在打开那一刻绕开它。"""
        seg = fn("hsPin")
        self.assertIn("if (!v.stick) return", seg)
        self.assertIn("!v.body.isConnected", fn("hsViewportCommit"))
        self.assertIn("return;", seg)

    def test_灌完历史那一路也自己钉一次(self):
        """冷页(needLoad)走的是 hsLoad,它末尾另有一次 stick + pin,与这次的改动互不依赖。"""
        seg = fn("hsLoad")
        self.assertIn("hsSetStick(v, true);", seg)
        self.assertIn("hsPin(v);", seg)


if __name__ == "__main__":
    unittest.main()
