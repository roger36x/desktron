"""工作工具登录态:后台刷,前台立刻回。

外部 CLI 的状态查询可能要几十秒。超时如果被 `except Exception` 吞成 logged_in=False,
设置页就会对着登着的账号显示未登录、旁边还摆一枚「登录」钮——照它去做是白跑一趟。

做法:请求只读缓存立刻回;缓存过期就起一个后台线程去问,给足 60 秒让 CLI 说完话。
第一次打开设置页拿到 pending,前端隔十秒再取一次,取到为止。
"""
import json
import os
import sys
import threading
import time
import unittest
import _srcmap
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import system.api as sapi  # noqa: E402
finally:
    sys.argv = _argv

SETTINGS = _srcmap.settings_src()


class H:
    def _send_json(self, o, code=200):
        self.out = o
        return o


def _call():
    h = H()
    t = time.perf_counter()
    sapi.handle(h, "/workbench/intranet-status", "", "GET")
    return time.perf_counter() - t, h.out


class NonBlocking(unittest.TestCase):
    def setUp(self):
        sapi._intranet.update({"at": 0.0, "v": None, "busy": False})

    tearDown = setUp

    def test_第一次立刻回_并且标着在查(self):
        slow = threading.Event()
        with mock.patch.object(sapi, "_intranet_probe", lambda: (slow.wait(30), {})[1]):
            d, out = _call()
        self.assertLess(d, 0.2, "请求线程等了探测")
        self.assertTrue(all(v.get("pending") for k, v in out.items() if k != "stale"))
        slow.set()

    def test_探完之后读到真结果(self):
        done = threading.Event()
        real = {"lark": {"installed": True, "logged_in": True}}
        with mock.patch.object(sapi, "_intranet_probe", lambda: (done.set(), real)[1]):
            _call()
            self.assertTrue(done.wait(5), "后台线程没跑起来")
            for _ in range(50):
                if sapi._intranet["v"] is not None:
                    break
                time.sleep(0.02)
            d, out = _call()
        self.assertLess(d, 0.05)
        self.assertEqual(out["lark"]["logged_in"], True)
        self.assertFalse(out["stale"])

    def test_同时只跑一轮(self):
        """八次并发打开设置页不该起八个探测——慢 CLI 会被并发探测打爆。"""
        n = []
        gate = threading.Event()
        def probe():
            n.append(1)
            gate.wait(10)
            return {}
        with mock.patch.object(sapi, "_intranet_probe", probe):
            ths = [threading.Thread(target=_call) for _ in range(8)]
            for t in ths:
                t.start()
            for t in ths:
                t.join(5)
            time.sleep(0.1)
            self.assertEqual(sum(n), 1, "起了 %d 轮探测" % sum(n))
            gate.set()

    def test_探测失败不把_busy_卡住(self):
        """一轮炸了还留着 busy 的话,后面再也不会刷,状态永远停在旧的。"""
        def boom():
            raise RuntimeError("炸了")
        with mock.patch.object(sapi, "_intranet_probe", boom):
            _call()
            for _ in range(50):
                if not sapi._intranet["busy"]:
                    break
                time.sleep(0.02)
        self.assertFalse(sapi._intranet["busy"])

    def test_超时不说成未登录(self):
        """查不到 ≠ 没登录。并成一档会对着登着的账号写「未登录」。"""
        import subprocess as sp
        with mock.patch.object(sapi.subprocess, "run",
                               side_effect=sp.TimeoutExpired("lark-cli", 60)), \
                mock.patch("shutil.which", lambda x: "/usr/bin/" + x):
            got = sapi._intranet_probe()
        self.assertTrue(got["lark"]["unknown"])
        self.assertIn("没有返回", got["lark"]["why"])

    def test_给足时间让慢的说完(self):
        """慢的 CLI 可能要几十秒,上限不能压得太短。"""
        self.assertGreaterEqual(sapi._INTRANET_WAIT, 60)

class LarkCredentialLifecycle(unittest.TestCase):
    def probe(self, user):
        out = json.dumps({"identities": {"bot": {"status": "ready"}, "user": user}})
        with mock.patch.object(sapi.subprocess, "run",
                               return_value=mock.Mock(stdout=out, stderr="")), \
                mock.patch("shutil.which", lambda x: "/usr/bin/" + x):
            return sapi._intranet_probe()["lark"]

    def test_access_token_expiry_does_not_require_login(self):
        # 官方 identitydiag: needs_refresh + available=true，下一次用户 API 自动续期。
        for verified in (None, True):
            with self.subTest(verified=verified):
                got = self.probe({"status": "needs_refresh", "available": True,
                                  "tokenStatus": "needs_refresh", "verified": verified})
                self.assertTrue(got["logged_in"])
                self.assertFalse(got.get("unknown", False))

    def test_missing_user_is_not_masked_by_ready_bot(self):
        for status in ("missing", "not_configured", "unauthorized"):
            with self.subTest(status=status):
                got = self.probe({"status": status, "available": False})
                self.assertFalse(got["logged_in"])
                self.assertFalse(got.get("unknown", False))

    def test_failed_verification_does_not_trust_stale_token_status(self):
        got = self.probe({"status": "verify_failed", "available": False,
                          "verified": False, "tokenStatus": "needs_refresh"})
        self.assertFalse(got["logged_in"])
        self.assertTrue(got["unknown"])

    def test_explicitly_unavailable_credentials_are_not_ready(self):
        for field in ("available", "verified"):
            with self.subTest(field=field):
                self.assertFalse(self.probe({"status": "needs_refresh", field: False})["logged_in"])

    def test_unrecognized_response_does_not_claim_user_logged_out(self):
        got = self.probe({})
        self.assertFalse(got["logged_in"])
        self.assertTrue(got["unknown"])


class Frontend(unittest.TestCase):
    def test_在查时接着取(self):
        self.assertIn("if (r2.pending)", SETTINGS)
        self.assertIn("setTimeout(itFetch, 10000)", SETTINGS)

    def test_有上限不空转(self):
        self.assertIn("++itTries < 8", SETTINGS)

    def test_页面关了就不再取(self):
        self.assertIn("it.isConnected", SETTINGS)

    def test_查不到单成一档(self):
        i = SETTINGS.index("if (r2.unknown)")
        self.assertIn("查不到", SETTINGS[i:i + 300])

    def test_关闭登录终端后失效缓存(self):
        start = SETTINGS.index("async function _authTuiModal")
        end = SETTINGS.index("async function openLoginTUI", start)
        block = SETTINGS[start:end]
        self.assertIn('api("/workbench/setup"', block)
        self.assertIn('JSON.stringify({ refresh: true })', block)


if __name__ == "__main__":
    unittest.main()
