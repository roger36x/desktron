"""回合的结束判据:用户这一轮只由它自己那条 result 收尾。

常驻进程跨回合复用,而 claude 并不只在我们发话时才跑:后台任务或子代理交回时,harness
往上下文里注入一条 task-notification,引擎就着它自己续一轮——读报告、判断下一步,有时接着
调工具。那是真在干活,只是不属于用户这一轮。

两种轮次的帧序一样(init … 内容 … result),init 帧的字段也一模一样;分得出来的是 result:
自续那一轮带 origin={"kind": "task-notification"},用户发起的那一轮 origin 为 null。
帧序照 2026-08-12 的实测抄——一个进程里让后台任务交回、同时从这侧发话,拿到的正是
notification → init → 内容 → result(origin) → init → 内容 → result(null)。

此前的判据是「见过一条 init 就开闸」,自续轮那条 init 于是替用户这一轮把闸开了,紧跟着它的
result 把回合判完:界面提前翻牌、耗时报的是自续轮的秒数,而用户这一轮真正的内容再没有人读,
刷新之后才从记录里整体出现(2026-08-12 两次实见)。

判据要区分的是「模型说完了一段话」与「用户这一轮结束了」:前者一回合里可以有多次
(正文 → 工具 → 再正文),后者只发生一次,且一定是本轮那条 origin 为空的 result。
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

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))

_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, ev):
        self.events.append(ev)

    def types(self):
        return [e.get("type") for e in self.events]

    def stats_ms(self):
        return [(e.get("properties") or {}).get("ms") for e in self.events
                if e.get("type") == "session.stats"]

    def asides(self):
        return [e.get("properties") or {} for e in self.events
                if e.get("type") == "session.aside"]


def _init(esid="eng-2"):
    return {"type": "system", "subtype": "init", "session_id": esid, "uuid": "u-init"}


def _notif(task="t1", status="completed"):
    return {"type": "system", "subtype": "task_notification", "task_id": task,
            "status": status}


def _result(ms, origin=None):
    o = {"type": "result", "subtype": "success", "duration_ms": ms,
         "usage": {"input_tokens": 1, "output_tokens": 1}, "origin": origin}
    return o


def _self_result(ms):
    return _result(ms, {"kind": "task-notification"})


def _text(idx, delta):
    return {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": idx,
        "delta": {"type": "text_delta", "text": delta}}}


def _message_start(model="claude-opus-5"):
    return {"type": "stream_event", "event": {
        "type": "message_start", "message": {"model": model}}}


def _tool(idx, name="Bash"):
    return {"type": "stream_event", "event": {
        "type": "content_block_start", "index": idx,
        "content_block": {"type": "tool_use", "id": "tu_%d" % idx, "name": name}}}


def _tool_result(text="ok"):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": [{"type": "text", "text": text}]}]}}


class TurnBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, persist=True):
        ctx = {"sid": self.sid, "msg_id": "msg_a_1", "parts": {}, "order": [],
               "last_tool": None, "t0": 0}
        if persist:
            ctx["persist"] = True
        return ctx

    def _feed(self, ctx, frames):
        """跑一遍读循环里的那一步,返回每一帧之后回合有没有被判结束。"""
        ends = []
        for obj in frames:
            self.ad.handle_event(obj, ctx)
            ends.append(bool(ctx.pop("_close_stdin", False)))
        return ends

    def _texts(self, ctx):
        return "".join(p.get("text", "") for p in ctx["parts"].values() if p["type"] == "text")

    def test_带工具的原生消息把说明迁入过程区(self):
        ctx = self._ctx()
        self._feed(ctx, [
            _init(), _message_start(), _text(0, "先检查当前状态。"), _tool(1),
            _tool_result(), _message_start(), _text(0, "检查完成。"), _result(100),
        ])
        parts = [ctx["parts"][pid] for pid in ctx["order"]]
        self.assertEqual([p["type"] for p in parts], ["reasoning", "tool", "text"])
        self.assertEqual(parts[0]["text"], "先检查当前状态。")
        self.assertEqual(parts[2]["text"], "检查完成。")
        moved = [e["properties"]["part"] for e in self.bus.events
                 if e.get("type") == "message.part.updated"
                 and (e.get("properties") or {}).get("part", {}).get("id") == parts[0]["id"]]
        self.assertTrue(moved)
        self.assertEqual(moved[-1]["type"], "reasoning")

    def test_没有工具的原生消息仍是主线正文(self):
        ctx = self._ctx()
        self._feed(ctx, [_init(), _message_start(), _text(0, "尚未完成，预计十分钟。"),
                         _result(100)])
        self.assertEqual([ctx["parts"][pid]["type"] for pid in ctx["order"]], ["text"])

    # ---- 自续轮 ----

    def test_compact_string_messages_are_not_tool_results(self):
        ctx = self._ctx(persist=False)
        tool = {'type': 'tool', 'state': {'status': 'running', 'input': {}}}
        ctx['parts']['tool'] = tool
        ctx['tuid'] = {'call-1': 'tool'}
        for role, content in (
            ('user', '<command-name>/compact</command-name>'),
            ('user', 'Summary of the previous conversation'),
            ('assistant', '<local-command-stdout>Compacted</local-command-stdout>'),
        ):
            self.ad.handle_event({'type': role, 'message': {'role': role, 'content': content}}, ctx)
        self.assertEqual(tool['state'], {'status': 'running', 'input': {}})
        self.assertFalse(ctx.get('_close_stdin'))
        self.assertEqual(self._texts(ctx), '')
        self.ad.handle_event({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'call-1', 'content': 'done'}]}}, ctx)
        self.assertEqual(tool['state']['output'], 'done')
        self.assertEqual(tool['state']['status'], 'done')
        self.ad.handle_event(_result(1), ctx)
        self.assertTrue(ctx['_close_stdin'])

    def test_自续轮不结束用户这一轮(self):
        """预告 → 自续轮 → 用户这一轮。只有 origin 为空的那条 result 才收尾。

        这条守的一直是回合边界:自续轮那条 result 带 origin,它只追认前面那一段的归属,
        不划边界,所以本回合的耗时与用量照用户自己那条(2498),自续轮的 3287 毫秒不冒充
        本回合的耗时,界面也不会提前翻牌。

        2026-08-18 判据改到 result 上之后,内容一律留在本回合的 parts 里(影子 ctx 取消),
        「用户这一轮只剩自己那段文本」这个断言因此不再成立——两段文本都在,哪一段属于
        子代理交回由 mark 那条事件的分界下标说明。两段各用一个 content index:实测里每条
        API 消息各有自己的 message_start,index 会重置,两段的块本来就不是同一块。"""
        ctx = self._ctx()
        ends = self._feed(ctx, [
            _notif(),                      # 后台任务交回,引擎要自己续一轮了
            _init(), _text(0, "子代理说的我看过了"), _self_result(3287),
            _init(), _text(1, "这是给用户的回答"), _result(2498),
        ])
        self.assertEqual(ends, [False] * 6 + [True])
        self.assertEqual(ctx["ms"], 2498)
        self.assertEqual(self.bus.stats_ms(), [2498])
        # 内容不搬家:两段都在本回合的消息里,中途刷新看到的就是这一份
        self.assertEqual(self._texts(ctx), "子代理说的我看过了这是给用户的回答")
        # 追认只盖住分界之前那一段,用户这一轮的那块不在其中
        row = self.bus.asides()[0]
        self.assertEqual(row["phase"], "mark")
        self.assertEqual(row["part_ids"], ctx["order"][:1])
        self.assertEqual(ctx["seg_start"], 1)     # 分界前移,余下那段归用户

    def test_通知之后引擎没另起轮_预告不许套到下一轮(self):
        """真实帧序(2026-08-18 从六份逐帧轨迹核出来的,不是照理想序编的)。

        harness 注入 task-notification 之后,引擎【不一定】另起一轮:实测五份轨迹里通知
        之后一直没有新的 init——它把通知并进当前这一轮接着跑。于是预告没有消费者,本回合
        跑完仍旧挂在适配器上,而下一回合的第一条 init 恰恰是用户的开场。老机制正是在那里
        消费预告,把用户整轮收进影子 ctx:界面上用户自己的回答挂到了上一回合下面,要等这
        一轮跑完撤回才跳回原位。

        新机制里 init 不参与归属判定,这条因此守「陈旧预告落不到用户这一轮上」:两个回合
        各由自己那条 origin 为空的 result 收尾,一条 session.aside 都不发,内容各自留在
        自己回合的消息里,live 快照照写。
        """
        ctx1 = self._ctx()
        ends = self._feed(ctx1, [_init(), _text(0, "派个子代理去查"),
                                 _notif(),                # 交回了,但引擎没另起一轮
                                 _text(0, "顺手把结果说了"), _result(1000)])
        self.assertEqual(ends, [False] * 4 + [True])      # 通知不打断,回合照自己那条收尾
        self.assertIsNone(ctx1.get("asides"))             # 没有自续轮,也就没有线要画
        self.assertEqual(self.bus.asides(), [])
        # 预告没有消费者:这一回合的读循环里它一直挂在适配器上(回合收尾才清,见 _finish_turn)
        self.assertEqual((self.ad._aside_hint.get(self.sid) or {}).get("task"), "t1")

        # 用户发下一条消息:新回合、新 ctx,而预告还在手上
        ctx2 = self._ctx()
        ctx2["msg_id"] = "msg_a_2"
        ends = self._feed(ctx2, [_init(), _text(0, "用户这一轮的回答"), _result(500)])
        self.assertEqual(ends, [False, False, True])
        self.assertEqual(ctx2["ms"], 500)
        self.assertEqual(self.bus.asides(), [])           # 第一条 init 没被这条预告套走
        self.assertIsNone(ctx2.get("asides"))
        self.assertEqual(self._texts(ctx2), "用户这一轮的回答")
        self.assertEqual({p["messageID"] for p in ctx2["parts"].values()}, {"msg_a_2"})
        # 中途刷新看得见:内容照写 live 快照(误套期间恰恰是一个字都看不到)
        snap = self.ad._read_live_checkpoint(self.sid)
        self.assertEqual([p.get("text") for p in snap["parts"]], ["用户这一轮的回答"])

    def test_同一回合里的第二条init不判归属只把预告收下(self):
        """本回合已经见过 init,之后又来一条:它【不划边界、不判归属】,只把预告收下备用。

        init 帧两轮一模一样,拿它猜谁是谁正是从前那些错位的来源;归属一律等 result 那时
        origin 说了算。这条守的是这一段的两个性质:第二条 init 既不给回合收尾、也不把段落
        切开(追认仍从第 0 块起),而预告从适配器挪进本回合的 ctx,追认时靠它写出身份。
        """
        ctx = self._ctx()
        self._feed(ctx, [_init(), _text(0, "派个子代理去查"), _notif("t7")])
        ends = self._feed(ctx, [_init()])                  # 同一回合里的第二条 init
        self.assertEqual(ends, [False])                    # 它不是回合边界
        self.assertEqual((ctx.get("aside_hint_at") or {}).get("task"), "t7")
        self.assertNotIn(self.sid, self.ad._aside_hint)    # 预告已从适配器上取走
        ends = self._feed(ctx, [_text(1, "报告读完了"), _self_result(900)])
        self.assertEqual(ends, [False, False])             # 追认不收本回合的场
        row = self.bus.asides()[0]
        self.assertEqual(row["phase"], "mark")
        self.assertEqual(row["mark"], 0)                   # 第二条 init 没有把段落切开
        self.assertEqual(row["part_ids"], ctx["order"])
        self.assertEqual(row["task_id"], "t7")             # 身份取自收下的那条预告

    def test_自续轮的内容留着且带来路(self):
        """它是引擎在处理子代理的交回,不是噪音——内容留着,来路记下来,能指出是哪几块。

        原来「归属」靠搬家:那几块收进影子 ctx,row 里带一份 parts 的副本。分界点方案下
        内容不动窝,一直在本回合的 parts 里,归属改由追认表达——mark 那条 row 用 part_ids
        点名从哪一块起是子代理交回引发的。同一个意图因此从两侧断言:row 点到的正是那两块
        (文本 + 工具),而那两块本身仍在 ctx["parts"] 里、挂着本回合的 messageID。"""
        ctx = self._ctx()
        self._feed(ctx, [_notif(), _init(), _text(0, "报告读完了"), _tool(1),
                         _self_result(3287), _init(), _text(2, "答复"), _result(2498)])
        asides = ctx["asides"]
        self.assertEqual(len(asides), 1)
        self.assertEqual(asides[0]["origin"], "task-notification")
        self.assertEqual(asides[0]["ms"], 3287)
        self.assertEqual(asides[0]["mark"], 0)          # 本回合第一块起就是自续轮的
        pids = asides[0]["part_ids"]
        self.assertEqual(pids, ctx["order"][:2])
        self.assertEqual([ctx["parts"][k]["type"] for k in pids], ["reasoning", "tool"])
        # 内容留在本回合的助手消息上,不再另挂一个影子 messageID
        self.assertEqual({ctx["parts"][k]["messageID"] for k in pids}, {ctx["msg_id"]})
        # 用户那一段不在追认范围里:它是分界之后新长出来的块
        self.assertNotIn(ctx["order"][2], pids)
        row = self.bus.asides()[0]
        self.assertEqual(row["phase"], "mark")
        self.assertEqual(row["origin"], "task-notification")
        self.assertEqual(row["part_ids"], pids)

    def test_自续轮照发事件也写进live快照(self):
        """事件要发:自续轮的内容得能边跑边显。快照这一半反过来了,内容【要】进 live 快照。

        原来不写快照,是因为那段内容收在影子 ctx 里:快照一个会话只有一份、讲的是用户
        这一轮跑到哪儿了,两轮同写会让刷新后的重放在两份之间跳。分界点方案下没有第二份
        ctx,内容就是本回合的内容,快照里少了它就是「误套期间中途刷新一个字都看不到」
        (旧机制的实见症状)。所以这里断言的是它在:快照里有那一块,挂的是本回合的
        messageID,不是影子那个 <msg_id>_aside0。"""
        ctx = self._ctx()
        self._feed(ctx, [_notif(), _init(), _text(0, "报告读完了"), _self_result(3287)])
        self.assertIn("message.part.delta", self.bus.types())
        snap = self.ad._read_live_checkpoint(self.sid)
        self.assertIsNotNone(snap)
        self.assertEqual("".join(p.get("text", "") for p in snap["parts"]
                                 if p["type"] == "text"), "报告读完了")
        self.assertEqual(snap["info"]["id"], ctx["msg_id"])
        # 事件里的块也挂本回合的 messageID:前端不必先认一条影子消息再把它并回来
        mids = {(e.get("properties") or {}).get("messageID")
                for e in self.bus.events if e.get("type") == "message.part.delta"}
        self.assertEqual(mids, {ctx["msg_id"]})

    def test_自续轮的信号只在追认时发一条(self):
        """信号从「开场一条 + 收场一条」变成「追认一条」,顺序也跟着反过来。

        原来的意图是让分割线在自续轮跑起来那一刻就位,免得那一段整块出现、一出现就标着
        「已完成」。代价是必须在 init 上前瞻性地判归属,而 init 帧两轮一模一样:判错就是
        整轮错位,只能等下一条 result 撤回重画——用户看到的「回答挂到上一回合、要等这一轮
        跑完才跳回原位」正是那个撤回。所以开场这件事是有意放弃的,不是漏掉的。

        新机制只发一条,在 result 到达时追认:内容此前已经边跑边显在本回合里,追认补的
        是分界与出处,晚一点到不影响读。这里断言的就是这一条——恰好一条 mark、排在内容
        之后、带本回合的 msg_id 与来路。"""
        ctx = self._ctx()
        self._feed(ctx, [_notif(), _init(), _text(0, "报告读完了"), _self_result(3287)])
        rows = self.bus.asides()
        self.assertEqual([r["phase"] for r in rows], ["mark"])
        self.assertEqual(rows[0]["msg_id"], ctx["msg_id"])   # 不再有影子消息 _aside0
        self.assertEqual(rows[0]["origin"], "task-notification")
        self.assertEqual(rows[0]["part_ids"], ctx["order"])  # 这一整段都是它的产出
        # 追认排在内容之后:内容先长出来,线后补在它上面
        types = self.bus.types()
        self.assertGreater(types.index("session.aside"), types.index("message.part.delta"))

    def test_收场帧没等到也没有悬挂要补(self):
        """2026-08-13 回归。进程被杀、回合超时、用户按下停止(那时归属闸整个跳过),
        自续轮的 result 再也不会来。那时内容收在影子 ctx 里,界面据收场信号把宿主回合的灯
        从「处理中」改回去,信号不来就一直停在那儿,读起来像两个回合同时在跑,所以回合收尾时
        替它补一条收场。

        分界点方案下这条兜底没有对象了:内容一律留在本回合的 parts 里,追认只发生在自续轮
        那条 result 到达时。收场帧不来 = 不追认 = 少一条分割线——既没有开着的影子状态要收,
        也没有停在「处理中」的灯要灭。断言因此换成「没有悬挂」:一条 session.aside 都没有,
        内容在本回合、也在 live 快照里(中途刷新看得见,旧机制误套期间恰恰是一个字都没有),
        回合收尾照常把它落盘。"""
        ctx = self._ctx()
        self._feed(ctx, [_notif(), _init(), _text(0, "报告读完了")])   # 收场帧没等到
        self.assertIsNone(ctx.get("aside"))        # 不再有影子 ctx,也就没有「还开着」这回事
        self.assertEqual(self.bus.asides(), [])    # 线没画出去,不必再补一条把它收掉
        self.assertEqual(self._texts(ctx), "报告读完了")
        snap = self.ad._read_live_checkpoint(self.sid)
        self.assertEqual([p.get("text") for p in snap["parts"]], ["报告读完了"])
        self.ad._finish_turn(self.sid, ctx, None)
        self.assertEqual(self.bus.asides(), [])
        stored = self.ad.messages(self.sid)[-1]    # 内容不丢:随本回合的助手消息落盘
        self.assertEqual([p["text"] for p in stored["parts"]], ["报告读完了"])
        self.assertNotIn(self.sid, self.ad._aside_hint)   # 预告不跨回合,见 _finish_turn 末尾

    def test_按下停止那一路也没有线要补(self):
        """按下停止之后归属闸整个跳过(见 handle_event 的 `not ctx.get("aborted")`),
        带 origin 的 result 不再被当成自续轮的收尾,而是照常给这一回合收尾。

        旧机制里这正是「影子 ctx 永远收不了场」的那一路:线已经画出去,收场信号却没人发,
        只能等 _finish_turn 补。分界点方案下这一帧不触发追认,内容本来就在本回合,
        少的只是一条分割线,没有要补的东西——断言改成停止之后一条 session.aside 都不再出现,
        并且已经跑出来的内容仍旧落盘。"""
        ctx = self._ctx()
        self._feed(ctx, [_notif(), _init(), _text(0, "读了一半")])
        ctx["aborted"] = "interrupt"
        ends = self._feed(ctx, [_self_result(900)])
        self.assertEqual(ends, [True])            # 闸跳过,这一帧直接给回合收尾
        self.assertIsNone(ctx.get("aside"))
        self.assertEqual(self.bus.asides(), [])
        self.ad._finish_turn(self.sid, ctx, None)
        self.assertEqual(self.bus.asides(), [])
        stored = self.ad.messages(self.sid)[-1]   # 打断保留已经跑出来的内容
        self.assertEqual([p["text"] for p in stored["parts"]], ["读了一半"])

    def test_正常收尾不会补第二条(self):
        """自续轮那条 result 已经追认过一次,回合收尾不该再发一条:界面按 part_ids 铺线,
        多一条就多一段重复的分割线,而它盖住的还是同几块内容。"""
        ctx = self._ctx()
        self._feed(ctx, [_notif(), _init(), _text(0, "读完了"), _self_result(3287),
                         _init(), _text(0, "答复"), _result(2498)])
        self.assertEqual([r["phase"] for r in self.bus.asides()], ["mark"])
        self.ad._finish_turn(self.sid, ctx, None)
        self.assertEqual([r["phase"] for r in self.bus.asides()], ["mark"])
        self.assertEqual(len(ctx["asides"]), 1)

    # ---- 顺序与来路 ----

    def test_自续轮的落位由后端给(self):
        """界面仍然不猜这条线该挂在谁下面,后端照旧给全宿主、序号、来路——只是宿主换了人。

        原来宿主是上一个回合的助手消息:那时内容收在影子 ctx 里,与本回合无关,只能跨回合
        去认。分界点方案下内容就长在本回合的消息上,宿主因此恒为本回合的 msg_id。这样改
        还去掉了一个隐患:刷新或对账重建之后,落盘消息的 id 换成 uuid、seq 归零,跨回合
        那两把钥匙同时失效,线就画不出来了(见 _aside_mark_close)。
        三样仍旧缺一不可——缺了界面就只能回到「末尾那一轮」那种猜法。"""
        ctx = self._ctx()
        ctx["prev"] = {"msg_id": "msg_a_0", "seq": 41}
        ctx["seq"], ctx["source"] = 42, "user"
        self._feed(ctx, [_notif(), _init(), _text(0, "报告读完了"), _self_result(3287)])
        rows = self.bus.asides()
        self.assertEqual([r["phase"] for r in rows], ["mark"])
        for r in rows:
            self.assertEqual(r["host"], ctx["msg_id"])    # 宿主是本回合,内容就在这条消息里
            self.assertEqual(r["msg_id"], ctx["msg_id"])
            self.assertEqual(r["seq"], 42)                # 本回合的序号,不再借上一轮的
            self.assertEqual(r["source"], "task-notification")
        # 用户这一轮自己的消息事件带的是它自己那一份
        mine = [(e.get("properties") or {}).get("info") for e in self.bus.events
                if e.get("type") == "message.updated"]
        self.assertEqual([(i["seq"], i["source"]) for i in mine if i["id"] == ctx["msg_id"]],
                         [(42, "user")])

    def test_没赶上开场的那一路也带宿主(self):
        """自续轮的开头落在上一回合的读循环里(通知帧没赶上),这一轮从半截开始:
        init → 内容 → 带 origin 的 result。追认照样发生,分界下标为 0——本回合到此为止
        这一整段都是子代理交回引发的。这一路此前只发一条连 msg_id 都没有的收场帧,
        界面无从判断该挂在谁下面,只能按「末尾那一轮」猜,而那一刻末尾往往是用户刚发的提问。

        落位仍旧由后端给,只是宿主换了:内容留在本回合的 parts 里,线就画在本回合自己身上
        (host 恒为本回合 msg_id)。旧机制把内容收进影子 ctx,才要跨回合去认上一轮的 msg_id
        与 seq,而刷新或对账重建之后落盘消息的 id 会换成 uuid、seq 归零,那两把钥匙同时失效,
        线反倒画不出来。"""
        ctx = self._ctx()
        ctx["prev"] = {"msg_id": "msg_a_0", "seq": 41}
        ctx["seq"], ctx["source"] = 42, "user"
        self._feed(ctx, [_init(), _text(0, "自续轮的话"), _self_result(3287)])
        row = self.bus.asides()[0]
        self.assertEqual(row["phase"], "mark")
        self.assertEqual(row["mark"], 0)
        self.assertEqual(row["part_ids"], ctx["order"])    # 这一段整个归自续轮
        self.assertEqual(row["msg_id"], ctx["msg_id"])
        self.assertEqual(row["host"], ctx["msg_id"])       # 宿主是本回合,不再指向上一轮
        self.assertEqual(row["seq"], 42)
        self.assertEqual(row["source"], "task-notification")
        self.assertEqual(self._texts(ctx), "自续轮的话")   # 内容不搬家,还在本回合

    def test_并进用户这一轮的通知不留一条线(self):
        """预告在手不等于引擎真的自续了一轮。2026-08-16 的逐帧轨迹里,通知在上一回合内到达、
        用户紧跟着发话的那几次,引擎把通知并进用户这一轮里跑:整段只有一条 init、一条 origin
        为空的 result。此前那条 result 被当成自续轮的收场吃掉——回合边界永远等不到,读循环
        挂到看门狗超时(实见一次 15 分钟);界面那侧则是用户这一轮的全部内容长在一条
        「处理后台任务的返回」之下,要等 origin 为空的 result 触发撤回才跳回原位。

        新机制不再拿预告去前瞻地判归属,归属一律等 result 说明:这一轮没有带 origin 的
        result,追认就一次也不发生。断言因此从「open 之后跟一条 revoke」改成「一条
        session.aside 都没有」——线没画出去,谈不上撤回,内容也不必照本回合再重铺一遍。"""
        ctx = self._ctx()
        ctx["prev"] = {"msg_id": "msg_a_0", "seq": 41}
        ctx["seq"], ctx["source"] = 42, "user"
        ends = self._feed(ctx, [_notif(), _init(), _text(0, "给用户的回答"), _result(2498)])
        self.assertEqual(ends, [False, False, False, True])   # 回合边界照常划得出来
        self.assertEqual(ctx["ms"], 2498)
        self.assertIsNone(ctx.get("aside"))
        # 这一轮没有带 origin 的 result,一次追认都不发生:线压根没画出去,也就没有要撤的
        self.assertEqual(ctx.get("asides"), None)
        self.assertEqual(self.bus.asides(), [])
        # 内容自始至终挂在本回合的助手消息上,不曾搬去别处再搬回来
        self.assertEqual(self._texts(ctx), "给用户的回答")
        self.assertEqual({p["messageID"] for p in ctx["parts"].values()}, {ctx["msg_id"]})
        self.assertTrue(all(not p.get("origin") for p in ctx["parts"].values()))
        # 中途刷新看得见:内容一直写进 live 快照,不再因为被当成自续轮而跳过
        snap = self.ad._read_live_checkpoint(self.sid)
        self.assertEqual([p.get("text") for p in snap["parts"]], ["给用户的回答"])

    def test_没赶上预告也不被自续轮收尾(self):
        """通知帧落在上一回合的读循环里时没有预告,只能靠 result 的 origin 兜底:
        它一样不结束这一轮,并把闸重新扣上,接着等我们自己那条 init。"""
        ctx = self._ctx()
        ends = self._feed(ctx, [
            _init(), _text(0, "自续轮的话"), _self_result(3287),
            _init(), _text(0, "给用户的答复"), _result(2498),
        ])
        self.assertEqual(ends, [False] * 5 + [True])
        self.assertEqual(ctx["ms"], 2498)
        self.assertEqual([a["origin"] for a in ctx["asides"]], ["task-notification"])

    # ---- 上一轮的残帧 ----

    def test_上一轮残留的帧不结束这一回合(self):
        ctx = self._ctx()
        ends = self._feed(ctx, [
            _text(0, "上一轮的尾巴"),      # 读循环停在 result 处,后面的帧留在管道里
            _result(50800),                # 它的收尾帧:此前就是被这条判死的
            _init(),                       # 本回合从这里开始
            _text(0, "这一回合的正文"),
            _result(129487),
        ])
        self.assertEqual(ends, [False, False, False, False, True])
        self.assertEqual(ctx["ms"], 129487)
        self.assertEqual(self.bus.stats_ms(), [129487])
        self.assertEqual(self._texts(ctx), "这一回合的正文")

    # ---- 一个回合内部 ----

    def test_正文出现多次也不算结束(self):
        """正文 → 工具 → 再正文是一个回合内的常态,只有 result 才是回合结束。"""
        ctx = self._ctx()
        ends = self._feed(ctx, [
            _init(), _text(0, "先说一段"), _tool(1), _text(2, "再说一段"), _result(21100),
        ])
        self.assertEqual(ends, [False, False, False, False, True])

    def test_同步派出的子代理照旧(self):
        """同步那一路阻塞在工具调用里,不产生自续轮(TUI 那侧也是这样),判据别把它卷进来。"""
        ctx = self._ctx()
        ends = self._feed(ctx, [
            _init(), _tool(0, "Agent"), _tool_result("子代理的报告"),
            _text(1, "报告收到"), _result(21700),
        ])
        self.assertEqual(ends, [False, False, False, False, True])
        self.assertEqual(ctx.get("asides"), None)
        self.assertEqual(self._texts(ctx), "报告收到")

    # ---- 边界 ----

    def test_一回合一进程那条路不设闸(self):
        """进程是新起的,管道里不可能有别人的帧;万一某一轮没有 init,也不该挂到超时。"""
        ctx = self._ctx(persist=False)
        ends = self._feed(ctx, [_text(0, "正文"), _result(9000)])
        self.assertEqual(ends, [False, True])

    def test_按了停止就不再等init(self):
        """中断的回应未必排在 init 之后,拦掉就没人给回合收尾了。"""
        ctx = self._ctx()
        ctx["aborted"] = "interrupt"
        ends = self._feed(ctx, [_result(3000)])
        self.assertEqual(ends, [True])

    def test_下一回合重新配对(self):
        """闸是按回合算的:新回合的 ctx 又要等它自己那条 init。"""
        first = self._ctx()
        self._feed(first, [_init(), _result(1000)])
        second = self._ctx()
        ends = self._feed(second, [_result(1000), _init(), _result(2000)])
        self.assertEqual(ends, [False, False, True])


class AsideHintAcrossTurns(unittest.TestCase):
    """预告帧未必落在轮次间隙。

    派出后台子代理的那一轮里,task_notification 常先于本轮 result 到达(2026-08-15 实测:
    init → tool_use(Agent) → … → notification → result(null) → init → … → result(origin))。
    此前预告只在轮次间隙收、记在回合自己的 ctx 上——回合一结束预告就丢,下一条 init 被
    错认成用户轮:用户的回合整轮错收进影子(不写 live 快照,刷新后回复消失,只剩他那条
    消息孤零零在那儿),真正的自续轮 result 又把闸重新扣上,读循环从此等一条不会再来的
    收尾帧,900 秒后被看门狗杀进程。预告因此挂在适配器上跨回合传。

    上面那一段是当时的解法,它本身也被推翻了:2026-08-18 用六份逐帧轨迹核过,预告挂着
    跨回合传之后,3884 帧自续轮态【全部】套在本回合第一条 init 上,而那正是用户轮的开场
    ——症状换成了用户的回答挂到上一回合下面,要等这一轮跑完撤回才跳回原位。归属因此从
    init 挪到 result:origin 是引擎明说的,回溯认定不必猜。预告退回只管身份(哪个子代理
    交回),不再跨回合——TTL 收到 30 秒,回合收尾时清掉。

    这个类下两条用例守的就是这一段:预告落在回合中间时,跨回合那一刀切在哪(下一条);
    以及手上还攥着一条没人配对的预告时,用户这一轮照常走完(再下一条)。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self):
        return {"sid": self.sid, "msg_id": "msg_a_1", "parts": {}, "order": [],
                "last_tool": None, "t0": 0, "persist": True}

    def _feed(self, ctx, frames):
        ends = []
        for obj in frames:
            self.ad.handle_event(obj, ctx)
            ends.append(bool(ctx.pop("_close_stdin", False)))
        return ends

    def test_回合中到的预告_自续轮落到下一回合照样追认(self):
        """实测帧序整段过一遍:派出的那一轮内到达预告,自续轮落在下一回合的读循环里。

        这条用例守的是跨回合那一刀切在哪。老机制靠预告跨回合传来判归属,断言写的是
        「身份没有因跨回合而丢」;新机制里预告不再承担归属(它连回合都不跨了,收尾即清),
        归属由 result 的 origin 回溯认定,所以同一个意图改成断言追认本身:第一回合由它
        自己那条 origin 为空的 result 收尾;下一回合里自续轮那条 result 只追认它前面那一段,
        不收回合的场;末条 result 才收。

        追认的形态一并守住,它跟老机制正相反:内容【留在本回合的 parts 里】,不再搬进影子
        ctx,事件只说明「从第几块起是子代理交回的」——phase=mark、mark 是分界下标、
        part_ids 是那几块、host 恒为本回合的 msg_id(不必跨回合去认上一轮的消息)。
        no_live 也一并作废:那一段现在就该写进 live 快照,中途刷新看得见。
        """
        first = self._ctx()
        ends = self._feed(first, [_init(), _text(0, "已派出"), _notif(), _result(1200)])
        self.assertEqual(ends, [False, False, False, True])   # 预告不打断本轮
        self.assertIsNone(first.get("asides"))                # 也不凭空给本轮添一条线
        self.assertTrue(self.ad._aside_hint.get(self.sid))    # 身份先记在适配器上
        self.ad._finish_turn(self.sid, first, None)
        # 预告不跨回合:回合收尾就清。留着它只会误伤——下一回合的第一条 init 是用户的开场。
        self.assertNotIn(self.sid, self.ad._aside_hint)

        second = self._ctx()
        second["msg_id"] = "msg_a_2"
        ends = self._feed(second, [
            _init(), _text(0, "已收到"), _self_result(3300),   # 自续轮:这条 result 只追认前一段
            # 第二段是用户的答复。实际帧序里它是新的一条 API 消息(message_start 让块 id 进位),
            # 这里的辅助函数不发 message_start,换一个 content index 表达同一件事。
            _init(), _text(1, "给用户的答复"), _result(2400),
        ])
        self.assertEqual(ends, [False] * 5 + [True])
        self.assertEqual(second["ms"], 2400)
        self.assertIsNone(second.get("aside"))                 # 没有影子 ctx 这回事了
        aside_pid, user_pid = second["order"]
        rows = self.bus.asides()
        self.assertEqual([r["phase"] for r in rows], ["mark"])         # 开场信号没了,只有追认
        self.assertEqual(rows[0]["mark"], 0)                           # 分界在段首
        self.assertEqual(rows[0]["part_ids"], [aside_pid])             # 只认前面那一段
        self.assertEqual(rows[0]["origin"], "task-notification")
        self.assertEqual(rows[0]["ms"], 3300)
        self.assertEqual(rows[0]["host"], second["msg_id"])            # 落位由后端给,恒为本回合
        self.assertEqual([a["part_ids"] for a in second["asides"]], [[aside_pid]])
        # 内容一律收在本回合:两段都在 parts 里,都挂本回合这条助手消息
        self.assertEqual([second["parts"][aside_pid]["text"], second["parts"][user_pid]["text"]],
                         ["已收到", "给用户的答复"])
        self.assertEqual({p["messageID"] for p in second["parts"].values()}, {"msg_a_2"})
        # 自续轮那一段也进 live 快照:中途刷新看得见(老机制那侧的 no_live 已废)
        snap = self.ad._read_live_checkpoint(self.sid)
        self.assertIn(aside_pid, [p["id"] for p in snap["parts"]])

    def test_预告过期不套眼前的init(self):
        """自续轮随进程换代丢了,预告没人配对。拿它套下一条用户的 init,会把用户的回合
        整轮错收进影子——比丢一条分割线重得多。

        老机制靠时效兜这一手(过了 TTL 就放弃);新机制里 init 【完全不参与归属判定】,
        新鲜也好陈旧也好都套不上——判据只看 result 的 origin,这一轮 origin 为空,
        就是用户自己的一轮。守的意图没变,断言换成正面描述那一轮:零追认(一条
        session.aside 都不发)、内容照常挂本回合的消息、live 快照照写。最后这条
        跟老机制正相反:误套期间不写快照,中途刷新一个字都看不到,那正是当时的症状。

        预告的处置也换了地方:不再由 init 用掉,而是回合收尾时清(见 _finish_turn 末尾),
        TTL 从 300 秒收到 30 秒——自续轮的 init 紧跟通知帧,同一进程里隔不了几秒。
        """
        self.ad._aside_hint[self.sid] = {"task": "t9", "status": "completed",
                                         "at": __import__("time").time() - 400}
        ctx = self._ctx()
        ends = self._feed(ctx, [_init(), _text(0, "正常的回合"), _result(900)])
        self.assertEqual(ends, [False, False, True])
        self.assertEqual(ctx.get("asides"), None)
        self.assertEqual(self.bus.asides(), [])          # 线一条都没画:既没追认,也没有开场
        # 内容归本回合,不带来路;快照照写
        self.assertEqual({p["messageID"] for p in ctx["parts"].values()}, {ctx["msg_id"]})
        self.assertTrue(all(not p.get("origin") for p in ctx["parts"].values()))
        snap = self.ad._read_live_checkpoint(self.sid)
        self.assertEqual([p.get("text") for p in snap["parts"]], ["正常的回合"])
        # 预告没被这条 init 用掉(它压根不看),留到回合收尾一起清,不污染后面的回合
        self.assertIn(self.sid, self.ad._aside_hint)
        self.ad._finish_turn(self.sid, ctx, None)
        self.assertNotIn(self.sid, self.ad._aside_hint)

    def test_预告帧本身不进正文(self):
        ctx = self._ctx()
        self._feed(ctx, [_init(), _notif(), _text(0, "正文"), _result(800)])
        self.assertEqual("".join(p.get("text", "") for p in ctx["parts"].values()
                                 if p["type"] == "text"), "正文")


class TurnSeq(unittest.TestCase):
    """回合的顺序与来路由后端给。

    用户发的消息走 HTTP 进 _run,子代理交回走注入产生自续轮,两条路在后端本来就是分开的,
    顺序也由后端决定。界面此前只能靠「末尾那一回合长什么样」去猜一条内容属于哪一轮——
    排队的消息与子代理交回撞在同一刻时怎么猜都不准。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.sid = self.ad.create_session()["id"]
        self.ctxs = []
        # 不起引擎进程:这几条看的是回合入口那一段,跑不跑得起来是另一件事。
        self.ad._turn_dispatch = lambda ctx, cmd, env, uframe: self.ctxs.append(ctx)

    def tearDown(self):
        self.tmp.cleanup()

    def _users(self):
        got = []
        for e in self.bus.events:
            info = (e.get("properties") or {}).get("info") or {}
            if e.get("type") == "message.updated" and info.get("role") == "user":
                got.append(info)
        return got

    def test_用户消息带序号与来路(self):
        import time as _t
        t0 = int(_t.time() * 1000)
        self.ad._run(self.sid, "第一问")
        self.ad._run(self.sid, "第二问")
        u = self._users()
        self.assertEqual([i["source"] for i in u], ["user", "user"])
        self.assertLess(u[0]["seq"], u[1]["seq"])     # 单调递增
        # 序号以毫秒时刻为下界:纯计数器在后端重启后从头开始,新回合的 1 号会认到
        # 记录里旧回合的 1 号上,自续轮于是挂到一条几天前的回合下面。
        self.assertGreaterEqual(u[0]["seq"], t0)

    def test_自续轮的宿主是上一个回合(self):
        self.ad._run(self.sid, "第一问")
        self.ad._run(self.sid, "第二问")
        first, second = self.ctxs[0], self.ctxs[1]
        self.assertIsNone(first["prev"])              # 会话头一轮没有上一轮
        self.assertEqual(second["prev"]["msg_id"], first["msg_id"])
        self.assertEqual(second["prev"]["seq"], first["seq"])
        self.assertNotEqual(second["msg_id"], first["msg_id"])

    def test_助手消息也带同一份序号与来路(self):
        """一条内容属于哪一轮,前端要在消息事件上就看得到,不能只在分割线那条事件上有。"""
        self.ad._run(self.sid, "第一问")
        ctx = self.ctxs[0]
        self.ad._ensure_part(ctx, "p0", "text")
        info = [(e.get("properties") or {}).get("info") for e in self.bus.events
                if e.get("type") == "message.updated"][-1]
        self.assertEqual(info["id"], ctx["msg_id"])
        self.assertEqual(info["seq"], ctx["seq"])
        self.assertEqual(info["source"], "user")


ESID = "ed6d6e9e-2020-4f58-89a2-5750d28db8a4"
CHAT_JS = os.path.join(ROOT, "frontend", "app-chat.js")


class AsideIdentity(unittest.TestCase):
    """自续轮是谁交回引发的。

    界面上那条分割线要说出「哪个子代理 · 怎么收的场 · 自己跑了多久」,而 init 帧两轮
    一模一样,身份只能取自它前面那条预告帧的 task_id——它与子代理记录文件名 agent-<id>.jsonl
    里的 id 是同一个,据此对到 subagents() 那一条。后台命令与 MCP 任务发的是同一种通知,
    对不上任何子代理,那时不硬安一个名字。

    2026-08-18 归属判定挪到 result 上之后,身份也跟着挪:一段自续轮只发一条 phase="mark"
    的追认事件,名字、收场方式、时长都在那一条里写全,没有开场那条先占位、收场那条再补名。
    帧序照实测的甲(通知 → 自续那一段 → 用户那一段)。这几条里 init 一律带 ESID:子代理的
    记录摆在引擎会话名下的目录里,而 init 帧会把 engine_sid 滚成它自报的那个,帧里写别的 id,
    subagents() 就翻到一个空目录去,身份一律对不上——那是帧的毛病,不是判据的。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus()
        self.path = os.path.join(self.tmp.name, ESID + ".jsonl")
        self.dir = os.path.join(self.tmp.name, ESID, "subagents")
        os.makedirs(self.dir)
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.sid = self.ad.create_session()["id"]
        self.ad.index[self.sid]["engine_sid"] = ESID
        self.ad._transcript_path = lambda esid: self.path

    def tearDown(self):
        self.tmp.cleanup()

    def _agent(self, aid, tuid, started_iso, desc="修回合未结束却显示已完成"):
        p = os.path.join(self.dir, "agent-%s.jsonl" % aid)
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "timestamp": started_iso,
                                "message": {"content": "去修一件事"}}, ensure_ascii=False) + "\n")
        with open(p[:-len(".jsonl")] + ".meta.json", "w", encoding="utf-8") as f:
            json.dump({"agentType": "general-purpose", "toolUseId": tuid,
                       "description": desc, "spawnDepth": 1}, f)
        # mtime 摆到这一行自己的时刻上。子代理的状态判据要拿它跟通知时刻比(记录在最后一条
        # 通知之后又动过,说明它被恢复、又跑起来了),留成「文件刚写下」就等于让每个样本都
        # 看起来刚被恢复过。
        t = ea.ClaudeAdapter._iso_ms(started_iso) / 1000.0
        os.utime(p, (t, t))

    def _rollout(self, rows):
        with open(self.path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _notif_row(self, aid, status="completed", at="2026-08-12T09:13:12.000Z"):
        return {"type": "user", "uuid": "n-" + aid, "timestamp": at,
                "origin": {"kind": "task-notification"},
                "message": {"role": "user", "content":
                            "<task-notification>\n<task-id>%s</task-id>\n"
                            "<status>%s</status>\n</task-notification>" % (aid, status)}}

    def _ctx(self):
        return {"sid": self.sid, "msg_id": "msg_a_1", "parts": {}, "order": [],
                "last_tool": None, "t0": 0, "persist": True}

    def _feed(self, ctx, frames):
        for obj in frames:
            self.ad.handle_event(obj, ctx)
            ctx.pop("_close_stdin", None)

    def _marks(self):
        """追认事件。一段自续轮只有这一条,开场与收场那一对已经没有了。"""
        return [r for r in self.bus.asides() if r.get("phase") == "mark"]

    def test_按_task_id_对到子代理(self):
        """自续那一段由带 origin 的 result 追认,身份取自它前面那条预告帧的 task_id。

        新机制下内容不搬家,所以这条要守两件事:身份对得上(名字、收场方式、子代理自己
        跑了多久),以及被追认的正是自续轮产出的那一块——事件里的 part_ids 指着它,而那一块
        本身留在本回合的 ctx["parts"] 里,宿主也就是本回合自己那条消息。
        """
        aid = "a681dd3328270ef54"
        self._agent(aid, "toolu_1", "2026-08-12T09:10:00.000Z")
        self._rollout([self._notif_row(aid)])
        ctx = self._ctx()
        self._feed(ctx, [_notif(aid), _init(ESID), _text(0, "报告读完了"), _self_result(3287),
                         _init(ESID), _text(1, "答复"), _result(2498)])
        row = ctx["asides"][0]
        ag = row["agent"]
        self.assertEqual(row["task_id"], aid)
        self.assertEqual(ag["id"], aid)
        self.assertEqual(ag["task"], "修回合未结束却显示已完成")
        self.assertEqual(ag["outcome"], "completed")
        # 时长是子代理自己的起止(09:10:00 → 09:13:12),不是自续轮那 3287 毫秒
        self.assertEqual(ag["ms"], 192000)
        self.assertEqual(row["ms"], 3287)
        ev = self._marks()[0]
        self.assertEqual(ev["agent"]["task"], "修回合未结束却显示已完成")
        # 追认的是那一块,不是整轮:分界在第 0 块,part_ids 指着自续轮产出的那一块
        self.assertEqual(ev["mark"], 0)
        self.assertEqual(ev["part_ids"], ctx["order"][:1])
        self.assertEqual(ev["host"], ctx["msg_id"])      # 宿主恒为本回合,不再跨回合去认
        # 内容留在本回合的 parts 里(不再有影子 ctx),追认只是给它标一个出处
        self.assertEqual(ctx["parts"][ctx["order"][0]]["text"], "报告读完了")

    def test_中止的子代理照实报收场(self):
        """收场方式照子代理记录里的写,不一律说成「执行完成」——界面上那句话就是照它翻的。"""
        aid = "ade769ef220eef17b"
        self._agent(aid, "toolu_2", "2026-08-12T09:10:00.000Z", desc="查一件事")
        self._rollout([self._notif_row(aid, "stopped")])
        ctx = self._ctx()
        # 中间那块文本是必需的:一块内容都没有的段落追认了也画不出线(前端 hsAsideMark 的
        # `if (!ids.length) return null`),这里要测的是「收场方式报得对」,不是空段该不该画。
        self._feed(ctx, [_notif(aid, "stopped"), _init(ESID), _text(0, "读完了"),
                         _self_result(900)])
        self.assertEqual(ctx["asides"][0]["agent"]["outcome"], "stopped")
        self.assertEqual(self._marks()[0]["agent"]["outcome"], "stopped")

    def test_记录还没落盘时退回预告帧自报的状态(self):
        """记录是边跑边写的,通知那一行偶尔还没到磁盘上;那时 subagents() 给不出收场方式。

        追认是这一段唯一的事件,没有第二条来补,退回预告帧自报的状态就得当场完成。
        状态特意取 failed 而不是 completed:后者与默认说法同字,一份写死「执行完成」的
        实现照样能过,守不住这一条。
        """
        aid = "af5362f036be826ee"
        self._agent(aid, "toolu_3", "2026-08-12T09:10:00.000Z")
        self._rollout([])
        ctx = self._ctx()
        self._feed(ctx, [_notif(aid, "failed"), _init(ESID), _text(0, "读完了"),
                         _self_result(900)])
        self.assertEqual(ctx["asides"][0]["agent"]["outcome"], "failed")
        self.assertEqual(self._marks()[0]["agent"]["outcome"], "failed")

    def test_追认那一条就把身份写全(self):
        """一段自续轮只发一条事件,名字、收场方式、时长都要在它里面写全。

        原来这条守的是「先出现一条无名的线、过一会儿跳成有名字的」不许发生——开场那条
        先占位、收场那条再补名,读起来是闪跳。新机制没有开场,线在追认时一次画出,等价的
        守法就是「补写的第二条事件根本不存在」:整段帧序跑完,session.aside 只此一条。

        收场证据是引擎边跑边写进主记录的,追认时偶尔还没落盘(这里 _rollout 空着就是那种
        情形)。通知落进上下文的那一刻就是子代理交回的时刻,拿它当收场时刻兜底,时长照样
        报得出来。
        """
        aid = "a681dd3328270ef54"
        self._agent(aid, "toolu_1", "2026-08-12T09:10:00.000Z")
        self._rollout([])                     # 通知那一行还没写进主记录
        ctx = self._ctx()
        self._feed(ctx, [_notif(aid), _init(ESID), _text(0, "报告读完了"), _self_result(3287),
                         _init(ESID), _text(1, "答复"), _result(2498)])
        rows = self.bus.asides()
        self.assertEqual([r["phase"] for r in rows], ["mark"])   # 就这一条,后面没有补名那条
        self.assertEqual(rows[0]["agent"]["task"], "修回合未结束却显示已完成")
        self.assertEqual(rows[0]["agent"]["outcome"], "completed")   # 退回预告帧自报的状态
        self.assertGreater(rows[0]["agent"]["ms"], 0)                # 收场时刻按通知那一刻兜底

    def test_对不上子代理的通知不硬安名字(self):
        """后台命令、MCP 任务跑完发的是同一种通知,它们不在 subagents 名单里。

        对不上时 agent 留空,界面据此改说「处理后台任务的返回」;task_id 照记——那一段
        确实是引擎在处理某个交回,只是交回的不是子代理。
        """
        ctx = self._ctx()
        self._feed(ctx, [_notif("bash:7f2"), _init(ESID), _text(0, "命令跑完了"),
                         _self_result(1200)])
        self.assertEqual(ctx["asides"][0]["agent"], None)
        self.assertEqual(ctx["asides"][0]["task_id"], "bash:7f2")
        self.assertEqual(self._marks()[0]["agent"], None)
        self.assertEqual(self._marks()[0]["task_id"], "bash:7f2")

    def test_没赶上预告的那一路不带身份(self):
        """通知帧落在上一回合的读循环里时没有 task_id,对不到具体是谁。"""
        ctx = self._ctx()
        self._feed(ctx, [_init(), _text(0, "自续轮的话"), _self_result(3287)])
        self.assertEqual(ctx["asides"][0]["agent"], None)
        self.assertEqual(ctx["asides"][0]["task_id"], "")


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class AsideRowText(unittest.TestCase):
    """分割线上那三段字的口径。收场文案与子代理菜单共用一份,不能各写各的。"""

    def _run(self, expr):
        src = _srcmap.chat_src()
        parts = [re.search(r"\nfunction hsAgentOutcomeText\(.*?\n}\n", src, re.S).group(0),
                 re.search(r"\nfunction hsSpan\(.*?\n}\n", src, re.S).group(0)]
        js = "\n".join(parts) + "\nconsole.log(JSON.stringify(" + expr + "));"
        out = subprocess.run(["node", "-e", NODE_PRELUDE + js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_收场四档各有说法(self):
        got = self._run('["completed","stopped","killed","gone","failed",""]'
                        ".map(hsAgentOutcomeText)")
        self.assertEqual(got, ["执行完成", "已中止", "已中止", "进程已退出", "已失败", "执行完成"])

    def test_时长按量级换单位(self):
        got = self._run("[0, 900, 42000, 192000, 600000, 3900000].map(hsSpan)")
        self.assertEqual(got, ["", "1 秒", "42 秒", "3 分 12 秒", "10 分钟", "1 小时 5 分"])


if __name__ == "__main__":
    unittest.main()


class NotificationAtTurnHead(unittest.TestCase):
    """通知落在用户这一轮的开头时,整轮不能被追认成后台处理。

    2026-08-20 用户实见:发出「子代理返回了吗?」之后,回答连同提问一起挂到了
    「处理后台任务的返回」下面。拿 adp_190349c63f7f48 的逐帧轨迹核过,这一轮的真实形状是

        turn.begin(text="子代理返回了吗?")
        system/background_tasks_changed → system/task_updated → system/task_notification
        system/init
        …这一轮全部产出…
        result(origin=task-notification)      ← 整轮收尾的那条带 origin

    origin 说的是「这一轮是被什么调度起来的」,不是「用户没说话」——通知落在用户这一轮的
    头上时两者都成立。所以第一段(里面一定有用户自己那句话)不追认,只追认尾段。

    另一种真实形状(同一份轨迹的 8763 那一轮,用户发「完成了吗」)是

        turn.begin → 通知 → init → result(origin) → init → …内容… → result(null)

    那条带 origin 的 result 前面一个 part 都没有,追认的是空段,本来就画不出线。
    两种形状都要保证:用户的内容不被追认。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, text="子代理返回了吗？"):
        return {"sid": self.sid, "msg_id": "msg_a_1", "parts": {}, "order": [],
                "last_tool": None, "t0": 0, "persist": True, "text": text}

    def _feed(self, ctx, frames):
        for obj in frames:
            self.ad.handle_event(obj, ctx)

    def _marks(self):
        return [p for p in self.bus.asides() if p.get("phase") == "mark"]

    def test_通知在轮首_整轮不被追认(self):
        """轨迹里 9944 那一轮的形状。这是用户实见的那一条。"""
        ctx = self._ctx()
        self._feed(ctx, [_notif("bq1"), _init(),
                         _text(0, "子代理"), _text(0, "已经返回了。"),
                         _self_result(41000)])
        self.assertEqual(self._marks(), [], "用户这一轮被整段追认成后台处理了")
        self.assertIn("已经返回了", "".join(
            p.get("text", "") for p in ctx["parts"].values() if p["type"] == "text"))

    def test_轮首那条空段本来就不该出线(self):
        """轨迹里 8763 那一轮的形状:带 origin 的 result 之前一个 part 都没有。"""
        ctx = self._ctx("完成了吗")
        self._feed(ctx, [_notif("bq1"), _init(), _self_result(1200),
                         _init(), _text(0, "完成了。"), _result(2400)])
        self.assertEqual(self._marks(), [])

    def test_第二段起才追认(self):
        """一轮里连着处理两次后台返回:第二段不含用户那句话,该出线还是要出。

        origin 为空的那条 result 会直接收尾整轮(见 TurnBoundary),所以「先答用户、
        再处理后台」不可能落在同一轮里;能出现尾段的只有连着两条带 origin 的 result。
        这个形状我没有在轨迹里见过,是照判据构造的——留着是为了钉住「不是一律不追认」,
        真见到实例再按实例校正。
        """
        ctx = self._ctx()
        self._feed(ctx, [_notif("bq2"), _init(), _text(0, "读第一份报告。"),
                         _self_result(3000),
                         _notif("bq3"), _init(), _text(1, "读第二份报告。"),
                         _self_result(4000)])
        marks = self._marks()
        self.assertEqual(len(marks), 1, "第二段没有被追认")
        self.assertTrue(marks[0]["part_ids"], "追认了却没带上是哪几块内容")
        self.assertEqual(marks[0]["mark"], 1, "追认的应当是第二段,不是从头")

    def test_没有用户正文时照旧追认(self):
        """别的引擎可能真的起一轮没有用户正文的自续轮,那一轮整段都是后台处理。"""
        ctx = self._ctx(text="")
        self._feed(ctx, [_notif("bq3"), _init(), _text(0, "报告读完了。"),
                         _self_result(5000)])
        self.assertEqual(len(self._marks()), 1)

    def test_追认与否都不改回合边界(self):
        """这条 result 只管归属,不划边界——不追认了也不能顺手把回合关掉。"""
        ctx = self._ctx()
        self.ad.handle_event(_notif("bq4"), ctx)
        self.ad.handle_event(_init(), ctx)
        self.ad.handle_event(_text(0, "在读了。"), ctx)
        self.ad.handle_event(_self_result(41000), ctx)
        self.assertFalse(ctx.get("_close_stdin"), "被这条 result 提前收尾了")
