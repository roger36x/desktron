"""/config 没回来时,聊天框那几个档位不该整组消失(2026-08-24 用户实见)。

症状是「模型点开提示不能选择、只能默认;mode 那一档干脆没了」,看着像两个控件同时坏了。
其实是同一件事:两者都由 /config 供数,而 hsEngineSync 的 catch 只补 caps 与 slashEng——
v.engine 没被赋值,hsUI 就落到 HS_UI_NONE(modes:null, models:null)。

更要紧的是它不会再试。那一页从此一直残着,除非人把它关掉重开——用户正是按了右上角的 ×
再打开才恢复的。手机经隧道时每个请求要一两秒,开机那一阵请求挤在一起,恢复出来的会话页
一出生就是残的。

测的粒度落在 catch 那一段本身,不整个跑 hsEngineSync:后者要摸一大片 DOM 与全局状态,
补桩补不完——每补一层就冒出下一层,而那些都与本条判据无关。取的仍是源码,不是抄一份。
"""
import json
import os
import re
import shutil
import subprocess
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()


def fn(name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}\n" % re.escape(name), CHAT, re.S)
    assert m, name + " 不见了"
    return m.group(0)


def const(name):
    """对象与数组两种写法都取(HS_MODES 那几个是数组),单行与多行也都要取。

    收尾的括号不能要求在行首:HS_UI_NONE 是写在一行里的,那样匹配不到就会一路吞到
    下一个行首 `};`——把中间整个 hsEngineSync 都卷进来,而它正是被测对象的真身,
    于是桩被真函数盖掉,报的却是「hsCapsPaint 未定义」这种看不出所以然的错。
    """
    m = re.search(r"\nconst %s = [\[{].*?[\]}];\s*$" % re.escape(name), CHAT, re.S | re.M)
    assert m, name + " 不见了"
    return m.group(0)


def catch_body():
    """取 hsEngineSync 里 /config 失败那一段的原样源码。"""
    src = fn("hsEngineSync")
    at = src.index("  } catch {")
    body = src[at + len("  } catch {"):]
    return body[:body.index("\n  }\n")]


# 档位表是纯函数,直接拿真的来算;顺序要紧,HS_ENGINE_UI 引用前面那几张表。
TABLES = (const("HS_MODES") + const("HS_MODELS") + const("HS_EFFORTS")
          + const("HS_AUTOCOMPACTS") + const("HS_UI_CODEX") + const("HS_UI_NONE")
          + const("HS_ENGINE_UI") + fn("hsUI") + fn("hsModes") + fn("hsModels"))

HARNESS = r"""
let timers = [], resyncs = 0;
const setTimeout = (fn, ms) => { timers.push({fn, ms}); return timers.length; };
function runTimers(){ const t = timers; timers = []; for (const x of t) x.fn(); }
function hsEngineSync(){ resyncs++; }     // 重试打到这儿,不真跑那一大段
TABLES
function fallback(v, st, curEngine){
CATCH
}
BODY
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class ConfigFallback(unittest.TestCase):
    def _run(self, body):
        js = (HARNESS.replace("TABLES", TABLES)
              .replace("CATCH", catch_body()).replace("BODY", body))
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_档位不会整组消失(self):
        """两个控件一起没了,看着像控件坏了,其实只是这一次请求没回来。"""
        got = self._run("""
const v = {sid:"a", el:{isConnected:true}};
fallback(v, {engine:"claude"}, "claude");
console.log(JSON.stringify({engine: v.engine, modes: hsModes(v).length, models: hsModels(v).length}));
""")
        self.assertEqual(got["engine"], "claude", "引擎名被一起丢了")
        self.assertGreater(got["modes"], 0, "模式整组消失")
        self.assertGreater(got["models"], 0, "模型一个都不剩")

    def test_引擎名先取引擎接口报的那个(self):
        """别一律当 claude:落到 codex 的会话上会列出一组它没有的档位,选中即是一次硬失败。"""
        got = self._run("""
const v = {sid:"a", el:{isConnected:true}};
fallback(v, {engine:"codex"}, "claude");
console.log(JSON.stringify({engine: v.engine, modes: hsModes(v).length, models: hsModels(v).length}));
""")
        self.assertEqual(got["engine"], "codex")
        self.assertEqual(got["modes"], 2, "codex 的两档协作模式没出来")
        self.assertEqual(got["models"], 0, "codex 的模型本就不在这里切,不该凭空列出来")

    def test_引擎接口也断了就用当前选中的(self):
        got = self._run("""
const v = {sid:"a", el:{isConnected:true}};
fallback(v, null, "pi");
console.log(JSON.stringify(v.engine));
""")
        self.assertEqual(got, "pi", "没有兜到 curEngine")

    def test_都没有时才当_claude(self):
        got = self._run("""
const v = {sid:"a", el:{isConnected:true}};
fallback(v, null, "");
console.log(JSON.stringify(v.engine));
""")
        self.assertEqual(got, "claude")

    def test_已经知道引擎就不覆盖(self):
        """重试那一次进来时 v.engine 可能已经有值,别把它冲掉。"""
        got = self._run("""
const v = {sid:"a", el:{isConnected:true}, engine:"codex"};
fallback(v, {engine:"claude"}, "claude");
console.log(JSON.stringify(v.engine));
""")
        self.assertEqual(got, "codex")

    def test_失败之后会自己再试一次(self):
        """原来失败就一直残着,要人把这一页关掉重开才好。"""
        got = self._run("""
const v = {sid:"a", el:{isConnected:true}};
fallback(v, null, "claude");
runTimers();
console.log(JSON.stringify(resyncs));
""")
        self.assertEqual(got, 1, "没有再取一次")

    def test_只再试一次(self):
        """再不行也还有静态表兜着,不必没完没了地打。"""
        got = self._run("""
const v = {sid:"a", el:{isConnected:true}};
fallback(v, null, "claude");
fallback(v, null, "claude");
fallback(v, null, "claude");
runTimers();
console.log(JSON.stringify(resyncs));
""")
        self.assertEqual(got, 1, "每失败一次就排一个,叠起来会连打")

    def test_页已经不在就不再打(self):
        """视图被清掉之后那次定时器还会到,不该再往后端打一次。"""
        got = self._run("""
const v = {sid:"a", el:{isConnected:false}};
fallback(v, null, "claude");
runTimers();
console.log(JSON.stringify(resyncs));
""")
        self.assertEqual(got, 0)


class OptsFallback(unittest.TestCase):
    """/config 没回来时,档位不该整组回默认——那看着像「设置被清了」。

    2026-08-25 用户实见:刷新后 auto mode 变 manual、Opus 5 变「默认」,而后端存的、
    /config 返回的都是对的。原因在这一行的判据:

        const o = v.sessionOpts !== null ? v.sessionOpts : (引擎级那一份);

    失败时 v.sessionOpts 从没被赋值,是 undefined;而 `undefined !== null` 为真,
    于是走了「用会话档位」那一支,把 v.opts 设成 undefined。再往下 `o.effort` 直接抛,
    hsEngineSync 断在半路——斜杠命令、沙箱档、自动压缩、对齐全都不再执行。
    (写那组测试时我撞见过这个 TypeError,当时判成「桩补得不够深」,其实那就是它。)
    """

    def _resolve_src(self):
        # 这段 2026-08-25 从 hsEngineSync 抽进了 hsOptsPaint:SSE 那条路上也要重画,
        # 而它没有 /config 可取。回落逻辑一字未动,只是换了个落脚处。
        src = fn("hsOptsPaint")
        at = src.index("  const o = ")
        return src[at:src.index("\n  v.opts = ") + len("\n  v.opts = o || {};")]

    def _run(self, setup):
        js = """
%s
function resolve(v, st){
%s
  return v.opts;
}
%s
""" % ("", self._resolve_src().replace("  const o", "  const o"), setup)
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_没拿到会话档位就退到引擎级(self):
        got = self._run("""
const v = {sessionOpts: undefined, engine: "claude"};
const st = {opts: {claude: {mode: "auto", model: "opus"}}};
console.log(JSON.stringify(resolve(v, st)));
""")
        self.assertEqual(got, {"mode": "auto", "model": "opus"},
                         "undefined 被当成「有会话档位」,档位整组落空")

    def test_两边都没有时给空对象而不是_undefined(self):
        """下一行就要读 o.effort,给 undefined 会直接抛,整段初始化断在半路。"""
        got = self._run("""
const v = {sessionOpts: undefined, engine: "claude"};
console.log(JSON.stringify(resolve(v, null)));
""")
        self.assertEqual(got, {})

    def test_真有会话档位时照用(self):
        """正常那一路一个字都不该变。"""
        got = self._run("""
const v = {sessionOpts: {mode: "plan"}, engine: "claude"};
const st = {opts: {claude: {mode: "auto"}}};
console.log(JSON.stringify(resolve(v, st)));
""")
        self.assertEqual(got, {"mode": "plan"}, "会话自己的那份被引擎级盖掉了")

    def test_显式_null_也退到引擎级(self):
        """后端明说这段会话没存过档位时给 null,那一路的行为不变。"""
        got = self._run("""
const v = {sessionOpts: null, engine: "claude"};
const st = {opts: {claude: {effort: "max"}}};
console.log(JSON.stringify(resolve(v, st)));
""")
        self.assertEqual(got, {"effort": "max"})


class StillWiredIn(unittest.TestCase):
    def test_这段就是_hsEngineSync_的_catch(self):
        """判据取自源码。哪天 catch 被改回「只补 caps」,上面那组会立刻红。"""
        body = catch_body()
        self.assertIn("v.engine = v.engine ||", body)
        self.assertIn("cfgRetried", body)
        self.assertIn("hsEngineSync(v)", body, "重试没有打回自己")
        self.assertIn("v.sessionOpts === undefined", body,
                      "没有把「没拿到档位」写成 null,undefined 会被当成「有」")

    def test_正常那一路不设重试记号(self):
        """只有 catch 里设 cfgRetried;成功那一支一个字都不该变。"""
        src = fn("hsEngineSync")
        head = src[:src.index("  } catch {")]
        self.assertNotIn("cfgRetried", head)
