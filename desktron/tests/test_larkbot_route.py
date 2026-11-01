"""飞书消息的路由与红线闸(docs/agent-hosting-design.md)。

钉住两条:
  锚点判定以 thread_id 为准,不认单独出现的 root_id——飞书的引用气泡会只给 root_id,
  认它就会把引用错当成话题回复,人在群里说的话被塞进一条不存在的话题里。
  红线的凭证一类必拦、且拦下之后原文不出现在给飞书的替代文本里。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from larkbot import redline, route  # noqa: E402

BOT = "ou_bot_self"


def msg(**kw):
    d = {"chat_type": "group", "chat_id": "oc_1", "message_id": "om_1", "mentions": []}
    d.update(kw)
    return d


def at(*ids):
    return [{"id": {"open_id": i}} for i in ids]


class ShouldReply(unittest.TestCase):
    def test_私聊不需要_at(self):
        ok, why = route.should_reply(msg(chat_type="p2p"), [BOT])
        self.assertTrue(ok)
        self.assertEqual(why, "p2p")

    def test_群里没_at_不理(self):
        ok, why = route.should_reply(msg(mentions=at("ou_someone")), [BOT])
        self.assertFalse(ok)
        self.assertEqual(why, "no-mention")

    def test_群里_at_了才理(self):
        ok, _ = route.should_reply(msg(mentions=at("ou_someone", BOT)), [BOT])
        self.assertTrue(ok)

    def test_app_id_形式的_at_也算(self):
        """飞书给机器人的 @ 有时带 app_id 而不是 open_id,两种都要认。"""
        m = msg(mentions=[{"id": {"app_id": "cli_app1"}}])
        self.assertTrue(route.should_reply(m, ["cli_app1"])[0])

    def test_话题群里不必反复_at(self):
        ok, why = route.should_reply(msg(thread_id="omt_1"), [BOT], chat_mode="topic")
        self.assertTrue(ok)
        self.assertEqual(why, "topic")

    def test_话题群的顶层消息仍需_at(self):
        """没有 thread_id = 还没进任何话题,那就是普通群发言,不该被当成对它说话。"""
        self.assertFalse(route.should_reply(msg(), [BOT], chat_mode="topic")[0])


class Anchor(unittest.TestCase):
    def test_私聊锚在群(self):
        self.assertEqual(route.anchor_of(msg(chat_type="p2p"))[0], "chat:oc_1")

    def test_普通群_at_锚在群(self):
        a, k = route.anchor_of(msg())
        self.assertEqual((a, k), ("chat:oc_1", "regular-group-chat"))

    def test_真话题回复锚在话题根(self):
        a, k = route.anchor_of(msg(root_id="om_root", thread_id="omt_1"))
        self.assertEqual((a, k), ("thread:om_root", "real-thread"))

    def test_引用气泡不算话题(self):
        """飞书客户端的「回复」小箭头会给顶层消息塞 root_id 却不塞 thread_id。
        认了它,人在群里的一句普通发言就会被塞进一条空话题,历史从此对不上。"""
        a, k = route.anchor_of(msg(root_id="om_quoted"))
        self.assertEqual((a, k), ("chat:oc_1", "regular-group-chat"))

    def test_话题群顶层消息自己当种子(self):
        a, k = route.anchor_of(msg(message_id="om_seed"), chat_mode="topic")
        self.assertEqual((a, k), ("thread:om_seed", "topic-chat"))

    def test_种子与其后的回复落同一个锚点(self):
        """种子消息 om_seed 开出的话题,后续回复带 root_id=om_seed。
        两者必须同锚,否则话题的第一句与其余的分在两条会话里。"""
        seed = route.anchor_of(msg(message_id="om_seed"), chat_mode="topic")[0]
        reply = route.anchor_of(msg(message_id="om_2", root_id="om_seed", thread_id="omt_1"),
                                chat_mode="topic")[0]
        self.assertEqual(seed, reply)

    def test_同群不同话题互不串(self):
        a1 = route.anchor_of(msg(root_id="om_a", thread_id="t1"))[0]
        a2 = route.anchor_of(msg(root_id="om_b", thread_id="t2"))[0]
        self.assertNotEqual(a1, a2)


class Allowed(unittest.TestCase):
    def test_两个都不配就不限制(self):
        self.assertTrue(route.allowed(msg(sender={"id": "ou_x"}), {})[0])

    def test_人不在名单里被拦(self):
        ok, why = route.allowed(msg(sender={"id": "ou_x"}), {"allowed_users": ["ou_y"]})
        self.assertFalse(ok)
        self.assertEqual(why, "user-not-allowed")

    def test_群不在名单里被拦(self):
        ok, why = route.allowed(msg(chat_id="oc_1"), {"allowed_chats": ["oc_2"]})
        self.assertFalse(ok)
        self.assertEqual(why, "chat-not-allowed")

    def test_sender_是嵌套字典时也取得到(self):
        m = msg(sender={"id": {"open_id": "ou_y"}})
        self.assertTrue(route.allowed(m, {"allowed_users": ["ou_y"]})[0])


class Pick(unittest.TestCase):
    def _ids(self, a):
        return [a.get("lark_app_id"), BOT]

    def test_按_app_id_挑到人(self):
        agents = [{"id": "a1", "lark_app_id": "cli_1"}, {"id": "a2", "lark_app_id": "cli_2"}]
        m = msg(mentions=at(BOT), _app_id="cli_2")
        a, anc, _kind, _why = route.pick(m, agents, self._ids)
        self.assertEqual(a["id"], "a2")
        self.assertEqual(anc, "chat:oc_1")

    def test_没有对应的_agent_就不接(self):
        m = msg(mentions=at(BOT), _app_id="cli_9")
        a, _, _, why = route.pick(m, [{"id": "a1", "lark_app_id": "cli_1"}], self._ids)
        self.assertIsNone(a)
        self.assertEqual(why, "no-agent-for-app")

    def test_没_at_时不接且带出理由(self):
        m = msg(_app_id="cli_1")
        a, _, _, why = route.pick(m, [{"id": "a1", "lark_app_id": "cli_1"}], self._ids)
        self.assertIsNone(a)
        self.assertEqual(why, "no-mention")

    def test_一个_app_绑了两个_agent_时标出来不静默(self):
        agents = [{"id": "a1", "lark_app_id": "cli_1"}, {"id": "a2", "lark_app_id": "cli_1"}]
        m = msg(mentions=at(BOT), _app_id="cli_1")
        a, _, _, why = route.pick(m, agents, self._ids)
        self.assertEqual(a["id"], "a1")
        self.assertTrue(why.startswith("dup-app:"), why)


class Redline(unittest.TestCase):
    def test_干净文本放行(self):
        r = redline.check("这个函数的写法可以简化一下,把两层循环合成一层。")
        self.assertTrue(r["ok"])
        self.assertEqual(r["level"], "pass")

    def test_各类凭证一律_block(self):
        for s in ("AKIA1234567890ABCDEF",
                  "ghp_abcdefghijklmnopqrstuvwxyz0123",
                  "sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaa",
                  "xoxb-1234567890-abcdefghij",
                  "-----BEGIN RSA PRIVATE KEY-----",
                  "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u"):
            r = redline.check("给你: " + s)
            self.assertEqual(r["level"], "block", s)
            self.assertFalse(r["ok"], s)

    def test_文档示例里的占位符不误拦(self):
        """`TOKEN=<your-token>` 恰恰是该发出去的内容,拦了它这道闸就没法用在讲配置的场景。"""
        for s in ("TOKEN=<your-token>", "api_key: $OPENAI_KEY", "password = changeme",
                  'secret: "占位符"', "token = ..."):
            self.assertEqual(redline.check(s)["level"], "pass", s)

    def test_赋值形态的真凭证要拦(self):
        r = redline.check('api_key = "A1b2C3d4E5f6G7h8J9k0"')
        self.assertEqual(r["level"], "block")

    def test_拦下的原文不出现在给飞书的替代文本里(self):
        r = redline.check("key 是 ghp_abcdefghijklmnopqrstuvwxyz0123")
        n = redline.notice(r)
        self.assertNotIn("ghp_", n)
        self.assertIn("Desk", n)

    def test_命中项只记类别不回显匹配串(self):
        """日志里躺着一份等于没拦。"""
        r = redline.check("ghp_abcdefghijklmnopqrstuvwxyz0123")
        for h in r["hits"]:
            self.assertNotIn("ghp_", str(h))

    def test_封存原文被原样转述时拦下(self):
        line = "这条是封存内容的一整行,长度足够长可以被指纹认出来"
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "s.md"), "w", encoding="utf-8") as f:
                f.write(line + "\n")
            r = redline.check("他说过:" + line, sealed_dir=d)
            self.assertEqual(r["level"], "block")
            self.assertEqual(r["hits"][0]["kind"], "sealed")

    def test_加了前缀改了标点仍拦得住(self):
        """转述几乎必然带前缀(「他说过:」),整行比对时这一条就漏了——滑窗才拦得住。"""
        line = "这条是封存内容的一整行,长度足够长可以被指纹认出来"
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "s.md"), "w", encoding="utf-8") as f:
                f.write("前面还有一行无关的\n" + line + "\n后面也有\n")
            for v in ("他说过:" + line, line + " —— 原话如此", "参见: " + line[:30]):
                self.assertEqual(redline.check(v, sealed_dir=d)["level"], "block", v)

    def test_短句不因为凑巧重合而误命中(self):
        """窗口取 24 字是为了这个:短了会把「这个方案我觉得可以」这种随处可见的话拦掉。"""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "s.md"), "w", encoding="utf-8") as f:
                f.write("封存里也提到了这个方案我觉得可以,但语境完全不同,这一整句才是真正要防的内容\n")
            self.assertEqual(redline.check("这个方案我觉得可以", sealed_dir=d)["level"], "pass")

    def test_没配封存目录时不误伤(self):
        self.assertTrue(redline.check("随便一句足够长的话,用来确认没有封存目录时一切照常")["ok"])

    def test_合规词是_warn_不是_block(self):
        """错拦的代价是人多点一下,但这一类识别本身不可靠,一律 block 会让它没法用。"""
        r = redline.check("这份数据含 PII")
        self.assertEqual(r["level"], "warn")
        self.assertTrue(r["ok"])

    def test_strict_把_warn_升成_block(self):
        r = redline.check("这份数据含 PII", strict=True)
        self.assertEqual(r["level"], "block")
        self.assertFalse(r["ok"])

    def test_缩写要词边界_不吃长词片段(self):
        self.assertEqual(redline.check("请看 xpiix 这个标识")["level"], "pass")

    def test_人名加评价命中_单独出现不命中(self):
        self.assertEqual(redline.check("张三这活干得不行", names=["张三"])["level"], "warn")
        self.assertEqual(redline.check("这个方案不行", names=["张三"])["level"], "pass")
        self.assertEqual(redline.check("张三今天休假", names=["张三"])["level"], "pass")

    def test_合规检查可关(self):
        self.assertEqual(redline.check("PII 的数据", compliance=False)["level"], "pass")

    def test_关了合规也拦不住凭证(self):
        """凭证那一类不受任何开关影响——漏一次就得换 token,不可逆。"""
        r = redline.check("ghp_abcdefghijklmnopqrstuvwxyz0123", compliance=False)
        self.assertEqual(r["level"], "block")


if __name__ == "__main__":
    unittest.main()
