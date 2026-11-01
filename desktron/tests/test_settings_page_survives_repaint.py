"""设置目录住在独立浮层，与主区及工作流侧栏互不覆盖；正文失败也不连坐目录。"""
import os
import re
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RAIL = _srcmap.rail_src()
SETTINGS = _srcmap.settings_src()
SETTINGS_PY = open(os.path.join(ROOT, "desktron", "lib", "settings.py"), encoding="utf-8").read()


def _fn(src, name):
    """取一个顶层 function 的源码(到下一个顶层 } 为止)。"""
    m = re.search(r"\nfunction %s\([^)]*\) \{[\s\S]+?\n\}" % re.escape(name), src)
    assert m, "找不到 %s" % name
    return m.group(0)


class 设置目录与工作流侧栏隔离(unittest.TestCase):
    def test_工作流重绘不再承担设置模式分支(self):
        body = _fn(RAIL, "renderSessionPanel")
        self.assertNotIn('sideMode === "settings"', body)
        self.assertIn('const el = $("#panel-list")', body)

    def test_设置目录使用独立容器(self):
        body = _fn(SETTINGS, "_renderSettingsNav")
        self.assertIn('const el = $("#sm-nav")', body)
        self.assertNotIn("#panel-list", body)

    def test_进入设置不改动工作流侧栏和主区(self):
        body = _fn(SETTINGS, "openSettings")
        self.assertIn('const modal = $("#settings-modal")', body)
        self.assertIn("modal.hidden = false", body)
        self.assertNotIn("_sideEnterTreeMode()", body)
        self.assertNotIn("_sideEnterSettingsMode()", body)
        self.assertNotIn("showView", body)


class 设置目录不被正文连坐(unittest.TestCase):
    def test_renderSettings失败时目录照样渲染(self):
        body = _fn(SETTINGS, "openSettings")
        m = re.search(r"renderSettings\(\)\.then\(([^,)]+),", body)
        self.assertTrue(m, "renderSettings().then 必须带第二个参数(失败分支)")
        self.assertIn(m.group(1).strip(), body[:m.start()], "成功与失败要落到同一个目录渲染函数")

    def test_失败分支给出可见提示且不吞异常(self):
        body = _fn(SETTINGS, "openSettings")
        tail = body[body.index("renderSettings()"):]
        self.assertIn("sm-loading", tail, "失败时主区要留一行提示,不能空着")
        self.assertIn("console.error", tail, "异常要留现场")


class 引擎探测的超时口径(unittest.TestCase):
    def test_不用with包线程池(self):
        """with 退出时 shutdown(wait=True),会一直等到最慢的探测跑完,期限等于白设。"""
        body = re.search(r"def _settings_get\(\):[\s\S]+?\n    return out", SETTINGS_PY).group(0)
        self.assertNotIn("with ThreadPoolExecutor", body)
        self.assertIn("shutdown(wait=False)", body)

    def test_超时是整趟的而不是每家的(self):
        """逐个 result(timeout=12) 时四家串起来最坏 48 秒,前端 15 秒就超时了。"""
        body = re.search(r"def _settings_get\(\):[\s\S]+?\n    return out", SETTINGS_PY).group(0)
        self.assertIn("deadline", body)
        self.assertRegex(body, r"result\(timeout=[^)]*deadline[^)]*\)")

    def test_探测失败的段报unknown而不是空(self):
        """空段会被 provision/setup.py 读成"未登录"并配一颗去登录的钮,比不报更坏。"""
        body = re.search(r"def _settings_get\(\):[\s\S]+?\n    return out", SETTINGS_PY).group(0)
        self.assertIn('{"unknown": True}', body)
        self.assertNotIn("out[k] = {}", body)


if __name__ == "__main__":
    unittest.main()
