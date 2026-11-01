"""服务重启与前端升级通过已有 SSE 握手自愈，不增加后台轮询。"""
import ast
import os
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SYSTEM = open(os.path.join(ROOT, "desktron", "lib", "system", "api.py"), encoding="utf-8").read()
IDENTITY = open(os.path.join(ROOT, "desktron", "lib", "runtime_identity.py"), encoding="utf-8").read()
CONTRACT = open(os.path.join(ROOT, "desktron", "lib", "ui_contract.py"), encoding="utf-8").read()
INIT = open(os.path.join(ROOT, "frontend", "app-init.js"), encoding="utf-8").read()
STATIC_PATH = os.path.join(ROOT, "desktron", "lib", "ui_static.py")
STATIC = open(STATIC_PATH, encoding="utf-8").read()


class RuntimeHandshake(unittest.TestCase):
    def test_运行时接口同时给进程与前端版本(self):
        self.assertIn('"/workbench/runtime"', SYSTEM)
        self.assertIn('**runtime_identity.facts()', SYSTEM)
        self.assertIn('"frontend_rev": _frontend_token()', SYSTEM)
        self.assertIn('BOOT_ID = secrets.token_hex(12)', IDENTITY)
        self.assertIn('"launch_generation": LAUNCH_GENERATION', IDENTITY)

    def test_SSE首帧携带运行时标识(self):
        self.assertIn('{"type": "server.connected", "properties": {"runtime": runtime_status()}}', CONTRACT)

    def test_SSE保活帧对前端可见(self):
        self.assertIn('ping = {"runtime": runtime_status()}', CONTRACT)
        self.assertIn('f"event: ping\\ndata:', CONTRACT)

    def test_所有页面都会建立事件流(self):
        self.assertIn('if (typeof hsEnsureSSE === "function") hsEnsureSSE();', INIT)

    def test_没有新增轮询(self):
        self.assertNotIn("setInterval", SYSTEM)
        self.assertNotIn("setInterval", CONTRACT)

    def test_任一较旧文件变化也推动前端签名(self):
        """不能再取最大 mtime：另一个文件较新时，恢复代码更新仍须改变运行时版本。"""
        tree = ast.parse(STATIC, STATIC_PATH)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_frontend_tree_signature")
        ns = {"os": os}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), STATIC_PATH, "exec"), ns)
        signature = ns["_frontend_tree_signature"]
        with tempfile.TemporaryDirectory() as root:
            older = os.path.join(root, "app-chat-core.js")
            newer = os.path.join(root, "app-rail.js")
            with open(older, "w", encoding="utf-8") as f: f.write("old")
            with open(newer, "w", encoding="utf-8") as f: f.write("new")
            os.utime(older, ns=(1_000_000_000, 1_000_000_000))
            os.utime(newer, ns=(9_000_000_000, 9_000_000_000))
            before = signature(root)
            with open(older, "w", encoding="utf-8") as f: f.write("fix")
            os.utime(older, ns=(2_000_000_000, 2_000_000_000))  # 仍早于 newer
            self.assertNotEqual(before, signature(root))

    def test_嵌套页面与应用也进入版本签名(self):
        self.assertIn("for base, dirs, names in os.walk(root)", STATIC)
        self.assertIn("st.st_mtime_ns", STATIC)
        self.assertIn("hashlib.blake2b", STATIC)


if __name__ == "__main__":
    unittest.main()
