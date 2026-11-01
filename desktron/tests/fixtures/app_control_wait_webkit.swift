// 本地 WKWebView fixture，不打开真实站点，也不注入鼠标键盘事件。
import Cocoa
import WebKit

@available(macOS 11.0, *)
final class WaitFixture: NSObject, WKNavigationDelegate {
    let view = WKWebView(frame: NSRect(x: 0, y: 0, width: 800, height: 600))
    let window = NSWindow(contentRect: NSRect(x: -10000, y: 0, width: 800, height: 600),
                          styleMask: [.borderless], backing: .buffered, defer: false)
    let script: String
    var index = 0
    var results: [[String: Any]] = []
    let cases: [(String, String, [String: Any], String)] = [
        ("delayed_modal", "setTimeout(()=>document.body.insertAdjacentHTML('beforeend','<div role=dialog>Confirm</div>'),650)",
         ["selector": "[role=dialog]", "state": "visible", "timeout_ms": 1800], "ok"),
        ("button_unlock", "setTimeout(()=>document.querySelector('button').disabled=false,650)",
         ["selector": "button", "state": "enabled", "timeout_ms": 1800], "ok"),
        ("failure_early_exit", "setTimeout(()=>document.body.insertAdjacentHTML('beforeend','<div id=failed>Failed</div>'),650)",
         ["selector": "#done", "abort_if": "#failed", "timeout_ms": 1800], "wait_aborted"),
        ("changed_business_url", "setTimeout(()=>location.hash='different-release',650)",
         ["selector": "#done", "guard_url": "about:blank", "timeout_ms": 1800], "resource_changed"),
        ("condition_timeout", "", ["selector": "#missing", "timeout_ms": 300], "wait_timeout"),
        ("detached_condition", "setTimeout(()=>document.querySelector('button').remove(),650)",
         ["selector": "button", "state": "detached", "timeout_ms": 1800], "ok")
    ]

    init(script: String) {
        self.script = script
        super.init()
        view.navigationDelegate = self
        window.contentView = view
        window.orderBack(nil) // 提供真实布局，不抢焦点，不覆盖用户窗口。
    }

    func next() {
        guard index < cases.count else {
            let data = try! JSONSerialization.data(withJSONObject: results, options: [.sortedKeys])
            print(String(data: data, encoding: .utf8)!)
            exit(0)
        }
        view.loadHTMLString("<html><body><button disabled>Continue</button></body></html>", baseURL: nil)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        let item = cases[index]
        // 计时从宿主进入脚本时开始，避免页面加载耗时吞掉异步测试窗口。
        view.evaluateJavaScript(item.1) { _, setupError in
            if let error = setupError { self.fail(error.localizedDescription); return }
            self.view.callAsyncJavaScript(self.script,
                arguments: ["command": ["kind": "act", "name": "wait", "input": item.2], "epoch": self.index],
                in: nil, in: WKContentWorld.world(name: "wait-fixture")) { result in
                switch result {
                case .failure(let error): self.fail(error.localizedDescription)
                case .success(let raw):
                    guard let result = raw as? [String: Any] else { self.fail("invalid result"); return }
                    let actual = (result["ok"] as? Bool) == true ? "ok" : (result["error"] as? String ?? "missing error")
                    guard actual == item.3 else { self.fail("\(item.0): expected \(item.3), got \(actual)"); return }
                    self.results.append(["case": item.0, "result": actual])
                    self.index += 1
                    self.next()
                }
            }
        }
    }

    func fail(_ message: String) {
        FileHandle.standardError.write(Data((message + "\n").utf8))
        exit(1)
    }
}

if #available(macOS 11.0, *) {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let source = try String(contentsOfFile: CommandLine.arguments[1], encoding: .utf8)
    let body = source.components(separatedBy: "private func appControlDOMScript() -> String {")[1]
        .components(separatedBy: "return #\"\"\"")[1].components(separatedBy: "\"\"\"#")[0]
    let fixture = WaitFixture(script: body)
    DispatchQueue.main.async { fixture.next() }
    DispatchQueue.main.asyncAfter(deadline: .now() + 30) { fixture.fail("fixture timeout") }
    app.run()
}
