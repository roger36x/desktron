"""用户删掉的应用不该自己回来。

开机兜底(pkgstore.bootstrap_default_workspace_apps)按发行版声明补装,reconcile 按安装记录
重新注册——两者都会把用户刚删的应用装回去(2026-08-05 实测:删完三个,重启后原样出现)。
删除记一笔墓碑,这两条路遇到就跳过;用户显式重装时清墓碑,因为那是更近的意图。
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
# workbench 推迟到 setUp 再导入,模块级不碰。
#
# deskcfg 是模块级单例:REPO_ROOT 在它被导入那一刻由 sys.argv 定死,之后谁再设 argv 都没用。
# 而 pytest 的收集阶段会先把所有测试模块 import 一遍——本文件按字母序排在前面,模块级导入
# workbench(它链到 deskcfg)就等于替整个进程把 REPO_ROOT 定成了这里给的值,
# test_pkgstore 随后在自己模块级设的那份临时 REPO_ROOT 就再也生效不了。
# 表现是它单跑全过、同跑全崩(2026-08-11 实见,9 条)。
#
# 本文件自己要的隔离由 setUp 里 patch WB_APPS_REMOVED 完成,并不在乎 REPO_ROOT 是谁的,
# 所以让出这个位置:同跑时用别人已经定好的,单跑时才自己给一个临时的。
workbench = None


def _load_workbench():
    global workbench
    if workbench is not None:
        return workbench
    if "deskcfg" not in sys.modules:
        sys.argv = ["test", "/tmp/tomb-frontend", "0", "0", tempfile.mkdtemp(prefix="tomb-repo-")]
    import workbench as _wb
    workbench = _wb
    return workbench


class Tombstone(unittest.TestCase):
    def setUp(self):
        _load_workbench()
        self.tmp = tempfile.TemporaryDirectory()
        self._p = workbench.WB_APPS_REMOVED
        workbench.WB_APPS_REMOVED = os.path.join(self.tmp.name, "apps-removed.json")

    def tearDown(self):
        workbench.WB_APPS_REMOVED = self._p
        self.tmp.cleanup()

    def test_没记过的不算删过(self):
        self.assertFalse(workbench.wb_app_removed(app_id="notepad"))

    def test_记了就算删过(self):
        workbench.wb_app_tombstone(app_id="meeting-digest", uid="desktron.meeting-digest.9b492a")
        self.assertTrue(workbench.wb_app_removed(app_id="meeting-digest"))
        self.assertTrue(workbench.wb_app_removed(uid="desktron.meeting-digest.9b492a"))
        self.assertFalse(workbench.wb_app_removed(app_id="notepad"))

    def test_id_与_uid_各自成立(self):
        # 发行版换作者命名空间时 uid 会变,id 仍要拦得住
        workbench.wb_app_tombstone(app_id="alert-governance", uid="alice.alert-governance.151372")
        self.assertTrue(workbench.wb_app_removed(app_id="alert-governance", uid="别人.alert-governance.aaaa"))

    def test_重装清墓碑(self):
        workbench.wb_app_tombstone(app_id="meeting-digest", uid="u1")
        workbench.wb_app_tombstone_clear(app_id="meeting-digest", uid="u1")
        self.assertFalse(workbench.wb_app_removed(app_id="meeting-digest"))

    def test_落盘可读且去重(self):
        for _ in range(3):
            workbench.wb_app_tombstone(app_id="x", uid="u")
        with open(workbench.WB_APPS_REMOVED, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["ids"], ["x"])
        self.assertEqual(d["uids"], ["u"])


class Wiring(unittest.TestCase):
    def _src(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_注册前查墓碑(self):
        s = self._src("desktron/lib/pkgstore.py")
        body = s[s.index("def _register_app("):s.index("def _register_kb(")]
        self.assertIn("wb_app_removed(app_id=aid)", body)

    def test_显式安装清墓碑(self):
        s = self._src("desktron/lib/pkgstore.py")
        body = s[s.index("def install("):s.index("def installed(")]
        self.assertIn("wb_app_tombstone_clear", body)

    def test_真删时记墓碑(self):
        s = self._src("desktron/lib/app/api.py")
        self.assertIn("wb_app_tombstone(app_id=", s)


if __name__ == "__main__":
    unittest.main()
