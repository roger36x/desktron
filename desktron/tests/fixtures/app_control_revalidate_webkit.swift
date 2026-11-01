// 只加载本地 HTML；调用真实隔离世界适配器，不投递鼠标、不访问真实平台。
import Cocoa
import WebKit

@available(macOS 11.0, *)
final class RevalidateFixture: NSObject, WKNavigationDelegate {
    let view = WKWebView(frame: NSRect(x: 0, y: 0, width: 800, height: 600))
    let window = NSWindow(contentRect: NSRect(x: -10000, y: 0, width: 800, height: 600),
                          styleMask: [.borderless], backing: .buffered, defer: false)
    let world = WKContentWorld.world(name: "approval-fixture")
    let script: String
    var index = 0
    var results: [[String: Any]] = []
    // 文本、URL、参数与目标身份任一变化必须重新审批。样式导致的版本漂移可安全续办。
    let cases: [(String, String, String)] = [
        ("style_refresh", "document.body.style.color='rgb(10,10,10)'", "ok"),
        ("business_text_changed", "document.querySelector('#context').textContent='Different release'", "approval_target_changed"),
        ("form_value_changed", "document.querySelector('input').value='changed'", "approval_target_changed"),
        ("url_changed", "location.hash='other-business'", "approval_target_changed"),
        ("target_replaced", "const b=document.querySelector('#go');b.replaceWith(b.cloneNode(true))", "approval_target_changed"),
        ("hidden_duplicate", "", "ok"),
        ("visible_duplicate", "document.querySelector('#hidden').style.display='block'", "locator_ambiguous"),
        ("missing_guard", "globalThis.__clearGuardForFixture=true", "approval_revalidation_unavailable"),
        ("unnamed_pointer_element", "", "ok"),
        ("nested_button_text", "", "ok")
    ]

    init(script: String) {
        self.script = script
        super.init()
        view.navigationDelegate = self
        window.contentView = view
        window.orderBack(nil)
    }

    func next() {
        guard index < cases.count else {
            let data = try! JSONSerialization.data(withJSONObject: results)
            print(String(data: data, encoding: .utf8)!)
            exit(0)
        }
        view.loadHTMLString("""
        <html><body><p id=context>Release one</p><input value=original>
        <button id=go><span>Enter</span></button><button id=hidden style='display:none'>Enter</button>
        <span id=pointer style='cursor:pointer'>Custom entry</span></body></html>
        """, baseURL: nil)
    }

    func call(_ command: [String: Any], _ done: @escaping ([String: Any]) -> Void) {
        view.callAsyncJavaScript(script, arguments: ["command": command, "epoch": index],
                                 in: nil, in: world) { result in
            switch result {
            case .failure(let error): self.fail(error.localizedDescription)
            case .success(let value):
                guard let row = value as? [String: Any] else { self.fail("invalid result"); return }
                done(row)
            }
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        call(["kind": "inspect", "name": "document", "input": [:]]) { inspected in
            guard let output = inspected["output"] as? [String: Any],
                  var proof = output["approval_guard"] as? [String: Any] else {
                self.fail("missing snapshot guard"); return
            }
            let item = self.cases[self.index]
            if item.0 == "missing_guard" { proof["token"] = "not-a-host-token" }
            if item.0 == "unnamed_pointer_element" {
                let nodes = output["nodes"] as? [[String: Any]] ?? []
                guard nodes.contains(where: { ($0["name"] as? String) == "Custom entry" }) else {
                    self.fail("pointer element missing from document"); return
                }
            }
            self.view.evaluateJavaScript(item.1) { _, error in
                if let error = error { self.fail(error.localizedDescription); return }
                let locator: [String: Any] = item.0 == "nested_button_text" ? ["text": "Enter", "exact": true]
                    : item.0 == "unnamed_pointer_element"
                    ? ["text": "Custom entry", "exact": true]
                    : ["role": "button", "name": "Enter", "exact": true]
                self.call(["kind": "act", "name": "prepare_pointer", "input": [
                    "pointer_action": "click", "locator": locator, "_approval_guard": proof,
                    "timeout_ms": 500]]) { row in
                    let actual = (row["ok"] as? Bool) == true ? "ok" : (row["error"] as? String ?? "unknown")
                    guard actual == item.2 else { self.fail("\(item.0): expected \(item.2), got \(actual)"); return }
                    if actual == "ok" {
                        guard let out = row["output"] as? [String: Any],
                              (out["approval_revalidated"] as? Bool) == true else {
                            self.fail("missing revalidation receipt"); return
                        }
                    }
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
    let script = source.components(separatedBy: "private func appControlDOMScript() -> String {")[1]
        .components(separatedBy: "return #\"\"\"")[1].components(separatedBy: "\"\"\"#")[0]
    let fixture = RevalidateFixture(script: script)
    DispatchQueue.main.async { fixture.next() }
    DispatchQueue.main.asyncAfter(deadline: .now() + 30) { fixture.fail("fixture timeout") }
    app.run()
}
