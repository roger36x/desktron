"""手机从后台回来要能自愈(2026-09-03 用户实见)。

permission.asked 只在发生那一刻推一次,服务端不重放。手机切后台、锁屏、蜂窝与 Wi-Fi
切换都会断开 SSE,那一帧就此丢失——卡片再也不出现,而回合还在等人答。

(会话恢复那条链路的对账见 test_frontend_resume_recovery.py,同一个根源、同一条修法。)

修法同一条:不依赖那一次推送,回前台时主动对账。审批面早先就是这么做的(轮询待批),
所以它不受影响——这里把同一个道理用到会话恢复与权限卡上。
"""
import os
import re
import sys
import threading
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
sys.argv = ["test", os.path.join(ROOT, "frontend"), "0", "0", ROOT]

CORE = open(os.path.join(ROOT, "frontend", "app-chat-core.js"), encoding="utf-8").read()
REC = open(os.path.join(ROOT, "frontend", "app-chat-reconcile.js"), encoding="utf-8").read()


def _fn(src, name):
    i = src.index("function %s(" % name)
    return src[i:src.index("\n}", i)]


class 权限卡可补齐(unittest.TestCase):
    def test_后端按会话报待答(self):
        from engine_adapters.claude import ClaudeAdapter
        a = ClaudeAdapter.__new__(ClaudeAdapter)
        a._plock = threading.Lock()
        a._pending_ask_meta = {
            "r1": {"id": "r1", "sessionID": "s1", "tool": "Bash", "input": {"command": "ls"}},
            "r2": {"id": "r2", "sessionID": "s2", "tool": "Edit", "input": {}},
        }
        self.assertEqual([m["id"] for m in a._pending_ask("s1")], ["r1"])
        self.assertEqual([m["id"] for m in a._pending_ask("s2")], ["r2"])

    def test_内容够重建那张卡(self):
        """光有 input 拼不出卡片:hsAsk 要 id / tool,落位还要 sessionID。"""
        from engine_adapters.claude import ClaudeAdapter
        a = ClaudeAdapter.__new__(ClaudeAdapter)
        a._plock = threading.Lock()
        a._pending_ask_meta = {"r1": {"id": "r1", "sessionID": "s1", "tool": "Bash", "input": {}}}
        self.assertGreaterEqual(set(a._pending_ask("s1")[0]), {"id", "sessionID", "tool", "input"})

    def test_答过就不再报(self):
        """不清的话回前台会把已经答过的又画一遍。"""
        src = open(os.path.join(ROOT, "desktron", "lib", "engine_adapters", "claude.py"),
                   encoding="utf-8").read()
        self.assertIn("_pending_ask_meta.pop(request_id", src)

    def test_两个适配器实例不共享待答状态(self):
        from engine_adapters.claude import ClaudeAdapter
        a, b = ClaudeAdapter.__new__(ClaudeAdapter), ClaudeAdapter.__new__(ClaudeAdapter)
        for item in (a, b):
            item._plock = threading.Lock(); item._pending_ask_meta = {}; item._pending_input = {}
        a._pending_ask_meta['one'] = {'id':'one','sessionID':'s'}
        self.assertEqual([x['id'] for x in a._pending_ask('s')], ['one'])
        self.assertEqual(b._pending_ask('s'), [])

    def test_并发快照与删除不会迭代崩溃(self):
        from engine_adapters.claude import ClaudeAdapter
        a = ClaudeAdapter.__new__(ClaudeAdapter)
        a._plock = threading.Lock(); a._pending_ask_meta = {}; a._pending_input = {}
        errors = []
        def mutate():
            try:
                for i in range(1000):
                    with a._plock:
                        a._pending_ask_meta[str(i)] = {'id':str(i),'sessionID':'s'}
                        a._pending_ask_meta.pop(str(i - 2), None)
            except Exception as exc: errors.append(exc)
        worker = threading.Thread(target=mutate); worker.start()
        while worker.is_alive():
            try: a._pending_ask('s')
            except Exception as exc: errors.append(exc); break
        worker.join()
        self.assertEqual(errors, [])

    def test_挂进_state_而不是新端点(self):
        """前端恢复时本来就查 /state,不必另加端点与轮询。"""
        store = open(os.path.join(ROOT, "desktron", "lib", "engine_adapters", "cli_store.py"),
                     encoding="utf-8").read()
        seg = store[store.index("def turn_state"):]
        seg = seg[:seg.index("\n    def ", 10)]
        self.assertIn('out["asks"]', seg)

    def test_基类给空实现(self):
        """只有走 control_request 的引擎才有待答概念,别的引擎不该因此报错。"""
        from engine_adapters.cli_store import SessionStoreMixin
        self.assertEqual(SessionStoreMixin._pending_ask(None, "any"), [])

    def test_前端补齐但不重画已有的(self):
        """重画会把人选了一半的选项清掉。"""
        seg = _fn(CORE, "hsAskStateSync")
        self.assertIn("v.ask && asks.some(p => p.id === v.ask.id)", seg)
        self.assertLess(seg.index("asks.some"), seg.index("hsAskClose(v)"))
        self.assertIn("hsAsk(v, p)", seg)
        self.assertIn("hsAskStateSync(v, st)", _fn(CORE, "hsStateSync"))


class 消失现象留证据(unittest.TestCase):
    def test_重建时记下字数对比(self):
        """两道守卫单看都成立,推演不出能擦掉内容的路径。与其继续猜,
        不如让复现时留下实据。"""
        self.assertIn("[reconcile] 重建", REC)
        for k in ("dbChars", "domChars", "dup"):
            self.assertIn(k, REC[REC.index("[reconcile] 重建") - 200:REC.index("[reconcile] 重建") + 400])

    def test_空页不记(self):
        """空页重建是正常开局,记了只是噪声。"""
        i = REC.index("[reconcile] 重建")
        self.assertIn("domChars > 0", REC[max(0, i - 300):i])


if __name__ == "__main__":
    unittest.main(argv=["test_resume_recovery"])
