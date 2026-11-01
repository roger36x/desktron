"""模型家族与 Standard/Max 规格在生产前端中的选择行为。"""
import json
from pathlib import Path
import subprocess
import unittest

from frontend_i18n_js import NODE_PRELUDE

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "frontend" / "app-chat-opts.js").read_text(encoding="utf-8")


class ModelVariants(unittest.TestCase):
    def run_case(self, body):
        harness = NODE_PRELUDE + r'''
const fs = require("fs");
eval(fs.readFileSync(process.argv[1], "utf8"));
const productionSetOpt = hsSetOpt, productionRefresh = hsRefreshDynamicModels;
hsRefreshDynamicModels = async () => true; // 大部分用例仅检查选项，与网络无关
let shown = [], picked = null, currentMenu = null;
openCtxMenu = (_x, _y, items, cls) => {
  if (currentMenu) currentMenu.open = false;
  const menu = {items, cls, open:true, updates:0,
    isOpen(){return this.open;},
    setLabels(labels){this.updates++;for(const it of items)if(labels.has(it.id))it.label=labels.get(it.id);}};
  shown.push(menu); currentMenu=menu; return menu;
};
hsHidden = v => !!v.hidden;
hsMenuAnchor = () => {};
hsSetOpt = (v, patch) => { picked = patch; v.opts = Object.assign({}, v.opts || {}, patch); };
alert = () => {};
const anchor = {isConnected: true, getBoundingClientRect: () => ({left: 300, top: 500})};
const effort = (id) => ({id, label: id.toUpperCase()});
const route = (family, key, efforts, defaultEffort) => ({
  id: `example-gw/${family}:${family}__${key === "standard" ? "dev" : "max"}`,
  key, label: key === "standard" ? "Standard" : "Max",
  efforts: efforts.map(effort), defaultEffort,
});
const family = (name, variants) => ({id: `example-gw/${name}:${name}`, label: name,
  defaultVariant: "standard", variants});
''' + "\n(async () => {\n" + body + "\n})().catch((e) => { console.error(e); process.exitCode = 1; });\n"
        out = subprocess.run(["node", "-e", harness, str(ROOT / "frontend" / "app-chat-opts.js")],
                             capture_output=True, text=True, timeout=20, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_exact_max_route_resolves_to_one_family(self):
        got = self.run_case(r'''
const sol = family("Sol", [route("sol", "standard", ["medium", "high"], "medium"),
                            route("sol", "max", ["high", "xhigh"], "high")]);
const v = {engine: "pi", catalog: {models: [sol]}, opts: {model: sol.variants[1].id}};
console.log(JSON.stringify({model: hsModel(v).label, variant: hsModelRoute(v).key,
  variants: hsVariants(v).length, runtime: hsRuntimeModelLabel(v, sol.variants[1].id)}));
''')
        self.assertEqual(got, {"model": "Sol", "variant": "max", "variants": 2,
                               "runtime": "Sol"})

    def test_new_session_configured_model_beats_catalog_default_before_first_turn(self):
        got = self.run_case(r'''
const deepseek = {id:"deepseek-v4-pro", label:"DeepSeek V4 Pro", default:true, variants:[]};
const gptRoute = {id:"gpt-5.6-sol-max", key:"max", label:"Max", efforts:[effort("xhigh")]};
const gpt = {id:"gpt-5.6-sol", label:"GPT-5.6-Sol", default:false,
  defaultVariant:"max", variants:[gptRoute]};
const v = {engine:"pi", catalog:{models:[deepseek,gpt]},
  opts:{model:gptRoute.id}, realModel:""};
await hsPickModel(v, anchor);
console.log(JSON.stringify({model:hsModel(v).label, route:hsModelRoute(v).id,
  checked:shown[0].items.filter(x=>x.checked).map(x=>x.label)}));
''')
        self.assertEqual(got,{"model":"GPT-5.6-Sol","route":"gpt-5.6-sol-max",
                              "checked":["GPT-5.6-Sol"]})

    def test_native_model_change_overrides_stale_saved_route(self):
        got = self.run_case(r'''
const sol = family("Sol", [route("sol", "standard", ["high"], "high"), route("sol", "max", ["high"], "high")]);
const terra = family("Terra", [route("terra", "standard", ["high"], "high"), route("terra", "max", ["high"], "high")]);
const v = {engine: "pi", catalog: {models: [sol, terra]},
  opts: {model: sol.variants[0].id}, realModel: terra.variants[1].id};
console.log(JSON.stringify({model: hsModel(v).label, variant: hsModelRoute(v).key,
  runtime: hsRuntimeModelLabel(v, v.realModel)}));
''')
        self.assertEqual(got, {"model": "Terra", "variant": "max", "runtime": "Terra"})

    def test_switch_family_keeps_max_when_supported(self):
        got = self.run_case(r'''
const sol = family("Sol", [route("sol", "standard", ["high"], "high"), route("sol", "max", ["high"], "high")]);
const terra = family("Terra", [route("terra", "standard", ["high"], "high"), route("terra", "max", ["high"], "high")]);
const v = {engine: "pi", catalog: {models: [sol, terra]}, opts: {model: sol.variants[1].id, effort: "high"}};
await hsPickModel(v, anchor);
shown[0].items.find(x => x.label === "Terra").fn();
console.log(JSON.stringify(picked));
''')
        self.assertEqual(got, {"model": "example-gw/terra:terra__max"})

    def test_switch_family_falls_back_to_only_standard_route(self):
        got = self.run_case(r'''
const sol = family("Sol", [route("sol", "standard", ["high"], "high"), route("sol", "max", ["high"], "high")]);
const seed = family("Seed", [route("seed", "standard", ["medium"], "medium")]);
const v = {engine: "pi", catalog: {models: [sol, seed]}, opts: {model: sol.variants[1].id, effort: "high"}};
await hsPickModel(v, anchor);
shown[0].items.find(x => x.label === "Seed").fn();
console.log(JSON.stringify(picked));
''')
        self.assertEqual(got, {"model": "example-gw/seed:seed__dev", "effort": "medium"})

    def test_model_patch_clears_old_runtime_route_before_persistence(self):
        got = self.run_case(r'''
const sol = family("Sol", [route("sol", "standard", ["high"], "high"), route("sol", "max", ["high"], "high")]);
const v = {engine: "pi", catalog: {models: [sol]}, opts: {model: sol.variants[0].id},
  realModel: sol.variants[0].id, ephemeral: true};
api = async () => ({}); hsEngineSync = async () => {};
const pending = productionSetOpt(v, {model: sol.variants[1].id});
const immediate = hsModelRoute(v).key;
await pending;
console.log(JSON.stringify({immediate, realModel: v.realModel}));
''')
        self.assertEqual(got, {"immediate": "max", "realModel": ""})

    def test_variant_row_and_effort_fallback(self):
        got = self.run_case(r'''
const sol = family("Sol", [route("sol", "standard", ["medium", "high"], "medium"),
                            route("sol", "max", ["xhigh"], "xhigh")]);
const v = {engine: "pi", catalog: {models: [sol]}, opts: {model: sol.variants[1].id, effort: "xhigh"}};
await hsPickModel(v, anchor);
const spec = shown[0].items.find(x => x.label === "规格");
spec.fn();
shown[1].items.find(x => x.label === "Standard").fn();
console.log(JSON.stringify({value: spec.value, patch: picked}));
''')
        self.assertEqual(got, {"value": "Max", "patch": {
            "model": "example-gw/sol:sol__dev", "effort": "medium"}})

    def test_single_route_hides_variant_row(self):
        got = self.run_case(r'''
const seed = family("Seed", [route("seed", "standard", ["high"], "high")]);
const v = {engine: "pi", catalog: {models: [seed]}, opts: {model: seed.variants[0].id, effort: "high"}};
await hsPickModel(v, anchor);
console.log(JSON.stringify(shown[0].items.map(x => x.label)));
''')
        self.assertNotIn("规格", got)

    def test_variant_uses_separate_option_without_flattening_model(self):
        got = self.run_case(r'''
const sol = family("Sol", [route("sol", "standard", ["medium"], "medium"),
                            route("sol", "max", ["xhigh"], "xhigh")]);
sol.id = "GPT-5.6-Sol"; sol.variantOption = "model_variant";
const v = {engine: "pi", catalog: {models: [sol]},
  opts: {model: sol.id, model_variant: sol.variants[0].id, effort: "medium"}};
await hsPickModel(v, anchor);
shown[0].items.find(x => x.label === "规格").fn();
shown[1].items.find(x => x.label === "Max").fn();
console.log(JSON.stringify(picked));
''')
        self.assertEqual(got, {"model": "GPT-5.6-Sol",
                               "model_variant": "example-gw/sol:sol__max",
                               "effort": "xhigh"})

    def test_collapsed_model_label_hides_runtime_status_only(self):
        got = self.run_case(r'''
console.log(JSON.stringify({
  complete: hsCollapsedModelLabel("GPT-5.6-Sol · Max · 负载 112% · 额度 80% · 状态待刷新"),
  loadOnly: hsCollapsedModelLabel("Seed-2.1 · 负载 8%"),
  ordinary: hsCollapsedModelLabel("负载均衡专家 · Standard"),
}));
''')
        self.assertEqual(got, {
            "complete": "GPT-5.6-Sol · Max",
            "loadOnly": "Seed-2.1",
            "ordinary": "负载均衡专家 · Standard",
        })

    def test_collapsed_button_renders_model_without_runtime_status(self):
        got = self.run_case(r'''
const label = "GPT-5.6-Sol · Max · 负载 112% · 额度 80% · 状态待刷新";
function render(shepherdMode) {
  const m1 = {textContent: ""};
  const model = {id: "GPT-5.6-Sol", label, variants: []};
  const v = {engine: "pi", shepherdMode,
    engineSnap: {engines: {pi: {label: "Pi"}}},
    catalog: {models: [model]}, opts: {model: model.id}, sessionOpts: {model: model.id},
    el: {querySelector: (selector) => selector === ".hs-m1" ? m1 : null}};
  hsOptsPaint(v);
  return m1.textContent;
}
console.log(JSON.stringify({regular: render(false), shepherd: render(true)}));
''')
        self.assertEqual(got, {"regular": "GPT-5.6-Sol · Max",
                               "shepherd": "Pi · GPT-5.6-Sol · Max"})

    def test_concurrent_clicks_share_one_request(self):
        got = self.run_case(r'''
hsRefreshDynamicModels=productionRefresh;let calls=0,resolve;
api=()=>{calls++;return new Promise(r=>resolve=r);};
const m={id:'example-gw/test',label:'cached'};
const v={sid:'one',engine:'pi',catalog:{models:[]},opts:{}};
const a=hsPickModel(v,anchor),b=hsPickModel(v,anchor);
resolve({engine:'pi',options:{models:[{...m,label:'fresh'}]}});await Promise.all([a,b]);
console.log(JSON.stringify({calls,label:shown[shown.length-1].items[0].label}));
''')
        self.assertEqual(got, {'calls':1,'label':'fresh'})

    def test_closed_or_replaced_or_hidden_menu_is_not_reopened(self):
        got = self.run_case(r'''
hsRefreshDynamicModels=productionRefresh;const out=[];
for(const mode of ['closed','submenu','hidden','detached']){
  let resolve;api=()=>new Promise(r=>resolve=r);
  const m={id:'example-gw/test',label:'cached'};
  const v={sid:'one',engine:'pi',catalog:{models:[]},opts:{}};
  const pending=hsPickModel(v,anchor),menu=currentMenu;
  if(mode==='closed')menu.open=false;
  if(mode==='submenu')openCtxMenu(0,0,[{label:'Effort'}],'sub');
  if(mode==='hidden'){v.el={};v.hidden=true;}
  if(mode==='detached')anchor.isConnected=false;
  const count=shown.length;
  resolve({engine:'pi',options:{models:[{...m,label:'fresh'}]}});await pending;
  out.push(menu.updates===0 && shown.length===count);anchor.isConnected=true;
}
console.log(JSON.stringify(out));
''')
        self.assertEqual(got, [True]*4)

    def test_late_catalog_cannot_cross_session_or_shepherd_engine_epoch(self):
        got = self.run_case(r'''
hsRefreshDynamicModels=productionRefresh;const out=[];
for(const mode of ['session','engine','epoch']){
  let resolve;api=()=>new Promise(r=>resolve=r);
  const old={models:[]};
  const v={sid:'one',engine:'pi',shepherdPickEpoch:1,catalog:old,opts:{}};
  const pending=hsPickModel(v,anchor),menu=currentMenu;
  if(mode==='session')v.sid='two';
  if(mode==='engine')v.engine='codex';
  if(mode==='epoch')v.shepherdPickEpoch=3; // Pi → 别的引擎 → Pi
  resolve({engine:'pi',options:{models:[{id:'wrong',label:'wrong'}]}});await pending;
  out.push(v.catalog===old && menu.updates===0);
}
console.log(JSON.stringify(out));
''')
        self.assertEqual(got,[True]*3)

    def test_first_empty_menu_has_loading_then_failure_retry_then_models(self):
        got = self.run_case(r'''
hsRefreshDynamicModels=productionRefresh;const replies=[];
api=()=>new Promise((resolve,reject)=>replies.push({resolve,reject}));
const v={sid:'one',engine:'pi',catalog:{models:[]},opts:{}};
const a=hsPickModel(v,anchor);
const loading={label:shown[0].items[0].label,disabled:shown[0].items[0].disabled};
replies.shift().reject(new Error('network'));await a;
const failed=shown[1].items[0].label;
const b=shown[1].items[0].fn();
replies.shift().resolve({engine:'pi',options:{models:[{id:'gpt',label:'GPT'}]}});await b;
console.log(JSON.stringify({loading,failed,last:currentMenu.items[0].label,requests:v.modelCatalogRequest}));
''')
        self.assertEqual(got,{'loading':{'label':'正在加载模型…','disabled':True},
                              'failed':'加载失败，点击重试','last':'GPT','requests':None})

    def test_static_menu_does_not_add_refresh_request(self):
        got = self.run_case(r'''
let calls = 0; api = async () => { calls++; return {}; };
const v = {engine: "codex", catalog: {models: [{id: "gpt", label: "GPT", variants: []}]},
  opts: {model: "gpt"}};
await hsPickModel(v, anchor);
console.log(JSON.stringify({calls, shown: shown.length}));
''')
        self.assertEqual(got, {"calls": 0, "shown": 1})

    def test_model_switch_inherits_max_variant(self):
        got = self.run_case(r'''
const sol = family("Sol", [route("sol", "standard", ["high"], "high"),
                            route("sol", "max", ["high"], "high")]);
sol.id = "GPT-5.6-Sol"; sol.variantOption = "model_variant";
const terra = family("Terra", [route("terra", "standard", ["high"], "high"),
                                route("terra", "max", ["high"], "high")]);
terra.id = "GPT-5.6-Terra"; terra.variantOption = "model_variant";
const v = {engine: "pi", catalog: {models: [sol, terra]},
  opts: {model: sol.id, model_variant: sol.variants[1].id, effort: "high"}};
await hsPickModel(v, anchor);
shown[0].items.find(x => x.label === "Terra").fn();
console.log(JSON.stringify(picked));
''')
        self.assertEqual(got, {"model": "GPT-5.6-Terra",
                               "model_variant": "example-gw/terra:terra__max"})


if __name__ == "__main__":
    unittest.main()
