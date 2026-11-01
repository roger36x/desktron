"""Desk App Control MCP transport 的无网络协议回归。"""
import json
import os
import subprocess
import sys
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SERVER = os.path.join(ROOT, "desktron", "bin", "app-control-mcp")


class AppControlMcp(unittest.TestCase):
    def test_initialize_and_tool_catalog_do_not_touch_browser(self):
        frames = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        proc = subprocess.run(
            [sys.executable, SERVER],
            input="".join(json.dumps(row) + "\n" for row in frames),
            text=True, encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            capture_output=True, timeout=2, check=True)
        rows = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(rows[0]["result"]["serverInfo"]["name"], "desk-app-control")
        tools = {row["name"] for row in rows[1]["result"]["tools"]}
        self.assertIn("browser_status", tools)
        self.assertIn("app_inspect", tools)
        self.assertIn("app_act", tools)
        self.assertNotIn("browser_grants", tools)
        inspect = next(row for row in rows[1]["result"]["tools"]
                       if row["name"] == "app_inspect")
        self.assertIn("input", inspect["inputSchema"]["properties"])

    def test_inspect_forwards_declared_observation_input(self):
        with open(SERVER, encoding="utf-8") as stream:
            source = stream.read()
        self.assertIn('"input": args.get("input") or {}', source)
        self.assertNotIn('"name": "browser_grants"', source)


if __name__ == "__main__":
    unittest.main()
