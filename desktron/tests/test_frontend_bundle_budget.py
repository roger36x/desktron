"""首屏源码可拆，网络请求不能跟着膨胀。

8 月 27 日把五个大文件拆成 33 片后，index.html 也逐片引用，首屏同步脚本从 16 个
涨到 44 个。测试只验证源码拼接等价，没守住浏览器实际付出的请求预算。本文件钉住
运行时合包：源码仍按片维护，服务端按原顺序拼成五个内容指纹资源。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
FRONTEND = os.path.join(ROOT, "frontend")
sys.path.insert(0, LIB)

from frontend_bundle import ASSET_BUNDLES, bundle_data  # noqa: E402


INDEX = open(os.path.join(FRONTEND, "index.html"), encoding="utf-8").read()


class RequestBudget(unittest.TestCase):
    def test_首屏脚本不超过优化前预算(self):
        scripts = re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', INDEX)
        self.assertLessEqual(len(scripts), 16, "首屏脚本请求预算又膨胀了:%d" % len(scripts))

    def test_五组拆分源码各走一个运行时资源(self):
        scripts = re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', INDEX)
        for bundle, parts in ASSET_BUNDLES.items():
            self.assertIn(bundle, scripts, bundle + " 没接进首页")
            for part in parts:
                if "/" + part == bundle:
                    continue   # /app-core.js 合包的最后一片就是同名的核心脚本本身
                self.assertNotIn("/" + part, scripts, part + " 仍被首页单独请求")

    def test_外部脚本都不阻塞_html_解析(self):
        tags = re.findall(r'<script\b[^>]*\bsrc="[^"]+"[^>]*>', INDEX)
        self.assertTrue(tags)
        for tag in tags:
            self.assertRegex(tag, r'\bdefer\b', tag + " 没有 defer")


class BundleSemantics(unittest.TestCase):
    def test_启动侧栏的调用与实现同包(self):
        """手机隧道漏载非关键面板包时，工作流与会话侧栏仍必须能启动。"""
        rail = bundle_data(FRONTEND, "/app-rail.js")[1].decode("utf-8")
        panel = bundle_data(FRONTEND, "/app-panel.js")[1].decode("utf-8")
        self.assertIn("function renderRail()", rail)
        self.assertIn("function renderSessionPanel()", rail)
        self.assertNotIn("function renderSessionPanel()", panel)

    def test_拼接顺序就是声明顺序(self):
        for virtual, parts in ASSET_BUNDLES.items():
            _key, got, _mtime = bundle_data(FRONTEND, virtual)
            chunks = []
            for part in parts:
                with open(os.path.join(FRONTEND, part), "rb") as f:
                    chunks.append(f.read())
            want = b"\n".join(chunks)
            self.assertEqual(got, want, virtual + " 拼接内容或顺序漂了")

    @unittest.skipUnless(shutil.which("node"), "需要 node 做合包语法检查")
    def test_每个合包都是合法脚本(self):
        for virtual in ASSET_BUNDLES:
            body = bundle_data(FRONTEND, virtual)[1]
            got = subprocess.run(["node", "--check"], input=body, capture_output=True)
            self.assertEqual(got.returncode, 0, virtual + ":" + got.stderr.decode("utf-8", "replace"))


class StartupGate(unittest.TestCase):
    def test_启动前检查接受服务端虚拟合包(self):
        """真实启动入口会先跑这条检查；合包只在 HTTP 层存在，不能再被当成缺文件。"""
        got = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "check_frontend.py")],
            capture_output=True,
            text=True,
         encoding='utf-8')
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 node 验证启动缓存")
    def test_启动前检查按内容指纹复用成功结果(self):
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ)
            env["DESKTRON_FRONTEND_CHECK_CACHE"] = os.path.join(td, "check.json")
            cmd = [sys.executable, os.path.join(ROOT, "scripts", "check_frontend.py")]
            first = subprocess.run(cmd, capture_output=True, text=True, env=env, encoding='utf-8')
            second = subprocess.run(cmd, capture_output=True, text=True, env=env, encoding='utf-8')
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("静态检查通过", first.stdout)
        self.assertIn("缓存命中", second.stdout)


if __name__ == "__main__":
    unittest.main()
