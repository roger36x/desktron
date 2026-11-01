"""Claude 登录必须以独立 CLI 为准，不能把残留钥匙串条目当成可用凭证。"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))

import claude_auth  # noqa: E402
import settings  # noqa: E402


class _Result:
    def __init__(self, payload):
        self.stdout = json.dumps(payload, indent=2)
        self.returncode = 0 if payload.get("loggedIn") else 1


class ClaudeAuthProbe(unittest.TestCase):
    def test_cli_明确未登录时返回_false(self):
        with mock.patch.object(claude_auth, "secrets_env", return_value={"PATH": "/desk/bin"}), \
                mock.patch.object(claude_auth.subprocess, "run", return_value=_Result({"loggedIn": False})) as run:
            self.assertIs(claude_auth.subscription_status(), False)
        self.assertEqual(run.call_args.args[0], ["claude", "auth", "status"])
        self.assertEqual(run.call_args.kwargs["env"]["PATH"], "/desk/bin")

    def test_cli_明确已登录时返回_true(self):
        with mock.patch.object(claude_auth.subprocess, "run", return_value=_Result({"loggedIn": True})):
            self.assertIs(claude_auth.subscription_status(), True)

    def test_订阅登录返回_true(self):
        payload = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}
        with mock.patch.object(claude_auth.subprocess, "run", return_value=_Result(payload)):
            self.assertIs(claude_auth.subscription_status(), True)

    def test_只有_api_key_不算订阅(self):
        """2026-09-09 实测:环境里放一个 ANTHROPIC_API_KEY,没有任何订阅也会得到
        loggedIn: true。照它显示,界面会对一个从没登录过的人写「订阅已登录」。"""
        payload = {"loggedIn": True, "authMethod": "claude.ai",
                   "apiKeySource": "ANTHROPIC_API_KEY", "subscriptionType": None}
        with mock.patch.object(claude_auth.subprocess, "run", return_value=_Result(payload)):
            self.assertIs(claude_auth.subscription_status(), False)

    def test_没有订阅字段的旧版输出仍按_loggedIn_判(self):
        """老版本 CLI 不发 subscriptionType。拿不到证据就不改判,不能把已登录的人判成未登录。"""
        with mock.patch.object(claude_auth.subprocess, "run",
                               return_value=_Result({"loggedIn": True, "authMethod": "claude.ai"})):
            self.assertIs(claude_auth.subscription_status(), True)

    def test_旧版或异常输出返回_unknown(self):
        result = _Result({})
        result.stdout = "not json"
        with mock.patch.object(claude_auth.subprocess, "run", return_value=result):
            self.assertIsNone(claude_auth.subscription_status())

    def test_超时返回_unknown(self):
        with mock.patch.object(claude_auth.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 5)):
            self.assertIsNone(claude_auth.subscription_status())

    def test_环境配置损坏返回_unknown(self):
        with mock.patch.object(claude_auth, "secrets_env",
                               side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")):
            self.assertIsNone(claude_auth.subscription_status())


class ClaudeSettings(unittest.TestCase):
    def test_探测失败在设置页标记_unknown(self):
        with mock.patch.object(settings, "_secrets_read", return_value={}):
            row = settings._gateway_section(("subscription", lambda: None),
                                            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")
        self.assertFalse(row["subscription"])
        self.assertTrue(row["unknown"])

    def test_登录按钮执行当前_cli_认证命令(self):
        with mock.patch.object(settings.subprocess, "Popen") as popen:
            settings._settings_apply({"login": "claude"})
        argv = popen.call_args.args[0]
        self.assertIn("claude auth login", " ".join(argv))


if __name__ == "__main__":
    unittest.main()
