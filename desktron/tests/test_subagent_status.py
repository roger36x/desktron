"""子代理的运行状态:后端按记录里的事实判,前端不再靠时间窗猜。

原来 /session/<sid>/agents 只给时间戳,前端于是按「记录 90 秒没动就当它不在跑」猜,
三处一起出问题(2026-08-12 用户实见):异步派出的代理活过了派它出来的那个回合,主线一停
入口就消失;刷新页面后按本回合起始时刻过滤,在跑的代理被滤掉;轮询的开关只由「名单里
有代理在跑」决定,而刚派出那一刻记录文件还没落地,名单是空的,于是再也没人去查。

准确判据取引擎自己写下的两样东西:父回合里那次 Agent 调用的返回(同步派出的那一路,
返回即交回;异步派出的那一路返回的只是一句「已在后台派出」,正文里写着 agentId),
以及跑完时注入的 task-notification(origin.kind=task-notification,带 task-id 与 status)。
样本照本机真实 rollout 的形状抄(claude-code 2.1.220)。
"""
import json
import os
import re
import shutil
import time
import subprocess
import sys
import tempfile
import unittest

import _srcmap
from frontend_i18n_js import NODE_PRELUDE

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FRONTEND = os.path.join(ROOT, "frontend")
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", FRONTEND, "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv

ESID = "ed6d6e9e-2020-4f58-89a2-5750d28db8a4"
ASYNC_RESULT = ("Async agent launched successfully. (This tool result is internal metadata — "
                "never quote or paste any part of it, including the agentId below, into a "
                "user-facing reply.)\nagentId: %s (internal ID - do not mention to user.)\n"
                "The agent is working in the background.")


def _iso(ms):
    """毫秒转记录里那种 ISO 时刻。子代理的派出时刻取自它记录首条的 timestamp。"""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:23] + "Z"


def _notif(aid, status="completed", tuid="", at=""):
    """跑完时注入的那条通知。at 不给就取此刻:通知时刻与子代理记录的 mtime 现在要放在一起
    比(见「恢复后又跑起来」那一节),两者得落在同一个时间尺度上,一个写死、一个取此刻会
    让所有样本看起来都像刚被恢复过。"""
    at = at or _iso(int(time.time() * 1000))
    body = ("<task-notification>\n<task-id>%s</task-id>\n" % aid
            + ("<tool-use-id>%s</tool-use-id>\n" % tuid if tuid else "")
            + "<status>%s</status>\n<summary>Agent \"某件事\" finished</summary>\n"
              "</task-notification>" % status)
    return {"type": "user", "uuid": "n-" + aid, "timestamp": at,
            "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": body}}


def _call(tuid, at="2026-08-12T09:10:00.000Z"):
    return {"type": "assistant", "uuid": "c-" + tuid, "timestamp": at,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tuid, "name": "Agent",
                 "input": {"description": "某件事", "subagent_type": "general-purpose"}}]}}


def _result(tuid, text, at="2026-08-12T09:10:01.000Z"):
    return {"type": "user", "uuid": "r-" + tuid, "timestamp": at,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tuid,
                 "content": [{"type": "text", "text": text}]}]}}


class Bus:
    def publish(self, ev):
        pass


class SubagentStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, ESID + ".jsonl")
        self.dir = os.path.join(self.tmp.name, ESID, "subagents")
        os.makedirs(self.dir)
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, Bus())
        self.ad.index["s1"] = {"engine_sid": ESID}
        self.ad._transcript_path = lambda esid: self.path
        self._rollout([])

    def tearDown(self):
        self.tmp.cleanup()

    def _rollout(self, rows):
        with open(self.path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _agent(self, aid, tuid, lines=1, ago=0, started=0, touched=0):
        """ago:这份记录多久没动过(毫秒)。updated 取自文件 mtime,所以往回拨 mtime。
        touched:直接指定最后一次写记录的时刻(毫秒绝对值),要把它摆到某条通知前后时用。
        started:派出时刻(毫秒),不给就用固定的那一刻。它取自首条的 timestamp。"""
        p = os.path.join(self.dir, "agent-%s.jsonl" % aid)
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user",
                                "timestamp": _iso(started) if started else "2026-08-12T09:10:00.000Z",
                                "message": {"content": "去查一件事"}}, ensure_ascii=False) + "\n")
            for i in range(lines):
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Bash"}]}}, ensure_ascii=False) + "\n")
        with open(p[:-len(".jsonl")] + ".meta.json", "w", encoding="utf-8") as f:
            json.dump({"agentType": "general-purpose", "toolUseId": tuid,
                       "description": "某件事", "spawnDepth": 1}, f)
        if ago or touched:
            t = touched / 1000.0 if touched else time.time() - ago / 1000.0
            os.utime(p, (t, t))

    def _one(self, aid="a44b3b6ebbc038143"):
        return {x["id"]: x for x in self.ad.subagents("s1")}[aid]

    def test_详情支持字节尾窗和消息上限(self):
        aid = "a44b3b6ebbc038143"
        self._agent(aid, "toolu_01J54yBgxS3Cbg7KZArhup3n", lines=40)
        rows = self.ad.subagent_log("s1", aid, limit=3, limit_bytes=512)
        self.assertLessEqual(len(rows), 3)
        self.assertTrue(rows)

    def test_异步派出后只拿到占位返回的算还在跑(self):
        """这一路的返回在派出的一瞬间就有了,只看有没有返回会当场判成已完成。"""
        self._agent("a44b3b6ebbc038143", "toolu_01J54yBgxS3Cbg7KZArhup3n")
        self._rollout([_call("toolu_01J54yBgxS3Cbg7KZArhup3n"),
                       _result("toolu_01J54yBgxS3Cbg7KZArhup3n",
                               ASYNC_RESULT % "a44b3b6ebbc038143")])
        self.assertEqual(self._one()["status"], "running")

    def test_跑完时注入的通知是交回的准确信号(self):
        # 记录最后一次写在通知之前一秒:交回的顺序本来就是先写完自己的记录、再注入通知。
        self._agent("a44b3b6ebbc038143", "toolu_01J54yBgxS3Cbg7KZArhup3n",
                    touched=1786526715509 - 1000)
        self._rollout([_call("toolu_01J54yBgxS3Cbg7KZArhup3n"),
                       _result("toolu_01J54yBgxS3Cbg7KZArhup3n",
                               ASYNC_RESULT % "a44b3b6ebbc038143"),
                       _notif("a44b3b6ebbc038143", tuid="toolu_01J54yBgxS3Cbg7KZArhup3n",
                              at="2026-08-12T09:25:15.509Z")])
        got = self._one()
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["outcome"], "completed")
        self.assertEqual(got["ended"], 1786526715509)   # 2026-08-12T09:25:15.509Z

    def test_收场方式照实报(self):
        """上一段会话退出时留下的代理,通知里的 status 是 stopped,不是 completed。"""
        self._agent("a9f73b336389d2aec", "toolu_01Gn9eNNggH4abLvSVZCG3Ky")
        self._rollout([_call("toolu_01Gn9eNNggH4abLvSVZCG3Ky"),
                       _notif("a9f73b336389d2aec", "stopped")])
        got = self._one("a9f73b336389d2aec")
        self.assertEqual((got["status"], got["outcome"]), ("done", "stopped"))

    def test_同步派出的那一路返回即交回(self):
        """返回的是整份报告,不是那句占位——没有通知也能判。"""
        self._agent("a19c0691029fc9cea", "toolu_01KPc3LxvWoTBmWTDCbKdchU")
        self._rollout([_call("toolu_01KPc3LxvWoTBmWTDCbKdchU"),
                       _result("toolu_01KPc3LxvWoTBmWTDCbKdchU", "假设不成立。后端路径解析是对的。")])
        self.assertEqual(self._one("a19c0691029fc9cea")["status"], "done")

    def test_同步派出且调用还挂着的算还在跑(self):
        self._agent("a19c0691029fc9cea", "toolu_01KPc3LxvWoTBmWTDCbKdchU")
        self._rollout([_call("toolu_01KPc3LxvWoTBmWTDCbKdchU")])
        self.assertEqual(self._one("a19c0691029fc9cea")["status"], "running")

    def test_等不到收场而记录早已不动的不算在跑(self):
        """「没等到收场的证据」不等于「还在跑」:进程被杀、会话被压缩把那几帧挤掉、通知落进
        另一份记录,证据都会永远等不来。实见一批 33 天前的子代理全判成 running,菜单里
        每条右边写着「无响应 47640 分钟」,入口上却挂着「33 个代理运行中」。"""
        self._agent("a19c0691029fc9cea", "toolu_01KPc3LxvWoTBmWTDCbKdchU",
                    ago=33 * 86400 * 1000)
        self._rollout([_call("toolu_01KPc3LxvWoTBmWTDCbKdchU")])
        self.assertEqual(self._one("a19c0691029fc9cea")["status"], "")

    def test_安静几分钟仍算在跑(self):
        """一次大扫描安静两三分钟是常事,不能一沉默就判死。"""
        self._agent("a19c0691029fc9cea", "toolu_01KPc3LxvWoTBmWTDCbKdchU", ago=5 * 60 * 1000)
        self._rollout([_call("toolu_01KPc3LxvWoTBmWTDCbKdchU")])
        self.assertEqual(self._one("a19c0691029fc9cea")["status"], "running")

    def test_有收场证据的不受静默时长影响(self):
        """通知在手就按通知走。一年前交回的仍然是 done,不该因为久远而变成判不出。"""
        self._agent("a19c0691029fc9cea", "toolu_01KPc3LxvWoTBmWTDCbKdchU",
                    ago=365 * 86400 * 1000)
        self._rollout([_call("toolu_01KPc3LxvWoTBmWTDCbKdchU"),
                       _notif("a19c0691029fc9cea", "completed")])
        self.assertEqual(self._one("a19c0691029fc9cea")["status"], "done")

    def test_主记录里找不到痕迹时不硬判(self):
        """派它出来的那一轮被撤回过,记录里一条都不剩。给空状态,由前端退回时间窗。"""
        self._agent("aa685db84b995ea1b", "toolu_01PnSz1E6wvvjbWuhw2VytDp")
        self._rollout([_call("toolu_01OtherCallEntirely")])
        self.assertEqual(self._one("aa685db84b995ea1b")["status"], "")

    def test_名单里多出一个代理时重扫(self):
        """续读只覆盖新追加的那一段,新代理的调用可能落在已经读过的地方。"""
        self._rollout([_call("toolu_01KPc3LxvWoTBmWTDCbKdchU"),
                       _result("toolu_01KPc3LxvWoTBmWTDCbKdchU",
                               ASYNC_RESULT % "a19c0691029fc9cea")])
        self.assertEqual(self.ad.subagents("s1"), [])
        self._agent("a19c0691029fc9cea", "toolu_01KPc3LxvWoTBmWTDCbKdchU")
        self.assertEqual(self._one("a19c0691029fc9cea")["status"], "running")

    def test_记录长出新的一段之后状态跟着改(self):
        self._agent("a19c0691029fc9cea", "toolu_01KPc3LxvWoTBmWTDCbKdchU")
        rows = [_call("toolu_01KPc3LxvWoTBmWTDCbKdchU"),
                _result("toolu_01KPc3LxvWoTBmWTDCbKdchU", ASYNC_RESULT % "a19c0691029fc9cea")]
        self._rollout(rows)
        self.assertEqual(self._one("a19c0691029fc9cea")["status"], "running")
        self._rollout(rows + [_notif("a19c0691029fc9cea")])
        self.assertEqual(self._one("a19c0691029fc9cea")["status"], "done")

    def test_不误伤用户自己粘的尖括号内容(self):
        """真人贴一段带 task-notification 标签的正文,不是引擎写下的事实,不作数。"""
        self._agent("a44b3b6ebbc038143", "toolu_01J54yBgxS3Cbg7KZArhup3n")
        paste = {"type": "user", "uuid": "u1", "timestamp": "2026-08-12T09:26:00.000Z",
                 "message": {"role": "user", "content":
                             "帮我看看这段:\n<task-notification>\n"
                             "<task-id>a44b3b6ebbc038143</task-id>\n"
                             "<status>completed</status>\n</task-notification>"}}
        self._rollout([_call("toolu_01J54yBgxS3Cbg7KZArhup3n"),
                       _result("toolu_01J54yBgxS3Cbg7KZArhup3n",
                               ASYNC_RESULT % "a44b3b6ebbc038143"), paste])
        self.assertEqual(self._one()["status"], "running")

    # ---- 恢复后又跑起来:见到通知不等于已经收场 ----
    # 一个子代理可以被恢复:给已经交回的它再发一条消息,它接着往下跑(通知正文里那句 note
    # 也写着 the same task-id may notify more than once)。恢复之后上一轮那条完成通知仍然留在
    # 主记录里,只看「有没有通知」就会让它一直报「执行完成」——实见 2026-08-12 恢复的一个
    # 子代理,/session/<sid>/agents 给的是 done/completed 而它确实在跑,界面上那一栏因此
    # 不出现(入口只在有 running 的时候显示)。判据补一条:它自己的记录在最后一条通知之后
    # 又动过,就说明又跑起来了。

    def test_恢复之后又跑起来的算还在跑(self):
        now = int(time.time() * 1000)
        self._agent("a44b3b6ebbc038143", "toolu_01J54yBgxS3Cbg7KZArhup3n")
        self._rollout([_call("toolu_01J54yBgxS3Cbg7KZArhup3n"),
                       _result("toolu_01J54yBgxS3Cbg7KZArhup3n",
                               ASYNC_RESULT % "a44b3b6ebbc038143"),
                       _notif("a44b3b6ebbc038143", at=_iso(now - 5 * 60 * 1000))])
        got = self._one()
        self.assertEqual((got["status"], got["outcome"], got["ended"]), ("running", "", 0))

    def test_交回之后记录不再动的仍然是已交回(self):
        """通知在手、记录此后一动不动,这才是真的收场了。"""
        now = int(time.time() * 1000)
        self._agent("a44b3b6ebbc038143", "toolu_01J54yBgxS3Cbg7KZArhup3n",
                    touched=now - 6 * 60 * 1000)
        self._rollout([_call("toolu_01J54yBgxS3Cbg7KZArhup3n"),
                       _notif("a44b3b6ebbc038143", at=_iso(now - 5 * 60 * 1000))])
        self.assertEqual(self._one()["status"], "done")

    def test_两个时刻挨得很近时不翻牌(self):
        """通知时刻取主记录里那一行的 timestamp,记录变动时刻取文件 mtime,两处来源不同。
        本机样本里 mtime 一律不晚于通知时刻、超出的最大值 0.08 秒,余量要盖得住这点抖动。"""
        now = int(time.time() * 1000)
        self._agent("a44b3b6ebbc038143", "toolu_01J54yBgxS3Cbg7KZArhup3n")
        self._rollout([_call("toolu_01J54yBgxS3Cbg7KZArhup3n"),
                       _notif("a44b3b6ebbc038143", at=_iso(now - 1000))])
        self.assertEqual(self._one()["status"], "done")

    def test_恢复之后又长久不动的判不出(self):
        """又跑起来不等于永远在跑:静默这道兜底照旧管着这一支。"""
        now = int(time.time() * 1000)
        self._agent("a44b3b6ebbc038143", "toolu_01J54yBgxS3Cbg7KZArhup3n",
                    ago=33 * 86400 * 1000)
        self._rollout([_call("toolu_01J54yBgxS3Cbg7KZArhup3n"),
                       _notif("a44b3b6ebbc038143", at=_iso(now - 40 * 86400 * 1000))])
        self.assertEqual(self._one()["status"], "")

    def test_收场方式取最后一条通知(self):
        """同一个子代理先 completed 后 stopped(本机记录里两种先后都见过),报最后那一次。"""
        now = int(time.time() * 1000)
        t1, t2 = now - 20 * 60 * 1000, now - 10 * 60 * 1000
        self._agent("a9f73b336389d2aec", "toolu_01Gn9eNNggH4abLvSVZCG3Ky", touched=t2 - 1000)
        self._rollout([_call("toolu_01Gn9eNNggH4abLvSVZCG3Ky"),
                       _notif("a9f73b336389d2aec", "completed", at=_iso(t1)),
                       _notif("a9f73b336389d2aec", "stopped", at=_iso(t2))])
        got = self._one("a9f73b336389d2aec")
        self.assertEqual((got["status"], got["outcome"], got["ended"]), ("done", "stopped", t2))

    def test_通知在记录里前后颠倒时仍按时刻取(self):
        """记录被压缩重写过、或旁支合并进来时,行的顺序未必就是时序。"""
        now = int(time.time() * 1000)
        t1, t2 = now - 20 * 60 * 1000, now - 10 * 60 * 1000
        self._agent("a9f73b336389d2aec", "toolu_01Gn9eNNggH4abLvSVZCG3Ky", touched=t2 - 1000)
        self._rollout([_call("toolu_01Gn9eNNggH4abLvSVZCG3Ky"),
                       _notif("a9f73b336389d2aec", "stopped", at=_iso(t2)),
                       _notif("a9f73b336389d2aec", "completed", at=_iso(t1))])
        got = self._one("a9f73b336389d2aec")
        self.assertEqual((got["outcome"], got["ended"]), ("stopped", t2))

    def test_同步派出的那一路不受这一维影响(self):
        """同步那一路没有通知,返回即交回,记录之后再动也不改判——它的返回是父回合当场
        拿到的整份报告,不像通知那样会在恢复之后仍旧留在记录里。"""
        self._agent("a19c0691029fc9cea", "toolu_01KPc3LxvWoTBmWTDCbKdchU")
        self._rollout([_call("toolu_01KPc3LxvWoTBmWTDCbKdchU"),
                       _result("toolu_01KPc3LxvWoTBmWTDCbKdchU", "假设不成立。",
                               at=_iso(int(time.time() * 1000) - 10 * 60 * 1000))])
        self.assertEqual(self._one("a19c0691029fc9cea")["status"], "done")

    # ---- 进程实例这一维:派它出来的那个引擎进程还在不在 ----
    # 子代理不是独立进程,它活在引擎进程的事件循环里,没有自己的 pid,所以只能盯派它出来的
    # 那个进程。而进程实例会在一条会话的生命期里更换(同一段会话先后见过 pid 24515 与
    # 40576),判据必须按实例记:起始早于当前实例的子代理属于一个已经消失的实例,必然不在跑。

    def _proc(self, started_ago_ms, pid=40576):
        """给这条会话登记一个引擎进程实例。"""
        self.ad.index["s1"]["proc"] = {"pid": pid,
                                       "started": int(time.time() * 1000) - started_ago_ms}

    def test_起始早于当前进程实例的必然不在跑(self):
        """记录还很新、证据也等不到,时间窗那道兜底判不出;而它是上一个进程实例派出来的,
        随那个实例一起没了。这一条不需要推断。"""
        now = int(time.time() * 1000)
        self._agent("a19c0691029fc9cea", "toolu_01KPc3LxvWoTBmWTDCbKdchU",
                    started=now - 3 * 60 * 1000)
        self._rollout([_call("toolu_01KPc3LxvWoTBmWTDCbKdchU")])
        self.assertEqual(self._one("a19c0691029fc9cea")["status"], "running")
        self._proc(60 * 1000)
        got = self._one("a19c0691029fc9cea")
        self.assertEqual((got["status"], got["outcome"]), ("done", "gone"))

    def test_当前实例之后派出的照旧按证据判(self):
        """进程还是那一个,某个子代理是交回了还是仍在跑,仍然要看通知与返回。"""
        now = int(time.time() * 1000)
        self._proc(10 * 60 * 1000)
        self._agent("a19c0691029fc9cea", "toolu_01KPc3LxvWoTBmWTDCbKdchU", started=now - 60 * 1000)
        self._rollout([_call("toolu_01KPc3LxvWoTBmWTDCbKdchU")])
        self.assertEqual(self._one("a19c0691029fc9cea")["status"], "running")

    def test_没有进程记录时不硬判(self):
        """会话可能在 Desk 之外起过(比如用户在终端里 resume),那时进程不在 Desk 手里。
        判不出就退回原有判据——把在跑的判死,代价比判活大。"""
        now = int(time.time() * 1000)
        self._agent("a19c0691029fc9cea", "toolu_01KPc3LxvWoTBmWTDCbKdchU",
                    started=now - 33 * 86400 * 1000)
        self._rollout([_call("toolu_01KPc3LxvWoTBmWTDCbKdchU")])
        self.assertEqual(self._one("a19c0691029fc9cea")["status"], "running")

    def test_收场证据压过进程实例(self):
        """通知连收场方式和时刻一起给,比这一维讲得清楚,不该被改写成 gone。"""
        now = int(time.time() * 1000)
        self._agent("a9f73b336389d2aec", "toolu_01Gn9eNNggH4abLvSVZCG3Ky", started=now - 600000)
        self._rollout([_call("toolu_01Gn9eNNggH4abLvSVZCG3Ky"),
                       _notif("a9f73b336389d2aec", "stopped")])
        self._proc(60 * 1000)
        got = self._one("a9f73b336389d2aec")
        self.assertEqual((got["status"], got["outcome"]), ("done", "stopped"))

    def test_恢复后又跑起来但属于旧进程实例的仍然判死(self):
        """次序:通知这一维先判出「又跑起来了」,进程实例这一维摆在最后再过一遍。子代理活在
        进程的事件循环里,派它出来的那个实例没了它必然跟着没,恢复的痕迹只是旧记录。"""
        now = int(time.time() * 1000)
        self._agent("a44b3b6ebbc038143", "toolu_01J54yBgxS3Cbg7KZArhup3n",
                    started=now - 10 * 60 * 1000)
        self._rollout([_call("toolu_01J54yBgxS3Cbg7KZArhup3n"),
                       _notif("a44b3b6ebbc038143", at=_iso(now - 5 * 60 * 1000))])
        self.assertEqual(self._one()["status"], "running")
        self._proc(60 * 1000)
        got = self._one()
        self.assertEqual((got["status"], got["outcome"]), ("done", "gone"))

    def test_两个时刻挨得很近时留余量(self):
        """子代理必然晚于派它的进程起步,严格比较本来就够;留一分钟是给系统对时留的。"""
        now = int(time.time() * 1000)
        self._proc(0)
        self._agent("a19c0691029fc9cea", "toolu_01KPc3LxvWoTBmWTDCbKdchU", started=now - 10 * 1000)
        self._rollout([_call("toolu_01KPc3LxvWoTBmWTDCbKdchU")])
        self.assertEqual(self._one("a19c0691029fc9cea")["status"], "running")

    def test_进程实例记进会话索引并跨重启留存(self):
        """后端重启之后这一维仍要在:重启前派出的子代理正是最需要它的那一批。"""
        import types
        self.ad._note_proc("s1", types.SimpleNamespace(pid=24515))
        got = self.ad.proc_instance("s1")
        self.assertEqual(got["pid"], 24515)
        self.assertLessEqual(abs(got["started"] - int(time.time() * 1000)), 5000)
        again = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, Bus())
        self.assertEqual(again.proc_instance("s1"), got)
        self.assertEqual(ea.ClaudeAdapter(self.tmp.name, self.tmp.name, Bus()).proc_instance("s2"), {})

    def test_起进程时自动登记实例(self):
        """老路(一回合一进程)与常驻路都要登记,子代理在哪条路上派出的都算得上。"""
        ctx = {"sid": "s1", "msg_id": "m1", "parts": {}, "order": []}
        self.ad._turn_oneshot(ctx, ["/bin/echo", "{}"], dict(os.environ), None)
        got = self.ad.proc_instance("s1")
        self.assertEqual(got["pid"], ctx["proc"].pid)
        self.assertLessEqual(abs(got["started"] - int(time.time() * 1000)), 5000)

    def test_原有字段一个不少(self):
        self._agent("a44b3b6ebbc038143", "toolu_01J54yBgxS3Cbg7KZArhup3n")
        got = self._one()
        for k in ("id", "kind", "tool_use_id", "task", "last", "started", "updated", "size"):
            self.assertIn(k, got)
        self.assertEqual(got["last"], "调用 Bash")


HARNESS = r"""
globalThis.setInterval = () => 7;
globalThis.clearInterval = () => {};
const NOW = Date.now();
const label = { textContent: "" };
const btn = { hidden: null, classList: { toggle() {} },
              querySelector: () => label, onclick: null };
function api() { return Promise.resolve(LIST); }
function hsEllipsis(s) { return s; }
function turn(tools) {
  return { parts: new Map(tools.map((t, i) => [i, { type: "tool", tool: "AGENT_TOOL_NAME",
                                                    state: { status: t } }])) };
}
const v = { sid: "s1", busy: BUSY, statusT0: SINCE, turns: TURNS.map(turn),
            agents: [], el: { isConnected: true, querySelector: (s) => (s === ".hs-agent" ? btn : null) } };

SRC

hsAgentsSync(v).then(() => console.log(JSON.stringify({
  ids: v.agents.map((a) => a.id),
  hidden: btn.hidden,
  watching: !!v.agentsTick,
  label: label.textContent,
  states: v.agents.map((a) => hsAgentState(v, a)),
})));
"""


def _slice(path, head):
    src = _srcmap.frontend_src(path)
    m = re.search(re.escape(head) + r".*?\n}\n", src, re.S)
    assert m, head
    return m.group(0)


def _consts(names):
    src = _srcmap.chat_src()
    return "\n".join(re.search(r"^const %s = .*$" % n, src, re.M).group(0) for n in names)


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class FrontendAgentEntry(unittest.TestCase):
    """入口的出现与消失,以及名单怎么切。函数从源文件里抽出来跑,喂后端真实字段形状。"""

    def _run(self, listing, busy=True, since=0, turns=(), tool='Agent'):
        src = "\n".join([_consts(["HS_AG_ARM", "HS_AG_FRESH", "HS_AG_STALE", "HS_AG_QUIET"])]
                        + [_slice("app-chat.js", "function " + n + "(") for n in
                           ("hsAgentsWatch", "hsAgentArm", "hsAgentLive",
                            "hsAgentToolBusy", "hsAgentOutcomeText", "hsAgentState")]
                        + [_slice("app-chat.js", "async function hsAgentsSync(")])
        js = (HARNESS.replace("SRC", src).replace("AGENT_TOOL_NAME", tool)
              .replace("LIST", json.dumps(listing, ensure_ascii=False))
              .replace("BUSY", json.dumps(bool(busy)))
              .replace("SINCE", json.dumps(since))
              .replace("TURNS", json.dumps([list(t) for t in turns])))
        out = subprocess.run(["node", "-e", NODE_PRELUDE + js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    @staticmethod
    def _a(aid, status, ago=0, **kw):
        import time
        at = int(time.time() * 1000) - ago
        return dict({"id": aid, "kind": "general-purpose", "task": "某件事", "last": "调用 Bash",
                     "started": at, "updated": at, "size": 100, "tool_use_id": "toolu_" + aid,
                     "status": status, "outcome": "", "ended": 0}, **kw)

    def test_主线结束而代理还在跑时入口不消失(self):
        """异步派出的代理活过了派它出来的那个回合。原来主线一停,入口立刻没了。"""
        r = self._run([self._a("a1", "running")], busy=False, since=0)
        self.assertEqual(r["ids"], ["a1"])
        self.assertIs(r["hidden"], False)
        self.assertEqual(r["label"], "1 个代理运行中")
        self.assertTrue(r["watching"])

    def test_queue_phase_updates_header_and_menu_and_yields_to_terminal(self):
        names = ('hsAgentLive', 'hsAgentToolBusy', 'hsAgentOutcomeText', 'hsAgentState',
                 'hsAgentHead', 'hsAgo', 'hsAgentDoing', 'hsAgentSpan', 'hsAgentValue')
        src = _consts(['HS_AG_FRESH', 'HS_AG_STALE', 'HS_AG_QUIET']) + '\n' + '\n'.join(
            _slice('app-chat.js', 'function ' + name + '(') for name in names)
        js = src + r'''
const assert = require('node:assert/strict');
const label = {textContent:''}, dot = {};
const host = {querySelector:s=>s==='.rp-dot' ? dot : label};
const v = {busy:false, turns:[], el:{querySelector:()=>host}};
function hsSetText(el,text) {el.textContent=text;}
function hsClock() {return '7m32s';}
const a = {status:'running',phase:'capacity',updated:Date.now()-600000,started:Date.now()-600000,last:'等待 Provider 容量'};
hsAgentHead(v,a);
assert.equal(label.textContent,'排队中 · 10 分钟前');
assert.equal(hsAgentValue(v,a),'排队中 7m32s');
assert.equal(dot.className,'rp-dot rp-dot-run');
a.phase='model'; a.updated=Date.now(); a.last='等待模型响应';
hsAgentHead(v,a);
assert.equal(label.textContent,'运行中 · 刚刚');
assert.ok(!hsAgentValue(v,a).includes('排队中'));
a.phase='capacity'; a.status='done'; a.outcome='failed';
hsAgentHead(v,a);
assert.equal(label.textContent,'已失败');
assert.equal(hsAgentValue(v,a),'已失败 7m32s');
'''
        result = subprocess.run(['node','-e',NODE_PRELUDE + js],capture_output=True,text=True,timeout=30, encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_刷新后在跑的代理不被回合边界滤掉(self):
        """刷新页面后 statusT0 重算成本回合起点,上一轮派出去、还在跑的那个会被滤掉。"""
        r = self._run([self._a("a1", "running", ago=600000)], busy=True, since=int(__import__(
            "time").time() * 1000) - 60000)
        self.assertEqual(r["ids"], ["a1"])
        self.assertIs(r["hidden"], False)

    def test_已交回的按回合边界切(self):
        """subagents 目录只增不减,不切的话一打开就是一份历史名单。"""
        r = self._run([self._a("old", "done", ago=3600000), self._a("a1", "running")],
                      busy=True, since=int(__import__("time").time() * 1000) - 60000)
        self.assertEqual(r["ids"], ["a1"])

    def test_刚派出还没落地时先盯着(self):
        """名单是空的,只有父回合里那张 Agent 工具卡。这一拍不开轮询就再也没人去查。"""
        r = self._run([], busy=True, turns=[["running"]])
        self.assertTrue(r["watching"])
        self.assertIs(r["hidden"], True)

    def test_没人在跑也没在看时不轮询(self):
        r = self._run([self._a("a1", "done", ago=600000)], busy=False)
        self.assertFalse(r["watching"])
        self.assertIs(r["hidden"], True)

    def test_pi_subagent_arms_polling_before_sidecar_exists(self):
        result = self._run([], busy=True, turns=[["running"]], tool='subagent')
        self.assertTrue(result['watching'])

    def test_收场方式报到菜单那一格(self):
        r = self._run([self._a("a1", "done", outcome="stopped")], busy=True,
                      since=int(__import__("time").time() * 1000) - 60000)
        self.assertEqual(r["states"], [{"dot": "done", "note": "已中止"}])

    def test_随进程消失的报进程已退出(self):
        """派它出来的那个引擎进程实例已经换过一轮,它随旧实例一起没了,谈不上「已交回」。"""
        r = self._run([self._a("a1", "done", outcome="gone")], busy=True,
                      since=int(__import__("time").time() * 1000) - 60000)
        self.assertEqual(r["states"], [{"dot": "done", "note": "进程已退出"}])

    def test_长工具调用期间不报无响应(self):
        """后端说还在跑,记录三分钟没动——一次大扫描安静两分钟是常事,不该报。"""
        r = self._run([self._a("a1", "running", ago=180000)], busy=False)
        self.assertEqual(r["states"], [{"dot": "run", "note": ""}])

    def test_后端说在跑而记录确实很久没动才报无响应(self):
        r = self._run([self._a("a1", "running", ago=600000)], busy=False)
        self.assertEqual(r["states"][0]["dot"], "stale")
        self.assertIn("无响应", r["states"][0]["note"])

    def test_后端明确在跑时长期静默也不改写成已停止(self):
        """排队模型或长工具可以静默超过半小时；状态仍由后端进程与调用身份决定。"""
        r = self._run([self._a("a1", "running", ago=3600000)], busy=False)
        self.assertEqual(r["ids"], ["a1"])
        self.assertIs(r["hidden"], False)
        self.assertEqual(r["label"], "1 个代理运行中")
        self.assertEqual(r["states"][0]["dot"], "stale")
        self.assertIn("无响应", r["states"][0]["note"])

    def test_后端给不出状态时退回时间窗(self):
        """旧版本后端,或那一轮已经不在记录里:照旧按「主线在忙 + 记录刚动过」猜。
        两处都给 since:这测的是状态判定,得先让它进得了名单——已交回的按回合归属切,
        没有 since 就没有「本回合」,一条都不列(见下一条用例)。"""
        since = int(__import__("time").time() * 1000) - 60000
        r = self._run([self._a("a1", "", ago=1000)], busy=True, since=since)
        self.assertEqual(r["states"], [{"dot": "run", "note": ""}])
        r = self._run([self._a("a1", "", ago=1000)], busy=False, since=since)
        self.assertEqual(r["states"], [{"dot": "done", "note": "执行完成"}])

    def test_判不出回合归属时不列已交回的(self):
        """刚刷完页面、还没开始新回合(statusT0 为 0)。这时「本回合」并不存在,
        放行等于把整段会话的历史名单摊开——实见刷新后菜单里一次冒出八条旧代理。
        在跑的不受影响,它本来就不按回合切。"""
        listing = [self._a("old1", "done", ago=3600000), self._a("old2", "done", ago=1800000),
                   self._a("now", "running")]
        self.assertEqual(self._run(listing, busy=False, since=0)["ids"], ["now"])

    def test_归属按派出时刻而不是最后写记录的时刻(self):
        """上一轮派出、这一轮才交回的代理不算这一轮的。updated 是它最后写记录的时刻,
        用它判归属会把上一轮的活挂到这一轮名下。"""
        now = int(__import__("time").time() * 1000)
        since = now - 60000
        early = self._a("early", "done", started=now - 600000, updated=now - 1000)
        mine = self._a("mine", "done", started=now - 30000, updated=now - 2000)
        self.assertEqual(self._run([early, mine], busy=True, since=since)["ids"], ["mine"])


class FirstSyncOrder(unittest.TestCase):
    """历史是切页主路径,子代理名单在首帧之后再取。

    Codex 名单要扫描 rollout；它若与 /message 并发,会持 GIL 把几十毫秒的历史请求拖到
    近一秒。名单不是首屏内容,排到历史完成并绘制一帧之后更符合优先级。"""

    def test_建页不让名单与历史争抢(self):
        src = _srcmap.chat_src()
        build = re.search(r"\nfunction hsBuild\(.*?\n}", src, re.S).group(0)
        before_load = build[:build.index("if (hsHidden(v))")]
        self.assertNotRegex(before_load, r"(?m)^  hsAgentsSync\(v\);$")

    def test_名单在历史完成后的下一帧取(self):
        src = _srcmap.chat_src()
        build = re.search(r"\nfunction hsBuild\(.*?\n}", src, re.S).group(0)
        self.assertIn("const agentsAfterLoad = () => requestAnimationFrame", build)
        self.assertIn("else hsLoad(v).then(agentsAfterLoad", build)


if __name__ == "__main__":
    unittest.main()
