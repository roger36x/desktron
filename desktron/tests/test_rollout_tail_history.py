"""往上翻历史改成开尾窗读(2026-08-23)。

`history()` 原来整份读 rollout,注释里的理由是「单段最大也就几十兆(实测 39MB 解析
0.3 秒)」。那个前提已经不成立:本机最大的一份是 352MB。实测 25MB 整份读进程峰值内存
+90MB,按比例 352MB 约 +1.3GB——而这条路每次往上翻都跑一遍。

尾窗的顾虑是会把工具调用与它的输出切散(靠 call_id 相认,分在窗口两侧就配不上对)。
import_session 那边早就解决了:窗口开完裁到第一条真人发言,半个回合不要。这里同一条路。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea
    import engine_adapters.codex_rollout as rollout_mod
finally:
    sys.argv = _argv

ESID = "019f7e78-5fce-7523-9dbf-4e0359d2f936"


def user_row(text, i):
    return {"type": "response_item", "timestamp": "2026-08-23T10:%02d:00Z" % (i % 60),
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text}]}}


def asst_row(text, i):
    return {"type": "response_item", "timestamp": "2026-08-23T10:%02d:30Z" % (i % 60),
            "payload": {"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": text}]}}


def compacted_row(pad=0, nested_response=False):
    messages = [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "压缩快照 %d" % i}]}
        for i in range(3)
    ]
    if nested_response:
        messages = [{"type": "response_item", "payload": item} for item in messages]
    return {"type": "compacted", "timestamp": "2026-08-23T10:20:00Z",
            "payload": {"message": "", "padding": "x" * pad,
                        "replacement_history": messages, "window_id": 7}}


def tool_call_row(call_id, i):
    return {"type": "response_item", "timestamp": "2026-08-23T10:%02d:10Z" % i,
            "payload": {"type": "function_call", "name": "exec_command",
                        "call_id": call_id,
                        "arguments": json.dumps({"cmd": "pwd"})}}


def tool_output_row(call_id, i):
    return {"type": "response_item", "timestamp": "2026-08-23T10:%02d:20Z" % i,
            "payload": {"type": "function_call_output", "call_id": call_id,
                        "output": "完成"}}


class Bus:
    def publish(self, e):
        pass


class TailWindow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.CodexAdapter(self.tmp.name, self.tmp.name, Bus())
        self.sid = self.ad.create_session()["id"]
        self.ad._set_engine_sid(self.sid, ESID)
        self.path = os.path.join(self.tmp.name, "rollout-2026-08-23T10-00-00-%s.jsonl" % ESID)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, turns, pad=0):
        """turns 个回合;pad 是每条塞多少字节垃圾,用来把文件撑大。"""
        with open(self.path, "w", encoding="utf-8") as f:
            for i in range(turns):
                f.write(json.dumps(user_row("问 %d" % i, i), ensure_ascii=False) + "\n")
                f.write(json.dumps(asst_row("答 %d" % i + "x" * pad, i), ensure_ascii=False) + "\n")
        self.ad._rollout_path = lambda esid: self.path

    def _write_rows(self, rows, path=None):
        target = path or self.path
        with open(target, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.ad._rollout_path = lambda esid: self.path

    @staticmethod
    def _texts(messages):
        return " ".join(str(part.get("text") or "")
                        for message in messages for part in (message.get("parts") or []))

    def _asked(self):
        """记下每次 _group_rollout 的起点,看是不是真的开了窗。"""
        seen = []
        real = self.ad._group_rollout
        self.ad._group_rollout = lambda p, s, start=0, exact=False: (
            seen.append(start) or real(p, s, start, exact))
        return seen

    def test_小文件照旧从头读(self):
        """窗口比文件大时 start 就是 0,行为与从前一字不差。"""
        self._write(6)
        seen = self._asked()
        self.ad.history(self.sid, have=0, limit=30)
        self.assertEqual(seen, [0])

    def test_大文件从末尾开窗(self):
        self._write(400, pad=60000)      # 约 25MB
        self.assertGreater(os.path.getsize(self.path), 4_000_000)
        seen = self._asked()
        self.ad.history(self.sid, have=0, limit=5)
        self.assertTrue(seen and seen[0] > 0, "还是从头读的")

    def test_取到的就是最近那几轮(self):
        self._write(60)
        got = self.ad.history(self.sid, have=0, limit=3)
        texts = ["".join(q.get("text") or "" for q in (m.get("parts") or [])
                         if q.get("type") == "text") for m in got]
        self.assertIn("问 59", " ".join(texts))
        self.assertNotIn("问 40", " ".join(texts))

    def test_翻到更早的那几轮(self):
        """have=3 表示界面上已经有最近三轮,这次要它们再往前的。"""
        self._write(60)
        got = self.ad.history(self.sid, have=3, limit=3)
        texts = " ".join("".join(q.get("text") or "" for q in (m.get("parts") or [])
                                 if q.get("type") == "text") for m in got)
        self.assertIn("问 56", texts)
        self.assertNotIn("问 57", texts)   # 那三轮界面已经有了

    def test_要多少就给多少(self):
        """want 大时不能少给。标记法按需要的回合数往回数,一趟就该数到位。"""
        self._write(400, pad=60000)
        seen = self._asked()
        got = self.ad.history(self.sid, have=0, limit=300)
        self.assertEqual(sum(1 for m in got if self.ad._turn_start(m)), 300)
        self.assertEqual(len(seen), 1, "标记已经数准了,不该再解析第二趟")

    def test_标记少于回合就放大再找(self):
        """标记与回合不是一一对应:同一轮里可以有多条 user 消息,压缩行也占字节标记。

        余量兜不住时要放大重找,不能少给。摆一个「每次只给 want 个回合」的解析结果驱动:
        照文件构造的话,余量够不够要看数据怎么排,测不稳。
        """
        self._write(400, pad=60000)
        marks_seen, real = [], self.ad._rollout_window_start
        def spy_start(path, size, marks):
            marks_seen.append(marks)
            return real(path, size, marks)
        turns = [{"info": {"id": "u%d" % i, "role": "user"}, "parts": []} for i in range(5)]
        with mock.patch.object(self.ad, "_rollout_window_start", spy_start), \
                mock.patch.object(self.ad, "_group_rollout", lambda *a, **k: list(turns)):
            self.ad._rollout_tail(self.path, self.sid, 5)
        self.assertGreater(len(marks_seen), 1, "回合不够却没有再找")
        self.assertGreater(marks_seen[1], marks_seen[0], "重找时标记数没放大")

    def test_读到头了就停(self):
        """文件全读完仍不够,要收工而不是无限翻倍。"""
        self._write(10)
        seen = self._asked()
        self.ad.history(self.sid, have=0, limit=999)
        self.assertEqual(seen[-1], 0)

    def test_没有更早的就给空(self):
        self._write(5)
        self.assertEqual(self.ad.history(self.sid, have=5, limit=3), [])

    def test_窗口起点不切出半个回合(self):
        """起点落在回合中间时,第一条会是半截助手消息——它的工具调用留在窗口外,配不上输出。

        直接摆一个「以助手消息开头」的窗口进来驱动:照文件构造的话,起点是否恰好落在
        回合中间要看字节数怎么排,测不稳(2026-08-23 反向验证发现撤掉裁剪照样全绿)。
        """
        self._write(4)
        half = [{"info": {"id": "a0", "role": "assistant"}, "parts": []},
                {"info": {"id": "u1", "role": "user"}, "parts": []},
                {"info": {"id": "a1", "role": "assistant"}, "parts": []}]
        calls = []
        def fake(p, s, start=0, exact=False):
            calls.append(start)
            return list(half)
        with mock.patch.object(self.ad, "_group_rollout", fake), \
                mock.patch.object(self.ad, "_rollout_window_start", lambda *a: 99):
            got = self.ad._rollout_tail(self.path, self.sid, 0)
        self.assertTrue(calls and calls[0] > 0, "没开窗,这条就测不到裁剪")
        self.assertTrue(got, "整段被裁没了")
        self.assertEqual(got[0]["info"]["id"], "u1", "第一条仍是半截助手消息")

    def test_两种JSON写法都认(self):
        """标记是字节匹配。本机 codex 写紧凑 JSON,但那是它的实现顺手定的,不是契约——
        只认一种的话,格式一变就一个标记也找不到、退回整份读,而且没有任何地方会报错。"""
        for sep, how in (((",", ":"), "紧凑"), ((", ", ": "), "带空格")):
            with open(self.path, "w", encoding="utf-8") as f:
                for i in range(400):
                    f.write(json.dumps(user_row("问 %d" % i, i), ensure_ascii=False,
                                       separators=sep) + "\n")
                    f.write(json.dumps(asst_row("答 %d" % i + "x" * 60000, i),
                                       ensure_ascii=False, separators=sep) + "\n")
            self.ad._rollout_path = lambda esid: self.path
            size = os.path.getsize(self.path)
            self.assertGreater(self.ad._rollout_window_start(self.path, size, 10), 0,
                               "%s写法没认出来" % how)
            seen = self._asked()
            got = self.ad.history(self.sid, have=0, limit=5)
            self.assertTrue(seen and seen[0] > 0, "%s写法下退回了整份读" % how)
            self.assertEqual(sum(1 for m in got if self.ad._turn_start(m)), 5)

    def test_压缩行不算回合起点(self):
        """压缩行把被压掉的内容原样带在 payload 里,里面同样有未转义的 role user 字节。

        按整行找会把它算成一个起点,而契约里它是压缩标记不是发言——标记就比回合多出一截,
        定位出来的窗口给不够(2026-08-24 拿 Desk Opt 实见:29 个标记里 9 个是压缩行)。
        """
        rows = [user_row("问 0", 0), asst_row("答 0", 0)]
        rows.append({"type": "compacted", "timestamp": "2026-08-23T10:01:00Z",
                     "payload": {"message": "",
                                 "snapshot": [{"type":"message","role":"user","content":[]}]}})
        rows += [user_row("问 1", 1), asst_row("答 1", 1)]
        with open(self.path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
        size = os.path.getsize(self.path)
        # 从末尾数 2 个起点,该落在「问 1」那一行;把压缩行也算进去就会落到「答 0」之后
        start = self.ad._rollout_window_start(self.path, size, 2)
        with open(self.path, "rb") as f:
            f.seek(start)
            head = f.readline().decode("utf-8")
        self.assertIn('"role":"user"', head, "起点没落在真人发言那一行")
        self.assertIn("问 0", head, "起点被压缩行顶掉了,少数了一个回合")

    def test_巨型压缩行内嵌response_item也不算回合起点(self):
        """只认顶层 response_item/message/user；压缩快照里的同名字段不是回合。

        padding 把内嵌 role 推到巨行末端，顺带钉住定位器不能复制 lo..role 的整段前缀。
        """
        rows = [
            {"type": "session_meta", "timestamp": "2026-08-23T09:59:00Z", "payload": {}},
            user_row("问 0", 0), asst_row("答 0", 0),
            compacted_row(pad=8_000_000, nested_response=True),
            user_row("问 1", 1), asst_row("答 1", 1),
        ]
        self._write_rows(rows)
        start = self.ad._rollout_window_start(self.path, os.path.getsize(self.path), 2)
        with open(self.path, "rb") as f:
            f.seek(start)
            head = f.read(512).decode("utf-8", "ignore")
        self.assertGreater(start, 0, "应跳过 session_meta，落在第一条真人发言")
        self.assertIn('"type":"response_item"', head)
        self.assertIn("问 0", head, "把 compacted 内嵌 response_item/user 错算成回合了")

    def test_巨型压缩载荷不进json解析且消息语义不变(self):
        """只投影压缩边界，不展开巨型快照，也不扰乱工具配对或目标哨兵。"""
        commentary = asst_row("过程说明", 2)
        commentary["payload"]["phase"] = "commentary"
        goal = {"type": "response_item", "timestamp": "2026-08-23T10:03:00Z",
                "payload": {"type": "function_call_output", "call_id": "goal-1",
                            "output": json.dumps({"goal": {
                                "threadId": ESID, "objective": "交付尾窗优化",
                                "status": "active", "updatedAt": 1770000000000,
                            }}, ensure_ascii=False)}}
        rows = [user_row("真实问题", 1), tool_call_row("call-1", 1),
                compacted_row(pad=5_000_000), tool_output_row("call-1", 1),
                commentary, asst_row("最终答复", 2), goal]
        self._write_rows(rows)

        real_loads = rollout_mod.json.loads
        parsed_sizes = []
        def guarded_loads(value, *args, **kwargs):
            parsed_sizes.append(len(value))
            if len(value) > 1_000_000:
                raise AssertionError("compacted 巨行进入了完整 json.loads")
            return real_loads(value, *args, **kwargs)
        with mock.patch.object(rollout_mod.json, "loads", side_effect=guarded_loads):
            got = self.ad._group_rollout(self.path, self.sid) or []

        self.assertTrue(parsed_sizes)
        self.assertLess(max(parsed_sizes), 1_000_000)
        self.assertEqual([m["info"]["role"] for m in got], ["user", "assistant", "user"])
        self.assertEqual([p["type"] for p in got[1]["parts"]], ["tool", "compact", "reasoning", "text"])
        tool = got[1]["parts"][0]
        self.assertEqual(tool["state"]["status"], "done")
        self.assertEqual(tool["state"]["output"], "完成")
        self.assertEqual(got[2]["info"]["goal"]["condition"], "交付尾窗优化")
        self.assertFalse(any((m.get("info") or {}).get("compact") for m in got))
        self.assertNotIn("压缩快照", json.dumps(got, ensure_ascii=False))

    def test_追加与半行写入(self):
        """完整追加立即可见；末尾 JSON 只写一半时忽略，补齐换行后再认领。"""
        self._write(2)
        user = (json.dumps(user_row("追加问题", 3), ensure_ascii=False,
                           separators=(",", ":")) + "\n").encode("utf-8")
        answer = (json.dumps(asst_row("追加答复", 3), ensure_ascii=False,
                             separators=(",", ":")) + "\n").encode("utf-8")
        cut = len(answer) // 2
        with open(self.path, "ab") as f:
            f.write(user)
            f.write(answer[:cut])
        half = self.ad._rollout_tail(self.path, self.sid, 0) or []
        self.assertIn("追加问题", self._texts(half))
        self.assertNotIn("追加答复", self._texts(half))
        with open(self.path, "ab") as f:
            f.write(answer[cut:])
        complete = self.ad._rollout_tail(self.path, self.sid, 0) or []
        self.assertIn("追加答复", self._texts(complete))

    def test_截断后不沿用旧尾窗(self):
        self._write(8)
        self.assertIn("问 7", self._texts(self.ad._rollout_tail(self.path, self.sid, 0) or []))
        self._write_rows([user_row("截断后的问题", 0), asst_row("截断后的答复", 0)])
        got = self.ad._rollout_tail(self.path, self.sid, 0)
        self.assertIn("截断后的答复", self._texts(got or []))
        self.assertNotIn("问 7", self._texts(got or []))

    def test_同尺寸替换后不沿用旧尾窗(self):
        old_rows = [user_row("旧问", 0), asst_row("旧答", 0)]
        new_rows = [user_row("新问", 0), asst_row("新答", 0)]
        self._write_rows(old_rows)
        before_size = os.path.getsize(self.path)
        self.assertIn("旧答", self._texts(self.ad._rollout_tail(self.path, self.sid, 0) or []))
        replacement = self.path + ".replacement"
        self._write_rows(new_rows, replacement)
        self.assertEqual(os.path.getsize(replacement), before_size)
        os.replace(replacement, self.path)
        got = self.ad._rollout_tail(self.path, self.sid, 0) or []
        self.assertIn("新答", self._texts(got))
        self.assertNotIn("旧答", self._texts(got))

    def test_判据是超过不是够了(self):
        """窗口里恰好 want 个时分不出「再往前没有了」与「没读到」,前者该返回空。"""
        import inspect
        src = inspect.getsource(self.ad._rollout_tail)
        self.assertIn("> want", src)
        self.assertNotIn(">= want", src)


class Cost(unittest.TestCase):
    """这条改动要省下的东西。"""

    def test_不再整份读(self):
        import inspect
        src = inspect.getsource(ea.CodexAdapter.history)
        self.assertIn("_rollout_tail", src)
        self.assertNotIn("self._group_rollout(path, sid), have, limit", src)


if __name__ == "__main__":
    unittest.main()
