"""首次设置的检查清单(2026-09-01)。

这一页的价值在「指路」:告诉人当前该做哪一步,以及为什么某一步还做不了。
所以判据的取舍都围绕一件事——不谎报。查不到不等于没登录,已经做成的不因前置条件
被否定,还在查的不显示成失败。三条都在下面钉着,它们各自都是实跑时发现的。

探测本身不在这里做:三处已有判断且都带缓存(见 provision/setup.py 的说明),
这个模块只做编排。再写一份就会与设置页说法不一,人不知道该信哪个。
"""
import json
import os
import subprocess
import sys
import unittest

from frontend_i18n_js import SOURCE as I18N_SOURCE
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
sys.argv = ["test", "/tmp/tsg-frontend", "0", "0", "/tmp/tsg-repo"]   # deskcfg 读 argv
from provision import setup  # noqa: E402
from core import api as core_api  # noqa: E402


def _st(runtime=True, engines=None, intra=None):
    """把三处数据源换成给定值,只测编排。"""
    engines = engines if engines is not None else [
        {"key": "claude", "label": "Claude Code", "installed": True, "logged_in": True, "unknown": False},
        {"key": "pi", "label": "Pi", "installed": True, "logged_in": False, "unknown": False}]
    intra = intra if intra is not None else {
        "rows": [{"key": "lark", "label": "lark-cli", "hint": "", "installed": True,
                  "logged_in": True, "unknown": False, "why": ""}],
        "pending": False}
    with mock.patch.object(setup, "_runtime", lambda: {"ok": runtime, "rows": [], "missing": []}), \
         mock.patch.object(setup, "_engines", lambda: engines), \
         mock.patch.object(setup, "_intranet", lambda: intra):
        return setup.status()


class 不谎报(unittest.TestCase):
    def test_查不到不算未登录(self):
        """外部 CLI 可能很慢,首访拿到的是 pending。
        判成失败的话,首启第一眼一个红叉、几十秒后自己变绿,中间人已经跑去登录了。"""
        intra = {"rows": [{"key": "lark", "installed": True, "logged_in": False,
                           "unknown": True, "label": "", "hint": "", "why": ""}],
                 "pending": True}
        st = _st(intra=intra)
        self.assertTrue(st["groups"]["intranet"]["ok"])
        self.assertTrue(st["groups"]["intranet"]["pending"])

    def test_任一工作引擎可用即可(self):
        engines = [
            {"key": "claude", "installed": True, "logged_in": False, "unknown": False, "label": ""},
            {"key": "codex", "installed": True, "logged_in": True, "unknown": False, "label": ""},
            {"key": "pi", "installed": True, "logged_in": False, "unknown": False, "label": ""}]
        st = _st(engines=engines)
        self.assertTrue(st["groups"]["engine_login"]["ok"])

    def test_Pi_不是首次设置硬依赖(self):
        engines = [
            {"key": "claude", "installed": True, "logged_in": True, "unknown": False, "label": ""},
            {"key": "pi", "installed": False, "logged_in": False, "unknown": False, "label": ""}]
        st = _st(engines=engines)
        self.assertTrue(st["groups"]["engines"]["ok"])
        self.assertTrue(st["complete"])

    def test_没有任何引擎时引导安装并登录(self):
        engines = [
            {"key": "claude", "installed": False, "logged_in": False, "unknown": False, "label": ""},
            {"key": "codex", "installed": False, "logged_in": False, "unknown": False, "label": ""}]
        st = _st(engines=engines)
        self.assertFalse(st["groups"]["engines"]["ok"])
        self.assertEqual(st["next"], "engine_login")

    def test_没装的引擎不拖累登录判定(self):
        """没装的引擎不该把人卡死在登录这一步。"""
        engines = [
            {"key": "claude", "installed": True, "logged_in": True, "unknown": False, "label": ""},
            {"key": "codex", "installed": False, "logged_in": False, "unknown": False, "label": ""}]
        st = _st(engines=engines)
        self.assertTrue(st["groups"]["engine_login"]["ok"])


class 各家登录字段不同名(unittest.TestCase):
    """claude 报 subscription(钥匙串条目),codex/pi 报 logged_in。
    只认 logged_in 会把 claude 判成未登录——而人正是通过 claude 在用 Desk。
    这种误报比不报更坏:它让一个已经配好的环境显示成没配好(2026-09-01 用户指出)。"""

    def _engines_real(self, cred):
        with mock.patch.object(setup, "shutil", create=True), \
             mock.patch("shutil.which", lambda n: "/usr/bin/" + n), \
             mock.patch.dict(sys.modules, {"settings": mock.Mock(_settings_get=lambda: cred)}):
            return setup._engines()

    def test_claude_的_subscription_算登录(self):
        got = self._engines_real({"claude": {"subscription": True, "api_key_set": False}})
        self.assertTrue(next(e["logged_in"] for e in got if e["key"] == "claude"))

    def test_其余三家的_logged_in_照旧(self):
        got = self._engines_real({"codex": {"logged_in": True}, "pi": {"logged_in": True}})
        self.assertTrue(next(e["logged_in"] for e in got if e["key"] == "codex"))
        self.assertTrue(next(e["logged_in"] for e in got if e["key"] == "pi"))

    def test_都没有就是未登录(self):
        got = self._engines_real({"claude": {"subscription": False, "api_key_set": False}})
        self.assertFalse(next(e["logged_in"] for e in got if e["key"] == "claude"))

    def test_api_key_也算配好(self):
        """claude/codex 用 key 的那条路一样能跑,只认 OAuth 会把这些人判成未配。"""
        got = self._engines_real({"claude": {"subscription": False, "api_key_set": True}})
        self.assertTrue(next(e["logged_in"] for e in got if e["key"] == "claude"))


class 进度只算必要项(unittest.TestCase):
    def test_不用工作工具也算配完(self):
        """lark-cli 是可选的。算进必要项的话,不用飞书的人永远差一项,
        而首启弹窗的判据正是「有没有配完」。"""
        intra = {"rows": [{"key": "lark", "installed": False, "logged_in": False,
                           "unknown": False, "label": "", "hint": "", "why": ""}],
                 "pending": False}
        st = _st(intra=intra)
        self.assertTrue(st["complete"])
        self.assertEqual((st["done"], st["total"]), (2, 2))
        self.assertNotIn("intranet", st["required"])

    def test_next_指向第一个未完成的必要项(self):
        st = _st(runtime=False)
        self.assertEqual(st["next"], "runtime")


class 安装记录不影响就绪(unittest.TestCase):
    def test_仅未结束的任务保留进度入口(self):
        for state in (None, "succeeded", "failed", "interrupted",
                      "starting", "waiting_terminal", "running"):
            with self.subTest(state=state), mock.patch(
                    "provision.setup_tasks.latest",
                    return_value={"state": state} if state else None):
                for ready in (True, False):
                    st = _st(runtime=ready)
                    self.assertEqual(st["complete"], ready)
                    self.assertEqual(st["operation_pending"], state in
                                     ("starting", "waiting_terminal", "running"))

    def test_任务记录不可读仍保留恢复入口(self):
        for error in (OSError("unreadable"), ValueError("invalid")):
            with self.subTest(error=error), mock.patch(
                    "provision.setup_tasks.latest", side_effect=error):
                st = _st()
                self.assertTrue(st["complete"])
                self.assertTrue(st["operation_pending"])

    def test_底栏随就绪及任务状态更新(self):
        # 用真实后端聚合结果驱动真实前端函数，覆盖已有按钮改名及移除。
        states = []
        for ready, state in ((False, "failed"), (True, "running"),
                             (True, "failed"), (True, "interrupted"),
                             (True, "succeeded"), (True, None),
                             (True, "starting"), (False, "running"),
                             (True, "waiting_terminal"), (True, "failed")):
            with mock.patch("provision.setup_tasks.latest",
                            return_value={"state": state} if state else None):
                states.append(_st(runtime=ready))
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
let buttons = [], opens = 0;
const parent = { insertBefore(button) { button.isConnected = true; buttons.push(button); } };
const document = {
  getElementById: () => ({parentNode: parent}),
  createElement: () => ({attrs: {}, getAttribute(k) { return this.attrs[k] || ''; },
    setAttribute(k, v) { this.attrs[k] = v; if (k === 'title') this.title = v; },
    remove() { this.isConnected = false; buttons = buttons.filter(b => b !== this); }})
};
const ctx = vm.createContext({document, isLocalView: () => true});
vm.runInContext(fs.readFileSync(0, 'utf8'), ctx); // DeskI18n via stdin, not an oversized Windows argv
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
ctx.openSetupGuide = () => { opens++; };
(async () => {
  for (const st of JSON.parse(process.argv[2])) {
    const previous = buttons[0];
    await ctx.setupResumeEntry(st);
    const visible = !st.complete || st.operation_pending;
    assert.equal(buttons.length, visible ? 1 : 0);
    if (visible) {
      const button = buttons[0];
      if (previous) assert.equal(button, previous);
      assert.equal(button.textContent, st.complete ? '查看安装进度' : '继续设置');
      assert.equal(button.title, st.complete ? '查看首次设置操作进度' : '继续完成 Desktron 首次设置');
      button.onclick();
      assert.ok(opens > 0);
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(
            ["node", "-e", script, os.path.join(ROOT, "frontend/app-settings-setup.js"),
             json.dumps(states)], input=I18N_SOURCE, capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class 首启标记(unittest.TestCase):
    def test_打开即记_不等配完(self):
        """配到一半关掉、下次启动又被弹一次,那是骚扰。标记只管弹不弹。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(setup, "_mark_path", lambda: os.path.join(d, ".setup-seen")):
                self.assertFalse(setup.seen())
                self.assertTrue(setup.mark_seen())
                self.assertTrue(setup.seen())

    def test_端点认领与两个方法(self):
        with open(os.path.join(ROOT, "desktron", "lib", "provision", "api.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"/workbench/setup"', src)
        self.assertNotIn('"/workbench/setup-agent"', src)
        self.assertIn("_setup.status()", src)
        self.assertIn("_setup.mark_seen()", src)
        self.assertIn("_setup.refresh()", src)

    def test_重新检查失效登录探测缓存(self):
        """登录发生在嵌入终端里；刷新时先失效探测缓存，再返回真实状态。"""
        intranet = mock.Mock()
        settings = mock.Mock()
        expected = {"complete": True}
        with mock.patch.dict(sys.modules, {"system.api": intranet, "settings": settings}), \
             mock.patch.object(setup, "status", return_value=expected):
            self.assertEqual(setup.refresh(), expected)
        intranet.invalidate_intranet_status.assert_called_once_with()
        settings.invalidate_auth_probe_caches.assert_called_once_with()

    def _read(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_分片已登记进合包(self):
        src = self._read("desktron/lib/frontend_bundle.py")
        m = __import__("re").search(r'"/app-settings\.js": \(([^)]*)\)', src, __import__("re").S)
        self.assertIsNotNone(m)
        self.assertIn("app-settings-setup.js", m.group(1))
        # 顺序是运行契约:它用 sections 的形制、core 的 renderSettings,得排在两者之后
        seg = m.group(1)
        self.assertLess(seg.index("app-settings-core.js"), seg.index("app-settings-setup.js"))
        self.assertLess(seg.index("app-settings-sections.js"), seg.index("app-settings-setup.js"))

    def test_首启只弹一次且只在本机(self):
        src = self._read("frontend/app-init.js")
        self.assertIn("openSetupGuide({ auto: true })", src)
        at = src.index("openSetupGuide({ auto: true })")
        # 手机是连过来的,配置动作都发生在本机那一头,在手机上弹一个自己做不了的清单只是打扰
        self.assertIn("isLocalView()", src[max(0, at - 400):at])

    def test_auto_那一路认_seen_且失败静默(self):
        src = self._read("frontend/app-settings-setup.js")
        self.assertIn("if (opts && opts.auto && st.seen) return;", src)
        self.assertIn("if (opts && opts.auto) return;", src)   # 取状态失败时不打断启动

    def test_打开即记不等配完(self):
        src = self._read("frontend/app-settings-setup.js")
        self.assertIn('body: JSON.stringify({ seen: true })', src)
        # 记在打开时,不在"配完"的分支里
        at = src.index('JSON.stringify({ seen: true })')
        self.assertIn("if (!st.seen)", src[max(0, at - 300):at])

    def test_设置页有入口(self):
        src = self._read("frontend/app-settings-core.js")
        self.assertIn("openSetupGuide({})", src)
        self.assertIn('smSection("首次设置"', src)

    def test_终端进入独立操作助手且不撑开步骤卡(self):
        """步骤页保持稳定；需要交互时进入有说明、有返回路径的子层。"""
        src = self._read("frontend/app-settings-setup.js")
        style = self._read("frontend/style.css")
        self.assertIn("_setupRunInPanel", src)
        self.assertGreater(src.index('class="setup-operation"'), src.index('class="setup-footer"'))
        focus = src[src.index('class="setup-content-grid"'):src.index('class="setup-footer"')]
        self.assertNotIn('class="setup-operation"', focus)
        self.assertIn(".setup-operation { position: absolute; inset: 0", style)
        self.assertIn("操作已经自动开始，无需输入命令", src)
        self.assertIn('data-setup-action="finish-terminal"', src)

    def test_真实必要项与动作已接线(self):
        src = self._read("frontend/app-settings-setup.js")
        for key in ('runtime', 'engines', 'engine_login', 'intranet'):
            self.assertIn(key, src)
        self.assertIn('st.done || 0', src)
        self.assertIn('st.total || 0', src)
        self.assertIn('body: JSON.stringify({ refresh: true })', src)
        self.assertIn('lark-cli auth login --domain all', src)
        self.assertIn('e.install', src)
        self.assertNotIn('api("/workbench/setup-agent"', src)
        self.assertNotIn('准备设置助手', src)
        self.assertIn('st.complete ? "setup.progress" : "setup.resume"', src)
        self.assertIn("setupResumeEntry(st)", src)
        self.assertIn('optional: true', src)

    def test_配置动作全部留在引导页(self):
        src = self._read("frontend/app-settings-setup.js")
        self.assertNotIn('data-setup-action="settings"', src)
        self.assertNotIn('action === "settings"', src)
        for cmd in ('claude auth login', 'codex login'):
            self.assertIn(cmd, src)
        self.assertIn('await _setupRunInPanel(back, cmd, head)', src)

    def test_配置按钮直接执行且授权页自动打开(self):
        setup = self._read("frontend/app-settings-setup.js")
        term = self._read("frontend/app-assets-term.js")
        self.assertIn('JSON.stringify({ command: cmd,', setup)
        self.assertIn('setup_request_id:', setup)
        self.assertIn("termOpts.onOutputLink = (url) => _setupReceiveAuthLink", setup)
        self.assertIn("onSessionClose: (event) => setTimeout", setup)
        self.assertIn('api("/workbench/open-external"', setup)
        self.assertIn('window.open(url, "_blank", "noopener")', setup)
        self.assertIn("打开授权页面", setup)
        self.assertIn("function _setupTrustedAuthLink", setup)
        self.assertIn("onOutputLink", term)

    def test_配置页使用全局中性色与克制紫色(self):
        """大面积表面跟随全局中性色；紫色只留给选中态和主要动作。"""
        src = self._read("frontend/style.css")
        start = src.index("/* ── 首次设置浮层")
        block = src[start:src.index("@media (max-width: 900px)", start)]
        self.assertIn("background: var(--surface-card)", block)
        self.assertIn("border-color: var(--border-base)", block)
        self.assertIn("#402c66", block)
        self.assertNotIn("#262525", block)
        self.assertNotIn("153,164,244", block)
        self.assertNotIn("125,173,139", block)

    def test_就绪状态标签清楚但保持克制(self):
        src = self._read("frontend/style.css")
        start = src.index("/* ── 首次设置浮层")
        block = src[start:src.index("@media (max-width: 900px)", start)]
        self.assertIn(".setup-node-state::before", block)
        self.assertIn("border-radius: 999px", block)
        self.assertIn("font-size: 10.5px", block)
        self.assertIn("font-weight: 510", block)
        self.assertIn(".setup-system-node.current:not(.selected) .setup-node-state", block)
        self.assertIn('.setup-score.ready strong::before', block)
        self.assertIn('.setup-action-prompt', block)
        self.assertNotIn("animation:", block)

    def test_就绪勾使用统一成功绿色(self):
        src = self._read("frontend/style.css")
        start = src.index("/* ── 首次设置浮层")
        block = src[start:src.index("@media (max-width: 900px)", start)]
        self.assertIn('.setup-score.ready strong::before', block)
        self.assertIn('.setup-system-node.done .setup-node-state::before', block)
        self.assertEqual(block.count("color: #56d364"), 2)
        self.assertEqual(block.count("background: rgba(86,211,100,.12)"), 1)
        self.assertIn('color: #15803d', block)
        self.assertIn('background: rgba(21,128,61,.1)', block)

    def test_下一步和就绪结论在手机端仍然可见(self):
        setup = self._read("frontend/app-settings-setup.js")
        style = self._read("frontend/style.css")
        self.assertIn('st.complete ? "已就绪" : `还需完成 ${remaining} 项`', setup)
        self.assertIn('DeskI18n.tx("点击“{action}”继续", { action: primary.textContent.trim() })', setup)
        self.assertIn('.setup-score { width: 100%; min-width: 0; }', style)
        self.assertNotIn('.setup-score { display: none; }', style)


if __name__ == "__main__":
    unittest.main(argv=["test_setup_guide"])   # sys.argv 已被 deskcfg 借道
