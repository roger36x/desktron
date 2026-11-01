"""自续轮在历史重放里也还原成同一条分割线。

harness 在子代理或后台任务交回时往主记录里注入一条 task-notification,引擎就着它自己
续一轮——读报告、判断下一步,有时接着调工具。实跑时这一轮由 handle_event 收进 ctx
(见 test_claude_turn_boundary),界面上是一条波浪分割线。但那条线由实时事件驱动,不进
消息存储:页面一刷新、或走一次对账重建,整段「引擎在处理子代理返回」就没了,读起来又变回
「这里莫名其妙停了一会儿」。

重放的判据:通知那条 user 条目是起点,它之后、下一条真人发言(或下一条通知)之前的
assistant 帧属于这一轮。判据拿本机一份真实记录核过——2026-08-12 的一份 60MB rollout,
20 条通知、118 个 assistant 帧,按这个线性窗口分出来的结果与顺着 parentUuid 回溯到通知
那条的结果逐帧一致,既不多收也不漏收;其中 2 条通知之后一个 assistant 帧都没有(用户紧跟着
发话,以及会话就此结束),那两条不产出分割线。

样本照那份记录的形状抄(claude-code 2.1.220),不是照文档编的。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import _srcmap
from frontend_i18n_js import NODE_PRELUDE

LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, LIB)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv

CHAT_JS = os.path.join(ROOT, "frontend", "app-chat.js")


def _notif(tid, status="completed"):
    return ("<task-notification>\n"
            "<task-id>%s</task-id>\n"
            "<tool-use-id>toolu_013usF1XvVLCMJVNHMKtTLtL</tool-use-id>\n"
            "<output-file>/private/tmp/claude-501/tasks/%s.output</output-file>\n"
            "<status>%s</status>\n"
            "<summary>Agent \"Explore\" finished</summary>\n"
            "</task-notification>" % (tid, tid, status))


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, e):
        self.events.append(e)


def _at(sec):
    return "2026-08-12T09:%02d:%02d.000Z" % (sec // 60, sec % 60)


def _user(uuid, text, sec, **extra):
    o = {"type": "user", "uuid": uuid, "timestamp": _at(sec),
         "message": {"role": "user", "content": text}}
    o.update(extra)
    return o


def _notif_row(uuid, tid, sec, status="completed"):
    return _user(uuid, _notif(tid, status), sec, origin={"kind": "task-notification"},
                 promptSource="sdk")


def _assistant(uuid, sec, blocks, request_id=""):
    row = {"type": "assistant", "uuid": uuid, "timestamp": _at(sec),
           "message": {"role": "assistant", "content": blocks}}
    if request_id:
        row["requestId"] = request_id
    return row


def _text(uuid, sec, txt):
    return _assistant(uuid, sec, [{"type": "text", "text": txt}])


def _tool(uuid, sec, tid, name, inp):
    return _assistant(uuid, sec, [{"type": "tool_use", "id": tid, "name": name, "input": inp}])


def _tool_result(uuid, sec, tid, out):
    return _user(uuid, [{"type": "tool_result", "tool_use_id": tid, "content": out}], sec)


class AsideReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.sid = self.ad.create_session()["id"]
        # 子代理名单在这些用例里是外部事实,按需摆一份;不摆就是「对不上任何子代理」。
        self.rows = []
        self.calls = []
        self.ad.subagents = self._subagents

    def tearDown(self):
        self.tmp.cleanup()

    def _subagents(self, sid):
        self.calls.append(sid)
        return list(self.rows)

    def _group(self, rows, **kw):
        path = os.path.join(self.tmp.name, "transcript.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return self.ad._group_transcript(path, self.sid, "esid-1", **kw), path

    def _asides(self, msgs):
        return [m for m in msgs if (m["info"] or {}).get("aside")]

    def _plain(self, m):
        return "".join(q.get("text") or "" for q in m.get("parts") or []
                       if q.get("type") == "text")

    def test_同一请求内工具前说明回放为过程输出(self):
        msgs, _ = self._group([
            _user("u1", "检查门禁", 0),
            _assistant("a1", 5, [{"type": "text", "text": "先跑 quick。"}], "req-1"),
            _assistant("a2", 6, [{"type": "tool_use", "id": "tu-1", "name": "Bash",
                                   "input": {"command": "pytest -q"}}], "req-1"),
            _tool_result("r1", 10, "tu-1", "passed"),
            _assistant("a3", 12, [{"type": "text", "text": "门禁通过。"}], "req-2"),
        ])
        answer = [m for m in msgs if m["info"]["role"] == "assistant"][-1]
        self.assertEqual([p["type"] for p in answer["parts"]],
                         ["reasoning", "tool", "text"])
        self.assertEqual(answer["parts"][0]["text"], "先跑 quick。")
        self.assertEqual(answer["parts"][2]["text"], "门禁通过。")

    def test_不同请求的阶段结论不被后续工具吞掉(self):
        msgs, _ = self._group([
            _user("u1", "现在进度如何", 0),
            _assistant("a1", 5, [{"type": "text", "text": "尚未完成，预计十分钟。"}], "req-1"),
            _assistant("a2", 6, [{"type": "tool_use", "id": "tu-1", "name": "Bash",
                                   "input": {"command": "status"}}], "req-2"),
        ])
        answer = [m for m in msgs if m["info"]["role"] == "assistant"][-1]
        self.assertEqual([p["type"] for p in answer["parts"]], ["text", "tool"])

    # ---- 归属 ----

    def test_通知之后的帧成一条自续轮(self):
        msgs, _ = self._group([
            _user("u1", "先看一眼适配器", 0),
            _text("a1", 5, "看完了"),
            _notif_row("n1", "a44b3b6ebbc038143", 30),
            _text("a2", 40, "子代理交回,我读一下"),
            _user("u2", "继续", 60),
        ])
        self.assertEqual([self._plain(m) for m in msgs if self.ad._turn_start(m)],
                         ["先看一眼适配器", "继续"])
        got = self._asides(msgs)
        self.assertEqual(len(got), 1)
        self.assertEqual(self._plain(got[0]), "子代理交回,我读一下")
        self.assertEqual(got[0]["info"]["aside"]["task_id"], "a44b3b6ebbc038143")
        # 位置:排在它发生的地方,不是追加到末尾
        self.assertEqual(msgs.index(got[0]), 3)  # 返回消息在前，主代理续答在后。
        self.assertEqual(msgs[2]["info"]["role"], "tool")
        self.assertEqual(got[0]["info"]["role"], "assistant")
        self.assertEqual(msgs[-1]["info"]["role"], "user")

    def test_自续轮里的工具步骤也归它(self):
        """自续轮不只是读报告,它常接着调工具。那几步连同回填的结果都要留在这一轮里。"""
        msgs, _ = self._group([
            _user("u1", "开始", 0),
            _text("a1", 5, "派个子代理"),
            _notif_row("n1", "a099eb2f29e27c08c", 30),
            _text("a2", 35, "先看看它改了什么"),
            _tool("a3", 40, "toolu_1", "Read", {"file_path": "/tmp/x.py"}),
            _tool_result("r1", 45, "toolu_1", "文件正文"),
            _text("a4", 50, "改得对"),
            _user("u2", "好", 90),
        ])
        got = self._asides(msgs)
        self.assertEqual(len(got), 1)
        kinds = [q["type"] for q in got[0]["parts"]]
        self.assertEqual(kinds, ["text", "tool", "text"])
        self.assertEqual(got[0]["parts"][1]["state"]["output"], "文件正文")
        # 这几步不该同时留在上一条助手消息里
        prev = [m for m in msgs if m["info"]["role"] == "assistant" and not m["info"].get("aside")]
        self.assertEqual([self._plain(m) for m in prev], ["派个子代理"])

    def test_两条通知背靠背各自成一条(self):
        msgs, _ = self._group([
            _user("u1", "开始", 0),
            _text("a1", 5, "派两个"),
            _notif_row("n1", "aaa1", 30),
            _text("a2", 35, "第一个回来了"),
            _notif_row("n2", "bbb2", 40),
            _text("a3", 45, "第二个也回来了"),
            _user("u2", "好", 90),
        ])
        got = self._asides(msgs)
        self.assertEqual([self._plain(m) for m in got], ["第一个回来了", "第二个也回来了"])
        self.assertEqual([m["info"]["aside"]["task_id"] for m in got], ["aaa1", "bbb2"])

    def test_没有自续内容的通知不留痕(self):
        """通知落进上下文、引擎却没有接着跑(用户紧跟着发话,或会话就此结束)。
        那时并不存在「引擎在处理子代理返回」这件事,不该画一条分割线。"""
        msgs, _ = self._group([
            _user("u1", "开始", 0),
            _text("a1", 5, "派出去了"),
            _notif_row("n1", "aaa1", 30),
            _user("u2", "目标达成了吗?", 31),
            _text("a2", 40, "还没有"),
        ])
        self.assertEqual(self._asides(msgs), [])
        self.assertEqual([self._plain(m) for m in msgs if self.ad._turn_start(m)],
                         ["开始", "目标达成了吗?"])

    def test_收尾在文件末也认(self):
        msgs, _ = self._group([
            _user("u1", "开始", 0),
            _notif_row("n1", "aaa1", 30),
            _text("a1", 40, "读完了"),
        ])
        self.assertEqual([self._plain(m) for m in self._asides(msgs)], ["读完了"])

    def test_子代理自己那份记录不认自续轮(self):
        """支线记录整份都是 isSidechain,里面没有自续轮这回事,别在那条路上认。"""
        rows = [dict(r, isSidechain=True) for r in [
            _user("u1", "去查一下", 0),
            _notif_row("n1", "aaa1", 30),
            _text("a1", 40, "查完了"),
        ]]
        msgs, _ = self._group(rows, side=True)
        self.assertEqual(self._asides(msgs), [])

    # ---- 时长 ----

    def test_耗时取通知到末帧(self):
        """实时那条的 ms 来自 result 帧的 duration_ms,重建时没有这个数。退回首末时刻之差:
        起点用通知那条的时刻(引擎正是从那时起这一轮),终点用最后一帧,口径同普通回合。"""
        msgs, _ = self._group([
            _notif_row("n1", "aaa1", 30),
            _text("a1", 35, "读"),
            _text("a2", 62, "读完了"),
            _user("u1", "好", 90),
        ])
        a = self._asides(msgs)[0]["info"]["aside"]
        self.assertEqual(a["ms"], 32000)
        self.assertEqual(a["started"], self._asides(msgs)[0]["info"]["at"])

    def test_只有一帧时不编一个耗时(self):
        msgs, _ = self._group([
            _notif_row("n1", "aaa1", 30),
            {"type": "assistant", "uuid": "a1", "timestamp": "",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "读完了"}]}},
            _user("u1", "好", 90),
        ])
        self.assertEqual(self._asides(msgs)[0]["info"]["aside"]["ms"], 0)

    # ---- 是谁交回 ----

    def test_代理名称与收场沿用同一条路(self):
        self.rows = [{"id": "a44b3b6ebbc038143", "kind": "general-purpose",
                      "task": "修回合未结束却显示已完成", "outcome": "completed",
                      "started": 1_000_000, "ended": 1_192_000}]
        msgs, _ = self._group([
            _notif_row("n1", "a44b3b6ebbc038143", 30),
            _text("a1", 40, "读完了"),
            _user("u1", "好", 90),
        ])
        g = self._asides(msgs)[0]["info"]["aside"]["agent"]
        self.assertEqual(g["id"], "a44b3b6ebbc038143")
        self.assertEqual(g["task"], "修回合未结束却显示已完成")
        self.assertEqual(g["outcome"], "completed")
        self.assertEqual(g["ms"], 192000)   # 子代理自己跑了多久,不是引擎读报告那几秒

    def test_对不上子代理时不硬安一个名字(self):
        """后台命令跑完发的是同一种通知,那类对不上任何子代理。界面据此换一种说法。"""
        msgs, _ = self._group([
            _notif_row("n1", "bb3yt66id", 30),
            _text("a1", 40, "后台那条命令跑完了"),
            _user("u1", "好", 90),
        ])
        self.assertIsNone(self._asides(msgs)[0]["info"]["aside"]["agent"])

    def test_名单一份记录只取一次(self):
        """名单要 listdir、逐个读子代理记录的首末、再扫一遍主记录收证据。
        一份记录里几十个自续轮,按条数重复取会把重建拖慢。"""
        msgs, _ = self._group([
            _notif_row("n1", "aaa1", 30), _text("a1", 35, "一"),
            _notif_row("n2", "aaa2", 40), _text("a2", 45, "二"),
            _notif_row("n3", "aaa3", 50), _text("a3", 55, "三"),
            _user("u1", "好", 90),
        ])
        self.assertEqual(len(self._asides(msgs)), 3)
        self.assertEqual(len(self.calls), 1)

    def test_没有自续轮时一次都不取(self):
        self._group([_user("u1", "开始", 0), _text("a1", 5, "好")])
        self.assertEqual(self.calls, [])

    # ---- 与实时那条的关系 ----

    def test_与实时那条事件同构(self):
        """两条路产出同一组字段,前端才谈得上共用一套渲染。

        实时那条 2026-08-20 换成了 _aside_mark_close(分段追认),它比重放这条多三个字段:
        mark / part_ids 说的是「本回合第几块起算」,内容本来就在回合里,不必随事件搬运
        (parts 因此恒为空)。重放那条没有「块序号」这个概念——记录重建出来的自续轮
        自成一条消息,内容就在它的 parts 上。所以判据是:重放的字段是实时的子集。
        """
        self.rows = [{"id": "aaa1", "kind": "general-purpose", "task": "查一下",
                      "outcome": "completed", "started": 1_000_000, "ended": 1_030_000}]
        msgs, _ = self._group([
            _notif_row("n1", "aaa1", 30), _text("a1", 40, "读完了"), _user("u1", "好", 90),
        ])
        replay = self._asides(msgs)[0]["info"]["aside"]
        ctx = {"sid": self.sid, "msg_id": "m1", "order": ["p1"],
               "aside_hint_at": {"task": "aaa1", "status": "completed", "at": 1_700.0}}
        live = self.ad._aside_mark_close(ctx, {"duration_ms": 10000,
                                               "origin": {"kind": "task-notification"}}, 0)
        self.assertLessEqual(set(replay), set(live), "重放那条有实时给不出的字段")
        self.assertEqual(live["agent"], replay["agent"])
        self.assertEqual(live["origin"], replay["origin"])
        self.assertEqual(live["part_ids"], ["p1"])   # 追认的是本回合第 0 块起

    def test_没有产出的那一段不出线(self):
        """子代理交回撞上用户发话的那一刻:引擎把通知并进用户这一轮跑,那一段一块内容都没有。
        重建按「有没有产出」判(flush_aside 的 `if not parts: return`),实时这侧则是发一条
        part_ids 为空的追认,前端见到空名单不画线(hsAsideMark 的 `if (!ids.length) return`)。
        两条路的结果一致:不画。

        旧机制在这里要专门发一条 phase=revoke 把已经画出去的线撤掉——那是因为它先画后判;
        分段追认是判完再画,没有可撤的东西,revoke 连同 _aside_close / _aside_revoke
        一起在 2026-08-20 撤了。"""
        sent = []
        self.ad.bus.publish = lambda ev: sent.append(ev)
        ctx = {"sid": self.sid, "msg_id": "m1", "order": []}
        row = self.ad._aside_mark_close(ctx, {"duration_ms": 10000,
                                              "origin": {"kind": "task-notification"}}, 0)
        self.assertEqual(row["part_ids"], [], "空段却报出了内容块")
        ev = [e for e in sent if e.get("type") == "session.aside"]
        self.assertEqual([p["properties"]["phase"] for p in ev], ["mark"])
        self.assertEqual(ev[0]["properties"]["part_ids"], [])

    def test_旧那套已经撤干净(self):
        """两套机制并存时,旧那条从别的口子冒出来过(2026-08-20 用户实见:同一段回答在
        消息上下各画了一遍,两遍的排版还不一样)。这里钉住它不再回来。"""
        for gone in ("_aside_new", "_aside_close", "_aside_note", "_aside_revoke", "_aside_bail"):
            self.assertFalse(hasattr(self.ad, gone), "%s 又回来了" % gone)
        src = _srcmap.engine_adapters_src()
        self.assertNotIn('"phase": "open"', src)
        self.assertNotIn('"phase": "revoke"', src)
        self.assertNotIn('"no_live"', src)

    # ---- 不破坏既有口径 ----

    def test_自续轮不算一个回合(self):
        """分割线在契约里也是 role=user(同压缩行、目标行),但它不是谁的发言。
        按发言切分的地方(翻页、取最近 N 回合、回退对齐)少认一处就整体错位。"""
        rows = [
            _user("u1", "第一问", 0),
            _text("a1", 5, "答"),
            _notif_row("n1", "aaa1", 30),
            _text("a2", 40, "读完了"),
            _user("u2", "第二问", 90),
            _text("a3", 95, "答"),
        ]
        msgs, path = self._group(rows)
        self.assertFalse(any(self.ad._turn_start(m) for m in self._asides(msgs)))
        turns = self.ad._human_turns(path)
        self.assertEqual([t for _, t in turns], ["第一问", "第二问"])
        self.assertEqual([self._plain(m) for m in msgs if self.ad._turn_start(m)],
                         [t for _, t in turns])

    def test_回退按序号对齐时排掉分割线(self):
        """_human_turns 把触发自续轮的那条通知滤掉了,这边不排掉就整体差一位。"""
        rows = [
            _user("u1", "第一问", 0),
            _text("a1", 5, "答"),
            _notif_row("n1", "aaa1", 30),
            _text("a2", 40, "读完了"),
            _user("u2", "第二问", 90),
        ]
        msgs, path = self._group(rows)
        self.ad._rewrite_msgs(self.sid, msgs)
        self.ad.index[self.sid]["engine_sid"] = "esid-1"
        self.ad._transcript_path = lambda esid: path
        mid = [m["info"]["id"] for m in msgs if self.ad._turn_start(m)][1]
        got, err = self.ad._engine_umid(self.sid, mid)
        self.assertEqual(err, "")
        self.assertEqual(got[1], "u2")

    def test_撤回不会把分割线当成刚打的字摘走(self):
        msgs, _ = self._group([
            _user("u1", "开始", 0), _text("a1", 5, "派出去了"),
            _notif_row("n1", "aaa1", 30), _text("a2", 40, "读完了"),
        ])
        self.ad._rewrite_msgs(self.sid, msgs)
        self.assertEqual(self.ad._pop_last_user(self.sid), "")
        self.assertEqual(len(self.ad.messages(self.sid)), len(msgs))


# 最小 DOM 替身,跑真的 hsAsideRow 那一族。它们是实时与重放共用的那一套——两条路长成
# 两个样子,读的人就得学两遍。
HARNESS = r"""
class CL {
  constructor(n){this.n=n;this.s=new Set();}
  // 节点的 className 是直接写上去的,classList 得先把它认下来,不然 add 一下就把原来那几个
  // 类名顶掉了(hsProcBox 正是先写 className 再 classList.add)。
  add(c){for(const x of (this.n.className||"").split(" ")) if(x) this.s.add(x);
         this.s.add(c); this.n.className=[...this.s].join(" ");}
  remove(c){this.s.delete(c);}
  toggle(c){this.s.has(c)?this.s.delete(c):this.s.add(c);}
  contains(c){return this.s.has(c);}
}
class Node {
  constructor(tag){this.tag=tag;this.children=[];this._text="";this._cn="";this.title="";
                   this.parentNode=null;this.style={};this.classList=new CL(this);}
  set className(v){this._cn=v;}
  get className(){return this._cn;}
  set textContent(v){this._text=v;this.children=[];}
  // 真实 DOM 里 textContent 是自己那段文字加上子节点的:hsStTxt 先写阶段名(它落在 _text
  // 上),再挂一个 .hs-st-d 放耗时,两段拼起来才是灯上那句话。
  get textContent(){return this._text + this.children.map((c)=>c.textContent).join("");}
  get firstChild(){return this.children[0] || null;}
  // 真实 DOM 的 Node.contains:自己或任一后代算命中，null 一律不命中。
  // 缺了它，前端里凡是「这块还在不在这个容器下」的判断都会当场抛错(hsProcEl 等)。
  contains(n){
    if (!n) return false;
    if (n === this) return true;
    for (const c of this.children) if (c && c.contains && c.contains(n)) return true;
    return false;
  }
  // 几段固定模板:分割线、过程区、状态灯、气泡。别的当文本收着即可。
  set innerHTML(h){
    const mk=(cls,tag)=>{const n=new Node(tag||"div");n.className=cls;n.parentNode=this;return n;};
    if (h.indexOf("hs-cmp-h") >= 0) {
      const head=mk("hs-cmp-h"); head.appendChild(mk("", "span"));
      this.children=[head];
    } else if (h.indexOf("hs-proc-bar") >= 0) {
      const bar=mk("hs-proc-bar"); bar.appendChild(mk("hs-proc-slot","span"));
      this.children=[bar, mk("hs-proc-body"), mk("hs-rule")];
    } else if (h.indexOf("hs-bubble") >= 0) {
      this.children=[mk("hs-bubble")];
    } else if (h.indexOf("hs-st-t") >= 0) {
      this.children=[mk("hs-dot","span"), mk("hs-st-t","span"), mk("hs-st-next","span")];
    } else { this._text=h; }
  }
  appendChild(n){
    if (n.parentNode) n.parentNode.children = n.parentNode.children.filter((x)=>x!==n);
    n.parentNode=this; this.children.push(n); return n;
  }
  insertBefore(n,ref){
    if (n.parentNode) n.parentNode.children = n.parentNode.children.filter((x)=>x!==n);
    n.parentNode=this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(n); else this.children.splice(i,0,n);
    return n;
  }
  remove(){ if (this.parentNode) this.parentNode.children =
    this.parentNode.children.filter((x)=>x!==this); this.parentNode=null; }
  addEventListener(){}
  querySelector(sel){
    const parts=sel.trim().split(/\s+/);
    let pool=this.children, hit=null;
    for (const p of parts) {
      hit=null;
      const walk=(ns)=>{for(const n of ns){ if(hit) return;
        if (p.startsWith(".") ? n.className.split(" ").indexOf(p.slice(1))>=0 : n.tag===p) {hit=n;return;}
        walk(n.children);}};
      walk(pool);
      if (!hit) return null;
      pool=hit.children;
    }
    return hit;
  }
  querySelectorAll(){return [];}
}
const document = { createElement:(t)=>new Node(t),
                   createTextNode:(s)=>{ const n=new Node("#text"); n.textContent=s; return n; } };
const md = { render:(t)=>t };
function cancelAnimationFrame(){}
// 计时器记账:那一行的后半段秒数每秒跳一次,收场与撤回都要把它停掉——不停的话
// 页面还在,它就一直在跑。TICKS 是当下还开着的个数,用例照它断言起停。
let TICKS = 0, TICKID = 0;
function setInterval(){ TICKS++; return ++TICKID; }
function clearInterval(id){ if (id) TICKS--; }
function hsScroll(){}
function hsIndex(){}
function isInjected(){ return false; }
function hsProcOpenSet(){ return new Set(); }
function hsProcOpenSave(){}
function hsProcRepaint(){ return false; }
function hsUserText(el, t){ el.textContent = t; }
function hsUFoot(v, t, u){ t.uFootEl = u.appendChild(document.createElement("div")); }
// 真的 hsPaint 要整套 DOM。这里只记下每一块落在哪个回合、哪个区、什么型别——归属才是
// 这几条用例要看的东西。分区照真的那套走:正文进正文,思考与工具进过程区。
function hsPaint(v, part){
  const box = (part.type === "tool" || part.type === "reasoning")
    ? hsProcBox(part.turn).querySelector(".hs-proc-body") : part.turn.holder;
  if (!part.el) { part.el = new Node("div"); box.appendChild(part.el); }
  part.el.className = "p-" + part.type;
  part.el.textContent = part.text || part.tool || "";
}
// 一个回合正常收尾:走真的 hsStatus,那盏「已完成 · 110.2s」于是和线上是同一个来路——
// 它就是消息流那盏灯留在原地的节点(见 hsStatusClear)。替身自己摆一个的话,写回落空
// 这类问题恰恰照不出来。
function hsDone(v, t, ms){
  v.stats = { ms: ms };
  hsStatus(v, "working", "处理中");
  hsStatus(v, "done", "已完成");
}
// 用户又发了一句。raw = 只做「灯的归属换到新回合」这一件事(hsStatusClear + 新回合 +
// hsStatus),不带 hsDispatch 那句就地收场——收场写回靠不靠得住,要在这种状态下才照得出来。
function hsSendRaw(v, text){
  hsStatusClear(v);
  v.stats = null;
  const t = hsNewTurn(v, text);
  hsStatus(v, "working", "处理中");
  return t;
}
// 完整的那条:hsDispatch 里与状态灯有关的那几步。
function hsSendNext(v, text){
  return hsSendRaw(v, text);
}
function hsLamp(t){
  const box = t.procEl;
  const el = box && box.querySelector(".hs-status");
  return el ? { cls: el.className, txt: el.querySelector(".hs-st-t").textContent } : null;
}
// 这些用例测的是「人正看着的那一页」。el 上要给出 hsHidden 判得出的可见标记:
// 少了它会被当成盖在后面的后台页,hsTrimDom 走的就是另一条分支(见 test_frontend_dom_trim)。
function mkView(){ return { stream: new Node("div"), turns: [], busy: false, asideOf: new Map(),
                            el: Object.assign(new Node("div"), { isConnected: true, offsetParent: {} }),
                            stats: null }; }
// 正文区里排着什么。过程区那一条是回合的顶行,不在这一列里看(它自己有 hsSteps)。
function hsBlocks(t){ return t.holder.children.filter((x)=>x!==t.procEl)
                                     .map((x)=>[x.className, x.textContent]); }
function hsSteps(t){ const b = t.procEl && t.procEl.querySelector(".hs-proc-body");
                     return b ? b.children.map((x)=>x.className) : []; }
SRC
BODY
"""


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class AsideRendering(unittest.TestCase):
    """两条路,一套渲染。

    实时(hsAsideMark):内容一直长在本回合里,后端在收尾那一刻追认「从第几块起是交回的
    那一段」,界面只在分界处插一条线、给那几块标出处。
    重放(hsAsideRow):记录重建出来的那条自成一条消息(role=user、info.aside),内容就在
    它自己的 parts 上,一次铺完。

    2026-08-20 撤掉的是中间那一版:开场画线、内容整批搬过来、判错再撤回重画。它与新机制
    并存期间从别的口子冒出来过——同一段回答在用户消息上下各铺一遍,两遍排版还不一样。
    """

    FNS = ("hsAsideRow", "hsAsideMark", "hsAsideHost", "hsAsideMid",
           "hsAsideOwn", "hsIsAside", "hsAsideLine", "hsAsideStat",
           "hsAsideFill", "hsAsideFoot",
           "hsNewTurn", "hsTrimDom", "hsHidden", "hsTurnFor", "hsPart",
           "hsProcBox", "hsProcTrim", "hsProcOpenKey", "hsStatus", "hsStatusClear",
           "hsStatusPaint", "hsActivityPaint", "hsStatsText", "hsCompacting",
           "hsStTxt", "hsSetText", "hsSkipPart", "hsClock", "hsEllipsis", "hsAgentOutcomeText")

    def _run(self, body):
        src = _srcmap.chat_src()
        parts = []
        for name in self.FNS:
            m = re.search(r"\nfunction " + name + r"\(.*?\n}\n", src, re.S)
            self.assertIsNotNone(m, name + " 不见了")
            parts.append(m.group(0))
        m = re.search(r"const HS_DOM_TURNS = (\d+);", src)   # hsTrimDom 用它,跟着源文件走
        self.assertIsNotNone(m, "HS_DOM_TURNS 不见了")
        parts.append("const HS_DOM_TURNS = %s;" % m.group(1))
        js = HARNESS.replace("SRC", "\n".join(parts)).replace("BODY", body)
        out = subprocess.run(["node", "-"], input=NODE_PRELUDE + js, capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    AGENT = {"id": "x1", "kind": "general-purpose", "task": "修回合未结束却显示已完成",
             "outcome": "completed", "ms": 192000}

    # 一个已经收尾的回合,里面两块正文:前一块是给用户的回答,后一块是读报告读出来的。
    SETUP = """
const v = mkView();
const t = hsNewTurn(v, null); t.msgId = "msg_a_1";
const p0 = hsPart(v, "msg_a_1", "q0", "text"); p0.text = "给用户的回答"; hsPaint(v, p0);
const p1 = hsPart(v, "msg_a_1", "q1", "text"); p1.text = "顺带把报告读了"; hsPaint(v, p1);
hsDone(v, t, 110193);
t.footEl = document.createElement("div");
t.footEl.className = "hs-foot";
t.holder.appendChild(t.footEl);
const AGENT = %s;
""" % json.dumps(AGENT, ensure_ascii=False)

    def _mark(self, tail, ids='["q1"]', agent="AGENT"):
        return self._run(self.SETUP + """
hsAsideMark(v, { host: "msg_a_1", msg_id: "msg_a_1", origin: "task-notification",
                 ms: 12000, task_id: "x1", agent: %s, mark: 1, part_ids: %s, parts: [] });
%s""" % (agent, ids, tail))

    # ---- 实时:分段追认 ----

    def test_线插在被追认那一块之前(self):
        got = self._mark('console.log(JSON.stringify(hsBlocks(t).map((x)=>x[0])));')
        self.assertEqual(got, ["p-text", "hs-status st-done hs-aside",
                               "p-text hs-from-aside", "hs-foot"],
                         "线应当夹在两块正文之间,线下那块带出处")

    def test_不新建回合(self):
        got = self._mark('console.log(JSON.stringify(v.turns.length));')
        self.assertEqual(got, 1)

    def test_内容不搬家(self):
        """追认只补一条线与出处,不重铺内容——重铺正是上一版把同一段摆两遍的来路。"""
        got = self._mark("""
console.log(JSON.stringify(hsBlocks(t).filter((x)=>x[0].indexOf("p-")===0).map((x)=>x[1])));""")
        self.assertEqual(got, ["给用户的回答", "顺带把报告读了"], "内容被重铺了一份")

    def test_被追认的块标上出处(self):
        got = self._mark('console.log(JSON.stringify([p0.el.className, p1.el.className]));')
        self.assertNotIn("hs-from-aside", got[0])
        self.assertIn("hs-from-aside", got[1])

    def test_空名单不画线(self):
        """通知落进上下文、引擎却没接着跑:那一段一块内容都没有,画线是无中生有。
        与重建那侧的 `if not parts: return` 同一个判据。"""
        got = self._mark('console.log(JSON.stringify(hsBlocks(t).map((x)=>x[0])));', ids="[]")
        self.assertNotIn("hs-status st-done hs-aside", got)

    def test_同一段不画两条(self):
        got = self._mark("""
hsAsideMark(v, { host: "msg_a_1", msg_id: "msg_a_1", ms: 12000, agent: AGENT,
                 mark: 1, part_ids: ["q1"], parts: [] });
console.log(JSON.stringify(hsBlocks(t).filter((x)=>x[0].indexOf("hs-aside")>=0).length));""")
        self.assertEqual(got, 1)

    def test_宿主认不出来就不画(self):
        """少一条线,好过画在错的回合下面。"""
        got = self._run(self.SETUP + """
const AG = AGENT;
hsAsideMark(v, { host: "msg_不存在", msg_id: "msg_不存在", ms: 1, agent: AG,
                 mark: 0, part_ids: ["q1"], parts: [] });
console.log(JSON.stringify(hsBlocks(t).map((x)=>x[0])));""")
        self.assertNotIn("hs-status st-done hs-aside", got)

    def test_耗时写定不再跳(self):
        got = self._mark("""
const line = hsBlocks(t).find((x)=>x[0].indexOf("hs-aside")>=0);
console.log(JSON.stringify(line[1]));""")
        self.assertIn("12s", got)

    def test_对不上子代理时换一句笼统的说法(self):
        """后台命令、MCP 任务跑完同样发通知,那些对不上子代理——不能说成某个子代理交回。"""
        got = self._mark("""
const line = hsBlocks(t).find((x)=>x[0].indexOf("hs-aside")>=0);
console.log(JSON.stringify(line[1]));""", agent="null")
        self.assertIn("处理后台任务的返回", got)
        self.assertNotIn("子代理", got)

    def test_认得出子代理的名字(self):
        got = self._mark("""
const line = hsBlocks(t).find((x)=>x[0].indexOf("hs-aside")>=0);
console.log(JSON.stringify(line[1]));""")
        self.assertIn("修回合未结束", got)
        self.assertIn("执行完成返回", got)

    # ---- 重放:从记录里认回来的那条 ----

    REPLAY = """
const EV = { host: "msg_a_1", msg_id: "msg_x_1", origin: "task-notification", ms: 12000,
             task_id: "x1", agent: AGENT,
             parts: [{ id: "r1", type: "text", text: "读完了" }] };
hsAsideRow(v, EV, true);
"""

    def test_重放接在记录给定的那一个回合下面(self):
        got = self._run(self.SETUP + self.REPLAY
                        + 'console.log(JSON.stringify([v.turns.length, hsBlocks(t).map((x)=>x[0])]));')
        self.assertEqual(got[0], 1, "重放不该新建回合")
        self.assertIn("hs-status st-done hs-aside", got[1])

    def test_重放的内容铺在线下面(self):
        got = self._run(self.SETUP + self.REPLAY
                        + 'console.log(JSON.stringify(hsBlocks(t).map((x)=>x[1])));')
        self.assertIn("读完了", got)

    def test_重放走的是同一条渲染(self):
        """线的样子、耗时的写法都与实时那条同源——两条路长成两个样子,读的人就得学两遍。"""
        a = self._mark("""
const line = hsBlocks(t).find((x)=>x[0].indexOf("hs-aside")>=0);
console.log(JSON.stringify(line));""")
        b = self._run(self.SETUP + self.REPLAY + """
const line = hsBlocks(t).find((x)=>x[0].indexOf("hs-aside")>=0);
console.log(JSON.stringify(line));""")
        self.assertEqual(a[0], b[0])   # 同一套类名
        self.assertEqual(a[1], b[1])   # 同一句话、同一个耗时


class AsideWiring(unittest.TestCase):
    """重放与实时要走进同一套渲染,不另做一个长得不一样的。"""

    def setUp(self):
                    self.src = _srcmap.chat_src()

    def test_重放把_info_aside_交给同一个函数(self):
        m = re.search(r"\nfunction hsPour\(.*?\n}\n", self.src, re.S)
        self.assertIsNotNone(m)
        self.assertIn("info.aside", m.group(0))
        self.assertIn("hsAsideRow(v,", m.group(0))

    def test_只有一个地方画这条线(self):
        self.assertEqual(len(re.findall(r"function hsAsideLine\(", self.src)), 1)
        self.assertEqual(len(re.findall(r"function hsAsideRow\(", self.src)), 1)

    def test_线下面复用普通回合的渲染(self):
        """不为自续轮另写一套:它用 hsPart 认块,用 hsPaint 落块。"""
        m = re.search(r"\nfunction hsAsideFill\(.*?\n}\n", self.src, re.S)
        self.assertIsNotNone(m)
        self.assertIn("hsPart(", m.group(0))
        self.assertIn("hsPaint(", m.group(0))

    def test_归属靠认领表不靠新开一轮(self):
        """hsTurnFor 按「最后一个回合还空着就认领」派活,而落点那一回合早已有 msgId——
        不认这一层,自续轮的每一块都会新开一轮。"""
        m = re.search(r"\nfunction hsTurnFor\(.*?\n}\n", self.src, re.S)
        self.assertIsNotNone(m)
        self.assertIn("asideOf", m.group(0))
        # 认领表指着那批回合对象,整页重建之后要跟着清
        m2 = re.search(r"\nfunction hsRebuild\(.*?\n}\n", self.src, re.S)
        self.assertIn("asideOf", m2.group(0))

    def test_旧那条路已经拆干净(self):
        """开场画线、内容整批搬过来、判错再撤回重画——这一版 2026-08-20 撤了。
        它与分段追认并存期间从别的口子冒出来过:同一段回答在用户消息上下各铺一遍,
        而且两遍排版不一样(一遍流式增量,一遍 hsAsideFill 整段成型)。
        留着任何一半,下次还会从别的地方冒出来。"""
        for gone in ("hsAsideClose", "hsAsideUndo", "hsAsideEnd", "hsAsideLamp"):
            self.assertNotIn("function %s(" % gone, self.src, gone + " 又回来了")
        self.assertNotIn("asideCur", self.src)
        self.assertNotIn("asideLast", self.src)
        # 活的那条路只认 mark 一种
        i = self.src.index('type === "session.aside"')
        seg = self.src[i:i + 900]
        self.assertIn('p.phase === "mark"', seg)
        for gone in ('phase === "close"', 'phase === "revoke"', 'phase === "open"'):
            self.assertNotIn(gone, seg, "还留着 " + gone)

    def test_自续轮的内容算进对账的口径(self):
        """它并进上一个回合、在界面上占着位置;记录那侧不数进去,每次收尾都会判成漂移。"""
        m = re.search(r"\nfunction hsMessageShape\(.*?\n}\n", self.src, re.S)
        self.assertIsNotNone(m)
        self.assertIn("info.aside", m.group(0))

    def test_分割线不算一次真人发言(self):
        """对账那道守卫按「人说过几句话」比对,把分割线数进去就会拦着不让重建。"""
        m = re.search(r"const dbAsk = [\s\S]*?;", self.src)
        self.assertIsNotNone(m)
        self.assertIn(".aside", m.group(0))


if __name__ == "__main__":
    unittest.main()
