import Foundation

// 与 desktop/native/native_control.swift 一起编译，验证真实进程的身份原语。
let sleeper = Process()
sleeper.executableURL = URL(fileURLWithPath: "/bin/sleep")
sleeper.arguments = ["30"]
try! sleeper.run()
defer { sleeper.terminate() }
let pid = sleeper.processIdentifier
let me = ProcessInfo.processInfo.processIdentifier
let first = NativeIdentity.startToken(pid) ?? ""
let second = NativeIdentity.startToken(pid) ?? ""
let result: [String: Any] = [
    "token_stable": !first.isEmpty && first == second,
    "token_distinct": first != (NativeIdentity.startToken(me) ?? ""),
    "token_missing_for_dead": NativeIdentity.startToken(999_999) == nil,
    "requirement_ok": NativeIdentity.satisfies(
        pid: pid, requirement: "identifier \"com.apple.sleep\" and anchor apple"),
    "requirement_wrong_id": NativeIdentity.satisfies(
        pid: pid, requirement: "identifier \"com.liguangming.Shadowrocket\" and anchor apple"),
    "requirement_malformed": NativeIdentity.satisfies(pid: pid, requirement: "not a requirement ((("),
    "no_windows": NativeIdentity.windows(pid: pid).isEmpty,
]
print(String(data: try! JSONSerialization.data(withJSONObject: result), encoding: .utf8)!)
