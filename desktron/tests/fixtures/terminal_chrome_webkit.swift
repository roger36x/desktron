// 隔离 WebKit，只加载终端外壳的静态布局夹具，不访问站点、不控制 Desk。
// 参数：<html 路径> <宽> <高>，用同一个二进制跑桌面与窄屏两种尺寸。
import Cocoa
import WebKit

let pagePath = CommandLine.arguments[1]
let width = Double(CommandLine.arguments[2]) ?? 1280
let height = Double(CommandLine.arguments[3]) ?? 800

final class Fixture: NSObject, WKScriptMessageHandler, WKNavigationDelegate {
    var view: WKWebView!
    var window: NSWindow!

    override init() {
        let config = WKWebViewConfiguration()
        super.init()
        config.userContentController.add(self, name: "result")
        view = WKWebView(frame: NSRect(x: 0, y: 0, width: width, height: height), configuration: config)
        window = NSWindow(contentRect: NSRect(x: 40, y: 40, width: width, height: height),
                          styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = view
        view.navigationDelegate = self
        window.title = "Desktron 终端外壳验收"
        window.orderFrontRegardless()
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        let data = try! JSONSerialization.data(withJSONObject: message.body, options: [.sortedKeys])
        print(String(data: data, encoding: .utf8)!)
        exit(0)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        FileHandle.standardError.write(Data("WebKit fixture loaded\n".utf8))
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
            self.view.evaluateJavaScript("void window.collectTerminalChrome(); null") { _, error in
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
let html = try String(contentsOfFile: pagePath, encoding: .utf8)
fixture.view.loadHTMLString(html, baseURL: nil)
DispatchQueue.main.asyncAfter(deadline: .now() + 45) {
    FileHandle.standardError.write(Data("WebKit fixture timeout\n".utf8))
    exit(1)
}
app.run()
