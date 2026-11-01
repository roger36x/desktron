"""主框架不能被链接带走。

2026-08-10 实见:任务备忘里一个相对路径链接(markdown 渲染成 <a href="desktron/docs/x.md">),
点一下就相对当前页导航到后端没有的 URL,拿回一张 http.server 的裸 Error response。那张页面
没有任何返回入口,整个应用被顶掉,只能退出重开。

三道都钉上:前端捕获期拦所有锚点、原生层限制主 webview 的顶层导航、后端 404 给一张能走回去的页。
"""
import os
import re
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _read(*parts):
    if parts and parts[-1] in ("app-chat.js", "app-panel.js", "app-rail.js"):      # 2026-08-27 分拆:拼接整体等价
        return _srcmap.frontend_src(parts[-1])
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


CORE = _read("frontend", "app-core.js")
SWIFT = _read("desktop", "native", "main.swift")
UI_SERVER = _read("desktron", "lib", "ui_server.py")


class FrontendIntercept(unittest.TestCase):
    def setUp(self):
        m = re.search(r'document\.addEventListener\("click", \(ev\) => \{\n'
                      r'  const a = ev\.target[\s\S]+?\n\}, true\);', CORE)
        self.assertTrue(m, "找不到全局链接路由")
        self.fn = m.group(0)

    def test_页内锚点仍然放行(self):
        self.assertIn('href.startsWith("#")', self.fn)

    def test_相对路径也拦下来(self):
        # 原来只拦 http(s),相对路径直接放过去 —— 那正是把应用顶掉的那条路
        self.assertNotIn('if (!/^https?:\\/\\//.test(href)) return;', self.fn)

    def test_每条出路都先挡掉默认导航(self):
        """页内锚点之外,任何一条 return 之前都必须已经 preventDefault 过。

        原来用「第一个 preventDefault 之前不许出现 https?」来近似这件事。那只是个字面代理:
        自定义协议那一支要排在 stopPropagation 之前(见下条),它自己也 preventDefault,
        不变量并没有破,但代理断言会误报。这里直接按不变量断。
        """
        body = self.fn[self.fn.index('href.startsWith("#")'):]
        body = body[body.index("\n"):]                     # 跳过锚点放行那一行
        pos = body.find("return")
        while pos >= 0:
            self.assertIn("ev.preventDefault()", body[:pos],
                          "这条 return 之前没挡默认导航:\n" + body[max(0, pos - 200):pos + 20])
            pos = body.find("return", pos + 1)

    def test_自定义协议只挡导航_不打断传播(self):
        """javascript:void(0) 这类 href 是占位,动作挂在元素自己的 onclick 上。

        捕获期 stopPropagation 会让事件到不了目标,那些链接就成了死链——2026-08-16 实见:
        会话里一屏文件引用(hsFileLink 建的 <a href="javascript:void(0)">)全部点不开。
        """
        i = self.fn.index("/^[a-z][\\w+.-]*:/i.test(href)")
        j = self.fn.index("ev.stopPropagation()")
        self.assertLess(i, j, "自定义协议那一支要排在 stopPropagation 之前")
        branch = self.fn[i:j]
        self.assertIn("ev.preventDefault()", branch)
        self.assertNotIn("ev.stopPropagation()", branch)

    def test_外链仍进标签页(self):
        self.assertIn("openPage(href", self.fn)


class NativeGuard(unittest.TestCase):
    def test_主_webview_的顶层导航被限制(self):
        m = re.search(r"func webView\(_ wv: WKWebView, decidePolicyFor[\s\S]+?\n    \}", SWIFT)
        self.assertTrue(m)
        body = m.group(0)
        self.assertIn("wv === web", body)                  # 只管主 webview,内置浏览器面板照旧
        self.assertIn("isMainFrame", body)                 # iframe 内的导航不拦
        self.assertIn("decisionHandler(.cancel)", body)
        self.assertIn("NSWorkspace.shared.open", body)     # 外链交给系统浏览器,不是一律吞掉


class BackendFallback(unittest.TestCase):
    def test_404_页带返回入口(self):
        self.assertIn("error_message_format", UI_SERVER)
        m = re.search(r"error_message_format = \(([\s\S]+?)\n    \)", UI_SERVER)
        self.assertTrue(m)
        self.assertIn('href="/"', m.group(1))
        self.assertIn("error_content_type", UI_SERVER)


if __name__ == "__main__":
    unittest.main()
