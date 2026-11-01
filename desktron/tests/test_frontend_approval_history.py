"""Replay receipts read-only; state recovery reuses one actionable card."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / 'frontend/app-chat-overlays.js').read_text(encoding='utf-8')
CORE = (ROOT / 'frontend/app-chat-core.js').read_text(encoding='utf-8')
SHAPES = (ROOT / 'frontend/app-chat-reconcile.js').read_text(encoding='utf-8')


def fn(name, source=SOURCE):
    start = source.index('function ' + name + '(')
    return source[start:source.index('\n}', start) + 3]


@unittest.skipUnless(shutil.which('node'), 'requires node')
class ApprovalReplay(unittest.TestCase):
    def test_timeout_receipt_uses_expiry_reason_in_live_event_handler(self):
        self.assertIn('timeout: "审批已过期，操作未执行"', CORE)

    def test_refresh_reuses_card_and_never_reactivates_resolved_receipt(self):
        script = r'''
const assert = require('node:assert/strict');
class Node {
  constructor() { this.children = []; this.fields = {}; }
  set innerHTML(value) {
    this.html = value; this.fields = {};
    for (const cls of ['hs-ico','hs-t1','hs-t2','hs-t3','hs-acts','hs-lnk','hs-pill']) {
      if (value.includes(cls)) this.fields['.'+cls] = new Node();
    }
    if (this.fields['.hs-acts']) this.fields['.hs-acts'].querySelectorAll = () => [this.fields['.hs-pill']];
  }
  get innerHTML() { return this.html || ''; }
  querySelector(s) { return this.fields[s] || null; }
  insertBefore(el) { el.remove(); this.children.push(el); el.parentNode = this; }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(x=>x!==this); this.parentNode = null; }
}
const document = { createElement: () => new Node() };
const HS_ICONS = { lock:'lock', unlock:'unlock' };
const hsToolLabel = x => x;
const hsToolArg = x => x.state.input.command || '';
const hsScroll = () => {};
const hsAskQuestions = () => false;
const hsNewTurn = () => { throw Error('unexpected new turn'); };
const turn = { holder: new Node(), proc: new Node(), parts: new Map() };
const view = { turns:[turn], inp:{dataset:{}} };
let revealed = 0;
function hsProcHost(t) { return t.proc; }
function hsProcPlacePart(part, el) { part.turn.proc.insertBefore(el); }
function hsProcPlace(t, el) { t.proc.insertBefore(el); }
function hsProcReveal() { revealed++; }
function hsPart(v, mid, id, type) {
  if (!turn.parts.has(id)) turn.parts.set(id, {id, mid, type, turn});
  return turn.parts.get(id);
}
''' + ''.join(fn(n) for n in ['hsApprovalLabel', 'hsPaintApproval', 'hsAsk', 'hsAskClose']) + r'''
const part = hsPart(view, 'm', 'receipt', 'approval');
Object.assign(part, {requestID:'req', resolution:'pending', reason:'run test', input:{command:'echo test'}, at:100});
hsPaintApproval(view, part);
const original = part.el;
assert.equal(original.querySelector('.hs-acts'), null);
hsAsk(view, {id:'req', historyID:'receipt', messageID:'m', reason:'run test', input:part.input});
assert.equal(part.el, original);
assert.equal(turn.holder.children.length, 0);
assert.equal(turn.proc.children.length, 1);
assert.ok(part.el.querySelector('.hs-acts'));
hsPaintApproval(view, part); // A pending snapshot must not erase active controls.
assert.ok(part.el.querySelector('.hs-acts'));
hsAskClose(view);
assert.equal(turn.holder.children.length, 0);
assert.equal(turn.proc.children.length, 1);
Object.assign(part, {resolution:'timeout', resolvedAt:200});
hsPaintApproval(view, part);
assert.equal(part.el.querySelector('.hs-acts'), null);
assert.match(part.el.querySelector('.hs-t3').textContent, /审批已过期，操作未执行/);
assert.equal(part.el.querySelector('.hs-t2').textContent, 'echo test');
assert.equal(turn.proc.children.length, 1);
assert.ok(revealed > 0);
// A fresh page paints the persisted result without acquiring an approval handle.
const fresh = { ...part, el:null, turn:{holder:new Node(),proc:new Node(),parts:new Map()} };
fresh.turn.parts.set(fresh.id, fresh);
hsPaintApproval({}, fresh);
assert.equal(fresh.el.querySelector('.hs-acts'), null);
assert.match(fresh.el.querySelector('.hs-t3').textContent, /审批已过期，操作未执行/);
''' + fn('hsProcessShape', SHAPES) + fn('hsShapeSame', SHAPES) + r'''
assert.equal(hsShapeSame(hsProcessShape([{...part,resolution:'pending'}]), hsProcessShape([part])), false);
assert.equal(hsShapeSame(hsProcessShape([]), hsProcessShape([part])), false);
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=20, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
