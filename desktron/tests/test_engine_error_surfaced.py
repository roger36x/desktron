"""引擎自己报的失败要走到界面上,不能悄悄收尾。

codex 家族经 app-server 的 JSON-RPC 跑回合。引擎报失败有三条路:
`turn/failed`、`error`、以及 `turn/completed` 带 `status: failed`。三条都把说明记进
`ctx["engine_error"]`——而在 2026-08-18 之前,全仓六处赋值、零处读取。

后果:RPC 本身没出错,`_turn_dispatch` 返回 None,`_finish_turn` 于是判这一轮「正常收尾」,
发 session.idle。界面上就是一句「已完成」,正文一个字都没有。用户看到的是「会话无缘无故
结束」(实见:存储里连着三条用户消息没有任何助手记录)。

修法是在唯一那条收尾路径上读它:报错本身之外,顺带把「回合异常收尾」的日志与 local_only
标记也补齐——那两样此前同样漏掉了。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = _srcmap.engine_adapters_src()


def fn(name):
    m = re.search(r"\n    def %s\(self.*?\n    def " % re.escape(name), SRC, re.S)
    assert m, "%s 不见了" % name
    return m.group(0)


class EngineErrorReachesUI(unittest.TestCase):
    def test_收尾时读它(self):
        seg = fn("_finish_turn")
        self.assertIn('abnormal = abnormal or ctx.get("engine_error")', seg)

    def test_读在判正常收尾之前(self):
        """ended_clean 决定 local_only 标记;读晚了标记就还是错的。"""
        seg = fn("_finish_turn")
        self.assertLess(seg.index('ctx.get("engine_error")'), seg.index("ended_clean ="))

    def test_异常收尾会发_session_error(self):
        seg = fn("_finish_turn")
        i = seg.index("if abnormal:")
        self.assertIn('self._publish_turn_error(sid, ctx, error)', seg[i:])
        self.assertIn("'type': 'session.error'", fn('_publish_turn_error'))
        self.assertIn("回合异常收尾", seg[i:])

    def test_三条报错路径都记进同一个键(self):
        """codex 那侧的三条:turn/completed(failed)、error、turn/failed。

        2026-08-19 起三条都经 _turn_failed 走:它除了记这个键,还把说明挂到输入框上方
        那条窄条上(见 test_frontend_env_bar.CodexTurnFailure)。所以这里认的是那个入口,
        再由下一条用例钉住入口本身确实写了这个键。"""
        seg = re.search(r'^        if method == "turn/completed":[\s\S]+?return\n', SRC, re.M).group(0)
        self.assertIn("self._turn_failed(ctx,", seg)
        self.assertIn('if method in ("error", "turn/failed"):', SRC)

    def test_那个入口确实写了这个键(self):
        """收尾只读 ctx["engine_error"];入口改道之后若忘了写,回合又会静默判成正常。"""
        seg = fn("_turn_failed")
        self.assertIn('ctx["engine_error"] = msg', seg)
        self.assertIn("_env_publish", seg)

    def test_用户主动停的不算故障(self):
        """打断把 abnormal 清掉,免得「我自己按的停止」被报成引擎故障。"""
        seg = fn("_finish_turn")
        self.assertIn("abnormal = None", seg)
        self.assertLess(seg.index('ctx.get("aborted")'), seg.index("abnormal = None"))


if __name__ == "__main__":
    unittest.main()
