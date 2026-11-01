// desktron 原生桌面壳(macOS WKWebView,零外部依赖)
// 启动:后台拉起 `desktron web` → 轮询就绪 → 原生窗口加载本地 URL;关窗收子进程并退出。
import Cocoa
import WebKit
import ApplicationServices
import UserNotifications

// Anonymous agent tabs do not populate the user-delegated lease map. Keep a
// short active burst after actual work, rather than treating every step as idle.
func appControlPollInterval(hasLeases: Bool, now: TimeInterval, activeUntil: TimeInterval) -> TimeInterval {
    return hasLeases || now < activeUntil ? 0.35 : 1.5
}

let PORT = 7765   // UI 端口(desktron ui:自建前端+引擎适配器)
let URLSTR = "http://127.0.0.1:\(PORT)/"

// 仓库根定位(每个候选都校验"内含 desktron/bin/desktron"才采纳,避免挪库后用到失效路径):
//   1. DESKTRON_HOME 环境变量(开发者指向自己的工作树)
//   2. 规范家目录 ~/.local/share/desktron/home —— 运行时按当前用户 $HOME 拼出,挪 .app/换用户都对(分发主路径)
//   3. .app 内烤入的 desktron_home(dev/向后兼容;构建机路径在他人机器上会校验失败被跳过)
//   4. cwd 兜底
func repoRoot() -> String {
    let fm = FileManager.default
    func valid(_ p: String) -> Bool { !p.isEmpty && fm.fileExists(atPath: p + "/desktron/bin/desktron") }
    if let h = ProcessInfo.processInfo.environment["DESKTRON_HOME"], valid(h) { return h }
    let canonical = NSHomeDirectory() + "/.local/share/desktron/home"
    if valid(canonical) { return canonical }
    let legacy = NSHomeDirectory() + "/.local/share/desktron/app"
    if valid(legacy) { return legacy }
    if let res = Bundle.main.resourcePath,
       let s = try? String(contentsOfFile: res + "/desktron_home", encoding: .utf8) {
        let p = s.trimmingCharacters(in: .whitespacesAndNewlines)
        if valid(p) { return p }
    }
    return fm.currentDirectoryPath
}

func portOwnerPID() -> Int32? {
    let task = Process()
    let output = Pipe()
    task.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
    task.arguments = ["-nP", "-t", "-iTCP:\(PORT)", "-sTCP:LISTEN"]
    task.standardOutput = output
    task.standardError = FileHandle.nullDevice
    do { try task.run(); task.waitUntilExit() } catch { return nil }
    let data = output.fileHandleForReading.readDataToEndOfFile()
    return String(data: data, encoding: .utf8)?.split(whereSeparator: { $0.isWhitespace })
        .compactMap { Int32($0) }.first
}

func backendProbe() -> BackendProbe? {
    var request = URLRequest(url: URL(string: URLSTR + "desk-probe")!)
    request.timeoutInterval = 0.4
    let semaphore = DispatchSemaphore(value: 0)
    var result: BackendProbe?
    URLSession.shared.dataTask(with: request) { data, _, _ in
        defer { semaphore.signal() }
        guard let data = data else { return }
        result = try? JSONDecoder().decode(BackendProbe.self, from: data)
    }.resume()
    _ = semaphore.wait(timeout: .now() + 0.5)
    return result
}

// 标签容器:铺满全窗浮在浏览器之上,但空白处放行鼠标(命中自身=空白 → nil),否则网页整片点不动
final class PassthroughView: NSView {
    override func hitTest(_ point: NSPoint) -> NSView? {
        let v = super.hitTest(point)
        return v === self ? nil : v
    }
}

// 右侧栏里的网页正文是独立 WKWebView，层级高于 Desk 主 WebView。它若吃掉左缘命中，
// 前端贯穿全高的分隔条就只剩网页上方一段能拖。仅在前端明确上报热区时让左缘穿透；
// 视图尺寸不变，因此网页与分隔线之间不会多出一条缝。
final class BrowserWebView: WKWebView {
    var passThroughLeft: CGFloat = 0
    var appControlPreservesUserFocus: () -> Bool = { false }
    var appControlHumanInput: (() -> Void)?

    override func hitTest(_ point: NSPoint) -> NSView? {
        let local = convert(point, from: superview)
        if passThroughLeft > 0 && local.x >= 0 && local.x < passThroughLeft { return nil }
        return super.hitTest(point)
    }
}

// WebKit 会由内部子视图申请第一响应者；只覆写 WKWebView 本身拦不住。
// 定向输入只改变页面内焦点，不能把用户从 Desk 输入框或另一页移开。
final class DeskWindow: NSWindow {
    private func browserAncestor(_ responder: NSResponder?) -> BrowserWebView? {
        var view = responder as? NSView
        while let current = view {
            if let browser = current as? BrowserWebView { return browser }
            view = current.superview
        }
        return nil
    }
    override func makeFirstResponder(_ responder: NSResponder?) -> Bool {
        if browserAncestor(responder)?.appControlPreservesUserFocus() == true { return false }
        return super.makeFirstResponder(responder)
    }
    override func sendEvent(_ event: NSEvent) {
        // 系统分发的真人事件先让权，再交 WebKit；Agent 定向 NSEvent 不经过这条入口。
        let target: NSResponder?
        switch event.type {
        case .leftMouseDown, .rightMouseDown, .otherMouseDown, .scrollWheel:
            target = contentView.flatMap { $0.hitTest($0.convert(event.locationInWindow, from: nil)) }
        case .keyDown:
            target = firstResponder
        default:
            target = nil
        }
        browserAncestor(target)?.appControlHumanInput?()
        super.sendEvent(event)
    }
}

// 悬浮标签 chip:容器(留右上 5px margin 给角标)+ 圆形毛玻璃子视图 + 右上关闭角标。
// 圆形 NSVisualEffectView 提供 vibrancy 对身后网页做模糊;激活页用品牌色描边环;hover 出角标。
// 角标悬在圆外(margin 内),1:1 对齐 HTML .ps-x(top:-5/right:-5,14px,#161616 底+白 .14 边)。
final class ChipView: NSView {
    var tagId = ""
    var onClick: ((String) -> Void)?
    var onClose: ((String) -> Void)?
    private let glass = NSVisualEffectView()
    private let closeBtn = NSButton()
    private var tracking: NSTrackingArea?
    private var sz: CGFloat = 30

    func build(ch: String, active: Bool, size: CGFloat) {
        sz = size
        let brand = NSColor(red: 0.094, green: 0.604, blue: 0.706, alpha: 1)   // #189ab4
        glass.frame = NSRect(x: 0, y: 0, width: sz, height: sz)   // 圆在容器左下,右上是角标 margin
        glass.material = .hudWindow
        glass.blendingMode = .withinWindow
        glass.state = .active
        glass.wantsLayer = true
        glass.layer?.cornerRadius = sz / 2   // 圆形
        glass.layer?.borderWidth = 1
        glass.layer?.borderColor = NSColor(white: 1, alpha: active ? 0.16 : 0.10).cgColor
        glass.layer?.backgroundColor = NSColor(white: 1, alpha: active ? 0.07 : 0.0).cgColor
        addSubview(glass)

        let label = NSTextField(labelWithString: ch)
        label.font = .systemFont(ofSize: 12, weight: .medium)
        label.textColor = active ? brand : NSColor(white: 1, alpha: 0.66)   // 激活=字母变品牌蓝
        label.alignment = .center
        label.drawsBackground = false; label.isBezeled = false; label.isEditable = false; label.isSelectable = false
        let lh: CGFloat = 15
        label.frame = NSRect(x: 0, y: (sz - lh) / 2, width: sz, height: lh)
        glass.addSubview(label)

        closeBtn.frame = NSRect(x: sz - 9, y: sz - 9, width: 14, height: 14)   // 悬在圆右上角外
        closeBtn.isBordered = false
        closeBtn.wantsLayer = true
        closeBtn.layer?.backgroundColor = NSColor(white: 0.086, alpha: 1).cgColor   // #161616
        closeBtn.layer?.borderWidth = 1
        closeBtn.layer?.borderColor = NSColor(white: 1, alpha: 0.14).cgColor
        closeBtn.layer?.cornerRadius = 7
        closeBtn.attributedTitle = NSAttributedString(string: "×", attributes: [
            .foregroundColor: NSColor(white: 0.68, alpha: 1), .font: NSFont.systemFont(ofSize: 10)])   // text-weak
        closeBtn.isHidden = true
        closeBtn.target = self; closeBtn.action = #selector(closeClicked)
        addSubview(closeBtn)
    }
    @objc private func closeClicked() { onClose?(tagId) }
    override func mouseDown(with e: NSEvent) { onClick?(tagId) }
    // 圆内→整块点击;可见角标→关闭;透明边角→放行(nil)给身后网页
    override func hitTest(_ point: NSPoint) -> NSView? {
        let p = convert(point, from: superview)   // point 在 superview(rail) 坐标 → 容器本地
        if !closeBtn.isHidden && closeBtn.frame.contains(p) { return closeBtn }
        let r = sz / 2, dx = p.x - r, dy = p.y - r
        return dx * dx + dy * dy <= r * r ? self : nil
    }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let t = tracking { removeTrackingArea(t) }   // bounds 含 margin:鼠标移到角标上 hover 不丢
        let t = NSTrackingArea(rect: bounds, options: [.activeAlways, .mouseEnteredAndExited], owner: self)
        addTrackingArea(t); tracking = t
    }
    override func mouseEntered(with e: NSEvent) { closeBtn.isHidden = false }
    override func mouseExited(with e: NSEvent) { closeBtn.isHidden = true }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKUIDelegate, WKScriptMessageHandler, WKNavigationDelegate, WKDownloadDelegate, UNUserNotificationCenterDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var server: Process?
    var backendSupervisor: BackendSupervisor?
    var caffeine: Process?
    var serverLog: FileHandle?
    private var quitPending = false
    private var quitApproved = false
    private var pendingNotificationIDs = Set<String>()
    private let notificationHistoryKey = "DesktronDeliveredNotificationIDs"
    let startupStartedAt = ProcessInfo.processInfo.systemUptime
    var browsers: [String: WKWebView] = [:]   // 内置浏览器:外链页 id → 原生 WKWebView(覆盖悬浮层 body)
    var browserObservations: [String: [NSKeyValueObservation]] = [:]
    var browserModes: [String: String] = [:]  // 外链页 id → 专用原生模式标记；用于模式切换时重建 WKWebView
    var browserTabIds = Set<String>()         // 浏览器应用内部标签；不含会话里的原生嵌入
    var browserLastUsed: [String: Date] = [:] // 隐藏页按 LRU + 空闲时间受控回收
    var browserTrimmedAt = Date.distantPast
    var tagRail: NSView?   // 悬浮标签条:原生绘制,浮在浏览器 webview 之上(HTML 标签盖不住原生层)
    let appControlToken = UUID().uuidString
    let appControlWorld = WKContentWorld.world(name: "desk-agent-control")
    var appControlLeases: [String: String] = [:]   // browser tab id → lease id；只驻留原生宿主
    /// Agent 自开页的租约由 Runtime 自签,不走 toggleAppControl,因此不在 appControlLeases 里。
    /// 缺了这张表,视图被销毁时没人通知 Runtime:租约照旧 active,调用方每次只拿到一个不带
    /// 原因的 browser_tab_unavailable,而且不会自行恢复(2026-09-03 另一会话就卡在这个形状上)。
    /// 取活返回里带 lease_id,执行时顺手记下即可。
    var appControlAgentLeases: [String: String] = [:]
    var appControlEpochs: [String: Int] = [:]
    var appControlPaused = Set<String>()          // browser tab id:人正在操作,已让开
    var appControlLastTouch: [String: Date] = [:] // 最后一次真实输入时刻;静默够久就自动恢复
    var appControlPauseReason: [String: String] = [:]  // 让开的缘由:决定能不能自动接回
    var appControlIsolated = Set<String>()             // 用隔离存储建的标签(Agent 自开)
    var appControlAllowedMainFrameHosts: [String: Set<String>] = [:] // Agent 专用页逐跳主框架域名白名单
    var appControlTimerInterval: TimeInterval = 0     // 当前取活节奏,变了才重建计时器
    var appControlTimer: Timer?
    var appControlPolling = false
    var appControlPollIndex = 0
    var appControlActiveUntil: TimeInterval = 0
    var appControlDownloads: [ObjectIdentifier: String] = [:]
    var appControlNativeInputUntil: [String: Date] = [:]   // press/type 送真实按键的那一小段,不算人在操作
    let deskControlWindowId = "desk-" + UUID().uuidString.lowercased()
    var deskControlResourceId = ""
    var deskControlSessionId = ""
    var deskControlRequestGeneration = 0
    var desktopCapabilityReportedAt = Date.distantPast
    var desktopCapabilityLastStatus = ""
    var desktopCapabilityRetryPending = false
    // 原生应用 L1 只读观察；窗口由人在原生选择框中选定，详见 native_control.swift。
    lazy var nativeControl: NativeControlCoordinator = {
        let coordinator = NativeControlCoordinator(
            request: { [weak self] path, method, body, completion in
                guard let self = self else { completion(nil, "host_unavailable"); return }
                self.appControlRequest(path, method: method, body: body, completion: completion)
            },
            hostInstanceID: { [weak self] in self?.backendSupervisor?.hostInstanceID ?? "" },
            log: { [weak self] line in self?.startupLog(line) },
            emit: { [weak self] event in
                guard let self = self,
                      let data = try? JSONSerialization.data(withJSONObject: event),
                      let text = String(data: data, encoding: .utf8) else { return }
                self.web.evaluateJavaScript("window.__nativeControlEvent && window.__nativeControlEvent(\(text))")
            })
        coordinator.reportCapability = { [weak self] in self?.reportDesktopCapability(force: true) }
        return coordinator
    }()

    // Safari 桌面 UA:WKWebView 原生 UA 缺 "Version/X Safari/..." 段,部分内网 SSO 据此判为
    // 非标准浏览器、走降级/异常分支(扫码登录就坏在这)。补全成真 Safari UA。引擎仍是系统 WebKit,
    // 这里只改 UA 字符串,不改渲染内核。Mac 版本号按 Apple 现行做法冻结在 10_15_7。
    static let safariUA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"

    /// 允许 Safari 的「开发」菜单附着这个 webview。
    ///
    /// macOS 13.3 起 WKWebView 默认不可检查,于是 Desk 的页面在任何检查器里都看不到——
    /// 前端出问题时只能靠后端日志和在浏览器里复刻来推,而「只在 webview 里发生」的那一类
    /// 就此进了盲区(2026-08-10 为一个展不开的折叠行来回猜了三轮,起因正是这个)。
    /// 这是本机工作台,不是面向公众的应用,开着检查器的收益远大于代价。
    /// 用法:Safari → 设置 → 高级 → 勾「显示网页开发者功能」,然后「开发」菜单里找本机 → Desktron。
    static func allowInspect(_ wv: WKWebView) {
        if #available(macOS 13.3, *) { wv.isInspectable = true }
    }
    // Feishu Messenger 在 Chrome/Chromium 视图下才稳定展示消息分组列。WKWebView 仍是 WebKit,
    // 但对 lark-select-chat 这类自动选择页面使用 Chrome-like UA，让 Web 前端走同一套布局分支。
    static let chromeUA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

    static func isLarkSelectMode(_ nativeMode: String) -> Bool {
        nativeMode == "lark-select-chat" || nativeMode == "lark-select-chat-no-collapse"
    }

    static func isLarkSearchOnlyMode(_ nativeMode: String) -> Bool {
        nativeMode == "lark-search-chat"
    }

    static func isLarkSearchSelectMode(_ nativeMode: String) -> Bool {
        nativeMode == "lark-search-select-chat"
    }

    static func isLarkMessageReferenceMode(_ nativeMode: String) -> Bool {
        nativeMode == "lark-message-reference"
    }

    static func isLarkAutomationMode(_ nativeMode: String) -> Bool {
        AppDelegate.isLarkSelectMode(nativeMode) || AppDelegate.isLarkSearchOnlyMode(nativeMode) || AppDelegate.isLarkSearchSelectMode(nativeMode)
    }

    static func isChromeUserAgentMode(_ nativeMode: String) -> Bool {
        AppDelegate.isLarkSelectMode(nativeMode)
            || AppDelegate.isLarkMessageReferenceMode(nativeMode)
            || nativeMode == "chrome-ua"
    }

    static func userAgent(for nativeMode: String) -> String {
        AppDelegate.isChromeUserAgentMode(nativeMode) ? AppDelegate.chromeUA : AppDelegate.safariUA
    }

    // 关掉 ITP(跨站跟踪防护)。扫码登录的会话 cookie 常在跨域 redirect 链里下发,
    // 被 ITP 当第三方 cookie 拦掉 → 登录态留不住、弹回扫码页。私有 selector,guarded,
    // 不可用即静默跳过(只影响 SSO 体验,不致命)。内部未签名工具,接受私有 API。
    static func disableITP(_ store: WKWebsiteDataStore) {
        let sel = NSSelectorFromString("_setResourceLoadStatisticsEnabled:")
        guard store.responds(to: sel) else { return }
        typealias Fn = @convention(c) (NSObject, Selector, Bool) -> Void
        let imp = store.method(for: sel)
        unsafeBitCast(imp, to: Fn.self)(store, sel, false)
    }

    // 内置浏览器配置硬化:持久存储 + 不跟随系统代理 + 关 ITP。
    // (process pool 共享在 macOS 12+ 已无意义——同一 default store 的 webview 本就共享会话。)
    func hardenedConfig(nativeMode: String = "", chatId: String = "", chatName: String = "",
                        tagName: String = "", isolated: Bool = false) -> WKWebViewConfiguration {
        let cfg = WKWebViewConfiguration()
        // Agent 自行开的隔离标签用 ephemeral 存储:没有用户的 cookie 与内网 SSO,
        // 能力上限就是一个刚装好的浏览器。这正是它不需要人逐个点头的依据——
        // 默认存储是持久且全标签共享的,任何新标签一开就带着用户身份(见下一行)。
        cfg.websiteDataStore = isolated ? WKWebsiteDataStore.nonPersistent()
                                        : WKWebsiteDataStore.default()   // 持久,cookie 跨重启留存
        if #available(macOS 14.0, *) { cfg.websiteDataStore.proxyConfigurations = [] }
        AppDelegate.disableITP(cfg.websiteDataStore)
        // 通顶标记:前端据此给侧栏顶部留出红绿灯高度(旧壳没有该类,前端不加内边距,兼容平滑升级)
        cfg.userContentController.addUserScript(WKUserScript(
            source: "document.documentElement.classList.add('fullsize-shell')",
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true))
        if AppDelegate.isLarkAutomationMode(nativeMode) {
            cfg.userContentController.addUserScript(WKUserScript(
                source: larkSelectChatScript(
                    chatName: chatName,
                    tagName: tagName,
                    clickResult: nativeMode != "lark-search-chat"),
                injectionTime: .atDocumentEnd,
                forMainFrameOnly: true))
        }
        if AppDelegate.isLarkMessageReferenceMode(nativeMode) {
            cfg.userContentController.addUserScript(WKUserScript(
                source: larkMessageReferenceScript(),
                injectionTime: .atDocumentEnd,
                forMainFrameOnly: true))
        }
        return cfg
    }

    // 红绿灯重定位:下移 dy 与页内顶部导航同中线;左移到 targetX 与侧栏品牌字左对齐。
    // 幂等:x 以首次捕获的系统原位为基准平移(保留钮间距),y 从 titlebar 容器高度推算;resize 后系统复位,delegate 里重放。
    private var _tlOrigX: [CGFloat] = []
    func adjustTrafficLights() {
        let dy: CGFloat = 4
        let targetX: CGFloat = 20   // 与 .side-brand 的 padding-left(20px)对齐
        let btns = [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton].compactMap { window?.standardWindowButton($0) }
        guard btns.count == 3, let sv = btns[0].superview else { return }
        if _tlOrigX.isEmpty { _tlOrigX = btns.map { $0.frame.origin.x } }
        let dx = targetX - _tlOrigX[0]
        for (i, b) in btns.enumerated() {
            var f = b.frame
            f.origin.x = _tlOrigX[i] + dx
            f.origin.y = (sv.bounds.height - f.height) / 2 - dy
            b.frame = f
        }
    }
    func windowDidResize(_ n: Notification) { adjustTrafficLights() }
    func windowDidExitFullScreen(_ n: Notification) { adjustTrafficLights() }
    func windowDidBecomeKey(_ n: Notification) { adjustTrafficLights() }

    func applicationDidFinishLaunching(_ note: Notification) {
        let frame = NSRect(x: 0, y: 0, width: 1160, height: 860)   // 默认宽度收窄(原 1280)
        window = DeskWindow(contentRect: frame,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Desktron"
        window.center()
        window.delegate = self
        window.minSize = NSSize(width: 900, height: 600)
        window.titlebarAppearsTransparent = true   // 标题栏透明:透出 vibrancy,与页内头部连成一片
        window.titleVisibility = .hidden           // 通顶布局:标题文字不显示(窗口名仍在 Dock/Mission Control)
        window.isMovableByWindowBackground = true  // 无标题条后仍可拖动:按住空白背景即拖(交互控件不受影响)
        adjustTrafficLights()                      // 红绿灯下移,与页内顶部导航按钮同一中线
        // App Control 的取活循环从这里就起:Agent 自开页的第一个动作是开页本身,
        // 那时还没有任何用户委托,等「点了交给 Agent」才起表就永远没人取(实测停在 queued)。
        ensureAppControlTimer()

        // 不跟随系统代理:VPN 客户端的代理配置/例外清单随连接状态漂移,
        // 对本机服务(127.0.0.1)是稳定性风险;置空 = 全部直连。
        // 代价:内网 iframe 不再经系统代理(VPN 隧道路由仍可达)。
        // 共享 pool + 持久存储 + 关 ITP(扫码登录会话不被跨站防护拦)见 hardenedConfig。
        let cfg = hardenedConfig()
        cfg.userContentController.add(self, name: "browser")   // 前端←→原生内置浏览器桥
        cfg.userContentController.add(self, name: "windowDrag")   // 通顶壳:前端顶部拖拽区按下→原生 performDrag 拖窗
        cfg.userContentController.add(self, name: "windowZoom")   // 通顶壳:顶部双击→原生缩放(标题栏双击语义)
        cfg.userContentController.add(self, name: "startup")      // 首屏数据、历史与输入框可交互里程碑
        cfg.userContentController.add(self, name: "deskControl")  // 主页面显式委托 Desk navigation surface
        cfg.userContentController.add(self, name: "deskControlUser") // 主页面真人输入让 Agent 暂停
        cfg.userContentController.add(self, name: "nativeControl")  // 主页面委托内置清单中的原生应用窗口
        cfg.userContentController.add(self, name: "approvalControl") // 主页面审批与授权管理经原生固定能力桥代发
        cfg.userContentController.add(self, name: "desktopNotification") // 固定类型的部署／更新终态通知
        cfg.userContentController.addUserScript(WKUserScript(
            source: "for(const t of ['pointerdown','keydown'])document.addEventListener(t,e=>{if(e.isTrusted)try{window.webkit.messageHandlers.deskControlUser.postMessage({controlPlane:!!e.target?.closest?.('[data-app-control-control-plane],.hs-approval,.hs-sit-approvals')})}catch(_){ }},true)",
            injectionTime: .atDocumentStart, forMainFrameOnly: true))
        web = WKWebView(frame: frame, configuration: cfg)
        AppDelegate.allowInspect(web)
        web.customUserAgent = AppDelegate.safariUA
        web.uiDelegate = self
        web.navigationDelegate = self   // 主页重载/导航时先藏起原生浏览器面板、不销毁(见 didCommit)
        web.autoresizingMask = [.width, .height]
        // 透明 webview + 窗口 vibrancy:页面里半透明区域(rail/panel/页面)透出磨砂桌面=真毛玻璃;
        // 会话区(终端/聊天)在 CSS 里保持不透明,不透。
        web.setValue(false, forKey: "drawsBackground")
        window.appearance = NSAppearance(named: .darkAqua)
        let vfx = NSVisualEffectView(frame: frame)
        vfx.autoresizingMask = [.width, .height]
        vfx.material = .sidebar
        vfx.blendingMode = .behindWindow
        vfx.state = .active
        window.contentView = vfx
        vfx.addSubview(web)

        // 占位页
        web.loadHTMLString(
            "<body style='background:#1a1a1a;color:#ddd;font:15px -apple-system;display:flex;height:100vh;margin:0;align-items:center;justify-content:center'>正在启动 Desktron 引擎…</body>",
            baseURL: nil)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        UNUserNotificationCenter.current().delegate = self

        startBackendSupervisor()
    }

    func startBackendSupervisor() {
        let supervisor = BackendSupervisor(
            updateLauncher: { [weak self] operationID, hostPID in
                guard let self = self else { throw NSError(domain: "DeskHost", code: 2) }
                return try self.launchUpdateHelper(operationID: operationID, hostPID: hostPID)
            },
            launcher: { [weak self] hostID, generation in
                guard let self = self else { throw NSError(domain: "DeskHost", code: 1) }
                return try self.launchServer(hostID: hostID, generation: generation)
            },
            probeReader: backendProbe,
            portOwner: portOwnerPID)
        supervisor.onHealthy = { [weak self] _ in
            guard let self = self else { return }
            self.startupLog("backend ready")
            // 新后端已撤销旧目标；本机状态同步作废，不向新后端重放读取。
            self.nativeControl.resetForBackendRestart()
            self.reportDesktopCapability(force: true)
            self.web.load(URLRequest(url: URL(string: URLSTR)!))
        }
        supervisor.onPhase = { [weak self] phase in
            guard let self = self else { return }
            switch phase {
            case .foreignPort(let pid):
                let detail = pid.map(String.init) ?? "未知"
                self.web.loadHTMLString("<body style='background:#1a1a1a;color:#ddd;font:15px -apple-system;padding:40px;text-align:center'>端口 \(PORT) 被非当前后端占用（PID \(detail)）。Desktron 未接管或终止该进程。</body>", baseURL: nil)
            case .failed(let reason):
                self.web.loadHTMLString("<body style='background:#1a1a1a;color:#ddd;font:15px -apple-system;padding:40px;text-align:center'>引擎连续启动失败：\(reason)</body>", baseURL: nil)
            default: break
            }
        }
        supervisor.onUpdateAccepted = { [weak self, weak supervisor] _ in
            guard let self = self, let supervisor = supervisor else { return }
            supervisor.shutdown {
                self.quitPending = false
                self.quitApproved = true
                NSApp.terminate(nil)
            }
        }
        backendSupervisor = supervisor
        supervisor.start()
    }

    func launchUpdateHelper(operationID: String, hostPID: Int32) throws -> Process {
        let root = repoRoot()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [root + "/desktron/bin/desktron", "_host-update-apply",
                       operationID, String(hostPID)]
        p.currentDirectoryURL = URL(fileURLWithPath: root)
        p.environment = ProcessInfo.processInfo.environment
        let updates = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/share/desktron/updates", isDirectory: true)
        try FileManager.default.createDirectory(at: updates, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let logURL = updates.appendingPathComponent("apply.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let log = try FileHandle(forWritingTo: logURL)
        log.seekToEndOfFile()
        if let header = "\n[host-update] operation=\(operationID) host_pid=\(hostPID)\n".data(using: .utf8) {
            log.write(header)
        }
        p.standardOutput = log
        p.standardError = log
        try p.run()
        log.closeFile()
        return p
    }

    func launchServer(hostID: String, generation: String) throws -> Process {
        let launchedAt = Date()
        let root = repoRoot()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [root + "/desktron/bin/desktron", "ui", String(PORT)]
        p.currentDirectoryURL = URL(fileURLWithPath: root)
        var env = ProcessInfo.processInfo.environment
        env["DESKTRON_NATIVE_LAUNCH"] = "1"
        env["DESKTRON_NATIVE_HOST_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        env["DESKTRON_HOST_INSTANCE_ID"] = hostID
        env["DESKTRON_LAUNCH_GENERATION"] = generation
        env["DESKTRON_HOST_CONTROL_TOKEN"] = backendSupervisor?.controlToken ?? ""
        if let resources = Bundle.main.resourceURL {
            let hostRev = (try? String(contentsOf: resources.appendingPathComponent("release-rev"),
                                      encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let hostBuild = (try? String(contentsOf: resources.appendingPathComponent("release-build"),
                                        encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            env["DESKTRON_HOST_REV"] = hostRev
            env["DESKTRON_HOST_BUILD"] = hostBuild
        }
        // 只有原生宿主持有。委托、审批与 adapter 回写端点缺它一律 fail closed；
        // 不注入主页面 JS，避免同源第三方 iframe 读取后伪装成人的确认。
        env["DESKTRON_APP_CONTROL_HOST_TOKEN"] = appControlToken
        p.environment = env
        let log = FileHandle(forWritingAtPath: "/tmp/desktron-web.log")
            ?? { FileManager.default.createFile(atPath: "/tmp/desktron-web.log", contents: nil)
                 return FileHandle(forWritingAtPath: "/tmp/desktron-web.log") }()
        if let log = log {
            // 每次启动从干净日志开始。原来只从文件头覆盖，旧文件更长时尾部会留下上一轮
            // traceback，排查重启时会把历史错误误认成本轮错误。
            log.truncateFile(atOffset: 0)
            log.seek(toFileOffset: 0)
            let stamp = ISO8601DateFormatter().string(from: Date())
            if let line = "[startup] native launch \(stamp)\n".data(using: .utf8) {
                log.write(line)
            }
            p.standardOutput = log
            p.standardError = log
            serverLog = log
        }
        do {
            try runNativeBackendProcess(p)
        } catch {
            if let line = "[startup] native process launch failed: \(error)\n".data(using: .utf8) {
                log?.write(line)
            }
            throw error
        }
        if let line = String(format: "[startup] native process launched (%.0fms)\n",
                             Date().timeIntervalSince(launchedAt) * 1000).data(using: .utf8) {
            log?.write(line)
        }
        // 先让服务进入启动链，再异步申请电源断言。caffeinate 偶发在 IOKit 里卡约 30 秒，
        // 若用它包住 Python，这 30 秒会完整挡住首屏；-w 仍让断言与服务 PID 同生共死。
        caffeine?.terminate()
        let c = Process()
        c.executableURL = URL(fileURLWithPath: "/usr/bin/caffeinate")
        c.arguments = ["-is", "-w", String(p.processIdentifier)]
        c.standardOutput = log
        c.standardError = log
        do {
            try c.run()
            caffeine = c
        } catch {
            caffeine = nil
            if let line = "[startup] sleep inhibitor unavailable: \(error)\n".data(using: .utf8) {
                log?.write(line)
            }
        }
        server = p
        return p
    }

    func startupLog(_ phase: String) {
        let elapsed = (ProcessInfo.processInfo.systemUptime - startupStartedAt) * 1000
        guard let line = String(format: "[startup] native %@ %.0fms\n", phase, elapsed)
                .data(using: .utf8) else { return }
        serverLog?.write(line)
    }

    // JS alert
    func webView(_ wv: WKWebView, runJavaScriptAlertPanelWithMessage msg: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler done: @escaping () -> Void) {
        let controlId = appControlNeedsUser(wv, reason: "javascript_dialog")
        let a = NSAlert(); a.messageText = msg; a.addButton(withTitle: "好")
        a.beginSheetModal(for: window) { [weak self] _ in
            done(); if let id = controlId { self?.resumeAppControl(id: id) }
        }
    }
    // JS confirm
    func webView(_ wv: WKWebView, runJavaScriptConfirmPanelWithMessage msg: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler done: @escaping (Bool) -> Void) {
        let controlId = appControlNeedsUser(wv, reason: "javascript_dialog")
        let a = NSAlert(); a.messageText = msg
        a.addButton(withTitle: "确定"); a.addButton(withTitle: "取消")
        a.beginSheetModal(for: window) { [weak self] resp in
            done(resp == .alertFirstButtonReturn)
            if let id = controlId { self?.resumeAppControl(id: id) }
        }
    }
    // JS prompt
    func webView(_ wv: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler done: @escaping (String?) -> Void) {
        let controlId = appControlNeedsUser(wv, reason: "javascript_dialog")
        let a = NSAlert(); a.messageText = prompt
        let tf = NSTextField(frame: NSRect(x: 0, y: 0, width: 240, height: 24))
        tf.stringValue = defaultText ?? ""; a.accessoryView = tf
        a.addButton(withTitle: "确定"); a.addButton(withTitle: "取消")
        a.beginSheetModal(for: window) { [weak self] resp in
            done(resp == .alertFirstButtonReturn ? tf.stringValue : nil)
            if let id = controlId { self?.resumeAppControl(id: id) }
        }
    }

    // 文件选择:WKWebView 不自带 <input type="file"> 的选择面板,宿主不实现这个方法,
    // 点了附件钮就是没有任何反应(前端已经 .click() 了,只是没人接)。整个应用里所有
    // input[type=file] 都走这里——聊天框的附件、技能与知识的上传都是。
    func webView(_ wv: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler done: @escaping ([URL]?) -> Void) {
        let controlId = appControlNeedsUser(wv, reason: "file_upload")
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.prompt = "选择"
        if let w = window {
            panel.beginSheetModal(for: w) { [weak self] resp in
                done(resp == .OK ? panel.urls : nil)
                if let id = controlId { self?.resumeAppControl(id: id) }
            }
        } else {
            panel.begin { [weak self] resp in
                done(resp == .OK ? panel.urls : nil)
                if let id = controlId { self?.resumeAppControl(id: id) }
            }
        }
    }

    /// 系统弹窗、上传选择器与下载路径不能由 Agent 代填。宿主把租约暂停并交给人，
    /// 人处理完后再恢复；能力边界同时会出现在 document.capabilities 中。
    private func appControlNeedsUser(_ view: WKWebView, reason: String) -> String? {
        guard let id = browsers.first(where: { $0.value === view })?.key,
              appControlLease(id) != nil else { return nil }
        pauseAppControl(id: id, reason: reason)
        return id
    }

    private func deliverDesktopNotification(eventID: String, kind: String) {
        guard eventID.count <= 180,
              eventID.range(of: #"^[A-Za-z0-9:_-]+$"#, options: .regularExpression) != nil else { return }
        let messages: [String: (String, String)] = [
            "update_succeeded": ("Desktron 更新完成", "新版本已安装并正常启动。"),
            "update_failed": ("Desktron 更新未完成", "请在设置中查看更新状态和日志。"),
        ]
        guard let message = messages[kind] else { return }
        let delivered = Set(UserDefaults.standard.stringArray(forKey: notificationHistoryKey) ?? [])
        guard !delivered.contains(eventID), !pendingNotificationIDs.contains(eventID) else { return }
        pendingNotificationIDs.insert(eventID)
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound]) { [weak self] granted, _ in
            guard let self = self else { return }
            DispatchQueue.main.async {
                guard granted else {
                    self.pendingNotificationIDs.remove(eventID)
                    return
                }
                let content = UNMutableNotificationContent()
                content.title = message.0
                content.body = message.1
                content.sound = .default
                let request = UNNotificationRequest(identifier: eventID, content: content, trigger: nil)
                center.add(request) { error in
                    DispatchQueue.main.async {
                        self.pendingNotificationIDs.remove(eventID)
                        guard error == nil else { return }
                        var history = UserDefaults.standard.stringArray(forKey: self.notificationHistoryKey) ?? []
                        if !history.contains(eventID) { history.append(eventID) }
                        if history.count > 64 { history.removeFirst(history.count - 64) }
                        UserDefaults.standard.set(history, forKey: self.notificationHistoryKey)
                    }
                }
            }
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    // ---- 内置浏览器(原生 WKWebView 面板):前端把外链路由到这里,绕过 iframe 的 X-Frame-Options/CSP ----
    // 前端发 {action, id, url, rect}:open=建/复用并显示, rect=跟随悬浮层 body 改尺寸, hide=收起(切到 iframe 页/关层),
    // close=销毁, back/forward/reload/navigate=导航。rect 为前端视口坐标(左上原点),这里转成 AppKit 左下原点。
    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.name == "startup" {
            guard let b = message.body as? [String: Any], let phase = b["phase"] as? String else { return }
            let webMs = (b["ms"] as? NSNumber)?.doubleValue ?? -1
            let detail = (b["detail"] as? String) ?? ""
            startupLog(String(format: "frontend %@ web=%.0fms %@", phase, webMs, detail))
            if phase == "interactive" { replayAppControlState() }   // 重载后把仍在手上的委托状态还给新页面
            return
        }
        if message.name == "windowDrag" {   // webview 吃掉鼠标事件,isMovableByWindowBackground 穿不过:前端上报,原生代拖
            if let ev = NSApp.currentEvent { window.performDrag(with: ev) }
            return
        }
        if message.name == "windowZoom" {   // 顶部双击缩放(标题栏语义)
            window.performZoom(nil)
            return
        }
        if message.name == "appControlUser" {
            // 宿主自己送的真实按键(press/type)同样命中页面里那段 isTrusted 监听,不算人在操作
            if let source = message.webView,
               let id = browsers.first(where: { $0.value === source })?.key,
               !(appControlNativeInputUntil[id].map { $0 > Date() } ?? false) {
                pauseAppControl(id: id, reason: "user_interaction")
            }
            return
        }
        if message.name == "deskControlUser" {
            guard message.frameInfo.isMainFrame,
                  let body = message.body as? [String: Any],
                  body["controlPlane"] as? Bool != true,
                  !deskControlResourceId.isEmpty else { return }
            deskControlRequestGeneration += 1
            pauseAppControl(id: deskControlResourceId, reason: "user_interaction")
            return
        }
        if message.name == "deskControl" {
            guard message.frameInfo.isMainFrame,
                  let body = message.body as? [String: Any],
                  let action = body["action"] as? String else { return }
            if action == "delegate" {
                delegateDeskControl(sessionId: body["sessionId"] as? String ?? "")
            } else if action == "takeover" || action == "resourceChanged" {
                revokeDeskControl(reason: action == "resourceChanged" ? "resource_changed" : "user_takeover")
            }
            return
        }
        if message.name == "nativeControl" {
            guard message.frameInfo.isMainFrame,
                  let body = message.body as? [String: Any],
                  let action = body["action"] as? String else { return }
            nativeControl.handle(action: action, appID: body["appId"] as? String ?? "",
                                 sessionID: body["sessionId"] as? String ?? "")
            return
        }
        if message.name == "desktopNotification" {
            let origin = message.frameInfo.securityOrigin
            guard message.frameInfo.isMainFrame, message.webView === web,
                  origin.protocol == "http", origin.host == "127.0.0.1", origin.port == PORT,
                  let body = message.body as? [String: Any],
                  let eventID = body["eventId"] as? String,
                  let kind = body["kind"] as? String else { return }
            deliverDesktopNotification(eventID: eventID, kind: kind)
            return
        }
        if message.name == "approvalControl" {
            guard message.frameInfo.isMainFrame,
                  let body = message.body as? [String: Any],
                  let requestId = body["requestId"] as? String, !requestId.isEmpty,
                  let action = body["action"] as? String else { return }
            let path: String
            let method: String
            var requestBody: [String: Any]? = nil
            switch action {
            case "list":
                path = "/workbench/app-control/approvals?include_resolved=1&limit=200"
                method = "GET"
            case "grantsList":
                path = "/workbench/app-control/grants?app_id=browser"
                method = "GET"
            case "grantRevoke":
                path = "/workbench/app-control/grants"
                method = "DELETE"
                requestBody = ["app_id": "browser",
                               "origin": body["origin"] as? String ?? "",
                               "scope_key": body["scopeKey"] as? String ?? ""]
            case "decide":
                path = "/workbench/app-control/approvals/decide"
                method = "POST"
                requestBody = ["operation_id": body["operationId"] as? String ?? "",
                               "approved": body["approved"] as? Bool ?? false]
                if let anchor = body["anchor"] as? [String: Any] { requestBody?["anchor"] = anchor }
            case "clear":
                path = "/workbench/app-control/approvals/clear"
                method = "POST"
                requestBody = ["before": body["before"] as? NSNumber ?? 0]
            default:
                sendApprovalControlResult(requestId, result: nil, error: "unsupported_approval_action")
                return
            }
            appControlRequest(path, method: method, body: requestBody) { [weak self] result, error in
                self?.sendApprovalControlResult(requestId, result: result, error: error)
            }
            return
        }
        guard message.name == "browser", let b = message.body as? [String: Any],
              let action = b["action"] as? String else { return }
        let id = b["id"] as? String ?? ""
        switch action {
        case "open":  openBrowser(
            id: id,
            url: b["url"] as? String ?? "",
            rect: b["rect"],
            nativeMode: b["nativeMode"] as? String ?? "",
            chatId: b["chatId"] as? String ?? "",
            chatName: b["chatName"] as? String ?? "",
            tagName: b["tagName"] as? String ?? "",
            isolated: b["isolated"] as? Bool ?? false,
            browserTab: b["browserTab"] as? Bool ?? false,
            background: b["background"] as? Bool ?? false,
            allowedMainFrameHosts: b["allowedMainFrameHosts"] as? [String] ?? [])
        case "rect":  if let v = browsers[id], !v.isHidden { applyRect(v, b["rect"]) }
        case "hide":  for (_, v) in browsers { v.isHidden = true }
        case "hideOne": if let v = browsers[id] { v.isHidden = true }
        case "tags":  renderTags(b["items"] as? [[String: Any]] ?? [])
        case "close":
            revokeAppControl(id: id, reason: "tab_closed")
            discardBrowserView(id, reason: "frontend_close")
        case "navigate": if let v = browsers[id], let u = URL(string: b["url"] as? String ?? "") { v.load(URLRequest(url: u)) }
        case "back":    browsers[id]?.goBack()
        case "forward": browsers[id]?.goForward()
        case "reload":  browsers[id]?.reload()
        case "larkContext":
            guard message.frameInfo.isMainFrame else { return }
            readLarkReferenceContext(
                id: id,
                requestId: b["requestId"] as? String ?? "")
        case "pickFolder": pickFolder()
        case "agentDelegate":
            // 只有主 WKWebView 的用户点击可以到这条路；browser 页面与第三方 frame 不可代发。
            guard message.frameInfo.isMainFrame else { return }
            toggleAppControl(id: id, sessionId: b["sessionId"] as? String ?? "")
        case "agentTakeover":
            guard message.frameInfo.isMainFrame else { return }
            revokeAppControl(id: id, reason: b["reason"] as? String ?? "user_takeover")
        case "agentPause":
            guard message.frameInfo.isMainFrame else { return }
            pauseAppControl(id: id, reason: b["reason"] as? String ?? "user_interaction")
        case "agentResume":
            guard message.frameInfo.isMainFrame else { return }
            resumeAppControl(id: id)
        // 窗口外观跟着页面主题走。页面里那些半透明区域(rail / panel / 页面)透出的是窗口
        // 这一层 vibrancy,它写死 darkAqua 的话,浅色主题下透出来的仍是深色磨砂,半透明处
        // 整片发灰——只能把它们改成不透明,毛玻璃就没了。这里让原生跟着切,浅色下才能继续半透明。
        case "appearance":
            let light = (b["mode"] as? String) == "light"
            window.appearance = NSAppearance(named: light ? .aqua : .darkAqua)
        default: break
        }
    }

    func pickFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "打开"
        panel.message = "选择一个目录作为 coding 项目"
        panel.begin { [weak self] resp in
            guard let self = self else { return }
            let path = (resp == .OK) ? (panel.url?.path ?? "") : ""
            self.web.evaluateJavaScript("window.__pickFolderCb && window.__pickFolderCb(\"\(self.jsEsc(path))\")")
        }
    }

    /// background=true:只把视图建出来并留在后台,不显示、不抢前台、不动别的面板。
    ///
    /// Agent 自开页的意义就是在后台干活,而建视图这条路原本要求「浏览器应用是当前可见页」
    /// (前端 browserSyncNative 的 browserVisible 闸)。人正看着会话时那道闸恒为假,补多少次
    /// 同步都建不出视图——2026-09-03 另一会话连撞两轮:先是假 succeeded,补了等待之后变成
    /// browser_view_not_created。而为开一个 Agent 页抢占前台同样不对,那会打断人正在读的东西。
    /// 所以让视图先在后台存在:JS 照常跑、inspect/act 照常可用;等人真的切到浏览器应用,
    /// 原来那条同步会复用同一个视图并显示出来。
    func openBrowser(id: String, url: String, rect: Any?, nativeMode: String = "", chatId: String = "", chatName: String = "", tagName: String = "", isolated: Bool = false, browserTab: Bool = false, background: Bool = false, allowedMainFrameHosts: [String] = []) {
        let hosts = Set(allowedMainFrameHosts.map { $0.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: ".")) }
            .filter { !$0.isEmpty })
        if hosts.isEmpty { appControlAllowedMainFrameHosts.removeValue(forKey: id) }
        else { appControlAllowedMainFrameHosts[id] = hosts }
        let v: WKWebView
        var created = false
        if let e = browsers[id], (browserModes[id] ?? "") == nativeMode {
            v = e
            browserViewLog(id, "reuse", "background=\(background)")
        }
        else {
            created = true
            if let old = browsers[id] {
                browserViewLog(id, "rebuild", "mode=\(browserModes[id] ?? "")->\(nativeMode)")
                browserObservations.removeValue(forKey: id)
                old.removeFromSuperview(); browsers.removeValue(forKey: id)
            }
            browserModes[id] = nativeMode
            let bcfg = hardenedConfig(nativeMode: nativeMode, chatId: chatId, chatName: chatName,
                                      tagName: tagName, isolated: isolated)   // 共享 pool + 持久/隔离存储 + 不跟随系统代理 + 关 ITP(扫码登录)
            if isolated { appControlIsolated.insert(id) }
            // 真用户在被委托页面上点击或输入时立即撤回租约。isTrusted 过滤 Agent 注入的合成事件。
            bcfg.userContentController.add(self, name: "appControlUser")
            bcfg.userContentController.addUserScript(WKUserScript(
                source: "for(const t of ['pointerdown','keydown'])document.addEventListener(t,e=>{if(e.isTrusted)try{window.webkit.messageHandlers.appControlUser.postMessage({kind:'interaction'})}catch(_){ }},true)",
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true))
            // 页面世界里只记 fetch/XHR 的在途数量与最后活动时间，不读取 URL、请求头或正文。
            // App Control 的隔离世界据此判断网络空闲；注入到各 frame，同源 frame 会一起统计。
            bcfg.userContentController.addUserScript(WKUserScript(
                source: #"""
                (()=>{if(globalThis.__deskNetworkTracker)return;globalThis.__deskNetworkTracker=true;
                let pending=0,last=Date.now();const publish=()=>{const root=document.documentElement;if(!root)return;
                root.setAttribute('data-desk-network-pending',String(pending));root.setAttribute('data-desk-network-last',String(last));};
                const begin=()=>{pending+=1;last=Date.now();publish();let ended=false;return()=>{if(ended)return;ended=true;
                pending=Math.max(0,pending-1);last=Date.now();publish();};};
                const oldFetch=globalThis.fetch;if(typeof oldFetch==='function')globalThis.fetch=function(...args){const end=begin();
                try{return oldFetch.apply(this,args).finally(end)}catch(error){end();throw error}};
                const oldSend=globalThis.XMLHttpRequest?.prototype?.send;if(oldSend)globalThis.XMLHttpRequest.prototype.send=function(...args){
                const end=begin();this.addEventListener('loadend',end,{once:true});try{return oldSend.apply(this,args)}catch(error){end();throw error}};
                publish();document.addEventListener('readystatechange',publish);})();
                """#,
                injectionTime: .atDocumentStart,
                forMainFrameOnly: false))
            v = BrowserWebView(frame: .zero, configuration: bcfg)
            if let browser = v as? BrowserWebView {
                browser.appControlPreservesUserFocus = { [weak self] in
                    guard let self = self else { return false }
                    return self.appControlLease(id) != nil && !self.appControlPaused.contains(id)
                }
                browser.appControlHumanInput = { [weak self] in
                    self?.pauseAppControl(id: id, reason: "user_interaction")
                }
            }
            AppDelegate.allowInspect(v)   // 内置浏览器面板同样可检查(内网登录出问题时要看它)
            v.customUserAgent = AppDelegate.userAgent(for: nativeMode)
            v.navigationDelegate = self
            v.uiDelegate = self
            browsers[id] = v
            observeBrowserState(v, id: id)
            browserViewLog(id, "create", "background=\(background) isolated=\(isolated)")
            web.superview?.addSubview(v)
            if let u = URL(string: url) { v.load(URLRequest(url: u)) }
        }
        if browserTab { browserTabIds.insert(id) }
        browserLastUsed[id] = Date()
        reportBrowser(v, phase: "state")   // 宿主页重载或切回标签时，以仍存活的原生页为准
        if background {
            // 给一个真实尺寸再藏起来:零尺寸的 WKWebView 不做布局,快照与元素矩形全是 0,
            // inspect 出来的节点就没有可用位置。藏而不是移出屏幕——移出去在多显示器与
            // 缩放下行为不一致。不动别的面板,也不把自己提到最前。
            if created {
                v.frame = NSRect(x: 0, y: 0, width: 1280, height: 800)
                v.isHidden = true
            }
            return
        }
        for (k, ov) in browsers where k != id { ov.isHidden = true }   // 同一时刻只显示当前页
        v.isHidden = false
        web.superview?.addSubview(v, positioned: .above, relativeTo: nil)   // 提到最前(原生层永远盖在 HTML 上)
        applyRect(v, rect)
        bringTagsToFront()   // 浏览器提到最前后,标签条要再提一次,否则被网页盖住
        if AppDelegate.isLarkAutomationMode(nativeMode) {
            applyLarkSelectChat(
                v,
                chatName: chatName,
                tagName: tagName,
                clickResult: nativeMode != "lark-search-chat")
        }
        trimBrowserViews(activeId: id, enforceLimit: true)
    }

    /// 刚切走的网页只隐藏并保留完整运行态；长期不用或驻留过多时，才回收底层 WKWebView。
    /// 标签条与 URL 仍由前端保留，重新激活会走 openBrowser 重建。用户委托给 Agent 的页面
    /// 不参与回收；Agent 自开页也给足一个完整租约周期，避免卡在模型思考与下一动作之间。
    private func trimBrowserViews(activeId: String = "", enforceLimit: Bool = false) {
        let now = Date()
        if !enforceLimit && now.timeIntervalSince(browserTrimmedAt) < 60 { return }
        browserTrimmedAt = now
        let idleLimit: TimeInterval = 15 * 60
        let residentLimit = 6
        var candidates = Array(browserTabIds.filter { id in
            id != activeId && appControlLeases[id] == nil && (browsers[id]?.isHidden == true)
        })
        for id in candidates where now.timeIntervalSince(browserLastUsed[id] ?? .distantPast) >= idleLimit {
            discardBrowserView(id, reason: "trim_idle")
        }
        var resident = browserTabIds.filter { browsers[$0] != nil }.count
        if resident <= residentLimit { return }
        // 硬上限只淘汰普通用户标签。Agent 自开页仍受上面的 15 分钟空闲阈值约束。
        candidates = browserTabIds.filter { id in
            id != activeId && !id.hasPrefix("agent-") && !id.hasPrefix("user-")
                && appControlLeases[id] == nil && (browsers[id]?.isHidden == true)
        }.sorted { (browserLastUsed[$0] ?? .distantPast) < (browserLastUsed[$1] ?? .distantPast) }
        for id in candidates where resident > residentLimit {
            discardBrowserView(id, reason: "trim_limit")
            resident -= 1
        }
    }

    /// 原生视图的生死全记在这里。事实源只记操作的成败,记不到「视图是被谁清掉的」——
    /// 2026-09-03 另一会话撞上「open 报成功、三秒后 inspect 说标签不在」,从外部只看得到
    /// browsers[id] 的进出结果,看不到中间过程,查到这里就断了。
    private func browserViewLog(_ id: String, _ what: String, _ extra: String = "") {
        let ts = AppDelegate.logStamp.string(from: Date())
        let line = "\(ts) \(what) id=\(id) resident=\(browsers.count)"
            + " lease=\(appControlLeases[id] != nil) \(extra)\n"
        let dir = NSHomeDirectory() + "/.local/share/desktron/data/app-control"
        let path = dir + "/native-browser.log"
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        guard let data = line.data(using: .utf8) else { return }
        if let h = FileHandle(forWritingAtPath: path) {
            h.seekToEndOfFile(); h.write(data); try? h.close()
        } else {
            try? data.write(to: URL(fileURLWithPath: path))
        }
    }

    private static let logStamp: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss.SSS"
        return f
    }()

    private func discardBrowserView(_ id: String, reason: String = "") {
        browserViewLog(id, "discard", "reason=" + (reason.isEmpty ? "unknown" : reason))
        revokeAgentLease(id, reason: reason.isEmpty ? "view_discarded" : reason)
        browserObservations.removeValue(forKey: id)
        if let v = browsers.removeValue(forKey: id) {
            v.stopLoading()
            v.navigationDelegate = nil
            v.uiDelegate = nil
            v.removeFromSuperview()
        }
        browserModes.removeValue(forKey: id)
        browserTabIds.remove(id)
        browserLastUsed.removeValue(forKey: id)
        appControlEpochs.removeValue(forKey: id)
        appControlAgentLeases.removeValue(forKey: id)
        appControlIsolated.remove(id)
        appControlAllowedMainFrameHosts.removeValue(forKey: id)
    }

    // ---- Agent 可操作应用：宿主委托 / 轮询 / WKWebView adapter ----
    // 只读查询当前进程是否已被 Accessibility 信任。禁止使用
    // 带提示的授权查询变体，因此不会弹授权框、打开系统设置或执行 AX 动作。
    private func reportDesktopCapability(force: Bool = false, retriesRemaining: Int = 3) {
        let status = AXIsProcessTrusted() ? "trusted" : "not_trusted"
        let now = Date()
        guard force || status != desktopCapabilityLastStatus
                || now.timeIntervalSince(desktopCapabilityReportedAt) >= 10 else { return }
        guard let supervisor = backendSupervisor,
              let bundleID = Bundle.main.bundleIdentifier,
              bundleID == "io.github.desktron" else {
            startupLog("desktop capability report skipped: host bundle identity unavailable")
            return
        }
        appControlRequest("/workbench/app-control/host/desktop-capability", method: "POST", body: [
            "host_instance_id": supervisor.hostInstanceID,
            "host_bundle_id": bundleID,
            "host_pid": ProcessInfo.processInfo.processIdentifier,
            "status": status,
            "checked_at": now.timeIntervalSince1970,
            "monitoring_status": nativeControl.monitoringStatus
        ]) { [weak self] _, error in
            guard let self = self else { return }
            if let error = error {
                self.startupLog("desktop capability report failed: \(error)")
                guard retriesRemaining > 0, !self.desktopCapabilityRetryPending else { return }
                self.desktopCapabilityRetryPending = true
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
                    self?.desktopCapabilityRetryPending = false
                    self?.reportDesktopCapability(force: true,
                                                  retriesRemaining: retriesRemaining - 1)
                }
                return
            }
            self.desktopCapabilityReportedAt = now
            self.desktopCapabilityLastStatus = status
        }
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        reportDesktopCapability()
    }

    private func appControlRequest(_ path: String, method: String = "GET",
                                   body: [String: Any]? = nil,
                                   completion: @escaping ([String: Any]?, String?) -> Void) {
        guard let url = URL(string: "http://127.0.0.1:\(PORT)" + path) else {
            completion(nil, "bad_url"); return
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue(appControlToken, forHTTPHeaderField: "X-Desk-App-Control-Host")
        if let body = body {
            guard JSONSerialization.isValidJSONObject(body),
                  let data = try? JSONSerialization.data(withJSONObject: body) else {
                completion(nil, "bad_body"); return
            }
            request.httpBody = data
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        URLSession.shared.dataTask(with: request) { data, response, error in
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            var object: [String: Any]? = nil
            if let data = data {
                object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            }
            let message = error?.localizedDescription
                ?? ((status >= 200 && status < 300) ? nil : (object?["error"] as? String ?? "http_\(status)"))
            DispatchQueue.main.async { completion(object, message) }
        }.resume()
    }

    private func sendApprovalControlResult(_ requestId: String, result: [String: Any]?,
                                           error: String?) {
        let value: [String: Any] = ["requestId": requestId,
                                    "result": result ?? NSNull(),
                                    "error": error ?? ""]
        guard let data = try? JSONSerialization.data(withJSONObject: value),
              let text = String(data: data, encoding: .utf8) else { return }
        web.evaluateJavaScript("window.__approvalNativeResult && window.__approvalNativeResult(\(text))")
    }

    private func sendAppControlEvent(id: String, type: String, leaseId: String = "",
                                     error: String = "", sessionId: String = "") {
        let isDesk = id.hasPrefix("desk-window:")
        let boundSession = isDesk && sessionId.isEmpty ? deskControlSessionId : sessionId
        let value: [String: Any] = ["id": id, "surface": isDesk ? "navigation" : "",
                                    "type": type, "lease_id": leaseId, "error": error,
                                    "session_id": boundSession,
                                    "interaction_epoch": isDesk ? deskControlRequestGeneration : 0]
        guard let data = try? JSONSerialization.data(withJSONObject: value),
              let text = String(data: data, encoding: .utf8) else { return }
        let receiver = isDesk ? "__deskControlEvent" : "__browserControlEvent"
        web.evaluateJavaScript("window.\(receiver) && window.\(receiver)(\(text))")
    }

    private func delegateDeskControl(sessionId: String) {
        guard !sessionId.isEmpty, deskControlResourceId.isEmpty else { return }
        let resourceId = "desk-window:\(deskControlWindowId):navigation"
        deskControlRequestGeneration += 1
        let generation = deskControlRequestGeneration
        appControlRequest("/workbench/app-control/host/delegate", method: "POST", body: [
            "app_id": "desk", "resource_id": resourceId, "window_id": deskControlWindowId,
            "session_id": sessionId, "ttl_seconds": 900
        ]) { [weak self] result, error in
            guard let self = self else { return }
            guard generation == self.deskControlRequestGeneration else {
                if let lease = result?["id"] as? String {
                    self.appControlRequest("/workbench/app-control/host/takeover", method: "POST",
                                           body: ["lease_id": lease, "reason": "resource_changed"]) { _, _ in }
                }
                return
            }
            guard error == nil, let lease = result?["id"] as? String else {
                self.sendAppControlEvent(id: resourceId, type: "error",
                                         error: error ?? "委托失败", sessionId: sessionId)
                return
            }
            self.deskControlResourceId = resourceId
            self.deskControlSessionId = sessionId
            self.appControlLeases[resourceId] = lease
            self.sendAppControlEvent(id: resourceId, type: "delegated", leaseId: lease,
                                     sessionId: sessionId)
            self.ensureAppControlTimer()
        }
    }

    private func revokeDeskControl(reason: String) {
        deskControlRequestGeneration += 1
        let resourceId = deskControlResourceId
        guard !resourceId.isEmpty else { return }
        revokeAppControl(id: resourceId, reason: reason)
        deskControlResourceId = ""
        deskControlSessionId = ""
    }

    func toggleAppControl(id: String, sessionId: String = "") {
        if appControlLeases[id] != nil {
            revokeAppControl(id: id, reason: "user_takeover")
            return
        }
        guard browsers[id] != nil else {
            sendAppControlEvent(id: id, type: "error", error: "页面尚未加载")
            return
        }
        let windowId = "window-\(ObjectIdentifier(window!).hashValue)"
        appControlRequest("/workbench/app-control/host/delegate", method: "POST", body: [
            "app_id": "browser", "resource_id": id, "window_id": windowId,
            "session_id": sessionId, "ttl_seconds": 900
        ]) { [weak self] result, error in
            guard let self = self else { return }
            guard error == nil, let lease = result?["id"] as? String else {
                self.sendAppControlEvent(id: id, type: "error", error: error ?? "委托失败")
                return
            }
            self.appControlLeases[id] = lease
            self.sendAppControlEvent(id: id, type: "delegated", leaseId: lease)
            self.ensureAppControlTimer()
        }
    }

    /// 人碰了被委托的页面:让开,但不收回。
    ///
    /// 同一个 DOM 不能有两个操作者,所以必须立刻停手;但过去这里直接 revoke,用户
    /// 想看一眼有没有生效、点一下页面滚动,租约就永久没了,得从头重新委托
    /// (2026-09-02 用户实见,连续三次)。manifest 声明的本来就是 user_interaction: pause。
    /// 每次输入都刷新最后触碰时刻;静默满 APP_CONTROL_RESUME_QUIET 秒自动恢复。
    private func appControlLease(_ id: String) -> String? {
        appControlLeases[id] ?? appControlAgentLeases[id]
    }

    func pauseAppControl(id: String, reason: String) {
        guard let lease = appControlLease(id) else { return }
        appControlLastTouch[id] = Date()
        // 暂停期间再切标签必须升级为“等待用户切回”，不能被静默计时器抢回。
        if appControlPaused.contains(id) && (reason != "tab_switched" || appControlPauseReason[id] == reason) { return }
        appControlPaused.insert(id)
        appControlPauseReason[id] = reason
        sendAppControlEvent(id: id, type: "paused", leaseId: lease, error: reason)
        appControlRequest("/workbench/app-control/host/pause", method: "POST", body: [
            "lease_id": lease, "reason": reason
        ]) { _, _ in }
        ensureAppControlTimer()
    }

    /// 人停手够久:把控制权还回去。只对自己暂停的那些恢复,用户明确收回的不在其中。
    private func resumeIdleAppControl() {
        let quiet = AppDelegate.APP_CONTROL_RESUME_QUIET
        for id in Array(appControlPaused) {
            guard let lease = appControlLease(id) else { appControlPaused.remove(id); continue }
            // 切走标签那种要等人切回来(见 agentResume):委托的范围是「当前可见标签」,
            // 人正看着别处时不该在藏着的那一页上继续动作。
            guard (appControlPauseReason[id] ?? "user_interaction") == "user_interaction" else { continue }
            let touched = appControlLastTouch[id] ?? Date.distantPast
            guard Date().timeIntervalSince(touched) >= quiet else { continue }
            appControlPaused.remove(id)
            appControlPauseReason.removeValue(forKey: id)
            appControlRequest("/workbench/app-control/host/resume", method: "POST", body: [
                "lease_id": lease, "ttl_seconds": 900
            ]) { [weak self] result, error in
                guard let self = self else { return }
                if error != nil {
                    // 恢复不了(多半是已经被撤或过期):按撤销收口,别让界面停在"暂停"。
                    self.appControlLeases.removeValue(forKey: id)
                    self.appControlAgentLeases.removeValue(forKey: id)
                    self.sendAppControlEvent(id: id, type: "revoked", error: error ?? "")
                    if id == self.deskControlResourceId {
                        self.deskControlResourceId = ""
                        self.deskControlSessionId = ""
                    }
                    return
                }
                self.sendAppControlEvent(id: id, type: "delegated", leaseId: lease)
            }
        }
    }

    /// 人切回被委托的那一页:接着干。与静默自动接回是同一个终点,只是触发不同。
    func resumeAppControl(id: String) {
        guard appControlPaused.contains(id), let lease = appControlLease(id) else { return }
        appControlPaused.remove(id)
        appControlPauseReason.removeValue(forKey: id)
        appControlRequest("/workbench/app-control/host/resume", method: "POST", body: [
            "lease_id": lease, "ttl_seconds": 900
        ]) { [weak self] _, error in
            guard let self = self else { return }
                if error != nil {
                    self.appControlLeases.removeValue(forKey: id)
                    self.appControlAgentLeases.removeValue(forKey: id)
                self.sendAppControlEvent(id: id, type: "revoked", error: error ?? "")
                if id == self.deskControlResourceId {
                    self.deskControlResourceId = ""
                    self.deskControlSessionId = ""
                }
                return
            }
            self.sendAppControlEvent(id: id, type: "delegated", leaseId: lease)
        }
    }

    /// 视图没了,租约不该还活着。Agent 自开页走这条:让调用方拿到「租约已撤,原因是 X」,
    /// 而不是对着一张永远不会好的标签重试。
    func revokeAgentLease(_ id: String, reason: String) {
        guard let lease = appControlAgentLeases.removeValue(forKey: id) else { return }
        browserViewLog(id, "agent_lease_revoked", "reason=\(reason) lease=\(lease)")
        appControlRequest("/workbench/app-control/host/takeover", method: "POST", body: [
            "lease_id": lease, "reason": reason
        ]) { _, _ in }
    }

    func revokeAppControl(id: String, reason: String) {
        appControlPauseReason.removeValue(forKey: id)
        appControlPaused.remove(id)
        appControlLastTouch.removeValue(forKey: id)
        guard let lease = appControlLeases.removeValue(forKey: id) else { return }
        // 原因随事件带给界面:过去只发一个空的 revoked,人看到按钮弹回去却不知道为什么。
        sendAppControlEvent(id: id, type: "revoked", leaseId: lease, error: reason)
        appControlRequest("/workbench/app-control/host/takeover", method: "POST", body: [
            "lease_id": lease, "reason": reason
        ]) { _, _ in }
        if appControlLeases.isEmpty {
            appControlPaused.removeAll()
            appControlPauseReason.removeAll()
            appControlLastTouch.removeAll()
            ensureAppControlTimer()   // 不停表,只降速:自开页那条路随时可能来活
        }
    }

    /// 取活循环。原来只在用户点了「交给 Agent」之后才起,于是 Agent 自开页那条路
    /// 没有起点——它的第一个动作恰恰是开页本身。改成常驻:空闲时 1.5 秒一拍(本机
    /// 一次几百字节的 GET),手上有租约时 0.35 秒一拍。
    private func ensureAppControlTimer() {
        let want = appControlPollInterval(hasLeases: !appControlLeases.isEmpty,
                                          now: ProcessInfo.processInfo.systemUptime,
                                          activeUntil: appControlActiveUntil)
        if appControlTimer != nil && abs(appControlTimerInterval - want) < 0.01 { return }
        appControlTimer?.invalidate()
        appControlTimerInterval = want
        appControlTimer = Timer.scheduledTimer(withTimeInterval: want, repeats: true) {
            [weak self] _ in self?.pollAppControl()
        }
        if let timer = appControlTimer { RunLoop.main.add(timer, forMode: .common) }
    }

    private static let APP_CONTROL_RESUME_QUIET: TimeInterval = 4

    private func pollAppControl() {
        ensureAppControlTimer()   // An expired burst returns to idle without a separate timer.
        resumeIdleAppControl()   // 静默够久就把控制权还回去
        trimBrowserViews()
        guard !appControlPolling else { return }
        appControlPolling = true
        // 全局取活,不按自己那张租约表逐个问。
        //
        // Agent 自开的标签(app-control open)由 Runtime 自签租约,原生这侧在它执行完
        // 之前根本不知道这个资源存在;按 appControlLeases 逐个轮询的话,那条 open 动作
        // 永远没人取,停在 queued——2026-09-02 实测就是这个结果。Runtime 的 host/next
        // 本来就支持不带 resource_id 返回该应用最早的一条,用它即可。
        //
        // 暂停中的标签由 Runtime 侧保证不派活(next_operation 只认 active 租约),
        // 不需要在这里再滤一遍。
        // 待审批动作只在发起它的会话过程区显示。原生壳若也轮询并弹 NSAlert，
        // 同一动作会出现两个入口，而且手机端审批与桌面弹窗会互相竞速。
        // Runtime 的 next_operation 只返回 queued，审批通过前自然取不到。
        let apps = ["browser", "desk"]
        let appId = apps[appControlPollIndex % apps.count]
        appControlPollIndex = (appControlPollIndex + 1) % apps.count
        appControlRequest("/workbench/app-control/host/next?app_id=\(appId)") {
            [weak self] next, nextError in
            guard let self = self else { return }
            self.appControlPolling = false
            if nextError == nil, let operation = next?["operation"] as? [String: Any] {
                self.appControlActiveUntil = ProcessInfo.processInfo.systemUptime + 5
                self.ensureAppControlTimer()
                if appId == "desk" {
                    self.executeDeskAppControl(operation)
                } else {
                    self.executeAppControl(operation)
                }
            }
        }
    }

    private func executeDeskAppControl(_ operation: [String: Any]) {
        guard let operationId = operation["id"] as? String else { return }
        let ticket = operation["dispatch_id"] as? String ?? ""
        let opId = ticket.isEmpty ? operationId : operationId + "." + ticket
        guard let resourceId = operation["resource_id"] as? String,
              let leaseId = operation["lease_id"] as? String,
              let sessionId = operation["session_id"] as? String,
              resourceId == deskControlResourceId,
              leaseId == appControlLeases[resourceId],
              sessionId == deskControlSessionId,
              !appControlPaused.contains(resourceId) else {
            finishAppControl(opId, ok: false, error: "desk_surface_unavailable"); return
        }
        let kind = operation["kind"] as? String ?? ""
        let name = operation["name"] as? String ?? ""
        let interactionEpoch = deskControlRequestGeneration
        if kind == "inspect" && name == "surface" {
            snapshotDeskSurface(opId, operation, interactionEpoch); return
        }
        if kind == "inspect" && name == "screenshot" {
            let input = operation["input"] as? [String: Any] ?? [:]
            snapshotAppControl(opId, resourceId, web, input: input,
                               hideDeskProtectedSurface: true) { [weak self] in
                guard let self = self else { return false }
                return self.deskOperationStillValid(operation, interactionEpoch)
            }
            return
        }
        if kind == "act" && ["open_page", "toggle_panel"].contains(name) {
            runDeskSemanticAction(opId, operation, interactionEpoch); return
        }
        finishAppControl(opId, ok: false, error: "unsupported_desk_operation")
    }

    private func deskOperationStillValid(_ operation: [String: Any], _ interactionEpoch: Int) -> Bool {
        guard let resourceId = operation["resource_id"] as? String,
              let leaseId = operation["lease_id"] as? String,
              let sessionId = operation["session_id"] as? String else { return false }
        return interactionEpoch == deskControlRequestGeneration
            && resourceId == deskControlResourceId
            && leaseId == appControlLeases[resourceId]
            && sessionId == deskControlSessionId
            && !appControlPaused.contains(resourceId)
    }

    private func runDeskSemanticAction(_ operationId: String, _ operation: [String: Any],
                                       _ interactionEpoch: Int) {
        guard deskOperationStillValid(operation, interactionEpoch) else {
            finishAppControl(operationId, ok: false, error: "desk_surface_unavailable"); return
        }
        let payload: [String: Any] = ["action": operation["name"] as? String ?? "",
                                      "session_id": deskControlSessionId,
                                      "interaction_epoch": interactionEpoch,
                                      "input": operation["input"] as? [String: Any] ?? [:]]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let text = String(data: data, encoding: .utf8) else {
            finishAppControl(operationId, ok: false, error: "bad_desk_action"); return
        }
        web.evaluateJavaScript("window.__deskAgentCommand && window.__deskAgentCommand(\(text))") {
            [weak self] result, error in
            guard let self = self else { return }
            guard self.deskOperationStillValid(operation, interactionEpoch) else {
                self.finishAppControl(operationId, ok: false,
                                      error: "human_intervention_reinspect_required"); return
            }
            guard error == nil, result as? Bool == true else {
                self.finishAppControl(operationId, ok: false,
                                      error: error?.localizedDescription ?? "desk_action_rejected"); return
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                guard self.deskOperationStillValid(operation, interactionEpoch) else {
                    self.finishAppControl(operationId, ok: false,
                                          error: "human_intervention_reinspect_required"); return
                }
                self.snapshotDeskSurface(operationId, operation, interactionEpoch)
            }
        }
    }

    private func snapshotDeskSurface(_ operationId: String, _ operation: [String: Any],
                                     _ interactionEpoch: Int) {
        guard deskOperationStillValid(operation, interactionEpoch) else {
            finishAppControl(operationId, ok: false, error: "human_intervention_reinspect_required")
            return
        }
        guard #available(macOS 11.0, *) else {
            finishAppControl(operationId, ok: false, error: "macOS 11+ required"); return
        }
        let script = #"""
        const clean = value => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 120);
        const visible = node => {
          const style = getComputedStyle(node), box = node.getBoundingClientRect();
          return !node.hidden && style.display !== 'none' && style.visibility !== 'hidden'
            && box.width > 0 && box.height > 0;
        };
        const nodes = Array.from(document.querySelectorAll(
          '[data-app-control-surface-id=\"navigation\"][data-app-control-item]'));
        const items = nodes.filter(node => visible(node) && !node.closest('[data-app-control-protected]'))
          .map(node => ({id: clean(node.getAttribute('data-app-control-item')),
            label: clean(node.getAttribute('data-app-control-label')),
            role: node.getAttribute('role') || (node.tagName === 'BUTTON' ? 'button' : 'link'),
            enabled: !node.disabled && node.getAttribute('aria-disabled') !== 'true',
            selected: node.getAttribute('aria-current') === 'page' || node.classList.contains('active')}));
        const sidebarOpen = !document.body.classList.contains('side-collapsed');
        const stable = JSON.stringify({items, sidebarOpen});
        let hash = 2166136261;
        for (let i = 0; i < stable.length; i += 1) { hash ^= stable.charCodeAt(i); hash = Math.imul(hash, 16777619); }
        return {ok: true, output: {schema: 'desk.surface/v1', route: 'workspace',
          surface: 'navigation', title: 'Desktron', items, modal_stack: [],
          sidebar_open: sidebarOpen,
          capabilities: {inspect: true, actions: ['open_page', 'toggle_panel']}},
          observation_version: 'navigation:' + (hash >>> 0).toString(16)};
        """#
        web.callAsyncJavaScript(script, arguments: [:], in: nil, in: appControlWorld) { [weak self] result in
            DispatchQueue.main.async {
                guard let self = self else { return }
                guard self.deskOperationStillValid(operation, interactionEpoch) else {
                    self.finishAppControl(operationId, ok: false,
                                          error: "human_intervention_reinspect_required"); return
                }
                switch result {
                case .success(let value):
                    guard let row = value as? [String: Any] else {
                        self.finishAppControl(operationId, ok: false, error: "invalid_desk_snapshot"); return
                    }
                    self.finishAppControl(operationId, ok: row["ok"] as? Bool == true,
                                          output: row["output"],
                                          error: row["error"] as? String ?? "",
                                          observationVersion: row["observation_version"] as? String ?? "")
                case .failure(let error):
                    self.finishAppControl(operationId, ok: false, error: error.localizedDescription)
                }
            }
        }
    }

    private func executeAppControl(_ operation: [String: Any]) {
        guard let operationId = operation["id"] as? String,
              let resourceId = operation["resource_id"] as? String else { return }
        // 等待探测的执行句柄带本轮票据；闭包保留旧票据，迟到结果不能完成下一轮。
        let ticket = operation["dispatch_id"] as? String ?? ""
        let opId = ticket.isEmpty ? operationId : operationId + "." + ticket
        if appControlLeases[resourceId] == nil, let lease = operation["lease_id"] as? String, !lease.isEmpty {
            appControlAgentLeases[resourceId] = lease
        }
        // open 要在视图存在之前就放行——它就是去建那个视图的。其余动作仍要求视图已在。
        if (operation["kind"] as? String) == "act" && (operation["name"] as? String) == "open" {
            openAgentTab(opId, resourceId, operation["input"] as? [String: Any] ?? [:]); return
        }
        guard let view = browsers[resourceId] else {
            browserViewLog(resourceId, "miss", "op=\(operation["name"] as? String ?? "")")
            finishAppControl(opId, ok: false, error: "browser_tab_unavailable")
            return
        }
        browserLastUsed[resourceId] = Date()
        let kind = operation["kind"] as? String ?? ""
        let name = operation["name"] as? String ?? ""
        let input = operation["input"] as? [String: Any] ?? [:]
        if kind == "inspect" && name == "screenshot" {
            snapshotAppControl(opId, resourceId, view, input: input); return
        }
        if kind == "act" && name == "navigate" {
            let raw = input["url"] as? String ?? ""
            guard let url = URL(string: raw), url.scheme == "http" || url.scheme == "https" else {
                finishAppControl(opId, ok: false, error: "only_http_https_navigation_allowed"); return
            }
            let epoch = (appControlEpochs[resourceId] ?? 0) + 1
            appControlEpochs[resourceId] = epoch   // load 提交前先失效旧节点；didCommit 会再进入新文档 epoch
            view.load(URLRequest(url: url))
            finishAppControl(opId, ok: true, output: ["url": raw],
                             observationVersion: "\(epoch):navigation"); return
        }
        if kind == "act" && ["back", "forward", "reload"].contains(name) {
            if name == "back" && !view.canGoBack {
                finishAppControl(opId, ok: false, error: "no_back_history"); return
            }
            if name == "forward" && !view.canGoForward {
                finishAppControl(opId, ok: false, error: "no_forward_history"); return
            }
            let epoch = (appControlEpochs[resourceId] ?? 0) + 1
            appControlEpochs[resourceId] = epoch
            if name == "back" { view.goBack() }
            else if name == "forward" { view.goForward() }
            else { view.reload() }
            finishAppControl(opId, ok: true,
                             output: ["navigation": name, "url": view.url?.absoluteString ?? ""],
                             observationVersion: "\(epoch):navigation"); return
        }
        if kind == "act" && name == "close" {
            // 先形成最终回执，再销毁标签。反过来 close 会先撤租约并把仍在 running 的
            // 自己取消，调用方拿到的是 cancelled，无法区分关闭失败还是成功。
            finishAppControl(opId, ok: true, output: ["tab": resourceId, "action": name])
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
                self?.runBrowserAgentCommand(name, resourceId: resourceId) { _ in }
            }
            return
        }
        if kind == "act" && name == "activate" {
            runBrowserAgentCommand(name, resourceId: resourceId) { [weak self] ok in
                guard let self = self else { return }
                self.finishAppControl(opId, ok: ok,
                                      output: ok ? ["tab": resourceId, "action": name,
                                                    "presentation": "background", "user_tab_unchanged": true] : nil,
                                      error: ok ? "" : "browser_tab_command_failed")
            }
            return
        }
        if kind == "act" && name == "handoff" {
            // 用户批准后显示租约绑定的原标签；回执先落库，再收回控制权，避免 takeover
            // 把仍在 running 的 handoff 自己取消。URL 不参与恢复，草稿仍留在同一 WKWebView。
            runBrowserAgentCommand(name, resourceId: resourceId) { [weak self] ok in
                guard let self = self else { return }
                guard ok else {
                    self.finishAppControl(opId, ok: false, error: "browser_tab_command_failed"); return
                }
                self.finishAppControl(opId, ok: true,
                                      output: ["resource_id": resourceId, "action": name,
                                               "presentation": "user_foreground", "same_tab": true,
                                               "requires_reinspect": true]) { stored in
                    guard stored else { return }
                    self.runBrowserAgentCommand("takeover", resourceId: resourceId) { _ in }
                }
            }
            return
        }
        // click/coordinate_click/hover 走宿主鼠标事件。页面收到的是 isTrusted=true，原生控件、
        // 命中测试与默认行为都和真人一致；页面脚本只负责等待目标可操作并给出安全坐标。
        if #available(macOS 11.0, *), kind == "act",
           ["click", "coordinate_click", "hover", "dblclick", "check", "uncheck"].contains(name) {
            runNativePointer(opId, resourceId, view, name: name, input: input); return
        }
        if #available(macOS 11.0, *), kind == "act", name == "drag" {
            runNativeDrag(opId, resourceId, view, input: input); return
        }
        // press/type 走真实按键:合成 KeyboardEvent 页面看得出是假的(isTrusted=false),表单默认
        // 提交、Tab 焦点移动、输入法与快捷键都不跟着走。宿主先让页面把焦点放到目标上,再经
        // WKWebView 的 responder 链送 NSEvent,最后回页面里看变化。映射不了的键(功能键之类)
        // 仍走页面内合成事件那条老路。
        if #available(macOS 11.0, *), kind == "act", name == "press" || name == "type",
           let keys = nativeKeyEvents(name: name, input: input) {
            runNativeInput(opId, resourceId, view, input: input, keys: keys); return
        }
        let epoch = appControlEpochs[resourceId] ?? 0
        let command: [String: Any] = ["kind": kind, "name": name, "input": input]
        if #available(macOS 11.0, *) {
            let runAdapter: (Bool?) -> Void = { [weak self] settled in
                guard let self = self else { return }
                view.callAsyncJavaScript(self.appControlDOMScript(),
                                         arguments: ["command": command, "epoch": epoch],
                                         in: nil, in: self.appControlWorld) { [weak self] result in
                    DispatchQueue.main.async {
                        guard let self = self else { return }
                        switch result {
                        case .success(let value):
                            guard let row = value as? [String: Any] else {
                                self.finishAppControl(opId, ok: false, error: "invalid_adapter_result"); return
                            }
                            var out = row["output"]
                            if kind == "inspect", name == "document", var d = out as? [String: Any] {
                                d["resource_id"] = resourceId
                                d["view_hidden"] = view.isHidden
                                d["presentation"] = view.isHidden ? "background" : "visible"
                                out = d
                            }
                            if let settled = settled, var d = out as? [String: Any] {
                                d["settled"] = settled   // 读早了要看得见,不能变成一次安静的误判
                                out = d
                            }
                            self.finishAppControl(opId, ok: (row["ok"] as? Bool) == true,
                                                  output: out,
                                                  error: row["error"] as? String ?? "",
                                                  observationVersion: row["observation_version"] as? String ?? "")
                        case .failure(let error):
                            self.finishAppControl(opId, ok: false,
                                error: name == "wait" && view.isLoading ? "wait_context_changed" : error.localizedDescription)
                        }
                    }
                }
            }
            // 只有 inspect 等页面安定:act 指名道姓要一个元素,找不到本来就该如实报错。
            let budget = min(max((input["settle_ms"] as? Double ?? 3000) / 1000, 0), 10)
            if kind == "inspect" && budget > 0 {
                waitForPageSettled(view, budget: budget) { runAdapter($0) }
            } else {
                runAdapter(nil)
            }
        } else {
            finishAppControl(opId, ok: false, error: "macOS 11+ required")
        }
    }

    /// 重前端应用在 load 之后还会接着挂节点,inspect 直接读会拿到一份半成品——2026-09-03
    /// 另一会话在一个云控制台只读到三个节点,而它下一步要照这份结果填表并提交。
    ///
    /// 这里等到文档 complete 且节点数连着两次相同再读。等不到就照读,但把 settled=false
    /// 一并交给调用方:宁可它知道自己读早了,也不要一份看着正常的残缺快照。settle_ms 可关。
    @available(macOS 11.0, *)
    private func waitForPageSettled(_ view: WKWebView, budget: TimeInterval,
                                    done: @escaping (Bool) -> Void) {
        let deadline = Date().addingTimeInterval(budget)
        var last = -1
        func step() {
            if Date() >= deadline { done(false); return }
            let js = "document.readyState==='complete'?document.getElementsByTagName('*').length:-1"
            view.evaluateJavaScript(js, in: nil, in: appControlWorld) { result in
                var n = -1
                if case .success(let value) = result { n = (value as? Int) ?? -1 }
                if n >= 0 && n == last { done(true); return }
                last = n
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { step() }
            }
        }
        step()
    }

    /// Agent 自开标签:让前端把它加进标签条,再由前端走既有的 open 通道建视图。
    ///
    /// 不在原生直接 openBrowser:标签模型(顺序、标题、持久化、关闭)全在前端,
    /// 原生自己建一个视图会得到一个界面上不存在、用户关不掉的孤儿标签。
    private func openAgentTab(_ operationId: String, _ resourceId: String, _ input: [String: Any]) {
        let url = input["url"] as? String ?? ""
        let isolated = input["isolated"] as? Bool ?? true
        guard let u = URL(string: url), u.scheme == "http" || u.scheme == "https" else {
            finishAppControl(operationId, ok: false, error: "only_http_https_allowed"); return
        }
        let allowedHosts = input["allowed_main_frame_hosts"] as? [String] ?? []
        let payload: [String: Any] = ["id": resourceId, "url": url, "isolated": isolated,
                                      "allowedMainFrameHosts": allowedHosts]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let text = String(data: data, encoding: .utf8) else {
            finishAppControl(operationId, ok: false, error: "bad_payload"); return
        }
        web.evaluateJavaScript("window.__browserAgentOpen && window.__browserAgentOpen(\(text))") {
            [weak self] result, error in
            guard let self = self else { return }
            if let error = error {
                self.finishAppControl(operationId, ok: false, error: error.localizedDescription)
                return
            }
            if (result as? Bool) != true {
                self.finishAppControl(operationId, ok: false, error: "browser_app_not_open")
                return
            }
            // 等视图真的建出来再报成功。
            //
            // 前端建的是标签模型,原生视图要等浏览器应用可见、布局出非零尺寸之后才创建。
            // 原来这里 JS 一返回 true 就报 succeeded,于是出现「open 成功但随后 inspect 一律
            // browser_tab_unavailable」——报成功报早了,而这种「成了却用不了」最难查
            // (2026-09-03 另一会话连撞三次)。等不到就如实失败,别让调用方以为拿到了页面。
            self.waitForBrowserView(resourceId, tries: 40) { ok in
                guard ok else {
                    self.finishAppControl(operationId, ok: false, error: "browser_view_not_created")
                    return
                }
                self.appControlEpochs[resourceId] = 1
                self.finishAppControl(operationId, ok: true,
                                      output: ["url": url, "isolated": isolated,
                                               "presentation": "background", "user_tab_unchanged": true],
                                      observationVersion: "1:open")
            }
        }
    }

    /// 轮询等这张标签的原生视图出现。每 150ms 一次,默认最多六秒。
    /// 不用通知回调:视图由前端的同步流程创建,那条路上没有可挂的钩子,而这里只需要
    /// 一个「到了没有」的答案。
    private func waitForBrowserView(_ id: String, tries: Int, done: @escaping (Bool) -> Void) {
        if browsers[id] != nil { done(true); return }
        guard tries > 0 else { done(false); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { [weak self] in
            guard let self = self else { done(false); return }
            self.waitForBrowserView(id, tries: tries - 1, done: done)
        }
    }

    @available(macOS 11.0, *)
    private func runNativeInput(_ opId: String, _ resourceId: String, _ view: WKWebView,
                                input: [String: Any], keys: [(NSEvent, NSEvent)]) {
        let epoch = appControlEpochs[resourceId] ?? 0
        let touchAtStart = appControlLastTouch[resourceId]
        let focus: [String: Any] = ["kind": "act", "name": "focus", "input": input]
        view.callAsyncJavaScript(appControlDOMScript(), arguments: ["command": focus, "epoch": epoch],
                                 in: nil, in: appControlWorld) { [weak self] result in
            DispatchQueue.main.async {
                guard let self = self else { return }
                var row: [String: Any] = [:]
                if case .success(let value) = result { row = value as? [String: Any] ?? [:] }
                guard (row["ok"] as? Bool) == true else {
                    self.finishAppControl(opId, ok: false, output: row["output"],
                                          error: row["error"] as? String ?? "focus_failed",
                                          observationVersion: row["observation_version"] as? String ?? "")
                    return
                }
                guard !self.appControlPaused.contains(resourceId), self.appControlLease(resourceId) != nil,
                      self.appControlLastTouch[resourceId] == touchAtStart else {
                    self.finishAppControl(opId, ok: false, output: ["action_executed": false],
                                          error: "human_intervention_reinspect_required"); return
                }
                self.appControlNativeInputUntil[resourceId] = Date().addingTimeInterval(1.0)
                for (down, up) in keys { view.keyDown(with: down); view.keyUp(with: up) }
                let before = row["output"] as? [String: Any] ?? [:]
                let settle: [String: Any] = ["kind": "act", "name": "settle",
                    "input": ["before_rev": before["rev"] ?? 0, "before_url": before["url"] ?? ""]]
                view.callAsyncJavaScript(self.appControlDOMScript(),
                                         arguments: ["command": settle, "epoch": epoch],
                                         in: nil, in: self.appControlWorld) { [weak self] result in
                    DispatchQueue.main.async {
                        guard let self = self else { return }
                        var out: [String: Any] = [:]
                        if case .success(let value) = result { out = value as? [String: Any] ?? [:] }
                        self.finishAppControl(opId, ok: (out["ok"] as? Bool) == true, output: out["output"],
                                              error: out["error"] as? String ?? (out.isEmpty ? "settle_failed" : ""),
                                              observationVersion: out["observation_version"] as? String ?? "")
                    }
                }
            }
        }
    }

    /// 页面适配器先按 locator/node/selector 等待唯一且可操作的目标，再由 WKWebView 的
    /// responder 链发送鼠标事件。这样 React/Vue 原生事件、链接默认导航和 CSS hover 都能命中。
    @available(macOS 11.0, *)
    private func runNativePointer(_ opId: String, _ resourceId: String, _ view: WKWebView,
                                  name: String, input: [String: Any]) {
        let epoch = appControlEpochs[resourceId] ?? 0
        let touchAtStart = appControlLastTouch[resourceId]
        var pointerInput = input
        pointerInput["pointer_action"] = name
        let prepare: [String: Any] = ["kind": "act", "name": "prepare_pointer", "input": pointerInput]
        view.callAsyncJavaScript(appControlDOMScript(), arguments: ["command": prepare, "epoch": epoch],
                                 in: nil, in: appControlWorld) { [weak self] result in
            DispatchQueue.main.async {
                guard let self = self else { return }
                var row: [String: Any] = [:]
                if case .success(let value) = result { row = value as? [String: Any] ?? [:] }
                guard (row["ok"] as? Bool) == true,
                      let before = row["output"] as? [String: Any] else {
                    self.finishAppControl(opId, ok: false, output: row["output"],
                                          error: row["error"] as? String ?? "pointer_target_failed",
                                          observationVersion: row["observation_version"] as? String ?? "")
                    return
                }
                if (before["skip"] as? Bool) == true {
                    self.finishAppControl(opId, ok: true, output: before,
                                          observationVersion: row["observation_version"] as? String ?? "")
                    return
                }
                guard let x = (before["x"] as? NSNumber)?.doubleValue,
                      let y = (before["y"] as? NSNumber)?.doubleValue else {
                    self.finishAppControl(opId, ok: false, error: "pointer_coordinates_missing",
                                          observationVersion: row["observation_version"] as? String ?? "")
                    return
                }
                guard !self.appControlPaused.contains(resourceId), self.appControlLease(resourceId) != nil,
                      self.appControlLastTouch[resourceId] == touchAtStart else {
                    self.finishAppControl(opId, ok: false, output: ["action_executed": false],
                                          error: "human_intervention_reinspect_required"); return
                }
                // CSS 原点在左上；WKWebView 通常已是 flipped，不能再次翻转 Y。
                let local = NSPoint(x: x, y: view.isFlipped ? y : Double(view.bounds.height) - y)
                let location = view.convert(local, to: nil)
                let ts = ProcessInfo.processInfo.systemUptime
                let win = view.window?.windowNumber ?? 0
                func mouse(_ type: NSEvent.EventType, clicks: Int = 0, pressure: Float = 0) -> NSEvent? {
                    NSEvent.mouseEvent(with: type, location: location, modifierFlags: [], timestamp: ts,
                                       windowNumber: win, context: nil, eventNumber: 0,
                                       clickCount: clicks, pressure: pressure)
                }
                self.appControlNativeInputUntil[resourceId] = Date().addingTimeInterval(1.0)
                guard let moved = mouse(.mouseMoved) else {
                    self.finishAppControl(opId, ok: false, error: "native_pointer_event_failed"); return
                }
                view.mouseMoved(with: moved)
                if name != "hover" {
                    let count = name == "dblclick" ? 2 : 1
                    for click in 1...count {
                        guard let down = mouse(.leftMouseDown, clicks: click, pressure: 1),
                              let up = mouse(.leftMouseUp, clicks: click) else {
                            self.finishAppControl(opId, ok: false, error: "native_pointer_event_failed"); return
                        }
                        view.mouseDown(with: down); view.mouseUp(with: up)
                    }
                }
                var settleInput = input
                settleInput["before_rev"] = before["rev"] ?? 0
                settleInput["before_url"] = before["url"] ?? ""
                settleInput["input_path"] = "native_pointer"
                settleInput["action"] = name
                settleInput["diagnostic"] = ["target": before["target"] ?? "",
                    "dom": before["diagnostic"] ?? [:], "css_x": x, "css_y": y,
                    "window_x": location.x, "window_y": location.y,
                    "view_hidden": view.isHidden, "window_key": view.window?.isKeyWindow ?? false]
                settleInput["approval_revalidated"] = before["approval_revalidated"] ?? false
                if name == "check" || name == "uncheck" { settleInput["desired_checked"] = name == "check" }
                let pointerEvidence: [String: Any] = ["action_executed": true, "delivery": "dispatched",
                    "effect_verified": false, "outcome": "delivery_only",
                    "diagnostic": settleInput["diagnostic"] ?? [:],
                    "approval_revalidated": before["approval_revalidated"] ?? false,
                    "requires_reinspect": true, "next_step": "inspect_or_wait_for_postcondition"]
                let settle: [String: Any] = ["kind": "act", "name": "settle",
                    "input": settleInput]
                view.callAsyncJavaScript(self.appControlDOMScript(),
                                         arguments: ["command": settle, "epoch": epoch],
                                         in: nil, in: self.appControlWorld) { [weak self] result in
                    DispatchQueue.main.async {
                        guard let self = self else { return }
                        var out: [String: Any] = [:]
                        if case .success(let value) = result { out = value as? [String: Any] ?? [:] }
                        var evidence = pointerEvidence
                        for (key, value) in out["output"] as? [String: Any] ?? [:] { evidence[key] = value }
                        self.finishAppControl(opId, ok: (out["ok"] as? Bool) == true, output: evidence,
                                              error: out["error"] as? String ?? (out.isEmpty ? "settle_failed" : ""),
                                              observationVersion: out["observation_version"] as? String ?? "")
                    }
                }
            }
        }
    }

    /// dragTo 对齐：源和目标都先通过同一套 Locator/可操作性等待，再发送一串真实拖拽事件。
    @available(macOS 11.0, *)
    private func runNativeDrag(_ opId: String, _ resourceId: String, _ view: WKWebView,
                               input: [String: Any]) {
        let epoch = appControlEpochs[resourceId] ?? 0
        let touchAtStart = appControlLastTouch[resourceId]
        let prepare: [String: Any] = ["kind": "act", "name": "prepare_drag", "input": input]
        view.callAsyncJavaScript(appControlDOMScript(), arguments: ["command": prepare, "epoch": epoch],
                                 in: nil, in: appControlWorld) { [weak self] result in
            DispatchQueue.main.async {
                guard let self = self else { return }
                var row: [String: Any] = [:]
                if case .success(let value) = result { row = value as? [String: Any] ?? [:] }
                guard (row["ok"] as? Bool) == true,
                      let before = row["output"] as? [String: Any],
                      let sx = (before["source_x"] as? NSNumber)?.doubleValue,
                      let sy = (before["source_y"] as? NSNumber)?.doubleValue,
                      let tx = (before["target_x"] as? NSNumber)?.doubleValue,
                      let ty = (before["target_y"] as? NSNumber)?.doubleValue else {
                    self.finishAppControl(opId, ok: false, output: row["output"],
                                          error: row["error"] as? String ?? "drag_target_failed",
                                          observationVersion: row["observation_version"] as? String ?? "")
                    return
                }
                guard !self.appControlPaused.contains(resourceId), self.appControlLease(resourceId) != nil,
                      self.appControlLastTouch[resourceId] == touchAtStart else {
                    self.finishAppControl(opId, ok: false, output: ["action_executed": false],
                                          error: "human_intervention_reinspect_required"); return
                }
                let win = view.window?.windowNumber ?? 0
                let started = ProcessInfo.processInfo.systemUptime
                func event(_ type: NSEvent.EventType, _ x: Double, _ y: Double,
                           _ step: Int, pressure: Float = 0) -> NSEvent? {
                    let local = NSPoint(x: x, y: view.isFlipped ? y : Double(view.bounds.height) - y)
                    return NSEvent.mouseEvent(with: type, location: view.convert(local, to: nil),
                                              modifierFlags: [], timestamp: started + Double(step) * 0.01,
                                              windowNumber: win, context: nil, eventNumber: step,
                                              clickCount: 1, pressure: pressure)
                }
                self.appControlNativeInputUntil[resourceId] = Date().addingTimeInterval(2.0)
                guard let moved = event(.mouseMoved, sx, sy, 0),
                      let down = event(.leftMouseDown, sx, sy, 1, pressure: 1) else {
                    self.finishAppControl(opId, ok: false, error: "native_drag_event_failed"); return
                }
                view.mouseMoved(with: moved); view.mouseDown(with: down)
                for step in 1...10 {
                    let ratio = Double(step) / 10.0
                    let x = sx + (tx - sx) * ratio, y = sy + (ty - sy) * ratio
                    guard let dragged = event(.leftMouseDragged, x, y, step + 1, pressure: 1) else {
                        self.finishAppControl(opId, ok: false, error: "native_drag_event_failed"); return
                    }
                    view.mouseDragged(with: dragged)
                }
                guard let up = event(.leftMouseUp, tx, ty, 12) else {
                    self.finishAppControl(opId, ok: false, error: "native_drag_event_failed"); return
                }
                view.mouseUp(with: up)
                let settle: [String: Any] = ["kind": "act", "name": "settle", "input": [
                    "before_rev": before["rev"] ?? 0, "before_url": before["url"] ?? "",
                    "input_path": "native_drag", "action": "drag"]]
                view.callAsyncJavaScript(self.appControlDOMScript(),
                                         arguments: ["command": settle, "epoch": epoch],
                                         in: nil, in: self.appControlWorld) { [weak self] result in
                    DispatchQueue.main.async {
                        guard let self = self else { return }
                        var out: [String: Any] = [:]
                        if case .success(let value) = result { out = value as? [String: Any] ?? [:] }
                        self.finishAppControl(opId, ok: (out["ok"] as? Bool) == true, output: out["output"],
                                              error: out["error"] as? String ?? (out.isEmpty ? "settle_failed" : ""),
                                              observationVersion: out["observation_version"] as? String ?? "")
                    }
                }
            }
        }
    }

    /// 把 press 的 key / type 的 text 翻成 NSEvent 对。映射不了就返回 nil,由页面内合成事件兜底。
    /// 组合键写成 "Shift+Tab" / "Meta+Enter";单个可打印字符直接按字符送。
    private func nativeKeyEvents(name: String, input: [String: Any]) -> [(NSEvent, NSEvent)]? {
        if name == "type" {
            let text = input["text"] as? String ?? ""
            if text.isEmpty { return nil }
            var events: [(NSEvent, NSEvent)] = []
            for ch in text {
                let chars = String(ch)
                let pair = chars == "\n" ? nativeKeyPair(chars: "\r", code: 36, flags: [])
                                        : nativeKeyPair(chars: chars, code: 0, flags: [])
                guard let p = pair else { return nil }
                events.append(p)
            }
            return events
        }
        var parts = (input["key"] as? String ?? "").split(separator: "+").map(String.init)
        guard let last = parts.popLast(), !last.isEmpty else { return nil }
        var flags: NSEvent.ModifierFlags = []
        for m in parts {
            switch m.lowercased() {
            case "shift": flags.insert(.shift)
            case "control", "ctrl": flags.insert(.control)
            case "alt", "option": flags.insert(.option)
            case "meta", "cmd", "command": flags.insert(.command)
            default: return nil
            }
        }
        let special: [String: (UInt16, String)] = [
            "enter": (36, "\r"), "return": (36, "\r"), "tab": (48, "\t"),
            "escape": (53, "\u{1B}"), "esc": (53, "\u{1B}"), "space": (49, " "), " ": (49, " "),
            "backspace": (51, "\u{7F}"), "delete": (117, "\u{F728}"),
            "arrowleft": (123, "\u{F702}"), "arrowright": (124, "\u{F703}"),
            "arrowdown": (125, "\u{F701}"), "arrowup": (126, "\u{F700}"),
            "home": (115, "\u{F729}"), "end": (119, "\u{F72B}"),
            "pageup": (116, "\u{F72C}"), "pagedown": (121, "\u{F72D}"),
        ]
        if let (code, chars) = special[last.lowercased()] {
            guard let pair = nativeKeyPair(chars: chars, code: code, flags: flags) else { return nil }
            return [pair]
        }
        if last.count == 1 {
            guard let pair = nativeKeyPair(chars: last, code: 0, flags: flags) else { return nil }
            return [pair]
        }
        return nil
    }

    private func nativeKeyPair(chars: String, code: UInt16,
                               flags: NSEvent.ModifierFlags) -> (NSEvent, NSEvent)? {
        let ts = ProcessInfo.processInfo.systemUptime
        let win = window?.windowNumber ?? 0
        guard let down = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags,
                                          timestamp: ts, windowNumber: win, context: nil,
                                          characters: chars, charactersIgnoringModifiers: chars,
                                          isARepeat: false, keyCode: code),
              let up = NSEvent.keyEvent(with: .keyUp, location: .zero, modifierFlags: flags,
                                        timestamp: ts, windowNumber: win, context: nil,
                                        characters: chars, charactersIgnoringModifiers: chars,
                                        isARepeat: false, keyCode: code)
        else { return nil }
        return (down, up)
    }

    /// 标签模型归前端所有，激活/关闭必须回到同一事实源执行；原生只持有 WKWebView，
    /// 直接改它会留下一个标签条不知道的孤儿状态。
    private func runBrowserAgentCommand(_ action: String, resourceId: String,
                                        done: @escaping (Bool) -> Void) {
        let payload: [String: Any] = ["action": action, "id": resourceId]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let text = String(data: data, encoding: .utf8) else { done(false); return }
        web.evaluateJavaScript("window.__browserAgentCommand && window.__browserAgentCommand(\(text))") {
            result, error in done(error == nil && (result as? Bool) == true)
        }
    }

    private func finishAppControl(_ operationId: String, ok: Bool, output: Any? = nil,
                                  error: String = "", observationVersion: String = "",
                                  completion: ((Bool) -> Void)? = nil) {
        let handle = operationId.split(separator: ".", maxSplits: 1).map(String.init)
        var body: [String: Any] = ["operation_id": handle[0], "ok": ok,
                                   "error": error, "observation_version": observationVersion]
        if handle.count == 2 { body["dispatch_id"] = handle[1] }
        if let output = output { body["output"] = output }
        appControlRequest("/workbench/app-control/host/result", method: "POST", body: body) { _, error in
            completion?(error == nil)
        }
    }

    private func snapshotAppControl(_ operationId: String, _ resourceId: String, _ view: WKWebView,
                                    input: [String: Any], hideDeskProtectedSurface: Bool = false,
                                    stillValid: (() -> Bool)? = nil) {
        guard stillValid?() ?? true else {
            finishAppControl(operationId, ok: false, error: "human_intervention_reinspect_required")
            return
        }
        let protectedRule = hideDeskProtectedSurface
            ? "[data-app-control-protected]{visibility:hidden!important}" : ""
        let mask = #"""
        (() => {
          let style = document.getElementById('__desk_agent_snapshot_mask');
          if (!style) {
            style = document.createElement('style');
            style.id = '__desk_agent_snapshot_mask';
            style.textContent = "input[type=password],input[autocomplete*=password],input[name*=token i],input[name*=secret i],[data-sensitive],[data-private]{color:transparent!important;text-shadow:0 0 10px #777!important;background:#777!important}" + \#(String(reflecting: protectedRule));
            document.documentElement.appendChild(style);
          }
          const root = document.documentElement, body = document.body;
          return {content_width: Math.max(root?.scrollWidth || 0, body?.scrollWidth || 0, innerWidth),
            content_height: Math.max(root?.scrollHeight || 0, body?.scrollHeight || 0, innerHeight)};
        })()
        """#
        let unmask = "document.getElementById('__desk_agent_snapshot_mask')?.remove()"
        view.evaluateJavaScript(mask) { [weak self] result, _ in
            guard let self = self else { return }
            guard stillValid?() ?? true else {
                view.evaluateJavaScript(unmask, completionHandler: nil)
                self.finishAppControl(operationId, ok: false,
                                      error: "human_intervention_reinspect_required")
                return
            }
            let config = WKSnapshotConfiguration()
            var fullPage = input["full_page"] as? Bool == true
            var truncated = false
            if let clip = input["clip"] as? [String: Any] {
                let x = (clip["x"] as? NSNumber)?.doubleValue ?? -1
                let y = (clip["y"] as? NSNumber)?.doubleValue ?? -1
                let width = (clip["width"] as? NSNumber)?.doubleValue ?? 0
                let height = (clip["height"] as? NSNumber)?.doubleValue ?? 0
                let rect = CGRect(x: x, y: y, width: width, height: height)
                guard x >= 0, y >= 0, width >= 1, height >= 1,
                      rect.maxX <= view.bounds.width, rect.maxY <= view.bounds.height else {
                    view.evaluateJavaScript(unmask, completionHandler: nil)
                    self.finishAppControl(operationId, ok: false, error: "screenshot_clip_outside_viewport")
                    return
                }
                config.rect = rect
                fullPage = false
            } else if fullPage {
                let metrics = result as? [String: Any] ?? [:]
                let width = min(12000, max(1, (metrics["content_width"] as? NSNumber)?.doubleValue ?? view.bounds.width))
                var height = min(12000, max(1, (metrics["content_height"] as? NSNumber)?.doubleValue ?? view.bounds.height))
                // 与 Runtime 的 20MB 证据上限同向约束；按最坏 RGBA 控制快照分配。
                let maxPixels = 20_000_000.0
                if width * height > maxPixels {
                    height = max(1, floor(maxPixels / width)); truncated = true
                }
                config.rect = CGRect(x: 0, y: 0, width: width, height: height)
            }
            view.takeSnapshot(with: config) { image, error in
                view.evaluateJavaScript(unmask, completionHandler: nil)
                guard stillValid?() ?? true else {
                    self.finishAppControl(operationId, ok: false,
                                          error: "human_intervention_reinspect_required")
                    return
                }
                guard error == nil, let image = image, let tiff = image.tiffRepresentation,
                      let bitmap = NSBitmapImageRep(data: tiff),
                      let png = bitmap.representation(using: .png, properties: [:]) else {
                    self.finishAppControl(operationId, ok: false,
                                          error: error?.localizedDescription ?? "snapshot_failed")
                    return
                }
                let epoch = self.appControlEpochs[resourceId] ?? 0
                self.finishAppControl(operationId, ok: true, output: [
                    "mime_type": "image/png", "data": png.base64EncodedString(),
                    "width": image.size.width, "height": image.size.height,
                    "full_page": fullPage, "truncated": truncated
                ], observationVersion: "\(epoch):screenshot:\(Int(Date().timeIntervalSince1970 * 1000))")
            }
        }
    }

    private func appControlDOMScript() -> String {
        return #"""
        const state = globalThis.__deskAgentControl || (globalThis.__deskAgentControl = (() => {
          const state = {seq: 0, rev: 1, nodes: new Map(), ids: new WeakMap(), observed: new WeakSet()};
          return state;
        })());
        const norm = (value, max = 500) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, max);
        // 两条脱敏正则是唯一的密钥剔除口径，单行与保留换行两条清洗路径共用同一份。
        const scrub = (value) => String(value)
          .replace(/\b(?:sk|pk|api|token)[-_][A-Za-z0-9_-]{16,}\b/gi, '[redacted]')
          .replace(/\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]{10,})?\b/g, '[redacted]');
        const redact = (value, max = 20000) => scrub(norm(value, max));
        const safeHref = (el) => {
          const raw = el?.getAttribute?.('href');
          if (!raw) return '';
          try {
            const u = new URL(String(raw), el.ownerDocument?.baseURI || location.href);
            if (u.protocol !== 'http:' && u.protocol !== 'https:') return '';
            if (u.username || u.password) return '';
            u.hash = '';
            const sensitiveKey = /^(?:access_token|api_key|auth|authorization|code|credential|id_token|jwt|key|oauth_token|password|refresh_token|secret|session|sessionid|signature|sig|state|ticket|token)$/i;
            if (Array.from(u.searchParams.keys()).some((key) => sensitiveKey.test(key))) return '';
            return u.toString().slice(0, 4096);
          } catch (_) { return ''; }
        };
        // 正文单独走保留换行的清洗：innerText 已按排版给出换行，norm 的 \s+ 会把标题和
        // 正文压成一条长串。这里只压合行内空白与连续空行，不改 norm 的语义（它被多处复用）。
        const redactBlock = (value, max = 20000) => scrub(String(value || '')
          .replace(/\r\n?/g, '\n').replace(/[^\S\n]+/g, ' ')
          .replace(/ ?\n ?/g, '\n').replace(/\n{3,}/g, '\n\n').trim().slice(0, max));
        const observe = (root) => {
          if (!root || state.observed.has(root)) return;
          try {
            new MutationObserver(records => {
              if (records.some(r => r.type !== 'attributes'
                || !String(r.attributeName || '').startsWith('data-desk-network-'))) state.rev += 1;
            }).observe(root,
              {subtree: true, childList: true, attributes: true, characterData: true});
            state.observed.add(root);
          } catch (_) {}
        };
        observe(document);
        const deepQuery = (selector, root = document) => {
          const hit = root.querySelector(selector); if (hit) return hit;
          for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) { const shadowHit = deepQuery(selector, el.shadowRoot); if (shadowHit) return shadowHit; }
            if (el.tagName === 'IFRAME') {
              try { const frameHit = deepQuery(selector, el.contentDocument); if (frameHit) return frameHit; } catch (_) {}
            }
          }
          return null;
        };
        const deepQueryAll = (selector, root = document, out = []) => {
          for (const hit of root.querySelectorAll(selector)) out.push(hit);
          for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) deepQueryAll(selector, el.shadowRoot, out);
            if (el.tagName === 'IFRAME') {
              try { if (el.contentDocument) deepQueryAll(selector, el.contentDocument, out); } catch (_) {}
            }
          }
          return [...new Set(out)];
        };
        const rendered = (el) => {
          if (!el || !el.isConnected) return false;
          const r = el.getBoundingClientRect();
          const s = (el.ownerDocument?.defaultView || window).getComputedStyle(el);
          return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
        };
        const inViewport = (el) => {
          if (!rendered(el)) return false;
          const r = absoluteRect(el);
          return r.bottom >= 0 && r.right >= 0 && r.top <= innerHeight && r.left <= innerWidth;
        };
        const sensitive = (el) => {
          const text = [el.type, el.name, el.id, el.autocomplete, el.getAttribute?.('aria-label')].join(' ');
          return el.type === 'password' || /password|passwd|secret|token|cookie|credential|authorization/i.test(text);
        };
        const nodeId = (el) => {
          let id = state.ids.get(el);
          if (!id) { id = 'n' + (++state.seq); state.ids.set(el, id); state.nodes.set(id, el); }
          return id;
        };
        const version = () => String(epoch) + ':' + String(state.rev);
        const absoluteRect = (el) => {
          const r = el.getBoundingClientRect();
          let x = r.x, y = r.y, win = el.ownerDocument?.defaultView;
          while (win && win !== window) {
            const frame = win.frameElement;
            if (!frame) break;
            const fr = frame.getBoundingClientRect(); x += fr.x; y += fr.y; win = frame.ownerDocument?.defaultView;
          }
          return {x, y, left: x, top: y, right: x + r.width, bottom: y + r.height,
            width: r.width, height: r.height};
        };
        const classHasState = (el, stateName) => {
          const tokens = String(el?.className || '').toLowerCase().split(/\s+/);
          return tokens.some(token => token === stateName || token === 'is-' + stateName
            || token.endsWith('-' + stateName));
        };
        // 自定义组件通常把隐藏 input 放在 label 内，并把状态镜像到 aria/class。
        // 这里只读浏览器可验证的关联关系，不执行站点脚本，也不改 DOM。
        const checkControl = (el) => {
          if (!el) return null;
          const native = el.matches?.('input[type=checkbox],input[type=radio]') ? el : null;
          const label = el.tagName === 'LABEL' ? el : el.closest?.('label');
          const associated = native || label?.control
            || label?.querySelector?.('input[type=checkbox],input[type=radio]') || null;
          const surface = label || el;
          const explicitRole = norm(el.getAttribute?.('role') || surface.getAttribute?.('role')).toLowerCase();
          const role = associated?.type === 'radio' || explicitRole === 'radio' ? 'radio'
            : associated?.type === 'checkbox' || explicitRole === 'checkbox' ? 'checkbox' : '';
          if (!role) return null;
          const ariaChecked = el.getAttribute?.('aria-checked') ?? surface.getAttribute?.('aria-checked')
            ?? associated?.getAttribute?.('aria-checked');
          let checked = null, mixed = false, checkedSource = '';
          if (associated && 'checked' in associated) {
            checked = !!associated.checked; mixed = !!associated.indeterminate;
            checkedSource = native ? 'native_control' : 'associated_control';
          } else if (ariaChecked !== null && ariaChecked !== undefined) {
            mixed = ariaChecked === 'mixed'; checked = ariaChecked === 'true'; checkedSource = 'aria';
          } else if (classHasState(surface, 'checked') || classHasState(surface, 'indeterminate')) {
            mixed = classHasState(surface, 'indeterminate'); checked = !mixed; checkedSource = 'class';
          }
          const disabled = !!associated?.disabled
            || el.getAttribute?.('aria-disabled') === 'true' || surface.getAttribute?.('aria-disabled') === 'true'
            || classHasState(surface, 'disabled');
          const disabledSource = associated?.disabled ? (native ? 'native_control' : 'associated_control')
            : (el.getAttribute?.('aria-disabled') !== null || surface.getAttribute?.('aria-disabled') !== null)
            ? 'aria' : classHasState(surface, 'disabled') ? 'class' : '';
          return {role, checked, mixed, disabled, surface, control: associated,
            sources: {checked: checkedSource, disabled: disabledSource}};
        };
        const enabled = (el) => !checkControl(el)?.disabled
          && !el.matches?.(':disabled,[aria-disabled=true]');
        // HTML-AAM 的常规映射。文档结构元素以前一律返回空角色，正文、列表、表格因此
        // 完全进不了节点树。取舍：
        // - 纯排版行内元素（strong/em/code/sub/sup/time/span）仍不给角色。它们数量极大，
        //   内容已由 text 字段带出，进树只会挤占节点预算。
        // - figcaption 在 HTML-AAM 里无对应角色，这里按 caption 收录，让图注单独成节点。
        // - pre 在 HTML-AAM 里是 generic，这里按 code 收录，让代码块单独成节点。
        // - canvas/video/audio/embed/object/iframe 在 HTML-AAM 里也无对应角色。它们的内部
        //   本就没有可访问 DOM，但"此处有一块 640x400 的视觉内容"这个事实必须可见，
        //   否则 agent 会以为这一页是空的，不会想到改用截图。角色名沿用浏览器 AX 树的叫法。
        const DOC_ROLES = {p: 'paragraph', ul: 'list', ol: 'list', menu: 'list', dl: 'list',
          li: 'listitem', dt: 'term', dd: 'definition', table: 'table', thead: 'rowgroup',
          tbody: 'rowgroup', tfoot: 'rowgroup', tr: 'row', td: 'cell', caption: 'caption',
          figcaption: 'caption', article: 'article', aside: 'complementary', main: 'main',
          nav: 'navigation', search: 'search', blockquote: 'blockquote', figure: 'figure',
          hr: 'separator', pre: 'code', details: 'group', fieldset: 'group', optgroup: 'group',
          option: 'option', output: 'status', progress: 'progressbar', meter: 'meter',
          dialog: 'dialog', svg: 'graphics-document', canvas: 'img', video: 'video',
          audio: 'audio', embed: 'embed', object: 'embed', iframe: 'iframe'};
        // aria-label / aria-labelledby / title 给出的名字。section、form 依 HTML-AAM 只有
        // 带可访问名时才是 region / form，而这里的"名"不看正文，否则每个 section 都成 region。
        const ariaName = (el) => {
          const labelled = norm(el.getAttribute?.('aria-labelledby') || '');
          if (labelled) {
            const doc = el.ownerDocument || document;
            const text = norm(labelled.split(/\s+/).map(id => doc.getElementById(id)?.textContent || '').join(' '));
            if (text) return text;
          }
          const svgTitle = el.tagName === 'svg'
            ? Array.from(el.children || []).find(child => child.tagName === 'title')?.textContent : '';
          return norm(el.getAttribute?.('aria-label') || el.getAttribute?.('alt')
            || el.getAttribute?.('title') || svgTitle || el.getAttribute?.('placeholder')
            || el.getAttribute?.('name') || '');
        };
        // header/footer 只在不位于 article/aside/main/nav/section 内时才是 banner/contentinfo；
        // th 的行列表头按 scope 判断，缺 scope 时按所在位置从简推断。
        const contextualRole = (el, tag) => {
          if (tag === 'section') return ariaName(el) ? 'region' : '';
          if (tag === 'form') return ariaName(el) ? 'form' : '';
          if (tag === 'header' || tag === 'footer')
            return el.closest?.('article,aside,main,nav,section') ? '' : (tag === 'header' ? 'banner' : 'contentinfo');
          if (tag === 'th') {
            const scope = String(el.getAttribute('scope') || '').toLowerCase();
            if (scope === 'row' || scope === 'rowgroup') return 'rowheader';
            if (scope === 'col' || scope === 'colgroup') return 'columnheader';
            if (el.closest?.('thead')) return 'columnheader';
            const row = el.parentElement;
            return row?.firstElementChild === el && row.querySelector?.('td') ? 'rowheader' : 'columnheader';
          }
          return '';
        };
        const implicitRole = (el) => {
          const tag = el.tagName?.toLowerCase() || '';
          if (tag === 'a' && el.hasAttribute('href')) return 'link';
          if (tag === 'button') return 'button';
          if (tag === 'textarea') return 'textbox';
          if (tag === 'select') return el.multiple ? 'listbox' : 'combobox';
          if (tag === 'img') return 'img';
          if (/^h[1-6]$/.test(tag)) return 'heading';
          if (tag === 'input') {
            const t = String(el.type || 'text').toLowerCase();
            if (['button','submit','reset'].includes(t)) return 'button';
            if (t === 'checkbox') return 'checkbox';
            if (t === 'radio') return 'radio';
            if (t === 'range') return 'slider';
            return t === 'hidden' ? '' : 'textbox';
          }
          if (tag === 'label') return checkControl(el)?.role || '';
          return DOC_ROLES[tag] || contextualRole(el, tag);
        };
        const accessibleName = (el) => ariaName(el) || norm(el.innerText || el.textContent || '');
        const locatorMatches = (el, locator) => {
          const exact = !!locator.exact;
          const same = (actual, wanted) => exact ? actual === wanted : actual.includes(wanted);
          if (locator.role && String(el.getAttribute?.('role') || implicitRole(el)).toLowerCase()
              !== String(locator.role).toLowerCase()) return false;
          if (locator.name && !same(accessibleName(el).toLowerCase(), norm(locator.name, 1000).toLowerCase())) return false;
          if (locator.text && !same(norm(el.innerText || el.textContent || '', 20000).toLowerCase(),
                                    norm(locator.text, 1000).toLowerCase())) return false;
          if (locator.test_id) {
            const id = norm(el.getAttribute?.('data-testid') || el.getAttribute?.('data-test-id')
              || el.getAttribute?.('data-test') || '', 1000);
            if (!same(id, norm(locator.test_id, 1000))) return false;
          }
          return true;
        };
        const locate = (locator) => {
          let candidates;
          try { candidates = deepQueryAll(String(locator.selector || '*')); }
          catch (_) { return {error: 'invalid_selector', matches: []}; }
          candidates = candidates.filter(el => locatorMatches(el, locator)
            && (locator.visible === undefined || rendered(el) === locator.visible));
          // 纯文本定位优先最深的命中，避免 body/容器/按钮同时包含同一段文字。
          if (locator.text && !locator.role && !locator.name && !locator.test_id) {
            candidates = candidates.filter(el => !candidates.some(other => other !== el && el.contains?.(other)));
          }
          if (Number.isInteger(locator.nth)) {
            const at = locator.nth < 0 ? candidates.length + locator.nth : locator.nth;
            candidates = at >= 0 && at < candidates.length ? [candidates[at]] : [];
          }
          return {matches: candidates};
        };
        // 文档结构角色让正文、列表、表格进入节点树。纯交互观察（如动作后的弹窗探测）
        // 不需要这些节点，可用 snapshot({structure: false}) 关掉，保持原来的口径。
        const STRUCTURE_ROLES = new Set(['paragraph', 'list', 'listitem', 'term', 'definition',
          'table', 'rowgroup', 'row', 'cell', 'columnheader', 'rowheader', 'caption', 'article',
          'complementary', 'main', 'navigation', 'search', 'region', 'banner', 'contentinfo',
          'blockquote', 'figure', 'group', 'form', 'separator', 'code', 'status']);
        // 这些角色的正文一定由子节点重新带出，节点自身只留一段摘要，避免 article/table
        // 把整篇内容在每一层重复一遍。paragraph、cell、code 这类内容落脚点不在其中。
        const SUMMARY_TEXT_ROLES = new Set(['list', 'table', 'rowgroup', 'row', 'article',
          'complementary', 'main', 'navigation', 'search', 'region', 'banner', 'contentinfo',
          'figure', 'group', 'form', 'document']);
        // 这些角色的名字只认 aria，不认内容：否则整段正文会在 name 与 text 里重复一遍。
        const ARIA_ONLY_NAME = new Set(['paragraph', 'list', 'table', 'rowgroup', 'row', 'article',
          'complementary', 'main', 'navigation', 'search', 'region', 'banner', 'contentinfo',
          'blockquote', 'figure', 'group', 'form', 'separator', 'code', 'graphics-document',
          'video', 'audio', 'embed', 'iframe', 'document']);
        const semantic = (el, structure = true) => {
          const explicit = norm(el.getAttribute?.('role') || '');
          const role = explicit || implicitRole(el);
          if (role && (structure || explicit || !STRUCTURE_ROLES.has(role.toLowerCase()))) return true;
          return el.matches?.('[tabindex],summary,dialog,[aria-modal=true],[contenteditable=true],[onclick]')
            || (rendered(el) && (el.ownerDocument?.defaultView || window).getComputedStyle(el).cursor === 'pointer'
                && !Array.from(el.children || []).some(child => rendered(child)
                  && (child.ownerDocument?.defaultView || window).getComputedStyle(child).cursor === 'pointer'));
        };
        // 纯包装容器折叠：隐式结构容器若没有可访问名、没有直接文本、且最多只有一个渲染
        // 子元素，就不单独建节点，子节点直接挂到祖先上。可访问性树对 generic 容器就是
        // 这么做的，避免 li > div > span > a 这类多层包装各占一个节点把预算吃光。
        // 带显式 role 属性的元素一律保留，不折叠，以免改变改动前的可见节点集合。
        const WRAPPER_ROLES = new Set(['paragraph', 'list', 'listitem', 'term', 'definition',
          'rowgroup', 'region', 'group', 'article', 'complementary', 'banner', 'contentinfo',
          'figure', 'blockquote', 'navigation', 'main', 'search', 'form', 'caption']);
        const directText = (el) => Array.from(el.childNodes || [])
          .filter(node => node.nodeType === 3).map(node => node.textContent).join('').trim();
        const wrapperOnly = (el) => {
          if (norm(el.getAttribute?.('role') || '')) return false;
          if (!WRAPPER_ROLES.has(implicitRole(el))) return false;
          if (ariaName(el) || directText(el)) return false;
          return Array.from(el.children || []).filter(rendered).length <= 1;
        };
        // aria-hidden 的子树不进可访问性树。站点里的装饰性图标（尤其是 svg）大量带这个
        // 属性，放进来只会淹没有效节点。但也有站点把可交互元素误标 aria-hidden，因此
        // 子树里还有可聚焦或带点击处理的元素时照常下探。
        const ariaHidden = (el) => el.getAttribute?.('aria-hidden') === 'true'
          && !el.matches?.('a[href],button,input,select,textarea,[tabindex],[onclick]')
          && !el.querySelector?.('a[href],button,input,select,textarea,[tabindex],[onclick]')
          && !checkControl(el);
        // svg 内部可能有上千个 path/g。svg 本身进树即可，除非内部带显式 role 或 aria 标注，
        // 否则不再下探；canvas 内部没有可访问 DOM，同样不下探。
        const opaqueSubtree = (el) => (el.tagName === 'svg' || el.tagName === 'CANVAS')
          && !el.querySelector?.('[role],[aria-label],[aria-labelledby]');
        const selectedValues = (el) => {
          if (el instanceof HTMLSelectElement) return Array.from(el.selectedOptions || [])
            .map(option => String(option.value || norm(option.label || option.textContent || '', 1000)));
          const role = norm(el.getAttribute?.('role') || implicitRole(el)).toLowerCase();
          if (role !== 'combobox' && role !== 'listbox') return null;
          let root = el;
          const controls = norm(el.getAttribute?.('aria-controls') || '');
          if (controls) root = el.ownerDocument?.getElementById(controls) || root;
          const options = Array.from(root.querySelectorAll?.('[role=option]') || [])
            .filter(option => option.getAttribute('aria-selected') === 'true');
          if (options.length) return options.map(option => norm(option.getAttribute('data-value')
            || option.getAttribute('value') || accessibleName(option), 1000));
          const value = sensitive(el) ? '' : norm(el.value || '', 1000);
          return value ? [value] : [];
        };
        const handoffState = () => {
          const current = deepQueryAll('[aria-current=step],[role=tab][aria-selected=true]')
            .find(rendered) || null;
          const fields = deepQueryAll('input,textarea,select').filter(el => !sensitive(el));
          const dirty = fields.some(el => el instanceof HTMLSelectElement
            ? Array.from(el.options).some(option => option.selected !== option.defaultSelected)
            : el.matches?.('input[type=checkbox],input[type=radio]')
            ? !!el.checked !== !!el.defaultChecked
            : String(el.value ?? '') !== String(el.defaultValue ?? ''));
          return {current_step: current ? accessibleName(current) : '', unsaved: dirty,
            submission_status: 'unknown', ticket_id: '', basis: 'dom_form_state'};
        };
        const frameName = (frame, index) => norm(frame.getAttribute('title') || frame.name || frame.id || ('frame-' + index));
        const snapshot = (input = {}) => {
          // 结构元素进树后同一页面的节点数会显著上升（长文尤其明显），默认与上限相应放宽。
          const limit = Math.max(1, Math.min(Number(input.max_nodes || 1500), 4000));
          const includeOffscreen = !!input.include_offscreen;
          const structure = input.structure !== false;
          const rootNode = input.root_node_id ? resolve(input.root_node_id) : null;
          if (input.root_node_id && !rootNode) return {error: 'stale_or_missing_root'};
          const nodes = [];
          const opaqueFrames = [];
          const texts = [];
          const visit = (root, parentId = '', depth = 0, frameId = 'top', shadow = false) => {
            if (!root || nodes.length >= limit) return;
            observe(root);
            const list = Array.from(root.children || root.childNodes || []);
            let frameIndex = 0;
            for (const el of list) {
              if (!(el instanceof Element) || nodes.length >= limit || ariaHidden(el)) continue;
              // depth 与 parent_id 同口径：只在真正建节点时加一层，未收录的包装元素不占层级，
              // 否则缩进读起来与父子关系对不上。
              let nextParent = parentId, nextDepth = depth;
              if (rendered(el) && (includeOffscreen || inViewport(el))
                  && semantic(el, structure) && !wrapperOnly(el)) {
                const id = String(epoch) + ':' + nodeId(el);
                const r = absoluteRect(el), secret = sensitive(el);
                const role = norm(el.getAttribute('role') || implicitRole(el));
                const states = {};
                const stateSources = {};
                for (const key of ['expanded','selected','pressed','required','invalid','readonly','busy','current']) {
                  const value = el.getAttribute('aria-' + key);
                  if (value !== null) states[key] = value;
                }
                if ('disabled' in el) states.disabled = !!el.disabled;
                if ('checked' in el) states.checked = !!el.checked;
                const check = checkControl(el);
                if (check) {
                  if (check.checked !== null) states.checked = check.checked;
                  if (check.mixed) states.mixed = true;
                  states.disabled = check.disabled;
                  if (check.sources.checked) stateSources.checked = check.sources.checked;
                  if (check.sources.disabled) stateSources.disabled = check.sources.disabled;
                }
                const selected = selectedValues(el);
                const href = el.tagName === 'A' && role.toLowerCase() === 'link' ? safeHref(el) : '';
                nodes.push({node_id: id, parent_id: parentId, depth, frame_id: frameId,
                  shadow_root: shadow, tag: el.tagName.toLowerCase(), role,
                  name: ARIA_ONLY_NAME.has(role.toLowerCase()) ? ariaName(el) : accessibleName(el),
                  type: norm(el.getAttribute('type') || ''),
                  ...(href ? {href} : {}),
                  // 容器节点的正文已经由子节点与顶层 text 字段带出，这里只留一段摘要，
                  // 避免 article/section 把整篇文章在每一层重复一遍。
                  text: redact(el.innerText || el.textContent || '',
                    SUMMARY_TEXT_ROLES.has(role.toLowerCase()) && el.childElementCount > 0 ? 2000 : 20000),
                  value: secret ? '[redacted]' : norm(el.value || '', 1000),
                  states, ...(Object.keys(stateSources).length ? {state_sources: stateSources} : {}),
                  ...(selected === null ? {} : {selected_values: selected}),
                  sensitive: secret, visible: rendered(el), in_viewport: inViewport(el),
                  needs_user: el.matches('input[type=file]') ? 'file_upload' : '',
                  rect: {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)}});
                nextParent = id; nextDepth = depth + 1;
              }
              if (opaqueSubtree(el)) continue;
              if (el.shadowRoot) visit(el.shadowRoot, nextParent, nextDepth, frameId, true);
              if (el.tagName === 'IFRAME') {
                frameIndex += 1;
                const childFrameId = frameId + '/' + frameName(el, frameIndex);
                try {
                  const doc = el.contentDocument;
                  if (doc?.documentElement) visit(doc.documentElement, nextParent, nextDepth, childFrameId, false);
                  else opaqueFrames.push({frame_id: childFrameId, reason: 'unavailable'});
                } catch (_) { opaqueFrames.push({frame_id: childFrameId, reason: 'cross_origin'}); }
              }
              visit(el, nextParent, nextDepth, frameId, shadow);
            }
          };
          visit(rootNode || document.documentElement);
          try { texts.push(document.body?.innerText || ''); } catch (_) {}
          let filtered = nodes;
          const wanted = norm(input.find || '', 1000).toLowerCase();
          if (wanted) {
            const matches = new Set(nodes.filter(n => [n.name,n.text,n.value,n.role,n.tag]
              .some(v => String(v || '').toLowerCase().includes(wanted))).map(n => n.node_id));
            const parents = new Map(nodes.map(n => [n.node_id, n.parent_id]));
            for (const id of [...matches]) { let p = parents.get(id); while (p) { matches.add(p); p = parents.get(p); } }
            filtered = nodes.filter(n => matches.has(n.node_id));
          }
          const stackZ = (el) => {
            const raw = (el.ownerDocument?.defaultView || window).getComputedStyle(el).zIndex;
            const value = Number(raw); return Number.isFinite(value) ? value : 0;
          };
          const modals = nodes.filter(n => n.role === 'dialog' || n.role === 'alertdialog'
            || n.tag === 'dialog').map(n => ({...n,
              z_index: stackZ(state.nodes.get(String(n.node_id).split(':')[1]))}))
            .sort((a, b) => a.z_index - b.z_index).map((n, index) => ({...n, stack_index: index}));
          const overlayStack = (kind) => deepQueryAll('*').filter(rendered).filter(el => {
            const role = norm(el.getAttribute?.('role') || implicitRole(el)).toLowerCase();
            const classes = String(el.className || '').toLowerCase();
            return kind === 'menu' ? ['menu','listbox','tree'].includes(role)
              : role === 'tooltip' || /(?:^|[-_])(popover|popup|dropdown|trigger-popup)(?:$|[-_])/.test(classes);
          }).sort((a, b) => stackZ(a) - stackZ(b)).slice(0, 40).map((el, index) => {
            const r = absoluteRect(el);
            return {node_id: String(epoch) + ':' + nodeId(el), stack_index: index,
              z_index: stackZ(el),
              role: norm(el.getAttribute?.('role') || implicitRole(el)), name: redact(accessibleName(el)),
              rect: {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)}};
          });
          const menus = overlayStack('menu'), popovers = overlayStack('popover');
          return {schema: 'desk.browser.document/v1', observation_version: version(),
            url: String(location.href), title: String(document.title || ''),
            handoff: handoffState(),
            viewport: {width: innerWidth, height: innerHeight, scroll_x: scrollX, scroll_y: scrollY},
            text: redactBlock(texts.join('\n')), nodes: filtered, total_nodes: nodes.length,
            truncated: nodes.length >= limit, modal_stack: modals, menu_stack: menus,
            popover_stack: popovers, opaque_frames: opaqueFrames,
            capabilities: {open_shadow_roots: 'readable', same_origin_frames: 'readable',
              cross_origin_frames: 'opaque', javascript_dialogs: 'needs_user',
              file_upload: 'needs_user', downloads: 'needs_user',
              locators: ['selector','role','name','text','test_id'],
              pointer_input: 'native', network_idle: 'fetch_xhr_and_dom_quiet'}};
        };
        const resolve = (raw) => {
          const parts = String(raw || '').split(':');
          if (parts.length !== 2 || Number(parts[0]) !== Number(epoch)) return null;
          const el = state.nodes.get(parts[1]);
          return el && el.isConnected ? el : null;
        };
        // 只在隔离世界保留完整比对材料，回执仅返回不透明凭据。任何不完整/敏感上下文
        // 都不提供自动续办；不截断后比较，以免漏掉屏幕外的实质变化。
        const approvalSignature = () => {
          const elements = deepQueryAll('*');
          if (elements.length > 4000 || elements.some(el => sensitive(el)
              || el.tagName === 'IFRAME' || el.shadowRoot)) return null;
          const facts = elements.filter(el => !['SCRIPT','STYLE','NOSCRIPT'].includes(el.tagName)).map(el => [
            el.tagName, Array.from(el.attributes || []).filter(a => a.name !== 'style'
              && !a.name.startsWith('data-desk-network-')).map(a => [a.name, a.value]),
            rendered(el), 'value' in el ? el.value : '', 'checked' in el ? el.checked : null,
            'selected' in el ? el.selected : null]);
          const value = JSON.stringify([String(location.href), document.title, document.body?.innerText || '', facts]);
          return value.length <= 500000 ? value : null;
        };
        const rememberApproval = (output) => {
          if (output.truncated || output.opaque_frames.length) return;
          const signature = approvalSignature();
          if (signature === null) return;
          state.guardSeq = (state.guardSeq || 0) + 1;
          const token = String(epoch) + ':' + state.guardSeq;
          const guards = state.approvalGuards || (state.approvalGuards = new Map());
          guards.set(token, {version: output.observation_version, signature,
            // 文本 locator 可能命中按钮内的 span；它没有独立语义节点，但仍须保留原身份。
            elements: new Set(deepQueryAll('*')), doc: document});
          while (guards.size > 8) guards.delete(guards.keys().next().value);
          output.approval_guard = {token, version: output.observation_version};
        };
        const verifyApproval = (el, proof) => {
          if (!proof) return '';
          const prior = state.approvalGuards?.get(proof.token);
          if (!prior || prior.doc !== document || prior.version !== proof.version)
            return 'approval_revalidation_unavailable';
          if (!prior.elements.has(el) || prior.signature !== approvalSignature())
            return 'approval_target_changed';
          return '';
        };
        const fire = (el, type) => el.dispatchEvent(new Event(type, {bubbles: true, composed: true}));
        const networkActivity = () => {
          let pending = 0, last = 0, trackedFrames = 0, opaqueFrames = 0;
          const visit = (doc) => {
            const root = doc?.documentElement; if (!root) return;
            const rawPending = root.getAttribute('data-desk-network-pending');
            const rawLast = root.getAttribute('data-desk-network-last');
            if (rawPending !== null) { pending += Number(rawPending || 0); trackedFrames += 1; }
            last = Math.max(last, Number(rawLast || 0));
            for (const frame of doc.querySelectorAll('iframe')) {
              try { if (frame.contentDocument) visit(frame.contentDocument); else opaqueFrames += 1; }
              catch (_) { opaqueFrames += 1; }
            }
          };
          visit(document);
          return {pending, last, tracked_frames: trackedFrames, opaque_frames: opaqueFrames};
        };
        const safeResourceUrl = (raw) => {
          try { const u = new URL(String(raw || ''), location.href); return u.origin + u.pathname; }
          catch (_) { return ''; }
        };
        const networkSnapshot = (input = {}) => {
          const limit = Math.max(1, Math.min(Number(input.limit || 200), 1000));
          const since = Math.max(0, Number(input.since_ms || 0));
          const initiator = norm(input.initiator || '', 100).toLowerCase();
          const wanted = norm(input.url_contains || '', 1000).toLowerCase();
          const entries = [], opaqueFrames = [];
          const visit = (doc, frameId = 'top') => {
            if (!doc || entries.length >= limit) return;
            const win = doc.defaultView; if (!win) return;
            let resources = [];
            try { resources = win.performance?.getEntriesByType?.('resource') || []; } catch (_) {}
            for (const row of resources) {
              const url = safeResourceUrl(row.name);
              const kind = norm(row.initiatorType || 'other', 100).toLowerCase();
              if (Number(row.startTime || 0) < since || (initiator && kind !== initiator)
                  || (wanted && !url.toLowerCase().includes(wanted))) continue;
              entries.push({frame_id: frameId, url, initiator: kind,
                start_ms: Math.round(Number(row.startTime || 0)),
                duration_ms: Math.round(Number(row.duration || 0)),
                response_end_ms: Math.round(Number(row.responseEnd || 0)),
                transfer_size: Number(row.transferSize || 0),
                encoded_body_size: Number(row.encodedBodySize || 0),
                decoded_body_size: Number(row.decodedBodySize || 0),
                protocol: norm(row.nextHopProtocol || '', 100),
                ...(Number(row.responseStatus || 0) > 0 ? {status: Number(row.responseStatus)} : {})});
              if (entries.length >= limit) break;
            }
            let index = 0;
            for (const frame of doc.querySelectorAll('iframe')) {
              index += 1; const childId = frameId + '/' + frameName(frame, index);
              try {
                if (frame.contentDocument?.documentElement) visit(frame.contentDocument, childId);
                else opaqueFrames.push({frame_id: childId, reason: 'unavailable'});
              } catch (_) { opaqueFrames.push({frame_id: childId, reason: 'cross_origin'}); }
            }
          };
          visit(document);
          return {schema: 'desk.browser.network/v1', url: safeResourceUrl(location.href),
            captured_at_ms: Math.round(performance.now()), entries, truncated: entries.length >= limit,
            opaque_frames: opaqueFrames, redaction: 'query_fragment_headers_bodies_omitted',
            capabilities: {resource_timing: 'available', same_origin_frames: 'readable',
              cross_origin_frames: 'opaque', request_headers: 'unavailable', response_bodies: 'unavailable'}};
        };
        if (command.kind === 'inspect' && command.name === 'document') {
          const output = snapshot(command.input || {});
          if (output.error) return {ok: false, error: output.error, observation_version: version()};
          rememberApproval(output);
          return {ok: true, output, observation_version: output.observation_version};
        }
        if (command.kind === 'inspect' && command.name === 'network') {
          return {ok: true, output: networkSnapshot(command.input || {}), observation_version: version()};
        }
        const input = command.input || {};
        if (command.name === 'wait') {
          const delay = Math.max(0, Math.min(Number(input.milliseconds ?? 0), 10000));
          const wanted = norm(input.text || '', 1000);
          const selector = norm(input.selector || '', 1000);
          const locator = input.locator && typeof input.locator === 'object' ? input.locator : null;
          const targetState = String(input.state || 'attached');
          const targetSpecified = !!selector || !!locator;
          const wantedUrl = String(input.url || '');
          const mode = String(input.url_mode || 'contains');
          const timeout = Math.max(100, Math.min(Number(input.timeout_ms || 10000), 60000));
          const started = Date.now(), deadline = started + Math.max(delay, timeout);
          let stableRev = state.rev, stableSince = Date.now();
          do {
            const now = Date.now();
            if (input.guard_url && String(location.href) !== input.guard_url)
              return {ok: false, error: 'resource_changed', observation_version: version()};
            if (input.abort_if) {
              let failed;
              try { failed = deepQuery(input.abort_if); } catch (_) {
                return {ok: false, error: 'invalid_selector', observation_version: version()};
              }
              if (rendered(failed)) return {ok: false, error: 'wait_aborted',
                output: {reason: 'failure_condition_visible'}, observation_version: version()};
            }
            if (state.rev !== stableRev) { stableRev = state.rev; stableSince = now; }
            let target = null;
            if (locator) {
              const found = locate(locator);
              if (found.error) return {ok: false, error: found.error, observation_version: version()};
              if (found.matches.length > 1) return {ok: false, error: 'locator_ambiguous',
                output: {matches: found.matches.length}, observation_version: version()};
              target = found.matches[0] || null;
            } else if (selector) {
              try { target = deepQuery(selector); } catch (_) {
                return {ok: false, error: 'invalid_selector', observation_version: version()};
              }
            }
            const targetEnabled = !!target && enabled(target);
            const control = target ? checkControl(target) : null;
            const values = target ? selectedValues(target) : null;
            const actualValue = target && !sensitive(target) ? norm(target.value || '', 1000) : '';
            const targetMatched = !targetSpecified ? true
              : targetState === 'detached' ? !target
              : targetState === 'visible' ? rendered(target)
              : targetState === 'hidden' ? (!target || !rendered(target))
              : targetState === 'enabled' ? (rendered(target) && targetEnabled)
              : targetState === 'disabled' ? (!!target && !targetEnabled)
              : targetState === 'checked' ? (rendered(target) && control?.checked === true)
              : targetState === 'unchecked' ? (rendered(target) && control?.checked === false)
              : targetState === 'mixed' ? (rendered(target) && control?.mixed === true)
              : !!target;
            const valueMatched = input.value === undefined || (target && actualValue === String(input.value));
            const valuesMatched = !Array.isArray(input.values) || (target && Array.isArray(values)
              && input.values.length === values.length
              && input.values.every(value => values.includes(String(value))));
            const url = String(location.href);
            const urlMatched = !wantedUrl || (mode === 'equals' ? url === wantedUrl
              : mode === 'prefix' ? url.startsWith(wantedUrl) : url.includes(wantedUrl));
            const textMatched = !wanted || norm(document.body?.innerText || '', 20000).includes(wanted);
            const network = networkActivity();
            const idleMatched = !input.network_idle
              || (document.readyState === 'complete' && network.pending === 0
                  && now - Math.max(stableSince, network.last || 0) >= 500);
            const delayMatched = !delay || now - started >= delay;
            if (targetMatched && valueMatched && valuesMatched && urlMatched && textMatched
                && idleMatched && delayMatched) {
              return {ok: true, output: {text: wanted, selector, target_state: targetState,
                target_matched: targetMatched, value_matched: valueMatched,
                selected_values_matched: valuesMatched, url, network_idle: idleMatched,
                ...(target && !sensitive(target) ? {target_value: actualValue} : {}),
                ...(Array.isArray(values) ? {selected_values: values} : {}),
                network_pending: network.pending, network_tracked_frames: network.tracked_frames,
                network_opaque_frames: network.opaque_frames,
                network_idle_basis: input.network_idle ? 'fetch_xhr_idle_and_dom_quiet_500ms' : ''},
                observation_version: version()};
            }
            await new Promise(resolve => setTimeout(resolve, 200));
          } while (Date.now() < deadline);
          return {ok: false, error: 'wait_timeout', observation_version: version()};
        }
        const deepElementFromPoint = (doc, x, y) => {
          let el = doc.elementFromPoint(x, y);
          if (!el) return null;
          if (el.shadowRoot) el = el.shadowRoot.elementFromPoint(x, y) || el;
          if (el.tagName === 'IFRAME') {
            try { const r = el.getBoundingClientRect(); return deepElementFromPoint(el.contentDocument, x-r.x, y-r.y) || el; }
            catch (_) { return el; }
          }
          return el;
        };
        const composedDescendant = (node, ancestor) => {
          for (let at = node; at; at = at.assignedSlot || at.parentNode || at.host) if (at === ancestor) return true;
          return false;
        };
        const findTarget = (spec = input) => {
          if (spec.node_id) {
            const node = resolve(spec.node_id);
            return node ? {element: node} : {error: 'stale_or_missing_node'};
          }
          if (spec.locator) {
            // 动作默认只匹配已渲染元素。等待 attached/hidden 仍使用 locate 原始语义。
            const result = locate({visible: true, ...spec.locator});
            if (result.error) return result;
            if (result.matches.length > 1) return {error: 'locator_ambiguous', count: result.matches.length};
            return result.matches.length ? {element: result.matches[0]} : {error: 'locator_not_found'};
          }
          if (spec.selector) {
            try {
              const matches = deepQueryAll(String(spec.selector)).filter(rendered);
              if (matches.length > 1) return {error: 'locator_ambiguous', count: matches.length};
              return matches.length ? {element: matches[0]} : {error: 'selector_not_found'};
            }
            catch (_) { return {error: 'invalid_selector'}; }
          }
          return {element: null};
        };
        const waitForTarget = async (spec, required, actionable) => {
          const timeout = Math.max(100, Math.min(Number(spec.timeout_ms || input.timeout_ms || 5000), 60000));
          const started = Date.now(), deadline = started + timeout;
          let lastError = required ? 'target_not_found' : '';
          let attempts = 0, lastRect = null, hitTag = '', candidates = [], occluder = null;
          do {
            attempts += 1;
            const found = findTarget(spec);
            if (found.error === 'invalid_selector' || found.error === 'stale_or_missing_node'
                || found.error === 'locator_ambiguous') return found;
            const target = found.element;
            if (!target) { if (!required) return {element: null}; lastError = found.error || lastError; }
            else if (!rendered(target)) lastError = 'element_not_visible';
            else if (!enabled(target)) lastError = 'element_disabled';
            else {
              if (!actionable) return {element: target};
              try { target.scrollIntoView?.({block: 'center', inline: 'nearest'}); } catch (_) {}
              const first = absoluteRect(target);
              await new Promise(resolve => setTimeout(resolve, 100));
              const second = absoluteRect(target);
              lastRect = {x: Math.round(second.x), y: Math.round(second.y),
                width: Math.round(second.width), height: Math.round(second.height)};
              if (Math.abs(first.x-second.x) > 0.5 || Math.abs(first.y-second.y) > 0.5
                  || Math.abs(first.width-second.width) > 0.5 || Math.abs(first.height-second.height) > 0.5) {
                lastError = 'element_not_stable';
              } else {
                const left = Math.max(0, second.left), right = Math.min(innerWidth, second.right);
                const top = Math.max(0, second.top), bottom = Math.min(innerHeight, second.bottom);
                candidates = [];
                for (const ry of [0.5, 0.25, 0.75]) for (const rx of [0.5, 0.2, 0.8]) {
                  const x = left + (right-left) * rx, y = top + (bottom-top) * ry;
                  if (right <= left || bottom <= top) continue;
                  const hit = deepElementFromPoint(document, x, y);
                  hitTag = String(hit?.tagName || '').toLowerCase();
                  candidates.push({x: Math.round(x), y: Math.round(y), hit: hitTag});
                  if (hit && composedDescendant(hit, target)) return {element: target, x, y,
                    diagnostic: {attempts, elapsed_ms: Date.now() - started, rect: lastRect,
                      clickable_region: {x: Math.round(left), y: Math.round(top),
                        width: Math.round(right-left), height: Math.round(bottom-top)},
                      hit: hitTag, point_strategy: rx === 0.5 && ry === 0.5 ? 'center' : 'visible_internal'}};
                  if (hit) {
                    const hr = absoluteRect(hit);
                    occluder = {tag: hitTag, role: norm(hit.getAttribute?.('role') || implicitRole(hit)),
                      name: accessibleName(hit), rect: {x: Math.round(hr.x), y: Math.round(hr.y),
                        width: Math.round(hr.width), height: Math.round(hr.height)}};
                  }
                }
                lastError = 'element_occluded';
              }
            }
            await new Promise(resolve => setTimeout(resolve, 100));
          } while (Date.now() < deadline);
          return {error: lastError === 'locator_not_found' ? 'locator_timeout'
            : lastError === 'selector_not_found' ? 'selector_timeout' : lastError,
            diagnostic: {attempts, elapsed_ms: Date.now() - started, last_error: lastError,
              rect: lastRect, hit: hitTag, candidate_points: candidates, occluder}};
        };
        let el = null;
        if (command.name === 'prepare_drag') {
          const source = await waitForTarget(input.source || {}, true, true);
          if (source.error) return {ok: false, error: source.error,
            output: {side: 'source', matches: source.count || 0, diagnostic: source.diagnostic || {}},
            observation_version: version()};
          const target = await waitForTarget(input.target || {}, true, true);
          if (target.error) return {ok: false, error: target.error,
            output: {side: 'target', matches: target.count || 0, diagnostic: target.diagnostic || {}},
            observation_version: version()};
          return {ok: true, output: {source_x: source.x, source_y: source.y,
            target_x: target.x, target_y: target.y, rev: state.rev, url: String(location.href),
            diagnostic: {source: source.diagnostic || {}, target: target.diagnostic || {}}},
            observation_version: version()};
        }
        if (command.name === 'prepare_pointer') {
          if (Number.isFinite(Number(input.x)) && Number.isFinite(Number(input.y))) {
            const x = Number(input.x), y = Number(input.y);
            el = deepElementFromPoint(document, x, y);
            if (!el) return {ok: false, error: 'coordinate_has_no_target', observation_version: version()};
            if (el.matches?.('input[type=file]')) return {ok: false, error: 'file_upload_needs_user',
              output: {needs_user: 'file_upload'}, observation_version: version()};
            return {ok: true, output: {x, y, rev: state.rev, url: String(location.href),
              target: String(el.tagName || '').toLowerCase()}, observation_version: version()};
          }
          const prepared = await waitForTarget(input, true, true);
          if (prepared.error) return {ok: false, error: prepared.error,
            output: {matches: prepared.count || 0, diagnostic: prepared.diagnostic || {}},
            observation_version: version()};
          el = prepared.element;
          const approvalError = verifyApproval(el, input._approval_guard);
          if (approvalError) return {ok: false, error: approvalError,
            output: {action_executed: false, requires_reinspect: true,
              next_step: 'inspect_then_request_new_approval'}, observation_version: version()};
          if (el.matches?.('input[type=file]')) return {ok: false, error: 'file_upload_needs_user',
            output: {needs_user: 'file_upload'}, observation_version: version()};
          const pointerAction = String(input.pointer_action || '');
          if (pointerAction === 'check' || pointerAction === 'uncheck') {
            const check = checkControl(el);
            if (!check) return {ok: false, error: 'element_not_checkable',
              output: {diagnostic: prepared.diagnostic || {}}, observation_version: version()};
            const current = check.checked;
            const desired = pointerAction === 'check';
            if (!desired && check.role === 'radio')
              return {ok: false, error: 'radio_cannot_uncheck', observation_version: version()};
            if (current === desired) return {ok: true, output: {skip: true, checked: desired,
              effect_verified: true, state_source: check.sources.checked,
              action_executed: false, delivery: 'skipped', outcome: 'effect_verified', condition_met: true,
              rev: state.rev, url: String(location.href), input_path: 'native_pointer',
              diagnostic: prepared.diagnostic || {}}, observation_version: version()};
          }
          return {ok: true, output: {x: prepared.x, y: prepared.y, rev: state.rev,
            url: String(location.href), target: String(el.tagName || '').toLowerCase(),
            approval_revalidated: !!input._approval_guard,
            diagnostic: prepared.diagnostic || {}},
            observation_version: version()};
        }
        if (command.name === 'settle') {
          await new Promise(resolve => setTimeout(resolve, 250));
          const url = String(location.href), changes = [];
          if (url !== String(input.before_url || '')) changes.push('url_changed');
          if (state.rev !== Number(input.before_rev || 0)) changes.push('document_changed');
          const modal = snapshot({max_nodes: 200, structure: false}).modal_stack || [];
          if (modal.length) changes.push('dialog_opened');
          let checked;
          if (typeof input.desired_checked === 'boolean') {
            const prepared = await waitForTarget(input, true, false);
            if (prepared.error) return {ok: false, error: prepared.error,
              output: {diagnostic: prepared.diagnostic || {}}, observation_version: version()};
            const target = prepared.element;
            const check = checkControl(target);
            checked = check?.checked;
            if (checked !== input.desired_checked) return {ok: false,
              error: 'check_state_not_reached', output: {checked, desired_checked: input.desired_checked,
                url, changes}, observation_version: version()};
          }
          state.rev += 1;
          const effectVerified = checked !== undefined;
          return {ok: true, output: {url, changes, requires_reinspect: true,
            action_executed: true, delivery: 'dispatched', effect_verified: effectVerified,
            outcome: effectVerified ? 'effect_verified' : 'delivery_only',
            condition_met: effectVerified ? true : null,
            next_step: effectVerified ? 'inspect_for_followup' : 'inspect_or_wait_for_postcondition',
            diagnostic: input.diagnostic || {},
            approval_revalidated: !!input.approval_revalidated,
            modal_count: modal.length, input_path: String(input.input_path || 'native'),
            action: String(input.action || ''), ...(checked === undefined ? {} : {checked})},
            observation_version: version()};
        }
        const hasTarget = !!(input.node_id || input.selector || input.locator);
        const needsTarget = ['fill','select'].includes(command.name);
        if (hasTarget || needsTarget) {
          const prepared = await waitForTarget(input, hasTarget || needsTarget, command.name !== 'scroll');
          if (prepared.error) return {ok: false, error: prepared.error,
            output: {matches: prepared.count || 0, diagnostic: prepared.diagnostic || {}},
            observation_version: version()};
          el = prepared.element;
        }
        if (command.name === 'focus') {
          // 原生按键前把焦点放到目标上。没指定目标就用当前焦点(没有则 body)。
          const target = el || document.activeElement || document.body;
          if (typeof input.text === 'string' && sensitive(target))
            return {ok: false, error: 'sensitive_field_needs_user', observation_version: version()};
          if (target.matches?.('input[type=file]')) return {ok: false, error: 'file_upload_needs_user',
            output: {needs_user: 'file_upload'}, observation_version: version()};
          try { target.scrollIntoView?.({block: 'center', inline: 'nearest'}); } catch (_) {}
          try { target.focus?.(); } catch (_) {}
          return {ok: true, output: {rev: state.rev, url: String(location.href),
            focused: String(target.tagName || '').toLowerCase()}, observation_version: version()};
        }
        if (command.name === 'blur') {
          const target = el || document.activeElement;
          if (!target || target === document.body) return {ok: true, output: {rev: state.rev,
            url: String(location.href), blurred: ''}, observation_version: version()};
          try { target.blur?.(); } catch (_) {}
          await new Promise(resolve => setTimeout(resolve, 250));
          return {ok: true, output: {rev: state.rev, url: String(location.href),
            blurred: String(target.tagName || '').toLowerCase()}, observation_version: version()};
        }
        const beforeRev = state.rev, beforeUrl = String(location.href);
        let selectedCount, selectedResult;
        if (command.name === 'fill') {
          if (sensitive(el)) return {ok: false, error: 'sensitive_field_needs_user'};
          el.focus();
          if (el.isContentEditable) {
            el.textContent = String(input.value ?? '');
          } else {
            const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(el, String(input.value ?? '')); else el.value = String(input.value ?? '');
          }
          fire(el, 'input'); fire(el, 'change');
        } else if (command.name === 'select') {
          if (!(el instanceof HTMLSelectElement)) return {ok: false, error: 'select_target_invalid',
            observation_version: version()};
          const desired = Array.isArray(input.values)
            ? input.values.map(value => String(value)) : [String(input.value ?? '')];
          if (!el.multiple && desired.length > 1) return {ok: false, error: 'select_multiple_not_allowed',
            output: {requested_count: desired.length}, observation_version: version()};
          const options = Array.from(el.options), picked = [];
          for (const wantedOption of desired) {
            const option = options.find(row => String(row.value) === wantedOption)
              || options.find(row => norm(row.label || row.textContent || '', 1000) === norm(wantedOption, 1000));
            if (!option) return {ok: false, error: 'select_option_not_found',
              output: {requested_count: desired.length, matched_count: picked.length},
              observation_version: version()};
            picked.push(option);
          }
          const chosen = new Set(picked);
          for (const option of options) option.selected = chosen.has(option);
          selectedCount = options.filter(option => option.selected).length;
          fire(el, 'input'); fire(el, 'change');
          selectedResult = selectedValues(el);
          if (!Array.isArray(selectedResult) || selectedResult.length !== desired.length
              || picked.some(option => !option.selected)) {
            return {ok: false, error: 'select_state_not_reached',
              output: {selected_values: selectedResult || [], requested_values: desired},
              observation_version: version()};
          }
        } else if (command.name === 'click' || command.name === 'coordinate_click') {
          return {ok: false, error: 'native_pointer_requires_macos11', observation_version: version()};
        } else if (command.name === 'press') {
          const target = el || document.activeElement || document.body;
          const key = String(input.key || '');
          const init = {key, code: key, bubbles: true, cancelable: true, composed: true};
          const proceed = target.dispatchEvent(new KeyboardEvent('keydown', init));
          target.dispatchEvent(new KeyboardEvent('keyup', init));
          if (proceed && key === 'Enter') {
            if (target.form) target.form.requestSubmit?.();
            else if (target.matches?.('button,[role=button],a[href]')) target.click();
          } else if (proceed && key === 'Tab') {
            const focusable = Array.from(document.querySelectorAll('a[href],button,input,textarea,select,[tabindex]'))
              .filter(x => rendered(x) && !x.disabled && x.tabIndex >= 0);
            const at = focusable.indexOf(target); focusable[(at + 1) % Math.max(1, focusable.length)]?.focus();
          } else if (proceed && (key === ' ' || key === 'Space')
                     && target.matches?.('button,[role=button],input[type=checkbox],input[type=radio]')) target.click();
          else if (key === 'Escape') target.blur?.();
        } else if (command.name === 'type') {
          // 原生按键映射不了时的兜底:execCommand 走的是编辑器路径,会正常发 beforeinput/input
          const target = el || document.activeElement || document.body;
          if (sensitive(target)) return {ok: false, error: 'sensitive_field_needs_user'};
          target.focus?.();
          const text = String(input.text ?? '');
          let inserted = false;
          try { inserted = document.execCommand('insertText', false, text); } catch (_) {}
          if (!inserted) {
            if (!('value' in target)) return {ok: false, error: 'type_target_not_editable'};
            const proto = target instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            const next = String(target.value || '') + text;
            if (setter) setter.call(target, next); else target.value = next;
            fire(target, 'input'); fire(target, 'change');
          }
        } else if (command.name === 'hover') {
          return {ok: false, error: 'native_pointer_requires_macos11', observation_version: version()};
        } else if (command.name === 'scroll') {
          if (el) el.scrollIntoView({block: 'center', inline: 'nearest'});
          else window.scrollBy({left: Number(input.x || 0), top: Number(input.y || 0), behavior: 'instant'});
        } else {
          return {ok: false, error: 'unsupported_action'};
        }
        await new Promise(resolve => setTimeout(resolve, 250));
        const afterUrl = String(location.href), changes = [];
        if (afterUrl !== beforeUrl) changes.push('url_changed');
        if (state.rev !== beforeRev) changes.push('document_changed');
        const modal = snapshot({max_nodes: 200, structure: false}).modal_stack || [];
        if (modal.length) changes.push('dialog_opened');
        state.rev += 1;
        return {ok: true, output: {url: afterUrl, changes, requires_reinspect: changes.length > 0,
          modal_count: modal.length, action_executed: true,
          effect_verified: selectedCount === undefined ? false : true,
          outcome: selectedCount === undefined ? 'delivery_only' : 'effect_verified',
          ...(selectedCount === undefined ? {} : {selected_count: selectedCount,
            selected_values: selectedResult, condition_met: true})},
          observation_version: version()};
        """#
    }

    private func larkMessageReferenceScript() -> String {
        return #"""
        (() => {
          if (window.__desktronLarkReference) return;
          window.__desktronLarkReference = {status: 'waiting'};
          const visible = (el) => {
            if (!el || !el.isConnected) return false;
            const r = el.getBoundingClientRect(), s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
          };
          const composer = () => Array.from(document.querySelectorAll(
            "textarea,[contenteditable='true'],[role='textbox']"))
            .filter(visible)
            .filter((el) => {
              const r = el.getBoundingClientRect();
              return r.width > 120 && r.top > innerHeight * 0.45 && r.right > innerWidth * 0.55;
            })
            .sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom)[0];
          const paneFor = (input) => {
            if (!input) return null;
            const rows = [];
            for (let el = input; el && el !== document.body; el = el.parentElement) {
              const r = el.getBoundingClientRect();
              if (r.width >= 280 && r.height >= innerHeight * 0.55
                  && r.right >= innerWidth * 0.88 && r.left >= 40) rows.push({el, r});
            }
            rows.sort((a, b) => (a.r.width * a.r.height) - (b.r.width * b.r.height));
            return rows.length ? rows[0].el : null;
          };
          const focus = (pane) => {
            if (!pane) return false;
            let style = document.getElementById('__desktron_lark_reference_css');
            if (!style) {
              style = document.createElement('style');
              style.id = '__desktron_lark_reference_css';
              document.head.appendChild(style);
            }
            style.textContent = `
              .desktron-lark-reference-hidden{display:none!important;visibility:hidden!important;pointer-events:none!important}
              .desktron-focused-chat-pane{display:flex!important;flex:1 1 auto!important;width:100%!important;max-width:none!important;min-width:0!important}
            `;
            document.querySelectorAll('.desktron-focused-chat-pane').forEach((el) => {
              if (el !== pane) el.classList.remove('desktron-focused-chat-pane');
            });
            const pr = pane.getBoundingClientRect();
            Array.from(document.querySelectorAll('aside,nav,section,div'))
              .filter((el) => el !== pane && !el.contains(pane) && visible(el))
              .filter((el) => {
                const r = el.getBoundingClientRect();
                return r.left < pr.left && r.right <= pr.left + 12
                  && r.height > innerHeight * 0.45 && r.width >= 32;
              })
              .forEach((el) => el.classList.add('desktron-lark-reference-hidden'));
            pane.classList.add('desktron-focused-chat-pane');
            for (let el = pane.parentElement; el && el !== document.body; el = el.parentElement) {
              el.style.minWidth = '0'; el.style.maxWidth = 'none';
              el.style.flex = '1 1 auto'; el.style.width = '100%';
            }
            window.__desktronLarkReference.status = 'focused';
            return true;
          };
          let ticks = 0;
          const run = () => {
            ticks += 1;
            const pane = paneFor(composer());
            if (pane) focus(pane);
            if (ticks >= 120) clearInterval(window.__desktronLarkReference.timer);
          };
          window.__desktronLarkReference.timer = setInterval(run, 500);
          run();
        })();
        """#
    }

    private func larkReferenceContextScript() -> String {
        return #"""
        (() => {
          const pane = document.querySelector('.desktron-focused-chat-pane');
          if (!pane || !pane.isConnected) return {ok:false,error:'chat_not_focused'};
          const text = String(pane.innerText || pane.textContent || '')
            .replace(/\r\n?/g, '\n').replace(/[^\S\n]+/g, ' ')
            .replace(/ ?\n ?/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
          if (!text) return {ok:false,error:'chat_empty'};
          return {ok:true,title:String(document.title || '飞书聊天').slice(0,240),
            url:String(location.href || '').split('#')[0].slice(0,2000),text:text.slice(0,60000),
            truncated:text.length > 60000};
        })();
        """#
    }

    private func sendLarkReferenceContext(requestId: String, ok: Bool,
                                          data: [String: Any]? = nil, error: String = "") {
        var payload: [String: Any] = ["requestId": requestId, "ok": ok]
        if let data = data { payload["data"] = data }
        if !error.isEmpty { payload["error"] = error }
        guard let bytes = try? JSONSerialization.data(withJSONObject: payload),
              let text = String(data: bytes, encoding: .utf8) else { return }
        web.evaluateJavaScript("window.__larkRefContextResult && window.__larkRefContextResult(\(text))",
                               completionHandler: nil)
    }

    private func readLarkReferenceContext(id: String, requestId: String) {
        guard !requestId.isEmpty, let view = browsers[id],
              AppDelegate.isLarkMessageReferenceMode(browserModes[id] ?? "") else {
            sendLarkReferenceContext(requestId: requestId, ok: false,
                                     error: "飞书原页尚未准备好")
            return
        }
        view.evaluateJavaScript(larkReferenceContextScript()) { [weak self] result, error in
            guard let self = self else { return }
            if let error = error {
                self.sendLarkReferenceContext(requestId: requestId, ok: false,
                                              error: error.localizedDescription)
                return
            }
            guard let row = result as? [String: Any], row["ok"] as? Bool == true else {
                let code = (result as? [String: Any])?["error"] as? String ?? "chat_unavailable"
                let message = code == "chat_not_focused"
                    ? "飞书聊天尚未加载完成，请稍后重试" : "当前窗口没有可读取的聊天正文"
                self.sendLarkReferenceContext(requestId: requestId, ok: false, error: message)
                return
            }
            self.sendLarkReferenceContext(requestId: requestId, ok: true, data: [
                "title": row["title"] as? String ?? "飞书聊天",
                "url": row["url"] as? String ?? "",
                "text": row["text"] as? String ?? "",
                "truncated": row["truncated"] as? Bool ?? false,
            ])
        }
    }

    private func larkSelectChatScript(chatName: String, tagName: String, clickResult: Bool) -> String {
        let cname = jsEsc(chatName)
        let tname = jsEsc(tagName)
        let shouldClickResult = clickResult ? "true" : "false"
        return """
        (() => {
          const targetChat = "\(cname)";
          const targetTag = "\(tname)";
          const clickResult = \(shouldClickResult);
          if (!targetChat) return;
          const norm = (s) => String(s || "").replace(/\\s+/g, " ").trim();
          const compact = (s) => norm(s).replace(/[.。…·\\-_/\\\\\\s]/g, "").toLowerCase();
          const elementText = (el) => {
            if (!el) return "";
            return [
              el.innerText,
              el.textContent,
              el.getAttribute && el.getAttribute("aria-label"),
              el.getAttribute && el.getAttribute("title"),
              el.getAttribute && el.getAttribute("data-title"),
              el.getAttribute && el.getAttribute("data-tooltip")
            ].map(norm).filter(Boolean).join(" ");
          };
          const textMatches = (haystack, needle) => {
            const h = norm(haystack);
            const n = norm(needle);
            if (!h || !n) return false;
            if (h.includes(n) || n.includes(h)) return true;
            const ch = compact(h);
            const cn = compact(n);
            return ch.length >= 3 && cn.length >= 3 && (ch.includes(cn) || cn.includes(ch) || cn.startsWith(ch.slice(0, Math.min(8, ch.length))));
          };
          const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          const rectOk = (r) => r && r.width > 0 && r.height > 0;
          const visible = (el) => {
            if (!el || !(el instanceof Element)) return false;
            const r = el.getBoundingClientRect();
            const st = getComputedStyle(el);
            return rectOk(r) && st.visibility !== "hidden" && st.display !== "none" && r.bottom > 0 && r.top < innerHeight;
          };
          const score = (el, text, preferFeed) => {
            const r = el.getBoundingClientRect();
            let s = Math.abs(elementText(el).length - text.length);
            if (preferFeed && el.closest("[data-feed-id]")) s -= 1000;
            if (r.left > innerWidth * 0.65) s += 500;
            if (r.width > innerWidth * 0.75) s += 300;
            return s + Math.max(0, r.left);
          };
          const clickable = (el) => {
            if (!el) return null;
            const direct = el.closest("[data-feed-id],button,[role='button'],[role='tab'],[role='treeitem'],[role='listitem'],li,a");
            if (direct) return direct;
            let best = el;
            for (let cur = el.parentElement, depth = 0; cur && cur !== document.body && depth < 5; depth++, cur = cur.parentElement) {
              const r = cur.getBoundingClientRect();
              if (rectOk(r) && r.left < innerWidth * 0.45 && r.width >= 80 && r.width <= 420 && r.height >= 20 && r.height <= 72) best = cur;
            }
            return best;
          };
          const byText = (text, preferFeed = false) => {
            const needle = norm(text);
            if (!needle) return null;
            const nodes = Array.from(document.querySelectorAll("[data-feed-id],button,[role='button'],[role='tab'],[role='treeitem'],a,span,div"))
              .filter(visible)
              .filter((el) => textMatches(elementText(el), needle));
            nodes.sort((a, b) => score(a, needle, preferFeed) - score(b, needle, preferFeed));
            return nodes.length ? clickable(nodes[0]) : null;
          };
          const fire = (el) => {
            if (!el) return false;
            el.scrollIntoView({block: "center", inline: "nearest"});
            const r = el.getBoundingClientRect();
            const init = {bubbles: true, cancelable: true, view: window, clientX: r.left + Math.min(24, r.width / 2), clientY: r.top + Math.min(18, r.height / 2)};
            for (const type of ["pointerover", "pointerdown", "pointerup"]) {
              try { el.dispatchEvent(new PointerEvent(type, init)); } catch {}
            }
            for (const type of ["mouseover", "mousedown", "mouseup", "click"]) el.dispatchEvent(new MouseEvent(type, init));
            try { el.click(); } catch {}
            return true;
          };
          const dismissDesktopNotificationBanner = () => {
            const bannerRe = /desktop notifications|Enable desktop notifications|开启桌面通知|桌面通知/i;
            const banners = Array.from(document.querySelectorAll("div,section,aside"))
              .filter(visible)
              .filter((el) => {
                const r = el.getBoundingClientRect();
                if (r.top > Math.max(140, innerHeight * 0.16) || r.height > 140 || r.width < innerWidth * 0.45) return false;
                return bannerRe.test(elementText(el));
              })
              .sort((a, b) => {
                const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
                return (ar.top - br.top) || ((br.width * br.height) - (ar.width * ar.height));
              });
            const banner = banners[0];
            if (!banner) return false;
            let style = document.getElementById("__desktron_lark_banner_css");
            if (!style) {
              style = document.createElement("style");
              style.id = "__desktron_lark_banner_css";
              document.head.appendChild(style);
            }
            style.textContent = ".desktron-hidden-notification-banner{display:none!important;visibility:hidden!important;pointer-events:none!important;}";
            const br = banner.getBoundingClientRect();
            const closeTargets = Array.from(banner.querySelectorAll("button,[role='button'],[aria-label],svg,path,span,div"))
              .filter(visible)
              .map((el) => ({el, r: el.getBoundingClientRect(), text: elementText(el)}))
              .filter(({r, text}) => r.left > br.left + br.width * 0.72 && r.top >= br.top && r.bottom <= br.bottom + 2 && (r.width <= 80 || /close|关闭|dismiss/i.test(text)))
              .sort((a, b) => b.r.left - a.r.left);
            const target = closeTargets[0] && closeTargets[0].el;
            if (target) fire(target);
            else {
              const maybe = document.elementFromPoint(br.right - 28, br.top + Math.min(48, br.height / 2));
              if (maybe) fire(maybe);
            }
            banner.classList.add("desktron-hidden-notification-banner");
            return true;
          };
          const leftScrollers = () => Array.from(document.querySelectorAll("div,aside,section"))
            .filter((el) => {
              if (!visible(el)) return false;
              const r = el.getBoundingClientRect();
              return r.left < innerWidth * 0.65 && r.width >= 160 && r.width <= 720 && el.scrollHeight > el.clientHeight + 80;
            })
            .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
          const findWithScroll = async (text, preferFeed) => {
            let el = byText(text, preferFeed);
            if (el) return el;
            for (const scroller of leftScrollers().slice(0, 4)) {
              const max = scroller.scrollHeight - scroller.clientHeight;
              const step = Math.max(180, Math.floor(scroller.clientHeight * 0.75));
              for (let y = 0; y <= max; y += step) {
                scroller.scrollTop = y;
                await sleep(120);
                el = byText(text, preferFeed);
                if (el) return el;
              }
            }
            return null;
          };
          const byTextInRoot = (root, text, preferFeed = false) => {
            const needle = norm(text);
            if (!root || !needle) return null;
            const nodes = Array.from(root.querySelectorAll("[data-feed-id],button,[role='button'],[role='listitem'],li,a,span,div"))
              .filter(visible)
              .filter((el) => textMatches(elementText(el), needle));
            nodes.sort((a, b) => score(a, needle, preferFeed) - score(b, needle, preferFeed));
            return nodes.length ? clickable(nodes[0]) : null;
          };
          const feedListContainers = () => Array.from(document.body.querySelectorAll("section,aside,div"))
            .filter(visible)
            .filter((el) => {
              const r = el.getBoundingClientRect();
              if (r.left < 280 || r.left > innerWidth * 0.62 || r.width < 180 || r.width > 620 || r.height < innerHeight * 0.45) return false;
              const txt = elementText(el);
              const feedCount = el.querySelectorAll("[data-feed-id],[role='listitem'],li").length;
              return feedCount >= 2 || /消息|Chats|Yesterday|AM|PM|\\d{1,2}:\\d{2}/.test(txt);
            })
            .sort((a, b) => {
              const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
              const as = a.querySelectorAll("[data-feed-id],[role='listitem'],li").length;
              const bs = b.querySelectorAll("[data-feed-id],[role='listitem'],li").length;
              return (bs * 10000 + br.height) - (as * 10000 + ar.height);
            });
          const findChatInFeed = async (text) => {
            for (let attempt = 0; attempt < 24; attempt++) {
              const containers = feedListContainers().slice(0, 3);
              for (const root of containers) {
                let el = byTextInRoot(root, text, true);
                if (el) return el;
                const max = Math.max(0, root.scrollHeight - root.clientHeight);
                const step = Math.max(180, Math.floor(root.clientHeight * 0.75));
                for (let y = 0; y <= max; y += step) {
                  root.scrollTop = y;
                  await sleep(120);
                  el = byTextInRoot(root, text, true);
                  if (el) return el;
                }
              }
              await sleep(500);
            }
            return null;
          };
          const textInputs = (root = document) => Array.from(root.querySelectorAll("input,textarea,[contenteditable='true'],[role='textbox']"))
            .filter(visible)
            .sort((a, b) => {
              const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
              return (br.width - ar.width) || (ar.top - br.top);
            });
          const globalSearchInputs = () => {
            const active = document.activeElement;
            const activeList = active && visible(active) ? [active] : [];
            return activeList.concat(textInputs())
              .filter((el, idx, arr) => arr.indexOf(el) === idx)
              .filter((el) => {
                const r = el.getBoundingClientRect();
                return r.top < Math.max(180, innerHeight * 0.28) && r.width > Math.min(360, innerWidth * 0.28);
              });
          };
          const inputValue = (el, value) => {
            if (!el) return false;
            el.focus();
            try {
              if ("value" in el) {
                const proto = Object.getPrototypeOf(el);
                const desc = Object.getOwnPropertyDescriptor(proto, "value");
                if (desc && desc.set) desc.set.call(el, value);
                else el.value = value;
              } else {
                el.textContent = value;
              }
              el.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: value}));
            } catch {
              try { el.value = value; el.dispatchEvent(new Event("input", {bubbles: true})); } catch {}
            }
            el.dispatchEvent(new Event("change", {bubbles: true}));
            return true;
          };
          const pressEnterGlobalSearch = (input) => {
            const init = {key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true};
            for (const target of [document.activeElement, input, document.body, document, window].filter(Boolean)) {
              try { target.dispatchEvent(new KeyboardEvent("keydown", init)); } catch {}
              try { target.dispatchEvent(new KeyboardEvent("keypress", init)); } catch {}
              try { target.dispatchEvent(new KeyboardEvent("keyup", init)); } catch {}
            }
            return true;
          };
          const openGlobalSearch = async () => {
            const keyInit = {key: "k", code: "KeyK", metaKey: true, bubbles: true, cancelable: true};
            for (const target of [document.activeElement, document.body, document, window].filter(Boolean)) {
              try { target.dispatchEvent(new KeyboardEvent("keydown", keyInit)); } catch {}
            }
            await sleep(500);
            let input = globalSearchInputs()[0];
            if (input) return input;
            const searchButton = byText("Search", false) || byText("搜索", false);
            if (searchButton) { fire(searchButton); await sleep(500); }
            input = globalSearchInputs()[0];
            return input || null;
          };
          const searchDialogRoot = (input) => {
            if (!input) return document.body;
            const roots = [];
            for (let cur = input.parentElement; cur && cur !== document.body; cur = cur.parentElement) {
              const r = cur.getBoundingClientRect();
              if (!rectOk(r)) continue;
              if (r.width > innerWidth * 0.35 && r.height > innerHeight * 0.25) roots.push({el: cur, r, text: elementText(cur)});
            }
            roots.sort((a, b) => {
              const am = /群组|群聊|Groups|Chats|云文档|Docs|筛选|Filter/.test(a.text) ? 1 : 0;
              const bm = /群组|群聊|Groups|Chats|云文档|Docs|筛选|Filter/.test(b.text) ? 1 : 0;
              return (bm - am) || ((b.r.width * b.r.height) - (a.r.width * a.r.height));
            });
            return roots.length ? roots[0].el : document.body;
          };
          const clickGroupSearchTab = async (root) => {
            for (const label of ["群组", "群聊", "Groups", "Group Chats"]) {
              const tab = byTextInRoot(root, label, false);
              if (tab) { fire(tab); await sleep(350); return true; }
            }
            return false;
          };
          const findSearchResult = (root, text) => {
            const needle = norm(text);
            const searchResultClickable = (el) => {
              if (!el) return null;
              const direct = el.closest("[role='option'],[role='listitem'],li,a,button");
              if (direct) return direct;
              let best = el;
              for (let cur = el.parentElement, depth = 0; cur && cur !== document.body && depth < 8; depth++, cur = cur.parentElement) {
                const r = cur.getBoundingClientRect();
                if (!rectOk(r)) continue;
                if (r.width >= 180 && r.height >= 36 && r.height <= 120 && r.left < innerWidth * 0.92) best = cur;
              }
              return best;
            };
            const nodes = Array.from(root.querySelectorAll("[role='option'],[role='listitem'],li,a,button,div,span"))
              .filter(visible)
              .filter((el) => {
                if (el.matches && el.matches("input,textarea,[contenteditable='true'],[role='textbox']")) return false;
                const r = el.getBoundingClientRect();
                if (r.top < 110 || r.height < 22 || r.width < 80) return false;
                return textMatches(elementText(el), needle);
              });
            nodes.sort((a, b) => score(a, needle, false) - score(b, needle, false));
            return nodes.length ? searchResultClickable(nodes[0]) : null;
          };
          const searchDialogOpen = (text) => {
            return globalSearchInputs().some((input) => {
              const value = "value" in input ? input.value : input.textContent;
              if (!textMatches(value, text)) return false;
              const root = searchDialogRoot(input);
              return /Search results|搜索结果|to select|筛选|群组|群聊|Groups/.test(elementText(root));
            });
          };
          const waitForSearchDialogClosed = async (text) => {
            for (let i = 0; i < 40; i++) {
              if (!searchDialogOpen(text)) return true;
              await sleep(250);
            }
            return false;
          };
          const findChatPane = (text) => {
            const candidates = Array.from(document.querySelectorAll("main,section,article,div"))
              .filter(visible)
              .map((el) => ({el, r: el.getBoundingClientRect(), text: elementText(el)}))
              .filter(({r, text: paneText}) => {
                if (r.left < 120 || r.right < innerWidth * 0.72) return false;
                if (r.width < innerWidth * 0.28 || r.height < innerHeight * 0.55) return false;
                if (/Search results|搜索结果|to select|筛选/.test(paneText)) return false;
                return textMatches(paneText, text) || /Aa|Send|发送|Reply|回复|Have a great day|Set off/.test(paneText);
              });
            candidates.sort((a, b) => {
              const ah = textMatches(a.text, text) ? 1 : 0;
              const bh = textMatches(b.text, text) ? 1 : 0;
              return (bh - ah) || (a.r.left - b.r.left) || ((b.r.width * b.r.height) - (a.r.width * a.r.height));
            });
            return candidates.length ? candidates[0].el : null;
          };
          const applyChatFocus = (pane) => {
            if (!pane) return false;
            let style = document.getElementById("__desktron_lark_focus_css");
            if (!style) {
              style = document.createElement("style");
              style.id = "__desktron_lark_focus_css";
              document.head.appendChild(style);
            }
            style.textContent = `
              .desktron-hidden-left-panel{display:none!important;visibility:hidden!important;pointer-events:none!important;}
              .desktron-focused-chat-pane{flex:1 1 auto!important;width:100%!important;max-width:none!important;min-width:0!important;}
            `;
            const paneRect = pane.getBoundingClientRect();
            const cutoff = Math.max(120, Math.min(innerWidth * 0.72, paneRect.left + 8));
            const panels = Array.from(document.querySelectorAll("aside,nav,section,div"))
              .filter((el) => el !== pane && !el.contains(pane) && visible(el))
              .filter((el) => {
                const r = el.getBoundingClientRect();
                if (r.left < -8 || r.right > cutoff) return false;
                if (r.width < 32 || r.width > Math.max(760, cutoff + 16)) return false;
                return r.height > innerHeight * 0.42 && r.top < innerHeight * 0.38;
              });
            panels.forEach((el) => el.classList.add("desktron-hidden-left-panel"));
            pane.classList.add("desktron-focused-chat-pane");
            for (let cur = pane.parentElement; cur && cur !== document.body; cur = cur.parentElement) {
              const r = cur.getBoundingClientRect();
              if (r.right > innerWidth * 0.72 && r.width > innerWidth * 0.32) {
                cur.style.minWidth = "0";
                cur.style.maxWidth = "none";
                cur.style.flex = "1 1 auto";
                cur.style.width = "100%";
              }
            }
            window.__desktronLarkSelectChat.status = "focused_chat_window";
            return true;
          };
          const focusMessengerChat = async (text) => {
            window.__desktronLarkSelectChat.status = "waiting_chat_window";
            await waitForSearchDialogClosed(text);
            dismissDesktopNotificationBanner();
            for (let i = 0; i < 24; i++) {
              const pane = findChatPane(text);
              if (pane && applyChatFocus(pane)) {
                if (window.__desktronLarkFocusTimer) clearInterval(window.__desktronLarkFocusTimer);
                let ticks = 0;
                window.__desktronLarkFocusTimer = setInterval(() => {
                  ticks += 1;
                  dismissDesktopNotificationBanner();
                  const livePane = findChatPane(text);
                  if (livePane) applyChatFocus(livePane);
                  if (ticks >= 24) clearInterval(window.__desktronLarkFocusTimer);
                }, 500);
                return true;
              }
              await sleep(250);
            }
            return false;
          };
          const selectChatByGlobalSearch = async (text) => {
            window.__desktronLarkSelectChat.status = "opening_global_search";
            dismissDesktopNotificationBanner();
            const input = await openGlobalSearch();
            if (!input) { window.__desktronLarkSelectChat.status = "search_input_not_found"; return false; }
            const root = searchDialogRoot(input);
            await clickGroupSearchTab(root);
            inputValue(input, text);
            await sleep(900);
            if (!clickResult) {
              window.__desktronLarkSelectChat.status = "search_results_ready";
              return true;
            }
            for (let i = 0; i < 60; i++) {
              window.__desktronLarkSelectChat.status = "polling_search_results";
              const liveRoot = searchDialogRoot(globalSearchInputs()[0] || input);
              const result = findSearchResult(liveRoot, text) || findSearchResult(root, text) || findSearchResult(document.body, text);
              if (result) {
                fire(result);
                window.__desktronLarkSelectChat.status = "clicked_global_search";
                await sleep(120);
                await pressEnterGlobalSearch(input);
                window.__desktronLarkSelectChat.status = "selected_global_search_result";
                await focusMessengerChat(text);
                return true;
              }
              await sleep(500);
            }
            try {
              pressEnterGlobalSearch(input);
              window.__desktronLarkSelectChat.status = "pressed_enter_global_search";
              await focusMessengerChat(text);
              return true;
            } catch {}
            window.__desktronLarkSelectChat.status = "global_search_failed_fallback_tag";
            try { document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", code: "Escape", bubbles: true, cancelable: true})); } catch {}
            return false;
          };
          const run = async () => {
            window.__desktronLarkSelectChat = {targetChat, targetTag, startedAt: Date.now(), status: "running"};
            for (let i = 0; i < 80; i++) {
              if (norm(document.body && document.body.innerText).length > 80) break;
              await sleep(250);
            }
            dismissDesktopNotificationBanner();
            let selected = await selectChatByGlobalSearch(targetChat);
            if (!clickResult) return;
            if (!selected) {
              if (targetTag) {
                const tag = await findWithScroll(targetTag, false);
                if (tag) { fire(tag); await sleep(1200); }
                else { window.__desktronLarkSelectChat.status = "tag_not_found"; return; }
              }
              const chat = await findChatInFeed(targetChat);
              if (chat) {
                fire(chat);
                selected = true;
                window.__desktronLarkSelectChat.status = "clicked_tag_feed";
              }
            }
            if (selected) {
              const focused = await focusMessengerChat(targetChat);
              if (!focused) window.__desktronLarkSelectChat.status = "selected_no_collapse";
            } else {
              window.__desktronLarkSelectChat.status = "chat_not_found";
            }
          };
          run().catch((err) => { window.__desktronLarkSelectChat = {targetChat, targetTag, status: "error", error: String(err && err.message || err)}; });
        })();
        """
    }

    private func applyLarkSelectChat(_ wv: WKWebView, chatName: String, tagName: String, clickResult: Bool) {
        wv.evaluateJavaScript(
            larkSelectChatScript(chatName: chatName, tagName: tagName, clickResult: clickResult),
            completionHandler: nil)
    }

    private func num(_ a: Any?) -> CGFloat { CGFloat((a as? NSNumber)?.doubleValue ?? 0) }
    func applyRect(_ v: WKWebView, _ rect: Any?) {
        guard let r = rect as? [String: Any], let host = v.superview else { return }
        (v as? BrowserWebView)?.passThroughLeft = num(r["passThroughLeft"])
        v.frame = NSRect(x: num(r["x"]), y: host.bounds.height - (num(r["y"]) + num(r["h"])),
                         width: num(r["w"]), height: num(r["h"]))   // 视口左上 → AppKit 左下
    }

    func bringTagsToFront() {
        if let rail = tagRail, let host = web.superview { host.addSubview(rail, positioned: .above, relativeTo: nil) }
    }

    // 悬浮标签条:JS 传来每个标签的视口矩形 + 字母 + 是否激活,原生绘制成毛玻璃 chip 浮在浏览器之上
    func renderTags(_ items: [[String: Any]]) {
        tagRail?.removeFromSuperview(); tagRail = nil
        guard !items.isEmpty, let host = web.superview else { return }
        let rail = PassthroughView(frame: host.bounds)
        rail.autoresizingMask = [.width, .height]
        for it in items {
            let w = num(it["w"]), h = num(it["h"])   // 圆直径;容器加 5px 右上 margin 容纳悬出的关闭角标
            let chip = ChipView(frame: NSRect(x: num(it["x"]), y: host.bounds.height - (num(it["y"]) + h), width: w + 5, height: h + 5))
            chip.tagId = it["id"] as? String ?? ""
            chip.onClick = { [weak self] id in self?.tagEvent("__tagClick", id) }
            chip.onClose = { [weak self] id in self?.tagEvent("__tagClose", id) }
            chip.build(ch: it["ch"] as? String ?? "", active: (it["active"] as? NSNumber)?.boolValue ?? false, size: w)
            rail.addSubview(chip)
        }
        host.addSubview(rail, positioned: .above, relativeTo: nil)
        tagRail = rail
    }
    private func tagEvent(_ fn: String, _ id: String) {
        web.evaluateJavaScript("window.\(fn) && window.\(fn)(\"\(jsEsc(id))\")")
    }

    private func jsEsc(_ s: String) -> String {
        var r = ""
        for c in s.unicodeScalars {
            switch c {
            case "\\": r += "\\\\"; case "\"": r += "\\\""
            case "\n": r += "\\n"; case "\r": r += "\\r"
            case "\u{2028}": r += "\\u2028"; case "\u{2029}": r += "\\u2029"
            default: r.unicodeScalars.append(c)
            }
        }
        return r
    }

    private func observeBrowserState(_ view: WKWebView, id: String) {
        // SPA 的 pushState/replaceState、锚点和历史跳转不一定触发导航 delegate。
        // 观察原生真实状态；下一轮主线程读取，避免 URL 已变但历史按钮尚未更新。
        let changed: (WKWebView) -> Void = { [weak self] view in
            DispatchQueue.main.async { [weak self, weak view] in
                guard let self = self, let view = view, self.browsers[id] === view else { return }
                self.reportBrowser(view, phase: "state")
            }
        }
        browserObservations[id] = [
            view.observe(\.url, options: [.new]) { view, _ in changed(view) },
            view.observe(\.title, options: [.new]) { view, _ in changed(view) },
            view.observe(\.canGoBack, options: [.new]) { view, _ in changed(view) },
            view.observe(\.canGoForward, options: [.new]) { view, _ in changed(view) },
        ]
    }

    private func reportBrowser(_ wv: WKWebView, phase: String = "") {   // 回传标题/地址/导航态与加载阶段
        guard let id = browsers.first(where: { $0.value === wv })?.key else { return }
        let js = "window.__browserEvent && window.__browserEvent({id:\"\(jsEsc(id))\",title:\"\(jsEsc(wv.title ?? ""))\","
            + "url:\"\(jsEsc(wv.url?.absoluteString ?? ""))\",canBack:\(wv.canGoBack),canFwd:\(wv.canGoForward),"
            + "phase:\"\(jsEsc(phase))\"})"
        web.evaluateJavaScript(js)
    }
    // 主页重载/导航(切引擎、切后端、手动刷新都是 location.reload / location.href):拥有这些
    // 原生面板的 JS 状态会被清掉,面板若还显示着,就是一张盖死新页面且 JS 拆不掉的孤儿。
    // 2026-09-04 之前这里连视图带租约一并销毁。代价是切一次引擎,所有内置浏览器页面被回收,
    // 重开后从头加载;正借这些页面干活的另一个引擎,手里的租约与节点 id 一起作废,下一步
    // inspect/act 直接报错(native-browser.log 里成串的 discard reason=host_navigation)。
    // 视图本身挂在窗口上(web.superview),不是宿主 webview 的子视图,物理上挺得过宿主重载;
    // 前端重载后仍按同一个 id 发 open(标签 id 是 localStorage 里持久化的序号,Agent 自开页
    // 的 id 由 Runtime 给定),openBrowser 会走 reuse 分支原样接回,不重新加载。
    // 所以这里只做两件事:全部藏起来,不让任何一张盖住正在重载的宿主页;拆掉与 HTML
    // 强耦合的标签条。视图、模式、最近使用时间、epoch、两类租约全部保留。前端此后不再
    // 认领的视图,由 trimBrowserViews 的 15 分钟空闲回收兜底——与平时切走一张标签同命。
    private func parkNativeOverlays() {
        for (id, v) in browsers {
            browserViewLog(id, "park", "reason=host_navigation")
            v.isHidden = true
        }
        tagRail?.removeFromSuperview(); tagRail = nil
    }

    /// 宿主页重载后,前端的 browserControlState 是空的(它只活在内存里),而原生这边的用户
    /// 委托租约在 parkNativeOverlays 里保留了下来。不补这一手,页面上的「Agent」按钮会显示成
    /// 未委托,人再点一次就是对一张已租出的页面重复委托。挑 interactive 这个启动里程碑回放:
    /// 此时 app-browser.js 已装好 window.__browserControlEvent;再早一步会打在空函数上被吞掉。
    private func replayAppControlState() {
        for (id, lease) in appControlLeases {
            let paused = appControlPaused.contains(id)
            sendAppControlEvent(id: id, type: paused ? "paused" : "delegated",
                                leaseId: lease, error: paused ? (appControlPauseReason[id] ?? "") : "",
                                sessionId: id == deskControlResourceId ? deskControlSessionId : "")
        }
    }
    // 内置浏览器里 WKWebView 自己能处理的协议;其余(lark://、feishu://、mailto: 等)
    // 交给 WebKit 只会静默导航失败。2026-09-24 实见:飞书条件访问拦截页的
    // 「前往飞书客户端授权」跳 lark:// 拉客户端,原先一律 .allow,客户端从未被唤起。
    static let webSchemes: Set<String> = ["http", "https", "about", "blob", "data", "file",
                                          "javascript", "ws", "wss"]
    // 与 Desk 同机常驻、已知安全的客户端协议:直接交系统打开,不再二次确认
    static let trustedAppSchemes: Set<String> = ["lark", "feishu", "larksuite", "mailto", "tel"]
    // 飞书 applink 通用链接。Safari/Chrome 由系统转给客户端;WKWebView 不支持通用链接,
    // 条件访问页把它塞进隐藏 iframe 当网页加载,客户端永远拉不起。飞书前端在 UC/Firefox
    // 分支用的等价写法就是 https→lark 同路径,这里照做。
    static let feishuApplinkHosts: Set<String> = ["applink.feishu.cn", "applink.larksuite.com"]

    static func feishuClientURL(_ u: URL) -> URL? {
        guard (u.scheme ?? "").lowercased() == "https",
              AppDelegate.feishuApplinkHosts.contains((u.host ?? "").lowercased()),
              var c = URLComponents(url: u, resolvingAgainstBaseURL: false) else { return nil }
        c.scheme = "lark"
        return c.url
    }

    /// 内置浏览器面板的非网页协议交给系统打开。返回 true 表示已接管(调用方应取消导航)。
    /// Agent 自开的隔离标签不拉起本机应用;其余未知协议先问人,与 Safari 的行为一致。
    func handoffExternalScheme(_ original: URL, from wv: WKWebView) -> Bool {
        let u = AppDelegate.feishuClientURL(original) ?? original
        let scheme = (u.scheme ?? "").lowercased()
        if scheme.isEmpty || AppDelegate.webSchemes.contains(scheme) { return false }
        guard let id = browsers.first(where: { $0.value === wv })?.key else { return false }
        if appControlIsolated.contains(id) {
            browserViewLog(id, "external_scheme_blocked", "scheme=\(scheme) isolated=true")
            return true
        }
        guard NSWorkspace.shared.urlForApplication(toOpen: u) != nil else {
            browserViewLog(id, "external_scheme_unhandled", "scheme=\(scheme)")
            return true
        }
        if AppDelegate.trustedAppSchemes.contains(scheme) {
            browserViewLog(id, "external_scheme_open", "scheme=\(scheme)")
            NSWorkspace.shared.open(u)
            return true
        }
        let alert = NSAlert()
        alert.messageText = "网页请求打开外部应用"
        alert.informativeText = "当前网页想用「\(scheme):」打开本机应用。只在你信任该网页时允许。"
        alert.addButton(withTitle: "打开")
        alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { [weak self] result in
            let allowed = result == .alertFirstButtonReturn
            self?.browserViewLog(id, "external_scheme_prompt", "scheme=\(scheme) allowed=\(allowed)")
            if allowed { NSWorkspace.shared.open(u) }
        }
        return true
    }

    /// 在钥匙串里找服务器点名签发者的客户端身份证书。服务器没给签发者列表时不挑证书,
    /// 避免把无关身份(如开发者证书)出示给任意站点。
    static func clientIdentity(issuers: [Data]) -> SecIdentity? {
        if issuers.isEmpty { return nil }
        let query: [String: Any] = [
            kSecClass as String: kSecClassIdentity,
            kSecMatchIssuers as String: issuers,
            kSecMatchValidOnDate as String: Date(),
            kSecMatchLimit as String: kSecMatchLimitAll,
            kSecReturnRef as String: true,
        ]
        var out: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess,
              let items = out as? [Any], let first = items.first else { return nil }
        return (first as! SecIdentity)
    }

    // 飞书等企业站点用 TLS 客户端证书(mTLS)认可信设备。Safari/Chrome 会自动从钥匙串挑证书,
    // WKWebView 不会:宿主不实现这条质询,请求就不带证书,被条件访问判成未授权设备
    // (blockBy DEVICE_MDM_STATUS, needCertVerify)。主页面与 Agent 隔离标签不出示证书。
    func webView(_ wv: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                 completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodClientCertificate,
              wv !== web,
              let id = browsers.first(where: { $0.value === wv })?.key,
              !appControlIsolated.contains(id) else {
            completionHandler(.performDefaultHandling, nil); return
        }
        let host = challenge.protectionSpace.host
        guard let identity = AppDelegate.clientIdentity(
            issuers: challenge.protectionSpace.distinguishedNames ?? []) else {
            browserViewLog(id, "client_cert_none", "host=\(host)")
            completionHandler(.performDefaultHandling, nil); return
        }
        browserViewLog(id, "client_cert_use", "host=\(host)")
        completionHandler(.useCredential,
                          URLCredential(identity: identity, certificates: nil, persistence: .forSession))
    }

    // 主 webview 只加载它自己那一页。前端已经在捕获期拦了所有锚点,这里是兜底:
    // 漏掉一处(动态插入的 <a>、window.location 赋值)就会把整个应用换成一张后端的 404,
    // 界面里再没有返回入口,只能退出重开——2026-08-10 实见。内置浏览器面板不受这条限制。
    func webView(_ wv: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if wv !== web, let u = action.request.url, handoffExternalScheme(u, from: wv) {
            decisionHandler(.cancel); return
        }
        if wv !== web, action.targetFrame?.isMainFrame == true,
           let id = browsers.first(where: { $0.value === wv })?.key,
           let allowed = appControlAllowedMainFrameHosts[id], !allowed.isEmpty {
            guard let url = action.request.url,
                  url.scheme == "https",
                  let host = url.host?.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: ".")),
                  allowed.contains(host) else {
                browserViewLog(id, "navigation_blocked", action.request.url?.host ?? "missing_host")
                decisionHandler(.cancel); return
            }
        }
        guard wv === web, action.targetFrame?.isMainFrame == true else {
            decisionHandler(.allow); return
        }
        let url = action.request.url
        // 主页面生成的诊断文件走既有保存面板；下载不替换 Desk 的主页面。
        if #available(macOS 11.3, *), action.shouldPerformDownload, url?.scheme == "blob" {
            decisionHandler(.download); return
        }
        let isAppPage = (url?.path ?? "/") == "/" || (url?.path ?? "") == "/index.html"
        if action.navigationType == .linkActivated || !isAppPage {
            if let u = url, u.scheme == "http" || u.scheme == "https", !isAppPage,
               u.host != "127.0.0.1", u.host != "localhost" {
                NSWorkspace.shared.open(u)   // 外链交给系统浏览器,不在这一层里打开
            }
            decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }
    func webView(_ wv: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        guard browsers.values.contains(where: { $0 === wv }) else {
            decisionHandler(.allow); return
        }
        let disposition = ((response.response as? HTTPURLResponse)?
            .value(forHTTPHeaderField: "Content-Disposition") ?? "").lowercased()
        if !response.canShowMIMEType || disposition.contains("attachment") {
            decisionHandler(.download)
        } else { decisionHandler(.allow) }
    }
    @available(macOS 11.0, *)
    func webView(_ wv: WKWebView, navigationAction: WKNavigationAction,
                 didBecome download: WKDownload) {
        beginDownload(download, from: wv)
    }
    @available(macOS 11.0, *)
    func webView(_ wv: WKWebView, navigationResponse: WKNavigationResponse,
                 didBecome download: WKDownload) {
        beginDownload(download, from: wv)
    }
    @available(macOS 11.0, *)
    private func beginDownload(_ download: WKDownload, from view: WKWebView) {
        download.delegate = self
        if let id = appControlNeedsUser(view, reason: "file_download") {
            appControlDownloads[ObjectIdentifier(download)] = id
        }
    }
    @available(macOS 11.0, *)
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let key = ObjectIdentifier(download)
        let panel = NSSavePanel(); panel.nameFieldStringValue = suggestedFilename
        panel.prompt = "保存"; panel.message = "选择下载文件的保存位置"
        panel.beginSheetModal(for: window) { [weak self] result in
            completionHandler(result == .OK ? panel.url : nil)
            if let id = self?.appControlDownloads.removeValue(forKey: key) {
                self?.resumeAppControl(id: id)
            }
        }
    }
    @available(macOS 11.0, *)
    func downloadDidFinish(_ download: WKDownload) {
        appControlDownloads.removeValue(forKey: ObjectIdentifier(download))
    }
    @available(macOS 11.0, *)
    func download(_ download: WKDownload, didFailWithError error: Error,
                  resumeData: Data?) {
        if let id = appControlDownloads.removeValue(forKey: ObjectIdentifier(download)) {
            resumeAppControl(id: id)
        }
    }
    func webView(_ wv: WKWebView, didStartProvisionalNavigation nav: WKNavigation!) {
        reportBrowser(wv, phase: "start")
    }
    func webView(_ wv: WKWebView, didFinish nav: WKNavigation!) {
        if wv === web, wv.url?.host == "127.0.0.1" { startupLog("main navigation finished") }
        reportBrowser(wv, phase: "finish")
    }
    func webView(_ wv: WKWebView, didCommit nav: WKNavigation!) {
        if wv === web {
            if wv.url?.host == "127.0.0.1" { startupLog("main navigation committed") }
            parkNativeOverlays(); return
        }   // 主 webview 提交新导航 → 藏起原生面板,不销毁;前端重载后按 id 接回
        if let id = browsers.first(where: { $0.value === wv })?.key {
            appControlEpochs[id] = (appControlEpochs[id] ?? 0) + 1
        }
        reportBrowser(wv, phase: "commit")
    }
    func webView(_ wv: WKWebView, didFail nav: WKNavigation!, withError error: Error) {
        reportBrowser(wv, phase: "fail")
    }
    func webView(_ wv: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError error: Error) {
        reportBrowser(wv, phase: "fail")
    }
    // target=_blank / window.open:在内置浏览器里就地打开,不弹新窗(仅对内置浏览器面板生效,不影响主 UI)
    func webView(_ wv: WKWebView, createWebViewWith config: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if browsers.values.contains(where: { $0 === wv }), let u = action.request.url,
           !handoffExternalScheme(u, from: wv) { wv.load(URLRequest(url: u)) }
        return nil
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if sender !== window { return true }
        NSApp.terminate(nil)
        return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitApproved { return .terminateNow }
        if quitPending { return .terminateLater }
        quitPending = true
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(PORT)/workbench/runtime")!)
        request.timeoutInterval = 2
        URLSession.shared.dataTask(with: request) { data, response, _ in
            let status = (response as? HTTPURLResponse)?.statusCode
            let body = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let active = status == 200 ? body?["active_work"] as? Int : nil
            DispatchQueue.main.async {
                var proceed = true
                if active == nil || active! > 0 {
                    let alert = NSAlert()
                    alert.messageText = "确认退出 Desktron？"
                    alert.informativeText = active == nil
                        ? "暂时无法确认执行状态。退出会中断正在运行的回合；未确认的结果将在下次打开时核验，不会自动重跑。"
                        : "仍有 \(active!) 项工作正在执行。退出会请求中断；队列保留，重新打开后需手动继续。"
                    alert.addButton(withTitle: "继续等待")
                    alert.addButton(withTitle: "中断并退出")
                    proceed = alert.runModal() == .alertSecondButtonReturn
                }
                if !proceed {
                    self.quitPending = false
                    sender.reply(toApplicationShouldTerminate: false)
                    return
                }
                guard let supervisor = self.backendSupervisor else {
                    self.quitPending = false
                    self.quitApproved = true
                    sender.reply(toApplicationShouldTerminate: true)
                    return
                }
                supervisor.shutdown {
                    self.quitPending = false
                    self.quitApproved = true
                    sender.reply(toApplicationShouldTerminate: true)
                }
            }
        }.resume()
        return .terminateLater
    }

    func applicationWillTerminate(_ note: Notification) {
        caffeine?.terminate()
        // Only this shell's child. SIGTERM/parent-death cleanup owns adapter
        // cancellation; never pkill another Desk instance or UI server.
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }
}

func buildMenu() -> NSMenu {
    let main = NSMenu()
    // App 菜单(退出)
    let appItem = NSMenuItem(); main.addItem(appItem)
    let appMenu = NSMenu()
    appMenu.addItem(NSMenuItem(title: "退出 Desktron", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
    appItem.submenu = appMenu
    // 编辑菜单(剪贴板快捷键依赖它)
    let editItem = NSMenuItem(); main.addItem(editItem)
    let edit = NSMenu(title: "编辑")
    edit.addItem(NSMenuItem(title: "撤销", action: Selector(("undo:")), keyEquivalent: "z"))
    edit.addItem(NSMenuItem(title: "重做", action: Selector(("redo:")), keyEquivalent: "Z"))
    edit.addItem(NSMenuItem.separator())
    edit.addItem(NSMenuItem(title: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
    edit.addItem(NSMenuItem(title: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
    edit.addItem(NSMenuItem(title: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
    edit.addItem(NSMenuItem(title: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
    editItem.submenu = edit
    // 视图菜单(刷新)
    let viewItem = NSMenuItem(); main.addItem(viewItem)
    let view = NSMenu(title: "视图")
    view.addItem(NSMenuItem(title: "刷新", action: #selector(AppDelegate.reloadPage), keyEquivalent: "r"))
    viewItem.submenu = view
    return main
}

extension AppDelegate {
    @objc func reloadPage() {
        // 首页 no-store 保证拿到最新引用；内容变化会换指纹 URL，未变化资源继续命中缓存。
        web?.load(URLRequest(url: URL(string: URLSTR)!))
    }
}

// Must run before AppKit creates any application/window or touches live backend state.
if runUpdateLaunchContractCheckIfRequested() { exit(0) }
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.mainMenu = buildMenu()
app.run()
