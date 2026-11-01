"""拖侧栏分割线的每帧代价(2026-08-23)。

用户实见拖动发涩。原来 syncGeom 合并到每帧(rAF),那仍是每秒六十次全应用几何同步:
一次 `resize` 事件跑遍所有监听者——原生面板重定位(经 webkit.messageHandlers 过一次
JS↔原生边界)、工作流目录重算位置、每个会话视图对齐输入框——它们各自 getBoundingClientRect
再写样式,一帧里反复触发同步布局。而 browserSyncNative 还被调了两遍:app-browser.js 已经
把它挂在 resize 上,syncGeom 里又显式调一次。

改成两档:CSS 变量每次指针移动都写(纯变量,布局交给合成器,拖起来跟手),几何同步降到
100ms 一次,松手时精确同步一遍。
"""
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
INIT = open(os.path.join(ROOT, "frontend", "app-init.js"), encoding="utf-8").read()
BROWSER = open(os.path.join(ROOT, "frontend", "app-browser.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


class Shape(unittest.TestCase):
    def test_宽度仍然每次移动都写(self):
        """这一档不能降频:降了拖动本身就不跟手。"""
        m = re.search(r"bar\.onpointermove = \(e\) => \{[\s\S]*?\};", INIT)
        self.assertTrue(m)
        self.assertIn("setW(", m.group(0))

    def test_几何同步不再每帧(self):
        self.assertNotIn("syncRaf", INIT)
        self.assertIn("const SYNC_GAP = 100;", INIT)

    def test_松手时精确同步一次(self):
        self.assertIn("syncGeom(true)", INIT)

    def test_不再重复调原生同步(self):
        """app-browser.js 已经把它挂在 resize 上,syncGeom 里再调一次等于做两遍。"""
        self.assertIn('window.addEventListener("resize", () => browserSyncNative())', BROWSER)
        i = INIT.index("const syncNow")
        seg = INIT[i:i + 700]
        self.assertNotIn("browserSyncNative()", seg)

    def test_同步仍然发得出去(self):
        i = INIT.index("const syncNow")
        seg = INIT[i:i + 700]
        self.assertIn('new Event("resize")', seg)

    def test_会话整列始终由同一内容轨即时跟随(self):
        """不应只在拖动期间用覆盖样式临时纠正输入区。"""
        block = re.search(r"\.hs-page \.hs-col \{[\s\S]*?\n\}", CSS)
        self.assertIsNotNone(block)
        self.assertIn("max-width: var(--hs-col)", block.group(0))
        self.assertIn("margin-left: var(--hs-col-ml)", block.group(0))
        self.assertNotIn("body.panel-resizing .hs-page .hs-goal", CSS)

    def test_回到最新按钮也走内容轨(self):
        self.assertIn(".hs-page .hs-down-track { display: flex; justify-content: center; }", CSS)
        self.assertNotIn("body.panel-resizing .hs-page .hs-down", CSS)


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class Throttle(unittest.TestCase):
    """节流本身:拖一秒钟发几次。"""

    def _run(self, moves, span_ms):
        # 要连 syncGeom 一起取到:只匹配到第一个 };  会停在 syncNow 那里
        m = re.search(r"  const SYNC_GAP = 100;[\s\S]*?const syncGeom = [\s\S]*?\n  \};\n", INIT)
        self.assertTrue(m, "syncGeom 那一段取不到")
        js = """
let NOW = 1787500000000;   // 用真实量级的时间戳:从 0 起会让首次判成「刚同步过」而推迟,
                           // 那是替身的假象——真实运行时 syncAt 初值 0 与当下相差极大,首次立刻同步
const Date_ = { now: () => NOW };
globalThis.Date = { now: () => NOW };
let fired = 0;
const timers = [];
globalThis.setTimeout = (fn, ms) => { timers.push({ at: NOW + ms, fn }); return timers.length; };
globalThis.clearTimeout = (id) => { if (id) timers[id - 1] = null; };
globalThis.window = { dispatchEvent: () => { fired++; } };
globalThis.termAppRefit = undefined;
%s
const MOVES = %d, SPAN = %d;
const T0 = NOW;
for (let i = 0; i < MOVES; i++) {
  NOW = T0 + Math.round(i * SPAN / MOVES);
  syncGeom();
  for (let k = 0; k < timers.length; k++) {
    const t = timers[k];
    if (t && t.at <= NOW) { timers[k] = null; t.fn(); }
  }
}
console.log(JSON.stringify(fired));
""" % (m.group(0), moves, span_ms)
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return int(out.stdout.strip())

    def test_一秒内六十次移动只同步十次上下(self):
        """原来是每帧一次,六十次移动就是六十次全应用同步。"""
        n = self._run(60, 1000)
        self.assertLessEqual(n, 12, "同步了 %d 次,节流没生效" % n)
        self.assertGreaterEqual(n, 8, "只同步了 %d 次,跟随太迟钝" % n)

    def test_第一次移动就同步(self):
        """起手那一下要立刻跟上,不能等满 100ms——短促一拖会整段没反应。"""
        self.assertEqual(self._run(1, 0), 1)

    def test_短促一拖也同步(self):
        self.assertGreaterEqual(self._run(5, 30), 1)


if __name__ == "__main__":
    unittest.main()
