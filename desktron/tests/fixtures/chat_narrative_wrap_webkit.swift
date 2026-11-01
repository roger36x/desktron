// 隔离 WebKit，只加载会话长文本布局夹具，不访问站点、不控制 Desktron。
import Cocoa
import WebKit

final class Fixture: NSObject, WKScriptMessageHandler, WKNavigationDelegate {
    var view: WKWebView!
    var window: NSWindow!

    override init() {
        let config = WKWebViewConfiguration()
        super.init()
        config.userContentController.add(self, name: "result")
        view = WKWebView(frame: NSRect(x: 0, y: 0, width: 1200, height: 800), configuration: config)
        window = NSWindow(contentRect: NSRect(x: 40, y: 40, width: 1200, height: 800),
                          styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = view
        view.navigationDelegate = self
        window.title = "Desktron 会话长文本换行验收"
        window.orderFrontRegardless()
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        let data = try! JSONSerialization.data(withJSONObject: message.body, options: [.sortedKeys])
        print(String(data: data, encoding: .utf8)!)
        exit(0)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
            self.view.evaluateJavaScript("void window.collectNarrativeLayout(); null") { _, error in
                if let error = error {
                    FileHandle.standardError.write(Data("WebKit evaluate failed: \(error)\n".utf8))
                    exit(1)
                }
            }
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let fixture = Fixture()
let html = try String(contentsOfFile: CommandLine.arguments[1], encoding: .utf8)
fixture.view.loadHTMLString(html, baseURL: nil)
DispatchQueue.main.asyncAfter(deadline: .now() + 45) {
    FileHandle.standardError.write(Data("WebKit fixture timeout\n".utf8))
    exit(1)
}
app.run()
