"""Claude 订阅令牌在会话恢复前续期。

SDK 恢复会话时复制钥匙串凭证并删掉 refreshToken，会话进程自己无法续期；
钥匙串令牌过期后每次恢复首轮即 401。派发前在令牌将到期时先让 CLI 续期。
"""
import json
import os
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktron/lib"))

import claude_auth  # noqa: E402


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


class EnsureFresh(unittest.TestCase):
    def setUp(self):
        claude_auth._last_failure = 0.0
        self.calls = []

    def runner(self, after):
        def run(cmd, **kw):
            self.calls.append((cmd, kw))
            self.expiry = after
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return run

    def call(self, expiry, after=None, env=None, now=10_000.0):
        self.expiry = expiry
        return claude_auth.ensure_fresh_subscription(
            "/bin/claude", env=env or {}, now=Clock(now),
            expires_at=lambda: self.expiry, runner=self.runner(after))

    def test_fresh_token_does_not_call_model(self):
        self.assertEqual(self.call(10_000 + 3600), "fresh")
        self.assertEqual(self.calls, [])

    def test_expired_token_is_refreshed_through_default_config(self):
        env = {"CLAUDE_CONFIG_DIR": "/tmp/claude-resume-x", "CLAUDECODE": "1",
               "CLAUDE_CODE_SESSION_ID": "s", "HOME": "/Users/x"}
        self.assertEqual(self.call(9_000, after=10_000 + 8 * 3600, env=env), "refreshed")
        cmd, kw = self.calls[0]
        self.assertIn("--safe-mode", cmd)
        self.assertIn("haiku", cmd)
        self.assertIn("--no-session-persistence", cmd)
        # 必须写回默认钥匙串条目，不能落进恢复副本的配置目录。
        self.assertNotIn("CLAUDE_CONFIG_DIR", kw["env"])
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", kw["env"])
        self.assertEqual(kw["env"]["HOME"], "/Users/x")

    def test_window_matches_cli_refresh_threshold(self):
        # 剩余多于 5 分钟时 CLI 判定 not_needed，提前跑续不了，只白耗额度。
        self.assertEqual(self.call(10_000 + 301), "fresh")
        self.assertEqual(self.call(10_000 + 299, after=10_000 + 3600), "refreshed")

    def test_failed_refresh_backs_off_instead_of_retrying_every_turn(self):
        self.assertEqual(self.call(9_000, after=9_000), "failed")
        self.assertEqual(self.call(9_000, after=9_000, now=10_060), "failed")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.call(9_000, after=9_000, now=10_200), "failed")
        self.assertEqual(len(self.calls), 2)

    def test_api_key_and_third_party_modes_are_untouched(self):
        for env in ({"ANTHROPIC_API_KEY": "k"}, {"ANTHROPIC_BASE_URL": "http://gw"},
                    {"CLAUDE_CODE_USE_BEDROCK": "1"}, {"CLAUDE_CODE_OAUTH_TOKEN": "t"}):
            self.assertEqual(self.call(9_000, env=env), "skipped")
        self.assertEqual(self.calls, [])

    def test_missing_keychain_entry_is_left_to_login_prompt(self):
        self.assertEqual(self.call(None), "skipped")
        self.assertEqual(self.calls, [])

    def test_runner_error_never_propagates(self):
        def boom(*a, **k): raise OSError("no binary")
        self.expiry = 9_000
        self.assertEqual(claude_auth.ensure_fresh_subscription(
            "/bin/claude", env={}, now=Clock(10_000), expires_at=lambda: self.expiry,
            runner=boom), "failed")


class KeychainRead(unittest.TestCase):
    def fake(self, payload, code=0):
        return lambda *a, **k: types.SimpleNamespace(returncode=code, stdout=payload, stderr="")

    @unittest.skipUnless(sys.platform == "darwin", "钥匙串只在 macOS")
    def test_reads_expiry_only_when_refreshable(self):
        good = json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r",
                                             "expiresAt": 1_700_000_000_000}})
        self.assertEqual(claude_auth.keychain_expires_at(self.fake(good)), 1_700_000_000.0)
        stripped = json.dumps({"claudeAiOauth": {"accessToken": "a", "expiresAt": 1}})
        self.assertIsNone(claude_auth.keychain_expires_at(self.fake(stripped)))
        self.assertIsNone(claude_auth.keychain_expires_at(self.fake("", code=44)))
        self.assertIsNone(claude_auth.keychain_expires_at(self.fake("not json")))


class DispatchWiring(unittest.TestCase):
    def test_both_claude_paths_refresh_before_resume(self):
        src = (ROOT / "desktron/lib/shepherd/unified_runners.py").read_text(encoding='utf-8')
        for name in ("def _claude(self", "def _claude_oneshot(self"):
            body = src[src.index(name):]
            body = body[:body.index("\n    def ", 10)]
            self.assertIn("_refresh_claude_subscription(self.env,binary)", body, name)

    def test_test_doubles_never_touch_keychain(self):
        sys.path.insert(0, str(ROOT / "desktron/lib"))
        from shepherd import unified_runners
        self.assertEqual(unified_runners._refresh_claude_subscription(
            {"DESK_SHEPHERD_CLAUDE_BINARY": "/fake"}, "/fake"), "skipped")


if __name__ == "__main__":
    unittest.main()
