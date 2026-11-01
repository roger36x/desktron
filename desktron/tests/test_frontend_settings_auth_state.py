"""执行设置页实际渲染片段，区分查询失败、未配置与已配置。"""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from frontend_i18n_js import NODE_PRELUDE

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "frontend/app-settings-core.js").read_text(encoding="utf-8")
BLOCKS = {
    "claude": ("const cl = smSection", "const loginBtn =", "st"),
    "codex": ("const cx = smSection", "const cxLogin =", "cxst"),
    "pi": ("const pi = smSection", "const piLogin =", "pist"),
}


@unittest.skipUnless(shutil.which("node"), "需要 Node 执行真实前端片段")
class AuthState(unittest.TestCase):
    def render(self, engine, state):
        start, end, node = BLOCKS[engine]
        block = JS[JS.index(start):JS.index(end, JS.index(start))]
        i = JS.index("function _pvState(")
        paint = JS[i:JS.index("\n}", i) + 2]
        code = """
const document = {createElement: () => ({})};
const BRAND_ICONS = {};
const smSection = () => ({});
const escapeHtml = s => s;
const _PV_CHECK = '<check>';
let meta;
const _routeSettingsSection = (_section, _view, value) => { meta = value; };
""" + "const s = " + json.dumps({engine: state}) + ";\n" + paint + block
        code += f"\nconsole.log(JSON.stringify({{text:meta.state, ready:!!meta.ready, html:{node}.innerHTML}}));"
        r = subprocess.run(["node", "-e", NODE_PRELUDE + code], capture_output=True, text=True, timeout=10, encoding='utf-8')
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_未知状态不冒充未配置或已登录(self):
        for engine in BLOCKS:
            with self.subTest(engine=engine):
                got = self.render(engine, {"unknown": True})
                self.assertEqual(got["text"], "状态未知")
                self.assertFalse(got["ready"])
                self.assertIn("状态未知", got["html"])
                self.assertNotIn("<check>", got["html"])

    def test_明确无凭证仍显示未配置(self):
        for engine in ("claude", "codex"):
            with self.subTest(engine=engine):
                got = self.render(engine, {"unknown": False, "api_key_set": False})
                self.assertEqual(got["text"], "未配置")
                self.assertFalse(got["ready"])

    def test_订阅查询失败不遮盖已配置的_key(self):
        for engine in ("claude", "codex"):
            with self.subTest(engine=engine):
                got = self.render(engine, {"unknown": True, "api_key_set": True})
                self.assertEqual(got["text"], "使用 API key")
                self.assertTrue(got["ready"])

    def test_重新查询成功恢复已登录(self):
        for engine, field, label in (("claude", "subscription", "订阅已登录"),
                                     ("codex", "logged_in", "已登录")):
            with self.subTest(engine=engine):
                self.assertEqual(self.render(engine, {"unknown": True})["text"], "状态未知")
                got = self.render(engine, {field: True})
                self.assertEqual(got["text"], label)
                self.assertTrue(got["ready"])
                self.assertIn("<check>", got["html"])
