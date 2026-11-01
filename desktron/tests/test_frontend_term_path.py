"""会话行的目录标:工作流里每行都标,行高才齐。

落在工作流目录里的会话标工作流名(目录名是内部 id,给人看没意义),引用进来的标其目录名,
聊天组不标(cwd 恒为工作区根,标出来每行一样)。
"""
import json
import os
import re
import shutil
import subprocess
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PANEL = os.path.join(ROOT, "frontend", "app-panel.js")

HARNESS = r"""
const DESKTOP = "_desktop";
const domains = [{ id: "desk-iter", name: "Desk 迭代" }];
function escapeHtml(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
SRC
const out = CASES.map(([s, dom]) => {
  const html = termPathHTML(s, dom);
  const m = html.match(/>([^<]*)<\/span>/);
  return m ? m[1] : "";
});
console.log(JSON.stringify(out));
"""

WS = "/Users/x/workspace"


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class TermPathLabel(unittest.TestCase):
    def _run(self, cases):
        src = re.search(r"function termPathHTML\(.*?\n}\n", _srcmap.panel_src(), re.S).group(0)
        js = (HARNESS.replace("SRC", src)
              .replace("CASES", json.dumps(cases, ensure_ascii=False)))
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_label_per_source(self):
        cases = [
            [{"cwd": WS + "/domains/desk-iter"}, "desk-iter"],       # 工作流本地:工作流名
            [{"cwd": "/Users/x/work/desktron"}, "desk-iter"],     # 引用进来的:目录名
            [{"cwd": WS}, "_desktop"],                               # 聊天组:不标
            [{"cwd": ""}, "desk-iter"],                              # 拿不到 cwd:不标
        ]
        self.assertEqual(self._run(cases), ["Desk 迭代", "desktron", "", ""])

    def test_unknown_workflow_falls_back_to_its_id(self):
        # domains 还没加载完时不能空着一行,退回目录名(= 工作流 id)
        self.assertEqual(self._run([[{"cwd": WS + "/domains/wf-x"}, "wf-x"]]), ["wf-x"])


if __name__ == "__main__":
    unittest.main()
