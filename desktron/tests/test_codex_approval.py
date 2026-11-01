"""codex 家族在 GUI 里的审批回路。

app-server 遇到沙箱外的动作会发一条服务端请求等人拍板。此前 GUI 这侧没有回路,一律当场
拒绝,并把 approvalPolicy 压成 never——工作台里的 codex 因此只会失败,人连
「要不要放行」都看不到,而终端那条路是能问的(2026-08-16 用户提出)。

现在接进 claude 那套现成的回路:同一个 permission.asked 事件、同一个
/session/<id>/permission 接口。这里盯三件事——问句发得出去、答复回得到服务端、
没人答时到点自动拒绝。
"""
import copy
import os
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


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, e):
        self.events.append(e)


class FakeConn:
    """只认应答的假连接:审批的回写是从另一个线程发的,这里把它接住看内容。"""

    def __init__(self):
        self.answered = []

    def answer_approval(self, msg_id, result):
        self.answered.append((msg_id, result))


ASK = {"jsonrpc": "2.0", "id": 42, "method": "item/commandExecution/requestApproval",
       "params": {"reason": "是否允许在 /tmp/x 中创建 a.txt？",
                  "command": "/bin/bash -lc 'touch /tmp/x/a.txt'",
                  "cwd": "/tmp/x",
                  "availableDecisions": ["accept",
                                         {"acceptWithExecpolicyAmendment": {}}, "cancel"]}}
PERMISSIONS_ASK = {
    "jsonrpc": "2.0", "id": 43, "method": "item/permissions/requestApproval",
    "params": {
        "threadId": "thr-1", "turnId": "turn-1", "itemId": "call-1",
        "cwd": "/work/repo", "reason": "需要读取配置并写入构建目录",
        "permissions": {
            "network": {"enabled": True},
            "fileSystem": {"read": ["/work/repo/config"],
                           "write": ["/work/repo/dist"]},
        },
    },
}

PROFILE_ASK = {
    "jsonrpc": "2.0", "id": 43, "method": "item/permissions/requestApproval",
    "params": {
        "threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1",
        "startedAtMs": 1788764381000,
        "reason": "需要更新工作区之外的源码仓库",
        "cwd": "/tmp/desk-workspace",
        "permissions": {
            "fileSystem": {
                "read": ["/Users/me/src/desktron"],
                "write": ["/Users/me/src/desktron"],
            },
            "network": {"enabled": True},
        },
    },
}


class CodexApproval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus()
        self.ad = ea.CodexAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.conn = FakeConn()

    def tearDown(self):
        self.tmp.cleanup()

    def _ask(self):
        took = self.ad._approval_ask("sess-1", ASK, self.conn)
        asked = [e for e in self.bus.events if e["type"] == "permission.asked"]
        return took, asked

    def test_审批请求转成界面问句(self):
        took, asked = self._ask()
        self.assertTrue(took)                       # 接管了,连接层不该再自己拒绝
        self.assertEqual(len(asked), 1)
        p = asked[0]["properties"]
        self.assertEqual(p["sessionID"], "sess-1")
        self.assertEqual(p["tool"], "Bash")          # 借用 claude 那套工具名,前端已有译名
        self.assertIn("创建 a.txt", p["reason"])      # 服务端自己写好的那句话,直接给人看
        self.assertEqual(p["input"]["command"], ASK["params"]["command"])
        self.assertTrue(p["id"])

    def test_允许时回服务端认的字面量(self):
        _, asked = self._ask()
        rid = asked[0]["properties"]["id"]
        self.assertTrue(self.ad.respond_permission(rid, True))
        self.assertEqual(self.conn.answered, [(42, {"decision": "accept"})])

    def test_拒绝时同样按可选项挑(self):
        _, asked = self._ask()
        rid = asked[0]["properties"]["id"]
        self.assertTrue(self.ad.respond_permission(rid, False))
        self.assertEqual(self.conn.answered, [(42, {"decision": "cancel"})])

    def test_新版权限配置请求显示审批卡(self):
        self.assertTrue(self.ad._approval_ask("sess-1", PROFILE_ASK, self.conn))
        event = [e for e in self.bus.events if e["type"] == "permission.asked"][-1]
        props = event["properties"]
        self.assertEqual(props["tool"], "权限")
        self.assertEqual(props["input"]["cwd"], PROFILE_ASK["params"]["cwd"])
        self.assertEqual(props["input"]["permissions"],
                         PROFILE_ASK["params"]["permissions"])

    def test_新版权限配置只允许本次申请的范围(self):
        self.ad._approval_ask("sess-1", PROFILE_ASK, self.conn)
        rid = self.bus.events[-1]["properties"]["id"]
        self.assertTrue(self.ad.respond_permission(rid, True))
        self.assertEqual(self.conn.answered[-1], (43, {
            "permissions": PROFILE_ASK["params"]["permissions"], "scope": "turn"}))

    def test_新版权限配置拒绝时返回空权限(self):
        self.ad._approval_ask("sess-1", PROFILE_ASK, self.conn)
        rid = self.bus.events[-1]["properties"]["id"]
        self.assertTrue(self.ad.respond_permission(rid, False))
        self.assertEqual(self.conn.answered[-1],
                         (43, {"permissions": {}, "scope": "turn"}))

    def test_畸形权限配置仍展示并返回空权限(self):
        malformed = [
            None,
            "all",
            [],
            {"network": "all", "fileSystem": "all"},
            {"network": [], "fileSystem": {"read": "/tmp/a", "entries": "bad"}},
        ]
        for index, permissions in enumerate(malformed):
            with self.subTest(permissions=permissions):
                ask = copy.deepcopy(PROFILE_ASK)
                ask["id"] = 100 + index
                ask["params"]["permissions"] = permissions
                self.assertTrue(self.ad._approval_ask("sess-1", ask, self.conn))
                event = self.bus.events[-1]
                self.assertEqual(event["type"], "permission.asked")
                self.assertTrue(event["properties"]["input"]["command"])
                rid = event["properties"]["id"]
                self.assertTrue(self.ad.respond_permission(rid, True))
                self.assertEqual(self.conn.answered[-1],
                                 (100 + index,
                                  {"permissions": {}, "scope": "turn"}))

    def test_非对象_params_也会进入拒绝回路(self):
        ask = dict(PROFILE_ASK, id=110, params="bad")
        self.assertTrue(self.ad._approval_ask("sess-1", ask, self.conn))
        event = self.bus.events[-1]
        self.assertEqual(event["type"], "permission.asked")
        rid = event["properties"]["id"]
        self.assertTrue(self.ad.respond_permission(rid, True))
        self.assertEqual(self.conn.answered[-1],
                         (110, {"permissions": {}, "scope": "turn"}))

    def test_刷新后能恢复新版权限审批卡(self):
        self.ad._approval_ask("sess-1", PROFILE_ASK, self.conn)
        pending = self.ad.turn_state("sess-1")["asks"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["input"]["permissions"],
                         PROFILE_ASK["params"]["permissions"])
        rid = pending[0]["id"]
        self.ad.respond_permission(rid, False)
        self.assertEqual(self.ad.turn_state("sess-1")["asks"], [])

    def test_答过一次就不再回第二次(self):
        """兜底定时器与人的点击会撞在一起。同一条只应答一次,第二次静默作废。"""
        _, asked = self._ask()
        rid = asked[0]["properties"]["id"]
        self.assertTrue(self.ad.respond_permission(rid, True))
        self.assertFalse(self.ad.respond_permission(rid, False))
        self.assertEqual(len(self.conn.answered), 1)

    def test_旧协议走另一套枚举(self):
        """applyPatchApproval 的 ReviewDecision 是带内容的枚举,与 v2 那组字面量不是一回事。"""
        old = dict(ASK, id=7, method="applyPatchApproval")
        self.assertTrue(self.ad._approval_ask("sess-1", old, self.conn))
        rid = [e for e in self.bus.events
               if e["type"] == "permission.asked"][-1]["properties"]["id"]
        self.ad.respond_permission(rid, True)
        self.assertEqual(self.conn.answered[-1], (7, {"decision": {"approved": {}}}))

    def test_MCP授权显示卡片并回标准elicitation响应(self):
        ask = {"jsonrpc": "2.0", "id": 9, "method": "mcpServer/elicitation/request",
               "params": {"serverName": "node_repl", "mode": "form",
                          "message": "Run JavaScript", "requestedSchema": {"type": "object"}}}
        self.assertTrue(self.ad._approval_ask("sess-1", ask, self.conn))
        event = [e for e in self.bus.events if e["type"] == "permission.asked"][-1]
        self.assertEqual(event["properties"]["tool"], "MCP")
        self.assertEqual(event["properties"]["reason"], "Run JavaScript")
        self.assertEqual(event["properties"]["input"]["serverName"], "node_repl")
        self.assertTrue(self.ad.respond_permission(event["properties"]["id"], True))
        self.assertEqual(self.conn.answered[-1], (9, {"action": "accept"}))

    def test_额外权限请求显示用途与范围(self):
        self.assertTrue(self.ad._approval_ask("sess-1", PERMISSIONS_ASK, self.conn))
        event = [e for e in self.bus.events if e["type"] == "permission.asked"][-1]
        props = event["properties"]
        self.assertEqual(props["tool"], "权限")
        self.assertEqual(props["reason"], "需要读取配置并写入构建目录")
        summary = props["input"]["command"]
        self.assertIn("访问网络", summary)
        self.assertIn("读取/work/repo/config", summary)
        self.assertIn("写入/work/repo/dist", summary)
        self.assertIn("工作目录 /work/repo", summary)
        self.assertEqual(props["input"]["permissions"],
                         PERMISSIONS_ASK["params"]["permissions"])

    def test_额外权限允许只授权本轮(self):
        self.ad._approval_ask("sess-1", PERMISSIONS_ASK, self.conn)
        event = [e for e in self.bus.events if e["type"] == "permission.asked"][-1]
        self.assertTrue(self.ad.respond_permission(event["properties"]["id"], True))
        self.assertEqual(self.conn.answered[-1], (43, {
            "permissions": PERMISSIONS_ASK["params"]["permissions"],
            "scope": "turn",
        }))

    def test_额外权限拒绝与回合清理都返回空权限(self):
        self.ad._approval_ask("sess-1", PERMISSIONS_ASK, self.conn)
        event = [e for e in self.bus.events if e["type"] == "permission.asked"][-1]
        self.assertTrue(self.ad.respond_permission(event["properties"]["id"], False))
        self.assertEqual(self.conn.answered[-1],
                         (43, {"permissions": {}, "scope": "turn"}))

        ask = dict(PERMISSIONS_ASK, id=44)
        self.ad._approval_ask("sess-1", ask, self.conn)
        self.ad._clear_session_approvals("sess-1")
        self.assertEqual(self.conn.answered[-1],
                         (44, {"permissions": {}, "scope": "turn"}))
        self.assertEqual(self.ad._approvals, {})

    def test_回合结束清理未决MCP授权(self):
        ask = {"jsonrpc": "2.0", "id": 10, "method": "mcpServer/elicitation/request",
               "params": {"serverName": "node_repl", "mode": "form",
                          "message": "Run JavaScript", "requestedSchema": {"type": "object"}}}
        self.ad._approval_ask("sess-1", ask, self.conn)
        self.ad._clear_session_approvals("sess-1")
        self.assertEqual(self.conn.answered[-1], (10, {"action": "decline"}))
        self.assertEqual(self.ad._approvals, {})

    def test_回合结束清理未决权限配置请求(self):
        self.ad._approval_ask("sess-1", PROFILE_ASK, self.conn)
        self.ad._clear_session_approvals("sess-1")
        self.assertEqual(self.conn.answered[-1],
                         (43, {"permissions": {}, "scope": "turn"}))
        self.assertEqual(self.ad._approvals, {})

    def test_不认识的请求不接管(self):
        """接管失败要如实返回 False,连接层才会照旧按协议拒绝,不会把回合挂住。"""
        other = dict(ASK, method="item/tool/requestUserInput")
        self.assertFalse(self.ad._approval_ask("sess-1", other, self.conn))
        self.assertEqual([e for e in self.bus.events if e["type"] == "permission.asked"], [])

    def test_声明了有审批回路(self):
        """asks_permission 决定界面给不给授权入口,也决定 approvalPolicy 敢不敢开 on-request。"""
        self.assertTrue(self.ad.asks_permission)


if __name__ == "__main__":
    unittest.main()
