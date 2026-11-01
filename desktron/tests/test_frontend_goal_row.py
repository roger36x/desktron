"""目标行的落位:设与达成两条,规则相反。

「已设定目标」的哨兵在记录里排在用户那句话之前,照原样铺会落在气泡上方,读成先有结论
后有请求——所以推迟到那一回合建出来之后,挂在气泡下面。

「目标已达成」发生在上一轮收尾的那一刻，必须归入上一回合末尾。不能依赖记录顺序：
目标哨兵可能先于助手消息落盘，直接按流顺序铺会把它夹在状态行与回答正文之间。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

import _srcmap
from frontend_i18n_js import NODE_PRELUDE

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()


class Placement(unittest.TestCase):
    def _seg(self):
        i = CHAT.index("      if (info.goal) {")
        return CHAT[i:i + 900]

    def test_达成那条不推迟(self):
        seg = self._seg()
        self.assertIn("if (info.goal.met) hsGoalRow(v, info.goal, null);", seg)
        self.assertIn("else pendGoal = info.goal;", seg)

    def test_设定那条仍然推迟(self):
        """它要等那一回合的气泡建出来才挂得上去。"""
        i = CHAT.index("if (pendGoal) { hsGoalRow(v, pendGoal, t); pendGoal = null; }")
        self.assertGreater(i, 0)

    def test_不给_turn_就归入上一回合末尾(self):
        """达成必须属于上一回合，不能依赖目标哨兵的落盘顺序。"""
        m = re.search(r"\nfunction hsGoalRow\(v, g, turn\)[\s\S]*?\n}\n", CHAT)
        self.assertTrue(m, "hsGoalRow 不见了")
        self.assertIn("const prev = v.turns[v.turns.length - 1]", m.group(0))
        self.assertIn("prev.el.appendChild(el)", m.group(0))
        self.assertIn("else v.stream.appendChild(el)", m.group(0))
        self.assertIn("if (u) turn.el.insertBefore(el, u.nextSibling);", m.group(0))

    def test_两种文案分得开(self):
        m = re.search(r"\nfunction hsGoalRow\(v, g, turn\)[\s\S]*?\n}\n", CHAT)
        self.assertIn('DeskI18n.t(g.met ? "goal.met" : "goal.set")', m.group(0))


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class Order(unittest.TestCase):
    """按回合结构铺一遍，确认达成行固定属于上一回合。"""

    HARNESS = r"""
class Node {
  constructor(tag){this.tag=tag;this.children=[];this._t="";this.className="";this.title="";}
  set textContent(v){this._t=v;this.children=[];}
  get textContent(){return this._t + this.children.map(c=>c.textContent).join("");}
  set innerHTML(v){ this.children=[new Node("div")]; this.children[0].className="hs-cmp-h";
                    this.children[0].children=[new Node("span")]; }
  appendChild(n){this.children.push(n); n.parentNode=this; return n;}
  insertBefore(n,ref){const i=this.children.indexOf(ref); this.children.splice(i<0?this.children.length:i,0,n); n.parentNode=this; return n;}
  querySelector(sel){
    const hit=(x)=>("."+String(x.className||"").split(" ").join(".")).indexOf(sel)>=0
                   || (sel==="span" && x.tag==="span");
    const walk=(x)=>{ for(const c of x.children){ if(hit(c)) return c; const r=walk(c); if(r) return r; } return null; };
    return walk(this);
  }
}
const document = { createElement: (t)=>new Node(t) };
function hsEllipsis(s,n){ s=String(s||""); return s.length>n ? s.slice(0,n-1)+"…" : s; }
SRC
// 一条会话的记录:提问 → 回答 → 目标达成 → 再提问 → 再回答
const v = { stream: new Node("div"), turns: [] };
function bubble(text){
  const t = { el: new Node("div"), holder: new Node("div") };
  t.el.className = "hs-turn";
  const u = new Node("div"); u.className = "hs-u"; u.textContent = text;
  t.el.appendChild(u);
  t.holder.className = "hs-a";
  t.el.appendChild(t.holder);
  v.stream.appendChild(t.el);
  v.turns.push(t);
  return t;
}
const t1 = bubble("修一下 rc");
t1.holder.appendChild(Object.assign(new Node("div"), { className: "reply", _t: "修好了" }));
hsGoalRow(v, { condition: "修复 rc 直到完全正常", met: true }, null);   // 达成:不给 turn
const t2 = bubble("Mac 重启,你说的是 desktop 吧?");
t2.holder.appendChild(Object.assign(new Node("div"), { className: "reply", _t: "是的" }));
console.log(JSON.stringify({
  stream: v.stream.children.map((x)=>[x.className, x.textContent]),
  turn1: t1.el.children.map((x)=>[x.className, x.textContent])
}));
"""

    def _run(self):
        m = re.search(r"\nfunction hsGoalRow\(v, g, turn\)[\s\S]*?\n}\n", CHAT)
        js = self.HARNESS.replace("SRC", m.group(0))
        out = subprocess.run(["node", "-e", NODE_PRELUDE + js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_达成落在回复之后_提问之前(self):
        rows = self._run()
        kinds = [r[0] for r in rows["turn1"]]
        self.assertIn("hs-a", kinds[-2], "回答容器不在目标达成之前")
        self.assertIn("hs-goal-row", kinds[-1], "达成没有固定在上一回合末尾")
        self.assertIn("Mac 重启", rows["stream"][1][1], "下一句提问没有另起回合")

    def test_达成不落进气泡那一格(self):
        """挂进气泡之后就成了「这句话把目标达成的」。"""
        rows = self._run()
        self.assertNotIn("hs-goal-row", rows["turn1"][0][0])
        self.assertIn("hs-goal-row", rows["turn1"][-1][0])

    def test_写的是达成不是设定(self):
        rows = self._run()
        row = next(r for r in rows["turn1"] if "hs-goal-row" in r[0])
        self.assertIn("目标已达成", row[1])
        self.assertIn("met", row[0])


if __name__ == "__main__":
    unittest.main()
