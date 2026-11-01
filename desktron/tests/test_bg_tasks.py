"""后台命令的台账与那枚芯片(2026-08-20)。

`Bash(run_in_background)` 起的命令跑在回合之外:回合早收尾了它还在跑,跑完由 harness 注入
一条 task-notification。界面上此前一个字都没有——用户实见两条命令跑了 120 秒,期间只能靠
问「子代理返回了吗」确认,而它们根本不是子代理(拿 adp_190349c63f7f48 的轨迹核过:
task_started 与 task_notification 常隔 20 毫秒到 1 秒,子代理不可能这么快)。

认它的依据是 Bash 工具回执的原文,不是 system/task_started ——后者我们手上没有帧体,
分不出后台命令、子代理还是别的什么。回执那句照本机 transcript 抄:
  Command running in background with ID: b80tqyx2a. Output is being written to: …
"""
import os
import sys
import tempfile
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv

CHAT = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()

# 本机 transcript 里的原话(toolUseResult.backgroundTaskId 同为 b80tqyx2a)
RECEIPT = ("Command running in background with ID: b80tqyx2a. Output is being written to: "
           "/private/tmp/claude-501/-Users-x-y/436ddefc/tasks/b80tqyx2a.output. "
           "You will be notified when it completes.")


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, e):
        self.events.append(e)

    def bg(self):
        return [e["properties"] for e in self.events if e.get("type") == "session.bgtasks"]


def _tool_result(text, call_id="tool_bg"):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": call_id,
         "content": [{"type": "text", "text": text}]}]}}


def _notif(task):
    return {"type": "system", "subtype": "task_notification", "task_id": task, "status": "completed"}


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self):
        return {"sid": self.sid, "msg_id": "msg_a_1", "parts": {}, "order": [],
                "last_tool": None, "t0": 0, "persist": True, "text": "跑一下"}

    def test_从回执认出一条(self):
        self.ad._bg_note(self.sid, RECEIPT)
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 1)

    def test_通知到了就销号(self):
        self.ad._bg_note(self.sid, RECEIPT)
        self.ad._bg_done(self.sid, "b80tqyx2a")
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 0)

    def test_id_带前缀也认(self):
        """有些形态下 id 带 `bash:` / `agent:` 前缀,取冒号后那一段(同 _aside_agent)。"""
        self.ad._bg_note(self.sid, RECEIPT)
        self.ad._bg_done(self.sid, "bash:b80tqyx2a")
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 0)

    def test_同一条不重复计数(self):
        self.ad._bg_note(self.sid, RECEIPT)
        self.ad._bg_note(self.sid, RECEIPT)
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 1)

    def test_多条各算各的(self):
        self.ad._bg_note(self.sid, RECEIPT)
        self.ad._bg_note(self.sid, RECEIPT.replace("b80tqyx2a", "bu951rr63"))
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 2)
        self.ad._bg_done(self.sid, "b80tqyx2a")
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 1)

    def test_普通命令的回执不算(self):
        for txt in ("total 88\ndrwxr-xr-x", "", "Command failed with exit code 1",
                    "Running in background"):
            self.ad._bg_note(self.sid, txt)
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 0)

    def test_不认识的_id_销号不炸(self):
        self.ad._bg_done(self.sid, "从没见过")
        self.ad._bg_done(self.sid, "")
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 0)

    def test_按会话隔开(self):
        other = self.ad.create_session()["id"]
        self.ad._bg_note(self.sid, RECEIPT)
        self.assertEqual(self.ad.bg_tasks(other)["running"], 0)

    def test_起止各发一帧(self):
        self.ad._bg_note(self.sid, RECEIPT)
        self.ad._bg_done(self.sid, "b80tqyx2a")
        got = self.bus.bg()
        self.assertEqual([g["running"] for g in got], [1, 0])
        self.assertEqual(got[0]["sessionID"], self.sid)

    def test_没变化不发帧(self):
        """重复的回执、认不出的 id 都不该把同一个数再播一遍。"""
        self.ad._bg_note(self.sid, RECEIPT)
        n = len(self.bus.bg())
        self.ad._bg_note(self.sid, RECEIPT)
        self.ad._bg_done(self.sid, "别的")
        self.assertEqual(len(self.bus.bg()), n)

    def test_台账不挂在_ctx_上(self):
        """命令活得比回合长(实测有跑满 120 秒的)。记在 ctx 上出了回合就没了,
        所以台账挂在适配器上——换一个 ctx 照样查得到。"""
        self.ad._bg_note(self.sid, RECEIPT)
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 1)
        self.ad.handle_event(_notif("从没见过的"), self._ctx())   # 另起一个 ctx
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 1)

    def test_走真实帧序认出来(self):
        """必须驱动 handle_event 那条真路:只调 _bg_note 的话,把调用点从工具回执那里
        删掉,上面那些用例照样全绿(2026-08-20 反向验证发现)。"""
        ctx = self._ctx()
        # 这一轮先要见过 init,否则整帧被当成上一轮的残帧丢掉(见 handle_event 的开闸判据)
        self.ad.handle_event({"type": "system", "subtype": "init", "session_id": "e1"}, ctx)
        # 工具回执按 tool_use_id 关联；真实帧中的调用标识不可省略。
        ctx["parts"]["p_tool"] = {"id": "p_tool", "type": "tool", "tool": "Bash",
                                  "state": {"status": "run", "input": {}, "output": ""}}
        ctx["order"].append("p_tool")
        ctx["last_tool"] = "p_tool"
        ctx["tuid"] = {"tool_bg": "p_tool"}
        self.ad.handle_event(_tool_result(RECEIPT), ctx)
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 1,
                         "工具回执那条路没有认出后台命令")

    def test_走真实帧序销号(self):
        ctx = self._ctx()
        self.ad.handle_event({"type": "system", "subtype": "init", "session_id": "e1"}, ctx)
        self.ad._bg_note(self.sid, RECEIPT)
        self.ad.handle_event(_notif("b80tqyx2a"), ctx)
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 0)

    def test_config_读取时按_output_退出标记对账(self):
        out = os.path.join(self.tmp.name, "b80tqyx2a.output")
        open(out, "w", encoding="utf-8").write("still running\n")
        self.ad._bg_note(self.sid, f"Command running in background with ID: b80tqyx2a. Output is being written to: {out}. You will be notified when it completes.")
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 1)
        open(out, "a", encoding="utf-8").write("\n[exited with code 0]\n")
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 0)

    def test_config_读取时按_output_killed_标记对账(self):
        out = os.path.join(self.tmp.name, "b80tqyx2a.output")
        open(out, "w", encoding="utf-8").write("\n")
        self.ad._bg_note(self.sid, f"Command running in background with ID: b80tqyx2a. Output is being written to: {out}. You will be notified when it completes.")
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 1)
        open(out, "a", encoding="utf-8").write("\n[killed]\n")
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 0)


class Chip(unittest.TestCase):
    def test_芯片挨着子代理那枚(self):
        i = CHAT.index('class="hs-agent"')
        j = CHAT.index('class="hs-bash"')
        self.assertLess(i, j)
        self.assertLess(j - i, 320, "两枚芯片应当相邻")

    def test_只报一个数(self):
        self.assertIn('DeskI18n.t("agent.scriptsRunning", { count: n })', CHAT)

    def test_没有在跑就收起(self):
        self.assertIn("el.hidden = !n;", CHAT)

    def test_接了_sse(self):
        self.assertIn('if (type === "session.bgtasks")', CHAT)

    def test_刷新后从_config_拿回来(self):
        """SSE 只在起止各发一帧,刷新之后不从 /config 取就一刷归零。"""
        self.assertIn("v.bgtasks = cf.bgtasks", CHAT)
        ui = _srcmap.ui_server_src()
        self.assertIn('"bgtasks": (ad.bg_tasks(session_id)', ui)

    def test_点不开(self):
        """命令的产出在它自己的输出文件里,菜单里摆一行点不开的命令名会让人以为点得开。"""
        i = CHAT.index("function hsBashPaint")
        self.assertNotIn("onclick", CHAT[i:i + 700])
        self.assertIn(".hs-page .hs-bash { ", CSS)
        self.assertIn("cursor: default", CSS[CSS.index(".hs-page .hs-bash {"):][:260])

    def test_点在呼吸(self):
        """没有箭头也没有菜单,静态一行字看不出还在跑。"""
        seg = CSS[CSS.index(".hs-page .hs-bash-d"):][:220]
        self.assertIn("hs-breathe", seg)

    def test_运行时轮询_config_以便清除_stale(self):
        self.assertIn("function hsBashWatch", CHAT)
        seg = CHAT[CHAT.index("function hsBashWatch"):][:500]
        self.assertIn("hsEngineSync(v)", seg)
        self.assertIn("bashTick", seg)


if __name__ == "__main__":
    unittest.main()


class LedgerFollowsTheProcess(unittest.TestCase):
    """后台命令活在引擎的常驻进程里,进程没了台账就该跟着清(2026-08-21)。

    销号原来只认 `system/task_notification` 那一帧,而那帧要等下一次调用模型才送达。
    命令跑完之后没人再跟这段会话说话、或者进程被回收/中止,通知就永远不来,输入框上那句
    「N 个命令运行中」一直挂着——用户实见:后端台账已空,界面还留着一个 1。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []

        class Bus:
            def publish(_, e):
                self.events.append(e)

        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, Bus())
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        ea._PERSIST.pop(self.sid, None)
        self.tmp.cleanup()

    def _fake_persist(self):
        class _P:
            def kill(self):
                pass

        ea._PERSIST[self.sid] = type("E", (), {"proc": _P()})()

    def test_进程回收后台账清空(self):
        self.ad._bg_note(self.sid, "Command running in background with ID: bax1. out: /tmp/a")
        self.ad._bg_note(self.sid, "Command running in background with ID: bax2. out: /tmp/b")
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 2)
        self._fake_persist()
        ea._persist_drop(self.sid)
        self.assertEqual(self.ad.bg_tasks(self.sid)["running"], 0)

    def test_清空要发一帧让界面跟上(self):
        self.ad._bg_note(self.sid, "Command running in background with ID: bax1. out: /tmp/a")
        self.events.clear()
        self._fake_persist()
        ea._persist_drop(self.sid)
        last = [e for e in self.events if e["type"] == "session.bgtasks"]
        self.assertTrue(last, "清空之后要广播,否则界面上的数字不会动")
        self.assertEqual(last[-1]["properties"]["running"], 0)

    def test_空账不空发(self):
        self.events.clear()
        self._fake_persist()
        ea._persist_drop(self.sid)
        self.assertEqual([e for e in self.events if e["type"] == "session.bgtasks"], [],
                         "四个适配器都会被遍历到,没有台账的那些不该各广播一遍")

    def test_回合收尾补发一次对账(self):
        """对账先于兼容 idle，控制面的 ready 最后释放下一轮。"""
        src = _srcmap.engine_adapters_src()
        bg = src.index("self._bg_publish(sid)\n        # idle 留给旧页面")
        idle = src.index('self.bus.publish({"type": "session.idle"', bg)
        ready = src.index("self._turn_state_publish(sid, ctx, self.TURN_READY)", idle)
        self.assertLess(bg, idle)
        self.assertLess(idle, ready)
