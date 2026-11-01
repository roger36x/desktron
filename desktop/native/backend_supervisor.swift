import Foundation
import Darwin

// One launch primitive for the production backend and the pre-switch contract check.
// Foundation.Process may create a child PGID; ownership is PID + parent + SID, not PGID equality.
func runNativeBackendProcess(_ process: Process) throws {
    try process.run()
}

/// Headless, side-effect-free launch check: no AppKit, ports, user data or backend startup.
/// The update helper owns this entire session and validates/cleans the exact child.
func runUpdateLaunchContractCheckIfRequested() -> Bool {
    let args = CommandLine.arguments
    guard args.count >= 2, args[1] == "--update-launch-contract-check" else { return false }
    guard args.count == 3 else { exit(64) }
    do {
        let child = Process()
        child.executableURL = URL(fileURLWithPath: "/bin/sleep")
        child.arguments = ["30"]
        try runNativeBackendProcess(child)
        let receipt: [String: Any] = ["contract": 1, "host_pid": getpid(),
                                      "pid": child.processIdentifier]
        try JSONSerialization.data(withJSONObject: receipt).write(
            to: URL(fileURLWithPath: args[2]), options: .atomic)
        child.waitUntilExit()
        return true
    } catch { exit(70) }
}

private func activeUptime() -> TimeInterval {
    var value = timespec()
    if clock_gettime(CLOCK_UPTIME_RAW, &value) == 0 {
        return TimeInterval(value.tv_sec) + TimeInterval(value.tv_nsec) / 1_000_000_000
    }
    return ProcessInfo.processInfo.systemUptime
}

private struct BackendRestartRequest: Codable {
    let operationID: String
    let hostInstanceID: String
    let launchGeneration: String
    let bootID: String
    let pid: Int32
    let token: String
    let expiresAt: TimeInterval

    enum CodingKeys: String, CodingKey {
        case operationID = "operation_id"
        case hostInstanceID = "host_instance_id"
        case launchGeneration = "launch_generation"
        case bootID = "boot_id"
        case pid, token
        case expiresAt = "expires_at"
    }
}

private struct BackendRestartResponse: Codable {
    let operationID: String
    let state: String
    let error: String?
    let hostInstanceID: String
    let requestLaunchGeneration: String
    let requestBootID: String
    let requestPID: Int32
    let targetLaunchGeneration: String?
    let targetBootID: String?

    enum CodingKeys: String, CodingKey {
        case operationID = "operation_id"
        case state, error
        case hostInstanceID = "host_instance_id"
        case requestLaunchGeneration = "request_launch_generation"
        case requestBootID = "request_boot_id"
        case requestPID = "request_pid"
        case targetLaunchGeneration = "target_launch_generation"
        case targetBootID = "target_boot_id"
    }
}

struct BackendProbe: Codable, Equatable {
    let desk: Bool
    let role: String?
    let rev: String?
    let revScope: String?
    let hostInstanceID: String?
    let launchGeneration: String?
    let bootID: String?
    let pid: Int32?

    enum CodingKeys: String, CodingKey {
        case desk, role, rev, pid
        case revScope = "rev_scope"
        case hostInstanceID = "host_instance_id"
        case launchGeneration = "launch_generation"
        case bootID = "boot_id"
    }
}

enum BackendSupervisorPhase: Equatable {
    case idle
    case launching(String)
    case healthy(String, String)
    case stopping(String)
    case backoff(Int)
    case foreignPort(Int32?)
    case degraded(String)
    case failed(String)
    case quitting
}

/// The native shell is the sole owner of its backend child.  All transitions
/// run on one serial queue; stale Process handlers and probe replies carry a
/// launch generation and cannot mutate a newer child.
final class BackendSupervisor {
    typealias Launcher = (_ hostID: String, _ generation: String) throws -> Process
    typealias UpdateLauncher = (_ operationID: String, _ hostPID: Int32) throws -> Process
    typealias ProbeReader = () -> BackendProbe?
    typealias PortOwner = () -> Int32?

    let hostInstanceID: String
    let controlToken: String
    let controlRoot: URL
    private let queue: DispatchQueue
    private let launcher: Launcher
    private let updateLauncher: UpdateLauncher?
    private let probeReader: ProbeReader
    private let portOwner: PortOwner
    private let now: () -> TimeInterval
    private let delays: [TimeInterval]
    private let startupTimeout: TimeInterval
    private let stableHealthyFor: TimeInterval
    private let updatePrepareTimeout: TimeInterval
    private var phaseValue: BackendSupervisorPhase = .idle
    private var child: Process?
    private var generation = ""
    private var launchedAt: TimeInterval = 0
    private var healthySince: TimeInterval = 0
    private var lastProbeAt: TimeInterval = 0
    private var failures = 0
    private var scheduled: DispatchWorkItem?
    private var loadedGeneration = ""
    private var foreignPortChecks = 0
    private var currentBootID = ""
    private var restartOperation = ""
    private var restartRequest: BackendRestartRequest?
    private var plannedExitGeneration = ""
    private var terminatingGeneration = ""
    private var lifecycleLockFD: Int32 = -1
    private var updateHelper: Process?
    private var updateRequest: BackendRestartRequest?

    var onPhase: ((BackendSupervisorPhase) -> Void)?
    var onHealthy: ((BackendProbe) -> Void)?
    var onUpdateAccepted: ((String) -> Void)?

    init(hostInstanceID: String = UUID().uuidString.lowercased(),
         controlToken: String = UUID().uuidString.lowercased(),
         controlRoot: URL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/state/desktron/host-control", isDirectory: true),
         queue: DispatchQueue = DispatchQueue(label: "io.github.desktron.backend-supervisor"),
         delays: [TimeInterval] = [0, 1, 2, 4, 8],
         startupTimeout: TimeInterval = 30,
         stableHealthyFor: TimeInterval = 30,
         updatePrepareTimeout: TimeInterval = 930,
         updateLauncher: UpdateLauncher? = nil,
         launcher: @escaping Launcher,
         probeReader: @escaping ProbeReader,
         portOwner: @escaping PortOwner,
         now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) {
        self.hostInstanceID = hostInstanceID
        self.controlToken = controlToken
        self.controlRoot = controlRoot
        self.queue = queue
        self.delays = delays
        self.startupTimeout = startupTimeout
        self.stableHealthyFor = stableHealthyFor
        self.updatePrepareTimeout = updatePrepareTimeout
        self.launcher = launcher
        self.updateLauncher = updateLauncher
        self.probeReader = probeReader
        self.portOwner = portOwner
        self.now = now
        try? FileManager.default.createDirectory(at: controlRoot, withIntermediateDirectories: true,
                                                  attributes: [.posixPermissions: 0o700])
        try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: controlRoot.path)
    }

    var phase: BackendSupervisorPhase { queue.sync { phaseValue } }
    var currentPID: Int32? { queue.sync { child?.isRunning == true ? child?.processIdentifier : nil } }

    func start() { queue.async { self.reconcile() } }

    func retry() {
        queue.async {
            guard self.phaseValue != .quitting else { return }
            self.failures = 0
            self.foreignPortChecks = 0
            self.scheduled?.cancel()
            self.scheduled = nil
            self.transition(.idle)
            self.reconcile()
        }
    }

    func shutdown(completion: (() -> Void)? = nil) {
        queue.async {
            if self.phaseValue == .quitting {
                if let completion = completion { DispatchQueue.main.async(execute: completion) }
                return
            }
            self.transition(.quitting)
            self.scheduled?.cancel()
            self.scheduled = nil
            let target = self.child
            self.child = nil
            guard let target = target, target.isRunning else {
                self.releaseLifecycleLock()
                if let completion = completion { DispatchQueue.main.async(execute: completion) }
                return
            }
            target.terminate()
            DispatchQueue.global().async {
                let deadline = Date().addingTimeInterval(4)
                while target.isRunning && Date() < deadline { usleep(50_000) }
                if target.isRunning { kill(target.processIdentifier, SIGKILL) }
                target.waitUntilExit()
                self.queue.async {
                    self.releaseLifecycleLock()
                    if let completion = completion { DispatchQueue.main.async(execute: completion) }
                }
            }
        }
    }

    private func transition(_ next: BackendSupervisorPhase) {
        phaseValue = next
        let callback = onPhase
        DispatchQueue.main.async { callback?(next) }
    }

    private func schedule(_ delay: TimeInterval, _ operation: @escaping () -> Void) {
        scheduled?.cancel()
        let item = DispatchWorkItem(block: operation)
        scheduled = item
        queue.asyncAfter(deadline: .now() + delay, execute: item)
    }

    private func reconcile() {
        guard phaseValue != .quitting else { return }
        processControlRequests()
        if case .stopping = phaseValue { return }
        if case .failed = phaseValue { return }
        if let running = child {
            guard running.isRunning else {
                childExited(running, generation: generation)
                return
            }
            verify(running, generation: generation)
            return
        }
        if let owner = portOwner() {
            transition(.foreignPort(owner))
            foreignPortChecks += 1
            // A backend whose native parent died exits through its parent-death
            // watchdog. Wait through its bounded cleanup, never signal it.
            if foreignPortChecks <= 24 {
                schedule(0.25) { self.reconcile() }
            } else if !restartOperation.isEmpty {
                failRestart("端口被非当前后端占用")
            }
            return
        }
        foreignPortChecks = 0
        launch()
    }

    private func launch() {
        guard phaseValue != .quitting, child == nil else { return }
        let nextGeneration = UUID().uuidString.lowercased()
        do {
            let process = try launcher(hostInstanceID, nextGeneration)
            child = process
            generation = nextGeneration
            launchedAt = now()
            healthySince = 0
            lastProbeAt = 0
            transition(.launching(nextGeneration))
            process.terminationHandler = { [weak self, weak process] _ in
                guard let self = self, let process = process else { return }
                self.queue.async { self.childExited(process, generation: nextGeneration) }
            }
            schedule(0.05) { self.reconcile() }
        } catch {
            failedLaunch(String(describing: error))
        }
    }

    private func verify(_ process: Process, generation expectedGeneration: String) {
        guard child === process, generation == expectedGeneration, phaseValue != .quitting else { return }
        if let probe = probeReader() {
            let owned = (probe.desk && probe.hostInstanceID == hostInstanceID
                && probe.launchGeneration == expectedGeneration
                && probe.pid == process.processIdentifier
                && !(probe.bootID ?? "").isEmpty)
            if owned {
                if healthySince == 0 { healthySince = now() }
                lastProbeAt = now()
                if now() - healthySince >= stableHealthyFor { failures = 0 }
                currentBootID = probe.bootID!
                transition(.healthy(expectedGeneration, probe.bootID!))
                if !restartOperation.isEmpty, let request = restartRequest,
                   writeControlResponse(request, state: "completed", error: nil,
                                        targetGeneration: expectedGeneration,
                                        targetBootID: probe.bootID) {
                    restartOperation = ""
                    restartRequest = nil
                    releaseLifecycleLock()
                }
                if loadedGeneration != expectedGeneration {
                    loadedGeneration = expectedGeneration
                    let callback = onHealthy
                    DispatchQueue.main.async { callback?(probe) }
                }
                schedule(0.5) { self.reconcile() }
                return
            }
            // A process that has already proved its identity is never restarted
            // merely because HTTP health later degrades. Only real process exit
            // drives recovery; transient GIL stalls and App Nap are not crashes.
            if lastProbeAt > 0 {
                transition(.degraded("后端探活身份暂时不匹配"))
                schedule(1) { self.reconcile() }
            } else {
                boundedStop(process, generation: expectedGeneration,
                            reason: "启动中的后端探活身份不匹配")
            }
            return
        }
        if lastProbeAt > 0 {
            transition(.degraded("后端探活暂时不可用"))
            schedule(1) { self.reconcile() }
            return
        }
        if now() >= launchedAt + startupTimeout {
            boundedStop(process, generation: expectedGeneration,
                        reason: "后端未在期限内提供匹配宿主身份的探活")
            return
        }
        schedule(0.1) { self.reconcile() }
    }

    private func childExited(_ process: Process, generation exitedGeneration: String) {
        guard phaseValue != .quitting, child === process, generation == exitedGeneration else { return }
        child = nil
        generation = ""
        currentBootID = ""
        terminatingGeneration = ""
        if updateRequest != nil && !restartOperation.isEmpty {
            // The accepted updater is already a direct host child and no longer
            // depends on this backend. Keep waiting for its ready receipt; do not
            // launch another old-generation backend during preparation.
            transition(.stopping(restartOperation))
        } else if !restartOperation.isEmpty && plannedExitGeneration == exitedGeneration {
            plannedExitGeneration = ""
            transition(.idle)
            schedule(0) { self.reconcile() }
        } else {
            failedLaunch("后端进程已退出")
        }
    }

    private func requestURL(_ operation: String) -> URL {
        controlRoot.appendingPathComponent("request-" + operation + ".json")
    }

    private func responseURL(_ operation: String) -> URL {
        controlRoot.appendingPathComponent("response-" + operation + ".json")
    }

    private func updateResponseURL(_ operation: String) -> URL {
        controlRoot.appendingPathComponent("update-response-" + operation + ".json")
    }

    private func updateReadyURL(_ operation: String) -> URL {
        controlRoot.appendingPathComponent("update-ready-" + operation + ".json")
    }

    private func acquireLifecycleLock() -> Bool {
        if lifecycleLockFD >= 0 { return true }
        let path = controlRoot.appendingPathComponent("lifecycle.lock").path
        let fd = open(path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        if fd < 0 { return false }
        if flock(fd, LOCK_EX | LOCK_NB) != 0 { close(fd); return false }
        lifecycleLockFD = fd
        return true
    }

    private func releaseLifecycleLock() {
        if lifecycleLockFD >= 0 {
            flock(lifecycleLockFD, LOCK_UN)
            close(lifecycleLockFD)
            lifecycleLockFD = -1
        }
    }

    @discardableResult
    private func writeControlResponse(_ request: BackendRestartRequest, state: String, error: String?,
                                      targetGeneration: String? = nil,
                                      targetBootID: String? = nil,
                                      update: Bool = false) -> Bool {
        let value = BackendRestartResponse(operationID: request.operationID, state: state, error: error,
            hostInstanceID: request.hostInstanceID,
            requestLaunchGeneration: request.launchGeneration,
            requestBootID: request.bootID, requestPID: request.pid,
            targetLaunchGeneration: targetGeneration, targetBootID: targetBootID)
        guard let data = try? JSONEncoder().encode(value) else { return false }
        let target = update ? updateResponseURL(request.operationID) : responseURL(request.operationID)
        do {
            try data.write(to: target, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: target.path)
            return true
        } catch { return false }
    }

    private func processControlRequests() {
        guard restartOperation.isEmpty, case .healthy = phaseValue,
              let process = child, process.isRunning else { return }
        let names = (try? FileManager.default.contentsOfDirectory(atPath: controlRoot.path)) ?? []
        for name in names.sorted() where name.hasPrefix("update-request-") && name.hasSuffix(".json") {
            let url = controlRoot.appendingPathComponent(name)
            guard acquireLifecycleLock() else { return }
            guard let data = try? Data(contentsOf: url),
                  let request = try? JSONDecoder().decode(BackendRestartRequest.self, from: data) else {
                releaseLifecycleLock(); continue
            }
            if request.expiresAt < Date().timeIntervalSince1970 {
                if writeControlResponse(request, state: "rejected", error: "request_expired", update: true) {
                    try? FileManager.default.removeItem(at: url)
                }
                releaseLifecycleLock(); continue
            }
            let owned = request.hostInstanceID == hostInstanceID
                && request.launchGeneration == generation && request.bootID == currentBootID
                && request.pid == process.processIdentifier && request.token == controlToken
            guard owned else {
                if writeControlResponse(request, state: "rejected", error: "identity_mismatch", update: true) {
                    try? FileManager.default.removeItem(at: url)
                }
                releaseLifecycleLock(); continue
            }
            let response = updateResponseURL(request.operationID)
            if FileManager.default.fileExists(atPath: response.path) {
                // Operation IDs are single-use even when request unlink failed or was replayed.
                guard let data = try? Data(contentsOf: response),
                      let prior = try? JSONDecoder().decode(BackendRestartResponse.self, from: data),
                      prior.operationID == request.operationID,
                      ["failed", "rejected", "cancelled", "completed"].contains(prior.state) else {
                    transition(.failed("update_operation_already_claimed")); return
                }
                do { try FileManager.default.removeItem(at: url) }
                catch { transition(.failed("update_request_revoke_failed")); return }
                releaseLifecycleLock(); continue
            }
            guard let updateLauncher = updateLauncher else {
                if writeControlResponse(request, state: "rejected", error: "update_launcher_unavailable", update: true) {
                    try? FileManager.default.removeItem(at: url)
                }
                releaseLifecycleLock(); continue
            }
            do {
                let helper = try updateLauncher(request.operationID, getpid())
                usleep(100_000)
                guard helper.isRunning else { throw NSError(domain: "DeskHostUpdate", code: 1) }
                updateHelper = helper
                updateRequest = request
                restartOperation = request.operationID
                guard writeControlResponse(request, state: "accepted", error: nil, update: true) else {
                    abortUpdatePreparation(request, helper: helper)
                    return
                }
                do { try FileManager.default.removeItem(at: url) }
                catch { abortUpdatePreparation(request, helper: helper); return }
                awaitUpdateReady(request, helper: helper,
                                 deadline: activeUptime() + updatePrepareTimeout)
                return
            } catch {
                _ = writeControlResponse(request, state: "rejected",
                                         error: "update_helper_failed", update: true)
                try? FileManager.default.removeItem(at: url)
                releaseLifecycleLock()
                continue
            }
        }
        for name in names.sorted() where name.hasPrefix("request-") && name.hasSuffix(".json") {
            let url = controlRoot.appendingPathComponent(name)
            guard acquireLifecycleLock() else { return }
            guard let data = try? Data(contentsOf: url),
                  let request = try? JSONDecoder().decode(BackendRestartRequest.self, from: data) else {
                releaseLifecycleLock(); continue
            }
            if request.expiresAt < Date().timeIntervalSince1970 {
                if writeControlResponse(request, state: "rejected", error: "request_expired") {
                    try? FileManager.default.removeItem(at: url)
                }
                releaseLifecycleLock(); continue
            }
            let owned = request.hostInstanceID == hostInstanceID
                && request.launchGeneration == generation && request.bootID == currentBootID
                && request.pid == process.processIdentifier && request.token == controlToken
            guard owned else {
                if writeControlResponse(request, state: "rejected", error: "identity_mismatch") {
                    try? FileManager.default.removeItem(at: url)
                }
                releaseLifecycleLock(); continue
            }
            guard writeControlResponse(request, state: "accepted", error: nil) else {
                releaseLifecycleLock()
                continue
            }
            restartOperation = request.operationID
            restartRequest = request
            plannedExitGeneration = generation
            transition(.stopping(request.operationID))
            try? FileManager.default.removeItem(at: url)
            let expectedGeneration = generation
            queue.asyncAfter(deadline: .now() + 0.15) {
                guard self.generation == expectedGeneration, self.child === process,
                      process.isRunning else { return }
                process.terminate()
            }
            queue.asyncAfter(deadline: .now() + 4) {
                guard self.generation == expectedGeneration, self.child === process,
                      process.isRunning else { return }
                kill(process.processIdentifier, SIGKILL)
            }
            return
        }
    }

    private func awaitUpdateReady(_ request: BackendRestartRequest, helper: Process,
                                  deadline: TimeInterval) {
        guard restartOperation == request.operationID, updateHelper === helper else { return }
        // Expiry revokes ready permission before inspecting a possibly late receipt.
        if !helper.isRunning || activeUptime() >= deadline {
            abortUpdatePreparation(request, helper: helper)
            return
        }
        let readyURL = updateReadyURL(request.operationID)
        if let data = try? Data(contentsOf: readyURL),
           let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           value["operation_id"] as? String == request.operationID,
           value["state"] as? String == "ready",
           (value["helper_pid"] as? NSNumber)?.int32Value == helper.processIdentifier {
            do { try FileManager.default.removeItem(at: readyURL) }
            catch { abortUpdatePreparation(request, helper: helper); return }
            transition(.stopping(request.operationID))
            let callback = onUpdateAccepted
            DispatchQueue.main.async { callback?(request.operationID) }
            return
        }
        queue.asyncAfter(deadline: .now() + 0.1) {
            self.awaitUpdateReady(request, helper: helper, deadline: deadline)
        }
    }

    private func abortUpdatePreparation(_ request: BackendRestartRequest, helper: Process) {
        // No further awaitUpdateReady is scheduled. Keep ownership/lock until the exact helper
        // is proven dead, so a failed receipt can safely reopen the OLD backend's admission.
        if helper.isRunning { helper.terminate() }
        finishAbortingUpdate(request, helper: helper, killAt: activeUptime() + 2)
    }

    private func finishAbortingUpdate(_ request: BackendRestartRequest, helper: Process,
                                     killAt: TimeInterval) {
        guard restartOperation == request.operationID, updateHelper === helper else { return }
        if helper.isRunning {
            if activeUptime() >= killAt { kill(helper.processIdentifier, SIGKILL) }
            queue.asyncAfter(deadline: .now() + 0.05) {
                self.finishAbortingUpdate(request, helper: helper, killAt: killAt)
            }
            return
        }
        helper.waitUntilExit()
        for name in ["update-ready-", "update-request-"] {
            let url = controlRoot.appendingPathComponent(name + request.operationID + ".json")
            if FileManager.default.fileExists(atPath: url.path) {
                do { try FileManager.default.removeItem(at: url) }
                catch { transition(.failed("update_request_revoke_failed")); return }
            }
        }
        guard writeControlResponse(request, state: "failed", error: "update_prepare_failed", update: true) else {
            transition(.failed("update_failure_receipt_failed")); return
        }
        updateHelper = nil
        updateRequest = nil
        restartOperation = ""
        releaseLifecycleLock()
        if child == nil { transition(.idle) }
        schedule(0) { self.reconcile() }
    }

    private func boundedStop(_ process: Process, generation expectedGeneration: String,
                             reason: String) {
        guard child === process, generation == expectedGeneration,
              terminatingGeneration != expectedGeneration else { return }
        terminatingGeneration = expectedGeneration
        if process.isRunning { process.terminate() }
        queue.asyncAfter(deadline: .now() + 4) {
            guard self.child === process, self.generation == expectedGeneration,
                  process.isRunning else { return }
            kill(process.processIdentifier, SIGKILL)
        }
        // The exact Process termination handler is the only place that advances
        // generation state; do not race it by calling failedLaunch here.
        if !restartOperation.isEmpty && plannedExitGeneration != expectedGeneration {
            transition(.backoff(max(1, failures + 1)))
        } else if restartOperation.isEmpty {
            transition(.backoff(max(1, failures + 1)))
        }
    }

    private func failRestart(_ reason: String) {
        transition(.failed(reason))
        if let request = restartRequest {
            _ = writeControlResponse(request, state: "failed", error: reason)
        }
        restartOperation = ""
        restartRequest = nil
        plannedExitGeneration = ""
        releaseLifecycleLock()
    }

    private func failedLaunch(_ reason: String) {
        guard phaseValue != .quitting else { return }
        failures += 1
        if failures > delays.count {
            transition(.failed(reason))
            if !restartOperation.isEmpty { failRestart(reason) }
            else { releaseLifecycleLock() }
            return
        }
        let delay = delays[failures - 1]
        transition(.backoff(failures))
        let expectedFailureCount = failures
        schedule(delay) {
            guard self.failures == expectedFailureCount else { return }
            if case .backoff(expectedFailureCount) = self.phaseValue { self.reconcile() }
        }
    }
}
