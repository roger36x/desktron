"""右缘系统车道的口径卡点(静态半)。

窗口右 0~15px 归常驻系统件:滚动条滑块 2~10px、程序坞感应线 12~15px,两者都
position:fixed 画在内容之上。页面内容铺进这条带就会被压穿(2026-07-28 Timeline
选中态实底块被感应线压穿即此)。

本测试只管三件事:车道宽度单一口径、感应线确实落在车道内、注册在案的贴右缘容器
用的是这个口径而不是字面量。**它认不出新页面的新写法**——覆盖检测归
scripts/check_edge_rail.py(headless Chrome 实测),两道卡点读同一个 --edge-rail。
当前没有登记中的贴右缘内容容器；新页面需补真实覆盖检查。
"""
import os
import re
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STYLE = os.path.join(ROOT, "frontend", "style.css")

# 贴窗口右缘、右侧内缩必须走 --edge-rail 的内容容器(登记制,见模块 docstring)
FLUSH_RIGHT_SELECTORS = ()   # Timeline 已下线；新增贴右缘页面时重新登记。


class FrontendEdgeRail(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(STYLE, encoding="utf-8") as f:
            cls.css = f.read()

    def _rail_px(self):
        m = re.search(r"--edge-rail:\s*(\d+)px", self.css)
        self.assertIsNotNone(m, "style.css 里没有 --edge-rail:车道宽度必须单一口径")
        return int(m.group(1))

    def test_rail_token_is_single_source(self):
        self.assertGreaterEqual(self._rail_px(), 16, "车道至少 16px 才盖得住感应线右缘")

    def test_sensor_line_sits_inside_the_rail(self):
        # 线的右缘 = #dock-sensor 的 right + ::after 的 right + width,必须 <= 车道宽
        host = re.search(r"#dock-sensor\s*\{([^}]*)\}", self.css).group(1)
        line = re.search(r"#dock-sensor::after\s*\{([^}]*)\}", self.css, re.S).group(1)
        host_right = int(re.search(r"right:\s*(\d+)px", host).group(1))
        line_right = int(re.search(r"right:\s*(\d+)px", line).group(1))
        line_width = int(re.search(r"width:\s*(\d+)px", line).group(1))
        self.assertLessEqual(
            host_right + line_right + line_width, self._rail_px(),
            "程序坞感应线伸出了 --edge-rail:要么收窄线,要么加宽车道并同步所有贴右缘容器",
        )



if __name__ == "__main__":
    unittest.main()
