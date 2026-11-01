"""客户端提前挂断不该被当成服务端错误;取历史失败不该静默。

2026-08-10 实见两件事:
1. 一次页面重载里五个并发 /config 同时 BrokenPipe,socketserver 把整条 traceback 打进日志环,
   一百多行,真正要看的那句(回合异常收尾的原话)被冲掉。
2. sync home 触发重载,重载把飞行中的请求掐断,之后 GUI 会话内容一直是空的,不会自己恢复——
   hsLoad 的 catch 直接 return,不重试、不报错、不留痕。

ui_server 有模块级副作用(装信号、起看门狗),测试不导入它,只钉源码上的这两处约定。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRV = _srcmap.ui_server_src()
CHAT = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


class QuietDisconnect(unittest.TestCase):
    def test_挂断静默收场(self):
        m = re.search(r"def handle_one_request\(self\):[\s\S]+?\n    def ", SRV)
        self.assertTrue(m, "Handler 没有覆写 handle_one_request")
        seg = m.group(0)
        self.assertIn("super().handle_one_request()", seg)
        self.assertIn("except (BrokenPipeError, ConnectionResetError):", seg)
        self.assertIn("self.close_connection = True", seg)

    def test_只吞这两类_别的照常抛(self):
        seg = re.search(r"def handle_one_request\(self\):[\s\S]+?\n    def ", SRV).group(0)
        self.assertNotIn("except Exception", seg)


class LoadRetries(unittest.TestCase):
    def test_取历史失败会重试(self):
        m = re.search(r"async function hsLoad\(v\)[\s\S]+?\n\}", CHAT)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn("for (let i = 0; i < 3", seg)      # 退避重试,不是一次就放弃
        self.assertIn("setTimeout", seg)
        self.assertNotIn("catch { return; }", seg)       # 原来的静默返回

    def test_到底失败时留一条出路(self):
        self.assertIn("function hsLoadFailed(v)", CHAT)
        self.assertIn("没能取到这段会话的历史", CHAT)
        m = re.search(r"function hsLoadFailed\(v\)[\s\S]+?\n\}", CHAT)
        self.assertIn("重试", m.group(0))
        self.assertIn("hsLoad(v)", m.group(0))           # 点了要真的再取一次
        self.assertIn(".hs-page .hs-loadfail", CSS)

    def test_成功之后把失败行撤掉(self):
        seg = re.search(r"async function hsLoad\(v\)[\s\S]+?\n\}", CHAT).group(0)
        # 首屏改成分两批铺(hsPourFast:先铺看得见的末尾,其余空闲时补),
        # 这里只关心「撤掉失败行」发生在灌历史之前,认函数名前缀即可。
        self.assertLess(seg.index("hsLoadFailedClear(v)"), seg.index("hsPourFast(v, msgs)"))


class OneEndHeaders(unittest.TestCase):
    """一个类里只能有一个 end_headers。

    2026-08-10 实见:加缓存分档时另写了一个,而它排在补种 Cookie 那个之前——Python 后定义的
    覆盖前面的,于是缓存分档整段成了死代码。实测两种 URL 都不带任何 Cache-Control,
    比改之前更糟:连原来的 no-store 也没了,WKWebView 会按启发式自行缓存旧文件。
    """

    def test_只定义一次(self):
        self.assertEqual(SRV.count("    def end_headers(self):"), 1)

    def test_两件事都在这一个里(self):
        m = re.search(r"    def end_headers\(self\):[\s\S]+?\n    def ", SRV)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn("max-age=31536000, immutable", seg)     # 带指纹的长缓存
        self.assertIn("no-store, must-revalidate", seg)       # 其余禁缓存
        self.assertIn("_set_remote_cookie", seg)              # 远端令牌补种 Cookie
        self.assertIn("super().end_headers()", seg)


if __name__ == "__main__":
    unittest.main()
