"""回复操作行的时间取成文时刻，并标注由哪个引擎生成。"""
import json
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
    from engine_adapters.shepherd import ShepherdAdapter  # noqa: E402
finally:
    sys.argv = _argv

from test_frontend_recall import RecallUI  # noqa: E402


class Bus(ea.EventBus):
    def __init__(self):
        super().__init__()
        self.events = []

    def publish(self, event):
        self.events.append(event)


class FinishTurn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus()

    def tearDown(self):
        self.tmp.cleanup()

    def test_收尾消息与收尾帧带成文时刻和引擎(self):
        ad = ea.PiAdapter(self.tmp.name, self.tmp.name, self.bus)
        sid = ad.create_session()["id"]
        ctx = {"sid": sid, "seq": 3, "msg_id": "msg_a_1000", "t0": 1000, "source": ad.SRC_USER,
               "parts": {"t": {"id": "t", "type": "text", "text": "答复"}}, "order": ["t"]}
        ad._live[sid] = ctx
        ad._finish_turn(sid, ctx, None)
        info = ad.messages(sid)[-1]["info"]
        idle = [e["properties"] for e in self.bus.events if e["type"] == "session.idle"][-1]
        self.assertGreater(info["done_at"], 1000)     # 不再是 id 里的起点时间
        self.assertNotIn("at", info)                  # at 是按时刻切的判据,不借用
        self.assertEqual(info["engine"], "pi")
        self.assertEqual((idle["at"], idle["engine"]), (info["done_at"], "pi"))

    def test_收尾消息与idle帧持久化本回合实际模型(self):
        ad = ea.PiAdapter(self.tmp.name, self.tmp.name, self.bus)
        sid = ad.create_session()["id"]
        ctx = {"sid":sid, "seq":4, "msg_id":"msg_a_2000", "t0":2000,
               "source":ad.SRC_USER, "actual_model":"example-gw/GPT-5.6-Sol",
               "parts":{"t":{"id":"t", "type":"text", "text":"答复"}}, "order":["t"]}
        ad._live[sid] = ctx
        ad._finish_turn(sid, ctx, None)
        info = ad.messages(sid)[-1]["info"]
        idle = [e["properties"] for e in self.bus.events if e["type"] == "session.idle"][-1]
        self.assertEqual(info["model"], "example-gw/GPT-5.6-Sol")
        self.assertEqual(idle["model"], info["model"])

    def test_收尾消息与idle帧持久化token与缓存统计(self):
        ad = ea.PiAdapter(self.tmp.name, self.tmp.name, self.bus)
        sid = ad.create_session()["id"]
        ctx = {"sid":sid, "seq":6, "msg_id":"msg_a_2500", "t0":2500,
               "source":ad.SRC_USER, "_pi_tokens":77700, "_pi_cache_read":28160,
               "parts":{"t":{"id":"t", "type":"text", "text":"答复"}}, "order":["t"]}
        ad._live[sid] = ctx
        ad._finish_turn(sid, ctx, None)
        info = ad.messages(sid)[-1]["info"]
        idle = [e["properties"] for e in self.bus.events if e["type"] == "session.idle"][-1]
        self.assertEqual(info["usage"], {"tokens": 77700, "cached": 28160})
        self.assertEqual(idle["usage"], info["usage"])
        self.assertNotIn("答复", json.dumps(info["usage"], ensure_ascii=False))

    def test_未自报模型时不拿会话最近模型冒充本回合(self):
        ad = ea.PiAdapter(self.tmp.name, self.tmp.name, self.bus)
        sid = ad.create_session()["id"]
        ad.set_session_model(sid, "old/model")
        ctx = {"sid":sid, "seq":5, "msg_id":"msg_a_3000", "t0":3000,
               "source":ad.SRC_USER,
               "parts":{"t":{"id":"t", "type":"text", "text":"答复"}}, "order":["t"]}
        ad._live[sid] = ctx
        ad._finish_turn(sid, ctx, None)
        self.assertNotIn("model", ad.messages(sid)[-1]["info"])

    def test_Codex正常选中与重路由都更新本回合模型(self):
        ad = ea.CodexAdapter(self.tmp.name, self.tmp.name, self.bus)
        sid = ad.create_session()["id"]
        ctx = {"sid": sid, "t0": 1000}
        ad._app_event("model/selected", {"model": "gpt-5.6-sol"}, ctx)
        self.assertEqual(ctx["actual_model"], "gpt-5.6-sol")
        ad._app_event("model/rerouted", {"toModel": "gpt-6-astra"}, ctx)
        self.assertEqual(ctx["actual_model"], "gpt-6-astra")

    def test_Shepherd按本回合Producer标注而非下一轮选择(self):
        ad = object.__new__(ShepherdAdapter)
        ad.session_engine = lambda _sid: "claude"
        self.assertEqual(ad._turn_engine("s", {"producer_engine": "pi"}), "pi")
        self.assertEqual(ad._turn_engine("s", {}), "claude")   # 派发前取消:退回当前绑定
        ad.session_engine = lambda _sid: ""
        self.assertEqual(ad._turn_engine("s", {}), "")


class PiHistory(unittest.TestCase):
    def test_回填时间取本回合最后一条助手记录(self):
        with tempfile.TemporaryDirectory() as tmp:
            ad = ea.PiAdapter(tmp, tmp, Bus())
            rows = [
                {"type": "message", "id": "u", "message": {"role": "user", "timestamp": 100, "content": "问"}},
                {"type": "message", "id": "a1", "message": {"role": "assistant", "timestamp": 105,
                 "stopReason": "toolUse", "content": [{"type": "text", "text": "查一下"}]}},
                {"type": "message", "id": "a2", "message": {"role": "assistant", "timestamp": 900,
                 "stopReason": "stop", "content": [{"type": "text", "text": "结论"}]}},
            ]
            path = os.path.join(tmp, "pi.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(json.dumps(r) + "\n" for r in rows)
            info = ad._pi_group(path, "adp_test", sink={})[-1]["info"]
            self.assertEqual((info["at"], info["done_at"]), (105, 900))


class FootUI(unittest.TestCase):
    run_js = RecallUI.run_js

    def test_正文晚于操作行到达时仍插在操作行之前(self):
        self.run_js([('app-chat-detail.js', 'hsTextAttach')], r'''
const assert=require('node:assert/strict');
function holder() {
  return {children:[], insertBefore(el, before) {
    const old=this.children.indexOf(el); if(old>=0)this.children.splice(old,1);
    const at=before ? this.children.indexOf(before) : -1;
    if(at>=0)this.children.splice(at,0,el);else this.children.push(el);
    el.parentNode=this;
  }};
}
const h=holder(), foot={name:'foot'}, text={name:'text'};
h.insertBefore(foot,null);
const t={holder:h,footEl:foot};
hsTextAttach(t,text);
assert.deepEqual(h.children.map(x=>x.name),['text','foot']);
// 重建后重新挂载也保持同一顺序
h.insertBefore(text,null);
hsTextAttach(t,text);
assert.deepEqual(h.children.map(x=>x.name),['text','foot']);
''')
        src = open(os.path.join(ROOT, "frontend", "app-chat-detail.js"), encoding="utf-8").read()
        self.assertEqual(src.count("hsTextAttach(part.turn, part.el)"), 2)

    def test_生成模型优先于Harness标签(self):
        self.run_js([('app-chat-refs.js', f) for f in ('hsCompactCount', 'hsUsageLabel', 'hsGenModelLabel', 'hsGenLabel')], r'''
const assert=require('node:assert/strict');
const HS_GEN_NAMES={claude:"Claude",codex:"Codex",pi:"Pi"};
function hsRuntimeModelLabel(_v,id){return id==='example-gw/gpt-5.6-sol'?'GPT-5.6-Sol':id}
function hsPrettyModel(){return ''}
function hsCollapsedModelLabel(x){return x}
assert.equal(hsGenLabel({shepherdMode:true,engine:'claude'},{engine:'pi',model:'example-gw/gpt-5.6-sol'}),'GPT-5.6-Sol');
assert.equal(hsGenLabel({shepherdMode:true,engine:'claude'},{engine:'pi',model:'private/internal-route'}),'Pi');
assert.equal(hsGenLabel({shepherdMode:true,engine:'claude'},{engine:'pi'}),'Pi');
// Shepherd 旧回合没记引擎:不拿当前选择去猜
assert.equal(hsGenLabel({shepherdMode:true,engine:'claude'},{}),'');
assert.equal(hsGenLabel({shepherdMode:false,engine:'codex'},{}),'Codex');
// /config 未回来之前不知道普通会话的引擎
assert.equal(hsGenLabel({engine:'claude'},{}),'');
assert.equal(hsGenLabel({shepherdMode:false},{engine:'shepherd'}),'');
''')

    def test_生成标签展示紧凑Tokens与Cached(self):
        self.run_js([('app-chat-refs.js', f) for f in ('hsCompactCount', 'hsUsageLabel', 'hsGenModelLabel', 'hsGenLabel')], r'''
const assert=require('node:assert/strict');
const HS_GEN_NAMES={claude:"Claude",codex:"Codex",pi:"Pi"};
function hsRuntimeModelLabel(_v,id){return id==='example-gw/gpt-5.6-sol'?'GPT-5.6-Sol':id}
function hsPrettyModel(){return ''}
function hsCollapsedModelLabel(x){return x}
assert.equal(hsCompactCount(777), '777');
assert.equal(hsCompactCount(77700), '77.7k');
assert.equal(hsCompactCount(77700000), '77.7m');
assert.equal(hsGenLabel({shepherdMode:true,engine:'claude'},
  {engine:'pi',model:'example-gw/gpt-5.6-sol',usage:{tokens:77700,cached:28160}}),
  'tokens 77.7k cached 28.2k GPT-5.6-Sol');
assert.equal(hsGenLabel({shepherdMode:true,engine:'claude'},
  {engine:'pi',usage:{tokens:42,cached:0}}), 'tokens 42 cached 0 Pi');
assert.equal(hsGenLabel({shepherdMode:true,engine:'claude'},
  {usage:{tokens:1200000,cached:700000}}), 'tokens 1.2m cached 700k');
assert.equal(hsGenLabel({shepherdMode:true,engine:'claude'},
  {engine:'pi',usage:{tokens:42}}), 'tokens 42 Pi');
''')

        css = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
        foot = re.search(r"\.hs-page \.hs-foot \{([^}]*)\}", css, re.S).group(1)
        time = re.search(r"\.hs-page \.hs-time \{([^}]*)\}", css, re.S).group(1)
        gen = re.search(r"\.hs-page \.hs-gen \{([^}]*)\}", css, re.S).group(1)
        self.assertIn("flex-wrap: nowrap", foot)
        self.assertIn("white-space: nowrap", time)
        self.assertIn("white-space: nowrap", gen)
        model = re.search(r"\.hs-page \.hs-gen-model \{([^}]*)\}", css, re.S).group(1)
        usage = re.search(r"\.hs-page \.hs-gen-usage \{([^}]*)\}", css, re.S).group(1)
        self.assertIn("display: inline-flex", gen)
        self.assertIn("flex: 0 0 auto", usage)
        self.assertIn("text-overflow: ellipsis", model)

    def test_短格式悬停仍解释缓存与总量(self):
        self.run_js([('app-chat-refs.js', f) for f in
                     ('hsCompactCount', 'hsUsageLabel', 'hsGenModelLabel', 'hsGenLabel', 'hsGenPaint')], r'''
const assert=require('node:assert/strict');
const HS_GEN_NAMES={claude:"Claude",codex:"Codex",pi:"Pi"};
function hsRuntimeModelLabel(_v,id){return id==='example-gw/gpt-5.6-sol'?'GPT-5.6-Sol':id}
function hsPrettyModel(){return ''}
function hsCollapsedModelLabel(x){return x}
function hsUiText(el,text){el.textContent=text}
function hsUiHidden(el,hidden){el.hidden=hidden}
const usageEl={};
const modelEl={};
const el={querySelector:(sel)=>sel==='.hs-gen-usage'?usageEl:modelEl};
const t={engine:'pi',model:'example-gw/gpt-5.6-sol',usage:{tokens:66100,cached:55100},
         footEl:{querySelector:()=>el}};
hsGenPaint({shepherdMode:true,engine:'claude'},t);
assert.equal(usageEl.textContent,'tokens 66.1k cached 55.1k');
assert.equal(modelEl.textContent,'GPT-5.6-Sol');
assert.equal(el.title,'Tokens 66.1k / Cached 55.1k / GPT-5.6-Sol');
assert.equal(el.hidden,false);
''')

    def test_实时与历史都把模型写入回合对象(self):
        core = open(os.path.join(ROOT, "frontend", "app-chat-core.js"), encoding="utf-8").read()
        replay = open(os.path.join(ROOT, "frontend", "app-chat-reconcile.js"), encoding="utf-8").read()
        runners = open(os.path.join(ROOT, "desktron", "lib", "shepherd", "runners.py"), encoding="utf-8").read()
        unified = open(os.path.join(ROOT, "desktron", "lib", "shepherd", "unified_runners.py"), encoding="utf-8").read()
        self.assertIn("if (p.model) tail.model = p.model", core)
        self.assertIn("if (p.usage) tail.usage = p.usage", core)
        self.assertIn("if (info.model) t.model = info.model", replay)
        self.assertIn("if (info.usage) t.usage = info.usage", replay)
        self.assertIn("on_model_selected=lambda model:event('model/selected'", runners)
        self.assertIn("on_model_selected=lambda model:event('model/selected'", unified)

    def test_收尾帧改写已建操作行的时间与引擎(self):
        src = open(os.path.join(ROOT, "frontend", "app-chat-core.js"), encoding="utf-8").read()
        body = src[src.index("function hsTurnSettled"):]
        body = body[:body.index("\n}\n")]
        fin = body.index("hsFinalize(v);")
        self.assertLess(fin, body.index("tail.at = p.at"))
        self.assertIn('querySelector(".hs-time")', body)
        self.assertIn("hsGenPaint(v, tail)", body)


if __name__ == "__main__":
    unittest.main()
