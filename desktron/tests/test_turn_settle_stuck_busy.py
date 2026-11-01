"""中断之后界面停在「处理中」,只能刷新——收尾这条路上的两道闸。

2026-09-05 用户实见:摁了 esc,回合确实中断了,状态一直显示处理中。后端那侧是好的
(刷新就正常),丢的是前端这一侧的收尾:

1. 撤回(Esc 打在还没出内容的那一轮上)记的是一个只增不减的计数器 recallIdle。撤回的
   那一轮未必等得到 ready——按得够快时后端那一轮已经收尾,abort 找不到 live ctx,
   一帧状态都不发。留下来的那个 1 会把后面某一轮真正的收尾吞掉。
2. 被吞掉之后本该由自愈探测(hsBusyVerify 轮询 /state)纠正,但会话空闲时 /state 报的
   generation 是上一轮的,于是那份 ready 被去重闸当成重复的丢掉——最后一条纠正通道
   也关着,只能靠人刷新。

这里把这两道闸放进 node 真跑一遍,不做正则锚定:要断言的是状态机的行为。
"""
import json
import os
import re
import shutil
import subprocess
import unittest

FRONTEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend"))

HARNESS = """
// 收尾会牵动一大片界面动作,这里只关心状态机,全部打桩。
const calls = [];
const stub = (n) => (...a) => { calls.push(n); };
const hsFlush = stub("flush"), hsFinalize = stub("finalize"), hsIndex = stub("index");
const hsAskClose = stub("askClose"), hsSeen = stub("seen"), hsReconcile = stub("reconcile");
const hsReconcileNow = () => Promise.resolve();
const hsGoalSync = stub("goal"), hsAgentsSync = stub("agents"), hsTasksSync = stub("tasks");
const hsEngineSync = stub("engine"), hsQFlush = stub("qflush");
const hsBusy = (v, on) => { v.busy = !!on; calls.push("busy:" + (on ? 1 : 0)); };
const hsStatus = (v, kind, text) => { v.status = kind + ":" + text; calls.push("status:" + text); };
const hsTurnErrorNotice = stub("turnError");
const hsProcFailure = stub("procFailure");

SRC

const v = VIEW;
for (const p of EVENTS) hsApply(v, "session.state", p);
console.log(JSON.stringify({busy: !!v.busy, status: v.status || "",
                            recallIdle: v.recallIdle || 0, calls}));
"""

# hsApply 的 session.state 那一支单独抄一份:整个 hsApply 牵进来的桩太多,
# 而这条测试要看的就是它到 hsTurnReady 的那一跳。
DISPATCH = """
function hsApply(v, type, p) {
  if (type !== "session.state") return;
  if (!hsStateTake(v, p)) return;
  if (p.state === "ready") { hsTurnReady(v, p); return; }
  v.turnReady = false;
  if (p.state === "running") { if (!v.busy) { hsBusy(v, true); hsStatus(v, "working", "处理中"); } }
  else if (p.state === "cancelling") { v.interrupted = true; v.interruptDrop = true; }
}
"""


def _state_machine():
    with open(os.path.join(FRONTEND, "app-chat-core.js"), encoding="utf-8") as f:
        src = f.read()
    a = src.index("function hsStateGeneration(p) {")
    b = src.index("async function hsStateSync(")
    return src[a:b] + DISPATCH


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class 收尾(unittest.TestCase):
    def run_js(self, view, events):
        js = (HARNESS.replace("SRC", _state_machine())
              .replace("VIEW", json.dumps(view, ensure_ascii=False))
              .replace("EVENTS", json.dumps(events, ensure_ascii=False)))
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def 撤回态(self, gen):
        """hsInterrupt 撤回那一支跑完之后的视图形态:界面已归位,等这一代的 ready。"""
        return {"busy": False, "turnGeneration": gen, "recallGen": gen,
                "recallIdle": 1, "interruptDrop": True, "noDrain": True}

    def test_撤回那一轮的收尾不重画已中断(self):
        r = self.run_js(self.撤回态(10), [{"state": "ready", "generation": 10}])
        self.assertEqual(r["recallIdle"], 0)
        self.assertNotIn("status:已中断", r["calls"])
        self.assertFalse(r["busy"])

    def test_撤回的ready丢了也不吞掉下一轮(self):
        """核心症状。撤回那一代的 ready 从没到过,下一轮跑完时界面必须归位。"""
        v = self.撤回态(10)
        v["interruptDrop"] = False   # 人重编辑后发出下一句,hsDispatch 会把它清掉
        r = self.run_js(v, [
            {"state": "running", "generation": 11},
            {"state": "ready", "generation": 11},
        ])
        self.assertFalse(r["busy"], "下一轮的收尾被撤回计数器吞了")
        self.assertEqual(r["status"], "done:已完成")

    def test_仍显示为忙时重复的ready照样收尾(self):
        """自愈探测拿回来的就是这样一份重复的 ready(空闲时 /state 报上一轮的 generation)。"""
        r = self.run_js({"busy": True, "turnGeneration": 12, "readyGeneration": 12,
                         "settledGeneration": 12},
                        [{"state": "ready", "generation": 12}])
        self.assertFalse(r["busy"])
        self.assertEqual(r["status"], "done:已完成")

    def test_失败后的ready保持已停止而不是已完成(self):
        r = self.run_js({"busy": True, "turnGeneration": 14},
                        [{"state": "ready", "generation": 14, "failed": True}])
        self.assertFalse(r["busy"])
        self.assertEqual(r["status"], "error:已停止")
        self.assertNotIn("status:已完成", r["calls"])

    def test_已经归位就不再重复收尾(self):
        """去重仍要成立,否则每次探测都把收尾的装饰重画一遍。"""
        r = self.run_js({"busy": False, "turnReady": True, "turnGeneration": 12, "readyGeneration": 12,
                         "settledGeneration": 12},
                        [{"state": "ready", "generation": 12}])
        self.assertEqual(r["calls"], [])

    def test_旧回合迟到的ready不解锁新回合(self):
        """generation 栅栏照旧:上一轮的收尾不能把正在跑的这一轮关掉。"""
        r = self.run_js({"busy": True, "turnGeneration": 13},
                        [{"state": "ready", "generation": 12}])
        self.assertTrue(r["busy"])


if __name__ == "__main__":
    unittest.main()
