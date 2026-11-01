"""中文里的加粗要能渲染出来。

CommonMark 判定开合看两侧字符:闭合的 `**` 前面若是标点,要求后面是空白或标点。
中文写作里「**为什么以前快。**GUI」「**结论:**后面」这类写法极常见,按原规则一律不闭合,
整句连同星号原样显示。app-core.js 在扫描标记那一步补了一层放行,这里对着真渲染器守住它。
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CORE = os.path.join(ROOT, "frontend", "app-core.js")
VENDOR = os.path.join(ROOT, "frontend", "vendor", "markdown-it.min.js")

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const sb = { window: {}, module: { exports: {} } };
sb.self = sb.window; sb.globalThis = sb; vm.createContext(sb);
vm.runInContext(fs.readFileSync(VENDOR, "utf8"), sb);
const md = (sb.window.markdownit || sb.markdownit)({ html: false, linkify: true, breaks: false });
sb.md = md;
const src = fs.readFileSync(CORE, "utf8");
const patch = src.match(/\(function \(m\) \{[\s\S]*?\}\)\(md\);/);
if (!patch) { console.log(JSON.stringify({ error: "补丁不见了" })); process.exit(0); }
vm.runInContext(patch[0], sb);
console.log(JSON.stringify(CASES.map((c) => md.renderInline(c).includes("<strong>"))));
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑渲染器")
class CjkEmphasis(unittest.TestCase):
    def _bold(self, cases):
        js = (HARNESS.replace("VENDOR", json.dumps(VENDOR))
              .replace("CORE", json.dumps(CORE))
              .replace("CASES", json.dumps(cases, ensure_ascii=False)))
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertIsInstance(got, list, out.stdout)
        return got

    def test_全角标点收尾后接拉丁字母(self):
        self.assertEqual(self._bold(["**为什么以前快。**GUI 之前主区是终端",
                                     "**还剩什么。**Timeline 出来即恢复"]), [True, True])

    def test_半角标点收尾后紧跟汉字(self):
        self.assertEqual(self._bold(["**结论:**后面是中文"]), [True])

    def test_全角引号收尾(self):
        self.assertEqual(self._bold(["**「目标已设定」**中文紧跟"]), [True])

    def test_英文与嵌套不受影响(self):
        self.assertEqual(self._bold(["**bold** normal english", "a**b**c",
                                     "**嵌套*强调*的例子**", "**纯汉字加粗**结束"]),
                         [True, True, True, True])

    def test_散星号仍然不算强调(self):
        self.assertEqual(self._bold(["5 * 3 * 2 散星号"]), [False])
