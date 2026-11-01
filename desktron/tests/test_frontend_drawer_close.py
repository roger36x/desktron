"""手机抽屉什么时候该收(2026-08-24 用户实见:点工作流行的「…」,抽屉收了,菜单孤零零浮在会话上)。

抽屉收起的判据原来是一份「点了这些就收」的正向清单。问题出在嵌套:工作流行是 <summary>,
而「…」是它里面的一个 <button>,closest("summary") 照样命中,于是弹菜单的同时抽屉也收了——
菜单锚在抽屉里,抽屉一走它就悬空。

那些控件自己写了 stopPropagation,但拦不住:这个监听挂在 document 的捕获阶段,比它们先跑完。

也不能简单地「按钮一律不收」:任务/应用/工坊/设置那四个入口本身就是 <button>,
一刀切会把它们该有的收起也挡掉。分界是「这个控件是不是行本身」——嵌在行里头的才是就地操作。
"""
import json
import os
import re
import shutil
import subprocess
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RAIL = _srcmap.rail_src()
EVENTS = _srcmap.frontend_src("app-events.js")

HARNESS = r"""
// 极简 DOM:只要 closest / contains 这两件事
class El {
  constructor(tag, cls, id, parent){
    this.tag = tag; this.cls = (cls||"").split(" ").filter(Boolean);
    this.id = id || ""; this.parentNode = parent || null;
  }
  matches(sel){
    return sel.split(",").map((s)=>s.trim()).some((s)=>{
      if (s.startsWith("#")) return this.id === s.slice(1);
      if (s.startsWith(".")) return this.cls.includes(s.slice(1));
      return this.tag === s;
    });
  }
  closest(sel){
    for (let e = this; e; e = e.parentNode) if (e.matches && e.matches(sel)) return e;
    return null;
  }
  contains(o){ for (let e = o; e; e = e.parentNode) if (e === this) return true; return false; }
}
// 被测判据:下面那个占位符会被换成 app-rail.js 里现取的源码,不是抄一份。
// 抄的话源码一改这里照旧全绿,测的就成了副本自己(本会话已经栽过一次)。
// 占位符用 __ 包起来:名字若与上面的说明文字撞上,replace 会把代码也注进注释里,
// 那几行掉到顶层就成了「Illegal return statement」(刚踩过)。
function decide(target){
  const ev = { target };
  let closed = false;
  const closeDrawer = () => { closed = true; };
  const setTimeout = () => {};
  const window = { dispatchEvent(){} };
  const Event = function(){};
  const ret = (() => { __RULE__ })();
  return closed ? "收起" : "留着";
}
BODY
"""


def rule_src():
    """从 app-rail.js 里取出判据那几行(nav → closeDrawer 那一段)。"""
    seg = RAIL[RAIL.index("// 抽屉收起的两条路"):]
    seg = seg[:seg.index("}, true);")]
    start = seg.index("    // 选中了什么就收")
    return seg[start:]


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class DrawerClose(unittest.TestCase):
    def _run(self, body):
        js = HARNESS.replace("__RULE__", rule_src()).replace("BODY", body)
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_工作流行的省略号不收抽屉(self):
        """这一条就是用户看到的那个:菜单锚在抽屉里,抽屉一收它就悬空。"""
        got = self._run("""
const summary = new El("summary");
const btns = new El("span", "pf-btns", "", summary);
const more = new El("button", "pg-add proj-more", "", btns);
console.log(JSON.stringify(decide(more)));
""")
        self.assertEqual(got, "留着")

    def test_点工作流名字展开折叠不收(self):
        """工作流标题只展开/折叠会话列表，不导航到主区。"""
        got = self._run("""
const summary = new El("summary");
const name = new El("span", "pf-name", "", summary);
console.log(JSON.stringify(decide(name)));
""")
        self.assertEqual(got, "留着")

    def test_工作流标题箭头和空白不收(self):
        got = self._run("""
const summary = new El("summary");
const arrow = new El("svg", "", "", new El("span", "pf-chev", "", summary));
console.log(JSON.stringify([decide(summary), decide(arrow)]));
""")
        self.assertEqual(got, ["留着", "留着"])

    def test_会话行要收(self):
        got = self._run("""
const row = new El("div", "term-item");
const label = new El("span", "ti-label", "", row);
console.log(JSON.stringify(decide(label)));
""")
        self.assertEqual(got, "收起")

    def test_会话行的叉不收(self):
        """删一段会话是就地操作,顺手把抽屉也收走并不是人要的。
        它在触屏上现在常显(悬停显现会吃掉第一次点击,见 test_frontend_touch_menu)。"""
        got = self._run("""
const row = new El("div", "term-item");
const x = new El("span", "ti-x", "", row);
console.log(JSON.stringify(decide(x)));
""")
        self.assertEqual(got, "留着")

    def test_会话重命名菜单项不收抽屉(self):
        """菜单挂在 body；重命名仍要留住抽屉，才能编辑回填到会话行的输入框。"""
        got = self._run("""
const menu = new El("div", "rp-ctx");
const rename = new El("div", "rp-row rp-drawer-keep", "", menu);
const label = new El("span", "rp-title", "", rename);
console.log(JSON.stringify(decide(label)));
""")
        self.assertEqual(got, "留着")

    def test_四个顶层入口自己是按钮_照样要收(self):
        """不能简单地「按钮一律不收」——这四个本身就是 <button>。"""
        got = self._run("""
const out = {};
for (const id of ["fx-tasks", "fx-apps", "settings-btn"]) {
  out[id] = decide(new El("button", "", id));
}
console.log(JSON.stringify(out));
""")
        self.assertEqual(got, {"fx-tasks": "收起", "fx-apps": "收起", "settings-btn": "收起"})

    def test_入口按钮里的图标也要收(self):
        """点在按钮里的 svg 上:act 是那个按钮,而它本身就是 nav,不该被当成嵌套控件。"""
        got = self._run("""
const b = new El("button", "", "fx-tasks");
const ico = new El("svg", "ico", "", b);
console.log(JSON.stringify(decide(ico)));
""")
        self.assertEqual(got, "收起")

    def test_抽屉里的其它地方不收(self):
        """点在空白、分组标题上并没有选中什么,抽屉该留着。"""
        got = self._run("""
console.log(JSON.stringify(decide(new El("div", "wf-sec-title"))));
""")
        self.assertEqual(got, "留着")

    def test_会话列表的展开收起不收(self):
        """.sess-more 只是改列表长度,没有去主区看什么。它是行外的按钮,本来就不在清单里。"""
        got = self._run("""
console.log(JSON.stringify(decide(new El("button", "sess-more"))));
""")
        self.assertEqual(got, "留着")


class WiredThatWay(unittest.TestCase):
    def test_刷新保留手机抽屉但不永久记住(self):
        """刷新不应把正在使用的侧栏收走；新开窗口则仍从主区开始。"""
        self.assertIn('const drawerReloadKey = "cd_mobile_drawer_open";', RAIL)
        self.assertIn('sessionStorage.getItem(drawerReloadKey) === "1"', RAIL)
        self.assertIn('window.addEventListener("pagehide"', RAIL)
        self.assertIn('sessionStorage.setItem(drawerReloadKey, "1")', RAIL)
        self.assertIn('sessionStorage.removeItem(drawerReloadKey)', RAIL)
        self.assertNotIn('localStorage.setItem(drawerReloadKey', RAIL)

    def test_引擎菜单随抽屉收起且选择后立即关闭(self):
        """菜单被挪到 body 后不再跟随 #panel；两条关闭路径都必须显式存在。"""
        bind = EVENTS[EVENTS.index("function bind() {"):EVENTS.index("async function initEnginePick()")]
        self.assertIn('window.addEventListener("cd-drawer-closed", shutEngineMenu);', bind)
        pick = EVENTS[EVENTS.index("async function initEnginePick() {"):]
        click = pick[pick.index('row.onclick = async () => {'):]
        self.assertLess(click.index('menu.hidden = true;'), click.index('if (!shepherdActive && id === curEngine) return;'))
        self.assertLess(click.index('menu.hidden = true;'), click.index('api("/workbench/engine"'))

    def test_判据落在抽屉那个监听里(self):
        seg = RAIL[RAIL.index("// 抽屉收起的两条路"):]
        seg = seg[:seg.index("}, true);")]
        self.assertIn('const act = ev.target.closest("button, .ti-x");', seg)
        self.assertIn("act !== nav", seg)

    def test_会话重命名动作声明保持抽屉(self):
        panel = _srcmap.panel_src()
        start = panel.index("function openTermCtxMenu")
        menu = panel[start:panel.index("// 放掉 GUI 会话", start)]
        self.assertIn('label: DeskI18n.t("menu.rename")', menu)
        self.assertIn("keepDrawer: true", menu)
        renderer = panel[panel.index("function openCtxMenu"):panel.index("function renameDomain")]
        self.assertIn('r.classList.add("rp-drawer-keep")', renderer)

    def test_先认行再认控件(self):
        """顺序要紧:没落在行上的点击直接留着,不必再判控件。"""
        seg = RAIL[RAIL.index("// 抽屉收起的两条路"):]
        seg = seg[:seg.index("}, true);")]
        self.assertLess(seg.index("const nav ="), seg.index("const act ="))
        self.assertLess(seg.index("if (!nav) return;"), seg.index("const act ="))
