"""长连接 sidecar 的状态判定(lib/larkbot/sidecar.py)。

钉住的是「进程在跑」与「真的连上了」必须分开:凭证过期、权限没开、网络被中间设备拦掉时,
进程照样活着(SDK 一直在重连),但一条消息也收不到。只看 pid 的话面板显示「在线」,
而人在群里 @ 它没有任何反应,无从下手。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from larkbot import sidecar  # noqa: E402

APP = "cli_x"


class ConnState(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(sidecar, "home", return_value=self.d)
        self.p.start()
        self.addCleanup(self.p.stop)

    def _write(self, status, at):
        with open(os.path.join(self.d, f"ws-{APP}.state.json"), "w", encoding="utf-8") as f:
            json.dump({"status": status, "at": at, "app_id": APP}, f)

    def test_没有状态文件时不假装连上(self):
        s = sidecar.conn_state(APP)
        self.assertEqual(s["status"], "")
        self.assertFalse(s["fresh"])

    def test_刚写的状态算新鲜(self):
        self._write("connected", int(time.time()))
        s = sidecar.conn_state(APP)
        self.assertEqual(s["status"], "connected")
        self.assertTrue(s["fresh"])

    def test_旧_sidecar_写的_object_Object_归一成_unknown(self):
        """SDK 的 getConnectionStatus 返回 {state,...} 对象,2026-08-27 之前的
        lark-ws.mjs 拿它去 String(),状态文件里落的是字符串 "[object Object]"——
        面板据此把在线的 bot 显示成「重连中」加一栏乱码。源头已修;这里防的是
        还没重启的旧 sidecar 进程,它会接着写一阵子。"""
        self._write("[object Object]", int(time.time()))
        s = sidecar.conn_state(APP)
        self.assertEqual(s["status"], "unknown")

    def test_源头取的是_state_字段不再_String_整个对象(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "bin", "lark-ws.mjs"),
                  encoding="utf-8") as f:
            js = f.read()
        self.assertIn("typeof raw === 'object' ? raw.state : raw", js)
        self.assertNotIn("String(wsClient.getConnectionStatus?.() ?? 'unknown')", js)

    def test_太旧的状态不算数(self):
        """进程可能卡死了——文件还在,但已经不再更新。"""
        self._write("connected", int(time.time()) - 300)
        self.assertFalse(sidecar.conn_state(APP)["fresh"])

    def test_文件坏了不抛异常(self):
        with open(os.path.join(self.d, f"ws-{APP}.state.json"), "w", encoding="utf-8") as f:
            f.write("{不是 json")
        self.assertEqual(sidecar.conn_state(APP)["status"], "")

    def test_停止时把状态文件一并清掉(self):
        """不清的话,停了之后面板还会读到上一次的 connected。"""
        self._write("connected", int(time.time()))
        with open(os.path.join(self.d, f"ws-{APP}.pid"), "w", encoding="utf-8") as f:
            f.write("999999")     # 一个几乎不可能存在的 pid
        sidecar.stop(APP)
        self.assertFalse(os.path.exists(os.path.join(self.d, f"ws-{APP}.state.json")))

    def test_进程没在跑时状态里不带连接态(self):
        self._write("connected", int(time.time()))
        with mock.patch.object(sidecar, "running", return_value=0):
            st = sidecar.status([{"id": "a", "lark_app_id": APP, "enabled": True}])
        self.assertEqual(st[0]["conn"], "")
        self.assertFalse(st[0]["conn_fresh"])

    def test_进程在跑时带出连接态(self):
        self._write("connected", int(time.time()))
        with mock.patch.object(sidecar, "running", return_value=123), \
             mock.patch.object(sidecar, "_has_secret", return_value=True):
            st = sidecar.status([{"id": "a", "lark_app_id": APP, "enabled": True}])
        self.assertEqual(st[0]["conn"], "connected")
        self.assertTrue(st[0]["conn_fresh"])


class Failed(unittest.TestCase):
    """被飞书拒掉是单独一档:进程起得来,几秒后被拒然后自己退,退出码还是 0。
    只显示「未启动」会让人再点一次启动,而真正要改的是凭证或事件订阅方式。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(sidecar, "home", return_value=self.d)
        self.p.start()
        self.addCleanup(self.p.stop)

    def _write(self, status, at, error=""):
        with open(os.path.join(self.d, f"ws-{APP}.state.json"), "w", encoding="utf-8") as f:
            json.dump({"status": status, "at": at, "error": error, "app_id": APP}, f)

    def test_failed_是终态_不按新鲜度判(self):
        """进程已经不在了,时间戳只会越来越旧,而这个状态恰恰最需要被看见。"""
        self._write("failed", int(time.time()) - 3600, "invalid appId")
        s = sidecar.conn_state(APP)
        self.assertEqual(s["status"], "failed")
        self.assertTrue(s["fresh"])
        self.assertIn("invalid appId", s["error"])

    def test_进程不在但失败原因仍带得出来(self):
        self._write("failed", int(time.time()), "[error] invalid appId: cli_x")
        with mock.patch.object(sidecar, "running", return_value=0), \
             mock.patch.object(sidecar, "_has_secret", return_value=True):
            st = sidecar.status([{"id": "a", "lark_app_id": APP, "enabled": True}])
        self.assertEqual(st[0]["conn"], "failed")
        self.assertIn("invalid appId", st[0]["conn_error"])

    def test_进程不在且状态是连上时按未启动算(self):
        """残留的 connected 是上一次的,进程都没了,不该显示在线。"""
        self._write("connected", int(time.time()))
        with mock.patch.object(sidecar, "running", return_value=0), \
             mock.patch.object(sidecar, "_has_secret", return_value=True):
            st = sidecar.status([{"id": "a", "lark_app_id": APP, "enabled": True}])
        self.assertEqual(st[0]["conn"], "")


class Deps(unittest.TestCase):
    """两个 npm 依赖分开报:SDK 缺了收发不了消息,qrcode-terminal 缺了只是「新建」
    退回六步引导。混成一句会让人以为长连接坏了。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(sidecar, "home", return_value=self.d)
        self.p.start()
        self.addCleanup(self.p.stop)

    def _put(self, *names):
        for n in names:
            os.makedirs(os.path.join(self.d, "node_modules", *n.split("/")), exist_ok=True)

    def test_缺_sdk_时说的是_sdk(self):
        self._put("qrcode-terminal")
        with mock.patch("shutil.which", return_value="/usr/bin/node"):
            ok, why = sidecar.deps_ok()
        self.assertFalse(ok)
        self.assertIn("SDK", why)

    def test_缺二维码包时说的是它且提到新建(self):
        self._put("@larksuiteoapi/node-sdk")
        with mock.patch("shutil.which", return_value="/usr/bin/node"):
            ok, why = sidecar.deps_ok()
        self.assertFalse(ok)
        self.assertIn("qrcode-terminal", why)
        self.assertIn("新建", why)

    def test_两个都在才算就绪(self):
        self._put("@larksuiteoapi/node-sdk", "qrcode-terminal")
        with mock.patch("shutil.which", return_value="/usr/bin/node"), \
             mock.patch.object(sidecar, "_BIN", __file__):
            ok, _ = sidecar.deps_ok()
        self.assertTrue(ok)

    def test_老_package_json_会被补上后加的依赖(self):
        """先装过 SDK 那批机器的 package.json 里没有 qrcode-terminal(2026-08-25 才加),
        不该要求人手动改文件。"""
        import json
        with open(os.path.join(self.d, "package.json"), "w") as f:
            json.dump({"dependencies": {"@larksuiteoapi/node-sdk": "^1.64.0"}}, f)
        with mock.patch("shutil.which", return_value="/usr/bin/node"), \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
            sidecar.ensure_deps()
        with open(os.path.join(self.d, "package.json")) as f:
            self.assertIn("qrcode-terminal", json.load(f)["dependencies"])

    def test_残缺_package_json_会补齐两项依赖(self):
        import json
        with open(os.path.join(self.d, "package.json"), "w") as f:
            json.dump({"dependencies": {"qrcode-terminal": "^0.12.0"}}, f)
        with mock.patch("shutil.which", return_value="/usr/bin/node"), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout="", stderr="")):
            sidecar.ensure_deps()
        with open(os.path.join(self.d, "package.json")) as f:
            deps = json.load(f)["dependencies"]
        self.assertIn("@larksuiteoapi/node-sdk", deps)
        self.assertIn("qrcode-terminal", deps)


class PidGuard(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(sidecar, "home", return_value=self.d)
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_pid_文件在但进程没了就顺手清掉(self):
        """不清的话状态会一直显示「在跑」。"""
        with open(os.path.join(self.d, f"ws-{APP}.pid"), "w", encoding="utf-8") as f:
            f.write("999999")
        self.assertEqual(sidecar.running(APP), 0)
        self.assertFalse(os.path.exists(os.path.join(self.d, f"ws-{APP}.pid")))

    def test_pid_文件是垃圾时不抛(self):
        with open(os.path.join(self.d, f"ws-{APP}.pid"), "w", encoding="utf-8") as f:
            f.write("不是数字")
        self.assertEqual(sidecar.running(APP), 0)

    def test_没配凭证时不启动且说清楚(self):
        with mock.patch.object(sidecar, "deps_ok", return_value=(True, "")), \
             mock.patch("larkbot.larkapi.app_secret", return_value=""):
            pid, err = sidecar.start(APP)
        self.assertEqual(pid, 0)
        self.assertIn("app_secret", err)

    def test_依赖缺失时报的是缺什么不是启动失败(self):
        with mock.patch.object(sidecar, "deps_ok", return_value=(False, "node 不在 PATH 上")):
            pid, err = sidecar.start(APP)
        self.assertEqual(pid, 0)
        self.assertIn("node", err)

    def test_后台启动会尝试补齐新机器缺失的依赖(self):
        with mock.patch.object(sidecar, "deps_ok", return_value=(False, "SDK 未安装")), \
             mock.patch.object(sidecar, "ensure_deps", return_value=(False, "npm 超时")) as ensure:
            pid, err = sidecar.start(APP, install_missing=True)
        self.assertEqual(pid, 0)
        self.assertEqual(err, "npm 超时")
        ensure.assert_called_once_with()

    def test_用户手动启动不暗中安装依赖(self):
        with mock.patch.object(sidecar, "deps_ok", return_value=(False, "SDK 未安装")), \
             mock.patch.object(sidecar, "ensure_deps") as ensure:
            pid, err = sidecar.start(APP)
        self.assertEqual(pid, 0)
        self.assertIn("SDK", err)
        ensure.assert_not_called()

    def test_重新启动前清掉上个进程的失败状态(self):
        state = os.path.join(self.d, f"ws-{APP}.state.json")
        with open(state, "w") as f:
            json.dump({"status": "failed", "error": "ECONNRESET", "at": 1}, f)
        proc = mock.Mock(pid=321)
        with mock.patch.object(sidecar, "deps_ok", return_value=(True, "")), \
             mock.patch("larkbot.larkapi.app_secret", return_value="secret"), \
             mock.patch.object(sidecar.subprocess, "Popen", return_value=proc), \
             mock.patch.object(sidecar, "_alive", return_value=True), \
             mock.patch.object(sidecar.time, "sleep"):
            pid, err = sidecar.start(APP)
        self.assertEqual((pid, err), (321, ""))
        self.assertFalse(os.path.exists(state), "旧 failed 不能误判新进程")


if __name__ == "__main__":
    unittest.main()
