"""飞书 bot 看不见图片(2026-09-10 用户实见)。

人在群里回复一条带图的消息问「你能看到这张图吗」,bot 答「看不到」。两处缺口叠在一起:
事件里被引用那条消息只给 parent_id,正文与图片一概不带;而图片消息本身在组装时也只被
折成「[图片]」四个字,从来没有下载过。

这里钉住:引用的那条要取回来,两条消息里的图片都要落到本地,路径连同「用读文件的工具打开」
一起进提示词。图片路径不进检索词、不进会话历史——前者是噪音,后者会在文件被清掉之后
留下一串死路径。
"""
import json
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from larkbot import handler, larkapi  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
JPG = b"\xff\xd8\xff\xe0" + b"0" * 32
GIF = b"GIF89a" + b"0" * 32
WEBP = b"RIFF" + b"0000" + b"WEBP" + b"0" * 32

HANDLER_SRC = open(os.path.join(os.path.dirname(__file__), "..", "lib", "larkbot",
                                "handler.py"), encoding="utf-8").read()


def img_msg(key="img_k1", mid="om_p"):
    return {"message_id": mid, "message_type": "image",
            "content": json.dumps({"image_key": key})}


class ImageKeys(unittest.TestCase):
    def test_图片消息取到一个(self):
        self.assertEqual(handler._image_keys(img_msg("img_a")), ["img_a"])

    def test_富文本里的图都取到(self):
        c = {"zh_cn": {"content": [[{"tag": "text", "text": "看这个"},
                                    {"tag": "img", "image_key": "k1"}],
                                   [{"tag": "img", "image_key": "k2"}]]}}
        m = {"message_type": "post", "content": json.dumps(c)}
        self.assertEqual(handler._image_keys(m), ["k1", "k2"])

    def test_纯文本没有图(self):
        m = {"message_type": "text", "content": json.dumps({"text": "你好"})}
        self.assertEqual(handler._image_keys(m), [])

    def test_内容坏了不炸(self):
        self.assertEqual(handler._image_keys({"message_type": "image", "content": "{不是"}), [])


class Ext(unittest.TestCase):
    def test_按内容认后缀(self):
        self.assertEqual(handler._img_ext(PNG), ".png")
        self.assertEqual(handler._img_ext(JPG), ".jpg")
        self.assertEqual(handler._img_ext(GIF), ".gif")
        self.assertEqual(handler._img_ext(WEBP), ".webp")

    def test_认不出退回png(self):
        self.assertEqual(handler._img_ext(b"whatever"), ".png")


class Save(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_落盘并给出路径(self):
        with mock.patch.object(larkapi, "download_resource", return_value=(JPG, "")):
            got = handler._save_images(self.d, "cli_1", "om_1", ["k1"])
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].endswith(".jpg"), "后缀该按内容认")
        self.assertEqual(open(got[0], "rb").read(), JPG)
        self.assertIn(os.path.join(".workbench", "agents", "_media"), got[0])

    def test_同一张不重复下载(self):
        fake = mock.Mock(return_value=(PNG, ""))
        with mock.patch.object(larkapi, "download_resource", fake):
            a = handler._save_images(self.d, "cli_1", "om_1", ["k1"])
            b = handler._save_images(self.d, "cli_1", "om_1", ["k1"])
        self.assertEqual(a, b)
        self.assertEqual(fake.call_count, 1, "已经在盘上的不该再取一次")

    def test_取不到的那张跳过不拖垮其余(self):
        def side(app, mid, key, domain="", **kw):
            return (PNG, "") if key == "ok" else (b"", "code=230002 没权限")
        logs = []
        with mock.patch.object(larkapi, "download_resource", side_effect=side):
            got = handler._save_images(self.d, "cli_1", "om_1", ["bad", "ok"], log=logs.append)
        self.assertEqual(len(got), 1)
        self.assertTrue(any("图片取不到" in x for x in logs), "跳过要留一行,否则无从排查")

    def test_张数有上限(self):
        with mock.patch.object(larkapi, "download_resource", return_value=(PNG, "")):
            got = handler._save_images(self.d, "cli_1", "om_1",
                                       [f"k{i}" for i in range(handler.MAX_IMAGES + 4)])
        self.assertEqual(len(got), handler.MAX_IMAGES)

    def test_文件名不由飞书的key决定(self):
        """key 是外部输入。让它进路径,一个 ../ 就写到工作区外面去了。"""
        with mock.patch.object(larkapi, "download_resource", return_value=(PNG, "")):
            got = handler._save_images(self.d, "cli_1", "om_1", ["../../evil"])
        self.assertEqual(len(got), 1)
        self.assertEqual(os.path.dirname(got[0]), handler.media_dir(self.d))
        self.assertTrue(re.fullmatch(r"[0-9a-f]{16}\.png", os.path.basename(got[0])))

    def test_没有app_id就不去取(self):
        fake = mock.Mock(return_value=(PNG, ""))
        with mock.patch.object(larkapi, "download_resource", fake):
            self.assertEqual(handler._save_images(self.d, "", "om_1", ["k1"]), [])
        self.assertEqual(fake.call_count, 0)

    def test_老图片会被扫掉(self):
        d = handler.media_dir(self.d)
        os.makedirs(d, exist_ok=True)
        old, new = os.path.join(d, "old.png"), os.path.join(d, "new.png")
        for fp in (old, new):
            open(fp, "wb").write(PNG)
        os.utime(old, (0, time.time() - handler.MEDIA_KEEP_S - 60))
        with mock.patch.object(larkapi, "download_resource", return_value=(PNG, "")):
            handler._save_images(self.d, "cli_1", "om_1", ["k1"])
        self.assertFalse(os.path.exists(old), "躺在工作区里的东西不能只增不减")
        self.assertTrue(os.path.exists(new))


class Attachments(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.agent = {"id": "b1", "lark_app_id": "cli_1", "domain": "feishu"}

    def test_引用的那条连图带话都取回来(self):
        parent = {"message_id": "om_p", "message_type": "post",
                  "content": json.dumps({"zh_cn": {"content": [
                      [{"tag": "text", "text": "@_user_1 纯AI绘制,我系统的流程图"},
                       {"tag": "img", "image_key": "k1"}]]}})}
        msg = {"_app_id": "cli_1", "message_id": "om_c", "parent_id": "om_p",
               "message_type": "text", "content": json.dumps({"text": "你能看到这张图吗"})}
        with mock.patch.object(larkapi, "get_message", return_value=(parent, "")), \
             mock.patch.object(larkapi, "download_resource", return_value=(PNG, "")):
            out = handler._attachments(self.d, self.agent, msg)
        self.assertIn("对方引用的那条消息", out)
        self.assertIn("我系统的流程图", out)
        self.assertNotIn("@_user_1", out, "@ 占位符该去掉")
        self.assertIn(".png", out)
        self.assertIn("读文件的工具", out, "不明说要读,模型多半仍答「看不到图」")

    def test_本条自己带的图也算(self):
        msg = dict(img_msg("k9", "om_c"), _app_id="cli_1")
        with mock.patch.object(larkapi, "download_resource", return_value=(PNG, "")):
            out = handler._attachments(self.d, self.agent, msg)
        self.assertIn(handler.media_dir(self.d), out)

    def test_有图取不下来要说明原因(self):
        """答「我看不到图」时人无从下手,他会以为是自己没发出去。"""
        msg = dict(img_msg("k9", "om_c"), _app_id="cli_1")
        with mock.patch.object(larkapi, "download_resource", return_value=(b"", "code=230002")):
            out = handler._attachments(self.d, self.agent, msg)
        self.assertIn("im:resource", out)
        self.assertIn("照实说明", out)

    def test_没有引用也没有图就是空串(self):
        msg = {"_app_id": "cli_1", "message_id": "om_c", "message_type": "text",
               "content": json.dumps({"text": "在吗"})}
        self.assertEqual(handler._attachments(self.d, self.agent, msg), "")

    def test_父消息取不到只记一行照常往下走(self):
        msg = {"_app_id": "cli_1", "message_id": "om_c", "parent_id": "om_p",
               "message_type": "text", "content": json.dumps({"text": "这个呢"})}
        logs = []
        with mock.patch.object(larkapi, "get_message", return_value=({}, "code=230002")):
            out = handler._attachments(self.d, self.agent, msg, logs.append)
        self.assertEqual(out, "")
        self.assertTrue(any("被引用的消息取不到" in x for x in logs))

    def test_只往上取一层(self):
        """父消息可能又引用了别人。顺着链一路取回去,一条消息能拖出几十次往返。"""
        parent = {"message_id": "om_p", "parent_id": "om_g", "message_type": "text",
                  "content": json.dumps({"text": "上一条"})}
        msg = {"_app_id": "cli_1", "message_id": "om_c", "parent_id": "om_p",
               "message_type": "text", "content": json.dumps({"text": "这个呢"})}
        fake = mock.Mock(return_value=(parent, ""))
        with mock.patch.object(larkapi, "get_message", fake):
            handler._attachments(self.d, self.agent, msg)
        self.assertEqual(fake.call_count, 1)


class ApiShape(unittest.TestCase):
    def test_取消息把正文抄到content(self):
        """事件里的 message 正文在 content,查接口回来的在 body.content。
        抄一份过去,_text_of 那一路两种来源同一套读法。"""
        r = {"code": 0, "data": {"items": [
            {"message_id": "om_p", "msg_type": "image",
             "body": {"content": json.dumps({"image_key": "k1"})}}]}}
        with mock.patch.object(larkapi, "_call", return_value=r):
            got, err = larkapi.get_message("cli_1", "om_p")
        self.assertEqual(err, "")
        self.assertEqual(handler._image_keys(got), ["k1"])

    def test_取不到给出错误码(self):
        with mock.patch.object(larkapi, "_call", return_value={"code": 230002, "msg": "no perm"}):
            got, err = larkapi.get_message("cli_1", "om_p")
        self.assertEqual(got, {})
        self.assertIn("230002", err)

    def test_下载走二进制不当json解(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "lib", "larkbot",
                                "larkapi.py"), encoding="utf-8").read()
        fn = src[src.index("def download_resource"):src.index("# ---- 身份与群 ----")]
        self.assertIn("r.read()", fn)
        self.assertNotIn("json.loads(r.read()", fn)
        self.assertIn("/resources/", fn)
        self.assertIn("99991663", fn, "token 失效该重取一次,与 _call 同一个做法")


class Wiring(unittest.TestCase):
    def test_接在组装之后不混进用户原话(self):
        i = HANDLER_SRC.index("prompt, used = context.build")
        seg = HANDLER_SRC[i:i + 500]
        self.assertIn("prompt += attach", seg)
        # text 落进历史与检索词,不能被图片路径污染
        j = HANDLER_SRC.index("store.turn_and_used(repo_root, aid, anchor")
        self.assertIn('"user", text)', HANDLER_SRC[j:j + 120])

    def test_取附件失败不影响回话(self):
        i = HANDLER_SRC.index('_advance("attach")')
        seg = HANDLER_SRC[i:i + 400]
        self.assertIn("try:", seg)
        self.assertIn('attach = ""', seg)

    def test_排在占位卡片之后(self):
        self.assertLess(HANDLER_SRC.index("card_thread.start()"),
                        HANDLER_SRC.index('_advance("attach")'),
                        "两边都是飞书往返,该叠在一起")


if __name__ == "__main__":
    unittest.main()
