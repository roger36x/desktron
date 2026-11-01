"""后台任务与子代理的完成通知不进聊天区。

claude 在后台任务(Bash run_in_background)或子代理跑完时,往上下文里塞一条
<task-notification>:任务 id、tool-use-id、输出文件、状态、摘要,子代理那种还带上
整份返回正文。它在 transcript 里是一条普通的 type=user 条目,不带 isMeta,于是此前
那几道过滤都放它过去了——界面上出现一个谁也没说过的用户气泡,里面是几千字的尖括号
标签(2026-08-12 实见,紧跟在自动压缩那条分隔线之后)。

判据取记录里的 origin.kind:那是引擎自己写下的来路,不会误伤用户粘贴的尖括号内容。
样本照本机真实 rollout 的形状抄(claude-code 2.1.220),不是照文档编的。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv

CORE_JS = os.path.join(ROOT, "frontend", "app-core.js")

NOTIF_BODY = (
    "<task-notification>\n"
    "<task-id>ab948002b9c5bc293</task-id>\n"
    "<tool-use-id>toolu_013usF1XvVLCMJVNHMKtTLtL</tool-use-id>\n"
    "<output-file>/private/tmp/claude-501/tasks/ab948002b9c5bc293.output</output-file>\n"
    "<status>completed</status>\n"
    "<summary>Agent \"Explore\" finished</summary>\n"
    "<note>结果已经在别处呈现</note>\n"
    "<result>改了 frontend/app-chat.js 的三处……</result>\n"
    "</task-notification>"
)


class Bus:
    def publish(self, ev):
        pass


def _user(uuid, text, **extra):
    o = {"type": "user", "uuid": uuid, "timestamp": "2026-08-12T09:00:00.000Z",
         "message": {"role": "user", "content": text}}
    o.update(extra)
    return o


def _assistant(uuid, text):
    return {"type": "assistant", "uuid": uuid, "timestamp": "2026-08-12T09:00:01.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class TaskNotificationNotAUserTurn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, Bus())
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rows):
        path = os.path.join(self.tmp.name, "transcript.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return path

    def _turns(self, rows):
        """重建之后留下的用户发言正文,按顺序。

        自续轮那条分割线在契约里也是 role=user(同压缩行、目标行),但它不是谁打的字,
        正文是引擎读完报告之后自己说的话——按「真人发言」取的地方都要把它排掉,
        判据同 _turn_start。"""
        msgs = self.ad._group_transcript(self._write(rows), self.sid, "esid-1")
        return ["".join(q.get("text") or "" for q in (m.get("parts") or [])
                        if q.get("type") == "text")
                for m in msgs if self.ad._turn_start(m)]

    def test_按_origin_认出的通知整条不入流(self):
        got = self._turns([
            _user("u1", "先看一眼适配器"),
            _assistant("a1", "好的"),
            _user("n1", NOTIF_BODY, origin={"kind": "task-notification"},
                  promptSource="system"),
            _assistant("a2", "子代理回来了"),
            _user("u2", "继续"),
        ])
        self.assertEqual(got, ["先看一眼适配器", "继续"])

    def test_通知标签不显示_报告作为工具来源保留(self):
        """通知的正文(标签、输出文件、摘要)一个字都不进消息。

        任务 id 是例外,而且是有意的:它随自续轮那条分割线以 info.aside.task_id 留下,
        用来对到具体是哪个子代理交回(见 test_claude_aside_replay)。那是一个结构化字段,
        不是铺进聊天区的正文。"""
        msgs = self.ad._group_transcript(self._write([
            _user("n1", NOTIF_BODY, origin={"kind": "task-notification"},
                  promptSource="sdk"),
            _assistant("a1", "收到"),
        ]), self.sid, "esid-1")
        dumped = json.dumps(msgs, ensure_ascii=False)
        cards = [p for m in msgs for p in m["parts"] if p["type"] == "execution-result"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["role"], "tool")
        self.assertEqual(cards[0]["text"], "改了 frontend/app-chat.js 的三处……")
        self.assertNotIn("task-id", dumped)
        self.assertNotIn("output-file", dumped)
        self.assertNotIn("Agent \"Explore\" finished", dumped)
        for m in msgs:
            for q in m.get("parts") or []:
                self.assertNotIn("ab948002b9c5bc293", q.get("text") or "")

    def test_不写_origin_的旧版本按正文前缀兜底(self):
        got = self._turns([_user("n1", NOTIF_BODY), _user("u1", "继续")])
        self.assertEqual(got, ["继续"])

    def test_块形态的正文同样认(self):
        got = self._turns([
            _user("n1", [{"type": "text", "text": NOTIF_BODY}],
                  origin={"kind": "task-notification"}),
            _user("u1", "继续"),
        ])
        self.assertEqual(got, ["继续"])

    def test_不误伤用户自己粘的尖括号内容(self):
        """用户贴一段带标签的正文,那是他打的字,照旧渲染。"""
        paste = "帮我看看这段:\n<task-notification>\n<status>completed</status>\n</task-notification>"
        got = self._turns([_user("u1", paste), _user("u2", "<div>也是我写的</div>")])
        self.assertEqual(got, [paste, "<div>也是我写的</div>"])

    def test_队列里的通知与_attachment_形态也不入流(self):
        """同一条通知在记录里还会以 queue-operation 与 attachment 各出现一次。"""
        got = self._turns([
            {"type": "queue-operation", "operation": "enqueue", "content": NOTIF_BODY},
            {"type": "attachment", "uuid": "at1", "timestamp": "2026-08-12T09:00:00.000Z",
             "attachment": {"type": "queued_command", "prompt": NOTIF_BODY,
                            "commandMode": "task-notification"}},
            _user("u1", "继续"),
        ])
        self.assertEqual(got, ["继续"])

    def test_回退的序号两侧口径一致(self):
        """_human_turns 与重建按「第几条真人发言」对齐,一侧多滤一条就整体错位。"""
        rows = [
            _user("u1", "第一问"),
            _assistant("a1", "答"),
            _user("n1", NOTIF_BODY, origin={"kind": "task-notification"}),
            _assistant("a2", "答"),
            _user("u2", "第二问"),
        ]
        path = self._write(rows)
        turns = self.ad._human_turns(path)
        self.assertEqual([t for _, t in turns], ["第一问", "第二问"])
        self.assertEqual([u for u, _ in turns], ["u1", "u2"])
        self.assertEqual(self._turns(rows), [t for _, t in turns])


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class FrontendFallback(unittest.TestCase):
    """存量存储里已经落下的那些通知,前端这道兜底照旧挡住。"""

    def _run(self, cases):
        with open(CORE_JS, encoding="utf-8") as f:
            src = f.read()
        parts = [re.search(r"^const INJECT_MARKERS = .*$", src, re.M).group(0),
                 re.search(r"^const LOCAL_CMD_OUT = [\s\S]*?\];$", src, re.M).group(0),
                 re.search(r"\nfunction isInjected\(.*?\n}\n", src, re.S).group(0)]
        js = ("\n".join(parts) + "\nconsole.log(JSON.stringify("
              + json.dumps(cases, ensure_ascii=False) + ".map(isInjected)));")
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_通知算注入_用户正文不算(self):
        got = self._run([NOTIF_BODY,
                         "\n" + NOTIF_BODY,
                         "帮我看看这段 <task-notification> 是什么",
                         "正常的一句话"])
        self.assertEqual(got, [True, True, False, False])


if __name__ == "__main__":
    unittest.main()
