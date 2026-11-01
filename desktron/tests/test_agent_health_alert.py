"""系统 Agent 停机要外显。

这些 Agent 熔断后是「按设计停下等人工」——状态一直读得到,只是没人会主动去开 Agents 页。
2026-08-17 旧记忆蒸馏链停了两周、飞书 im 源坏了 11 天，故所有停机都要外显。
两件都是等到用户觉得不对劲才被发现的。状态页读得到不等于有人读。

判据只取记忆引擎停机与 Desk 后端旧代码；已下线的 Timeline 不再参与。不含「跑得慢」——
红点常亮就等于没有红点。
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FRONTEND = os.path.join(ROOT, "frontend")

HARNESS = r"""
let painted = null;
const document = { querySelector: () => ({
  classList: { toggle: (_c, on) => { painted = painted || {}; painted.on = !!on; } },
  set title(v) { painted = painted || {}; painted.why = v; },
  removeAttribute: () => { painted = painted || {}; painted.why = ""; },
}) };
const api = async () => (STATUS);   // 括号必须留:箭头函数直接返回对象字面量会被当成函数体
const setInterval = () => 0;
// 首拍在本机立刻取、在远端(手机经隧道)推迟 2 秒让开启动窗,见 app-init 里的判据。
// 沙箱按本机跑,这样断言的仍是判据本身,不必等定时器;远端那一支由下面单独一条验。
const isLocalView = () => LOCAL_VIEW;
const setTimeout_ = setTimeout;
POLL_SRC
setTimeout(() => console.log(JSON.stringify(painted)), 0);
"""


def _slice(path, head):
    with open(os.path.join(FRONTEND, path), encoding="utf-8") as f:
        src = f.read()
    m = re.search(re.escape(head) + r".*?\n\}\)\(\);\n", src, re.S)
    assert m, head
    return m.group(0)


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class AgentHealthAlert(unittest.TestCase):
    def _run(self, status, local=True):
        js = (HARNESS
              .replace("POLL_SRC", _slice("app-init.js", "(function agentHealthPoll("))
              .replace("LOCAL_VIEW", "true" if local else "false")
              .replace("STATUS", json.dumps(status, ensure_ascii=False)))
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_全都正常时不点红点(self):
        r = self._run({"memory": {"project_memory": {"stopped": False}},
                       "lark": {"sources": {"im": {"degraded": False}}, "stale_code": False}})
        self.assertFalse(r["on"])

    def test_记忆引擎停机(self):
        r = self._run({"memory": {"project_memory": {"stopped": True, "fails": 3}}, "lark": {}})
        self.assertTrue(r["on"])
        self.assertIn("记忆引擎已停机", r["why"])


    def test_进程跑着旧代码(self):
        """Desk 后端修复落盘但尚未重启时要外显。"""
        r = self._run({"memory": {}, "desk": {"stale_code": True}})
        self.assertTrue(r["on"])
        self.assertIn("旧代码", r["why"])

    def test_多项同时异常都列出来(self):
        r = self._run({"memory": {"project_memory": {"stopped": True}},
                       "desk": {"stale_code": True}})
        self.assertTrue(r["on"])
        for frag in ("记忆引擎已停机", "旧代码"):
            self.assertIn(frag, r["why"])

    def test_retired_timeline_status_is_ignored(self):
        r = self._run({"memory": {}, "lark": {"sources": {"im": {"degraded": True}},
                        "distill": {"stopped": True}, "stale_code": True}})
        self.assertFalse(r["on"])

    def test_取不到状态时不报警(self):
        """后端一抖就红会让红点失去意义;取不到就维持原样。"""
        js = (HARNESS
              .replace("POLL_SRC", _slice("app-init.js", "(function agentHealthPoll("))
              .replace("const api = async () => (STATUS);   // 括号必须留:箭头函数直接返回对象字面量会被当成函数体",
                       "const api = async () => { throw new Error('down'); };")
              .replace("LOCAL_VIEW", "true"))
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "null")   # paint 根本没被调用


class ArrangementAlertPresentation(unittest.TestCase):
    def test_安排入口不再绘制健康状态点(self):
        with open(os.path.join(FRONTEND, "style.css"), encoding="utf-8") as f:
            css = f.read()
        self.assertNotIn(".side-fx.has-alert::after", css)
        self.assertNotIn("#fx-tasks.has-alert::after", css)



if __name__ == "__main__":
    unittest.main()

    def test_远端首拍不挤启动窗(self):
        """手机经隧道那一侧,这一拍要让开整页重载的启动窗。

        它问四处状态,本机实测 113~205ms,经隧道还要再加一次跨区往返(中位 242ms);
        而它报的是以小时计的状态,迟两秒没有代价。2026-09-02 排查手机端切引擎变慢时
        收敛:切引擎是一次 location.reload,启动窗里每多一趟都直接摊在人等待的时间上。
        """
        r = self._run({"memory": {"project_memory": {"stopped": True}}, "lark": {}}, local=False)
        self.assertIsNone(r)   # 首拍被推迟,同步窗内没有任何绘制
