"""缺依赖、缺工作工具时,引导页要给得出可执行的下一步(2026-09-02 复盘)。

原来这两处都是死路:
  · runtime 缺失(新 Mac 上 node 最常缺)——面板只有「重新检查」,装法要人自己去别处找;
  · lark-cli 未安装——过去只给跳转设置页,设置页却没有安装入口。
两者都不是提示级问题:缺 node 则引擎装不上,缺工作工具则一整类能力不存在。
"""
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
# deskcfg 的 REPO_ROOT 由它 import 时的 argv 定,而全量 discover 时谁先 import 不由
# 本文件决定(单独跑绿、全量跑红就是这么来的)。所以不靠 argv,取用时直接改写它——
# _runtime() 里是函数内 from deskcfg import REPO_ROOT,每次调用重新取,改得动。
sys.argv = ["test", os.path.join(ROOT, "frontend"), "0", "0", ROOT]
JS = open(os.path.join(ROOT, "frontend", "app-settings-setup.js"), encoding="utf-8").read()


def _fn(name):
    """取某个函数的源码段。"""
    i = JS.index("function %s(" % name)
    j = JS.index("\n}", i)
    return JS[i:j]


class 缺依赖给得出装法(unittest.TestCase):
    def test_manifest_三条_runtime_都有可执行命令(self):
        import check_deps
        rows = [r for r in check_deps.parse_reqs(ROOT) if r.get("kind") == "runtime"]
        self.assertEqual(len(rows), 3, "runtime 条目数变了,这条要跟着核对")
        for r in rows:
            self.assertTrue(r.get("install"), r["name"] + " 没有可执行的 install,引导页按不了")

    def test_命令不带_yaml_引号(self):
        """简易解析器原来只脱双引号,值用单引号包裹时那对引号会被读进命令里,
        预填出去跑不通。两个解析器(简易正则 / 真 YAML)必须给出同一个字符串。"""
        import check_deps
        import yaml
        a = {r["name"]: r.get("install", "") for r in check_deps.parse_reqs(ROOT)}
        b = {r["name"]: r.get("install_windows", r.get("install", ""))
             if sys.platform == 'win32' else r.get("install", "")
             for r in yaml.safe_load(open(os.path.join(ROOT, "manifest.yaml"), encoding='utf-8'))["requirements"]}
        for n in ("python3", "node", "git"):
            self.assertEqual(a[n], b[n], n + " 两个解析器读出来不一样")
            self.assertFalse(a[n].startswith(("'", '"')), n + " 命令带着 YAML 引号")

    def test_命令语法可执行(self):
        """跑一遍 bash -n:预填的是要人回车执行的东西,语法错就是白给一颗钮。"""
        import check_deps
        for r in check_deps.parse_reqs(ROOT):
            if r.get("kind") != "runtime":
                continue
            p = subprocess.run(["bash", "-n", "-c", r["install"]], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(p.returncode, 0, r["name"] + " 命令语法错:" + p.stderr[:80])

    def test_没装_brew_时命令有指向(self):
        """新 Mac 上没有 Homebrew 是常态。裸跑 brew install 只会得到 command not found,
        对不熟悉的人是死路——必须指到 brew.sh。"""
        import check_deps
        for r in check_deps.parse_reqs(ROOT):
            if r.get("kind") == "runtime" and "brew install" in r.get("install", ""):
                self.assertIn("command -v brew", r["install"], r["name"] + " 没先探 brew")
                self.assertIn("brew.sh", r["install"], r["name"] + " 没给出装 brew 的去处")

    def test_后端把命令传给前端(self):
        import deskcfg
        import provision.setup as setup
        keep = deskcfg.REPO_ROOT
        deskcfg.REPO_ROOT = ROOT
        try:
            rows = setup._runtime()["rows"]
        finally:
            deskcfg.REPO_ROOT = keep
        self.assertTrue(rows)
        for r in rows:
            self.assertIn("install", r)
            self.assertIn("hint", r)   # 命令失败时的备选,要显示出来

    def test_面板对缺失项给安装钮(self):
        seg = _fn("_setupRuntimePanel")
        self.assertIn("bad", seg)
        self.assertIn('"command"', seg)
        self.assertIn("r.install", seg)
        # 只给缺的那几条,不给已经装好的
        self.assertRegex(seg, r"bad\s*=\s*all\.filter\(\(r\)\s*=>\s*!r\.ok\)")

    def test_缺失项把_hint_显示出来(self):
        """预填的命令只有一条,hint 里写着备选(node 那条的 nvm)。
        命令跑不通时人得有下一步可看。"""
        self.assertIn("r.hint", _fn("_setupRuntimePanel"))


class 引擎安装修复协议漂移(unittest.TestCase):
    def test_pi_安装显式追最新且已有命令仍过协议探针(self):
        import check_deps
        pi = next(r for r in check_deps.parse_reqs(ROOT) if r.get("name") == "pi")
        self.assertIn("@earendil-works/pi-coding-agent@latest", pi["install"])
        source = open(os.path.join(ROOT, "desktron/bin/desktron"), encoding="utf-8").read()
        self.assertIn('--engine-ready "$name"', source)
        self.assertIn('if $_installed && ! $upgrade', source)

class 工作工具给得出装法(unittest.TestCase):
    def test_未安装时给安装指引(self):
        seg = _fn("_setupToolsPanel")
        self.assertIn("github.com/larksuite/cli", seg)
        self.assertRegex(seg, r"rows\.some\(\(r\)\s*=>\s*!r\.installed\)")

    def test_工具配置不再跳出引导页(self):
        seg = _fn("_setupToolsPanel")
        self.assertNotIn('"settings"', seg)


if __name__ == "__main__":
    unittest.main(argv=["test_setup_install_actions"])
