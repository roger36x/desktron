"""真实 WebKit 验证生产观察器；只访问测试进程提供的本地页面。"""
import http.server
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import pytest

ROOT = Path(__file__).resolve().parents[2]


class PageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/final')
            self.end_headers()
            return
        body = b'<html><title>Initial</title><a id="next" href="/redirect">next</a></html>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc')
                     and os.environ.get('DESK_WEBKIT_TESTS') == '1', '需启用 macOS WebKit 测试')
@pytest.mark.desktop
class BrowserNavigationWebKit(unittest.TestCase):
    def test_real_navigation_observation(self):
        source = (ROOT / 'desktop/native/main.swift').read_text()
        observe = source.split('    private func observeBrowserState', 1)[1].split(
            '    private func reportBrowser', 1)[0]
        swift = r'''
import Cocoa
import WebKit
final class Fixture: NSObject {
    var browsers: [String: WKWebView] = [:]
    var browserObservations: [String: [NSKeyValueObservation]] = [:]
    var events: [(String, String, Bool, Bool)] = []
    let view = WKWebView()
    func reportBrowser(_ view: WKWebView, phase: String) {
        precondition(phase == "state")
        events.append((view.url?.absoluteString ?? "", view.title ?? "", view.canGoBack, view.canGoForward))
    }
    OBSERVE
    func fail(_ text: String) -> Never {
        FileHandle.standardError.write(Data((text + "\n").utf8)); exit(1)
    }
    func spin(_ ready: () -> Bool) {
        let end = Date().addingTimeInterval(8)
        while !ready() && Date() < end { RunLoop.current.run(until: Date().addingTimeInterval(0.02)) }
        if !ready() { fail("timed out, observed: \(events)") }
    }
    func js(_ script: String) {
        var done = false
        view.evaluateJavaScript(script) { _, error in
            if let error = error { self.fail(error.localizedDescription) }; done = true
        }
        spin { done }
    }
    func expect(_ suffix: String) {
        spin { self.events.last?.0.hasSuffix(suffix) == true }
    }
    func run(_ base: String) {
        browsers["tab"] = view; observeBrowserState(view, id: "tab")
        view.load(URLRequest(url: URL(string: base + "/start")!))
        spin { !self.view.isLoading && self.events.last?.1 == "Initial" }
        expect("/start")
        js("history.pushState({}, '', '/route'); document.title='Route'")
        spin { self.events.last?.1 == "Route" && self.events.last?.2 == true }
        expect("/route")
        js("history.replaceState({}, '', '/replaced')"); expect("/replaced")
        js("location.hash='section'"); expect("/replaced#section")
        view.goBack(); expect("/replaced")
        spin { self.events.last?.3 == true }
        view.goForward(); expect("/replaced#section")
        js("document.querySelector('#next').click()")
        expect("/final"); spin { !self.view.isLoading }
        // 替换视图后，旧视图已排队的回调不能串入同 id 的新标签。
        browsers["tab"] = WKWebView()
        let count = events.count
        js("history.pushState({}, '', '/stale')")
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        precondition(events.count == count)
        browsers["tab"] = view
        browserObservations.removeValue(forKey: "tab")
        js("history.pushState({}, '', '/closed')")
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        precondition(events.count == count)
        print("navigation, history, title, redirect, disposal: ok")
    }
}
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let fixture = Fixture()
fixture.run(CommandLine.arguments[1])
'''.replace('OBSERVE', 'private func observeBrowserState' + observe)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), PageHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix='desk-browser-test-') as tmp:
                path = Path(tmp) / 'main.swift'
                path.write_text(swift)
                binary = str(Path(tmp) / 'fixture')
                built = subprocess.run(['swiftc', str(path), '-o', binary],
                                       capture_output=True, text=True, timeout=90)
                self.assertEqual(built.returncode, 0, built.stderr)
                result = subprocess.run([binary, f'http://127.0.0.1:{server.server_port}'],
                                        capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('disposal: ok', result.stdout)
        finally:
            server.shutdown()
            server.server_close()
