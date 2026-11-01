"""输入框上方那条引擎状态条(.hs-env)的形制。

2026-08-19 用户实见:claude 的 529 那句在条上被切成两截——首行读到「…If it persists, check」
就没了,展开之后同一句英文又原样摆了第二遍。两处成因:
  一、后端按 `[:120]` 硬切首行当摘要,切点恰好落在词中间,也不补省略号,读起来像话没说完;
  二、条本身靠 `max-height` 卡高度、超出由 overflow 切掉,切在字腰上,像渲染坏了。
改法是把它排成任务条(.hs-tasks)那个样子:类别词 + 一行摘要 + 贴着摘要的箭头,
摘要单行省略,完整那句留给展开态——而且就地摊开,不另起一块。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import _srcmap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()

_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv

HEAD = ea.ClaudeAdapter._notice_head
OVERLOAD = ("API Error: 529 Overloaded. This is a server-side issue, usually temporary — "
            "try again in a moment. If it persists, check https://status.claude.com.")


class NoticeHead(unittest.TestCase):
    """后端摘要:词边界断开,补省略号。"""

    def test_短句原样不动(self):
        self.assertEqual(HEAD("Claude Code 未登录"), "Claude Code 未登录")

    def test_只取首行(self):
        self.assertEqual(HEAD("第一行报错\n第二行细节"), "第一行报错")

    def test_过载那句断在词边界并补省略号(self):
        got = HEAD(OVERLOAD)
        self.assertTrue(got.endswith("…"), got)
        self.assertTrue(OVERLOAD.startswith(got[:-1]), "省略号之前必须是原文的前缀")
        # 硬切会停在「check」后面(第 120 字),那正是用户看到「像话没说完」的位置
        self.assertFalse(got.rstrip("…").endswith("check"))
        self.assertNotIn("check", got)

    def test_不切在词中间(self):
        """保留的那一段之后必须是空格或标点,落在字母上就说明把一个词劈了。
        末尾的逗号句号先去掉再补省略号——「persists,…」比「persists…」多一个读不出意思的逗号。"""
        got = HEAD(OVERLOAD).rstrip("…")
        self.assertEqual(got, got.rstrip(" "))
        nxt = OVERLOAD[len(got):len(got) + 1]
        self.assertFalse(nxt.isalnum(), "断点落在词里了:%r" % (got[-12:] + "|" + nxt))

    def test_没有词边界时照原位切(self):
        """中文与长串标识符里找不到空格,不能因此丢掉半行内容。"""
        got = HEAD("x" * 300)
        self.assertEqual(got, "x" * 120 + "…")

    def test_摘要仍是详情的前缀(self):
        """前端靠这个前缀关系判断「详情只是同一句的完整版」,断了它展开就会摆两遍。"""
        self.assertTrue(OVERLOAD.startswith(HEAD(OVERLOAD).rstrip("…")))


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, e):
        self.events.append(e)


class NoticeEndToEnd(unittest.TestCase):
    """走真实那条路:引擎合成的报错 → 窄条帧。只测 _notice_head 会漏掉调用点——
    把调用点改回 `[:120]`,单测那几条照样全绿(2026-08-19 反向验证发现)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.sid = self.ad.create_session()["id"]
        self.path = os.path.join(self.tmp.name, "transcript.jsonl")
        self.ad._transcript_path = lambda esid: self.path

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, note):
        rows = [
            {"type": "user", "uuid": "u1", "timestamp": "2026-08-19T12:00:00.000Z",
             "message": {"role": "user", "content": "跑一下"}},
            {"type": "assistant", "uuid": "a1", "timestamp": "2026-08-19T12:00:20.000Z",
             "isApiErrorMessage": True,
             "message": {"role": "assistant", "model": "<synthetic>",
                         "stop_reason": "stop_sequence",
                         "content": [{"type": "text", "text": note}]}},
        ]
        with open(self.path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.ad.import_session("esid-1", sid=self.sid)
        return [e["properties"] for e in self.bus.events
                if e.get("type") == "session.environment"]

    def test_过载那句上条时已经断好(self):
        got = self._run(OVERLOAD)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["state"], "error")
        self.assertTrue(got[0]["label"].endswith("…"), got[0]["label"])
        self.assertNotIn("check", got[0]["label"], "硬切会停在 check 后面")
        self.assertEqual(got[0]["details"], [OVERLOAD], "完整那句要随帧带上,展开态靠它")

    def test_短报错后端不必伪造详情(self):
        """后端仍可只发摘要；前端会为所有错误条补齐稳定的展开契约。"""
        got = self._run("API Error: 401 Unauthorized")
        self.assertEqual(got[0]["label"], "API Error: 401 Unauthorized")
        self.assertEqual(got[0]["details"], [])

    def test_通用回合错误跨刷新保留(self):
        message = "native process exited before completion"
        self.ad._turn_error_environment(self.sid, {"message": message})
        env = self.ad.session_environment(self.sid)
        self.assertEqual(env["state"], "error")
        self.assertEqual(env["stage"], "turn")
        self.assertEqual(env["details"], [message])

    def test_通用回合错误不覆盖带动作的专用提示(self):
        self.ad._env_publish(self.sid, {"state": "error", "stage": "auth",
            "label": "需要重新登录", "details": ["token expired"],
            "action": {"kind": "terminal", "label": "重新登录"}})
        self.ad._turn_error_environment(self.sid, {"message": "generic failure"})
        env = self.ad.session_environment(self.sid)
        self.assertEqual(env["stage"], "auth")
        self.assertEqual(env["action"]["label"], "重新登录")

    def test_通用回合错误不覆盖无按钮的专用错误(self):
        self.ad._env_publish(self.sid, {"state": "error", "stage": "permission",
            "label": "权限闸异常", "details": ["extension failed"]})
        self.ad._turn_error_environment(self.sid, {"message": "generic failure"})
        self.assertEqual(self.ad.session_environment(self.sid)["stage"], "permission")

    def test_新回合只清除上一轮通用错误(self):
        self.ad._turn_error_environment(self.sid, {"message": "old turn failed"})
        self.ad._clear_turn_error_environment(self.sid)
        self.assertIsNone(self.ad.session_environment(self.sid))
        self.ad._env_publish(self.sid, {"state": "error", "stage": "auth",
            "label": "需要重新登录"})
        self.ad._clear_turn_error_environment(self.sid)
        self.assertEqual(self.ad.session_environment(self.sid)["stage"], "auth")

    def test_新回合在写入用户消息前清理旧通用错误(self):
        run = _srcmap.engine_adapters_src()
        start = run.index("    def _run(self, sid, text")
        end = run.index("    def _turn_dispatch", start)
        body = run[start:end]
        self.assertLess(body.index("self._clear_turn_error_environment(sid)"),
                        body.index("self._append_msg(sid, user_msg)"))


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class EnvFull(unittest.TestCase):
    """前端那半边的判据:详情是不是摘要的完整版。"""

    @classmethod
    def setUpClass(cls):
        m = re.search(r"\nfunction hsEnvFull\(label, details\)[\s\S]*?\n}\n", CHAT)
        assert m, "hsEnvFull 不见了"
        cls.fn = m.group(0)

    def _full(self, label, details):
        js = self.fn + "\nconsole.log(JSON.stringify(hsEnvFull(%s, %s)));" % (
            __import__("json").dumps(label), __import__("json").dumps(details))
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return __import__("json").loads(out.stdout)

    def test_同一句的完整版认得出(self):
        self.assertEqual(self._full(HEAD(OVERLOAD), [OVERLOAD]), OVERLOAD)

    def test_多条详情不算(self):
        """MCP 那一路一次报十几条,它们不是摘要的续写,该列在下面。"""
        self.assertEqual(self._full("Codex 启动 · 2 项", ["MCP a:超时", "MCP b:401"]), "")

    def test_没有详情时为空(self):
        self.assertEqual(self._full("Claude Code 未登录", []), "")

    def test_详情另起一句时不算(self):
        self.assertEqual(self._full("引擎已恢复", ["上一次失败发生在 10:21"]), "")


class EnvBarShape(unittest.TestCase):
    """形制与任务条一致:类别词 + 单行摘要 + 贴着摘要的箭头。"""

    def test_类别词四档齐全(self):
        m = re.search(r"const HS_ENV_KIND = \{([^}]*)\}", CHAT)
        self.assertTrue(m, "HS_ENV_KIND 不见了")
        for state in ("working", "ready", "warning", "error"):
            self.assertIn(state + ":", m.group(1).replace(" ", "").replace(":", ":"))
        self.assertIn('warning: "警告"', m.group(1))
        self.assertIn('error: "错误"', m.group(1))

    def test_条里有类别词那一格(self):
        self.assertIn('<span class="hs-env-k"></span>', CHAT)
        self.assertIn('.hs-env-k").textContent = hsEnvironmentKind', CHAT)

    def test_降级上下文是ready提醒且停留五秒(self):
        kind = re.search(r"function hsEnvironmentKind\(p, state\) \{[\s\S]*?\n\}", CHAT)
        duration = re.search(r"function hsEnvironmentReadyDuration\(p\) \{[\s\S]*?\n\}", CHAT)
        self.assertIsNotNone(kind); self.assertIsNotNone(duration)
        script = 'const HS_ENV_KIND={working:"引擎",ready:"引擎",warning:"警告",error:"错误",update:"更新"};\n' + kind.group(0) + "\n" + duration.group(0) + r'''
const assert=require('node:assert/strict');
const degraded={state:'ready',stage:'context_degraded'};
assert.equal(hsEnvironmentKind(degraded,'ready'),'提醒');
assert.equal(hsEnvironmentReadyDuration(degraded),5000);
assert.equal(hsEnvironmentKind({stage:'engine'},'ready'),'引擎');
assert.equal(hsEnvironmentReadyDuration({stage:'engine'}),800);
'''
        out=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode,0,out.stderr)
        self.assertIn('hsEnvironmentReadyDuration(p)',CHAT)

    def test_ready重复帧不取消延长或复活提醒(self):
        begin = re.search(r"function hsEnvironmentBegin\(v, sig, now, state\) \{[\s\S]*?\n\}", CHAT)
        self.assertIsNotNone(begin)
        script = r'''
const assert=require('node:assert/strict');
let cleared=[];function clearTimeout(id){if(id)cleared.push(id);}
''' + begin.group(0) + r'''
let v={envSig:'',envSigAt:0,envTimer:0,envHideTimer:0};
assert.equal(hsEnvironmentBegin(v,'ready-frame',1000,'ready'),true);
v.envTimer=17;
assert.equal(hsEnvironmentBegin(v,'ready-frame',2000,'ready'),false);
assert.deepEqual(cleared,[]);assert.equal(v.envTimer,17);assert.equal(v.envSigAt,1000);
v.envTimer=0;
assert.equal(hsEnvironmentBegin(v,'ready-frame',7000,'ready'),false);
assert.equal(v.envSigAt,1000);
assert.equal(hsEnvironmentBegin(v,'working-frame',7100,'working'),true);
assert.equal(hsEnvironmentBegin(v,'ready-frame-revision-2',7200,'ready'),true);
'''
        out=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode,0,out.stderr)

    def test_摘要单行省略(self):
        """靠 max-height 卡高度会把字切在腰上;单行 + ellipsis 才看得出是被截断的。"""
        m = re.search(r"\.hs-page \.hs-env-t \{[^}]*\}", CSS)
        self.assertTrue(m, ".hs-env-t 的规则不见了")
        for prop in ("text-overflow: ellipsis", "white-space: nowrap", "overflow: hidden"):
            self.assertIn(prop, m.group(0))

    def test_折叠态不再靠_max_height_切内容(self):
        m = re.search(r"\.hs-page \.hs-env \{[^}]*\}", CSS)
        self.assertTrue(m)
        self.assertNotIn("max-height", m.group(0),
                         "折叠靠单行省略,不靠卡高度——卡高度会切在字腰上")
        self.assertNotIn("max-height: 5.4em", CSS, "手机上那条三行的放行规则该撤了")

    def test_展开态就地摊开(self):
        self.assertIn(".hs-page .hs-env.open .hs-env-t", CSS)
        self.assertIn('el.querySelector(".hs-env-t").textContent = open ? full', CHAT)

    def test_所有持久问题都有展开详情(self):
        self.assertIn('if (["warning", "error"].includes(state) && !details.length) details.push(label);', CHAT)
        self.assertIn('"hs-env hs-col " + state + (details.length ? " has-detail" : "")', CHAT)

    def test_整条可点(self):
        """点开的是一句话,不是行尾那枚小箭头——热区给到整条。"""
        self.assertIn('querySelector(".hs-env-bar").onclick = envToggle', CHAT)
        self.assertNotIn('querySelector(".hs-env-t").onclick', CHAT)

    def test_圆点已撤(self):
        """严重程度由类别词的颜色带,再留一枚同色圆点是同一件事说两遍。"""
        self.assertNotIn("hs-env-dot", CHAT)
        self.assertNotIn("hs-env-dot", CSS)

    def test_收起时清掉摊开态(self):
        """摊开态留着,下一条提示会顶着上一条的正文出现。"""
        self.assertIn('el.dataset.short = ""; el.dataset.full = "";', CHAT)


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class EnvBarToggle(unittest.TestCase):
    def test_错误详情可以展开再收起(self):
        source = re.search(r"\nfunction hsEnvironmentToggle\(v\)[\s\S]*?\n}\n", CHAT).group(0)
        script = source + r'''
const assert = require('node:assert/strict');
const classes = new Set(['has-detail']);
const title = {textContent:'short'};
const detail = {hidden:true};
const chev = {label:'',setAttribute(k,v){if(k==='aria-label')this.label=v;}};
const el = {
  dataset:{short:'short',full:''}, attrs:{},
  classList:{contains:x=>classes.has(x),toggle(x){classes.has(x)?classes.delete(x):classes.add(x);return classes.has(x);}},
  querySelector(s){return s==='.hs-env-t'?title:s==='.hs-env-d'?detail:chev;},
  setAttribute(k,v){this.attrs[k]=v;}
};
assert.equal(hsEnvironmentToggle({envEl:el}),true);
assert.equal(detail.hidden,false); assert.equal(el.attrs['aria-expanded'],'true');
assert.equal(chev.label,'收起详情');
assert.equal(hsEnvironmentToggle({envEl:el}),false);
assert.equal(detail.hidden,true); assert.equal(el.attrs['aria-expanded'],'false');
assert.equal(chev.label,'展开详情');
'''
        got = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(got.returncode, 0, got.stderr)


class CodexLabel(unittest.TestCase):
    """行首已经写着「警告」,摘要不必再说一遍。"""

    def test_标签不再重复警告二字(self):
        src = open(os.path.join(ROOT, "desktron", "lib", "codex_appserver.py"),
                   encoding="utf-8").read()
        self.assertNotIn('启动存在警告"', src)
        self.assertNotIn('配置存在警告"', src)
        self.assertRegex(src, r'f"\{label\} 启动 · \{len\((?:warning_details\(\)|rows)\)\} 项"')


if __name__ == "__main__":
    unittest.main()


class ClaudeTurnFailure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_sdk_api_error_message_is_visible_and_persisted(self):
        ctx = {"sid": self.sid, "msg_id": "assistant", "parts": {}, "order": []}
        message = "Failed to authenticate. API Error: 401 OAuth access token has expired."
        self.ad.handle_event({
            "type": "assistant", "isApiErrorMessage": True, "apiErrorStatus": 401,
            "message": {"content": [{"type": "text", "text": message}]},
        }, ctx)
        self.assertNotIn("engine_error", ctx)
        self.assertFalse([e for e in self.bus.events if e["type"] == "session.error"])
        self.ad.handle_event({"type": "result", "is_error": True, "result": message}, ctx)
        self.assertEqual(ctx["engine_error"], message)
        self.assertEqual(ctx["engine_failure"], {
            "source": "provider", "provider": "claude", "code": "AUTH",
            "message": message, "status": 401})
        errors = [e["properties"] for e in self.bus.events if e.get("type") == "session.error"]
        self.assertEqual(errors[-1]["error"], ctx["engine_failure"])
        env = self.ad.session_environment(self.sid) or {}
        self.assertEqual(env.get("state"), "error")
        self.assertEqual(env.get("stage"), "auth")
        self.assertEqual(env.get("label"), message)
        self.assertEqual(env.get("action"), {"kind": "terminal", "label": "重新登录"})

    def test_api_retry_is_checkpointed_with_server_deadline_and_hour_budget(self):
        now = ea.time.time()
        ctx = {"sid": self.sid, "msg_id": "assistant", "parts": {}, "order": [],
               "t0": int(now * 1000)}
        with mock.patch.object(self.ad, "_checkpoint_live") as checkpoint:
            self.ad.handle_event({"type": "system", "subtype": "api_retry",
                "attempt": 2, "max_retries": 10, "retry_delay_ms": 125000,
                "error_status": 429, "error": "rate limited"}, ctx)
        retry = ctx["retry"]
        self.assertEqual((retry["attempt"], retry["max"], retry["delay_ms"]),
                         (2, 10, 125000))
        self.assertAlmostEqual(retry["deadline_at"] - int(now * 1000), 125000, delta=1500)
        self.assertFalse(retry["watchdog"])
        checkpoint.assert_called_once_with(ctx, force=True)
        event = [e["properties"] for e in self.bus.events if e.get("type") == "session.retry"][-1]
        self.assertEqual(event["deadline_at"], retry["deadline_at"])
        self.assertAlmostEqual(event["remaining_ms"], 125000, delta=1500)
        self.ad._live[self.sid] = ctx
        live = self.ad.live_message(self.sid)
        self.assertTrue(live["info"]["running"])
        self.assertEqual(live["info"]["retry"]["attempt"], 2)
        self.assertAlmostEqual(live["info"]["retry"]["remaining_ms"], 125000, delta=1500)

    def test_watchdog_retry_uses_hour_budget_not_misleading_sdk_max(self):
        ctx = {"sid": self.sid, "msg_id": "assistant", "parts": {}, "order": [], "t0": 1}
        self.ad.handle_event({"type":"system", "subtype":"api_retry", "attempt":11,
            "max_retries":10, "retry_delay_ms":2 * 60 * 60 * 1000,
            "retry_watchdog":True, "retry_budget_remaining_ms":45 * 60 * 1000,
            "error_status":429, "error":"rate limited"}, ctx)
        retry = ctx["retry"]
        self.assertTrue(retry["watchdog"])
        self.assertEqual(retry["max"], 0)
        self.assertEqual(retry["provider_max"], 10)
        self.assertEqual(retry["delay_ms"], 45 * 60 * 1000)

    def test_retry_budget_expiry_is_provider_failure_not_user_interrupt(self):
        ctx = {"sid": self.sid, "msg_id": "assistant", "parts": {}, "order": [],
               "retry":{"attempt":4, "max":0}}
        self.ad.handle_event({"type":"system", "subtype":"retry_budget_expired",
                              "retry_budget_ms":60 * 60 * 1000}, ctx)
        self.assertEqual(ctx["engine_failure"]["code"], "RETRY_TIMEOUT")
        self.assertNotIn("aborted", ctx)
        self.assertTrue(ctx["_engine_error_published"])

    def test_retry_metadata_and_error_survive_failed_turn_storage(self):
        ctx = {"sid": self.sid, "seq": 7, "msg_id": "assistant", "parts": {}, "order": [],
               "t0": 1, "engine_error": "rate limited",
               "engine_failure": {"source":"provider", "provider":"claude",
                                  "code":"RATE_LIMIT", "message":"rate limited", "status":429},
               "retry": {"attempt":3, "max":10, "delay_ms":120000,
                         "deadline_at":123000, "started_at":3000,
                         "budget_deadline_at":3603000}}
        self.ad._live[self.sid] = ctx
        self.ad._finish_turn(self.sid, ctx, None)
        info = self.ad.messages(self.sid)[-1]["info"]
        self.assertTrue(info["failed"])
        self.assertEqual(info["error"]["code"], "RATE_LIMIT")
        self.assertEqual(info["retry"]["attempt"], 3)
        idle = [e["properties"] for e in self.bus.events if e.get("type") == "session.idle"][-1]
        ready = [e["properties"] for e in self.bus.events if e.get("type") == "session.state"][-1]
        self.assertEqual(idle["error"]["message"], "rate limited")
        self.assertEqual(ready["retry"]["attempt"], 3)


class CodexTurnFailure(unittest.TestCase):
    """codex/pi 的回合失败也要上窄条,不能只留在状态行里。

    两条各说一件事(见 _env_publish 的注释):状态行说「这一轮停在哪儿」,窄条说
    「引擎此刻处在什么状态」。claude 那侧两条都发;codex 家族此前只发前者,于是同样是
    接口报错,在 claude 上点得开、看得到全文,在 codex 上只剩状态行里截断的一句。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus()
        self.ad = ea.CodexAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _envs(self):
        return [e["properties"] for e in self.bus.events
                if e.get("type") == "session.environment"]

    def test_turn_failed_上窄条(self):
        ctx = {"sid": self.sid}
        self.ad._app_event("turn/failed", {"error": {"message": OVERLOAD}}, ctx)
        got = self._envs()
        self.assertEqual(len(got), 1, "回合失败没有发窄条帧")
        self.assertEqual(got[0]["state"], "error")
        self.assertTrue(got[0]["label"].endswith("…"))
        self.assertEqual(got[0]["details"], [OVERLOAD])
        self.assertEqual(ctx["engine_error"], OVERLOAD, "状态行那一路照旧要有")
        self.assertEqual(got[0]["stage"], "turn")
        self.ad._clear_turn_error_environment(self.sid)
        self.assertIsNone(self.ad.session_environment(self.sid))

    def test_turn_completed_失败态同样上窄条(self):
        """两条通知路径都会报失败,只接一条会漏掉另一条(实测两者不总是成对出现)。"""
        ctx = {"sid": self.sid, "t0": 0}
        self.ad._app_event("turn/completed",
                           {"turn": {"status": "failed", "durationMs": 12,
                                     "error": {"message": OVERLOAD}}}, ctx)
        got = self._envs()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["details"], [OVERLOAD])

    def test_跨刷新留着(self):
        self.ad._app_event("error", {"error": {"message": OVERLOAD}}, {"sid": self.sid})
        keep = self.ad.session_environment(self.sid) or {}
        self.assertEqual(keep.get("state"), "error")

    def test_空消息仍保留失败且不显示空白提示(self):
        ctx = {"sid": self.sid}
        self.ad._app_event("turn/failed", {"error": {"message": "   "}}, ctx)
        self.assertEqual(ctx["engine_error"], "引擎报告了一个错误")
        self.assertEqual(self._envs()[0]["label"], ctx["engine_error"])

    def test_摘要与详情的前缀关系成立(self):
        """前端靠这个关系决定「展开就地摊开」,断了就会把同一句摆两遍。"""
        self.ad._app_event("turn/failed", {"error": {"message": OVERLOAD}}, {"sid": self.sid})
        p = self._envs()[0]
        self.assertTrue(p["details"][0].startswith(p["label"].rstrip("…")))

    def test_自动重连是运行态而不是回合失败(self):
        ctx = {"sid": self.sid, "msg_id": "msg_retry", "parts": {}, "order": []}
        self.ad._app_event("error", {
            "error": {
                "message": "Reconnecting... 2/5",
                "additionalDetails": "Idle timeout waiting for SSE",
            },
            "willRetry": True,
        }, ctx)

        got = self._envs()[-1]
        self.assertEqual(got["state"], "working")
        self.assertEqual(got["stage"], "reconnect")
        self.assertEqual(got["label"], "连接中断，正在重连 · 2/5")
        self.assertEqual(got["details"], ["Idle timeout waiting for SSE"])
        self.assertTrue(ctx["stream_retrying"])
        self.assertNotIn("engine_error", ctx)
        self.assertIsNone(self.ad.session_environment(self.sid), "临时重连不应跨刷新留成错误")

    def test_重连后续收到流帧即恢复(self):
        ctx = {"sid": self.sid, "msg_id": "m1", "phase": {}, "parts": [],
               "esid": "thread-1", "engine_turn_id": "turn-1"}
        self.ad._app_event("error", {
            "error": {"message": "Reconnecting... 3/5"}, "willRetry": True,
        }, ctx)
        with mock.patch.object(self.ad, "_ensure_part", return_value={"text": ""}), \
             mock.patch.object(self.ad, "_delta"):
            self.ad._app_event("item/agentMessage/delta", {
                "threadId": "thread-1", "turnId": "turn-1",
                "itemId": "answer", "delta": "继续",
            }, ctx)

        got = self._envs()[-1]
        self.assertEqual(got["state"], "ready")
        self.assertEqual(got["label"], "连接已恢复，继续执行")
        self.assertNotIn("stream_retrying", ctx)
        self.assertNotIn("engine_error", ctx)

    def test_容量重试使用持久倒计时且恢复后清理(self):
        now = int(ea.time.time() * 1000)
        ctx = {"sid":self.sid, "seq":8, "msg_id":"m-capacity",
               "parts":{}, "order":[], "t0":now}
        self.ad._live[self.sid] = ctx
        self.ad._app_event("turn/retry", {"active":True, "attempt":2, "max":4,
            "delay_ms":30000, "deadline_at":now + 30000,
            "status":"capacity", "error":"Selected model is at capacity"}, ctx)
        self.assertEqual(ctx["retry"]["status"], "capacity")
        self.assertEqual((ctx["retry"]["attempt"], ctx["retry"]["max"]), (2, 4))
        event = [e["properties"] for e in self.bus.events
                 if e.get("type") == "session.retry"][-1]
        self.assertTrue(event["active"])
        self.assertGreater(event["remaining_ms"], 28000)
        env = self._envs()[-1]
        self.assertEqual((env["state"], env["stage"]), ("working", "capacity"))
        self.assertIn("不会重发原始输入", env["details"][0])

        self.ad._app_event("turn/retry", {"active":False, "attempt":2,
                                           "max":4, "reason":"progress"}, ctx)
        self.assertNotIn("retry", ctx)
        self.assertNotIn("capacity_retrying", ctx)
        self.assertFalse([e["properties"] for e in self.bus.events
                          if e.get("type") == "session.retry"][-1]["active"])
        self.assertEqual(self._envs()[-1]["label"], "模型容量已恢复，继续执行")


class StatusLineReason(unittest.TestCase):
    """过程状态保持中性终态；结构化错误进入该回合的可展开过程。"""

    def test_实时状态行不显示错误原因(self):
        self.assertIn('hsProcFailure(tail, p.error);', CHAT)
        self.assertIn('hsTurnErrorNotice(v, p);\n    hsStatus(v, "error", "已停止");', CHAT)
        self.assertNotIn('hsStatus(v, "error", "已停止", tail', CHAT)
        self.assertNotIn("failedReason", CHAT)

    def test_实时错误不因过程为空而被裁掉(self):
        status = CHAT[CHAT.index("function hsStatus("):CHAT.index("function hsClock(")]
        self.assertNotIn('state === "error") hsProcTrim', status)

    def test_刷新历史状态只保留已停止和耗时(self):
        proc = CHAT[CHAT.index("function hsProcFailed("):CHAT.index("function hsProcInterrupted(")]
        self.assertIn('hsStTxt(el.querySelector(".hs-st-t"), "已停止", dur ? " " + dur : "")', proc)
        self.assertNotIn("error?.message", proc)
        self.assertNotIn("hsRetrySummary", proc)
        self.assertIn("hsProcFailure(t, info.error)", CHAT)
        self.assertIn("hsProcFailed(t, info.ms)", CHAT)
        self.assertIn("if (info.retry) hsRetrySync(v, info.retry)", CHAT)
        self.assertIn('if (p.retry && p.state !== "ready") hsRetrySync(v, p.retry)', CHAT)

    def test_长等待使用后端相对时间且不会倒计时归零后消失(self):
        self.assertIn("p?.remaining_ms ?? p?.delay_ms", CHAT)
        self.assertIn('DeskI18n.t("status.retryIn", { time: hsClock(left) }', CHAT)
        self.assertIn('DeskI18n.t("status.retrying", {}, "正在重试")', CHAT)
        self.assertIn('capacity ? "模型繁忙，等待重试"', CHAT)

    def test_附注跟着状态一起存取(self):
        """重建时只搬 label 的话,刷新一次原因就没了。"""
        self.assertIn("const status = { state: v.statusState, label: v.statusLabel, tail: v.statusTail };", CHAT)
        self.assertIn("hsStatus(v, status.state, status.label, status.tail)", CHAT)
        self.assertIn('v.statusTail = "";', CHAT)
