"""站点图标的解析结果按来源落盘(lib/favicon.py)。

原来只有进程内缓存,Desk 一重启就全忘:浏览器标签恢复时每个站点都要重新出外网抓一次
首页,2026-09-05 实测开机 20 秒内六次、合计 12 秒——正是页面取首屏数据的那一段。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import _srcmap  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import favicon  # noqa: E402


class DiskCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "cache", "favicon.json")
        p = mock.patch.object(favicon, "_cache_path", return_value=self.path)
        p.start(); self.addCleanup(p.stop)
        favicon._cache.clear()
        favicon._loaded = False
        self.addCleanup(favicon._cache.clear)

    def test_抓到之后落盘(self):
        with mock.patch.object(favicon.urllib.request, "urlopen", side_effect=OSError("no net")):
            favicon.resolve("https://example.test/x")
        with open(self.path, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertIn("https://example.test", raw)

    def test_重启后先读盘不出网(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"https://cached.test": [favicon.time.time(), "https://cdn.test/i.png"]}, f)
        favicon._cache.clear(); favicon._loaded = False
        with mock.patch.object(favicon.urllib.request, "urlopen") as m:
            r = favicon.resolve("https://cached.test/page")
            m.assert_not_called()
        self.assertEqual(r["href"], "https://cdn.test/i.png")

    def test_盘上过期的照旧重抓(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"https://old.test": [favicon.time.time() - 2 * favicon._TTL, "https://cdn.test/old.png"]}, f)
        favicon._cache.clear(); favicon._loaded = False
        with mock.patch.object(favicon.urllib.request, "urlopen", side_effect=OSError("no net")) as m:
            favicon.resolve("https://old.test/")
            m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
