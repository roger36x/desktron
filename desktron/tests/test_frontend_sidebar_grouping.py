"""侧栏会话入口：有工作流归属或引用的会话不重复出现在聊天组。

聊天组只收纳无工作流关联的记录；会话原始归属保留。两处都按当前引擎隔离。
函数从源文件里抽出来跑，喂同一份会话数据。
"""
import json
import os
import re
import shutil
import subprocess
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FRONTEND = os.path.join(ROOT, "frontend")

HARNESS = r"""
const DESKTOP = "_desktop";
const SESS_VISIBLE = 5;
const curEngine = "claude";
const shepherdMode = false;
let visibilityChecks = 0;
function sessionVisibleInCurrentMode(row) {
  visibilityChecks++;
  if (!row || row.task) return false;
  if (shepherdMode) return row.shepherd_mode === true;
  return row.shepherd_mode !== true && (!row.engine || row.engine === curEngine);
}
const _moreOpen = new Set();
const wbState = { sessionOrder: SEED };
const _termsList = TERMS;
let _sessionBucketsCache = null;
function saveState() {}   // 次序钉住会落盘,这里只需不报错
APPLY_ORDER_SRC

SIDEBAR_SRC

console.log(JSON.stringify({
  chat: looseSessions().map((t) => t.title),
  wf: visibleSessionsFor(GROUP).all.map((t) => t.title),
  count: domSessionCount(GROUP),
  visibilityChecks,
  order: wbState.sessionOrder,
}));
"""


def _slice(path, head):
    src = _srcmap.frontend_src(path)
    m = re.search(re.escape(head) + r".*?\n}\n", src, re.S)
    assert m, head
    return m.group(0)


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class SidebarGrouping(unittest.TestCase):
    TERMS = [
        {"id": "a", "title": "聊天甲", "engine": "claude", "domain": "_desktop", "alive": True},
        {"id": "b", "title": "聊天乙", "engine": "claude", "domain": "_desktop", "alive": True},
        {"id": "c", "title": "被引用丙", "engine": "claude", "domain": "_desktop",
         "refs": ["wf-a"], "alive": True},
        {"id": "d", "title": "工作流丁", "engine": "claude", "domain": "wf-a", "alive": True},
        {"id": "e", "title": "别的引擎", "engine": "codex", "domain": "_desktop", "alive": True},
        {"id": "f", "title": "休眠戊", "engine": "claude", "domain": "wf-a", "alive": False},
    ]

    @classmethod
    def _run(cls, terms, seed=None):
        js = (HARNESS
              .replace("APPLY_ORDER_SRC", _slice("app-rail.js", "function applyOrder("))
              .replace("SEED", json.dumps(seed or {}))
              .replace("SIDEBAR_SRC",
                       # 次序钉住的两个助手:visibleSessionsFor 调它们,不切进来会 ReferenceError
                       _slice("app-rail.js", "function termOrderId(")
                       + "\n" + _slice("app-rail.js", "function invalidateSessionBuckets(")
                       + "\n" + _slice("app-rail.js", "function sessionBuckets(")
                       + "\n" + _slice("app-rail.js", "function domSessionCount(")
                       + "\n" + _slice("app-rail.js", "function _pinSessionOrder(")
                       + "\nlet _pinTimer = 0;\nconst _pinDirty = new Set();\n"
                       + _slice("app-rail.js", "function _pinSave(")
                       + "\n" + _slice("app-rail.js", "function looseSessions(")
                       + "\n" + _slice("app-rail.js", "function visibleSessionsFor("))
              .replace("TERMS", json.dumps(terms, ensure_ascii=False))
              .replace("GROUP", json.dumps("wf-a")))
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        if out.returncode != 0:
            raise AssertionError(out.stderr)
        return json.loads(out.stdout)

    @classmethod
    def setUpClass(cls):
        cls.r = cls._run(cls.TERMS)

    def test_次序钉住_不随后端重排而变(self):
        """后端那份列表按最后落盘时间倒序给,谁刚回复谁排在前。侧栏不该跟着动:
        读到一半时一段会话自己跳到最上面,位置就不可依赖了。首次见到即钉进 sessionOrder,
        之后只有长按拖拽能改(2026-08-16 按用户要求改成只保留手工排序)。"""
        first = self._run(self.TERMS)
        # 后端把「工作流丁」挪到最前(它刚回复),侧栏次序应当不变
        reordered = [t for t in self.TERMS if t["id"] == "d"] + \
                    [t for t in self.TERMS if t["id"] != "d"]
        second = self._run(reordered, seed=first["order"])
        self.assertEqual(second["wf"], first["wf"])

    def test_聊天组也不随后端重排而变(self):
        first = self._run(self.TERMS)
        reordered = [self.TERMS[1], self.TERMS[0], *self.TERMS[2:]]
        second = self._run(reordered, seed=first["order"])
        self.assertEqual(second["chat"], first["chat"])

    def test_运行态临时_id_不改变位置(self):
        first = self._run(self.TERMS)
        active = [dict(t) for t in self.TERMS]
        row = next(t for t in active if t["id"] == "d")
        row["id"] = "t9"
        row["resume_id"] = "d"
        second = self._run(active, seed=first["order"])
        self.assertEqual(second["wf"], first["wf"])
        self.assertNotIn("t9", second["order"]["wf-a"])

    def test_活会话不再被提到前面(self):
        """按 alive 分组也是一种自动排序:同一段会话在起停之间会上下跳。
        「在跑的看得见」交给状态点(带晕、会呼吸),不靠位置表达。"""
        r = self._run(self.TERMS)
        self.assertEqual(r["wf"], ["被引用丙", "工作流丁", "休眠戊"])

    def test_工作流归属与引用会话不出现在聊天组(self):
        self.assertIn("被引用丙", self.r["wf"])
        self.assertNotIn("被引用丙", self.r["chat"])
        self.assertNotIn("工作流丁", self.r["chat"])
        self.assertNotIn("休眠戊", self.r["chat"])

    def test_工作流与聊天分组互斥且不丢记录(self):
        self.assertFalse(set(self.r["wf"]) & set(self.r["chat"]))
        self.assertEqual(set(self.r["wf"]) | set(self.r["chat"]),
                         {t['title'] for t in self.TERMS if t['engine'] == 'claude'})

    def test_聊天渲染按记录真实归属打开(self):
        rail = _srcmap.rail_src()
        self.assertIn("container.appendChild(termRow(s, s.domain || DESKTOP))", rail)

    def test_快捷切换跳过工作流与聊天中的重复行(self):
        cycle = _slice("app-assets-term-ui.js", "function cycleTerminal(")
        js = r'''
let activeTermId = "adp-a", clicked = "";
function row(sid, sessionId) { return { dataset:{sid, sessionId}, title:"", click(){clicked=sessionId}, scrollIntoView(){} }; }
const rows = [row("engine-a", "adp-a"), row("engine-b", "adp-b"), row("engine-a", "adp-a")];
const document = {querySelectorAll(){return rows}};
function termRowMatchesId(row, sid) { return [row.dataset.sid, row.dataset.sessionId].includes(sid); }
CYCLE
cycleTerminal(1);
console.log(clicked);
'''.replace("CYCLE", cycle)
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "adp-b")

    def test_都按当前引擎隔离(self):
        self.assertNotIn("别的引擎", self.r["chat"])
        self.assertNotIn("别的引擎", self.r["wf"])

    def test_shepherd_无归属会话进入聊天分组(self):
        terms = [{"id": "adp_new", "title": "新会话", "engine": "pi",
                  "domain": "_desktop", "shepherd_mode": True}]
        js = (HARNESS
              .replace('const shepherdMode = false;', 'const shepherdMode = true;')
              .replace("APPLY_ORDER_SRC", _slice("app-rail.js", "function applyOrder("))
              .replace("SEED", "{}")
              .replace("SIDEBAR_SRC",
                       _slice("app-rail.js", "function termOrderId(")
                       + "\n" + _slice("app-rail.js", "function invalidateSessionBuckets(")
                       + "\n" + _slice("app-rail.js", "function sessionBuckets(")
                       + "\n" + _slice("app-rail.js", "function domSessionCount(")
                       + "\n" + _slice("app-rail.js", "function _pinSessionOrder(")
                       + "\nlet _pinTimer = 0;\nconst _pinDirty = new Set();\n"
                       + _slice("app-rail.js", "function _pinSave(")
                       + "\n" + _slice("app-rail.js", "function looseSessions(")
                       + "\n" + _slice("app-rail.js", "function visibleSessionsFor("))
              .replace("TERMS", json.dumps(terms, ensure_ascii=False))
              .replace("GROUP", json.dumps("wf-a")))
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["chat"], ["新会话"])

    def test_分组与计数只线性扫描一次(self):
        self.assertEqual(self.r["count"], 2)
        self.assertEqual(self.r["visibilityChecks"], len(self.TERMS))

    def test_休眠会话仍在列且殿后(self):
        self.assertEqual(self.r["wf"][-1], "休眠戊")


if __name__ == "__main__":
    unittest.main()


class ShellNotInChatGroup(unittest.TestCase):
    """临时 shell 终端不进「聊天」组。

    这一支是终端还没有独立应用时留下的:那时侧栏是它唯一的入口,所以借「聊天」组挂了出来。
    现在终端是一个应用,多个 shell 是它内部的标签(app-terminal.js),侧栏再列一份就成了
    「终端跑到聊天里去了」(2026-08-16 用户实见)——那一行既不是聊天,也不归这个组管。

    后端 /workbench/terminals 仍然列它:终端应用要靠那份清单枚举自己的标签,不能从源头砍掉,
    所以这道判据必须落在侧栏这一侧。
    """
    TERMS = SidebarGrouping.TERMS + [
        {"id": "t1", "title": "终端", "engine": "shell", "domain": "", "alive": True},
    ]

    @classmethod
    def setUpClass(cls):
        cls.r = SidebarGrouping._run(cls.TERMS)

    def test_聊天组里没有终端(self):
        self.assertNotIn("终端", self.r["chat"])

    def test_聊天只包含无工作流关联的会话(self):
        self.assertEqual(self.r["chat"], ["聊天甲", "聊天乙"])

    def test_工作流组也不受影响(self):
        self.assertNotIn("终端", self.r["wf"])
