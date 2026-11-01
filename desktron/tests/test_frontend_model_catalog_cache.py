"""模型目录按引擎共享并持久化：切到本视图未加载过的引擎时，菜单立即可用。"""
import os
import re
import subprocess
import unittest

from frontend_i18n_js import NODE_PRELUDE

FRONT = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
OPTS = open(os.path.join(FRONT, "app-chat-opts.js"), encoding="utf-8").read()


def _fn(name):
    return re.search(r"function %s\(.*?\n}\n" % name, OPTS, re.S).group()


class ModelCatalogCache(unittest.TestCase):
    def run_node(self, body):
        store = re.search(r'const HS_CATALOG_STORE = .*?;\n', OPTS).group()
        script = "\n".join([
            "const assert=require('node:assert/strict');",
            "const mem={}; globalThis.localStorage={getItem:k=>k in mem?mem[k]:null,"
            "setItem:(k,v)=>{mem[k]=String(v);}};",
            NODE_PRELUDE,
            "function hsOptsPaint(){} function hsFlushShepherdEngine(){}",
            store, _fn("hsOptLabel"), _fn("hsCatalogRemember"), _fn("hsCatalogRecall"),
            _fn("hsSwitchShepherdEngine"), _fn("hsRefreshDynamicModels"),
            "async " + _fn("hsPickModel"),
            "function hsModels(v){return v.catalog?.models || [];}",
            "let menus=[]; function hsShowModelMenu(v,anchor,label){"
            "if(menus.length) menus.at(-1).open=false;"
            "const menu={ids:hsModels(v).map(m=>m.id),label,open:true,"
            "isOpen(){return this.open},setLabels(labels){this.labels=labels}};"
            "menus.push(menu);return menu;}",
            "let api; function hsHidden(){return false;}",
            "(async()=>{" + body + "})().catch(e=>{console.error(e);process.exitCode=1;});"])
        subprocess.run(["node", "-e", script], check=True)

    def test_switch_uses_catalog_remembered_by_another_session(self):
        self.run_node("""
hsCatalogRemember('pi',{models:[{id:'deepseek/v4',label:'DeepSeek'}]});
const v={shepherdMode:true,engine:'codex',opts:{},catalog:{models:[{id:'gpt'}]}};
hsSwitchShepherdEngine(v,'pi');
assert.equal(v.catalog.models[0].id,'deepseek/v4');
""")

    def test_pi_shared_empty_catalog_replaces_view_cache(self):
        self.run_node("""
hsCatalogRemember('pi',{models:[{id:'shared'}]});
hsCatalogRemember('pi',{models:[]});
assert.deepEqual(hsCatalogRecall('pi').models,[]);
const fresh={shepherdMode:true,engine:'codex',opts:{}};
hsSwitchShepherdEngine(fresh,'pi');
assert.deepEqual(fresh.catalog.models,[]);
const v={shepherdMode:true,engine:'pi',opts:{},catalog:{models:[{id:'own'}]}};
hsSwitchShepherdEngine(v,'codex'); hsSwitchShepherdEngine(v,'pi');
assert.deepEqual(v.catalog.models,[]);
assert.equal(hsCatalogRecall('unknown'),null);
""")

    def test_switch_back_uses_shared_empty_even_if_config_fails(self):
        self.run_node("""
const v={shepherdMode:true,engine:'pi',sid:'one',opts:{model:'openai/old'},
  catalog:{models:[{id:'openai/old',label:'Old'}]}};
hsSwitchShepherdEngine(v,'codex');
hsCatalogRemember('pi',{models:[]});
hsSwitchShepherdEngine(v,'pi');
api=async()=>{throw new Error('offline')};
await hsPickModel(v,{isConnected:true});
assert.deepEqual(v.catalog.models,[]);
assert(menus.every(m=>!m.ids.includes('openai/old')));
assert.equal(v.sessionOpts.model,'openai/old');
assert.equal(v.modelCatalogRequest,null);
""")

    def test_open_menu_uses_new_shared_result_without_switching_engine(self):
        self.run_node("""
const v={engine:'pi',sid:'one',catalog:{models:[{id:'openai/old'}]}};
hsCatalogRemember('pi',{models:[{id:'deepseek/v4',label:'DeepSeek'}]});
api=async()=>{throw new Error('offline')};
await hsPickModel(v,{isConnected:true});
assert(menus.every(m=>JSON.stringify(m.ids)==='["deepseek/v4"]'));
""")

    def test_partial_cold_catalog_recovers_openai_on_next_open(self):
        self.run_node("""
const partial={models:[{id:'deepseek/v4',label:'DeepSeek'}]};
const complete={models:[...partial.models,{id:'openai/visible',label:'Visible'}]};
const v={engine:'pi',sid:'one',catalog:partial};
const replies=[partial,complete];let calls=0;
api=async()=>({engine:'pi',options:replies[calls++]});
await hsPickModel(v,{isConnected:true});
assert.equal(calls,1);assert.deepEqual(menus.at(-1).ids,['deepseek/v4']);
await hsPickModel(v,{isConnected:true});
assert.equal(calls,2);
assert.deepEqual(menus.at(-1).ids,['deepseek/v4','openai/visible']);
assert.deepEqual(hsCatalogRecall('pi').models,complete.models);
""")

    def test_ephemeral_pi_refresh_names_engine_when_global_pref_is_different(self):
        self.run_node("""
const partial={models:[{id:'deepseek/v4',label:'DeepSeek'}]};
const complete={models:[...partial.models,{id:'openai/visible',label:'Visible'}]};
const v={engine:'pi',ephemeral:true,catalog:partial};
const paths=[]; let ready=false;
api=async path=>{
  paths.push(path);
  // The global preference is Codex: a bare /config must not be used.
  if(path==='/config') return {engine:'codex',options:{models:[{id:'codex/current'}]}};
  assert.equal(path,'/config?engine=pi');
  return {engine:'pi',options:ready ? complete : partial};
};
await hsPickModel(v,{isConnected:true});
assert.deepEqual(menus.at(-1).ids,['deepseek/v4']);
ready=true;
await hsPickModel(v,{isConnected:true});
assert.deepEqual(menus.at(-1).ids,['deepseek/v4','openai/visible']);
assert.deepEqual(paths,['/config?engine=pi','/config?engine=pi']);
assert.deepEqual(hsCatalogRecall('pi').models,complete.models);
""")

    def test_refreshed_removal_clears_open_menu(self):
        self.run_node("""
const v={engine:'pi',sid:'one',catalog:{models:[{id:'openai/old',label:'Old'}]}};
api=async()=>({engine:'pi',options:{models:[]}});
await hsPickModel(v,{isConnected:true});
assert.deepEqual(menus.at(-1).ids,[]);
assert.equal(menus[0].open,false);
assert.deepEqual(hsCatalogRecall('pi').models,[]);
""")

    def test_same_ids_update_labels_without_rebuilding(self):
        self.run_node("""
const v={engine:'pi',sid:'one',catalog:{models:[{id:'openai/visible',label:'Before'}]}};
api=async()=>({engine:'pi',options:{models:[{id:'openai/visible',label:'After'}]}});
await hsPickModel(v,{isConnected:true});
assert.equal(menus.length,1);
assert.equal(menus[0].labels.get('openai/visible'),'After');
""")

    def test_late_response_does_not_replace_closed_menu_or_submenu(self):
        self.run_node("""
const v={engine:'pi',sid:'one',catalog:{models:[{id:'openai/old',label:'Old'}]}};
let resolve;api=()=>new Promise(r=>resolve=r);
const pending=hsPickModel(v,{isConnected:true});
menus[0].open=false;
resolve({engine:'pi',options:{models:[{id:'openai/new',label:'New'}]}});
await pending;
assert.equal(menus.length,1);
assert.deepEqual(v.catalog.models.map(m=>m.id),['openai/new']);
""")

    def test_overlapping_opens_share_one_request(self):
        self.run_node("""
const v={engine:'pi',sid:'one',catalog:{models:[{id:'deepseek/v4',label:'DeepSeek'}]}};
let resolve,calls=0;api=()=>{calls++;return new Promise(r=>resolve=r)};
const anchor={isConnected:true};
const first=hsPickModel(v,anchor),second=hsPickModel(v,anchor);
assert.equal(calls,1);
resolve({engine:'pi',options:{models:[{id:'deepseek/v4',label:'Fresh'}]}});
await Promise.all([first,second]);
assert.equal(menus.length,2);
assert.equal(menus.at(-1).labels.get('deepseek/v4'),'Fresh');
""")

    def test_response_for_previous_engine_does_not_change_catalog_or_shared_cache(self):
        self.run_node("""
const v={engine:'pi',sid:'one',shepherdPickEpoch:1,catalog:{models:[{id:'openai/old'}]}};
let resolve;api=()=>new Promise(r=>resolve=r);
const pending=hsPickModel(v,{isConnected:true});
v.engine='codex';v.shepherdPickEpoch=2;v.catalog={models:[{id:'codex/current'}]};
resolve({engine:'pi',options:{models:[{id:'openai/new'}]}});
await pending;
assert.deepEqual(v.catalog.models.map(m=>m.id),['codex/current']);
assert.equal(hsCatalogRecall('pi'),null);
assert.equal(menus.length,1);
""")

    def test_other_engines_do_not_refresh_on_menu_open(self):
        self.run_node("""
const v={engine:'codex',catalog:{models:[{id:'visible'}]}};
api=()=>{throw new Error('not needed')};
await hsPickModel(v,{isConnected:true});
assert.deepEqual(menus.at(-1).ids,['visible']);
""")

    def test_old_unfiltered_storage_is_not_reused(self):
        self.run_node("""
localStorage.setItem('desk.modelCatalog.v1',JSON.stringify({pi:{catalog:{models:[{id:'openai/gpt-4'}]}}}));
assert.equal(hsCatalogRecall('pi'),null);
hsCatalogRemember('pi',{models:[{id:'openai/gpt-codex'}]});
assert.equal(hsCatalogRecall('pi').models[0].id,'openai/gpt-codex');
""")

    def test_other_engines_still_keep_their_last_nonempty_catalog(self):
        self.run_node("""
hsCatalogRemember('codex',{models:[{id:'shared'}]});
hsCatalogRemember('codex',{models:[]});
assert.equal(hsCatalogRecall('codex').models[0].id,'shared');
""")

    def test_broken_storage_falls_back_to_empty_catalog(self):
        self.run_node("""
globalThis.localStorage={getItem(){throw new Error('denied')},setItem(){throw new Error('denied')}};
hsCatalogRemember('pi',{models:[{id:'x'}]});
const v={shepherdMode:true,engine:'codex',opts:{}};
hsSwitchShepherdEngine(v,'pi');
assert.deepEqual(v.catalog,{});
""")


if __name__ == "__main__":
    unittest.main()
