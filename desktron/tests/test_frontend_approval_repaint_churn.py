"""审批账本五秒一轮同步时,过程审批卡内容没变就不再动 DOM(2026-09-07)。

现场取证(docs/chat-viewport.md「取证」一节):桌面端会话正文在贴底态下每 5 秒上跳一段、
再被贴底逻辑拉回;上跳与审批轮询同相位,上跳前一批 DOM 变动正是 _apprPaint 每轮
换图标节点、换文本节点、重设 hidden 这几处。手机端没有原生审批桥,轮询第一步就退出,所以不跳。

这里守的是:同一状态重复画,DOM 一次都不碰;状态变了照常画。
"""
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
JS = open(os.path.join(ROOT, "frontend", "app-approval.js"), encoding="utf-8").read()


def fn(name):
    i = JS.index("function " + name + "(")
    return JS[i:JS.index("\n}\n", i) + 3]


HARNESS = r"""
const HS_ICONS = { lock: "<svg>lock</svg>", unlock: "<svg>unlock</svg>" };
const _apprReplying = new Set();
function _apprTitle(op) { return "title " + op.id; }
function _apprInputText(op) { return "input " + op.id; }
function _apprStateText(op) { return op.approval_state.startsWith("approved:") ? "已允许" : "已结束"; }
let writes = 0;
function node(cls) {
  return { cls, _html: "", _text: "", hidden: false, attrs: {},
    set innerHTML(v) { writes++; this._html = v; }, get innerHTML() { return this._html; },
    set textContent(v) { writes++; this._text = v; }, get textContent() { return this._text; },
    setAttribute(k, v) { writes++; this.attrs[k] = v; } };
}
function card() {
  const parts = { ".hs-ico": node("ico"), ".hs-t1": node("t1"), ".hs-t2": node("t2") };
  const btn = node("btn");
  Object.defineProperty(btn, "disabled", { set(v) { writes++; this._d = v; }, get() { return this._d; } });
  const acts = node("acts");
  Object.defineProperty(acts, "hidden", { set(v) { writes++; this._h = v; }, get() { return this._h; } });
  acts.querySelectorAll = () => [btn];
  parts[".hs-acts"] = acts;
  const classes = new Set();
  return { classList: { toggle(c, on) { writes++; on ? classes.add(c) : classes.delete(c); },
                        contains: (c) => classes.has(c) },
           querySelector: (sel) => parts[sel], parts };
}
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class 同状态不重绘(unittest.TestCase):
    def run_js(self, body):
        code = fn("_apprPending") + fn("_apprPaint") + HARNESS + body
        r = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_已结束的卡第二轮起一次写入都没有(self):
        out = self.run_js("""
const op = { id: "op1", approval_state: "approved:user", status: "done" };
const el = card();
_apprPaint(el, op); const first = writes;
_apprPaint(el, op); _apprPaint(el, op);
console.log(first > 0 && writes === first ? "ok" : "churn " + first + " -> " + writes);
""")
        self.assertEqual(out, "ok")

    def test_状态变化仍然重画(self):
        out = self.run_js("""
const el = card();
_apprPaint(el, { id: "op1", approval_state: "pending", status: "waiting_user" });
const hiddenWhilePending = el.parts[".hs-acts"].hidden;
const before = writes;
_apprPaint(el, { id: "op1", approval_state: "approved:user", status: "done" });
console.log(JSON.stringify({ hiddenWhilePending, repainted: writes > before,
  hiddenAfter: el.parts[".hs-acts"].hidden, icon: el.parts[".hs-ico"].innerHTML,
  resolved: el.classList.contains("resolved") }));
""")
        import json
        got = json.loads(out)
        self.assertFalse(got["hiddenWhilePending"])
        self.assertTrue(got["repainted"])
        self.assertTrue(got["hiddenAfter"])
        self.assertIn("unlock", got["icon"])
        self.assertTrue(got["resolved"])

    def test_通知卡换到另一条审批时照常重画(self):
        """右上通知复用同一个节点轮流显示不同待办,签名必须含 id。"""
        out = self.run_js("""
const el = card();
_apprPaint(el, { id: "a", approval_state: "pending", status: "waiting_user" });
const before = writes;
_apprPaint(el, { id: "b", approval_state: "pending", status: "waiting_user" });
console.log(writes > before && el.parts[".hs-t1"].textContent === "title b" ? "ok" : "stale");
""")
        self.assertEqual(out, "ok")

    def test_源码里守卫在任何写入之前(self):
        body = fn("_apprPaint")
        guard = body.index("el._apprSig === sig")
        for write in ("classList.toggle", "innerHTML", "textContent", ".hidden"):
            self.assertGreater(body.index(write), guard, write + " 在守卫之前就写了 DOM")
        self.assertNotIn("_apprPaint(", re.sub(r"function _apprPaint\(", "", body))


if __name__ == "__main__":
    unittest.main()
