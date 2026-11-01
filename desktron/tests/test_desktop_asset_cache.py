"""桌面壳只刷新无缓存首页，不应抹掉带内容指纹的静态资源。"""
import os
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SWIFT = open(os.path.join(ROOT, "desktop", "native", "main.swift"), encoding="utf-8").read()
SUPERVISOR = open(os.path.join(ROOT, "desktop", "native", "backend_supervisor.swift"), encoding="utf-8").read()


class FingerprintedCache(unittest.TestCase):
    def test_启动不清空_webview_缓存(self):
        self.assertNotIn("WKWebsiteDataTypeDiskCache", SWIFT)
        self.assertNotIn("URLCache.shared.removeAllCachedResponses()", SWIFT)

    def test_主页请求不把子资源一并强制冷取(self):
        self.assertNotIn("reloadIgnoringLocalAndRemoteCacheData", SWIFT)

    def test_仍从主页地址加载(self):
        self.assertIn("self.web.load(URLRequest(url: URL(string: URLSTR)!))", SWIFT)

    def test_每次启动截断旧日志并写启动时间(self):
        self.assertIn("log.truncateFile(atOffset: 0)", SWIFT)
        self.assertIn("[startup] native launch", SWIFT)

    def test_后端就绪由精确身份监督而非端口沉降猜测(self):
        self.assertIn("BackendSupervisor(", SWIFT)
        self.assertIn("probe.hostInstanceID == hostInstanceID", SUPERVISOR)
        self.assertIn("probe.launchGeneration == expectedGeneration", SUPERVISOR)
        self.assertIn("probe.pid == process.processIdentifier", SUPERVISOR)
        self.assertNotIn("pollThenLoad()", SWIFT)

    def test_原生壳先拉后端再异步保活(self):
        self.assertIn('env["DESKTRON_NATIVE_LAUNCH"] = "1"', SWIFT)
        self.assertLess(SWIFT.index("try p.run()"), SWIFT.index('c.arguments = ["-is", "-w"'))
        self.assertIn("caffeine?.terminate()", SWIFT)

    def test_记录从原生导航到前端可交互的完整启动链(self):
        self.assertIn('add(self, name: "startup")', SWIFT)
        self.assertIn('startupLog("main navigation committed")', SWIFT)
        self.assertIn('startupLog("main navigation finished")', SWIFT)
        self.assertIn('message.name == "startup"', SWIFT)

    def test_启动器记录后端之前的各阶段(self):
        with open(os.path.join(ROOT, "desktron", "bin", "desktron"), encoding="utf-8") as f:
            launcher = f.read()
        for phase in ("begin", "environment ready", "frontend check ready", "workspace ready", "exec ui server"):
            self.assertIn(f'startup_phase "{phase}"', launcher)
        self.assertIn('DESKTRON_NATIVE_LAUNCH:-}', launcher)
        self.assertIn('DESKTRON_NATIVE_HOST_PID', launcher)
        self.assertIn('_native_host_alive', launcher)
        self.assertGreaterEqual(launcher.count('_native_host_alive || exit 75'), 2)
        helper = launcher[launcher.index('  _host-update-apply)'):launcher.index('  ui)')]
        self.assertIn('[[ "$PPID" -eq "$_host_pid" ]]', helper)
        self.assertLess(helper.index('[[ "$PPID" -eq "$_host_pid" ]]'), helper.index('exec python3'))


if __name__ == "__main__":
    unittest.main()
