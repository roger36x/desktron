"""只读那条路的缓存(2026-08-20)。

`GET /session/<id>/message` 原来每次都把整份 jsonl 读出来、逐行 json.loads、再整个
json.dumps 回去。同一段会话反复打开,做的是同一件事;而且这段全程持 GIL,实测一个重请求
在传时,轻请求从 4.6ms 涨到 75.9ms(放大 16.5 倍)。

缓存的是 bytes 而不是对象,理由在下面 Poison 那一组:messages() 的返回值有调用方就地改
(撤回会 pop 掉最后一条,pi 那侧会修旧投影里的图片标记),共享一份可变结构迟早被改脏。
"""
import json
import os
import sys
import tempfile
import threading
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv

W = ea.SESSION_WINDOW


class Bus:
    def publish(self, e):
        pass


def _msg(i, role="user"):
    return {"info": {"id": f"m{i}", "role": role, "at": 1000 + i},
            "parts": [{"id": f"m{i}_p0", "type": "text", "text": "第 %d 条" % i}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, Bus())
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def fill(self, n):
        for i in range(n):
            self.ad._append_msg(self.sid, _msg(i, "user" if i % 2 == 0 else "assistant"))

    def load(self):
        return json.loads(b"[" + self.ad.messages_window_json(self.sid, W) + b"]")


class Correct(Base):
    def test_与老写法语义一致(self):
        """老写法是 messages() 截窗口再整体 dumps。字节可以不同(分隔符空格),内容不能。"""
        self.fill(12)
        out = self.ad.messages(self.sid)
        starts = [i for i, m in enumerate(out) if self.ad._turn_start(m)]
        if len(starts) > W:
            out = out[starts[-W:][0]:]
        self.assertEqual(self.load(), json.loads(json.dumps(out, ensure_ascii=False)))

    def test_拼出来的是合法_json(self):
        self.fill(3)
        self.assertEqual([m["info"]["id"] for m in self.load()], ["m0", "m1", "m2"])

    def test_空会话给空串(self):
        """空串让路由拼出 `[]`;给 b'[]' 的话会拼成 `[[]]`。"""
        self.assertEqual(self.ad.messages_window_json(self.sid, W), b"")
        self.assertEqual(self.load(), [])

    def test_没有这段会话也不抛(self):
        self.assertEqual(self.ad.messages_window_json("adp_不存在", W), b"")

    def test_按回合截窗口(self):
        """截的是回合数不是消息数,而且从回合的头上切——从中间切会让第一条是半截回合。"""
        for i in range(W + 6):
            self.ad._append_msg(self.sid, _msg(i, "user"))
            self.ad._append_msg(self.sid, _msg(1000 + i, "assistant"))
        got = self.load()
        self.assertEqual(sum(1 for m in got if self.ad._turn_start(m)), W)
        self.assertTrue(self.ad._turn_start(got[0]))


class Invalidate(Base):
    def test_命中时逐字节相同(self):
        self.fill(5)
        self.assertEqual(self.ad.messages_window_json(self.sid, W),
                         self.ad.messages_window_json(self.sid, W))

    def test_追加之后自己失效(self):
        self.fill(3)
        self.load()
        self.ad._append_msg(self.sid, _msg(99, "user"))
        self.assertEqual(self.load()[-1]["info"]["id"], "m99")

    def test_重写之后自己失效(self):
        """撤回走的是 _rewrite_msgs:条数变少,戳也跟着变。"""
        self.fill(4)
        self.load()
        self.ad._rewrite_msgs(self.sid, [_msg(0)])
        self.assertEqual([m["info"]["id"] for m in self.load()], ["m0"])

    def test_同长度改写也认得出(self):
        """只比字节数会漏掉「长度没变、内容变了」,所以戳里带 mtime_ns。

        改写前后必须逐字节等长,否则光靠 size 也能认出来,这条就白测了——
        原来的写法把 role 从 assistant 改成了 user,长度其实变了(2026-08-20 反向验证发现)。
        """
        rows = [_msg(0, "user"), _msg(1, "user")]
        self.ad._rewrite_msgs(self.sid, rows)
        before = os.path.getsize(self.ad._msg_path(self.sid))
        self.load()
        rows2 = [_msg(0, "user"), _msg(1, "user")]
        rows2[1]["parts"][0]["text"] = "第 9 条"      # 与 "第 1 条" 等长
        self.ad._rewrite_msgs(self.sid, rows2)
        self.assertEqual(os.path.getsize(self.ad._msg_path(self.sid)), before,
                         "改写前后长度不同,这条用例就测不到 mtime 那一半")
        self.assertEqual(self.load()[1]["parts"][0]["text"], "第 9 条")

    def test_同一时钟刻度内的等长改写也认得出(self):
        """Windows 时间戳按约 15 ms 的刻度走，CI 上两次改写拿到了同一个 mtime_ns。
        把新文件的时间戳拨回旧值，确定性地复现这种情况：此时只剩文件号能区分。"""
        path = self.ad._msg_path(self.sid)
        self.ad._rewrite_msgs(self.sid, [_msg(0, "user"), _msg(1, "user")])
        old = os.stat(path)
        self.load()
        rows2 = [_msg(0, "user"), _msg(1, "user")]
        rows2[1]["parts"][0]["text"] = "第 9 条"
        self.ad._rewrite_msgs(self.sid, rows2)
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        new = os.stat(path)
        self.assertEqual((new.st_mtime_ns, new.st_size), (old.st_mtime_ns, old.st_size))
        self.assertEqual(self.load()[1]["parts"][0]["text"], "第 9 条")

    def test_窗口不同不串(self):
        self.fill(6)
        a = self.ad.messages_window_json(self.sid, 1)
        b = self.ad.messages_window_json(self.sid, W)
        self.assertNotEqual(a, b)
        self.assertEqual(self.ad.messages_window_json(self.sid, 1), a)

    def test_读取期间被追加_下一次仍读到新的(self):
        """戳在读之前取,所以缓存里的内容只会与戳同新或更新,不会更旧——
        读到一半被追加的话,下一次请求 stat 到新戳,照样未命中重读。
        (曾在这里加过一道「落缓存前再核一次 mtime」,2026-08-20 反向验证发现它挡不住
        任何错误:撤掉那几行,本用例照样通过。)"""
        self.fill(2)
        index = self.ad._turn_index(self.sid)
        real = index.raw_window
        def racing(**kwargs):
            out = real(**kwargs)
            # 模拟 Desk 外的写入者在本次读取后改变源文件；sidecar 故意不更新，
            # 下一次读取必须凭 source stamp 失效并从 JSONL 重建。
            with open(self.ad._msg_path(self.sid), "a", encoding="utf-8") as stream:
                stream.write(json.dumps(_msg(77, "user"), ensure_ascii=False) + "\n")
            return out
        index.raw_window = racing
        self.ad.messages_window_json(self.sid, W)
        index.raw_window = real
        self.assertEqual(self.load()[-1]["info"]["id"], "m77")

    def test_缓存有上限(self):
        for i in range(self.ad._MSG_CACHE_MAX + 4):
            s = self.ad.create_session()["id"]
            self.ad._append_msg(s, _msg(i))
            self.ad.messages_window_json(s, W)
        self.assertLessEqual(len(self.ad._msg_cache), self.ad._MSG_CACHE_MAX)


class Poison(Base):
    """缓存存 bytes 的理由:messages() 的返回值有人就地改。"""

    def test_改了_messages_的返回值也污染不了缓存(self):
        self.fill(4)
        before = self.load()
        got = self.ad.messages(self.sid)
        got.pop()                      # recall 走的就是这一步
        got[0]["info"]["id"] = "被改了"
        got[0]["parts"].clear()
        self.assertEqual(self.load(), before)

    def test_返回的是不可变字节(self):
        self.fill(2)
        self.assertIsInstance(self.ad.messages_window_json(self.sid, W), bytes)


class Concurrent(Base):
    def test_多线程同时取不炸也不串(self):
        self.fill(8)
        want = self.ad.messages_window_json(self.sid, W)
        bad = []
        def go():
            for _ in range(40):
                if self.ad.messages_window_json(self.sid, W) != want:
                    bad.append(1)
        ths = [threading.Thread(target=go) for _ in range(6)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(timeout=20)
        self.assertEqual(bad, [])


class Route(unittest.TestCase):
    SRC = _srcmap.ui_server_src()

    def test_路由改用缓存那条(self):
        self.assertIn("ad.messages_window_json(seg[2], SESSION_WINDOW)", self.SRC)
        self.assertNotIn("out = ad.messages(seg[2])", self.SRC)

    def test_跑动中那条仍每次现拼(self):
        """live 是这一回合正在长的内容,缓存它会让缓存每一帧都失效。"""
        i = self.SRC.index("messages_window_json(seg[2]")
        seg = self.SRC[i:i + 700]
        self.assertIn("ad.live_message(seg[2])", seg)
        self.assertIn('body + b"," + one if body else one', seg)

    def test_首尾方括号补齐(self):
        i = self.SRC.index("messages_window_json(seg[2]")
        self.assertIn('b"[" + body + b"]"', self.SRC[i:i + 700])


if __name__ == "__main__":
    unittest.main()
