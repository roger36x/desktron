"""十四天完成情况:一天一根堆叠柱,各工作流按当日占比切段。

折线试过一版又换回来(2026-08-16):十四天里多数是零,折线把零值连成贴地的长横线,读起来像
「一直在做但产出为零」,而事实是那些天没有闭环。柱子只在有闭环的那天出现,零值就是没有柱。

柱子放细:外层 svg 是 preserveAspectRatio="none" 横向拉伸的,视口宽度大约是 viewBox 的两倍,
所以 8 个单位落到屏幕上是十几像素。原来 34 个单位铺满整格,一眼看去是一片色块而不是一根柱。
"""
import json
import os
import re
import shutil
import subprocess
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PANEL = _srcmap.panel_src()


def _slice(name):
    m = re.search(r"\nfunction %s\(.*?\n}\n" % re.escape(name), PANEL, re.S)
    assert m, name
    return m.group(0)


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class TrendBars(unittest.TestCase):
    def _svg(self, trends):
        rows = [{"d": {"name": "wf%d" % i}, "data": {"stats": {"trend14": t}}}
                for i, t in enumerate(trends)]
        js = ('const _TP_COLORS = ["#a", "#b", "#c"];\n'
              'const escapeHtml = (s) => String(s);\n'
              + _slice("_tpTrendStack")
              + "\nconsole.log(_tpTrendStack(%s));" % json.dumps(rows, ensure_ascii=False))
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def _rects(self, svg):
        return [dict(re.findall(r'(\w+)="([^"]+)"', m)) for m in re.findall(r"<rect[^>]+>", svg)]

    def test_是柱不是线(self):
        svg = self._svg([[0, 3] + [0] * 12])
        self.assertIn("<rect", svg)
        self.assertNotIn("<polyline", svg)

    def test_柱子细(self):
        # 视口宽约是 viewBox 的两倍,8 个单位落屏十几像素;超过 12 就又成色块了
        r = self._rects(self._svg([[1] + [0] * 13]))[0]
        self.assertLessEqual(float(r["width"]), 12)

    def test_零值那天不画柱(self):
        svg = self._svg([[0, 3] + [0] * 12])
        self.assertEqual(len(self._rects(svg)), 1)

    def test_一件都没有时整张图为空(self):
        self.assertEqual(self._svg([[0] * 14]), "")

    def test_同一天多个工作流叠成一根(self):
        rects = self._rects(self._svg([[2] + [0] * 13, [1] + [0] * 13]))
        self.assertEqual(len(rects), 2)
        self.assertEqual(rects[0]["x"], rects[1]["x"])          # 同一根柱,x 相同
        self.assertGreater(float(rects[0]["height"]), float(rects[1]["height"]))   # 占比切段

    def test_柱子落在每格中点(self):
        # 14 格、宽 700 → 每格 50;柱宽 8 时左缘应在 25 - 4 = 21
        r = self._rects(self._svg([[1] + [0] * 13]))[0]
        self.assertAlmostEqual(float(r["x"]) + float(r["width"]) / 2, 25.0, places=1)

    def test_每段带名字与数值(self):
        svg = self._svg([[0, 3] + [0] * 12])
        self.assertIn("<title>wf0:3</title>", svg)


if __name__ == "__main__":
    unittest.main()
