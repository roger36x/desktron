"""代码变更卡的行号应在展开时立即可用，不再等待文件回读。"""
import re
import subprocess
import unittest

import _srcmap


CHAT = _srcmap.chat_src()


def fn(name):
    match = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), CHAT, re.S)
    assert match, name
    return match.group(0)


class DiffLineNumbers(unittest.TestCase):
    def run_node(self, body):
        script = "const assert = require('assert');\n" + fn("hsDiffNumber") + "\n" + body
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pi单段编辑使用结果元数据同步编号(self):
        self.run_node(r'''
let apiCalls = 0;
async function api() { apiCalls++; throw new Error("不应读取文件"); }
async function hsResolveFile() { throw new Error("不应解析路径"); }
function row(classes, skipped) {
  const slot = {textContent: ""};
  return {
    classList: {contains: (name) => classes.includes(name)},
    dataset: skipped ? {skipped: String(skipped)} : {},
    querySelector: (sel) => sel === ".hs-dn" ? slot : null,
    slot,
  };
}
const rows = [row(["ctx"]), row(["del"]), row(["add"]), row(["gap"], 5), row(["ctx"])];
const diff = {dataset: {}, querySelectorAll: () => rows};
const part = {
  tool: "edit",
  state: {line_start: 40, input: {path: "/tmp/a.py", edits: [{oldText: "a", newText: "b"}]}},
  el: {querySelector: () => diff},
};
(async () => {
  await hsDiffNumber(part);
  assert.deepEqual(rows.map((r) => r.slot.textContent), [39, 40, 40, "", 46]);
  assert.equal(apiCalls, 0);
})().catch((error) => { console.error(error); process.exit(1); });
''')

    def test_多段编辑不误用单一起始行(self):
        segment = fn("hsDiffNumber")
        self.assertIn("edits.length <= 1", segment)
        self.assertIn("Number(state.line_start)", segment)
        self.assertIn("n += Number(r.dataset.skipped) || 0", segment)

    def test_折叠行记录跳过数量(self):
        segment = fn("hsDiffTrim")
        self.assertIn('skipped: 1', segment)
        self.assertIn('out[out.length - 1].skipped++', segment)
        detail = fn("hsDiffDetail")
        self.assertIn('data-skipped="${r.skipped || 0}"', detail)


if __name__ == "__main__":
    unittest.main()
