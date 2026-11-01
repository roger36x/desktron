// 只加载本地 HTML，验证自定义控件读回、后置条件与遮挡安全点。
import Cocoa
import WebKit

@available(macOS 11.0, *)
final class ControlsFixture: NSObject, WKNavigationDelegate {
    let view = WKWebView(frame: NSRect(x: 0, y: 0, width: 800, height: 600))
    let window = NSWindow(contentRect: NSRect(x: -10000, y: 0, width: 800, height: 600),
                          styleMask: [.borderless], backing: .buffered, defer: false)
    let world = WKContentWorld.world(name: "controls-fixture")
    let script: String
    var results: [[String: Any]] = []

    init(script: String) {
        self.script = script
        super.init()
        view.navigationDelegate = self
        window.contentView = view
        window.orderBack(nil)
    }

    func call(_ command: [String: Any], _ done: @escaping ([String: Any]) -> Void) {
        view.callAsyncJavaScript(script, arguments: ["command": command, "epoch": 7],
                                 in: nil, in: world) { result in
            switch result {
            case .failure(let error): self.fail(error.localizedDescription)
            case .success(let value):
                guard let row = value as? [String: Any] else { self.fail("invalid result"); return }
                done(row)
            }
        }
    }

    func record(_ name: String, _ ok: Bool, _ detail: String = "") {
        results.append(["case": name, "result": ok ? "ok" : detail])
        if !ok { fail("\(name): \(detail)") }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        call(["kind": "inspect", "name": "document", "input": ["include_offscreen": true]]) { first in
            let output = first["output"] as? [String: Any] ?? [:]
            let nodes = output["nodes"] as? [[String: Any]] ?? []
            let east = nodes.first { ($0["role"] as? String) == "checkbox"
                && ($0["name"] as? String)?.contains("China-East") == true }
            let states = east?["states"] as? [String: Any] ?? [:]
            let sources = east?["state_sources"] as? [String: Any] ?? [:]
            self.record("associated_checkbox", (states["checked"] as? Bool) == false
                        && sources["checked"] as? String == "associated_control", "state missing")
            let mixed = nodes.first { ($0["name"] as? String)?.contains("China-Mixed") == true }
            let mixedStates = mixed?["states"] as? [String: Any] ?? [:]
            self.record("mixed_disabled", mixedStates["mixed"] as? Bool == true
                        && mixedStates["disabled"] as? Bool == true, "mixed/disabled missing")
            self.record("overlay_stacks", !(output["menu_stack"] as? [[String: Any]] ?? []).isEmpty
                        && !(output["popover_stack"] as? [[String: Any]] ?? []).isEmpty, "stack missing")
            let handoff = output["handoff"] as? [String: Any] ?? [:]
            self.record("handoff_state", handoff["current_step"] as? String == "Step 2"
                        && handoff["unsaved"] as? Bool == true
                        && handoff["submission_status"] as? String == "unknown", "handoff state missing")
            let select = nodes.first { ($0["name"] as? String) == "Cluster" }
            self.record("selected_values", (select?["selected_values"] as? [String]) == ["sre-2"],
                        "selected value missing")

            let locator: [String: Any] = ["role": "checkbox", "name": "China-East", "exact": true]
            self.call(["kind": "act", "name": "prepare_pointer", "input": [
                "pointer_action": "check", "locator": locator, "timeout_ms": 500]]) { prepared in
                let before = prepared["output"] as? [String: Any] ?? [:]
                guard let x = before["x"] as? NSNumber, let y = before["y"] as? NSNumber else {
                    self.fail("checkbox point missing"); return
                }
                self.view.evaluateJavaScript("document.elementFromPoint(\(x),\(y)).click()") { _, error in
                    if let error = error { self.fail(error.localizedDescription); return }
                    self.call(["kind": "act", "name": "settle", "input": [
                        "locator": locator, "desired_checked": true,
                        "before_rev": before["rev"] ?? 0, "before_url": before["url"] ?? "",
                        "input_path": "fixture", "action": "check"]]) { settled in
                        let out = settled["output"] as? [String: Any] ?? [:]
                        self.record("check_readback", (settled["ok"] as? Bool) == true
                                    && out["effect_verified"] as? Bool == true
                                    && out["checked"] as? Bool == true, "check not verified")
                        self.runOcclusionCases()
                    }
                }
            }
        }
    }

    func runOcclusionCases() {
        call(["kind": "act", "name": "prepare_pointer", "input": [
            "pointer_action": "click", "selector": "#partly-covered", "timeout_ms": 500]]) { safe in
            let out = safe["output"] as? [String: Any] ?? [:]
            let diag = out["diagnostic"] as? [String: Any] ?? [:]
            self.record("visible_internal_point", (safe["ok"] as? Bool) == true
                        && diag["point_strategy"] as? String == "visible_internal", "safe point not found")
            self.call(["kind": "act", "name": "prepare_pointer", "input": [
                "pointer_action": "click", "selector": "#fully-covered", "timeout_ms": 250]]) { blocked in
                let diagnostic = (blocked["output"] as? [String: Any])?["diagnostic"] as? [String: Any] ?? [:]
                self.record("occluder_diagnostic", blocked["error"] as? String == "element_occluded"
                            && diagnostic["occluder"] != nil
                            && !(diagnostic["candidate_points"] as? [[String: Any]] ?? []).isEmpty,
                            "occlusion evidence missing")
                self.call(["kind": "act", "name": "wait", "input": [
                    "locator": ["role": "checkbox", "name": "China-East", "exact": true],
                    "state": "checked", "timeout_ms": 500]]) { waited in
                    self.record("checked_wait", (waited["ok"] as? Bool) == true, "checked wait failed")
                    self.call(["kind": "act", "name": "wait", "input": [
                        "selector": "#cluster", "values": ["sre-2"], "timeout_ms": 500]]) { selected in
                        self.record("selected_values_wait", (selected["ok"] as? Bool) == true,
                                    "selected values wait failed")
                        let data = try! JSONSerialization.data(withJSONObject: self.results)
                        print(String(data: data, encoding: .utf8)!)
                        exit(0)
                    }
                }
            }
        }
    }

    func fail(_ message: String) {
        FileHandle.standardError.write(Data((message + "\n").utf8)); exit(1)
    }
}

if #available(macOS 11.0, *) {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let source = try String(contentsOfFile: CommandLine.arguments[1], encoding: .utf8)
    let script = source.components(separatedBy: "private func appControlDOMScript() -> String {")[1]
        .components(separatedBy: "return #\"\"\"")[1].components(separatedBy: "\"\"\"#")[0]
    let fixture = ControlsFixture(script: script)
    fixture.view.loadHTMLString("""
    <html><head><style>
      body { margin: 0; font: 16px sans-serif; }
      label { display:block; width:180px; height:40px; cursor:pointer; }
      #partly-covered,#fully-covered { position:absolute; left:100px; width:400px; height:60px; }
      #partly-covered { top:180px; } #fully-covered { top:300px; }
      #center-cover { position:absolute; z-index:10; left:220px; top:180px; width:160px; height:60px; }
      #full-cover { position:absolute; z-index:10; left:100px; top:300px; width:400px; height:60px; }
      .arco-trigger-popup { position:absolute; top:420px; width:160px; height:40px; }
    </style></head><body>
      <label class="arco-checkbox"><input type="checkbox" hidden><span>China-East</span></label>
      <label class="arco-checkbox-indeterminate arco-checkbox-disabled"><input id="mixed" type="checkbox" checked disabled hidden><span>China-Mixed</span></label>
      <select id="cluster" aria-label="Cluster"><option value="sre-1">SRE 1</option><option value="sre-2" selected>SRE 2</option></select>
      <button id="partly-covered">Partly covered</button><div id="center-cover">overlay</div>
      <button id="fully-covered">Fully covered</button><div id="full-cover">overlay</div>
      <div role="listbox" aria-label="Cluster menu"><div role="option" aria-selected="true">SRE 2</div></div>
      <div class="arco-trigger-popup">Popup</div>
      <div aria-current="step">Step 2</div><input id="draft" value="before">
      <script>document.querySelector('#mixed').indeterminate = true;document.querySelector('#draft').value='after'</script>
    </body></html>
    """, baseURL: nil)
    DispatchQueue.main.asyncAfter(deadline: .now() + 30) { fixture.fail("fixture timeout") }
    app.run()
}
