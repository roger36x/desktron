"""切引擎之后,上一个引擎的会话页不该被恢复出来(2026-08-25 用户实见)。

切引擎走的是整页重载(app-events.js 的引擎菜单里 location.reload()),而 cd_pages 里的
会话页此前不记引擎——重载后每一页都照开,data.active 还会把上一个引擎的那段顶到前台:
人切到 pi,屏幕上却是 claude 那段「desktron refactor」。

盯三件事:只开当前引擎的、别家的原样留在记录里(切回去还在)、被筛掉的那页不顶到前台。
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PAGES_JS = os.path.join(ROOT, "frontend", "app-pages.js")

HARNESS = r"""
let STORE = null;
globalThis.localStorage = { getItem: () => STORE, setItem: (k, v) => { STORE = v; } };
let pages = [], pActive = null, overlayOpen = false, _restoringPages = false;
let curEngine = "ENGINE";
let shepherdMode = false;
const opened = [];
const _hsViews = new Map();
function openHtmlSession(id, title, show) { opened.push(id); pages.push({ url: "chat:" + id, title }); }
function showOverlay(p) { globalThis.__shown = p.url; }
function browserHandlesUrl() { return false; }
function restoreTermPages() {}
function restoreFilesPages() {}
SRC
STORE = JSON.stringify(DATA);
restorePages();
console.log(JSON.stringify({
  opened,
  shown: globalThis.__shown || null,
  kept: JSON.parse(STORE).list.map((d) => d.id),
}));
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class RestoreIsEngineScoped(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PAGES_JS, encoding="utf-8") as f:
            src = f.read()
        parts = [re.search(r"let _pagesOtherEngines = \[\];.*", src).group(0)]
        for name in ("persistPages", "restorePages"):
            # 形参写成 \(\) 会把「这个函数不许收参数」也一并钉死:2026-08-25 persistPages
            # 加了个可选的 keep(恢复收尾那次写回不覆盖 open/active),这一组就整个报错在
            # setUpClass 上,而它本来跟形参无关。放宽到任意形参,断言仍落在函数体上。
            m = re.search(r"\nfunction %s\([^)]*\)[\s\S]*?\n}\n" % name, src)
            assert m, "%s 不见了" % name
            parts.append(m.group(0))
        cls.src = "\n".join(parts)

    def _run(self, data, engine="pi"):
        js = (HARNESS.replace("SRC", self.src)
              .replace("ENGINE", engine)
              .replace("DATA", json.dumps(data, ensure_ascii=False)))
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    DATA = {
        "list": [
            {"kind": "chat", "id": "adp_claude1", "title": "desktron refactor", "engine": "claude"},
            {"kind": "chat", "id": "adp_pi1", "title": "Greeting request", "engine": "pi"},
            {"kind": "chat", "id": "adp_legacy", "title": "老记录没有 engine 字段"},
        ],
        "open": True, "active": "chat:adp_claude1",
    }

    def test_只开当前引擎的会话页(self):
        got = self._run(self.DATA)
        self.assertIn("adp_pi1", got["opened"])
        self.assertNotIn("adp_claude1", got["opened"], "上一个引擎的会话页又被恢复出来了")

    def test_没记引擎的老条目照常恢复(self):
        """写漏 engine 不该导致丢页:恢复那侧对没有 engine 的条目一律放行。"""
        self.assertIn("adp_legacy", self._run(self.DATA)["opened"])

    def test_别家的原样留在记录里(self):
        """筛掉不等于删掉——切回那个引擎时这一页还要在。"""
        self.assertIn("adp_claude1", self._run(self.DATA)["kept"])

    def test_被筛掉的那页不顶到前台(self):
        self.assertIsNone(self._run(self.DATA)["shown"])

    def test_切回去就恢复(self):
        got = self._run(self.DATA, engine="claude")
        self.assertIn("adp_claude1", got["opened"])
        self.assertNotIn("adp_oc1", got["opened"])
        self.assertEqual(got["shown"], "chat:adp_claude1")


if __name__ == "__main__":
    unittest.main()
