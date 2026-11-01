"""记录漂移到别的目录之后,会话列表与删除这两条路要能按会话 id 找回它。

claude 的记录目录名在会话创建时由当时的 cwd 定死,之后 --resume 一直追加同一个文件,不随
进程 cwd 迁移。于是一段跑在工作区里的会话,记录可能留在出生目录下——按工作目录编码算出来的
目录里根本没有这个文件。适配器读记录那条路已经按 id 兜底(见 test_claude_transcript_path),
列表与删除还没有:出生目录一旦不在 _workspace_dirs 里(比如把那个仓从工作流的 repos 里摘掉),
这段会话就会在侧栏列表里消失,而删除只删得到不存在的路径,记录留在盘上,刷新又冒出来。

删除不可逆,所以兜底只认按 id 的精确命中:id 必须是 uuid 形状,文件名与 id 全等,且文件直属
~/.claude/projects 的某个子目录。通配符之类的输入一律不进 glob 模式。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import term  # noqa: E402
    from engines.stores import JsonlDirStore  # noqa: E402
finally:
    sys.argv = _argv

WORKSPACE = "/Users/somebody/.local/share/desktron/workspace"
DOMAIN = "/Users/somebody/.local/share/desktron/workspace/domains/desk-iter"
SRCREPO = "/Users/somebody/work/desktron"      # 会话出生的目录,已不在 _workspace_dirs 里
ESID = "ed6d6e9e-2020-4f58-89a2-5750d28db8a4"
OTHER = "3cab2c5c-d778-4529-a36f-0dd1e4ea4d50"


class RelocatedTranscript(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="claude-reloc-")
        self._exp = os.path.expanduser
        os.path.expanduser = (lambda p: p.replace("~", self.home, 1)
                              if isinstance(p, str) and p.startswith("~") else self._exp(p))
        self._saved = {k: getattr(term, k) for k in
                       ("_workspace_dirs", "_domain_for_cwd", "_term_meta_read", "_claude_named")}
        self.meta = {}
        term._claude_named = lambda: {}      # 本机 /rename 注册表与缓存不该串进标题断言
        term._workspace_dirs = lambda: [WORKSPACE, DOMAIN]
        term._domain_for_cwd = lambda wd: {WORKSPACE: "_desktop", DOMAIN: "desk-iter"}.get(wd, "")
        term._term_meta_read = lambda: self.meta
        self.store = JsonlDirStore("claude")

    def tearDown(self):
        os.path.expanduser = self._exp
        for k, v in self._saved.items():
            setattr(term, k, v)
        shutil.rmtree(self.home, ignore_errors=True)

    def _put(self, wd, esid=ESID, text="第一句提问"):
        d = os.path.join(self.home, ".claude", "projects", term._claude_proj_enc(wd))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, esid + ".jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "cwd": wd,
                                "message": {"content": text}}) + "\n")
        return path

    def _same(self, a, b):
        return os.path.realpath(a) == os.path.realpath(b)

    # ---- 按 id 定位 ----
    def test_session_by_id_prefers_encoded_dir(self):
        self._put(WORKSPACE, text="工作目录里的那份")
        self._put(SRCREPO, text="出生目录里的那份")
        self.assertEqual(term._claude_session_by_id(ESID)["title"], "工作目录里的那份")

    def test_session_by_id_falls_back_to_birth_dir(self):
        """出生目录不在 _workspace_dirs 里:找不回来的话,这段会话整条从休眠列表消失。"""
        self._put(SRCREPO, text="出生目录里的那份")
        self.assertEqual(term._claude_session_by_id(ESID)["title"], "出生目录里的那份")

    def test_session_by_id_absent(self):
        self.assertIsNone(term._claude_session_by_id(ESID))

    def test_hits_locate_the_file(self):
        want = self._put(SRCREPO)
        self.assertTrue(self._same(term._claude_transcript_hits(ESID)[0], want))

    def test_hits_reject_wildcards_and_paths(self):
        """通配符、路径分隔符不进 glob 模式:删除靠这一层保证不误伤。"""
        self._put(SRCREPO)
        for bad in ("*", "?" * 36, "ed6d6e9e-2020-4f58-89a2-5750d28db8a*",
                    "../" + ESID, ESID + "/x", "", None):
            self.assertEqual(term._claude_transcript_hits(bad), [], bad)
        self.assertEqual(len(term._claude_transcript_hits(ESID)), 1)

    # ---- 列表 ----
    def test_list_by_cwd_picks_up_relocated(self):
        self._put(SRCREPO, text="查 rollout 路径归属错位")
        self.meta = {ESID: {"engine": "claude", "domain": "_desktop"}}
        rows = self.store.list_by_cwd(WORKSPACE)
        self.assertEqual([r["id"] for r in rows], [ESID])
        self.assertEqual(rows[0]["title"], "查 rollout 路径归属错位")

    def test_list_by_cwd_needs_a_meta_record(self):
        """没登记归属的散记录不列:兜底是按 id 找回已知的会话,不是反向扫描全机。"""
        self._put(SRCREPO)
        self.assertEqual(self.store.list_by_cwd(WORKSPACE), [])

    def test_list_by_cwd_respects_domain(self):
        self._put(SRCREPO)
        self.meta = {ESID: {"engine": "claude", "domain": "desk-iter"}}
        self.assertEqual([r["id"] for r in self.store.list_by_cwd(WORKSPACE)], [])
        self.assertEqual([r["id"] for r in self.store.list_by_cwd(DOMAIN)], [ESID])

    def test_list_by_cwd_skips_other_engines_and_tasks(self):
        self._put(SRCREPO)
        self.meta = {ESID: {"engine": "codex", "domain": "_desktop"}}
        self.assertEqual(self.store.list_by_cwd(WORKSPACE), [])
        self.meta = {ESID: {"engine": "claude", "domain": "_desktop", "task": True}}
        self.assertEqual(self.store.list_by_cwd(WORKSPACE), [])

    def test_no_double_listing_across_workspace_dirs(self):
        """记录在另一个工作目录的编码目录下:那一侧已经列了它,这里不重复补。"""
        self._put(DOMAIN)
        self.meta = {ESID: {"engine": "claude", "domain": "_desktop"}}
        self.assertEqual(self.store.list_by_cwd(WORKSPACE), [])
        self.assertEqual([r["id"] for r in self.store.list_by_cwd(DOMAIN)], [ESID])

    def test_list_by_cwd_keeps_encoded_dir_rows(self):
        self._put(WORKSPACE, esid=OTHER)
        self._put(SRCREPO)
        self.meta = {ESID: {"engine": "claude", "domain": "_desktop"}}
        self.assertEqual({r["id"] for r in self.store.list_by_cwd(WORKSPACE)}, {ESID, OTHER})

    def test_list_by_cwd_unmapped_dir_unchanged(self):
        """外部仓这类无归属目录:meta 的 domain 对应不上,只按编码列,不补。"""
        self._put(SRCREPO)
        self.meta = {ESID: {"engine": "claude", "domain": "_desktop"}}
        self.assertEqual([r["id"] for r in self.store.list_by_cwd(SRCREPO)], [ESID])
        self.assertEqual(self.store.list_by_cwd("/Users/somebody/work/nothing"), [])

    # ---- 删除 ----
    def test_delete_removes_relocated(self):
        path = self._put(SRCREPO)
        self.store.delete(ESID)
        self.assertFalse(os.path.exists(path))

    def test_delete_removes_every_copy(self):
        a, b = self._put(WORKSPACE), self._put(SRCREPO)
        self.store.delete(ESID)
        self.assertFalse(os.path.exists(a))
        self.assertFalse(os.path.exists(b))

    def test_delete_touches_only_the_id(self):
        keep = [self._put(SRCREPO, esid=OTHER), self._put(WORKSPACE, esid=OTHER)]
        self._put(SRCREPO)
        self.store.delete(ESID)
        for p in keep:
            self.assertTrue(os.path.exists(p))

    def test_delete_with_wildcard_id_removes_nothing(self):
        keep = [self._put(SRCREPO), self._put(WORKSPACE, esid=OTHER)]
        for bad in ("*", "ed6d6e9e-2020-4f58-89a2-5750d28db8a*", "../" + ESID):
            self.store.delete(bad)
        for p in keep:
            self.assertTrue(os.path.exists(p))


if __name__ == "__main__":
    unittest.main()
