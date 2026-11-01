"""原生网页与 DOM 浮层的层级契约。

外链正文在 Swift 壳里是一张独立 WKWebView,画在主 WebView 之上,DOM 的 z-index 盖不住它。
谁在前面只能由前端裁,判据原先是两个类名的白名单——后加的浮层没人记得回去登记,于是两头
都出过事:浮层被网页盖住,以及引导页里点开一个链接、网页被压着不显示(2026-09-08 用户实见)。
现在的判据是几何:面板中心那一点归谁。枚举的是宿主(浏览器视口、应用页 iframe),不是浮层。
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FRONT = os.path.join(ROOT, "frontend")
PAGES = open(os.path.join(FRONT, "app-pages.js"), encoding="utf-8").read()
BRIDGE = PAGES[PAGES.index("const NATIVE_BROWSER ="):PAGES.index("// 通顶壳拖窗:")]

HARNESS = r'''
const assert = require('assert');
const sent = [], classes = new Set();
let changed = null, leftSettings = 0, hit = null;
const mk = (id) => {
  const el = { id };
  el.contains = (x) => x === el;
  el.closest = (sel) => (sel === '#' + id ? el : null);
  return el;
};
const host = mk('br-view'), settings = mk('settings-modal'), perm = mk('perm-modal');
const overlayBody = mk('overlay-body');            // 宿主的祖先:视口不接受命中时落到它
overlayBody.contains = (x) => x === overlayBody || x === host;
const body = { classList: { contains: (n) => classes.has(n) }, id: 'body' };
body.contains = () => false;
const document = {
  body,
  querySelector: (sel) => (sel === '#settings-modal' ? settings : null),
  elementFromPoint: () => hit,
};
const window = { webkit: { messageHandlers: { browser: { postMessage: (m) => sent.push(m) } } },
                 innerWidth: 1440, innerHeight: 900 };
class MutationObserver { constructor(fn) { changed = fn; } observe() {} }
const pages = [];
const nativeEmbeds = new Map();
function browserActiveTab() { return { id: 'browser:1' }; }
function browserViewEl() { return host; }
function _leaveSettings() { leftSettings += 1; classes.delete('settings-dialog-open'); hit = host; }
BRIDGE
const RECT = { x: 200, y: 100, w: 900, h: 600 };
const openTab = () => nb({ action: 'open', id: 'browser:1', url: 'https://example.test', rect: RECT });
const domMoved = () => changed([{ target: mk('x'), attributeName: 'hidden' }]);
const settle = () => new Promise((r) => setTimeout(r, 200));
BODY
'''


def _run(body):
    js = HARNESS.replace("BRIDGE", BRIDGE).replace("BODY", body)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout


@unittest.skipUnless(shutil.which("node"), "需要 node 跑原生桥协调")
class Geometry(unittest.TestCase):
    def test_a_layer_over_the_page_hides_it_and_leaving_restores_it(self):
        # 权限询问这类浮层不在任何白名单里,过去就这么盖在网页底下,人看不见也答不了
        _run(r'''(async () => {
  hit = host; openTab();
  assert.equal(sent.at(-1).action, 'open');
  hit = perm; domMoved(); await settle();
  assert.equal(sent.at(-1).action, 'hide');
  hit = host; domMoved(); await settle();
  assert.deepEqual(sent.at(-1), { action: 'open', id: 'browser:1', url: 'https://example.test', rect: RECT });
})();''')

    def test_the_hosts_own_ancestors_do_not_count_as_a_veil(self):
        # 视口若不接受命中,elementFromPoint 落到它的祖先。那不是遮挡,不能据此把网页藏了
        _run(r'''(async () => {
  hit = host; openTab();
  const n = sent.length;
  hit = overlayBody; domMoved(); await settle();
  assert.equal(sent.length, n);
})();''')

    def test_the_page_is_not_hidden_when_nothing_is_in_front(self):
        _run(r'''(async () => {
  hit = host; openTab();
  const n = sent.length;
  domMoved(); await settle();
  assert.equal(sent.length, n);
})();''')


@unittest.skipUnless(shutil.which("node"), "需要 node 跑原生桥协调")
class OpeningFromAModal(unittest.TestCase):
    def test_a_link_clicked_inside_the_setup_dialog_shows_the_page(self):
        # 引导页里点链接:过去网页被压着不显示,看上去像点了没反应
        _run(r'''(async () => {
  classes.add('settings-dialog-open'); hit = settings;
  openTab();
  assert.equal(leftSettings, 1);
  assert.equal(sent.at(-1).action, 'open');
})();''')

    def test_a_blocking_dialog_still_wins_over_a_newly_opened_page(self):
        _run(r'''(async () => {
  hit = perm;
  openTab();
  assert.equal(sent.at(-1).action, 'hide');
  hit = host; domMoved(); await settle();
  assert.equal(sent.at(-1).action, 'open');
})();''')


class Contract(unittest.TestCase):
    def test_the_criterion_is_geometry_not_a_list_of_overlay_class_names(self):
        self.assertIn("document.elementFromPoint", BRIDGE)
        self.assertIn("function nativeVeilEl", BRIDGE)
        # 白名单只剩两条快路(其余出现在注释里说来由);新加的浮层不必来这里登记
        fast = re.search(r"const NATIVE_VEIL_CLASS = \{.*?\};", BRIDGE, re.S).group(0)
        self.assertEqual(len(re.findall(r"-dialog-open", fast)), 2)

    def test_only_the_two_dismissible_dialogs_step_aside(self):
        block = re.search(r"const NATIVE_VEIL_DISMISS = \[.*?\];", BRIDGE, re.S).group(0)
        self.assertIn("_leaveSettings", block)
        self.assertIn("closeResourcePage", block)
        self.assertNotIn("perm", block)

    def test_the_watcher_only_runs_while_a_page_is_in_front(self):
        poke = re.search(r"function _nativeVeilPoke\(\).*?\n}", BRIDGE, re.S).group(0)
        self.assertIn("!_nativeForeground", poke)
        self.assertIn("_nativeVeilTimer", poke)


if __name__ == "__main__":
    unittest.main()
