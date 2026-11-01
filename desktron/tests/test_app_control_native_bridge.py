"""原生 App Control 桥的安全边界由源码契约与真实 swiftc 编译共同验证。"""
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
with open(os.path.join(ROOT, "desktop", "native", "main.swift"), encoding="utf-8") as f:
    SWIFT = f.read()
with open(os.path.join(ROOT, "desktron", "bin", "desktron"), encoding="utf-8") as f:
    LAUNCHER = f.read()


class NativeBridge(unittest.TestCase):
    def test_host_token_stays_in_native_process(self):
        self.assertIn('env["DESKTRON_APP_CONTROL_HOST_TOKEN"] = appControlToken', SWIFT)
        self.assertIn('forHTTPHeaderField: "X-Desk-App-Control-Host"', SWIFT)
        self.assertNotIn("__DESK_APP_CONTROL_HOST_TOKEN", SWIFT)
        capture = LAUNCHER.index('_DESK_APP_CONTROL_HOST_TOKEN="${DESKTRON_APP_CONTROL_HOST_TOKEN:-}"')
        scrub = LAUNCHER.index("unset DESKTRON_APP_CONTROL_HOST_TOKEN", capture)
        lint = LAUNCHER.index('python3 "$PROJECT_ROOT/scripts/check_frontend.py"', scrub)
        restore = LAUNCHER.index('export DESKTRON_APP_CONTROL_HOST_TOKEN="$_DESK_APP_CONTROL_HOST_TOKEN"', lint)
        backend = LAUNCHER.index('exec python3 "$SCRIPT_DIR/../lib/ui_server.py"', restore)
        self.assertLess(capture, scrub)
        self.assertLess(scrub, lint)
        self.assertLess(lint, restore)
        self.assertLess(restore, backend)

    def test_only_main_frame_can_delegate_or_take_over(self):
        self.assertGreaterEqual(SWIFT.count("guard message.frameInfo.isMainFrame else { return }"), 2)

    def test_real_user_interaction_revokes_the_lease(self):
        self.assertIn("if(e.isTrusted)", SWIFT)
        self.assertIn('reason: "user_interaction"', SWIFT)

    def test_runtime_restart_does_not_depend_on_sse_for_results(self):
        self.assertIn("/workbench/app-control/host/result", SWIFT)
        self.assertIn("/workbench/app-control/host/next", SWIFT)

    def test_accessibility_probe_is_read_only_host_owned_and_throttled(self):
        self.assertIn("import ApplicationServices", SWIFT)
        self.assertIn("AXIsProcessTrusted()", SWIFT)
        self.assertNotIn("AXIsProcessTrustedWithOptions", SWIFT)
        report = SWIFT[SWIFT.index("private func reportDesktopCapability"):]
        report = report[:report.index("private func appControlRequest")]
        self.assertIn('"/workbench/app-control/host/desktop-capability"', report)
        self.assertIn("supervisor.hostInstanceID", report)
        self.assertIn("ProcessInfo.processInfo.processIdentifier", report)
        self.assertIn('bundleID == "io.github.desktron"', report)
        self.assertNotIn('?? "io.github.desktron"', report)
        self.assertIn(">= 10", report)
        self.assertLess(report.index("appControlRequest("),
                        report.index("desktopCapabilityReportedAt = now"))
        self.assertIn("DispatchQueue.main.asyncAfter", report)
        healthy = SWIFT[SWIFT.index("supervisor.onHealthy"):]
        healthy = healthy[:healthy.index("supervisor.onPhase")]
        self.assertIn("reportDesktopCapability(force: true)", healthy)
        self.assertIn("func applicationDidBecomeActive", SWIFT)

    def test_poll_registers_browser_and_desk_hosts(self):
        poll = SWIFT[SWIFT.index("private func pollAppControl"):]
        poll = poll[:poll.index("private func executeAppControl")]
        self.assertIn('let apps = ["browser", "desk"]', poll)
        self.assertIn(r'host/next?app_id=\(appId)', poll)

    def test_desk_snapshot_is_allowlisted_and_never_reads_main_content(self):
        snap = SWIFT[SWIFT.index("private func snapshotDeskSurface"):]
        snap = snap[:snap.index("private func executeAppControl")] if "private func executeAppControl" in snap else snap[:snap.index("private func openAgentTab")]
        self.assertIn("desk.surface/v1", snap)
        self.assertIn("data-app-control-surface-id", snap)
        self.assertIn("data-app-control-protected", snap)
        self.assertNotIn("innerText", snap)
        self.assertNotIn("textContent", snap)
        self.assertNotIn("#main-stage", snap)
        self.assertIn("sidebar_open: sidebarOpen", snap)
        self.assertIn("JSON.stringify({items, sidebarOpen})", snap)
        self.assertIn("actions: ['open_page', 'toggle_panel']", snap)

    def test_desk_actions_are_semantic_allowlist(self):
        execute = SWIFT[SWIFT.index("private func executeDeskAppControl"):]
        execute = execute[:execute.index("private func snapshotDeskSurface")]
        self.assertIn('["open_page", "toggle_panel"]', execute)
        self.assertIn("window.__deskAgentCommand", execute)
        self.assertIn('"session_id": deskControlSessionId', execute)
        self.assertIn("leaseId == appControlLeases[resourceId]", execute)
        self.assertIn("sessionId == deskControlSessionId", execute)
        self.assertIn("!appControlPaused.contains(resourceId)", execute)
        self.assertIn("deskOperationStillValid", execute)
        self.assertIn('"interaction_epoch": interactionEpoch', execute)
        self.assertGreaterEqual(execute.count("human_intervention_reinspect_required"), 2)
        for unsafe in ("click", "fill", "type", "press"):
            self.assertNotIn('\"' + unsafe + '\"', execute)

    def test_desk_screenshot_reuses_private_evidence_path_and_hides_protected_surface(self):
        execute = SWIFT[SWIFT.index("private func executeDeskAppControl"):]
        execute = execute[:execute.index("private func deskOperationStillValid")]
        self.assertIn('kind == "inspect" && name == "screenshot"', execute)
        self.assertIn("hideDeskProtectedSurface: true", execute)
        self.assertIn("deskOperationStillValid(operation, interactionEpoch)", execute)
        snapshot = SWIFT[SWIFT.index("private func snapshotAppControl"):]
        snapshot = snapshot[:snapshot.index("private func appControlDOMScript")]
        self.assertIn("[data-app-control-protected]{visibility:hidden!important}", snapshot)
        self.assertGreaterEqual(snapshot.count("stillValid?() ?? true"), 3)
        self.assertIn('"mime_type": "image/png"', snapshot)

    def test_desk_control_messages_are_main_frame_only(self):
        seg = SWIFT[SWIFT.index('if message.name == "deskControlUser"'):]
        seg = seg[:seg.index('guard message.name == "browser"')]
        self.assertGreaterEqual(seg.count("message.frameInfo.isMainFrame"), 2)
        self.assertIn("data-app-control-control-plane", SWIFT)
        self.assertIn(".hs-approval", SWIFT)
        self.assertNotIn("#main-stage", seg)
        user = SWIFT[SWIFT.index('if message.name == "deskControlUser"'):]
        user = user[:user.index('if message.name == "deskControl"')]
        self.assertLess(user.index("deskControlRequestGeneration += 1"),
                        user.index('pauseAppControl(id: deskControlResourceId'))

    def test_approval_and_grant_ledgers_use_fixed_native_bridge(self):
        self.assertIn('name: "approvalControl"', SWIFT)
        seg = SWIFT[SWIFT.index('if message.name == "approvalControl"'):]
        seg = seg[:seg.index('guard message.name == "browser"')]
        self.assertIn("message.frameInfo.isMainFrame", seg)
        for action in ('case "list"', 'case "decide"', 'case "clear"',
                       'case "grantsList"', 'case "grantRevoke"'):
            self.assertIn(action, seg)
        self.assertIn('path = "/workbench/app-control/grants?app_id=browser"', seg)
        self.assertIn('"app_id": "browser"', seg)
        self.assertNotIn('body["path"]', seg)

    def test_late_desk_delegation_is_revoked(self):
        seg = SWIFT[SWIFT.index("private func delegateDeskControl"):]
        seg = seg[:seg.index("func toggleAppControl")]
        self.assertIn("deskControlRequestGeneration += 1", seg)
        self.assertIn("generation == self.deskControlRequestGeneration", seg)
        self.assertIn('"reason": "resource_changed"', seg)
        self.assertLess(seg.index("generation == self.deskControlRequestGeneration"),
                        seg.index("guard error == nil"))
        self.assertIn("deskControlSessionId = sessionId", seg)
        self.assertIn("sessionId: id == deskControlResourceId ? deskControlSessionId", SWIFT)
        self.assertIn("let boundSession = isDesk && sessionId.isEmpty ? deskControlSessionId", SWIFT)

    def test_arbitrary_javascript_is_not_an_action(self):
        self.assertNotIn("command.name === 'javascript'", SWIFT)
        self.assertIn("unsupported_action", SWIFT)

    @unittest.skipUnless(shutil.which("node"), "需要 node 校验注入脚本")
    def test_isolated_dom_adapter_javascript_parses(self):
        match = re.search(r"private func appControlDOMScript\(\) -> String \{\s*return #\"\"\"(.*?)\"\"\"#",
                          SWIFT, re.S)
        self.assertIsNotNone(match)
        source = "async function adapter(command, epoch) {\n" + match.group(1) + "\n}"
        result = subprocess.run(["node", "--check", "-"], input=source,
                                text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 node 校验网络追踪脚本")
    def test_page_world_network_tracker_javascript_parses(self):
        match = re.search(r"source: #\"\"\"\s*(\(\(\)=>\{if\(globalThis\.__deskNetworkTracker\).*?)\s*\"\"\"#",
                          SWIFT, re.S)
        self.assertIsNotNone(match)
        result = subprocess.run(["node", "--check", "-"], input=match.group(1),
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

class BrowserSearchSecurity(unittest.TestCase):
    def test_主框架导航受精确域名白名单约束(self):
        seg = SWIFT[SWIFT.index("func webView(_ wv: WKWebView, decidePolicyFor action") :]
        seg = seg[:seg.index("func webView(_ wv: WKWebView, decidePolicyFor response")]
        self.assertIn("appControlAllowedMainFrameHosts", seg)
        self.assertIn('url.scheme == "https"', seg)
        self.assertIn("allowed.contains(host)", seg)
        self.assertIn("decisionHandler(.cancel)", seg)

    def test_文档链接仅暴露清洗后的http地址(self):
        seg = SWIFT[SWIFT.index("const safeHref = (el)") :]
        seg = seg[:seg.index("const rendered =")]
        self.assertIn("el.ownerDocument?.baseURI", seg)
        self.assertIn("u.protocol !== 'http:' && u.protocol !== 'https:'", seg)
        self.assertIn("u.username || u.password", seg)
        self.assertIn("some((key) => sensitiveKey.test(key))) return ''", seg)
        self.assertIn("u.hash = ''", seg)
        self.assertIn("role.toLowerCase() === 'link'", SWIFT)
        self.assertIn("...(href ? {href} : {})", SWIFT)


class PauseNotRevoke(unittest.TestCase):
    """页面里的真实输入走暂停,不走销毁(2026-09-02)。"""

    def test_交互触发暂停而不是撤销(self):
        seg = SWIFT[SWIFT.index('if message.name == "appControlUser"'):]
        seg = seg[:seg.index("return") + 6]
        self.assertIn('pauseAppControl(id: id, reason: "user_interaction")', seg)
        self.assertNotIn("revokeAppControl", seg)

    def test_暂停期间不派活(self):
        """这条闸 2026-09-02 从原生挪到 Runtime。

        取活改成全局(不带 resource_id)之后,原生这侧没有「按资源过滤」的位置了;
        而 Runtime 的 next_operation 本来就 JOIN 了 l.status='active',暂停的租约不是
        active,自然派不出活。闸只留一处,不在两边各写一遍。
        """
        seg = SWIFT[SWIFT.index("private func pollAppControl"):]
        seg = seg[:seg.index("private func executeAppControl")]
        self.assertIn("resumeIdleAppControl()", seg)
        rt = open(os.path.join(LIB, "app_control", "runtime.py"), encoding="utf-8").read()
        nxt = rt[rt.index("def next_operation"):rt.index("def pending_approval")]
        self.assertIn("l.status='active'", nxt)

    def test_静默够久自动恢复(self):
        seg = SWIFT[SWIFT.index("private func resumeIdleAppControl"):]
        seg = seg[:seg.index("func revokeAppControl")]
        self.assertIn("APP_CONTROL_RESUME_QUIET", seg)
        self.assertIn("/workbench/app-control/host/resume", seg)
        # 恢复失败要收口成撤销,不能把界面永久停在"已让开"
        self.assertIn('type: "revoked"', seg)
        self.assertEqual(seg.count('deskControlResourceId = ""'), 2)
        self.assertEqual(seg.count('deskControlSessionId = ""'), 2)

    def test_撤销带原因并清暂停态(self):
        seg = SWIFT[SWIFT.index("func revokeAppControl"):]
        seg = seg[:seg.index("private static let APP_CONTROL_RESUME_QUIET")]
        self.assertIn("appControlPaused.remove(id)", seg)
        self.assertIn('type: "revoked", leaseId: lease, error: reason', seg)

    def test_切标签让开切回接续(self):
        # 三个意图各走各的:让开 / 接回 / 收回。收回要用调用方给的缘由,
        # 原来一律写死 user_takeover,导航与按钮在回执里分不出来。
        self.assertIn('case "agentPause"', SWIFT)
        self.assertIn('case "agentResume"', SWIFT)
        self.assertIn('revokeAppControl(id: id, reason: b["reason"] as? String ?? "user_takeover")', SWIFT)

    def test_只有本页输入才自动接回(self):
        seg = SWIFT[SWIFT.index("private func resumeIdleAppControl"):]
        seg = seg[:seg.index("func resumeAppControl")]
        # 切走标签那种要等人切回来:委托范围是「当前可见标签」,人没看着不该继续动
        self.assertIn('appControlPauseReason[id] ?? "user_interaction") == "user_interaction"', seg)


    def test_取活是全局且常驻(self):
        seg = SWIFT[SWIFT.index("private func pollAppControl"):]
        seg = seg[:seg.index("private func executeAppControl")]
        # 自开资源在执行前原生这侧并不知道它存在,按自己那张租约表逐个问会永远取不到
        self.assertIn('let apps = ["browser", "desk"]', seg)
        self.assertIn(r'host/next?app_id=\(appId)', seg)
        self.assertNotIn("resource_id=", seg)
        # 计时器要在启动时就起,并且租约清空后只降速不停表
        self.assertIn("ensureAppControlTimer()", SWIFT[SWIFT.index("func applicationDidFinishLaunching"):
                                                       SWIFT.index("func applicationDidFinishLaunching") + 3000])
        timer = SWIFT[SWIFT.index("private func ensureAppControlTimer"):]
        timer = timer[:timer.index("RunLoop.main.add")]
        self.assertIn("appControlLeases.isEmpty", timer)
        self.assertIn("appControlPollInterval(", timer)

    @unittest.skipUnless(shutil.which('swift'), 'Swift compiler unavailable')
    def test_匿名标签操作触发短突发且空闲后自动恢复低频(self):
        helper = re.search(r'func appControlPollInterval\([^\n]+\) -> TimeInterval \{.*?\n\}', SWIFT, re.S)
        self.assertIsNotNone(helper)
        source = 'import Foundation\n' + helper.group(0) + '''
assert(appControlPollInterval(hasLeases: false, now: 100, activeUntil: 0) == 1.5)
assert(appControlPollInterval(hasLeases: false, now: 100, activeUntil: 105) == 0.35)
assert(appControlPollInterval(hasLeases: false, now: 105, activeUntil: 105) == 1.5)
assert(appControlPollInterval(hasLeases: true, now: 110, activeUntil: 105) == 0.35)
'''
        result = subprocess.run([shutil.which('swift'), '-'], input=source, text=True,
                                capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        poll = SWIFT[SWIFT.index('private func pollAppControl'):SWIFT.index('private func executeDeskAppControl')]
        self.assertIn('ensureAppControlTimer()', poll)
        self.assertIn('ProcessInfo.processInfo.systemUptime + 5', poll)
        self.assertIn('guard !appControlPolling', poll)  # no overlapping host requests

    def test_审批只在会话过程区呈现(self):
        """原生弹窗会与会话卡片竞速，也让移动端无法独立完成审批。"""
        seg = SWIFT[SWIFT.index("private func pollAppControl"):]
        seg = seg[:seg.index("private func executeAppControl")]
        self.assertNotIn("pending-approval", seg)
        self.assertNotIn("NSAlert()", seg)
        self.assertNotIn("promptAppControlApproval", SWIFT)

class AgentOwnedTabs(unittest.TestCase):
    """Agent 自开标签:隔离存储 + 由前端建标签(2026-09-02)。"""

    def test_隔离标签用ephemeral存储(self):
        seg = SWIFT[SWIFT.index("func hardenedConfig"):]
        seg = seg[:seg.index("func openBrowser")]
        self.assertIn("isolated ? WKWebsiteDataStore.nonPersistent()", seg)
        self.assertIn("WKWebsiteDataStore.default()", seg)   # 委托标签仍用持久存储

    def test_开页交给前端建标签(self):
        seg = SWIFT[SWIFT.index("private func openAgentTab"):]
        seg = seg[:seg.index("private func finishAppControl")]
        # 标签模型在前端;原生自建视图会得到界面上不存在、用户关不掉的孤儿标签
        self.assertIn("window.__browserAgentOpen", seg)
        self.assertIn('u.scheme == "http" || u.scheme == "https"', seg)
        self.assertIn("browser_app_not_open", seg)

    def test_open动作在视图存在前就放行(self):
        seg = SWIFT[SWIFT.index("private func executeAppControl"):]
        seg = seg[:seg.index("private func finishAppControl")]
        # open 就是去建视图的,不能被「视图必须已存在」那道守卫挡住
        self.assertLess(seg.index("openAgentTab"), seg.index("browser_tab_unavailable"))


    def test_视图就绪才报开页成功(self):
        """前端建的是标签模型,原生视图要等应用可见且布局出非零尺寸才创建。

        2026-09-03 另一会话实见:open succeeded,随后 inspect 连续三次 browser_tab_unavailable
        ——成功报早了。等不到视图就如实失败,别让调用方以为拿到了页面。
        """
        seg = SWIFT[SWIFT.index("private func openAgentTab"):]
        seg = seg[:seg.index("private func waitForBrowserView")]
        self.assertIn("waitForBrowserView(resourceId", seg)
        self.assertIn("browser_view_not_created", seg)
        # 成功那一支必须在等待回调里面,不能在外面直接报
        ok_at = seg.index("observationVersion: \"1:open\"")
        wait_at = seg.index("waitForBrowserView(resourceId")
        self.assertLess(wait_at, ok_at)
        wait = SWIFT[SWIFT.index("private func waitForBrowserView"):]
        wait = wait[:wait.index("private func finishAppControl")]
        self.assertIn("browsers[id] != nil", wait)
        self.assertIn("tries - 1", wait)


    def test_自开页在后台也能建视图(self):
        """Agent 自开页的意义就是在后台干活,不该要求浏览器应用在前台。

        建视图原本要过前端 browserVisible 那道闸;人正看着会话时它恒为假,视图永远建不出来
        (2026-09-03 另一会话连撞两轮)。抢占前台同样不对——会打断人正在读的东西。
        """
        seg = SWIFT[SWIFT.index("func openBrowser"):]
        seg = seg[:seg.index("func browserVisibleNative") if "func browserVisibleNative" in seg
                  else seg.index("\n    func ", 10)]
        self.assertIn("background: Bool = false", seg)
        self.assertIn("if background {", seg)
        self.assertIn("v.isHidden = true", seg)
        # 零尺寸的 WKWebView 不做布局,快照与元素矩形全是 0
        self.assertIn("width: 1280, height: 800", seg)
        # 后台建视图不能动别的面板,也不能把自己提到最前
        bg = seg[seg.index("if background {"):]
        bg = bg[:bg.index("}")]
        self.assertNotIn("isHidden = false", bg)
        self.assertIn('background: b["background"] as? Bool ?? false', SWIFT)


class AgentLeaseFollowsView(unittest.TestCase):
    """Agent 自开页的租约由 Runtime 自签,原生不认识它。视图没了却不撤租约,调用方只会
    对着一张永远不会好的标签重试；宿主页重载但视图仍在时则应保留。"""

    def test_执行时记下自签租约(self):
        seg = SWIFT[SWIFT.index("private func executeAppControl"):]
        seg = seg[:seg.index("browserLastUsed[resourceId]")]
        self.assertIn("appControlLeases[resourceId] == nil", seg)
        self.assertIn("appControlAgentLeases[resourceId] = lease", seg)

    def test_视图销毁就撤掉自签租约(self):
        seg = SWIFT[SWIFT.index("private func discardBrowserView"):]
        seg = seg[:seg.index("\n    }")]
        self.assertIn("revokeAgentLease(", seg)
        rev = SWIFT[SWIFT.index("func revokeAgentLease"):]
        rev = rev[:rev.index("\n    }")]
        self.assertIn("host/takeover", rev)
        self.assertIn("\"reason\": reason", rev)

    def test_宿主页重载只停放视图不撤租约(self):
        seg = SWIFT[SWIFT.index("private func parkNativeOverlays"):]
        seg = seg[:seg.index("\n    }")]
        self.assertIn("v.isHidden = true", seg)
        self.assertNotIn("revokeAgentLease", seg)
        self.assertNotIn("revokeAppControl", seg)
        self.assertIn("replayAppControlState()", SWIFT)

    def test_视图生死留痕(self):
        for what in ("create", "discard", "miss", "rebuild"):
            self.assertIn('browserViewLog(', SWIFT)
            self.assertIn('"%s"' % what, SWIFT)
        self.assertIn("native-browser.log", SWIFT)


class InspectWaitsForSettle(unittest.TestCase):
    """重前端应用在 load 之后还会接着挂节点,inspect 读早了拿到的是半成品。"""

    def test_只有_inspect_等安定(self):
        seg = SWIFT[SWIFT.index("let runAdapter:"):]
        seg = seg[:seg.index("private func waitForPageSettled")]
        self.assertIn('kind == "inspect" && budget > 0', seg)
        self.assertIn("runAdapter(nil)", seg)

    def test_等到文档完成且节点数稳定(self):
        seg = SWIFT[SWIFT.index("private func waitForPageSettled"):]
        seg = seg[:seg.index("\n    }\n")]
        self.assertIn("readyState==='complete'", seg)
        self.assertIn("n == last", seg)
        self.assertIn("done(false)", seg)   # 超预算要如实说没安定

    def test_读早了要看得见(self):
        seg = SWIFT[SWIFT.index("let runAdapter:"):]
        seg = seg[:seg.index("private func waitForPageSettled")]
        self.assertIn('d["settled"] = settled', seg)


class BrowserViewLifecycle(unittest.TestCase):
    """短期保活页面状态，长期按 LRU 释放 WKWebView。"""

    def test_单页隐藏不等于关闭(self):
        switch = SWIFT[SWIFT.index('switch action {'):SWIFT.index('func pickFolder')]
        self.assertIn('case "hideOne": if let v = browsers[id] { v.isHidden = true }', switch)
        self.assertIn('case "close":', switch)
        self.assertRegex(switch, r'discardBrowserView\(id[,)]')

    def test_只回收浏览器标签且保护租约(self):
        seg = SWIFT[SWIFT.index("private func trimBrowserViews"):]
        seg = seg[:seg.index("private func discardBrowserView")]
        self.assertIn("browserTabIds.filter", seg)
        self.assertIn("appControlLeases[id] == nil", seg)
        self.assertIn("residentLimit = 6", seg)
        self.assertIn("15 * 60", seg)
        self.assertIn('!id.hasPrefix("agent-")', seg)

    def test_回收释放原生引用但不动前端标签模型(self):
        seg = SWIFT[SWIFT.index("private func discardBrowserView"):]
        seg = seg[:seg.index("// ---- Agent 可操作应用")]
        self.assertIn("v.stopLoading()", seg)
        self.assertIn("v.removeFromSuperview()", seg)
        self.assertIn("browserTabIds.remove(id)", seg)
