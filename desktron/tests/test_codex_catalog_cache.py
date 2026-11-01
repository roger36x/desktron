"""Codex 模型目录不该挡在切会话的路上。

2026-08-11 实测:`GET /config` 首次 5164ms、之后 58ms(claude 侧恒定 20ms)。差的那 5 秒全在
`codex_appserver.catalog()`——起一个 app-server 进程再问两轮 RPC。而 /config 是切会话必经的
一站,缓存(当时只在内存、TTL 300 秒)一过期,那 5 秒就整个压在切换上。

目录按版本变不按分钟变,所以:TTL 放到一天、落盘让重启后也能直接用、拿不到就先返回空目录
让界面走兜底档位表,后台补。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))

import codex_appserver as ca  # noqa: E402


class CatalogCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._path = os.path.join(self.tmp.name, "codex-catalog.json")
        self.p = mock.patch.object(ca, "_catalog_cache_path", lambda binary="codex": self._path)
        self.p.start()
        ca._CATALOGS.clear()
        ca._CATALOG_FETCHING.clear()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()
        ca._CATALOGS.clear()

    def test_没有缓存时不阻塞_先给空目录再后台补(self):
        seen = []
        with mock.patch.object(ca, "_catalog_refresh_async", lambda *a, **k: seen.append(1)):
            got = ca.catalog(cwd="/tmp")
        self.assertEqual(got, {"models": [], "modes": []})   # 空目录:界面走自己的兜底档位表
        self.assertEqual(len(seen), 1)                        # 但后台已经去拉了

    def test_盘上有就直接用_不必再起进程(self):
        val = {"models": [{"id": "gpt-5.5"}], "modes": [{"mode": "default"}]}
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump({"at": time.time(), "value": val}, f)
        with mock.patch.object(ca, "_catalog_fetch", side_effect=AssertionError("不该真去拉")):
            self.assertEqual(ca.catalog(cwd="/tmp"), val)

    def test_过期了先用旧值_同时后台换新(self):
        val = {"models": [{"id": "old"}], "modes": []}
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump({"at": time.time() - ca._CATALOG_TTL - 10, "value": val}, f)
        seen = []
        with mock.patch.object(ca, "_catalog_refresh_async", lambda *a, **k: seen.append(1)):
            self.assertEqual(ca.catalog(cwd="/tmp"), val)   # 不是空的:旧目录也比没有强
        self.assertEqual(len(seen), 1)

    def test_拉到之后写盘_重启不必重来(self):
        val = {"models": [{"id": "gpt-5.5"}], "modes": []}

        class FakeConn:
            def request(self, method, params, on_notification=None):
                return {"data": val["models"] if method == "model/list" else val["modes"]}

        got = ca._catalog_fetch(conn=FakeConn())
        self.assertEqual(got, val)
        with open(self._path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["value"], val)

    def test_带连接来的照旧同步拉(self):
        """跑回合那条路要拿确定值选模型,而且它复用现有连接,不会另起进程。"""
        val = {"models": [{"id": "x"}], "modes": []}

        class FakeConn:
            def request(self, method, params, on_notification=None):
                return {"data": val["models"] if method == "model/list" else val["modes"]}

        self.assertEqual(ca.catalog(conn=FakeConn(), max_age=0), val)

    def test_同一时刻只跑一个后台拉取(self):
        ca._CATALOG_FETCHING.add("codex")
        with mock.patch.object(ca, "_catalog_fetch", side_effect=AssertionError("不该并发")):
            ca._catalog_refresh_async(cwd="/tmp")   # 已有一个在跑:直接返回

    def test_TTL_按天算不按分钟算(self):
        self.assertGreaterEqual(ca._CATALOG_TTL, 3600)


if __name__ == "__main__":
    unittest.main()
