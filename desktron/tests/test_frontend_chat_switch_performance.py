"""会话切换的主线程预算。

Codex 的工具步骤密度通常高于 Claude。折叠过程区若仍提前构建每一步详情，或每画一步
都扫描整轮 DOM，接口即使只用几毫秒，页面仍会明显卡住。
"""
import os
import re
import shutil
import subprocess
import unittest

from frontend_i18n_js import NODE_PRELUDE

import _srcmap


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()


def fn(name):
    match = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), CHAT, re.S)
    assert match, name
    return match.group(0)


class ToolStepLazyRendering(unittest.TestCase):
    def test_折叠步骤不提前生成详情(self):
        paint = fn("hsPaintTool")
        self.assertNotIn("const detail = hsStepDetail(part)", paint)
        # 2026-08-24 多了第二个参数(视图),见 test_tool_output_lazy
        self.assertIn("hsStepDetailMount(part, v)", paint)
        self.assertNotIn("hsDiffNumber(part)", paint)
        self.assertNotIn("hsReadPic(part)", paint)

    def test_详情展开时才生成高亮和行号(self):
        mount = fn("hsStepDetailMount")
        self.assertIn("const detail = hsStepDetail(part)", mount)
        self.assertIn("hsDiffNumber(part)", mount)
        self.assertIn("hsReadPic(part)", mount)

    def test_每新增一步不再全量扫描过程区(self):
        paint = fn("hsPaintTool")
        self.assertNotIn('querySelectorAll(".hs-step")', paint)
        self.assertNotIn('querySelectorAll(".hs-step.run")', paint)
        self.assertIn('box.classList.add("has-steps")', paint)

class EngineSwitchDomBudget(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
    def test_重复的切换回读不再写长会话DOM(self):
        """乐观值、SSE、PUT 与 /config 相同回读必须成为真正的 DOM no-op。"""
        script = NODE_PRELUDE + r'''
const fs=require("fs"), assert=require("node:assert/strict");
eval(fs.readFileSync(process.argv[1],"utf8")+"\n"+fs.readFileSync(process.argv[2],"utf8"));
let writes=0, queries=0;
function cell(text="", hidden=false) {
  let value=text, off=hidden;
  return {classList:{on:false,contains(x){return x==="on"&&this.on;},toggle(x,v){
      if(x==="on"&&this.on!==!!v){this.on=!!v;writes++;}}},
    get textContent(){return value;},set textContent(v){value=String(v);writes++;},
    get hidden(){return off;},set hidden(v){off=!!v;writes++;}};
}
const modeText=cell(), mode=cell("",false); mode.querySelector=()=>modeText;
const m1=cell(), m2=cell("",false), sandboxText=cell(), sandbox=cell("",true);
sandbox.querySelector=()=>sandboxText;
const controls={".hs-mode":mode,".hs-m1":m1,".hs-m2":m2,".hs-sandbox":sandbox};
const v={engine:"pi",shepherdMode:true,engineSnap:{engines:{pi:{label:"Pi Agent"}}},
  catalog:{modes:[{id:"bypassPermissions",label:"bypass permissions mode"}],models:[
    {id:"model",label:"Model",default:true,variants:[],efforts:[{id:"high",label:"High"}]}]},
  sessionOpts:{mode:"bypassPermissions",model:"model",effort:"high"},
  shepherdEngineState:{},realModel:"",el:{querySelector:s=>controls[s]||null}};
hsOptsPaint(v); writes=0; hsOptsPaint(v);
assert.equal(writes,0,"相同 Pi 档位回读仍改写输入栏 DOM");
const rewind=cell("",false), fork=cell("",false), gen=cell("",true);
// 真实节点总是有 querySelector；旧版单节点 .hs-gen 里没有子节点，回退到整行文本。
gen.querySelector=()=>null;
const turn={engine:"pi",footEl:{querySelector:s=>s===".hs-gen"?gen:null}};
v.caps={rewind:false,fork:false}; v.turns=[turn];
v.el.querySelectorAll=(s)=>{queries++;return s.includes("rewind")?[rewind]:[fork];};
hsCapsPaint(v); writes=0; queries=0; hsCapsPaint(v);
assert.equal(queries,0,"相同能力回读仍扫描全部历史回合");
assert.equal(writes,0,"相同能力回读仍改写历史区 DOM");
v.caps={rewind:false,fork:true}; hsCapsPaint(v);
assert.equal(queries,2,"能力真实变化时没有刷新操作按钮");
'''
        out = subprocess.run([
            "node", "-e", script,
            os.path.join(ROOT, "frontend", "app-chat-opts.js"),
            os.path.join(ROOT, "frontend", "app-chat-refs.js"),
        ], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)


class HistoryBatching(unittest.TestCase):
    def test_批量回填只在末尾重建刻度(self):
        turn = fn("hsNewTurn")
        pour = fn("hsPour")
        # 2026-08-22 这一行多了一次 hsTrimDom(同样只在非回填时跑),守的仍是
        # 「整批回填期间一次都不重建刻度」。按 v.pouring 这个闸比,不按整行字面比。
        self.assertIn("if (!v.pouring) { hsIndex(v); hsTrimDom(v); }", turn)
        self.assertIn("v.pouring = true", pour)
        self.assertIn("v.pouring = wasPouring", pour)

    def test_贴底任务会合并而不是并发多条动画(self):
        pin = fn("hsPin")
        self.assertIn("s.until = Math.max", pin)
        self.assertIn("hsViewportSchedule(v)", pin)
        self.assertIn("if (s.frame || s.depth", fn("hsViewportSchedule"))

    def test_贴底刻度不逐回合读取布局(self):
        sync = fn("hsTocSync")
        fast = sync[sync.index("if (v.stick)"):sync.index("const r = v.body")]
        self.assertNotIn("getBoundingClientRect", fast)
        self.assertIn("ticks[ticks.length - 1]", fast)

    def test_隐藏页不轮询子代理记录(self):
        watch = fn("hsAgentsWatch")
        self.assertIn("if (hsHidden(v)) return", watch)

    def test_隐藏的子代理详情不拉完整日志(self):
        detail = re.search(r"async function hsAgentOpen\(v, a\)[\s\S]+?\n}\n\nfunction hsAgentClose", CHAT).group(0)
        self.assertIn("if (hsAgentViewHidden(v)) { v.agentLogOwed = true; return; }", detail)
        self.assertIn("tail_bytes=262144&limit=60", detail)
        shown = fn("hsShown")
        self.assertIn("hsAgentRefreshOpen(v)", shown)
        cold = fn("hsEvictCold")
        self.assertIn("clearInterval(v.agentTick)", cold)

    def test_旧历史分批且隐藏时暂停(self):
        rest = fn("hsPourRest")
        soon = fn("hsPourRestSoon")
        shown = fn("hsShown")
        self.assertIn("++seen > 4", rest)
        self.assertIn("hsPrepend(v, chunk, !v.pendRest)", rest)
        self.assertIn("hsHidden(v)", soon)
        self.assertIn("if (v.pendRest) hsPourRestSoon(v)", shown)


if __name__ == "__main__":
    unittest.main()
