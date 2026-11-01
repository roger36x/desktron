"""Shepherd Claude must request thinking summaries like the native Claude adapter.

Pinned Claude Code 2.1.268 leaves thinking blocks empty (signature only) unless
`--thinking-display summarized` is passed; 2026-09-14 probe with the pinned SDK:
resume without the flag -> thinking length 0, with the flag -> non-empty summary.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / 'desktron' / 'lib' / 'shepherd' / 'claude_runner.mjs'
OPTIONS = ROOT / 'desktron' / 'lib' / 'shepherd' / 'claude_sdk_options.mjs'
NATIVE = ROOT / 'desktron' / 'lib' / 'engine_adapters' / 'claude.py'


class ThinkingDisplay(unittest.TestCase):
    def test_bridge_requests_summaries_by_default(self):
        source = BRIDGE.read_text(encoding='utf-8')
        normalized = OPTIONS.read_text(encoding='utf-8')
        self.assertIn('claudeExtraArgs(payload.thinkingDisplay,payload.autocompact)', source)
        self.assertIn("args['thinking-display']=thinkingDisplay ?? 'summarized'", normalized)

    def test_bridge_does_not_force_adaptive_thinking(self):
        """`options.thinking` would also pass --thinking adaptive; only the display is wanted."""
        source = BRIDGE.read_text(encoding='utf-8')
        self.assertIsNone(re.search(r'\bthinking\s*:\s*\{', source))

    def test_native_adapter_keeps_same_default(self):
        self.assertIn('"--thinking-display", opts.get("thinking_display") or "summarized"',
                      NATIVE.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
