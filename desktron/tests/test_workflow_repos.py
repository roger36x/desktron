"""工作流「项目」节(引用清单,docs/workflow-unification-plan.md)回归:
_repo_name/_repo_items/_repos_segment、导入会话、前后端接线契约。
worktree 隔离已退役(cwd 恒为工作流目录,分支交给 agent 判断),故无 worktree 用例。"""
import os
import sys
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

# deskcfg 按 argv 位置取参(ui_server 的启动约定);import 前给测试桩(同 test_container_project 范式)
sys.argv = ["test", "/tmp/wfrepo-test-frontend", "0", "0", "/tmp/wfrepo-test-repo"]
import domains as _domains  # noqa: E402

def _read(*parts):
    if parts and parts[-1] in ("app-chat.js", "app-panel.js", "app-rail.js"):      # 2026-08-27 分拆:拼接整体等价
        return _srcmap.frontend_src(parts[-1])
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class RepoName(unittest.TestCase):
    def test_basename_normalized(self):
        self.assertEqual(_domains._repo_name("~/work/desktron"), "desktron")
        self.assertEqual(_domains._repo_name("/a/b/repo.name v2/"), "repo-name-v2")
        self.assertEqual(_domains._repo_name(""), "repo")


class RepoItemsAndSegment(unittest.TestCase):
    def setUp(self):
        self._orig = _domains._domain_entry
        self.entry = {"id": "desk-iter", "name": "Desk 迭代", "repos": [
            {"path": "/tmp/src-a"},
            {"path": "/tmp/src-b", "branch": "desk/x"},
            {"path": ""},              # 非法条目跳过
            "not-a-dict",              # 非法条目跳过
        ]}
        _domains._domain_entry = lambda wid: self.entry if wid == "desk-iter" else None

    def tearDown(self):
        _domains._domain_entry = self._orig

    def test_items_normalize_to_source_dir(self):
        items = _domains._repo_items("desk-iter")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["dir"], os.path.abspath("/tmp/src-a"))   # dir 恒等于源仓(worktree 已退役)
        self.assertEqual(items[1]["dir"], os.path.abspath("/tmp/src-b"))   # 存量 branch 字段被忽略,不再派生副本路径
        self.assertEqual(items[1]["name"], "src-b")
        self.assertNotIn("branch", items[1])

    def test_segment_lists_repos_and_instruction(self):
        seg = _domains._repos_segment("desk-iter", "/x/domains/desk-iter")
        self.assertIn("## 代码仓", seg)
        self.assertIn("- " + os.path.abspath("/tmp/src-a"), seg)
        self.assertIn("- " + os.path.abspath("/tmp/src-b"), seg)
        self.assertNotIn("worktree", seg)
        self.assertIn("修改前先读各目录中的 `AGENTS.md`", seg)
        self.assertIn("不会向其中加入额外文件", seg)

    def test_segment_empty_without_repos(self):
        self.entry = {"id": "desk-iter", "repos": []}
        self.assertEqual(_domains._repos_segment("desk-iter", "/x"), "")


class Wiring(unittest.TestCase):
    """接线契约(字符串级,与本仓其余前端测试同款)。"""
    def test_backend_route(self):
        api = _read("desktron", "lib", "workflow", "api.py")
        self.assertIn('p.endswith("/repos")', api)
        self.assertIn("list-sessions", api)
        self.assertIn("import-sessions", api)
        self.assertIn("_list_repo_sessions", api)
        self.assertIn("_list_repo_sessions(wd, engine)", api)   # 只列当前引擎
        self.assertIn("_import_repo_sessions", api)
        self.assertIn("_repo_scan_sessions", api)
        self.assertIn("_ensure_domain_dir(did)", api)   # 变更后重合成 AGENTS.md

    def test_injection_wired(self):
        dm = _read("desktron", "lib", "domains.py")
        self.assertIn("def _repos_segment", dm)
        self.assertIn("_repos_segment(ws, d)", dm)      # _ensure_domain_dir 里挂上了注入段

    def test_frontend_menu(self):
        js = _read("frontend", "app-rail.js") + "\n" + _read("frontend", "app-panel.js")
        self.assertIn("async function _openWorkflowMenu", js)
        self.assertIn('header: DeskI18n.t("wf.projects")', js)
        self.assertIn('label: DeskI18n.t("wf.addProject")', js)
        self.assertIn('label: DeskI18n.t("wf.openProjectFiles")', js)
        self.assertIn('label: DeskI18n.t("wf.removeProject")', js)
        self.assertIn('只移除引用，不删除磁盘目录', js)
        self.assertIn('chev: true', js)
        self.assertIn('op: "import-sessions"', js)
        self.assertIn("_wfRepoPick", js)
        self.assertIn("_wfRepoRemove", js)
        self.assertIn("const path = await pickFolder();", js)
        for gone in ('<div class="wf-repos">', "_wfReposRender", "_wfRepoRow",
                     "wf-repo-add", "转为独立 worktree"):
            self.assertNotIn(gone, js)
        self.assertIn('DeskI18n.t("wf.linkSessions")', js)
        self.assertIn("_wfRepoImportPicker", js)
        self.assertIn("关联所选 ${n} 个会话", js)   # 引用不是归属:关联不搬家
        self.assertIn("engine: curEngine", js)      # 候选与导入都按当前引擎过滤
        self.assertIn("关联不会移动会话", js)        # 顶部一句说清语义,行内不重复
        self.assertNotIn("改归属", js)               # 不再用「搬家」语义
        self.assertNotIn("ri-eng", js)              # 引擎码是内部缩写,不在弹层行内占列
        css = _read("frontend", "style.css")
        self.assertNotIn(".wf-repo ", css)
        self.assertIn(".rp-ctx .rp-ico .ico-folder-open", css)

    def test_home_copy_in_sync(self):
        home = os.path.expanduser("~/.local/share/desktron/home")
        if not os.path.isdir(home):
            self.skipTest("无部署副本")
        hs = open(os.path.join(home, "desktron", "lib", "workflow", "api.py"), encoding="utf-8").read()
        self.assertIn('p.endswith("/repos")', hs)


if __name__ == "__main__":
    unittest.main()
