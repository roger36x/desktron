"""改档位要广播给其余客户端。

回归的是这条:档位是会话的属性,不是某个页面的本地状态。原来 POST /session/<sid>/settings
只把新值回给发起那一端,不发事件;别处的页面停在自己打开时读到的那份——同一段会话在 mac 上
调成 max,手机上仍显示 high,除非把会话页关掉重开(2026-08-25 用户实见)。

钉三件:后端改完要发 session.settings、发在锁外(publish 会走到订阅者,持锁调外部代码
是自找死锁)、前端收到这一帧要就地重画而不是再打一趟 /config(那条路上没有 /config 可取,
何况经隧道一个往返就要一两秒)。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EA = _srcmap.engine_adapters_src()
CHAT = _srcmap.chat_src()


def _fn(src, head):
    i = src.index(head)
    j = src.index("\n    def ", i + 1)
    return src[i:j]


class Backend(unittest.TestCase):
    def setUp(self):
        self.seg = _fn(EA, "    def update_session_options(self")

    def test_改完要广播(self):
        self.assertIn('"type": "session.settings"', self.seg)
        self.assertIn('"sessionID": sid', self.seg)
        self.assertIn('"opts"', self.seg)

    def test_广播发在锁外(self):
        """publish 会走到订阅者。持着 self._lock 调外部代码,对方再回头碰适配器就死锁。

        判据用缩进:`with self._lock:` 开在方法体那一级(8 空格),块内是 12 空格。
        publish 所在的行只要回到 8 空格,就说明锁已经退出了。
        """
        for line in self.seg.splitlines():
            if "self.bus.publish" in line:
                indent = len(line) - len(line.lstrip())
                self.assertEqual(indent, 12,
                                 "publish 应在方法体的 try 里(12),不在 with self._lock 块内(16)")
                break
        else:
            self.fail("没找到 publish")
        # 而且要先在锁内把结果取成副本,退出锁之后再用——不能在锁外回头读 entry
        self.assertIn("out = dict(entry[", self.seg)

    def test_不在意外分支上静默丢掉(self):
        self.assertIn("if entry is None:", self.seg, "找不到会话仍要照旧回 None")


class Frontend(unittest.TestCase):
    def test_有_session_settings_分支(self):
        self.assertIn('if (type === "session.settings")', CHAT)

    def test_收到就地重画_不再打一趟接口(self):
        i = CHAT.index('if (type === "session.settings")')
        seg = CHAT[i:CHAT.index('if (type === "session.bgtasks")', i)]
        self.assertIn("hsOptsPaint(v)", seg)
        self.assertNotIn("api(", seg, "这条路上不该再发请求:经隧道一个往返就要一两秒")

    def test_shepherd_迟到引擎事件只更新缓存不重画当前控件(self):
        i = CHAT.index('if (type === "session.settings")')
        seg = CHAT[i:CHAT.index('if (type === "session.bgtasks")', i)]
        self.assertIn('if (!v.shepherdMode || eventEngine === v.engine) hsOptsPaint(v)', seg)

    def test_重画函数是独立的(self):
        """两个调用方:hsEngineSync 取完 /config,和 SSE 这一帧。绘制不能和取数绑死。"""
        self.assertIn("function hsOptsPaint(v)", CHAT)
        self.assertGreaterEqual(CHAT.count("hsOptsPaint(v)"), 2)

    def test_两个写各自成败(self):
        """会话级那条失败,不该连累引擎级那条——原来串在一个 try 里,前者一抛后者不执行。"""
        i = CHAT.index("async function hsSetOpt(")
        seg = CHAT[i:CHAT.index("\nfunction ", i)]
        self.assertEqual(seg.count("try {"), 2, "两个写要各自 try")
        self.assertNotIn("} catch {}", seg, "失败不能无声吞掉")
        self.assertEqual(seg.count("deskLog("), 2, "两处失败都要留痕")

    def test_断线重连要核一次档位(self):
        seg = CHAT[CHAT.index("function hsRecoverLiveViews"):]
        seg = seg[:seg.index("\nfunction ")]
        self.assertIn("hsEngineSync(v)", seg,
                      "服务端不重放断线期间的事件,漏掉的那一帧只能靠重连时回读")


if __name__ == "__main__":
    unittest.main()
