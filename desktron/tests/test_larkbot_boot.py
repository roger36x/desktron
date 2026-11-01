"""开机拉长连接(lib/larkbot/api.py:boot)。

钉住的是「在哪一侧配就在哪一侧跑」:本机与 服务器 的 workspace 各自独立——sync-home
同步的是 home/(代码)而不是运行态,agent 声明落在 workspace/.workbench/agents/ 下,
凭证在各自的 ~/.config/desktron/env。声明本来就不跨侧,所以 boot 不做任何按侧过滤。

这里原本测的是一个 run_on 字段(local/remote/any)。那个设计假设两侧都能看到同一份
声明,而实际不能:标了 run_on=remote 的 agent 在本机按标记不启动、在 服务器 上又根本
不存在,结果谁也不跑,界面上还看不出为什么。字段已去掉,换成记录「这份声明建在哪台
机器上」——那是事实,不是要人手填的意图。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

# deskcfg 在导入那一刻就从 sys.argv 取 FRONTEND/UI_PORT/REPO_ROOT(模块级单例)。
# 直接 import larkbot.api 会拿 unittest 自己的 argv 去 int(),报 "invalid literal
# for int()"——与真实原因毫无字面关联。同 test_bg_tasks.py:21 的处理。
_argv = sys.argv
sys.argv = ["ui_server.py", "/tmp/lb-frontend", "0", "0", tempfile.mkdtemp(prefix="lb-boot-")]
try:
    from larkbot import api as lb_api, store  # noqa: E402
finally:
    sys.argv = _argv

# 日志一律 patch 到 lb_api.deskcfg 这个对象上,不用 patch("deskcfg.log")。
# 后者打的是 sys.modules 里当时那个模块对象;别的测试若重载过 deskcfg,
# api 持有的就是旧对象,patch 落空、断言拿到空日志——只在全量跑时暴露。


class Boot(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        for aid in ("a-one", "a-two"):
            _, err = store.save(self.d, aid, {"lark_app_id": "cli_" + aid.replace("-", "")})
            self.assertEqual(err, "")

    def _boot(self, desk_id="desk_me", secret="s"):
        started, logs = [], []
        with mock.patch.object(lb_api, "REPO_ROOT", self.d), \
             mock.patch("larkbot.larkapi.app_secret", return_value=secret), \
             mock.patch("remote_auth.desk_id", return_value=desk_id), \
             mock.patch("larkbot.sidecar.start",
                        side_effect=lambda app, *a, **k: (started.append(app), (1, ""))[1]), \
             mock.patch.object(lb_api.deskcfg, "log",
                               side_effect=lambda mod, msg: logs.append(str(msg))):
            lb_api.boot()
        return started, " ".join(logs)

    def test_这一侧的_agent_全都拉起来(self):
        """不按侧过滤:声明在这台机器上,就是这台机器该跑的。"""
        started, _ = self._boot()
        self.assertEqual(sorted(started), ["cli_aone", "cli_atwo"])

    def test_没配凭证的不起(self):
        started, _ = self._boot(secret="")
        self.assertEqual(started, [])

    def test_停用的不起(self):
        a = store.load(self.d, "a-one")
        a["enabled"] = False
        store.save(self.d, "a-one", a)
        started, _ = self._boot()
        self.assertEqual(started, ["cli_atwo"])

    def test_每个都在日志里留下结果(self):
        _, logs = self._boot()
        for aid in ("a-one", "a-two"):
            self.assertIn(aid, logs)


class BornElsewhere(unittest.TestCase):
    """从别处拷过来的声明:提示但照跑。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def _save_as(self, aid, desk_id):
        with mock.patch("remote_auth.desk_id", return_value=desk_id):
            _, err = store.save(self.d, aid, {"lark_app_id": "cli_x"})
            self.assertEqual(err, "")

    def _boot(self, me):
        started, logs = [], []
        with mock.patch.object(lb_api, "REPO_ROOT", self.d), \
             mock.patch("larkbot.larkapi.app_secret", return_value="s"), \
             mock.patch("remote_auth.desk_id", return_value=me), \
             mock.patch("larkbot.sidecar.start",
                        side_effect=lambda app, *a, **k: (started.append(app), (1, ""))[1]), \
             mock.patch.object(lb_api.deskcfg, "log",
                               side_effect=lambda mod, msg: logs.append(str(msg))):
            lb_api.boot()
        return started, " ".join(logs)

    def test_建声明时记下这台机器(self):
        self._save_as("aa", "desk_here")
        self.assertEqual(store.load(self.d, "aa")["created_on"], "desk_here")

    def test_本机建的不提示(self):
        self._save_as("aa", "desk_here")
        _, logs = self._boot("desk_here")
        self.assertNotIn("另一台机器", logs)

    def test_别处建的提示但照跑(self):
        """拷过来接着用是正当需求,拦了反而挡路;但那台若还开着,同一个 app 会有
        两条长连接、bot 答两遍——这个现象很难反推到「另一台也在跑」。"""
        self._save_as("aa", "desk_other")
        started, logs = self._boot("desk_here")
        self.assertEqual(started, ["cli_x"])       # 照跑
        self.assertIn("另一台机器", logs)           # 但留线索
        self.assertIn("两次", logs)

    def test_认不出本机时不误报(self):
        """desk_id 读不出来(SECRETS 不可写等)时返回空串,那时无从比较,不该乱提示。"""
        self._save_as("aa", "desk_other")
        _, logs = self._boot("")
        self.assertNotIn("另一台机器", logs)

    def test_老声明没有这个字段时不误报(self):
        """这个字段是后加的,存量声明里没有。

        直接写文件构造,不走 save——save 一定会补上这个字段,而这里要的正是「没有」。
        真实的存量声明也是旧代码写下的,同样绕过了今天的 save。"""
        import json
        os.makedirs(store.dir_of(self.d), exist_ok=True)
        with open(os.path.join(store.dir_of(self.d), "bb.json"), "w", encoding="utf-8") as f:
            json.dump({"lark_app_id": "cli_y", "name": "老的"}, f)
        self.assertEqual(store.load(self.d, "bb")["created_on"], "")
        _, logs = self._boot("desk_here")
        self.assertNotIn("另一台机器", logs)


class Watch(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        _, err = store.save(self.d, "bot-one", {"lark_app_id": "cli_botone"})
        self.assertEqual(err, "")
        self.old = (dict(lb_api._watch_bad_since), dict(lb_api._watch_retry_at),
                    dict(lb_api._watch_failures))
        lb_api._watch_bad_since.clear()
        lb_api._watch_retry_at.clear()
        lb_api._watch_failures.clear()

    def tearDown(self):
        lb_api._watch_bad_since.clear(); lb_api._watch_bad_since.update(self.old[0])
        lb_api._watch_retry_at.clear(); lb_api._watch_retry_at.update(self.old[1])
        lb_api._watch_failures.clear(); lb_api._watch_failures.update(self.old[2])

    def _once(self, running=0, conn=None, now=100):
        starts, stops = [], []
        conn = conn or {"status": "", "fresh": False, "error": ""}
        with mock.patch.object(lb_api, "REPO_ROOT", self.d), \
             mock.patch("larkbot.larkapi.app_secret", return_value="s"), \
             mock.patch("larkbot.sidecar.running", return_value=running), \
             mock.patch("larkbot.sidecar.conn_state", return_value=conn), \
             mock.patch("larkbot.sidecar.stop", side_effect=lambda app: stops.append(app)), \
             mock.patch("larkbot.sidecar.start",
                        side_effect=lambda app, *a, **k: (starts.append(app), (321, ""))[1]), \
             mock.patch.object(lb_api.deskcfg, "log"):
            lb_api._watch_once(now=now)
        return starts, stops

    def test_进程意外退出会自动拉起(self):
        starts, stops = self._once(running=0)
        self.assertEqual(starts, ["cli_botone"])
        self.assertEqual(stops, [])

    def test_看护启动允许补装新机器缺失的依赖(self):
        calls = []
        with mock.patch.object(lb_api, "REPO_ROOT", self.d), \
             mock.patch("larkbot.larkapi.app_secret", return_value="s"), \
             mock.patch("larkbot.sidecar.running", return_value=0), \
             mock.patch("larkbot.sidecar.conn_state",
                        return_value={"status": "", "fresh": False}), \
             mock.patch("larkbot.sidecar.start",
                        side_effect=lambda app, *a, **k: (calls.append(k), (321, ""))[1]), \
             mock.patch.object(lb_api.deskcfg, "log"):
            lb_api._watch_once(now=100)
        self.assertEqual(calls, [{"install_missing": True}])

    def test_短暂断线先交给SDK重连(self):
        starts, stops = self._once(
            running=123, conn={"status": "reconnecting", "fresh": True}, now=100)
        self.assertEqual((starts, stops), ([], []))
        starts, stops = self._once(
            running=123, conn={"status": "reconnecting", "fresh": True},
            now=100 + lb_api.WATCH_STUCK_S + 1)
        self.assertEqual(starts, ["cli_botone"])
        self.assertEqual(stops, ["cli_botone"])

    def test_连接恢复会清掉退避状态(self):
        lb_api._watch_bad_since["cli_botone"] = 1
        lb_api._watch_retry_at["cli_botone"] = 999
        lb_api._watch_failures["cli_botone"] = 4
        self._once(running=123, conn={"status": "connected", "fresh": True})
        self.assertNotIn("cli_botone", lb_api._watch_bad_since)
        self.assertNotIn("cli_botone", lb_api._watch_retry_at)
        self.assertNotIn("cli_botone", lb_api._watch_failures)

    def test_明确被飞书拒绝时不反复启动(self):
        starts, stops = self._once(
            running=0, conn={"status": "failed", "fresh": True,
                             "error": "invalid appId"})
        self.assertEqual((starts, stops), ([], []))

    def test_网络异常导致进程退出仍会重启(self):
        starts, stops = self._once(
            running=0, conn={"status": "failed", "fresh": True,
                             "error": "socket hang up ECONNRESET"})
        self.assertEqual(starts, ["cli_botone"])
        self.assertEqual(stops, [])

    def test_停用后不会被自动拉起(self):
        a = store.load(self.d, "bot-one")
        a["enabled"] = False
        store.save(self.d, "bot-one", a)
        starts, stops = self._once(running=0)
        self.assertEqual((starts, stops), ([], []))

    def test_手动启停会保存看护意愿(self):
        with mock.patch.object(lb_api, "REPO_ROOT", self.d):
            a = store.load(self.d, "bot-one")
            saved, err = lb_api._save_enabled(a, False)
            self.assertEqual(err, "")
            self.assertFalse(saved["enabled"])
            self.assertFalse(store.load(self.d, "bot-one")["enabled"])
            _saved, err = lb_api._save_enabled(store.load(self.d, "bot-one"), True)
            self.assertEqual(err, "")
            self.assertTrue(store.load(self.d, "bot-one")["enabled"])

    def test_看护启动幂等(self):
        old = lb_api._watch_started
        lb_api._watch_started = False
        started = []
        fake_thread = mock.Mock()
        fake_thread.start.side_effect = lambda: started.append(1)
        try:
            with mock.patch.object(lb_api.threading, "Thread", return_value=fake_thread):
                self.assertTrue(lb_api.start_background())
                self.assertFalse(lb_api.start_background())
            self.assertEqual(started, [1])
        finally:
            lb_api._watch_started = old


if __name__ == "__main__":
    unittest.main()
