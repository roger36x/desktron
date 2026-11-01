// 隔离 WebKit，只加载测试正文，不访问站点、不控制 Desk；rAF 必须在可见窗口验收。
import Cocoa
import WebKit

final class Fixture: NSObject, WKScriptMessageHandler, WKNavigationDelegate {
    let view: WKWebView
    let window: NSWindow
    override init() {
        let config = WKWebViewConfiguration()
        view = WKWebView(frame: NSRect(x: 0, y: 0, width: 900, height: 700), configuration: config)
        window = NSWindow(contentRect: NSRect(x: 40, y: 40, width: 900, height: 400),
                          styleMask: [.titled], backing: .buffered, defer: false)
        super.init()
        config.userContentController.add(self, name: "result")
        view.navigationDelegate = self
        window.contentView = view
        window.title = "Desktron 流式渲染验收（仅测试文本）"
        window.orderFrontRegardless()
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
            self.view.evaluateJavaScript("JSON.stringify({ready:document.readyState,stage:window.fixtureStage,frames:window.fixtureFrames,error:window.fixtureError})") { value, error in
                FileHandle.standardError.write(Data("fixture diagnostic: \(value ?? "nil") \(String(describing:error))\n".utf8))
            }
        }
    }
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        let data = try! JSONSerialization.data(withJSONObject: message.body, options: [.sortedKeys])
        print(String(data: data, encoding: .utf8)!)
        exit(0)
    }
}
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let fixture = Fixture()
let html = try String(contentsOfFile: CommandLine.arguments[1], encoding: .utf8)
fixture.view.loadHTMLString(html, baseURL: nil)
DispatchQueue.main.asyncAfter(deadline: .now() + 45) {
    FileHandle.standardError.write(Data("WebKit fixture timeout\n".utf8)); exit(1)
}
app.run()
