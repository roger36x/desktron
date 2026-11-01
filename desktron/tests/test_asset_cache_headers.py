"""打不上 ?v= 指纹的静态资源也该允许缓存(2026-08-26 实测手机端刷新)。

缓存分档原来只认两种:带 ?v=<指纹> 的长缓存,/vendor/ 下的 no-cache,其余一律 no-store。
而图标散落在应用清单与 CSS 里、字体由 @font-face 引,都不经 index.html 注入指纹,
于是全落进 no-store 那一支——每次刷新整份重下。

实测两个应用图标 31KB、各花 0.5~0.8 秒,而且它们排在 DOMContentLoaded 之后、
挡在 load 事件前面:一次刷新的尾巴因此多出大半秒。

它们几乎不变,给 no-cache 就够——允许存,每次问一下,没改过是一个 304 空身。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = _srcmap.ui_server_src()


def _re():
    """把判扩展名的那个正则抠出来单独跑——整个 ui_server 一 import 就起服务。"""
    m = re.search(r"_ASSET_CACHEABLE = re\.compile\((r\".*?\"), re\.I\)", SRC)
    assert m, "_ASSET_CACHEABLE 不见了"
    return re.compile(eval(m.group(1)), re.I)


class Matches(unittest.TestCase):
    def setUp(self):
        self.rx = _re()

    def test_认图标(self):
        for p in ("/icons/app-terminal.png", "/app-assets/excalidraw/icon.png",
                  "/favicon.ico", "/brand-logo.png"):
            self.assertTrue(self.rx.search(p), p + " 没被认出来")

    def test_认字体(self):
        """@font-face 引的那几份同样打不上指纹。"""
        for p in ("/vendor/fonts/JetBrainsMono-Regular.woff2", "/x.woff", "/y.ttf"):
            self.assertTrue(self.rx.search(p), p + " 没被认出来")

    def test_不认脚本与样式(self):
        """那两类由 index.html 注入 ?v=,走的是长缓存那一支,别抢过来。"""
        for p in ("/app-chat.js", "/style.css", "/index.html"):
            self.assertFalse(self.rx.search(p), p + " 被错认成免指纹资源")

    def test_不认接口(self):
        """API 一律不缓存。这些路径不带扩展名,本来就不该命中。"""
        for p in ("/workbench/domains", "/session/adp_x/message", "/event"):
            self.assertFalse(self.rx.search(p), p + " 被当成静态资源")

    def test_只看结尾(self):
        """/workbench/tasks?ids=a.png 这种查询串里带扩展名的不算——
        判定前已经切掉了 ?，这里再钉一次结尾锚点。"""
        self.assertFalse(self.rx.search("/workbench/tasks"))
        self.assertTrue(self.rx.search("/a/b/c.png"))


class Wiring(unittest.TestCase):
    """接线:这一档要排在 no-store 之前,且不能盖掉带指纹那一支。"""

    def _seg(self):
        m = re.search(r"def end_headers\(self\):.*?\n        # 首次凭", SRC, re.S)
        assert m, "end_headers 不见了"
        return m.group(0)

    def test_用上了那个正则(self):
        self.assertIn("_ASSET_CACHEABLE.search(", self._seg())

    def test_给的是no_cache不是长缓存(self):
        """图标是会换的(改了应用图标要能看到),不能 immutable 一年。"""
        seg = self._seg()
        at = seg.index("_ASSET_CACHEABLE.search(")
        tail = seg[at:at + 700]
        self.assertIn('"no-cache"', tail)
        self.assertNotIn("immutable", tail)

    def test_带指纹那一支仍在最前(self):
        """它比扩展名更强:带 ?v= 的 js/css 该走一年长缓存。"""
        seg = self._seg()
        self.assertLess(seg.index('"v=" in'), seg.index("_ASSET_CACHEABLE.search("))

    def test_排在no_store之前(self):
        seg = self._seg()
        self.assertLess(seg.index("_ASSET_CACHEABLE.search("), seg.index('"no-store, must-revalidate"'))

    def test_接口不受影响(self):
        """整段判定包在 not _is_workbench() and not _is_contract() 里。"""
        seg = self._seg()
        self.assertLess(seg.index("_is_workbench()"), seg.index("_ASSET_CACHEABLE.search("))


if __name__ == "__main__":
    unittest.main()
