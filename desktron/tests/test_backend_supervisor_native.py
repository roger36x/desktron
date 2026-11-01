"""Native shell supervisor owns exactly one backend generation."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
import pytest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'requires Swift on macOS')
class BackendSupervisorNative(unittest.TestCase):
    def test_update_prepare_deadline_uses_awake_uptime(self):
        source=(ROOT/'desktop/native/backend_supervisor.swift').read_text()
        self.assertIn('clock_gettime(CLOCK_UPTIME_RAW', source)
        self.assertIn('updatePrepareTimeout: TimeInterval = 930', source)
        self.assertIn('deadline: activeUptime() + updatePrepareTimeout', source)
        self.assertIn('activeUptime() >= deadline', source)
        self.assertNotIn('deadline: Date().addingTimeInterval(930)', source)

    def test_shutdown_releases_lifecycle_lock_only_after_exact_child_exits(self):
        source=(ROOT/'desktop/native/backend_supervisor.swift').read_text()
        shutdown=source[source.index('    func shutdown('):source.index('    private func transition',source.index('    func shutdown('))]
        wait=shutdown.index('target.waitUntilExit()')
        release=shutdown.index('self.releaseLifecycleLock()',wait)
        self.assertLess(wait,release)

    @pytest.mark.native
    def test_process_ownership_generation_backoff_and_quit(self):
        fixture = r'''
import Foundation

final class Box {
    let lock = NSLock()
    var processes: [Process] = []
    var generations: [String] = []
    var foreign: Int32? = nil
    var crash = false
    func snapshot() -> ([Process], [String], Int32?, Bool) {
        lock.lock(); defer { lock.unlock() }
        return (processes, generations, foreign, crash)
    }
}

func spin(_ seconds: TimeInterval, _ ready: () -> Bool) {
    let end = Date().addingTimeInterval(seconds)
    while !ready() && Date() < end { RunLoop.current.run(until: Date().addingTimeInterval(0.01)) }
    precondition(ready(), "timed out")
}

func process(_ seconds: Int) throws -> Process {
    let p = Process(); p.executableURL = URL(fileURLWithPath: "/bin/sleep")
    p.arguments = [String(seconds)]; try p.run(); return p
}

// A healthy child crash creates exactly one successor with a fresh generation.
do {
    let box = Box(); let host = "0123456789abcdef0123456789abcdef"
    let supervisor = BackendSupervisor(hostInstanceID: host, delays: [0, 0.01, 0.02], startupTimeout: 2,
        launcher: { _, generation in
            let p = try process(30); box.lock.lock(); box.processes.append(p); box.generations.append(generation); box.lock.unlock(); return p
        },
        probeReader: {
            let s = box.snapshot(); guard !s.3, let p = s.0.last, let generation = s.1.last, p.isRunning else { return nil }
            return BackendProbe(desk:true, role:"local", rev:"test", revScope:"process",
                hostInstanceID:host, launchGeneration:generation, bootID:"boot-"+generation, pid:p.processIdentifier)
        }, portOwner: { nil })
    var healthy = 0; let lock = NSLock()
    supervisor.onHealthy = { _ in lock.lock(); healthy += 1; lock.unlock() }
    supervisor.start(); spin(3) { lock.lock(); defer { lock.unlock() }; return healthy == 1 }
    let stable=box.snapshot();box.lock.lock();box.crash=true;box.lock.unlock()
    RunLoop.current.run(until:Date().addingTimeInterval(1.2));precondition(stable.0[0].isRunning)
    precondition(box.snapshot().0.count==1);box.lock.lock();box.crash=false;box.lock.unlock()
    spin(2){if case .healthy=supervisor.phase{return true};return false}
    let first = box.snapshot(); precondition(first.0.count == 1); first.0[0].terminate(); first.0[0].waitUntilExit()
    spin(3) { lock.lock(); defer { lock.unlock() }; return healthy == 2 }
    let second = box.snapshot(); precondition(second.0.count == 2); precondition(Set(second.1).count == 2)
    RunLoop.current.run(until: Date().addingTimeInterval(0.2)); precondition(box.snapshot().0.count == 2)
    supervisor.shutdown(); second.0.last?.waitUntilExit()
    RunLoop.current.run(until: Date().addingTimeInterval(0.2)); precondition(box.snapshot().0.count == 2)
}

// A foreign listener is reported and never launches or receives a signal.
do {
    let box = Box()
    let supervisor = BackendSupervisor(delays:[0], startupTimeout:1,
        launcher: { _, _ in box.lock.lock(); box.generations.append("BAD"); box.lock.unlock(); return try process(1) },
        probeReader: { nil }, portOwner: { box.snapshot().2 })
    box.lock.lock(); box.foreign = 4242; box.lock.unlock()
    supervisor.start(); spin(1) { if case .foreignPort(let pid) = supervisor.phase { return pid == 4242 }; return false }
    precondition(box.snapshot().1.isEmpty)
    supervisor.shutdown()
}

// Repeated startup crashes stop at failed instead of an infinite rapid loop.
do {
    let box = Box()
    let supervisor = BackendSupervisor(delays:[0, 0.01, 0.02], startupTimeout:1,
        launcher: { _, generation in
            let p = Process(); p.executableURL = URL(fileURLWithPath:"/usr/bin/false"); try p.run()
            box.lock.lock(); box.processes.append(p); box.generations.append(generation); box.lock.unlock(); return p
        }, probeReader:{ nil }, portOwner:{ nil })
    supervisor.start(); spin(3) { if case .failed = supervisor.phase { return true }; return false }
    precondition(box.snapshot().0.count == 4)
    RunLoop.current.run(until: Date().addingTimeInterval(0.2)); precondition(box.snapshot().0.count == 4)
    supervisor.shutdown()
}
// A durable restart request is accepted only for the exact child and completes
// after a distinct generation becomes healthy.
do {
    let box = Box(); let host = "fedcba9876543210fedcba9876543210"
    let control = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: control, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: control) }
    let supervisor = BackendSupervisor(hostInstanceID: host, controlToken: "secret", controlRoot: control,
        delays:[0,0.01], startupTimeout:2,
        launcher:{ _, generation in let p=try process(30); box.lock.lock();box.processes.append(p);box.generations.append(generation);box.lock.unlock();return p },
        probeReader:{ let s=box.snapshot();guard let p=s.0.last,let g=s.1.last,p.isRunning else{return nil};return BackendProbe(desk:true,role:"local",rev:"test",revScope:"process",hostInstanceID:host,launchGeneration:g,bootID:"boot-"+g,pid:p.processIdentifier) },
        portOwner:{nil})
    var boots=0;let lock=NSLock();supervisor.onHealthy={_ in lock.lock();boots+=1;lock.unlock()}
    supervisor.start();spin(3){lock.lock();defer{lock.unlock()};return boots==1}
    let first=box.snapshot();let operation=String(repeating:"a",count:32)
    let req:[String:Any]=["operation_id":operation,"host_instance_id":host,
        "launch_generation":first.1[0],"boot_id":"boot-"+first.1[0],
        "pid":first.0[0].processIdentifier,"token":"secret","expires_at":Date().timeIntervalSince1970+5]
    let data=try JSONSerialization.data(withJSONObject:req);try data.write(to:control.appendingPathComponent("request-"+operation+".json"),options:.atomic)
    spin(3){(try?JSONSerialization.jsonObject(with:Data(contentsOf:control.appendingPathComponent("response-"+operation+".json"))) as? [String:Any])?["state"] as? String=="accepted"}
    spin(3){lock.lock();defer{lock.unlock()};return boots==2}
    let response=try JSONSerialization.jsonObject(with:Data(contentsOf:control.appendingPathComponent("response-"+operation+".json"))) as! [String:Any]
    precondition(response["state"] as? String=="completed");precondition(Set(box.snapshot().1).count==2)
    supervisor.shutdown();box.snapshot().0.last?.waitUntilExit()
}

// Wrong identity is rejected without signalling the healthy child.
do {
    let box=Box();let host="00112233445566778899aabbccddeeff"
    let control=URL(fileURLWithPath:NSTemporaryDirectory()).appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at:control,withIntermediateDirectories:true)
    defer{try?FileManager.default.removeItem(at:control)}
    let supervisor=BackendSupervisor(hostInstanceID:host,controlToken:"right",controlRoot:control,
        delays:[0],startupTimeout:2,
        launcher:{_,generation in let p=try process(30);box.lock.lock();box.processes.append(p);box.generations.append(generation);box.lock.unlock();return p},
        probeReader:{let s=box.snapshot();guard let p=s.0.last,let g=s.1.last,p.isRunning else{return nil};return BackendProbe(desk:true,role:"local",rev:"x",revScope:"process",hostInstanceID:host,launchGeneration:g,bootID:"boot",pid:p.processIdentifier)},portOwner:{nil})
    var healthy=false;supervisor.onHealthy={_ in healthy=true};supervisor.start();spin(3){healthy}
    let s=box.snapshot();let operation=String(repeating:"b",count:32)
    let req:[String:Any]=["operation_id":operation,"host_instance_id":host,
        "launch_generation":s.1[0],"boot_id":"boot","pid":s.0[0].processIdentifier,
        "token":"wrong","expires_at":Date().timeIntervalSince1970+5]
    try JSONSerialization.data(withJSONObject:req).write(to:control.appendingPathComponent("request-"+operation+".json"),options:.atomic)
    spin(2){(try?JSONSerialization.jsonObject(with:Data(contentsOf:control.appendingPathComponent("response-"+operation+".json"))) as? [String:Any])?["state"] as? String=="rejected"}
    precondition(s.0[0].isRunning);precondition(box.snapshot().0.count==1)
    supervisor.shutdown();s.0[0].waitUntilExit()
}

// A planned successor blocked by a foreign listener fails the transaction and
// releases the cross-process lifecycle lock without signalling the listener.
do {
    let box=Box();let host="11223344556677889900aabbccddeeff"
    let control=URL(fileURLWithPath:NSTemporaryDirectory()).appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at:control,withIntermediateDirectories:true)
    defer{try?FileManager.default.removeItem(at:control)}
    let supervisor=BackendSupervisor(hostInstanceID:host,controlToken:"secret",controlRoot:control,
        delays:[0],startupTimeout:1,
        launcher:{_,generation in let p=try process(30);box.lock.lock();box.processes.append(p);box.generations.append(generation);box.lock.unlock();return p},
        probeReader:{let s=box.snapshot();guard let p=s.0.last,let g=s.1.last,p.isRunning else{return nil};return BackendProbe(desk:true,role:"local",rev:"x",revScope:"process",hostInstanceID:host,launchGeneration:g,bootID:"boot",pid:p.processIdentifier)},
        portOwner:{box.snapshot().0.first?.isRunning==false ? 9999:nil})
    var healthy=false;supervisor.onHealthy={_ in healthy=true};supervisor.start();spin(3){healthy}
    let s=box.snapshot();let operation=String(repeating:"d",count:32)
    let req:[String:Any]=["operation_id":operation,"host_instance_id":host,"launch_generation":s.1[0],
        "boot_id":"boot","pid":s.0[0].processIdentifier,"token":"secret","expires_at":Date().timeIntervalSince1970+5]
    try JSONSerialization.data(withJSONObject:req).write(to:control.appendingPathComponent("request-"+operation+".json"),options:.atomic)
    spin(9){if case .failed=supervisor.phase{return true};return false}
    let result=try JSONSerialization.jsonObject(with:Data(contentsOf:control.appendingPathComponent("response-"+operation+".json"))) as! [String:Any]
    precondition(result["state"] as? String=="failed")
    let fd=open(control.appendingPathComponent("lifecycle.lock").path,O_RDWR)
    precondition(flock(fd,LOCK_EX|LOCK_NB)==0);flock(fd,LOCK_UN);close(fd)
    supervisor.shutdown()
}
// Once the planned old generation exits, crashing successors use normal
// backoff and eventually fail the operation instead of looping forever.
do {
    let box=Box();let host="ffeeddccbbaa99887766554433221100"
    let control=URL(fileURLWithPath:NSTemporaryDirectory()).appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at:control,withIntermediateDirectories:true)
    defer{try?FileManager.default.removeItem(at:control)}
    let supervisor=BackendSupervisor(hostInstanceID:host,controlToken:"secret",controlRoot:control,
        delays:[0,0.01],startupTimeout:1,stableHealthyFor:30,
        launcher:{_,generation in
            let count=box.snapshot().0.count
            let p=Process();p.executableURL=URL(fileURLWithPath:count==0 ? "/bin/sleep":"/usr/bin/false")
            p.arguments=count==0 ? ["30"]:[];try p.run();box.lock.lock();box.processes.append(p);box.generations.append(generation);box.lock.unlock();return p},
        probeReader:{let s=box.snapshot();guard s.0.count==1,let p=s.0.last,let g=s.1.last,p.isRunning else{return nil};return BackendProbe(desk:true,role:"local",rev:"x",revScope:"process",hostInstanceID:host,launchGeneration:g,bootID:"boot",pid:p.processIdentifier)},portOwner:{nil})
    var healthy=false;supervisor.onHealthy={_ in healthy=true};supervisor.start();spin(3){healthy}
    let s=box.snapshot();let operation=String(repeating:"c",count:32)
    let req:[String:Any]=["operation_id":operation,"host_instance_id":host,"launch_generation":s.1[0],
        "boot_id":"boot","pid":s.0[0].processIdentifier,"token":"secret","expires_at":Date().timeIntervalSince1970+5]
    try JSONSerialization.data(withJSONObject:req).write(to:control.appendingPathComponent("request-"+operation+".json"),options:.atomic)
    spin(3){if case .failed=supervisor.phase{return true};return false}
    precondition(box.snapshot().0.count==4)
    let result=try JSONSerialization.jsonObject(with:Data(contentsOf:control.appendingPathComponent("response-"+operation+".json"))) as! [String:Any]
    precondition(result["state"] as? String=="failed")
    let fd=open(control.appendingPathComponent("lifecycle.lock").path,O_RDWR)
    precondition(flock(fd,LOCK_EX|LOCK_NB)==0);flock(fd,LOCK_UN);close(fd)
    supervisor.shutdown()
}
// A native update request launches one fixed helper, acknowledges the exact
// backend identity, and lets the host shut down its own child without spawning
// a replacement generation.
do {
    let box=Box();let host="abcdefabcdefabcdefabcdefabcdefab"
    let control=URL(fileURLWithPath:NSTemporaryDirectory()).appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at:control,withIntermediateDirectories:true)
    defer{try?FileManager.default.removeItem(at:control)}
    var helper:Process?=nil;var updateAccepted=false;let updateLock=NSLock()
    let supervisor=BackendSupervisor(hostInstanceID:host,controlToken:"secret",controlRoot:control,
        delays:[0],startupTimeout:2,
        updateLauncher:{operation,hostPID in
            precondition(operation==String(repeating:"e",count:32));precondition(hostPID==getpid())
            let p=try process(30);updateLock.lock();helper=p;updateLock.unlock()
            let ready:[String:Any]=["operation_id":operation,"helper_pid":p.processIdentifier,"state":"ready"]
            DispatchQueue.global().asyncAfter(deadline:.now()+0.4){
                try? JSONSerialization.data(withJSONObject:ready).write(to:control.appendingPathComponent("update-ready-"+operation+".json"),options:.atomic)}
            return p},
        launcher:{_,generation in let p=try process(30);box.lock.lock();box.processes.append(p);box.generations.append(generation);box.lock.unlock();return p},
        probeReader:{let s=box.snapshot();guard let p=s.0.last,let g=s.1.last,p.isRunning else{return nil};return BackendProbe(desk:true,role:"local",rev:"x",revScope:"process",hostInstanceID:host,launchGeneration:g,bootID:"boot",pid:p.processIdentifier)},portOwner:{nil})
    var healthy=false;supervisor.onHealthy={_ in healthy=true}
    supervisor.onUpdateAccepted={_ in supervisor.shutdown{updateLock.lock();updateAccepted=true;updateLock.unlock()}}
    supervisor.start();spin(3){healthy}
    let s=box.snapshot();let operation=String(repeating:"e",count:32)
    let req:[String:Any]=["operation_id":operation,"host_instance_id":host,
        "launch_generation":s.1[0],"boot_id":"boot","pid":s.0[0].processIdentifier,
        "token":"secret","expires_at":Date().timeIntervalSince1970+5]
    try JSONSerialization.data(withJSONObject:req).write(to:control.appendingPathComponent("update-request-"+operation+".json"),options:.atomic)
    spin(2){(try?JSONSerialization.jsonObject(with:Data(contentsOf:control.appendingPathComponent("update-response-"+operation+".json"))) as? [String:Any])?["state"] as? String=="accepted"}
    s.0[0].terminate();s.0[0].waitUntilExit()
    spin(3){updateLock.lock();defer{updateLock.unlock()};return updateAccepted}
    let response=try JSONSerialization.jsonObject(with:Data(contentsOf:control.appendingPathComponent("update-response-"+operation+".json"))) as! [String:Any]
    precondition(response["state"] as? String=="accepted")
    precondition(box.snapshot().0.count==1);precondition(!s.0[0].isRunning)
    updateLock.lock();let exactHelper=helper;updateLock.unlock();exactHelper?.terminate();exactHelper?.waitUntilExit()
}
// A timed-out helper ignores TERM and writes a late ready. Until exact exit, the response
// stays accepted and the lock stays held. Late ready must never shut down the old backend.
do {
    let box=Box(); let host="0123456789abcdef0123456789abcdef"
    let control=URL(fileURLWithPath:NSTemporaryDirectory()).appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at:control,withIntermediateDirectories:true)
    defer{try?FileManager.default.removeItem(at:control)}
    var helper:Process?=nil; var accepted=false; var helperLaunches=0
    let operation=String(repeating:"f",count:32)
    let supervisor=BackendSupervisor(hostInstanceID:host,controlToken:"secret",controlRoot:control,
        delays:[0],startupTimeout:2,updatePrepareTimeout:0.2,
        updateLauncher:{_,_ in
            let p=Process();p.executableURL=URL(fileURLWithPath:"/bin/sh")
            p.arguments=["-c", "trap '' TERM; while :; do :; done"]
            try runNativeBackendProcess(p);helper=p;helperLaunches+=1;return p},
        launcher:{_,g in let p=try process(30);box.lock.lock();box.processes.append(p);box.generations.append(g);box.lock.unlock();return p},
        probeReader:{let s=box.snapshot();guard let p=s.0.last,let g=s.1.last,p.isRunning else{return nil};return BackendProbe(desk:true,role:"local",rev:"x",revScope:"process",hostInstanceID:host,launchGeneration:g,bootID:"boot",pid:p.processIdentifier)},portOwner:{nil})
    var healthy=false;supervisor.onHealthy={_ in healthy=true}
    supervisor.onUpdateAccepted={_ in accepted=true}
    supervisor.start();spin(3){healthy}
    let s=box.snapshot()
    let req:[String:Any]=["operation_id":operation,"host_instance_id":host,"launch_generation":s.1[0],
        "boot_id":"boot","pid":s.0[0].processIdentifier,"token":"secret","expires_at":Date().timeIntervalSince1970+5]
    try JSONSerialization.data(withJSONObject:req).write(to:control.appendingPathComponent("update-request-"+operation+".json"),options:.atomic)
    let responseURL=control.appendingPathComponent("update-response-"+operation+".json")
    spin(2){(try?JSONSerialization.jsonObject(with:Data(contentsOf:responseURL)) as? [String:Any])?["state"] as? String=="accepted"}
    RunLoop.current.run(until:Date().addingTimeInterval(0.5))
    precondition(helper?.isRunning==true)
    let ready:[String:Any]=["operation_id":operation,"state":"ready","helper_pid":helper!.processIdentifier]
    try JSONSerialization.data(withJSONObject:ready).write(to:control.appendingPathComponent("update-ready-"+operation+".json"),options:.atomic)
    let during=try JSONSerialization.jsonObject(with:Data(contentsOf:responseURL)) as! [String:Any]
    precondition(during["state"] as? String=="accepted");precondition(!accepted)
    spin(4){(try?JSONSerialization.jsonObject(with:Data(contentsOf:responseURL)) as? [String:Any])?["state"] as? String=="failed"}
    precondition(helper?.isRunning==false);precondition(s.0[0].isRunning);precondition(!accepted)
    precondition(!FileManager.default.fileExists(atPath:control.appendingPathComponent("update-ready-"+operation+".json").path))
    // Replayed terminal request cannot start another helper even before its expiry.
    try JSONSerialization.data(withJSONObject:req).write(to:control.appendingPathComponent("update-request-"+operation+".json"),options:.atomic)
    RunLoop.current.run(until:Date().addingTimeInterval(0.6))
    precondition(helperLaunches==1);precondition(s.0[0].isRunning)
    supervisor.shutdown();s.0[0].waitUntilExit()
}
// Failure to persist the first accepted response must revoke the request, not replay it.
do {
    let box=Box();let host="aabbccddaabbccddaabbccddaabbccdd"
    let control=URL(fileURLWithPath:NSTemporaryDirectory()).appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at:control,withIntermediateDirectories:true)
    defer{try?FileManager.default.removeItem(at:control)}
    let op=String(repeating:"9",count:32);var launches=0;var helper:Process?=nil
    let response=control.appendingPathComponent("update-response-"+op+".json")
    let supervisor=BackendSupervisor(hostInstanceID:host,controlToken:"secret",controlRoot:control,delays:[0],startupTimeout:2,
        updateLauncher:{_,_ in
            launches+=1
            // Atomic response write cannot replace this nonempty directory.
            try FileManager.default.createDirectory(at:response,withIntermediateDirectories:true)
            try Data("block".utf8).write(to:response.appendingPathComponent("block"))
            let p=try process(30);helper=p;return p},
        launcher:{_,g in let p=try process(30);box.lock.lock();box.processes.append(p);box.generations.append(g);box.lock.unlock();return p},
        probeReader:{let s=box.snapshot();guard let p=s.0.last,let g=s.1.last,p.isRunning else{return nil};return BackendProbe(desk:true,role:"local",rev:"x",revScope:"process",hostInstanceID:host,launchGeneration:g,bootID:"boot",pid:p.processIdentifier)},portOwner:{nil})
    var healthy=false;supervisor.onHealthy={_ in healthy=true};supervisor.start();spin(3){healthy}
    let s=box.snapshot();let requestURL=control.appendingPathComponent("update-request-"+op+".json")
    let req:[String:Any]=["operation_id":op,"host_instance_id":host,"launch_generation":s.1[0],"boot_id":"boot",
                        "pid":s.0[0].processIdentifier,"token":"secret","expires_at":Date().timeIntervalSince1970+10]
    try JSONSerialization.data(withJSONObject:req).write(to:requestURL,options:.atomic)
    spin(3){if case .failed=supervisor.phase{return true};return false}
    precondition(helper?.isRunning==false);precondition(!FileManager.default.fileExists(atPath:requestURL.path))
    try FileManager.default.removeItem(at:response)
    supervisor.retry();RunLoop.current.run(until:Date().addingTimeInterval(0.5))
    precondition(launches==1);precondition(s.0[0].isRunning)
    supervisor.shutdown();s.0[0].waitUntilExit()
}
print("backend supervisor: ok")
'''
        with tempfile.TemporaryDirectory(prefix='desk-supervisor-') as tmp:
            main = Path(tmp) / 'main.swift'
            binary = Path(tmp) / 'fixture'
            main.write_text(fixture)
            built = subprocess.run([
                'swiftc', str(ROOT / 'desktop/native/backend_supervisor.swift'),
                str(main), '-o', str(binary)], capture_output=True, text=True, timeout=90)
            self.assertEqual(built.returncode, 0, built.stderr)
            process = subprocess.Popen([str(binary)], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True,
                                       start_new_session=True)
            try:
                stdout, stderr = process.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=5)
                self.fail(f'backend supervisor fixture timed out: {stderr}')
            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn('backend supervisor: ok', stdout)


if __name__ == '__main__':
    unittest.main()
