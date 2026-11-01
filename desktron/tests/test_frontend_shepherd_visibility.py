"""Shepherd 模式拥有独立会话书桌，不能混入当前原生引擎的普通会话。"""
import os
import re
import subprocess
import unittest

FRONT = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
EVENTS = open(os.path.join(FRONT, "app-events.js"), encoding="utf-8").read()
SIDEBAR = open(os.path.join(FRONT, "app-panel-sidebar.js"), encoding="utf-8").read()
TERMS = open(os.path.join(FRONT, "app-panel-terms.js"), encoding="utf-8").read()
SEARCH = open(os.path.join(FRONT, "app-rail-nav.js"), encoding="utf-8").read()
CREATE = open(os.path.join(FRONT, "app-assets-create.js"), encoding="utf-8").read()
PAGES = open(os.path.join(FRONT, "app-pages.js"), encoding="utf-8").read()
OPTS = open(os.path.join(FRONT, "app-chat-opts.js"), encoding="utf-8").read()
CHAT_PAGE = open(os.path.join(FRONT, "app-chat-page.js"), encoding="utf-8").read()
CSS = open(os.path.join(FRONT, "style.css"), encoding="utf-8").read()
TOPNAV = open(os.path.join(FRONT, "app-rail-topnav.js"), encoding="utf-8").read()


class ShepherdVisibility(unittest.TestCase):
    def test_统一谓词双向隔离(self):
        match = re.search(r"function sessionVisibleInCurrentMode\(row\) \{[\s\S]+?\n\}", EVENTS)
        self.assertIsNotNone(match)
        script = """
let curEngine='pi', shepherdMode=false;
%s
const ordinary={engine:'pi'}, shepherd={engine:'pi',shepherd_mode:true};
if (!sessionVisibleInCurrentMode(ordinary) || sessionVisibleInCurrentMode(shepherd)) process.exit(1);
shepherdMode=true;
if (sessionVisibleInCurrentMode(ordinary) || !sessionVisibleInCurrentMode(shepherd)) process.exit(2);
""" % match.group(0)
        subprocess.run(["node", "-e", script], check=True)

    def test_书桌模式不被当前错误页面反向覆盖(self):
        self.assertNotIn("hsSituationCurrentView", re.search(
            r"function sessionVisibleInCurrentMode\(row\) \{[\s\S]+?\n\}", EVENTS).group(0))

    def test_恢复会话按公开别名核验真实模式(self):
        predicate = re.search(
            r"function sessionVisibleInCurrentMode\(row\) \{[\s\S]+?\n\}", EVENTS).group(0)
        visible = re.search(
            r"function _sessionViewVisible\(sid, fallback\) \{[\s\S]+?\n\}", TOPNAV).group(0)
        script = """
const assert=require('node:assert/strict');
let curEngine='codex', shepherdMode=true, shepherdTrial=false;
const _termsList=[{id:'native-codex',session_id:'adp_ordinary',engine:'codex'}];
function termMatchesId(row,sid){
 return [row.id,row.resume_id,row.session_id].filter(Boolean).includes(sid);
}
%s
%s
assert.equal(_sessionViewVisible('adp_ordinary',{shepherd_mode:true}),false);
""" % (predicate, visible)
        subprocess.run(["node", "-e", script], check=True)

    def test_侧栏搜索恢复计数共用谓词(self):
        self.assertIn("sessionBuckets().byDomain", SIDEBAR)
        self.assertIn("sessionBuckets().loose", SIDEBAR)
        self.assertIn("sessionVisibleInCurrentMode(t)", TERMS)
        self.assertIn("sessionVisibleInCurrentMode(s)", SEARCH)
        self.assertGreaterEqual(CREATE.count("sessionVisibleInCurrentMode(t)"), 2)

    def test_聊天分组按书桌模式双向隔离(self):
        predicate = re.search(
            r"function sessionVisibleInCurrentMode\(row\) \{[\s\S]+?\n\}", EVENTS).group(0)
        loose = re.search(r"function looseSessions\(\) \{[\s\S]+?\n\}", SIDEBAR).group(0)
        invalidate = re.search(r"function invalidateSessionBuckets\(\) \{[^\n]+\}", TERMS).group(0)
        buckets = re.search(r"function sessionBuckets\(\) \{[\s\S]+?\n\}", TERMS).group(0)
        script = """
const assert=require('node:assert/strict');
const DESKTOP='_desktop';
let curEngine='pi', shepherdMode=false;
let _termsList=[
 {id:'ordinary',engine:'pi',domain:DESKTOP},
 {id:'sheep',engine:'pi',domain:DESKTOP,shepherd_mode:true},
 {id:'other',engine:'codex',domain:DESKTOP},
];
let _sessionBucketsCache=null;
function applyOrder(items){return items;}
function _pinSessionOrder(){return [];}
function termOrderId(t){return t.id;}
%s
%s
%s
%s
assert.deepEqual(looseSessions().map(x=>x.id),['ordinary']);
shepherdMode=true;
assert.deepEqual(looseSessions().map(x=>x.id),['sheep']);
""" % (predicate, invalidate, buckets, loose)
        subprocess.run(["node", "-e", script], check=True)

    def test_Shepherd_搜索走独立后端而不回退普通引擎正文(self):
        self.assertNotRegex(SEARCH, r"if \(shepherdMode\) \{\s*sideSearchPaint\(query, \{ results: \[\]")
        self.assertIn('shepherdMode ? "shepherd" : curEngine', SEARCH)

    def test_标签页持久化并按模式恢复(self):
        self.assertIn("if (v && v.shepherdMode) row.shepherd_mode = true", PAGES)
        self.assertIn("? d.shepherd_mode === true", PAGES)
        self.assertIn("d.shepherd_mode !== true", PAGES)

    def test_正式入口合并旧试用会话且不显示试用标签(self):
        predicate = re.search(
            r"function sessionVisibleInCurrentMode\(row\) \{[\s\S]+?\n\}", EVENTS).group(0)
        self.assertNotIn("shepherd_trial", predicate)
        for source in (EVENTS, TERMS, OPTS, CHAT_PAGE, CSS):
            self.assertNotIn("Shepherd 试用", source)
            self.assertNotIn("统一存储试用", source)
            self.assertNotIn("shepherd-trial-badge", source)

    def test_切换模式后重载独立书桌(self):
        self.assertIn('deskStartupMark("reload", "shepherd_mode_switch")', EVENTS)
        self.assertRegex(EVENTS, r"shepherdMode = !!result\.shepherd_mode;[\s\S]{0,180}location\.reload\(\)")

    def test_左下普通引擎与Shepherd互斥(self):
        self.assertIn('body: JSON.stringify({ engine: id, shepherd_mode: false })', EVENTS)
        self.assertNotIn('/session/${encodeURIComponent(activeView.sid)}/engine', EVENTS)
        self.assertNotIn('fm-current', EVENTS)

    def test_Shepherd执行引擎在聊天框即时切换(self):
        self.assertIn('function hsPickShepherdEngine(v, anchor)', OPTS)
        self.assertIn('function hsSwitchShepherdEngine(v, engine)', OPTS)
        self.assertIn('async function hsFlushShepherdEngine(v)', OPTS)
        self.assertIn('/session/${encodeURIComponent(v.sid)}/engine', OPTS)
        self.assertIn('label: DeskI18n.t("model.engine")', OPTS)
        self.assertNotIn('label: "执行引擎"', OPTS)
        self.assertIn('const collapsedModelText = hsCollapsedModelLabel(hsOptLabel(modelText))', OPTS)
        self.assertIn('hsUiText(m1, v.shepherdMode ? currentText : collapsedModelText)', OPTS)
        self.assertNotIn('→ 下轮', OPTS)
        self.assertNotIn('下一回合', OPTS)
        self.assertNotIn('v.nextEngine', OPTS)
        self.assertNotIn('classList.toggle("switching"', OPTS)
        self.assertNotIn('hs-model-spin', CHAT_PAGE)
        self.assertNotIn('hs-model-switch-spin', CSS)
        self.assertIn('hsUiHidden(m2, !efforts.length)', OPTS)

    def test_快速连续选择只让最新响应更新控件(self):
        start = OPTS.index('function hsSwitchShepherdEngine')
        end = OPTS.index('\nfunction hsPickShepherdEngine', start)
        functions = OPTS[start:end]
        script = """
const assert=require('node:assert/strict');
let calls=[], synced=0, paints=0, releases=[], preferences=[];
let engineInfo={engine:'claude'};
function api(path,opts){const body=JSON.parse(opts.body);
 if(path==='/workbench/engine'){preferences.push(body.shepherd_engine);return Promise.resolve({ok:true});}
 calls.push(body.engine);
 return new Promise(resolve=>releases.push(()=>resolve({engine:body.engine,opts:{model:body.engine+'-model'}})));}
async function hsEngineSync(v){synced++;}
function hsOptsPaint(v){paints++;}
function setStatusMsg(){}
%s
(async()=>{
 const v={sid:'adp_s',shepherdMode:true,engine:'pi',opts:{model:'pi-model'},catalog:{},realModel:'pi-real'};
 hsSwitchShepherdEngine(v,'claude');
 hsSwitchShepherdEngine(v,'codex');
 hsSwitchShepherdEngine(v,'pi');
 assert.equal(v.engine,'pi'); assert.deepEqual(calls,['claude']);
 releases.shift()(); await new Promise(r=>setImmediate(r));
 assert.equal(v.engine,'pi'); assert.deepEqual(calls,['claude','pi']);
 releases.shift()(); await new Promise(r=>setImmediate(r));
 assert.equal(v.engine,'pi'); assert.equal(v.sessionOpts.model,'pi-model');
 assert.ok(!calls.includes('codex')); assert.equal(synced,1); assert.ok(paints>=3);
 assert.deepEqual(preferences,['pi']);
 assert.equal(engineInfo.shepherd_engine,'pi'); assert.equal(engineInfo.engine,'claude');
})().catch(e=>{console.error(e);process.exit(1)});
""" % functions
        subprocess.run(["node", "-e", script], check=True)

    def test_first_engine_selection_uses_target_defaults_before_response(self):
        function = re.search(r"function hsSwitchShepherdEngine\(.*?\n}\n", OPTS, re.S).group()
        script = r'''
const assert=require('node:assert/strict');
function hsOptsPaint(v){v.opts={...v.sessionOpts};}
function hsFlushShepherdEngine(){}
''' + function + r'''
const defaults={sandbox:'danger-full-access',model:'codex-model',desk_role:'ops',
                active_engine:'pi',shepherd_mode:true};
const v={shepherdMode:true,engine:'pi',opts:{mode:'bypassPermissions'},
         engineSnap:{opts:{codex:defaults}}};
hsSwitchShepherdEngine(v,'codex');
assert.equal(v.opts.sandbox,'danger-full-access');
assert.equal(v.opts.model,'codex-model');
for(const key of ['desk_role','active_engine','shepherd_mode'])assert(!(key in v.opts));
assert.equal(defaults.desk_role,'ops');
v.opts.sandbox='read-only';
hsSwitchShepherdEngine(v,'pi');hsSwitchShepherdEngine(v,'codex');
assert.equal(v.opts.sandbox,'read-only');
'''
        subprocess.run(["node", "-e", script], check=True)

    def test_主区快照按模式独立且拒绝旧快照串台(self):
        predicate = re.search(r"function sessionVisibleInCurrentMode\(row\) \{[\s\S]+?\n\}", EVENTS).group(0)
        source = TOPNAV.split('// 顶层入口互斥', 1)[0]
        script = """
const assert=require('node:assert/strict');
let shepherdMode=false, curEngine='pi', overlayOpen=true;
let pActive={url:'chat:ordinary',title:'ordinary'}, pages=[], shown='';
let _termsList=[{id:'ordinary',engine:'pi'},{id:'sheep',engine:'claude',shepherd_mode:true}];
const stored=new Map();
const localStorage={getItem:k=>stored.get(k)||null,setItem:(k,v)=>stored.set(k,v)};
function showOverlay(p){shown=p.url;}
async function openHtmlSession(id,title){pages.push({url:'chat:'+id,title});}
%s
%s
function _restoreEngineWelcome(){shown='welcome';return true;}
(async()=>{
  _restoringEngineViewState=false;
  rememberEngineView('pi');
  shepherdMode=true; pActive={url:'chat:sheep',title:'sheep'};
  rememberEngineView('pi');
  assert.equal(_engineViewsRead().pi.id,'ordinary');
  assert.equal(_engineViewsRead().shepherd.id,'sheep');
  await restoreEngineView('codex');
  assert.equal(shown,'chat:sheep'); // 执行引擎切换不改变书桌归属
  shepherdMode=false;
  await restoreEngineView('pi'); assert.equal(shown,'chat:ordinary');
  const old=_engineViewsRead(); old.pi={kind:'chat',id:'sheep',rev:2};
  localStorage.setItem(_ENGINE_VIEWS_KEY,JSON.stringify(old));
  await restoreEngineView('pi'); assert.equal(shown,'welcome');
  shepherdMode=true; old.shepherd={kind:'chat',id:'ordinary',rev:2};
  localStorage.setItem(_ENGINE_VIEWS_KEY,JSON.stringify(old));
  await restoreEngineView('pi'); assert.equal(shown,'welcome');
})().catch(e=>{console.error(e);process.exit(1);});
""" % (predicate, source)
        subprocess.run(["node", "-e", script], check=True)


if __name__ == "__main__":
    unittest.main()
