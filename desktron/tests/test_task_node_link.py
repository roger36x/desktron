"""任务流节点 ↔ 任务的关联(nl 注记)。

节点行右键可以把这个节点关联到清单里已有的一条任务。关联指向任务的稳定 id(t: 注记),
不是行号也不是标题——行会漂、标题会改,只有 id 稳。坐标沿用 fdone 那套「阶段.节点」。
"""
import os
import sys
import tempfile
import unittest
import _srcmap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import tasks  # noqa: E402


class NodeLink(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "TASKS.md")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("- [ ] 带流程的任务   <!-- flow:%E7%94%B2|%E4%B9%99 -->\n- [ ] 目标任务\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _items(self):
        return {t["title"]: t for t in tasks.read_tasks(self.path)}

    def test_link_round_trip(self):
        tid, err = tasks.task_ensure_id(self.path, 1, "目标任务")
        self.assertEqual(err, "")
        self.assertTrue(tid)
        self.assertEqual(tasks.update_task(self.path, 0, "带流程的任务", nl=f"0.0={tid}"), "")
        self.assertEqual(self._items()["带流程的任务"]["nl"], f"0.0={tid}")

    def test_id_is_stable_across_rename(self):
        tid, _ = tasks.task_ensure_id(self.path, 1, "目标任务")
        self.assertEqual(tasks.update_task(self.path, 1, "目标任务", title="改过名的任务"), "")
        again, _ = tasks.task_ensure_id(self.path, 1, "改过名的任务")
        self.assertEqual(again, tid)   # 关联不会因为改名而断

    def test_changing_the_flow_drops_stale_links(self):
        tid, _ = tasks.task_ensure_id(self.path, 1, "目标任务")
        tasks.update_task(self.path, 0, "带流程的任务", nl=f"0.0={tid}")
        # 节点集换了,旧的「阶段.节点」坐标指向的已经不是原来那个节点
        tasks.update_task(self.path, 0, "带流程的任务", flow="%E4%B8%99|%E4%B8%81")
        self.assertNotIn("nl", self._items()["带流程的任务"])

    def test_link_needs_a_flow(self):
        tid, _ = tasks.task_ensure_id(self.path, 1, "目标任务")
        tasks.update_task(self.path, 1, "目标任务", nl=f"0.0={tid}")   # 这条没有 flow
        self.assertNotIn("nl", self._items()["目标任务"])

    def test_blank_clears_the_link(self):
        tid, _ = tasks.task_ensure_id(self.path, 1, "目标任务")
        tasks.update_task(self.path, 0, "带流程的任务", nl=f"0.0={tid}")
        tasks.update_task(self.path, 0, "带流程的任务", nl=" ")
        self.assertNotIn("nl", self._items()["带流程的任务"])


class FrontendWiring(unittest.TestCase):
    """两处节点视图都要能右键关联:上面的流程预览图与下面的节点树。"""

    def setUp(self):
        p = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "app-panel.js")
        self.js = _srcmap.panel_src()

    def test_both_node_views_open_the_link_menu(self):
        self.assertIn("row.oncontextmenu = (ev) => _wfNodeLinkMenu(", self.js)          # 节点树
        self.assertIn("(ev, si, ni) => _wfNodeLinkMenu(ev, domId, t, `${si}.${ni}`", self.js)   # 预览图
        self.assertIn("g.addEventListener(\"contextmenu\"", self.js)

    def test_tree_dot_and_graph_share_the_progress_semantics(self):
        # 两处圆点同形就必须同义:行首点 = 完成态(可点切换),备忘有无内容另用小点表达
        self.assertIn("function _wfFlowToggle(", self.js)
        self.assertIn("const clickNode = (si, ni) => onChange(_wfFlowToggle(", self.js)   # 图
        self.assertIn('fdone: _wfFlowToggle(stages, doneSet, si, ni)', self.js)           # 树
        self.assertIn('class="wf-node-memo"', self.js)

    def test_menu_targets_unfinished_tasks_only(self):
        body = self.js[self.js.index("async function _wfNodeLinkMenu("):self.js.index("function _wfNoteRender(")]
        self.assertIn('x.state === "done"', body)
        self.assertIn('op: "task-id"', body)      # 目标缺稳定 id 时现取


if __name__ == "__main__":
    unittest.main()
