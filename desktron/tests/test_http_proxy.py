"""Workbench HTTP proxy tests.

Run: python3 -m unittest desktron/tests/test_http_proxy.py
"""
import base64
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status = status
        self._body = body if isinstance(body, bytes) else (body or "").encode("utf-8")
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append({"req": req, "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_jwt(exp):
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return header + "." + payload + ".sig"


class WorkbenchHttpProxy(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="http-proxy-test-")
        self.repo = os.path.join(self.base, "repo")
        self.frontend = os.path.join(self.base, "frontend")
        self.data = os.path.join(self.base, "data")
        for path in (self.repo, self.frontend, self.data):
            os.makedirs(path, exist_ok=True)
        os.environ["DESKTRON_DATA_HOME"] = self.data
        sys.argv = ["test", self.frontend, "0", "0", self.repo]
        lib = os.path.join(os.path.dirname(__file__), "..", "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        sys.modules.pop("http_proxy", None)
        self.proxy = importlib.import_module("http_proxy")
        self.proxy.clear_token_cache()
        self.proxy.UPSTREAMS["example-api"] = {
            "base_url": "https://api.example.com",
            "allowed_methods": {"GET", "POST"},
            "allowed_path": re.compile(r"^/v1/[A-Za-z0-9_./-]+$"),
            "default_headers": {"Accept": "application/json", "Origin": "https://console.example.com"},
            "allowed_request_headers": {"accept", "content-type"},
            "auth": {"type": "command-jwt", "command": ["example-cli", "token"], "header": "X-jwt-token"},
            "timeout": 25,
        }

    def tearDown(self):
        sys.modules.pop("http_proxy", None)
        shutil.rmtree(self.base, ignore_errors=True)

    def test_proxy_adds_command_jwt_and_forwards_json_response(self):
        opener = FakeOpener([
            FakeResponse(200, json.dumps({"error_code": 0, "data": ["svc.alpha.monitor"]}))
        ])
        token_calls = []

        def token_fetcher(profile):
            token_calls.append(profile)
            return make_jwt(2000)

        status, body = self.proxy.handle_proxy_request({
            "upstream": "example-api",
            "method": "GET",
            "path": "/v1/services/fuzzy",
            "query": {"keyword": "svc.alpha"},
            "debug": True,
        }, opener=opener, token_fetcher=token_fetcher, now=lambda: 1000)

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["body"]["data"], ["svc.alpha.monitor"])
        self.assertEqual(len(token_calls), 1)
        req = opener.requests[0]["req"]
        self.assertEqual(req.full_url, "https://api.example.com/v1/services/fuzzy?keyword=svc.alpha")
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.headers["X-jwt-token"], make_jwt(2000))
        self.assertEqual(req.headers["Origin"], "https://console.example.com")
        self.assertEqual(body["debug"]["target_url"], req.full_url)
        self.assertEqual(body["debug"]["token_source"], "fetched")
        self.assertNotIn(make_jwt(2000), json.dumps(body))

    def test_proxy_reuses_cached_jwt_until_refresh_window(self):
        opener = FakeOpener([
            FakeResponse(200, '{"ok": true}'),
            FakeResponse(200, '{"ok": true}'),
            FakeResponse(200, '{"ok": true}'),
        ])
        tokens = [make_jwt(2000), make_jwt(4000)]
        token_calls = []

        def token_fetcher(profile):
            token_calls.append(profile)
            return tokens[len(token_calls) - 1]

        payload = {
            "upstream": "example-api",
            "path": "/v1/service/apis",
            "query": {"psm": "svc.alpha.monitor"},
        }
        self.proxy.handle_proxy_request(payload, opener=opener, token_fetcher=token_fetcher, now=lambda: 1000)
        self.proxy.handle_proxy_request(payload, opener=opener, token_fetcher=token_fetcher, now=lambda: 1500)
        self.proxy.handle_proxy_request(payload, opener=opener, token_fetcher=token_fetcher, now=lambda: 1710)

        self.assertEqual(len(token_calls), 2)
        self.assertEqual(opener.requests[0]["req"].headers["X-jwt-token"], tokens[0])
        self.assertEqual(opener.requests[1]["req"].headers["X-jwt-token"], tokens[0])
        self.assertEqual(opener.requests[2]["req"].headers["X-jwt-token"], tokens[1])

    def test_proxy_retries_once_after_unauthorized_response(self):
        opener = FakeOpener([
            FakeResponse(401, '{"error": "expired"}'),
            FakeResponse(200, '{"ok": true}'),
        ])
        tokens = [make_jwt(2000), make_jwt(3000)]
        token_calls = []

        def token_fetcher(profile):
            token_calls.append(profile)
            return tokens[len(token_calls) - 1]

        status, body = self.proxy.handle_proxy_request({
            "upstream": "example-api",
            "path": "/v1/service/api_schema",
            "query": {"psm": "svc.alpha.monitor", "func_name": "GetMonitorDetail"},
            "debug": True,
        }, opener=opener, token_fetcher=token_fetcher, now=lambda: 1000)

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(token_calls), 2)
        self.assertEqual(opener.requests[0]["req"].headers["X-jwt-token"], tokens[0])
        self.assertEqual(opener.requests[1]["req"].headers["X-jwt-token"], tokens[1])
        self.assertEqual(body["debug"]["retry"], "refreshed-token-after-401")

    def test_proxy_does_not_refresh_jwt_after_forbidden_response(self):
        opener = FakeOpener([
            FakeResponse(403, '{"error": "forbidden"}'),
        ])
        token_calls = []

        def token_fetcher(profile):
            token_calls.append(profile)
            return make_jwt(2000)

        status, body = self.proxy.handle_proxy_request({
            "upstream": "example-api",
            "path": "/v1/service/api_schema",
            "query": {"psm": "svc.alpha.monitor", "func_name": "GetMonitorDetail"},
            "debug": True,
        }, opener=opener, token_fetcher=token_fetcher, now=lambda: 1000)

        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])
        self.assertEqual(len(token_calls), 1)
        self.assertEqual(len(opener.requests), 1)
        self.assertNotIn("retry", body["debug"])

    def test_proxy_preserves_allowed_content_type_for_json_body(self):
        opener = FakeOpener([FakeResponse(200, '{"ok": true}')])

        status, body = self.proxy.handle_proxy_request({
            "upstream": "example-api",
            "method": "POST",
            "path": "/v1/service/api_schema",
            "headers": {"content-type": "application/x.custom+json"},
            "body": {"psm": "svc.alpha.monitor"},
        }, opener=opener, token_fetcher=lambda _: make_jwt(2000), now=lambda: 1000)

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        req = opener.requests[0]["req"]
        self.assertEqual(req.headers["Content-type"], "application/x.custom+json")

    def test_proxy_rejects_unregistered_upstream_and_disallowed_path(self):
        status, body = self.proxy.handle_proxy_request({"upstream": "unknown", "path": "/x"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertIn("upstream", body["error"])

        status, body = self.proxy.handle_proxy_request({
            "upstream": "example-api",
            "path": "https://metadata.google.internal/latest",
        })
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertIn("path", body["error"])

        status, body = self.proxy.handle_proxy_request({
            "upstream": "example-api",
            "path": "/internal/admin",
        })
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])
        self.assertIn("not allowed", body["error"])

    def test_redirect_handler_does_not_follow_redirects(self):
        handler = self.proxy.NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://example.com/next"))


if __name__ == "__main__":
    unittest.main()
