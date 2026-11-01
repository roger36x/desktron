"""内置浏览器要能走完飞书条件访问的设备授权。

2026-09-24 实见:内置浏览器打开飞书文档被拦到 security.feishu.cn/conditionalaccess/deny,
blockBy=DEVICE_MDM_STATUS,点「前往飞书客户端授权」没有任何反应。原因在原生壳:

1. 拦截页把 https://applink.feishu.cn/... 塞进隐藏 iframe,指望系统按通用链接拉起客户端;
   WKWebView 不支持通用链接,只会把它当网页加载。
2. lark:// 等客户端协议原先一律 .allow 给 WebKit,导航静默失败。
3. 策略带 needCertVerify,Safari/Chrome 自动从钥匙串出示可信设备证书,WKWebView 要宿主
   实现客户端证书质询,原生壳没有实现。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
with open(os.path.join(ROOT, "desktop", "native", "main.swift"), encoding="utf-8") as f:
    SWIFT = f.read()


def _func(signature):
    m = re.search(re.escape(signature) + r"[\s\S]+?\n    \}\n", SWIFT)
    assert m, signature
    return m.group(0)


class ExternalSchemeHandoff(unittest.TestCase):
    def test_面板导航先交外部协议再走主页面规则(self):
        body = _func("func webView(_ wv: WKWebView, decidePolicyFor action: WKNavigationAction,")
        i = body.index("handoffExternalScheme(u, from: wv)")
        self.assertLess(i, body.index("guard wv === web"), "iframe 与面板导航都要先过这一关")
        self.assertIn("wv !== web", body[:i + 60], "主页面不走这条")

    def test_window_open_同样不把客户端协议交给_WebKit(self):
        body = _func("func webView(_ wv: WKWebView, createWebViewWith config:")
        self.assertIn("!handoffExternalScheme(u, from: wv)", body)

    def test_飞书协议直接交系统_隔离标签不拉起本机应用(self):
        body = _func("func handoffExternalScheme(")
        self.assertIn("AppDelegate.feishuClientURL(original) ?? original", body)
        self.assertLess(body.index("appControlIsolated.contains(id)"),
                        body.index("NSWorkspace.shared.open(u)"))
        self.assertIn("NSAlert()", body, "未知协议先问人")
        for scheme in ('"lark"', '"feishu"'):
            self.assertIn(scheme, SWIFT[SWIFT.index("static let trustedAppSchemes"):][:200])

    def test_网页协议仍由_WebKit_处理(self):
        m = re.search(r"static let webSchemes: Set<String> = \[([^\]]+)\]", SWIFT)
        self.assertTrue(m)
        for scheme in ("http", "https", "about", "blob", "data"):
            self.assertIn(f'"{scheme}"', m.group(1))


class ClientCertificate(unittest.TestCase):
    def test_面板响应客户端证书质询(self):
        body = _func("func webView(_ wv: WKWebView, didReceive challenge: URLAuthenticationChallenge,")
        self.assertIn("NSURLAuthenticationMethodClientCertificate", body)
        self.assertIn("wv !== web", body)
        self.assertIn("!appControlIsolated.contains(id)", body, "Agent 隔离标签不出示设备证书")
        self.assertIn("distinguishedNames", body)
        self.assertIn(".useCredential", body)
        self.assertIn(".performDefaultHandling", body)

    def test_只按服务器点名的签发者挑证书(self):
        body = _func("static func clientIdentity(issuers: [Data]) -> SecIdentity? {")
        self.assertIn("if issuers.isEmpty { return nil }", body)
        self.assertIn("kSecMatchIssuers", body)
        self.assertIn("kSecMatchValidOnDate", body)


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swift"), "需要 macOS swift")
class FeishuApplinkRewrite(unittest.TestCase):
    """把 main.swift 里的真实实现抽出来跑,不在测试里重抄一份逻辑。"""

    def test_applink_改写为_lark_协议且保留路径与查询(self):
        hosts = re.search(r"static let feishuApplinkHosts: Set<String> = \[[\s\S]+?\]", SWIFT).group(0)
        fn = re.search(r"static func feishuClientURL\(_ u: URL\) -> URL\? \{[\s\S]+?\n    \}", SWIFT).group(0)
        src = "import Foundation\nenum AppDelegate {\n" + hosts + "\n" + fn + "\n}\n" + (
            "for s in CommandLine.arguments.dropFirst() {\n"
            "  print(AppDelegate.feishuClientURL(URL(string: s)!)?.absoluteString ?? \"nil\")\n}\n")
        cases = {
            "https://applink.feishu.cn/client/web_app/open?appId=cli_x&path=%2Fa%3Fb%3D1":
                "lark://applink.feishu.cn/client/web_app/open?appId=cli_x&path=%2Fa%3Fb%3D1",
            "https://APPLINK.feishu.cn/client/op/open": "lark://APPLINK.feishu.cn/client/op/open",
            "https://example.feishu.cn/drive/home/": "nil",
            "http://applink.feishu.cn/x": "nil",
        }
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "applink.swift")
            with open(path, "w", encoding="utf-8") as f:
                f.write(src)
            out = subprocess.run(["swift", path, *cases], capture_output=True, text=True, timeout=180)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), list(cases.values()))


if __name__ == "__main__":
    unittest.main()
