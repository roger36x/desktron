// 仅本地 HTML、非激活窗口、定向 NSEvent；生产输入方法由测试从 main.swift 注入。
import Cocoa
import WebKit

/* PRODUCTION_BROWSER_VIEW */

final class InputProbe: NSView {
    var received: (() -> Void)?
    override var acceptsFirstResponder: Bool { true }
    override func keyDown(with event: NSEvent) { received?() }
}

@available(macOS 11.0, *)
final class BackgroundFixture: NSObject, WKNavigationDelegate {
    let window: NSWindow? = DeskWindow(contentRect: NSRect(x: -10000, y: 0, width: 800, height: 650),
        styleMask: [.borderless], backing: .buffered, defer: false)
    let view = BrowserWebView(frame: NSRect(x: 0, y: 0, width: 800, height: 600))
    let userField = NSTextField(string: "user text unchanged")
    let appControlWorld = WKContentWorld.world(name: "background-fixture")
    var appControlEpochs = ["test": 1]
    var appControlLastTouch: [String: Date] = [:]
    var appControlPaused = Set<String>()
    var appControlNativeInputUntil: [String: Date] = [:]
    var done: ((Bool, Any?, String) -> Void)?
    var index = 0
    var rows: [[String: Any]] = []
    var humanEvents = 0
    let modes = ["nonkey", "hidden_view", "ordered_out"]
    func appControlLease(_ id: String) -> String? { "fixture-lease" }
    func finishAppControl(_ id: String, ok: Bool, output: Any? = nil, error: String = "", observationVersion: String = "") {
        done?(ok, output, error)
    }

    /* PRODUCTION_INPUT_METHODS */

    override init() {
        super.init()
        let root = NSView(frame: NSRect(x: 0, y: 0, width: 800, height: 650))
        root.addSubview(view)
        userField.frame = NSRect(x: 0, y: 610, width: 300, height: 30)
        root.addSubview(userField)
        window?.contentView = root
        view.navigationDelegate = self
        view.appControlPreservesUserFocus = { [weak self] in
            !(self?.appControlPaused.contains("test") ?? true)
        }
        view.appControlHumanInput = { [weak self] in
            self?.humanEvents += 1
            self?.appControlPaused.insert("test")
            self?.appControlLastTouch["test"] = Date()
        }
    }
    func next() {
        guard index < modes.count else {
            let data = try! JSONSerialization.data(withJSONObject: rows)
            print(String(data: data, encoding: .utf8)!)
            exit(0)
        }
        appControlPaused.removeAll(); humanEvents = 0
        view.isHidden = modes[index] == "hidden_view"
        if modes[index] == "ordered_out" { window?.orderOut(nil) } else { window?.orderBack(nil) }
        view.loadHTMLString("""
        <html><body><button id=go style='position:absolute;left:30px;top:35px;width:120px;height:40px'>Go</button>
        <input id=entry style='position:absolute;left:30px;top:110px;width:180px;height:40px'>
        <div id=source style='position:absolute;left:30px;top:200px;width:120px;height:40px'>Drag</div>
        <div id=target style='position:absolute;left:230px;top:200px;width:120px;height:40px'>Target</div>
        <script>globalThis.results={clicks:0,trusted:false,keys:0,drag_start:false,drag_end:false};
        go.addEventListener('click',e=>{results.clicks++;results.trusted=e.isTrusted});
        source.addEventListener('mousedown',e=>{results.drag_start=e.isTrusted});
        target.addEventListener('mouseup',e=>{results.drag_end=e.isTrusted});
        entry.addEventListener('keydown',e=>{if(e.isTrusted)results.keys++});</script></body></html>
        """, baseURL: nil)
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        window?.makeFirstResponder(userField)
        let userResponder = window?.firstResponder
        var receipt: [String: Any] = [:]
        done = { ok, output, error in
            receipt["pointer_ok"] = ok; receipt["pointer_error"] = error
            receipt["pointer_output"] = output
            self.done = { keyOK, _, keyError in
                receipt["key_ok"] = keyOK; receipt["key_error"] = keyError
                self.done = { dragOK, _, dragError in
                    receipt["drag_ok"] = dragOK; receipt["drag_error"] = dragError
                    self.view.evaluateJavaScript("({...results,value:entry.value,visibility:document.visibilityState})") { result, error in
                        var row = result as? [String: Any] ?? [:]
                        row["mode"] = self.modes[self.index]; row["receipt"] = receipt
                        row["app_active"] = NSApp.isActive
                        row["window_key"] = self.window?.isKeyWindow ?? false
                        row["user_field"] = self.userField.stringValue
                        row["user_focus_preserved"] = self.window?.firstResponder === userResponder
                        row["agent_triggered_handoff"] = self.humanEvents != 0
                        row["flipped"] = self.view.isFlipped
                        row["error"] = error?.localizedDescription ?? ""
                        self.verifyHumanRouting { verified in
                            row["human_input_routed_first"] = verified
                            self.rows.append(row); self.index += 1; self.next()
                        }
                    }
                }
                self.runNativeDrag("drag", "test", self.view,
                    input: ["source": ["selector": "#source"], "target": ["selector": "#target"]])
            }
            guard let keys = self.nativeKeyEvents(name: "type", input: ["text": "z"]) else { self.fail("no keys"); return }
            self.runNativeInput("key", "test", self.view, input: ["selector": "#entry", "text": "z"], keys: keys)
        }
        runNativePointer("click", "test", view, name: "click", input: ["selector": "#go", "timeout_ms": 1000])
    }
    func verifyHumanRouting(_ complete: @escaping (Bool) -> Void) {
        // 仅将本地事件送进测试窗口：覆盖系统分发入口，不向桌面或其他应用投递。
        let probe = InputProbe(frame: .zero)
        view.addSubview(probe)
        appControlPaused.insert("test")
        window?.makeFirstResponder(probe)
        appControlPaused.remove("test")
        var routedFirst = false
        probe.received = { routedFirst = self.appControlPaused.contains("test") && self.humanEvents == 1 }
        let keys = nativeKeyEvents(name: "type", input: ["text": "x"])!
        window?.sendEvent(keys[0].0)
        probe.removeFromSuperview()
        done = { ok, output, error in
            let blocked = !ok && error == "human_intervention_reinspect_required"
                && (output as? [String: Any])?["action_executed"] as? Bool == false
            complete(routedFirst && blocked)
        }
        runNativeInput("blocked-key", "test", view, input: ["selector": "#entry"], keys: keys)
    }
    func fail(_ message: String) {
        FileHandle.standardError.write(Data((message + "\n").utf8)); exit(1)
    }
}

if #available(macOS 11.0, *) {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let fixture = BackgroundFixture()
    DispatchQueue.main.async { fixture.next() }
    DispatchQueue.main.asyncAfter(deadline: .now() + 40) { fixture.fail("fixture timeout") }
    app.run()
}
