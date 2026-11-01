import Cocoa
import ApplicationServices
import Darwin
import Security

// macOS 原生应用 L1 只读观察。
//
// 边界：
// - 只处理发行版 nativeApps 清单里的应用；窗口由人在原生选择框里选定，Agent 看不到列表；
// - 每次读取前后都复核 bundle、代码签名要求、pid、进程启动令牌与 CGWindowID；
// - 投影器只输出角色映射、开关/选中/可用状态，以及与清单固定标签完全相等时的令牌；
//   AX 原文、窗口标题、截图和坐标不离开本进程；
// - 人碰目标窗口立即上报 user_interaction，Runtime 推进 epoch 并作废在途读取；
// - 本文件不调用任何 AX 写接口，也不合成输入。

struct NativeAppSpec {
    let id: String
    let label: String
    let bundleID: String
    let codeRequirement: String
    let projector: String
    let fingerprint: String
    let allowedRoles: Set<String>
    let allowedSemantics: Set<String>
    let allowedValues: Set<String>
    let labelValues: [String: String]

    init?(_ raw: [String: Any]) {
        guard let id = raw["id"] as? String, !id.isEmpty,
              let bundle = raw["bundle_id"] as? String, !bundle.isEmpty,
              let requirement = raw["code_requirement"] as? String, !requirement.isEmpty,
              let projector = raw["projector"] as? String,
              let fingerprint = raw["manifest_fingerprint"] as? String, !fingerprint.isEmpty
        else { return nil }
        self.id = id
        self.label = raw["label"] as? String ?? id
        self.bundleID = bundle
        self.codeRequirement = requirement
        self.projector = projector
        self.fingerprint = fingerprint
        self.allowedRoles = Set(raw["allowed_roles"] as? [String] ?? [])
        self.allowedSemantics = Set(raw["allowed_semantics"] as? [String] ?? [])
        self.allowedValues = Set(raw["allowed_values"] as? [String] ?? [])
        self.labelValues = raw["label_values"] as? [String: String] ?? [:]
    }
}

struct NativeTarget {
    let leaseID: String
    let appID: String
    let sessionID: String
    let bundleID: String
    let pid: pid_t
    let startToken: String
    let windowID: CGWindowID
    let generation: String
    let spec: NativeAppSpec
}

enum NativeIdentity {
    /// 进程启动时间令牌；pid 复用后必然变化。
    static func startToken(_ pid: pid_t) -> String? {
        var info = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.stride)
        guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size) == size else { return nil }
        return String(format: "%llu.%06llu", info.pbi_start_tvsec, info.pbi_start_tvusec)
    }

    /// 以 pid 取运行中代码对象，按清单要求做签名校验；bundle ID 可仿冒，不单独作为身份。
    static func satisfies(pid: pid_t, requirement: String) -> Bool {
        var code: SecCode?
        let attrs = [kSecGuestAttributePid: NSNumber(value: pid)] as CFDictionary
        guard SecCodeCopyGuestWithAttributes(nil, attrs, [], &code) == errSecSuccess,
              let guest = code else { return false }
        var req: SecRequirement?
        guard SecRequirementCreateWithString(requirement as CFString, [], &req) == errSecSuccess,
              let parsed = req else { return false }
        return SecCodeCheckValidity(guest, [], parsed) == errSecSuccess
    }

    static func runningBundle(_ pid: pid_t) -> String? {
        guard let app = NSRunningApplication(processIdentifier: pid), !app.isTerminated else { return nil }
        return app.bundleIdentifier
    }

    /// 仅列普通层级窗口；不读取 kCGWindowName（需要屏幕录制权限，也不需要）。
    static func windows(pid: pid_t) -> [(id: CGWindowID, bounds: CGRect)] {
        let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
        guard let rows = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else {
            return []
        }
        return rows.compactMap { row in
            guard (row[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid,
                  (row[kCGWindowLayer as String] as? NSNumber)?.intValue == 0,
                  let number = (row[kCGWindowNumber as String] as? NSNumber)?.uint32Value,
                  let boundsDict = row[kCGWindowBounds as String] as? NSDictionary,
                  let bounds = CGRect(dictionaryRepresentation: boundsDict) else { return nil }
            return (CGWindowID(number), bounds)
        }
    }

    enum Check: String {
        case ok
        case processExited = "native_process_exited"
        case identityChanged = "native_identity_changed"
        case windowClosed = "native_window_closed"
    }

    static func verify(_ target: NativeTarget) -> Check {
        guard let bundle = runningBundle(target.pid) else { return .processExited }
        guard bundle == target.bundleID,
              startToken(target.pid) == target.startToken,
              satisfies(pid: target.pid, requirement: target.spec.codeRequirement)
        else { return .identityChanged }
        guard windows(pid: target.pid).contains(where: { $0.id == target.windowID }) else {
            return .windowClosed
        }
        return .ok
    }
}

enum NativeAX {
    private typealias GetWindowFn = @convention(c) (AXUIElement, UnsafeMutablePointer<CGWindowID>) -> AXError
    // 仅用于把 AX 窗口对应回人选定的 CGWindowID；不可用时回落到唯一几何匹配。
    private static let getWindow: GetWindowFn? = {
        guard let symbol = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "_AXUIElementGetWindow") else {
            return nil
        }
        return unsafeBitCast(symbol, to: GetWindowFn.self)
    }()

    static func attr(_ element: AXUIElement, _ name: String) -> AnyObject? {
        var value: AnyObject?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
        return value
    }

    private static func frame(_ element: AXUIElement) -> CGRect? {
        guard let pos = attr(element, kAXPositionAttribute), let size = attr(element, kAXSizeAttribute),
              CFGetTypeID(pos) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID() else { return nil }
        var point = CGPoint.zero, extent = CGSize.zero
        guard AXValueGetValue(pos as! AXValue, .cgPoint, &point),
              AXValueGetValue(size as! AXValue, .cgSize, &extent) else { return nil }
        return CGRect(origin: point, size: extent)
    }

    static func window(pid: pid_t, id: CGWindowID) -> AXUIElement? {
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 0.5)
        guard let list = attr(app, kAXWindowsAttribute) as? [AXUIElement] else { return nil }
        if let getWindow = getWindow {
            for element in list {
                var number: CGWindowID = 0
                if getWindow(element, &number) == .success, number == id { return element }
            }
            return nil
        }
        guard let bounds = NativeIdentity.windows(pid: pid).first(where: { $0.id == id })?.bounds else {
            return nil
        }
        let matches = list.filter { frame($0).map { $0.integral == bounds.integral } ?? false }
        return matches.count == 1 ? matches[0] : nil
    }

    /// 人选择窗口时在本机显示的标题；只用于本地选择框，不发送给 Runtime 或 Agent。
    static func localTitle(pid: pid_t, id: CGWindowID) -> String {
        guard let element = window(pid: pid, id: id),
              let title = attr(element, kAXTitleAttribute) as? String, !title.isEmpty else {
            return "窗口 \(id)"
        }
        return title
    }

    static let roleSemantics: [String: String] = [
        "AXWindow": "window", "AXGroup": "group", "AXScrollArea": "group",
        "AXButton": "button", "AXCheckBox": "toggle", "AXRadioButton": "option",
        "AXStaticText": "text", "AXCell": "list_item", "AXRow": "list_item",
        "AXTable": "list", "AXOutline": "list", "AXList": "list",
        "AXTabGroup": "tabs", "AXToolbar": "toolbar",
    ]
    static let maxNodes = 200
    static let maxDepth = 16

    /// 结构投影。只有白名单角色成为节点；其余元素只作为透明容器继续下探。
    /// 安全文本框整棵跳过；文字仅在与清单标签完全相等时转成令牌。
    static func project(window root: AXUIElement, spec: NativeAppSpec, deadline: TimeInterval)
        -> [String: Any]? {
        guard spec.projector == "ax-structural/v1" else { return nil }
        var nodes: [[String: Any]] = []
        var truncated = false
        var stack: [(AXUIElement, String, Int)] = [(root, "", 0)]
        while let (element, parent, depth) = stack.popLast() {
            if ProcessInfo.processInfo.systemUptime > deadline { truncated = true; break }
            // 默认单次 AX 调用可阻塞 6 秒；逐元素限到 0.5 秒，保证总时限可控。
            AXUIElementSetMessagingTimeout(element, 0.5)
            let role = attr(element, kAXRoleAttribute) as? String ?? ""
            let subrole = attr(element, kAXSubroleAttribute) as? String ?? ""
            if subrole == (kAXSecureTextFieldSubrole as String) || role == "AXTextField"
                || role == "AXTextArea" || role == "AXWebArea" { continue }
            var childParent = parent
            if let semantic = roleSemantics[role], spec.allowedRoles.contains(role),
               spec.allowedSemantics.contains(semantic) {
                if nodes.count >= maxNodes { truncated = true; break }
                let ref = "n\(nodes.count)"
                var node: [String: Any] = ["ref": ref, "role": role, "semantic": semantic]
                if !parent.isEmpty { node["parent"] = parent }
                if let value = projectedValue(element, semantic: semantic, spec: spec) {
                    node["value"] = value
                }
                if let enabled = attr(element, kAXEnabledAttribute) as? Bool { node["enabled"] = enabled }
                if let selected = attr(element, kAXSelectedAttribute) as? Bool { node["selected"] = selected }
                nodes.append(node)
                childParent = ref
            } else if nodes.isEmpty {
                // 根必须是窗口本身；角色不符说明绑定对象不对。
                return nil
            }
            guard depth < maxDepth else { truncated = true; continue }
            if let children = attr(element, kAXChildrenAttribute) as? [AXUIElement] {
                for child in children.reversed() { stack.append((child, childParent, depth + 1)) }
            }
        }
        guard !nodes.isEmpty else { return nil }
        return ["nodes": nodes, "truncated": truncated]
    }

    private static func projectedValue(_ element: AXUIElement, semantic: String,
                                       spec: NativeAppSpec) -> String? {
        if semantic == "toggle", let number = attr(element, kAXValueAttribute) as? NSNumber {
            let token = number.intValue == 0 ? "off" : "on"
            return spec.allowedValues.contains(token) ? token : nil
        }
        for name in [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute] {
            guard let text = attr(element, name) as? String,
                  let token = spec.labelValues[text.trimmingCharacters(in: .whitespacesAndNewlines)],
                  spec.allowedValues.contains(token) else { continue }
            return token
        }
        return nil
    }
}

final class NativeControlCoordinator {
    typealias Request = (_ path: String, _ method: String, _ body: [String: Any]?,
                         _ completion: @escaping ([String: Any]?, String?) -> Void) -> Void

    private let request: Request
    private let hostInstanceID: () -> String
    private let log: (String) -> Void
    private let emit: ([String: Any]) -> Void
    /// 让宿主立刻重报 Accessibility 与监测状态（带 monitoring_status）。
    var reportCapability: (() -> Void)?

    private var specs: [String: NativeAppSpec] = [:]
    private(set) var targets: [String: NativeTarget] = [:]
    private var pausedByUser: Set<String> = []
    private var idleWork: [String: DispatchWorkItem] = [:]
    private var inputMonitor: Any?
    private var terminateObserver: NSObjectProtocol?
    private var pollTimer: Timer?
    private var polling = false
    private var lastHeartbeat = Date.distantPast
    private var lastWindowCheck = Date.distantPast
    private var selecting = false
    private let axQueue = DispatchQueue(label: "desk.native-control.ax")

    init(request: @escaping Request, hostInstanceID: @escaping () -> String,
         log: @escaping (String) -> Void, emit: @escaping ([String: Any]) -> Void) {
        self.request = request
        self.hostInstanceID = hostInstanceID
        self.log = log
        self.emit = emit
    }

    /// 监测只在人发起委托到目标结束期间安装；平时不挂全局输入监听。
    var monitoringStatus: String {
        AXIsProcessTrusted() && inputMonitor != nil ? "ready" : "unavailable"
    }

    // ---- 前端入口 ----

    func handle(action: String, appID: String, sessionID: String) {
        switch action {
        case "list":
            loadSpecs { [weak self] in self?.emitList() }
        case "delegate":
            guard !appID.isEmpty, !sessionID.isEmpty else { return }
            beginDelegation(appID: appID, sessionID: sessionID)
        case "takeover", "resourceChanged":
            release(appID: appID, event: "host_released", notify: "idle")
        default:
            break
        }
    }

    private func emitList() {
        let apps = specs.values.sorted { $0.id < $1.id }.map { spec -> [String: Any] in
            let target = targets[spec.id]
            return ["app_id": spec.id, "label": spec.label,
                    "state": target == nil ? "idle"
                        : (pausedByUser.contains(spec.id) ? "paused" : "delegated"),
                    "session_id": target?.sessionID ?? ""]
        }
        emit(["type": "list", "apps": apps, "trusted": AXIsProcessTrusted()])
    }

    private func loadSpecs(_ done: @escaping () -> Void) {
        request("/workbench/app-control/host/desktop-apps", "GET", nil) { [weak self] result, error in
            guard let self = self else { return }
            if let error = error { self.log("native apps list failed: \(error)") }
            var next: [String: NativeAppSpec] = [:]
            for raw in result?["apps"] as? [[String: Any]] ?? [] {
                if let spec = NativeAppSpec(raw) { next[spec.id] = spec }
            }
            self.specs = next
            done()
        }
    }

    // ---- 人选择窗口 ----

    private func beginDelegation(appID: String, sessionID: String) {
        guard !selecting else { return }
        guard AXIsProcessTrusted() else {
            emit(["type": "error", "app_id": appID, "session_id": sessionID,
                  "error": "Desk 未获得辅助功能权限；请在系统设置中手动授权后重试"])
            return
        }
        selecting = true
        loadSpecs { [weak self] in
            guard let self = self else { return }
            guard let spec = self.specs[appID] else {
                self.selecting = false
                self.emit(["type": "error", "app_id": appID, "session_id": sessionID,
                           "error": "应用不在内置原生清单中"])
                return
            }
            self.installMonitor()
            self.reportCapability?()
            // 等能力回报落库后再登记，避免 Runtime 看到的仍是 monitoring=unavailable。
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
                self?.pickWindow(spec: spec, sessionID: sessionID)
            }
        }
    }

    private func candidates(_ spec: NativeAppSpec) -> [(pid: pid_t, id: CGWindowID, title: String)] {
        var out: [(pid_t, CGWindowID, String)] = []
        for app in NSWorkspace.shared.runningApplications where app.bundleIdentifier == spec.bundleID {
            let pid = app.processIdentifier
            guard NativeIdentity.satisfies(pid: pid, requirement: spec.codeRequirement) else {
                log("native candidate rejected: code requirement mismatch pid=\(pid)")
                continue
            }
            for window in NativeIdentity.windows(pid: pid) {
                out.append((pid, window.id, NativeAX.localTitle(pid: pid, id: window.id)))
            }
        }
        return out
    }

    private func pickWindow(spec: NativeAppSpec, sessionID: String) {
        let options = candidates(spec)
        guard !options.isEmpty else {
            selecting = false
            finishIfIdle()
            emit(["type": "error", "app_id": spec.id, "session_id": sessionID,
                  "error": "未找到可委托的 \(spec.label) 窗口；请先打开该应用"])
            return
        }
        let alert = NSAlert()
        alert.messageText = "把 \(spec.label) 窗口交给当前会话观察"
        alert.informativeText = "Agent 只能读取窗口结构和开关状态，不能点击或输入；你操作该窗口时会自动暂停。"
        let popup = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 320, height: 26), pullsDown: false)
        for option in options { popup.addItem(withTitle: option.title) }
        alert.accessoryView = popup
        alert.addButton(withTitle: "委托")
        alert.addButton(withTitle: "取消")
        let chosen = alert.runModal() == .alertFirstButtonReturn ? options[popup.indexOfSelectedItem] : nil
        guard let choice = chosen, let token = NativeIdentity.startToken(choice.pid) else {
            selecting = false
            finishIfIdle()
            emit(["type": "idle", "app_id": spec.id, "session_id": sessionID])
            return
        }
        request("/workbench/app-control/host/desktop-target", "POST", [
            "app_id": spec.id, "bundle_id": spec.bundleID, "pid": Int(choice.pid),
            "process_start_token": token, "code_requirement": spec.codeRequirement,
            "window_id": String(choice.id), "host_instance_id": hostInstanceID(),
            "host_pid": Int(ProcessInfo.processInfo.processIdentifier),
            "session_id": sessionID, "ttl_seconds": 900,
        ]) { [weak self] result, error in
            guard let self = self else { return }
            self.selecting = false
            guard error == nil, let lease = result?["lease"] as? [String: Any],
                  let leaseID = lease["id"] as? String,
                  let target = result?["target"] as? [String: Any],
                  let generation = target["target_generation"] as? String,
                  target["manifest_fingerprint"] as? String == spec.fingerprint else {
                self.finishIfIdle()
                self.emit(["type": "error", "app_id": spec.id, "session_id": sessionID,
                           "error": error ?? "委托失败"])
                return
            }
            if let old = self.targets[spec.id], old.leaseID != leaseID {
                self.idleWork.removeValue(forKey: spec.id)?.cancel()
            }
            self.targets[spec.id] = NativeTarget(
                leaseID: leaseID, appID: spec.id, sessionID: sessionID, bundleID: spec.bundleID,
                pid: choice.pid, startToken: token, windowID: choice.id,
                generation: generation, spec: spec)
            self.pausedByUser.remove(spec.id)
            self.ensurePolling()
            self.emit(["type": "delegated", "app_id": spec.id, "session_id": sessionID,
                       "lease_id": leaseID])
        }
    }

    // ---- 人介入监测 ----

    private func installMonitor() {
        guard inputMonitor == nil else { return }
        let mask: NSEvent.EventTypeMask = [.leftMouseDown, .rightMouseDown, .otherMouseDown,
                                           .keyDown, .scrollWheel]
        inputMonitor = NSEvent.addGlobalMonitorForEvents(matching: mask) { [weak self] event in
            self?.userInput(event)
        }
        if terminateObserver == nil {
            terminateObserver = NSWorkspace.shared.notificationCenter.addObserver(
                forName: NSWorkspace.didTerminateApplicationNotification, object: nil, queue: .main
            ) { [weak self] note in
                guard let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
                      let self = self else { return }
                for target in self.targets.values where target.pid == app.processIdentifier {
                    self.release(appID: target.appID, event: "process_exited", notify: "revoked")
                }
            }
        }
    }

    private func removeMonitor() {
        if let monitor = inputMonitor { NSEvent.removeMonitor(monitor) }
        inputMonitor = nil
        if let observer = terminateObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(observer)
        }
        terminateObserver = nil
    }

    private func userInput(_ event: NSEvent) {
        let frontmost = NSWorkspace.shared.frontmostApplication?.processIdentifier
        let mouse = NSEvent.mouseLocation
        let primaryHeight = NSScreen.screens.first?.frame.height ?? 0
        let cgPoint = CGPoint(x: mouse.x, y: primaryHeight - mouse.y)
        for target in targets.values {
            var touched = frontmost == target.pid
            if !touched && event.type != .keyDown {
                // 背景窗口上的点击或滚动同样会改变它；宁可多暂停，不漏报。
                touched = NativeIdentity.windows(pid: target.pid)
                    .contains { $0.id == target.windowID && $0.bounds.contains(cgPoint) }
            }
            guard touched else { continue }
            if !pausedByUser.contains(target.appID) {
                pausedByUser.insert(target.appID)
                targetEvent(target, "user_interaction")
                emit(["type": "paused", "app_id": target.appID, "session_id": target.sessionID])
            }
            idleWork.removeValue(forKey: target.appID)?.cancel()
            let work = DispatchWorkItem { [weak self] in
                guard let self = self, let current = self.targets[target.appID],
                      current.leaseID == target.leaseID else { return }
                self.pausedByUser.remove(target.appID)
                self.targetEvent(current, "user_idle")
                self.emit(["type": "delegated", "app_id": target.appID,
                           "session_id": target.sessionID])
            }
            idleWork[target.appID] = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 3, execute: work)
        }
    }

    private func targetEvent(_ target: NativeTarget, _ event: String) {
        request("/workbench/app-control/host/desktop-target-event", "POST", [
            "lease_id": target.leaseID, "target_generation": target.generation,
            "host_instance_id": hostInstanceID(), "event": event,
        ]) { [weak self] _, error in
            if let error = error { self?.log("native target event \(event) failed: \(error)") }
        }
    }

    private func release(appID: String, event: String, notify: String) {
        guard let target = targets.removeValue(forKey: appID) else { return }
        pausedByUser.remove(appID)
        idleWork.removeValue(forKey: appID)?.cancel()
        targetEvent(target, event)
        emit(["type": notify, "app_id": appID, "session_id": target.sessionID])
        finishIfIdle()
    }

    private func finishIfIdle() {
        guard targets.isEmpty, !selecting else { return }
        pollTimer?.invalidate()
        pollTimer = nil
        removeMonitor()
        reportCapability?()
    }

    /// lease 过期、Agent 归还或 Runtime 侧撤销后，本机同步停止监测与取活。
    private func syncTargets() {
        for target in targets.values {
            let query = [("lease_id", target.leaseID), ("target_generation", target.generation),
                         ("host_instance_id", hostInstanceID())]
                .map { "\($0.0)=\($0.1.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? "")" }
                .joined(separator: "&")
            request("/workbench/app-control/host/desktop-target-status?" + query, "GET", nil) {
                [weak self] result, error in
                guard let self = self, error == nil,
                      let status = result?["status"] as? String, status != "active",
                      let current = self.targets[target.appID], current.leaseID == target.leaseID
                else { return }
                self.targets.removeValue(forKey: target.appID)
                self.pausedByUser.remove(target.appID)
                self.idleWork.removeValue(forKey: target.appID)?.cancel()
                self.emit(["type": "revoked", "app_id": target.appID, "session_id": target.sessionID])
                self.finishIfIdle()
            }
        }
    }

    // ---- 取活与观察 ----

    private func ensurePolling() {
        guard pollTimer == nil else { return }
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.tick()
        }
    }

    private func tick() {
        guard !targets.isEmpty else { finishIfIdle(); return }
        let now = Date()
        // Runtime 的宿主状态 30 秒过期；有目标时持续回报，监测失效会立刻撤销目标。
        if now.timeIntervalSince(lastHeartbeat) >= 10 {
            lastHeartbeat = now
            reportCapability?()
            syncTargets()
        }
        if now.timeIntervalSince(lastWindowCheck) >= 2 {
            lastWindowCheck = now
            for target in targets.values {
                switch NativeIdentity.verify(target) {
                case .ok: break
                case .processExited: release(appID: target.appID, event: "process_exited", notify: "revoked")
                case .windowClosed: release(appID: target.appID, event: "window_closed", notify: "revoked")
                case .identityChanged: release(appID: target.appID, event: "identity_changed", notify: "revoked")
                }
            }
        }
        guard !polling else { return }
        for appID in targets.keys.sorted() {
            polling = true
            let path = "/workbench/app-control/host/desktop-next?app_id="
                + (appID.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")
                + "&host_instance_id="
                + (hostInstanceID().addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")
            request(path, "GET", nil) { [weak self] result, error in
                guard let self = self else { return }
                guard error == nil, let work = result?["work"] as? [String: Any] else {
                    self.polling = false
                    return
                }
                self.run(work)
            }
            return
        }
    }

    private func run(_ work: [String: Any]) {
        guard let op = work["operation"] as? [String: Any],
              let opID = op["id"] as? String, let dispatchID = op["dispatch_id"] as? String,
              let raw = work["target"] as? [String: Any],
              let appID = raw["app_id"] as? String,
              let target = targets[appID],
              let epoch = (raw["interaction_epoch"] as? NSNumber)?.intValue else {
            polling = false
            return
        }
        // Runtime 下发的绑定必须与本机登记的目标逐项一致，否则按身份漂移处理。
        let matches = raw["lease_id"] as? String == target.leaseID
            && raw["target_generation"] as? String == target.generation
            && raw["bundle_id"] as? String == target.bundleID
            && (raw["pid"] as? NSNumber)?.int32Value == target.pid
            && raw["process_start_token"] as? String == target.startToken
            && raw["window_id"] as? String == String(target.windowID)
            && raw["manifest_fingerprint"] as? String == target.spec.fingerprint
        guard matches else {
            finish(opID, dispatchID, raw, epoch, ok: false, output: nil, error: "native_identity_changed")
            return
        }
        let timeout = Double((op["timeout_ms"] as? NSNumber)?.intValue ?? 10000) / 1000.0
        axQueue.async { [weak self] in
            let deadline = ProcessInfo.processInfo.systemUptime + min(max(timeout - 2, 1), 5)
            var output: [String: Any]? = nil
            var error = ""
            let before = NativeIdentity.verify(target)
            if before != .ok {
                error = before.rawValue
            } else if let window = NativeAX.window(pid: target.pid, id: target.windowID) {
                output = NativeAX.project(window: window, spec: target.spec, deadline: deadline)
                let after = NativeIdentity.verify(target)
                if after != .ok { output = nil; error = after.rawValue }
                else if output == nil { error = "native_projection_failed" }
            } else {
                error = "native_ax_unavailable"
            }
            DispatchQueue.main.async {
                self?.finish(opID, dispatchID, raw, epoch, ok: output != nil, output: output, error: error)
            }
        }
    }

    private func finish(_ opID: String, _ dispatchID: String, _ raw: [String: Any], _ epoch: Int,
                        ok: Bool, output: [String: Any]?, error: String) {
        var body: [String: Any] = [
            "operation_id": opID, "dispatch_id": dispatchID,
            "host_instance_id": hostInstanceID(),
            "target_generation": raw["target_generation"] as? String ?? "",
            "interaction_epoch": epoch, "ok": ok, "error": error,
        ]
        if let output = output { body["output"] = output }
        request("/workbench/app-control/host/desktop-result", "POST", body) { [weak self] result, err in
            guard let self = self else { return }
            self.polling = false
            if let err = err { self.log("native result failed: \(err)"); return }
            let appID = raw["app_id"] as? String ?? ""
            let stopped = ["native_identity_changed", "native_window_closed", "native_process_exited",
                           "native_target_invalidated", "native_host_mismatch",
                           "native_target_generation_changed"]
            if let status = result?["error"] as? String, stopped.contains(status),
               let target = self.targets.removeValue(forKey: appID) {
                self.pausedByUser.remove(appID)
                self.idleWork.removeValue(forKey: appID)?.cancel()
                self.emit(["type": "revoked", "app_id": appID, "session_id": target.sessionID])
                self.finishIfIdle()
            }
        }
    }

    /// 宿主退出或后端换代：本机状态作废，不向旧后端重放任何读取。
    func resetForBackendRestart() {
        let apps = Array(targets.keys)
        targets.removeAll()
        pausedByUser.removeAll()
        idleWork.values.forEach { $0.cancel() }
        idleWork.removeAll()
        selecting = false
        polling = false
        finishIfIdle()
        for appID in apps { emit(["type": "revoked", "app_id": appID, "session_id": ""]) }
    }
}
