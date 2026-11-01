"""引擎有没有新版本(2026-08-21)。

改之前 Desk 对这件事完全无感:`available()` 只是 `shutil.which(bin)`,答的是「装没装」;
manifest 里 kind=engine 的几条只有 install、没有 min,连比对的对象都没有;`desktron deps`
读得出本机版本却从不问上游,而且只有命令行入口。人知道引擎更新了,靠的是在终端里跑它时
它自己提示。

补上的是「问上游」这一步:结果挂到输入框上方那条系统信息,类别词写「更新」。
形状照 system/api.py 的内网登录态——请求只读缓存立刻回,`npm view`(本机实测 0.7~3.7 秒)
放后台线程。
"""
import os
import sys
import threading
import time
import unittest

import _srcmap
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_updates as eu  # noqa: E402
finally:
    sys.argv = _argv

CHAT = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()


class Compare(unittest.TestCase):
    def test_按段比不按字符串比(self):
        """'0.9.0' > '0.10.0' 是字符串比的结果,那会把旧版当成新版。"""
        self.assertTrue(eu._newer("0.10.0", "0.9.0"))
        self.assertFalse(eu._newer("0.9.0", "0.10.0"))

    def test_段数不同补零(self):
        self.assertTrue(eu._newer("1.2", "1.1.9"))
        self.assertFalse(eu._newer("1.2", "1.2.0"))

    def test_prerelease_probe_does_not_break_version_comparison(self):
        self.assertTrue(eu._newer('0.155.0', '0.154.0-beta.1'))
        self.assertFalse(eu._newer('0.155.0', '0.156.0-beta.1'))

    def test_相同不算新(self):
        self.assertFalse(eu._newer("1.18.19", "1.18.19"))

    def test_缺一边就不判(self):
        """查不到上游版本时不能说「有新版本」,也不能说「已是最新」。"""
        self.assertFalse(eu._newer("", "1.0"))
        self.assertFalse(eu._newer("1.0", ""))

    def test_从各家的_version_输出里取得出版本(self):
        """五家的输出格式各不相同,都是本机实跑抄下来的。"""
        for out, want in [("2.1.235 (Claude Code)", "2.1.235"),
                          ("codex-cli 0.147.0", "0.147.0"),
                          ("tool 0.201.4(internal edition)", "0.201.4"),
                          ("1.18.19", "1.18.19"),
                          ("0.84.2", "0.84.2")]:
            self.assertEqual(eu._ver(out), want, out)


class Status(unittest.TestCase):
    def setUp(self):
        # 上一条用例起的后台线程可能还在跑,它跑完会往 _cache 里写。只清不等的话,
        # 下一条用例读到的是它写进去的那一份,于是「该起一轮探测」变成「命中缓存不起」
        # ——单跑绿、连着跑红(2026-08-22 实见)。
        for _ in range(200):
            if not eu._busy:
                break
            time.sleep(0.02)
        eu._cache.clear()
        eu._busy.clear()

    tearDown = setUp

    def test_第一次立刻回_不等网络(self):
        slow = threading.Event()
        with mock.patch.object(eu, "_latest", lambda *a, **k: (slow.wait(30), "9.9.9")[1]):
            t = time.perf_counter()
            got = eu.status("codex")
            d = time.perf_counter() - t
        self.assertLess(d, 0.2, "请求线程等了 npm view")
        self.assertEqual(got, {}, "还没查出来就该回空,不该替它下结论")
        slow.set()

    def test_查完之后读得到(self):
        with mock.patch.object(eu, "_latest", return_value='0.999.0') as latest, \
                mock.patch("check_deps.probe", lambda n: (True, "0.147.0")):
            eu.status("codex")
            for _ in range(100):
                if eu._cache.get("codex"):
                    break
                time.sleep(0.02)
            got = eu.status("codex", eu.install_cmd(ROOT, 'codex'))
        latest.assert_called_once_with('@openai/codex', '')
        self.assertTrue(got["hasUpdate"])
        self.assertEqual((got["have"], got["latest"]), ("0.147.0", '0.999.0'))
        self.assertTrue(got["how"].endswith('@openai/codex'))

    def test_codex_update_does_not_pin_an_executable_version(self):
        with mock.patch.object(eu.subprocess, 'run', return_value=mock.Mock(
                stdout=b'', stderr=b'', returncode=0)) as run:
            result = eu.run_update(ROOT, 'codex')
        self.assertTrue(result['ok'])
        self.assertTrue(run.call_args.args[0][-1].endswith('@openai/codex'))

    @unittest.skipIf(os.name == 'nt', 'POSIX npm symlink and bin-prefix layout')
    def test_npm更新用引擎所属前缀且不经登录shell(self):
        """bash -l 会落到 x86_64 node（Bad CPU type）；更新必须落到正在使用的那份安装。"""
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            prefix = os.path.join(os.path.realpath(root), 'nvm', 'v24')
            pkg = os.path.join(prefix, 'lib', 'node_modules', '@earendil-works', 'pi-coding-agent')
            os.makedirs(os.path.join(pkg, 'dist'))
            os.makedirs(os.path.join(prefix, 'bin'))
            entry = os.path.join(pkg, 'dist', 'cli.js')
            open(entry, 'w').close()
            os.symlink(entry, os.path.join(prefix, 'bin', 'pi'))
            npm = os.path.join(prefix, 'bin', 'npm')
            open(npm, 'w').close()
            os.chmod(npm, 0o755)
            with mock.patch.object(eu.shutil, 'which', return_value=os.path.join(prefix, 'bin', 'pi')), \
                    mock.patch.object(eu.subprocess, 'run', return_value=mock.Mock(
                        stdout=b'', stderr=b'', returncode=0)) as run:
                result = eu.run_update(ROOT, 'pi')
            argv = run.call_args.args[0]
            self.assertTrue(result['ok'])
            self.assertEqual(argv[0], npm)
            self.assertEqual(argv[argv.index('--prefix') + 1], prefix)
            self.assertNotIn('bash', argv)
            self.assertTrue(run.call_args.kwargs['env']['PATH'].startswith(os.path.join(prefix, 'bin') + os.pathsep))

    def test_pi查询和安装都显式走公共源(self):
        registry = 'https://registry.npmjs.org/'
        self.assertEqual(eu.NPM['pi'][1], registry)
        with mock.patch.object(eu, "_latest", return_value='0.86.0') as latest, \
                mock.patch("check_deps.probe", lambda n: (True, "0.85.1")):
            eu.status("pi")
            for _ in range(100):
                if eu._cache.get("pi"):
                    break
                time.sleep(0.02)
        latest.assert_called_once_with('@earendil-works/pi-coding-agent', registry)
        self.assertIn('--registry=' + registry, eu.install_cmd(ROOT, 'pi'))

    def test_已是最新不带更新命令(self):
        with mock.patch.object(eu, "_latest", lambda *a, **k: "1.18.19"), \
                mock.patch("check_deps.probe", lambda n: (True, "1.18.19")):
            eu.status("claude")
            for _ in range(100):
                if eu._cache.get("claude"):
                    break
                time.sleep(0.02)
            got = eu.status("claude", "claude update")
        self.assertFalse(got["hasUpdate"])
        self.assertNotIn("how", got)

    def test_同时只查一轮(self):
        """八次并发打开设置页不该起八个探测——那正是把探测命令打爆的那一路。

        check_deps.probe 一并换掉:它真去跑 `codex --version`,冷启动那一次要好几秒,
        上一条用例的后台线程因此拖到本用例开始之后才写缓存,于是这里读到「不 stale」、
        一轮探测都不起(冷跑红、连着跑绿,2026-08-22 实见)。
        """
        n = []
        gate = threading.Event()
        def latest(*a, **k):
            n.append(1); gate.wait(10); return "9.9.9"
        eu._cache.clear(); eu._busy.clear()
        with mock.patch("check_deps.probe", lambda e: (True, "0.1.0")), \
                mock.patch.object(eu, "_latest", latest):
            ths = [threading.Thread(target=lambda: eu.status("codex")) for _ in range(8)]
            for t in ths: t.start()
            for t in ths: t.join(5)
            time.sleep(0.1)
            self.assertEqual(sum(n), 1, "起了 %d 轮 npm view" % sum(n))
            gate.set()

    def test_查炸了不把_busy_卡住(self):
        def boom(*a, **k):
            raise RuntimeError("网络断了")
        with mock.patch.object(eu, "_latest", boom), \
                mock.patch("check_deps.probe", lambda n: (True, "1.0.0")):
            eu.status("codex")
            for _ in range(100):
                if "codex" not in eu._busy:
                    break
                time.sleep(0.02)
        self.assertNotIn("codex", eu._busy)

    def test_每个能查的引擎都有安装命令(self):
        """更新命令进详情供人抄走;缺了那一条,提示就只剩「有新版本」而没有下一步。"""
        for e in eu.NPM:
            self.assertTrue(eu.install_cmd(ROOT, e), e + " 在 manifest 里没有 install")


class Notice(unittest.TestCase):
    def test_类别词多一档更新(self):
        self.assertIn('update: "更新"', CHAT)
        self.assertIn('"update"].includes(p.state)', CHAT)

    def test_引擎状态优先_不被更新顶掉(self):
        """报错、未登录比「有个新版本」要紧得多。"""
        self.assertIn("if (!cf.environment) hsUpdateNotice(v, cf.update);", CHAT)

    def test_没有新版本就不出条(self):
        self.assertIn("if (!up || !up.hasUpdate || !up.latest) return;", CHAT)

    def test_关掉之后本次会话不再提(self):
        self.assertIn("_hsUpdateMuted.add(v.updateKey)", CHAT)
        self.assertIn("if (_hsUpdateMuted.has(key)) return;", CHAT)

    def test_更新命令进详情(self):
        i = CHAT.index("function hsUpdateNotice")
        self.assertIn("details: up.how", CHAT[i:i + 800])

    def test_更新那条可以关掉(self):
        self.assertIn(".hs-page .hs-env.update .hs-env-x { display: inline-flex; }", CSS)

    def test_更新用另一个色相(self):
        """它既不是警告也不是错误,与琥珀、红分开,扫一眼知道不用停下手里的事。"""
        self.assertIn(".hs-page .hs-env.update .hs-env-k", CSS)


class Config(unittest.TestCase):
    def test_config_带出来(self):
        src = _srcmap.ui_server_src()
        self.assertIn('"update": _engine_update(engine_name),', src)
        self.assertIn("def _engine_update(name):", src)


if __name__ == "__main__":
    unittest.main()
