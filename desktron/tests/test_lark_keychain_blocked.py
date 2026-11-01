"""钥匙串读不到 ≠ 没登录(2026-09-04 用户交付到别人机器上实见)。

lark-cli 的主密钥存在 macOS 钥匙串里。Desk 进程读不到钥匙串时 `auth status` 直接失败,
于是飞书通知一直是哑的,而界面把这一档显示成「未登录」——人反复去点登录,方向就是错的。
lark-cli 自己给了解法:在交互式终端里跑一次 `config keychain-downgrade`,把主密钥落成
本地文件,此后自动化环境读文件即可(沙箱里跑这条命令必然失败,是设计如此)。

钉三件:后端把这一档单列并带上那条命令;首次设置不把它算作就绪;通知失败不再无声。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
if "deskcfg" not in sys.modules:
    sys.argv = ["test", os.path.join(ROOT, "frontend"), "0", "0", tempfile.mkdtemp()]

CORE = open(os.path.join(ROOT, "frontend", "app-settings-core.js"), encoding="utf-8").read()
SETUP_JS = open(os.path.join(ROOT, "frontend", "app-settings-setup.js"), encoding="utf-8").read()
SECTIONS = open(os.path.join(ROOT, "frontend", "app-settings-sections.js"),
                encoding="utf-8").read()
RAIL = open(os.path.join(ROOT, "frontend", "app-rail-topnav.js"), encoding="utf-8").read()

FIX = "lark-cli config keychain-downgrade"


def lark_judge():
    """取出 _intranet_probe 里那个判据,单独喂它几种输出。"""
    import system.api as api
    src = open(api.__file__, encoding="utf-8").read()
    seg = src[src.index("    def lark_ok(out):"):src.index("    jobs = {")]
    ns = {"json": __import__("json")}
    exec("".join(l[4:] if l.startswith("    ") else l for l in seg.splitlines(True)), ns)
    return ns["lark_ok"]


class 后端分档(unittest.TestCase):
    def setUp(self):
        self.judge = lark_judge()

    def test_登着就是登着(self):
        got = self.judge('{"identities":{"user":{"status":"ready","openId":"ou_x"}}}')
        self.assertTrue(got is True or (isinstance(got, dict) and got.get("logged_in")))

    def test_钥匙串受阻单列一档并给出修复命令(self):
        for out in ('keychain not initialized',
                    'keychain Get error: keychain access blocked',
                    'Error: KEYCHAIN item not found'):
            got = self.judge(out)
            self.assertIsInstance(got, dict, out)
            self.assertTrue(got.get("keychain"), out)
            self.assertFalse(got.get("logged_in"), out)
            self.assertEqual(got.get("fix"), FIX, out)

    def test_真没登录不误报成钥匙串(self):
        got = self.judge('{"identities":{"user":{"status":"unauthorized"}}}')
        keychain = got.get("keychain") if isinstance(got, dict) else False
        self.assertFalse(keychain)

    def test_判据回字典时原样带出(self):
        """cli() 只认真假的话,keychain/fix 会在这一层被丢掉。"""
        import system.api as api
        src = open(api.__file__, encoding="utf-8").read()
        seg = src[src.index("    def cli(cmd, judge):"):src.index("    def lark_ok(out):")]
        self.assertIn("isinstance(got, dict)", seg)
        self.assertIn('return {"installed": True, **got}', seg)


class 首次设置(unittest.TestCase):
    def test_行里带上钥匙串与修复动作(self):
        from provision import setup
        src = open(setup.__file__, encoding="utf-8").read()
        seg = src[src.index("def _intranet()"):src.index("def _runtime()")]
        self.assertIn('"keychain": bool(v.get("keychain"))', seg)
        self.assertIn('"fix": str(v.get("fix") or "")', seg)

    def _status(self, lark_row):
        from unittest import mock
        from provision import setup
        intra = {"rows": [dict({"key": "lark", "label": "lark-cli", "hint": "", "installed": True,
                                "unknown": False, "why": ""}, **lark_row)],
                 "pending": False}
        with mock.patch.object(setup, "_runtime", lambda: {"ok": True, "rows": [], "missing": []}), \
             mock.patch.object(setup, "_engines", lambda: []), \
             mock.patch.object(setup, "_intranet", lambda: intra):
            return setup.status()["groups"]["intranet"]

    def test_钥匙串受阻不算就绪(self):
        """它正是「看着像登着、实际用不了」那一档;算作就绪的话交付出去就一直是哑的。"""
        blocked = self._status({"logged_in": False, "keychain": True, "fix": FIX})
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["detail"][0]["fix"], FIX)
        # 正常登着仍然就绪,别把这条闸修成谁都过不去
        self.assertTrue(self._status({"logged_in": True})["ok"])


class 界面动作(unittest.TestCase):
    def test_设置页换成修复而不是登录(self):
        i = CORE.index("if (r2.keychain) {")
        seg = CORE[i:i + 600]
        self.assertIn('"钥匙串受阻"', seg)
        self.assertIn('btn.textContent = DeskI18n.tx("修复")', seg)
        self.assertIn(FIX, seg)
        # 登录那条路修不好这件事,不能落到它上面去
        self.assertLess(CORE.index("if (r2.keychain) {"),
                        CORE.index('pill.innerHTML = (r2.logged_in ? _PV_CHECK'))

    def test_首次设置也给同一条命令(self):
        i = SETUP_JS.index("if (r.installed && r.keychain) {")
        seg = SETUP_JS[i:i + 400]
        self.assertIn(FIX, seg)
        self.assertIn("continue;", seg)   # 别再顺手补一枚「登录」
        self.assertIn('status = "钥匙串受阻"', SETUP_JS)


if __name__ == "__main__":
    unittest.main()
