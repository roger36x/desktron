"""未登录时,输入框上方那条要能报出来。

回归的是这条:claude 侧此前没有任何开会话前的状态提示,没登录要等第一个回合失败才知道,
而失败信息落在状态行里,与别的崩溃混在一起。这里把 env_notice 的三种输入各跑一遍:
配了 key 不报、CLI 已登录不报、两者都没有才报,并且帧里带得出可执行的动作。
"""
import os
import sys
import threading
import time
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))

import engine_adapters as ea  # noqa: E402
import claude_auth  # noqa: E402
import credstore  # noqa: E402


class EnvNotice(unittest.TestCase):
    def setUp(self):
        self.ad = ea.ClaudeAdapter.__new__(ea.ClaudeAdapter)
        self.key = os.environ.pop("ANTHROPIC_API_KEY", None)
        self.probe = claude_auth.subscription_status
        self.secrets_read = credstore._secrets_read
        credstore._secrets_read = lambda: {}
        # 缓存挂在函数的默认参数上,每个用例都要清,否则第一个用例的结论会盖住后面的
        ea.ClaudeAdapter._ENV_CACHE.clear()

    def tearDown(self):
        claude_auth.subscription_status = self.probe
        credstore._secrets_read = self.secrets_read
        os.environ.pop("ANTHROPIC_API_KEY", None)
        if self.key is not None:
            os.environ["ANTHROPIC_API_KEY"] = self.key
        ea.ClaudeAdapter._ENV_CACHE.clear()

    def _subscription(self, state):
        claude_auth.subscription_status = lambda: state

    def test_login_refresh_invalidates_preflight_cache(self):
        import settings
        ea.ClaudeAdapter._ENV_CACHE.update(at=1, v={'state':'warning'})
        settings.invalidate_auth_probe_caches()
        self.assertEqual(ea.ClaudeAdapter._ENV_CACHE,{})

    def test_cli_已登录_不报(self):
        self._subscription(True)
        self.assertIsNone(self.ad.env_notice())

    def test_配了_key_不查钥匙串也不报(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"

        def boom(*a, **k):
            raise AssertionError("配了 key 就不该再启动 CLI 探测")

        claude_auth.subscription_status = boom
        self.assertIsNone(self.ad.env_notice())

    def test_两者都没有_报一条可执行的警告(self):
        self._subscription(False)
        row = self.ad.env_notice()
        self.assertIsInstance(row, dict)
        self.assertEqual(row["state"], "warning")
        self.assertEqual(row["stage"], "auth")
        self.assertIn("未登录", row["label"])
        self.assertTrue(row["details"], "要给出为什么这么判,否则用户无从下手")
        details = " ".join(row["details"])
        self.assertIn("Claude Desktop", details)
        self.assertIn("claude auth login", details)
        self.assertIn("claude doctor", details)
        self.assertEqual(row["action"]["kind"], "terminal", "登录只能在终端里做")
        self.assertTrue(row["action"]["label"])

    def test_探测失败不误报未登录(self):
        """查不到时不能喊——错报比漏报更烦人,真没登录第一个回合会自己失败。"""
        self._subscription(None)
        self.assertIsNone(self.ad.env_notice())

    def test_登录与未知状态在_ttl_内只探测一次(self):
        for state in (True, None):
            with self.subTest(state=state):
                calls = []

                def probe():
                    calls.append(state)
                    return state

                claude_auth.subscription_status = probe
                self.assertIsNone(self.ad.env_notice())
                self.assertIsNone(self.ad.env_notice())
                self.assertEqual(calls, [state])
                ea.ClaudeAdapter._ENV_CACHE.clear()

    def test_并发预检共用一次_cli_探测(self):
        calls = []
        results = []
        start = threading.Barrier(3)

        def probe():
            calls.append(True)
            time.sleep(0.05)
            return True

        def check():
            start.wait()
            results.append(self.ad.env_notice())

        claude_auth.subscription_status = probe
        workers = [threading.Thread(target=check) for _ in range(2)]
        for worker in workers:
            worker.start()
        start.wait()
        for worker in workers:
            worker.join()
        self.assertEqual(calls, [True])
        self.assertEqual(results, [None, None])

    def test_返回的是副本_调用方改不脏缓存(self):
        self._subscription(False)
        a = self.ad.env_notice()
        a["label"] = "改坏了"
        b = self.ad.env_notice()
        self.assertIn("未登录", b["label"])


class ConfigCarriesNotice(unittest.TestCase):
    def test_config_端点带出_environment(self):
        src = _srcmap.ui_server_src()
        seg = src[src.index('if p == "/config"'):]
        seg = seg[:seg.index("if p ==", 10)]
        self.assertIn("env_notice", seg, "/config 应把 env_notice 带给界面")
        self.assertIn('"environment"', seg)
        self.assertEqual(seg.count('"environment":'), 1, "同名键会让后一项静默覆盖会话现场")
        self.assertIn("environment = ad.session_environment", seg)
        self.assertIn("if not environment", seg, "只有无会话异常时才应回退到引擎级预检")

    def test_基类给了默认实现(self):
        """别的引擎没实现也不能让 /config 抛错。"""
        self.assertIsNone(ea.CliAdapter.env_notice(object()))


if __name__ == "__main__":
    unittest.main()
