"""压缩边界按输出顺序落位，正文与可折叠过程区共用同一条规则。"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktron/lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", str(ROOT / "frontend"), "7999", "7998", str(ROOT)]
try:
    import engine_adapters as ea
finally:
    sys.argv = _argv

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


class Bus:
    def publish(self, event):
        pass


class NativeCompactOrder(unittest.TestCase):
    def test_codex_keeps_boundary_between_output_parts(self):
        for cls in (ea.CodexAdapter,):
            with self.subTest(engine=cls.name), tempfile.TemporaryDirectory() as tmp:
                ad = cls(tmp, tmp, Bus())
                sid = ad.create_session()["id"]
                rows = [
                    {"type": "response_item", "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "问题"}]}},
                    {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                     "phase": "commentary", "content": [{"type": "output_text", "text": "之前"}]}},
                    {"type": "compacted", "payload": {"replacement_history": [
                        {"type": "message", "role": "user", "content": "不能重放的快照"}]}},
                    {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "之后"}]}},
                ]
                path = Path(tmp) / "rollout.jsonl"
                path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
                got = ad._group_rollout(str(path), sid)
                self.assertEqual([p["type"] for p in got[-1]["parts"]],
                                 ["reasoning", "compact", "text"])
                self.assertNotIn("不能重放的快照", json.dumps(got, ensure_ascii=False))
                path.write_text(json.dumps(rows[0]) + "\n" +
                                '{"type":"compacted","payload":{"replacement_history":[')
                pending = ad._group_rollout(str(path), sid)
                self.assertFalse(any(p["type"] == "compact" for m in pending for p in m["parts"]))

    def test_claude_auto_and_manual_stay_after_previous_output(self):
        for trigger in ("auto", "manual"):
            with self.subTest(trigger=trigger), tempfile.TemporaryDirectory() as tmp:
                ad = ea.ClaudeAdapter(tmp, tmp, Bus())
                sid = ad.create_session()["id"]
                rows = [
                    {"type": "user", "uuid": "u", "message": {"content": "问题"}},
                    {"type": "assistant", "uuid": "a", "message": {
                        "content": [{"type": "thinking", "thinking": "之前"}]}},
                    {"type": "system", "compactMetadata": {"trigger": trigger}},
                    {"type": "user", "uuid": "c", "isCompactSummary": True,
                     "message": {"content": "摘要"}},
                    {"type": "assistant", "uuid": "b", "message": {
                        "content": [{"type": "text", "text": "之后"}]}},
                ]
                for i, row in enumerate(rows):
                    row["timestamp"] = f"2026-09-12T10:00:0{i}Z"
                path = Path(tmp) / "claude.jsonl"
                path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
                got = ad._group_transcript(str(path), sid, "esid")
                self.assertEqual([m["info"]["role"] for m in got],
                                 ["user", "assistant", "user", "assistant"])
                self.assertEqual(got[1]["parts"][0]["text"], "之前")
                self.assertEqual(got[2]["info"]["compact"]["trigger"], trigger)

    def test_pi_boundary_preserves_previous_and_following_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            ad = ea.PiAdapter(tmp, tmp, Bus())
            sid = ad.create_session()["id"]
            rows = [
                {"type": "message", "message": {"role": "user", "content": "问题"}},
                {"type": "message", "message": {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "之前"}]}},
                {"type": "compaction", "summary": "摘要", "tokensBefore": 50000},
                {"type": "message", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "之后"}]}},
            ]
            for i, row in enumerate(rows):
                row.update(id=str(i), parentId=str(i - 1) if i else None,
                           timestamp=f"2026-09-12T10:00:0{i}Z")
            path = Path(tmp) / "pi.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            got = ad._pi_group(str(path), sid)
            self.assertEqual(got[1]["parts"][0]["text"], "之前")
            self.assertIn("compact", got[2]["info"])
            self.assertEqual(got[3]["parts"][0]["text"], "之后")


def function(file, name):
    src = (ROOT / "frontend" / file).read_text()
    return re.search(r"function " + name + r"\(.*?\n}\n", src, re.S).group()


@unittest.skipUnless(sync_playwright, "需要 Playwright WebKit")
@pytest.mark.desktop
class CompactPlacementBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = (cls.pw.chromium.launch(channel="chrome", timeout=10000)
                        if os.environ.get("DESK_COMPACT_BROWSER") == "chrome"
                        else cls.pw.webkit.launch(timeout=10000))
        cls.page = cls.browser.new_page()
        cls.page.set_default_timeout(10000)

    def setUp(self):
        self.page.goto("about:blank")
        self.page.set_content('<div class="hs-page"><div id="stream"></div></div>')
        self.page.add_style_tag(content=(ROOT / "frontend/style.css").read_text())
        source = '\n'.join((ROOT / 'frontend' / name).read_text() for name in (
            'app-i18n.js', 'app-i18n-zh.js'))
        source += r'''
const md={render:s=>s};
const v={turns:[],roles:new Map(),asideOf:new Map(),stream:document.querySelector('#stream')};
function hsNewTurn(v,txt,uid){
  const el=document.createElement('div'),holder=document.createElement('div');
  el.className='hs-turn'; holder.className='hs-a';
  if(txt!==null){const user=document.createElement('div');user.className='hs-u';user.textContent=txt;el.append(user);}
  el.append(holder); v.stream.append(el);
  const t={view:v,el,holder,parts:new Map(),user:txt,uid};v.turns.push(t);return t;
}
function hsProcOpenSet(){return new Set();}
function hsProcOpenKey(){return '';}
function hsProcOpenSave(){}
function hsPaint(v,p){
  if(p.type==='compact'){hsPaintCompact(v,p);return;}
  const parent=['tool','reasoning'].includes(p.type)?hsProcHost(p.turn):p.turn.holder;
  if(!p.el){p.el=document.createElement('div');p.el.className='output '+p.type;}
  p.el.textContent=p.text||p.id;
  if(!parent.contains(p.el))parent.append(p.el);
}
function hsFoot(){}
function hsProcDone(t){if(t.procEl)t.procEl.querySelector('.hs-proc-bar').textContent='已完成';}
function hsSkipPart(){return false;}
function isInjected(){return false;}
function hsTouch(){}
function deskLog(){}
function hsStatus(){}
function hsBusy(v,on){v.busy=on;}
function hsRetrySync(){}
function hsActivitySync(){}
function hsViewportChange(v,fn){return fn();}
function hsLogStep(){}
function hsToolOutcome(){return '';}
function hsToolObj(){return {t:''};}
function hsTrimDom(){}
function hsErrorNotice(){}
function hsCompactOn(){}
function hsCompactOff(){}
function hsScroll(){}
function hsFlush(){}
function add(type,text,id){const p=hsPart(v,'a',id,type);p.text=text;hsPaint(v,p);return p;}
function reset(){v.stream.replaceChildren();v.turns=[];v.roles=new Map();v.asideOf=new Map();}
function history(type){return [
 {info:{id:'u',role:'user'},parts:[{type:'text',text:'问题'}]},
 {info:{id:'a',role:'assistant'},parts:[{id:'before',type,text:'之前'}]},
 {info:{id:'c',role:'user',compact:{trigger:'auto'}},parts:[{type:'text',text:'摘要'}]},
 {info:{id:'b',role:'assistant'},parts:[{id:'after',type,text:'之后'}]}];}
'''
        for file, names in {
            "app-chat-model.js": ["hsTurnFor", "hsPart", "hsCompactAnchor", "hsCompactRow", "hsPaintCompact", "hsCmpTok"],
            "app-chat-tools.js": ["hsProcBox", "hsProcRepaint"],
            "app-chat-detail.js": ["hsProcHost"],
            "app-chat-reconcile.js": ["hsPour", "hsProcessShape", "hsMessageShape", "hsViewShape", "hsShapeSame"],
            "app-chat-core.js": ["hsApply"],
        }.items():
            source += "\n".join(function(file, name) for name in names)
        self.page.add_script_tag(content=source)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_history_process_boundary_is_inside_one_fold_and_survives_reload(self):
        for kind in ("reasoning", "tool"):
            for width in (1000, 390):
                self.page.set_viewport_size({"width": width, "height": 844})
                self.page.evaluate("kind=>{reset();hsPour(v,history(kind));}", kind)
                self.assertEqual(self.page.locator('.hs-proc').count(), 1)
                self.assertEqual(self.page.locator('.hs-proc-body > *').all_text_contents(),
                                 ['之前', '上下文已压缩 · 自动摘要', '之后'])
                self.assertFalse(self.page.locator('.hs-cmp').is_visible())
                self.page.locator('.hs-proc-bar').click()
                self.assertTrue(self.page.locator('.hs-cmp').is_visible())
                self.page.locator('.hs-proc-bar').click()
                self.assertFalse(self.page.locator('.hs-cmp').is_visible())
                self.assertTrue(self.page.evaluate('hsShapeSame(hsMessageShape(history("'+kind+'")),hsViewShape(v))'))

    def test_shepherd_checkpoint_boundaries_survive_reload_without_live_duplicates(self):
        from engine_adapters.shepherd import ShepherdAdapter
        from engine_adapters.lifecycle import compact_update
        for kind in ('reasoning', 'tool', 'text'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                ad = ShepherdAdapter(tmp, tmp, Bus())
                ctx = {'sid':'adp_compact_ui', 'msg_id':'a', 'seq':1, 'text':'question',
                       'parts':{}, 'order':[], 't0':0}
                ad._live[ctx['sid']] = ctx
                ad._ensure_part(ctx, 'before', kind, text='之前')
                compact_update(ad, ctx, {'running':True, 'trigger':'auto'})
                ad._ensure_part(ctx, 'after', kind, text='之后')
                compact_update(ad, ctx, {'running':False, 'trigger':'auto', 'result':'success'})
                message = ad._read_live_checkpoint(ctx['sid'])
                history = [{'info':{'id':'u', 'role':'user'},
                            'parts':[{'type':'text', 'text':'问题'}]}, message]
                for width in (1000, 390):
                    self.page.set_viewport_size({'width':width, 'height':844})
                    for _ in range(2):
                        self.page.evaluate('''msgs=>{reset();hsPour(v,msgs);
                          hsApply(v,'session.compact',{running:false,trigger:'auto',result:'success'});
                        }''', history)
                        self.assertEqual(self.page.locator('.hs-cmp').count(), 1)
                        self.assertEqual(self.page.locator('.hs-turn').count(), 1)
                        self.assertEqual(self.page.locator('.hs-u').count(), 1)
                        parent = '.hs-a' if kind == 'text' else '.hs-proc-body'
                        self.assertEqual(self.page.locator(parent + ' > *').all_text_contents(),
                                         ['之前', '上下文已压缩 · 自动', '之后'])
                        self.assertTrue(self.page.evaluate(
                            'msgs=>hsShapeSame(hsMessageShape(msgs),hsViewShape(v))', history))

    def test_text_boundary_follows_output_not_user_or_end(self):
        self.page.evaluate('hsPour(v,history("text"))')
        self.assertEqual(self.page.locator('.hs-a > *').all_text_contents(),
                         ['之前', '上下文已压缩 · 自动摘要', '之后'])
        self.assertEqual(self.page.locator('.hs-proc').count(), 0)

    def test_live_captures_start_output_and_deduplicates_finish(self):
        for trigger in ('auto', 'manual', 'threshold'):
            self.page.evaluate('''trigger=>{reset(); add('reasoning','之前','p1');
              hsApply(v,'session.compact',{running:true,trigger});
              add('reasoning','之后','p2');
              hsApply(v,'session.compact',{running:false,trigger,result:'success'});
              hsApply(v,'session.compact',{running:false,trigger,result:'success'});
            }''', trigger)
            self.assertEqual(self.page.locator('.hs-cmp').count(), 1)
            self.assertEqual(self.page.locator('.hs-proc-body > *').last.inner_text(), '之后')
            self.assertEqual(self.page.locator('.hs-cmp').evaluate('(e)=>e.previousSibling.textContent'), '之前')

    def test_failure_has_no_separator(self):
        self.page.evaluate('''add('tool','之前','p');
          hsApply(v,'session.compact',{running:true});
          hsApply(v,'session.compact',{running:false,result:'failed'});''')
        self.assertEqual(self.page.locator('.hs-cmp').count(), 0)

    def test_shepherd_manual_waits_for_product_terminal_and_follows_command(self):
        self.page.evaluate('''reset();add('text','回复','p1');hsNewTurn(v,'/compact','u2');
          v.shepherdMode=true;v.manualCompact=true;
          hsApply(v,'session.compact',{running:true,trigger:'manual'});
          hsApply(v,'session.compact',{running:true,trigger:'auto',messageID:'native'});
          hsApply(v,'session.compact',{running:false,trigger:'auto',result:'success',messageID:'native'});''')
        self.assertEqual(self.page.locator('.hs-cmp').count(), 0)
        self.page.evaluate("hsApply(v,'session.compact',{running:false,trigger:'manual',result:'success'})")
        self.assertEqual(self.page.locator('.hs-cmp').count(), 1)
        self.assertEqual(self.page.locator('.hs-cmp').inner_text(), '上下文已压缩 · 手动')
        self.assertEqual(self.page.locator('.hs-cmp').evaluate(
            '(e)=>e.previousSibling.textContent'), '/compact')

    def test_native_part_boundary_and_repair_of_empty_process(self):
        self.page.evaluate('''hsPour(v,[{info:{id:'a',role:'assistant'},parts:[
          {id:'p1',type:'reasoning',text:'之前'},
          {id:'c',type:'compact',compact:{},text:''},
          {id:'p2',type:'reasoning',text:'之后'}]}]);
          document.querySelector('.hs-proc-body').replaceChildren();''')
        self.page.locator('.hs-proc-bar').click()
        self.assertEqual(self.page.locator('.hs-proc-body > *').all_text_contents(),
                         ['之前', '上下文已压缩', '之后'])

    def test_manual_compaction_follows_command_and_repeated_markers_keep_order(self):
        self.page.evaluate('''add('text','回复','p1');
          hsNewTurn(v,'/compact','u2');
          hsCompactRow(v,'',{trigger:'manual'});
          hsCompactRow(v,'',{trigger:'manual'});''')
        self.assertEqual(self.page.locator('.hs-turn').first.locator('.hs-cmp').count(), 0)
        self.assertEqual(self.page.locator('.hs-turn').last.locator('.hs-cmp').count(), 2)
        self.assertEqual(self.page.locator('.hs-cmp').first.evaluate(
            '(e)=>e.previousSibling.textContent'), '/compact')


@unittest.skipUnless(os.environ.get("DESK_JSDOM_PATH"), "需要 DESK_JSDOM_PATH")
class CompactPlacementDOM(unittest.TestCase):
    def test_production_routing_and_process_rebuild(self):
        class Page:
            def goto(self, *_): pass
            def set_content(self, content): self.html = content
            def add_style_tag(self, **kwargs): pass
            def add_script_tag(self, content): self.script = content

        self.page = Page()
        CompactPlacementBrowser.setUp(self)
        checks = r'''
const assert=require('node:assert/strict');
for(const kind of ['reasoning','tool','text']) {
  reset();hsPour(v,history(kind));
  const parent=kind==='text'?'.hs-a':'.hs-proc-body';
  assert.deepEqual(Array.from(document.querySelector(parent).children,e=>e.textContent),
    ['之前','上下文已压缩 · 自动摘要','之后']);
  assert.equal(v.turns.length,1);
  assert.equal(hsShapeSame(hsMessageShape(history(kind)),hsViewShape(v)),true);
  if(kind!=='text') {
    const box=document.querySelector('.hs-proc');
    box.querySelector('.hs-proc-bar').click();assert.equal(box.classList.contains('open'),true);
    box.querySelector('.hs-proc-bar').click();assert.equal(box.classList.contains('open'),false);
    box.querySelector('.hs-proc-body').replaceChildren();
    box.querySelector('.hs-proc-bar').click();
    assert.equal(box.querySelectorAll('.hs-cmp').length,1);
    assert.equal(box.querySelector('.hs-cmp').previousSibling.textContent,'之前');
  }
}
for(const trigger of ['auto','manual','threshold']) {
  reset();add('reasoning','之前','p1');
  hsApply(v,'session.compact',{running:true,trigger});add('reasoning','之后','p2');
  hsApply(v,'session.compact',{running:false,trigger,result:'success'});
  hsApply(v,'session.compact',{running:false,trigger,result:'success'});
  assert.equal(document.querySelectorAll('.hs-cmp').length,1);
  assert.equal(document.querySelector('.hs-cmp').previousSibling.textContent,'之前');
  assert.equal(hsCompactAnchor(v).text,'之后');
}
reset();add('text','回复','p1');hsNewTurn(v,'/compact','u2');
hsCompactRow(v,'',{trigger:'manual'});
assert.equal(document.querySelectorAll('.hs-turn')[0].querySelectorAll('.hs-cmp').length,0);
assert.equal(document.querySelectorAll('.hs-turn')[1].querySelectorAll('.hs-cmp').length,1);
assert.equal(document.querySelector('.hs-cmp').previousSibling.textContent,'/compact');
reset();add('text','回复','p1');hsNewTurn(v,'/compact','u2');
v.shepherdMode=true;v.manualCompact=true;
hsApply(v,'session.compact',{running:true,trigger:'manual'});
hsApply(v,'session.compact',{running:true,trigger:'auto',messageID:'native'});
hsApply(v,'session.compact',{running:false,trigger:'auto',result:'success',messageID:'native'});
assert.equal(document.querySelectorAll('.hs-cmp').length,0);
hsApply(v,'session.compact',{running:false,trigger:'manual',result:'success'});
assert.equal(document.querySelectorAll('.hs-cmp').length,1);
assert.equal(document.querySelector('.hs-cmp').textContent,'上下文已压缩 · 手动');
assert.equal(document.querySelector('.hs-cmp').previousSibling.textContent,'/compact');
reset();add('tool','之前','p');
hsApply(v,'session.compact',{running:true});
hsApply(v,'session.compact',{running:false,result:'failed'});
assert.equal(document.querySelectorAll('.hs-cmp').length,0);
reset();hsPour(v,[{info:{id:'a',role:'assistant'},parts:[
  {id:'p1',type:'reasoning',text:'之前'},{id:'c',type:'compact',compact:{},text:''},
  {id:'p2',type:'reasoning',text:'之后'}]}]);
assert.equal(document.querySelector('.hs-cmp').previousSibling.textContent,'之前');
console.log('DOM boundaries, folding, repair, repeated events and failures passed');
'''
        runner = r'''
const {JSDOM}=require(process.env.DESK_JSDOM_PATH);
const vm=require('node:vm'),fs=require('node:fs');
const fixture=JSON.parse(fs.readFileSync(0,'utf8'));
const dom=new JSDOM(fixture.html,{runScripts:'outside-only'});
const ctx=dom.getInternalVMContext();ctx.require=require;ctx.console=console;
vm.runInContext(fixture.script+'\n'+fixture.checks,ctx);
'''
        result = subprocess.run(["node", "-e", runner], input=json.dumps({
            "html": self.page.html, "script": self.page.script, "checks": checks}),
            text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
