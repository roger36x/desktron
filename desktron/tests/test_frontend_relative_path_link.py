"""会话内的相对文件链接必须以该会话的工作目录为基准。"""
import os
import re
import json
import shutil
import subprocess
import unittest

import _srcmap


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CORE = open(os.path.join(ROOT, "frontend", "app-core.js"), encoding="utf-8").read()
CHAT = _srcmap.chat_src()
REFS = open(os.path.join(ROOT, "frontend", "app-chat-refs.js"), encoding="utf-8").read()


@unittest.skipUnless(shutil.which("node"), "需要 Node 执行生产链接处理函数")
class RelativeFileLinkBehavior(unittest.TestCase):
    def run_js(self, assertions):
        router = CORE.split("// 全局链接路由", 1)[1]
        router = router[router.index('document.addEventListener'):].split('}, true);', 1)[0] + '}, true);'
        script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const opened = [], pages = [], external = [];
let route;
const ctx = {
  document: {addEventListener: (type, fn) => {route = fn;}},
  openPage: href => pages.push(href), openExternal: href => external.push(href),
};
vm.createContext(ctx);
vm.runInContext(REFS, ctx);
vm.runInContext(ROUTER, ctx);
ctx.hsOpenFile = (path, line, source) => opened.push({path, line, source});
function click(href) {
  const a = {getAttribute: () => href, textContent: 'link'};
  const ev = {target: {closest: () => a}, prevented: false, stopped: false,
    preventDefault() {this.prevented = true;}, stopPropagation() {this.stopped = true;}};
  route(ev);
  return {a, ev};
}
function code(text, inPre = false) {
  const c = {textContent: text, closest: () => inPre ? {} : null,
    classList: {add() {}}};
  ctx.hsCodePaths({querySelectorAll: () => [c]});
  if (c.onclick) c.onclick();
  return c;
}
'''.replace('vm.runInContext(REFS, ctx)', 'vm.runInContext(' + json.dumps(REFS) + ', ctx)')
        script = script.replace('vm.runInContext(ROUTER, ctx)', 'vm.runInContext(' + json.dumps(router) + ', ctx)')
        result = subprocess.run(["node"], input=script + assertions, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_inline_paths_with_line_numbers_keep_source(self):
        self.run_js(r'''
for (const [text, path, line] of [
  ['docs/a.md:12', 'docs/a.md', '12'], ['README.md:3', 'README.md', '3'],
  ['./src/a.ts:7', './src/a.ts', '7'], ['../a.py:9', '../a.py', '9'],
  ['docs/a.md', 'docs/a.md', ''],
]) {
  const c = code(text);
  assert.deepEqual(opened.pop(), {path, line, source: c});
}
for (const text of ['https://example.com:12', 'javascript:12', 'docs/a.md:no', 'a + b.md:12']) {
  assert.equal(code(text).onclick, undefined, text);
}
assert.equal(code('docs/a.md:12', true).onclick, undefined);
assert.equal(opened.length, 0);
''')

    def test_markdown_line_routes_and_protocols(self):
        self.run_js(r'''
for (const [href, path, line] of [
  ['README.md:12', 'README.md', '12'], ['app.ts:7', 'app.ts', '7'],
  ['docs/a.md:12', 'docs/a.md', '12'], ['./a.md#L5', 'a.md', '5'],
  ['docs/a%20b.md#L8', 'docs/a b.md', '8'],
]) {
  const {a, ev} = click(href);
  assert.deepEqual(opened.pop(), {path, line, source: a});
  assert.equal(ev.prevented, true); assert.equal(ev.stopped, true);
}
for (const href of ['javascript:void(0)', 'vscode://file/x', 'custom.app:12', 'README.md:invalid']) {
  const {ev} = click(href);
  assert.equal(ev.prevented, true); assert.equal(ev.stopped, false);
}
assert.equal(click('#section').ev.prevented, false);
click('https://example.com/a.md:12'); click('mailto:user@example.com'); click('tel:123');
assert.deepEqual(pages, ['https://example.com/a.md:12']);
assert.deepEqual(external, ['mailto:user@example.com', 'tel:123']);
assert.equal(opened.length, 0);
''')


class RelativeFileLinkContext(unittest.TestCase):
    def test_markdown_link_passes_its_originating_anchor(self):
        self.assertIn("hsOpenFileHref(href, a)", CORE)
        self.assertIn("function hsOpenFileHref(href, source)", CHAT)
        self.assertIn("hsOpenFile(path, line, source)", CHAT)

    def test_bare_and_code_paths_keep_the_same_context(self):
        self.assertIn('hsOpenFile(m[1], m[2] || m[3] || "", c)', CHAT)
        self.assertIn("hsOpenFile(path, line, a)", CHAT)

    def test_context_root_comes_from_the_owning_session(self):
        fn = re.search(r"function hsFileContextRoot\(source\) \{[\s\S]+?\n\}", CHAT)
        self.assertTrue(fn, "缺少会话文件根解析")
        body = fn.group(0)
        self.assertIn('source.closest(".hs-page")', body)
        self.assertIn("x.el === page", body)
        self.assertIn("x.resume_id, x.session_id", body)
        self.assertIn('return "abs:" + cwd', body)
        self.assertIn("view.draftDomain", body)
        self.assertIn("fileCwd: (buildOpts && buildOpts.cwd)", CHAT)

    def test_cwd_is_tried_before_registered_repositories(self):
        fn = re.search(r"async function hsResolveFile\(path, source\) \{[\s\S]+?\n\}", CHAT)
        self.assertTrue(fn, "缺少文件解析函数")
        body = fn.group(0)
        self.assertLess(body.index("hsFileContextRoot(source)"), body.index('api("/workbench/domains")'))
        self.assertIn("rootKey = contextRoot", body)
        self.assertIn("_hsRootHit.set(rootKey, c)", body)

    def test_encoded_local_path_is_decoded_once(self):
        self.assertIn("path = decodeURI(path)", CHAT)


if __name__ == "__main__":
    unittest.main()
