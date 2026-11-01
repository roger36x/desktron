"""工作台状态局部更新不能覆盖其他设备刚写入的排序。"""
import os
import json
import re
import subprocess
import sys
import tempfile
import unittest
import _srcmap

sys.path.insert(0, _srcmap.LIB)
if "deskcfg" not in sys.modules:
    sys.argv = ["test", _srcmap.FRONTEND, "0", "0", _srcmap.ROOT]
import workbench

RAIL = _srcmap.frontend_src("app-rail.js")
SAVE_STATE = re.search(r"function saveState\([\s\S]+?\n}\n", RAIL).group(0)


class WorkbenchStatePatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = workbench.WB_STATE
        workbench.WB_STATE = os.path.join(self.tmp.name, "state.json")

    def tearDown(self):
        workbench.WB_STATE = self.old_path
        self.tmp.cleanup()

    def test_会话排序按分组合并(self):
        workbench._wb_state_write({
            "sessionOrder": {"wf-a": ["a", "b"]},
            "taskRuns": {"run-a": {"at": 1}},
        })
        workbench._wb_state_patch({"sessionOrder": {"_desktop": ["c", "d"]}})
        self.assertEqual(workbench._wb_state_read(), {
            "sessionOrder": {"wf-a": ["a", "b"], "_desktop": ["c", "d"]},
            "taskRuns": {"run-a": {"at": 1}},
        })

    def test_局部字段更新保留其余状态(self):
        workbench._wb_state_write({"sessionOrder": {"wf-a": ["a"]}, "engine": "claude"})
        workbench._wb_state_patch({"engine": "codex"})
        self.assertEqual(workbench._wb_state_read()["sessionOrder"], {"wf-a": ["a"]})
        self.assertEqual(workbench._wb_state_read()["engine"], "codex")

    def test_自动钉住不覆盖另一端的手工次序(self):
        workbench._wb_state_write({"sessionOrder": {"wf-a": ["b", "a"]}})
        workbench._wb_state_patch({"sessionOrderPins": {"wf-a": ["c", "a", "b"]}})
        self.assertEqual(workbench._wb_state_read()["sessionOrder"]["wf-a"], ["c", "b", "a"])
        self.assertNotIn("sessionOrderPins", workbench._wb_state_read())


class FrontendStateSaveCompatibility(unittest.TestCase):
    def _calls(self, patch_endpoint_ok):
        reject_patch = "false" if patch_endpoint_ok else "true"
        js = f"""
const calls = [];
const wbState = {{ engine: "codex", sideProjOrder: ["a", "b"], sessionOrderPins: {{stale: []}} }};
function api(path, opts) {{
  calls.push([path, opts.method, JSON.parse(opts.body)]);
  if (path.endsWith("/patch") && {reject_patch}) return Promise.reject(new Error("404"));
  return Promise.resolve({{ok: true}});
}}
{SAVE_STATE}
(async () => {{ await saveState({{sideProjOrder: ["b", "a"]}}); console.log(JSON.stringify(calls)); }})();
"""
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=10)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_旧服务拒绝局部端点后回退完整保存(self):
        calls = self._calls(False)
        self.assertEqual([(x[0], x[1]) for x in calls], [
            ("/workbench/state/patch", "POST"), ("/workbench/state", "PUT")])
        self.assertEqual(calls[1][2]["sideProjOrder"], ["a", "b"])
        self.assertNotIn("sessionOrderPins", calls[1][2])

    def test_新服务接受局部更新时不再完整覆盖(self):
        calls = self._calls(True)
        self.assertEqual([(x[0], x[1]) for x in calls], [("/workbench/state/patch", "POST")])


if __name__ == "__main__":
    unittest.main()
