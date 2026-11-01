"""Offline regressions for Windows search proxy/blank-page and CLI transport.

No GUI is launched. Runtime PID authorization is not changed by these fixes.
"""
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class BrowserSearch(unittest.TestCase):
    def test_public_webviews_do_not_force_direct_connections(self):
        app = (ROOT / "desktop/windows/src/app.rs").read_text(encoding="utf-8")
        args = re.search(r'pub const BROWSER_ARGS: &str = "([^"]+)";', app).group(1)
        self.assertNotIn("--no-proxy-server", args)
        self.assertNotIn("--proxy-server", args)
        self.assertNotIn("--ignore-certificate-errors", args)
        views = (ROOT / "desktop/windows/src/views.rs").read_text(encoding="utf-8")
        self.assertIn("crate::network::browser_args(BROWSER_ARGS)", app)
        self.assertIn(".with_additional_browser_args(&browser_args)", app)
        self.assertEqual(views.count(".with_additional_browser_args(&self.browser_args)"), 2)
        self.assertIn(".with_incognito(isolated)", views)
        self.assertIn("policy.allowed_hosts.contains(&h)", views)

    def test_settle_loop_waits_for_non_null_matching_sample_and_preserves_timeout(self):
        control = (ROOT / "desktop/windows/src/control.rs").read_text(encoding="utf-8")
        self.assertIn('include_str!("../js/page-settled.js")', control)
        self.assertIn("last: Option<String>", control)
        self.assertIn("if n.is_some() && n == last", control)
        self.assertIn("return done(self, Some(false))", control)
        self.assertIn("Instant::now() + Duration::from_secs_f64(budget), None", control)

    @unittest.skipUnless(shutil.which("node"), "node required")
    def test_blank_loading_and_same_size_navigation_samples(self):
        script = (ROOT / "desktop/windows/js/page-settled.js").read_text(encoding="utf-8")
        harness = r"""
const vm = require('node:vm');
const sample = (url, ready, count) => vm.runInNewContext(SCRIPT, {
  location: {href: url}, document: {readyState: ready, getElementsByTagName: () => ({length: count})}
});
const assert = require('node:assert/strict');
assert.equal(sample('about:blank', 'complete', 3), null);
assert.equal(sample('https://duckduckgo.com/?q=test', 'loading', 3), null);
assert.equal(sample('https://duckduckgo.com/?q=test', 'interactive', 3), null);
const a = sample('https://duckduckgo.com/?q=test', 'complete', 100);
assert.equal(a, sample('https://duckduckgo.com/?q=test', 'complete', 100));
assert.notEqual(a, sample('https://duckduckgo.com/?q=other', 'complete', 100));
assert.notEqual(a, sample('https://duckduckgo.com/?q=test', 'complete', 101));
""".replace("SCRIPT", json.dumps(script))
        proc = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class LocalTransport(unittest.TestCase):
    def test_cli_and_mcp_select_only_injected_loopback_or_unix_channel(self):
        for name in ("app-control", "app-control-mcp"):
            with self.subTest(name=name):
                module = runpy.run_path(str(ROOT / "desktron/bin" / name))
                connection = module["_channel_connection"]("tcp:127.0.0.1:12345", 7)
                self.assertIsInstance(connection, http.client.HTTPConnection)
                self.assertEqual((connection.host, connection.port, connection.timeout), ("127.0.0.1", 12345, 7))
                self.assertIsInstance(module["_channel_connection"]("/tmp/test.sock", 7), module["UnixHTTPConnection"])
                for invalid in ("tcp:example.com:123", "tcp:127.0.0.2:123", "tcp:127.0.0.1:0", "tcp:127.0.0.1:65536", "tcp:127.0.0.1:abc"):
                    with self.assertRaisesRegex(RuntimeError, "app_control_unavailable"):
                        module["_channel_connection"](invalid, 7)

    def test_cli_uses_dedicated_channel_without_public_http_fallback(self):
        # This local fixture verifies channel selection and header forwarding,
        # not Runtime's kernel PID authorization (covered separately).
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("X-Desk-App-Control-Session")))
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            module = runpy.run_path(str(ROOT / "desktron/bin/app-control"))
            env = {"DESKTRON_APP_CONTROL_SOCKET": f"tcp:127.0.0.1:{server.server_port}"}
            with patch.dict(os.environ, env), patch("urllib.request.urlopen", side_effect=AssertionError("public fallback")):
                result = module["_unix_request"]("/workbench/app-control/probe", None,
                                                 {"X-Desk-App-Control-Session": "fixture-only"}, 3)
            self.assertEqual(result, {"ok": True})
            self.assertEqual(seen, [("/workbench/app-control/probe", "fixture-only")])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
