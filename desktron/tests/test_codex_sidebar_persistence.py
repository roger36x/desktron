"""Codex 刷新不能因存储路径或扫描窗口丢掉已登记会话。"""
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktron/lib"))
_argv = sys.argv
sys.argv = ["test", str(ROOT / "frontend"), "0", "0", str(ROOT)]
try:
    import codexgate
    import deskcfg
    import term
    from engines import get
    from engine_adapters import CodexAdapter
finally:
    sys.argv = _argv


class SidebarPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.managed = self.root / "share/codex-home"
        self.global_home = self.root / "global"
        self.meta = {}
        self.index = {}
        self.store = get("codex").store
        patches = [
            mock.patch.object(deskcfg, "DATA_HOME", str(self.root / "share/data")),
            mock.patch.object(deskcfg, "log"),
            mock.patch.object(self.store, "_rollout_dir", str(self.global_home / "sessions")),
            mock.patch("codex_history.indexed_path", return_value=None),
            mock.patch.object(term, "_CODEX_CACHE", {}),
            mock.patch.object(term, "_TERM_DROPPED", set()),
            mock.patch.object(term, "_TERMS", {}),
            mock.patch.object(term, "_workspace_dirs", return_value=[str(self.workspace)]),
            mock.patch.object(term, "_term_meta_read", side_effect=lambda: self.meta),
            mock.patch.object(term, "_adapter", side_effect=lambda eng: SimpleNamespace(index=self.index if eng == "codex" else {})),
            mock.patch.object(term, "_live_map", return_value={}),
            mock.patch.object(term, "_gui_live_sids", return_value=set()),
            mock.patch.object(term, "_gui_resident_sids", return_value=set()),
            mock.patch.object(term, "_gui_busy_unread", return_value=(False, False)),
            mock.patch.object(term, "_cursor_sessions", return_value=[]),
            mock.patch.object(term, "_transcript_cwd", return_value=""),
            mock.patch.object(term, "_session_branch_label", return_value=""),
            mock.patch.object(term, "_display_title", side_effect=lambda sid, eng, row, *a: row.get("title")),
            mock.patch.object(term, "_codex_rollout_title", return_value="记录标题"),
            mock.patch.object(term, "_codex_list_domain", side_effect=lambda dom, cwd, fallback: dom),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def rollout(self, sid=None, base=None, cwd=None):
        sid = sid or str(uuid.uuid4())
        base = base or self.managed / "sessions"
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"rollout-2026-09-09-{sid}.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": sid, "cwd": str(cwd or self.workspace)}}) + "\n")
        return sid, path

    def register(self, sid):
        self.meta[sid] = {"engine": "codex", "domain": "work", "refs": ["ref-work"]}
        self.index["adp_test"] = {"engine_sid": sid, "domain": "work", "title": "会话标题", "time": {"created": 10}}

    def refresh(self):
        term._CODEX_CACHE.clear()
        return term._term_persisted_scan()

    def test_first_provision_creates_symlink(self):
        with mock.patch.multiple(codexgate, CODEX_HOME=str(self.managed), USER_CODEX=str(self.global_home)), \
             mock.patch.object(codexgate, "_link_user_skills"), \
             mock.patch.object(codexgate, "_config_needs_refresh", return_value=False), \
             mock.patch.object(codexgate, "_enable_hooks_config"), \
             mock.patch.object(codexgate, "_configure_app_control"), \
             mock.patch.object(codexgate, "_hooks_json", return_value={}):
            self.assertEqual(codexgate.ensure_codex_home(), str(self.managed))
            self.assertEqual(codexgate.ensure_codex_home(), str(self.managed))
        self.assertTrue((self.managed / "sessions").is_symlink())
        self.assertEqual((self.managed / "sessions").resolve(), (self.global_home / "sessions").resolve())

    def test_existing_managed_directory_and_global_records_are_both_visible(self):
        sid, path = self.rollout()
        old, _ = self.rollout(base=self.global_home / "sessions")
        self.assertEqual({r["id"] for r in term._codex_sessions()}, {sid, old})
        self.assertTrue(self.store.owns(sid))
        self.assertTrue(self.store.owns(old))
        adapter = CodexAdapter.__new__(CodexAdapter)
        self.assertEqual(Path(adapter._rollout_path(sid)), path)

    def test_symlink_is_scanned_once(self):
        sid, _ = self.rollout(base=self.global_home / "sessions")
        self.managed.mkdir(parents=True)
        (self.managed / "sessions").symlink_to(self.global_home / "sessions", target_is_directory=True)
        self.assertEqual(len(self.store.session_dirs()), 1)
        self.assertEqual([r["id"] for r in term._codex_sessions()], [sid])

    def test_owns_validates_rollout_header_without_title_parsing(self):
        sid, _ = self.rollout()
        with mock.patch.object(self.store, "list_all", side_effect=AssertionError("不能构建列表")), \
             mock.patch.object(term, "_codex_rollout_title", side_effect=AssertionError("不能取标题")):
            self.assertTrue(self.store.owns(sid))

    def test_owns_rejects_filename_match_with_wrong_header(self):
        filename_sid = str(uuid.uuid4())
        payload_sid = str(uuid.uuid4())
        self.rollout(sid=filename_sid)
        path = self.managed / "sessions" / f"rollout-2026-09-09-{filename_sid}.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": payload_sid, "cwd": str(self.workspace)}}) + "\n")
        self.assertFalse(self.store.owns(filename_sid))

    def test_300_newer_foreign_records_do_not_hide_registered_chat(self):
        sid, path = self.rollout()
        self.register(sid)
        os.utime(path, (1, 1))
        for _ in range(300):
            self.rollout(cwd=self.root / "other")
        rows = self.refresh()
        self.assertEqual([r["id"] for r in rows], [sid])
        self.assertNotIn("record_status", rows[0])
        self.assertEqual(rows[0]["session_id"], "adp_test")
        self.assertEqual(rows[0]["refs"], ["ref-work"])

    def test_more_than_300_workspace_records_are_not_truncated(self):
        for _ in range(405):
            self.rollout()
        self.assertEqual(len(term._codex_sessions()), 405)
        self.assertEqual(len(self.store.list_by_cwd(str(self.workspace))), 405)

    def test_workspace_alias_and_registered_old_cwd_remain_visible(self):
        alias = self.root / "alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        one, _ = self.rollout(cwd=alias)
        two, _ = self.rollout(cwd=self.root / "old-workspace")
        self.register(two)
        self.assertEqual({r["id"] for r in term._codex_sessions()}, {one, two})

    def test_missing_record_keeps_identity_and_recovers_on_refresh(self):
        sid = str(uuid.uuid4())
        self.register(sid)
        for _ in range(2):
            row, = self.refresh()
            self.assertEqual(row["id"], sid)
            self.assertEqual(row["session_id"], "adp_test")
            self.assertEqual(row["record_status"], "unavailable")
        self.rollout(sid=sid)
        row, = self.refresh()
        self.assertEqual(row["id"], sid)
        self.assertNotIn("record_status", row)

    def test_partial_record_is_retried_after_completion(self):
        sid, path = self.rollout()
        self.register(sid)
        path.write_text('{"type":')
        self.assertEqual(self.refresh()[0]["record_status"], "unavailable")
        self.rollout(sid=sid)
        self.assertNotIn("record_status", self.refresh()[0])

    def test_adapter_index_survives_interrupted_meta_registration(self):
        sid, _ = self.rollout(cwd=self.root / "old-workspace")
        self.register(sid)
        self.meta.clear()
        row, = self.refresh()
        self.assertEqual(row["id"], sid)
        self.assertEqual(row["domain"], "work")
        self.assertEqual(row["session_id"], "adp_test")
        self.assertNotIn("record_status", row)

    def test_existing_managed_directory_is_preserved_during_provision(self):
        sid, path = self.rollout()
        with mock.patch.multiple(codexgate, CODEX_HOME=str(self.managed), USER_CODEX=str(self.global_home)), \
             mock.patch.object(codexgate, "_link_user_skills"), \
             mock.patch.object(codexgate, "_config_needs_refresh", return_value=False), \
             mock.patch.object(codexgate, "_enable_hooks_config"), \
             mock.patch.object(codexgate, "_configure_app_control"), \
             mock.patch.object(codexgate, "_hooks_json", return_value={}):
            self.assertEqual(codexgate.ensure_codex_home(), str(self.managed))
        self.assertFalse((self.managed / "sessions").is_symlink())
        self.assertTrue(path.is_file())
        self.assertEqual([r["id"] for r in term._codex_sessions()], [sid])

    def test_closed_and_task_rows_are_not_resurrected(self):
        sid, _ = self.rollout()
        self.register(sid)
        for flag in ("closed", "task"):
            with self.subTest(flag=flag):
                self.meta[sid][flag] = True
                self.assertEqual(self.refresh(), [])
                self.meta[sid].pop(flag)

    def test_partial_identity_migration_does_not_duplicate_or_reopen(self):
        sid, _ = self.rollout()
        self.register(sid)
        self.meta["adp_test"] = self.meta.pop(sid)
        row, = self.refresh()
        self.assertEqual(row["id"], sid)
        self.assertEqual(row["refs"], ["ref-work"])
        self.meta["adp_test"]["closed"] = True
        self.assertEqual(self.refresh(), [])

    def test_deletion_removes_both_copies_and_does_not_resurrect(self):
        sid, path = self.rollout()
        _, other = self.rollout(sid=sid, base=self.global_home / "sessions")
        self.register(sid)
        self.assertEqual(len(self.refresh()), 1)
        term._codex_del_rollout(sid)
        self.meta.clear()
        self.index.clear()
        self.assertFalse(path.exists())
        self.assertFalse(other.exists())
        self.assertEqual(self.refresh(), [])

    def endpoint_fixture(self):
        """真实适配器索引和原生文件，仅隔离进程、标题及能力撤销。"""
        from session import api
        sid, path = self.rollout()
        _, other = self.rollout(sid=sid, base=self.global_home / "sessions")
        self.register(sid)
        self.meta["adp_test"] = dict(self.meta[sid])
        ad = CodexAdapter(str(self.root / "data"), str(self.workspace),
                          SimpleNamespace(publish=lambda event: None))
        ad.index.update(self.index)
        self.index = ad.index
        ad._save_index()
        message = Path(ad._msg_path("adp_test"))
        message.parent.mkdir(parents=True, exist_ok=True)
        message.write_text('{"kind":"user","text":"保留的历史"}\n', encoding="utf-8")
        for patch in (
            mock.patch("codex_appserver.drop_session"),
            mock.patch.object(ad, "_recap_request_clear"),
            mock.patch("app_control.capability.revoke"),
            mock.patch.object(term.termpty, "pty_kill"),
            mock.patch.object(term, "_term_set_title"),
            mock.patch("session_autotitle.drop_auto_title"),
            mock.patch.object(term, "_term_meta_write", side_effect=lambda m: setattr(self, "meta", m)),
            mock.patch.object(api, "_term_meta_read", side_effect=lambda: self.meta),
            mock.patch.object(api, "_term_meta_write", side_effect=lambda m: setattr(self, "meta", m)),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        handler = SimpleNamespace(_adapter_owning=lambda identity: ad,
                                  _send_json=lambda body, code=200: (body, code))
        def request(identity, query=""):
            method = "POST" if identity.endswith("/reopen") else "DELETE"
            return api.handle(handler, "/workbench/terminals/" + identity, query, method)
        return ad, sid, (path, other, message), request

    def check_endpoint_delete(self, native):
        ad, sid, paths, request = self.endpoint_fixture()
        self.assertEqual(len(self.refresh()), 1)
        body, status = request(sid if native else "adp_test")
        self.assertEqual(status, 200, body)
        self.assertEqual(ad.index, {})
        self.assertEqual(self.meta, {})
        self.assertTrue(all(not p.exists() for p in paths))
        self.assertEqual(self.refresh(), [])
        # 模拟重启重载索引与再次扫描，不能只靠本轮内存已删除。
        fresh = CodexAdapter(str(self.root / "data"), str(self.workspace), ad.bus)
        self.index = fresh.index
        self.assertEqual(self.index, {})
        self.assertEqual(self.refresh(), [])

    def test_endpoint_adapter_id_deletes_native_records_before_refresh(self):
        self.check_endpoint_delete(native=False)

    def test_endpoint_native_id_deletes_adapter_and_both_meta_keys(self):
        self.check_endpoint_delete(native=True)

    def test_busy_endpoint_keeps_every_record_for_both_ids(self):
        ad, sid, paths, request = self.endpoint_fixture()
        ad._live["adp_test"] = {"seq": 1, "parts": {}}
        for identity in (sid, "adp_test"):
            body, status = request(identity)
            self.assertEqual(status, 409)
            self.assertEqual(body["code"], "session_busy")
            self.assertIn("adp_test", ad.index)
            self.assertEqual(set(self.meta), {sid, "adp_test"})
            self.assertTrue(all(p.exists() for p in paths))
            self.assertEqual(len(self.refresh()), 1)

    def test_close_preserves_history_and_grouping_for_reopen(self):
        ad, sid, paths, request = self.endpoint_fixture()
        before = [p.read_bytes() for p in paths]
        with mock.patch.object(ad, "abort") as abort:
            body, status = request(sid, "keep=1")
        self.assertEqual(status, 200, body)
        abort.assert_called_once_with("adp_test", recall=False)
        self.assertIn("adp_test", ad.index)
        self.assertEqual([p.read_bytes() for p in paths], before)
        for identity in (sid, "adp_test"):
            self.assertTrue(self.meta[identity]["closed"])
            self.assertEqual(self.meta[identity]["refs"], ["ref-work"])
        self.assertEqual(self.refresh(), [])
        self.assertEqual(request("adp_test/reopen")[1], 200)
        self.assertEqual(len(self.refresh()), 1)
        for identity in (sid, "adp_test"):
            self.assertFalse(self.meta[identity]["closed"])
            self.assertEqual(self.meta[identity]["domain"], "work")
            self.assertEqual(self.meta[identity]["refs"], ["ref-work"])

    def test_unchanged_headers_are_cached(self):
        self.rollout()
        term._codex_sessions()
        term._CODEX_CACHE["codex"]["t"] = 0
        with mock.patch("builtins.open", side_effect=AssertionError("不应重读首行")):
            self.assertEqual(len(term._codex_sessions()), 1)

    def test_frontend_warning_appears_and_clears_after_recovery(self):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const src = fs.readFileSync(process.argv[1], 'utf8');
const record = src.slice(src.indexOf('function termRecordHTML('), src.indexOf('// 就地更新一行'));
const update = src.slice(src.indexOf('function updateTermRow('), src.indexOf('async function renderTermSessions('));
let warning = null;
const row = {classList: {toggle() {}}, querySelector(sel) {
  if (sel === '.ti-record-warning') return warning;
  if (sel === '.ti-main') return {insertAdjacentHTML(pos, html) {
    assert(html.includes('记录暂不可读'));
    warning = {remove() { warning = null; }};
  }};
  return null;
}};
const ctx = {bindTermRowIdentity() {}, stripTitleGlyph: x => x, compactBranchName: () => ''};
vm.createContext(ctx); vm.runInContext(record + update, ctx);
assert.strictEqual(ctx.termRecordHTML({}), '');
ctx.updateTermRow(row, {record_status: 'unavailable'});
assert(warning);
ctx.updateTermRow(row, {});
assert.strictEqual(warning, null);
'''
        subprocess.run(["node", "-e", script, str(ROOT / "frontend/app-panel-terms.js")], check=True)


if __name__ == "__main__":
    unittest.main()
