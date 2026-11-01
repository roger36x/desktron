"""Desk 自动更新：清单、暂存、强制静默与独立更新器边界。"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "desktron" / "lib"
sys.path.insert(0, str(LIB))
import self_update as update  # noqa: E402
import self_update_apply as apply  # noqa: E402
import dev_app_update as dev_app  # noqa: E402


def manifest(rev="a" * 40, build=12, size=3, sha=None, **extra):
    value = {
        "rev": rev, "build_number": build, "built_at": "2026-09-07T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z", "channel": "test", "signed": True,
        "mandatory_after": "", "min_supported_build": 0, "reason": "",
        "artifacts": {"arm64": {"file": "desk.tgz",
                                      "sha256": sha or hashlib.sha256(b"new").hexdigest(),
                                      "bytes": size}},
    }
    value.update(extra)
    return value


class ManifestContract(unittest.TestCase):
    def test_build_number_is_required_and_orders_versions(self):
        with mock.patch.object(update.platform, "machine", return_value="arm64"):
            got = update._manifest(manifest(), "https://updates.invalid")
        self.assertEqual(got["build_number"], 12)
        self.assertEqual(got["url"], "https://updates.invalid/desk.tgz")
        with self.assertRaisesRegex(ValueError, "build_number"):
            update._manifest({**manifest(), "build_number": 0})

    def test_expired_channel_is_rejected(self):
        with mock.patch.object(update.platform, "machine", return_value="arm64"):
            with self.assertRaisesRegex(ValueError, "通道已过期"):
                update._manifest(manifest(expires_at="2020-01-01T00:00:00Z"), now=time.time())

    def test_unsigned_release_manifest_is_rejected(self):
        with mock.patch.object(update.platform, "machine", return_value="arm64"):
            with self.assertRaisesRegex(ValueError, "稳定签名"):
                update._manifest(manifest(signed=False), "https://updates.invalid")

    def test_invalid_policy_dates_are_rejected(self):
        with mock.patch.object(update.platform, "machine", return_value="arm64"):
            with self.assertRaisesRegex(ValueError, "expires_at"):
                update._manifest(manifest(expires_at="later"))
            with self.assertRaisesRegex(ValueError, "mandatory_after"):
                update._manifest(manifest(mandatory_after="soon"))

    def test_artifact_cannot_escape_channel_origin(self):
        data = manifest()
        data["artifacts"]["arm64"]["file"] = "https://evil.invalid/desk.tgz"
        with mock.patch.object(update.platform, "machine", return_value="arm64"):
            with self.assertRaisesRegex(ValueError, "其他来源"):
                update._manifest(data, "https://updates.invalid")


class CheckAndStage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        update._CHECKING = False

    def tearDown(self):
        self.tmp.cleanup()

    def _patches(self, current=("b" * 40, 10, False)):
        return (mock.patch.object(update, "_base_dir", return_value=str(self.base)),
                mock.patch.object(update, "_current", return_value=current),
                mock.patch.object(update.platform, "machine", return_value="arm64"))

    def _serve(self, body=b"new", **fields):
        (self.base / "desk.tgz").write_bytes(body)
        data = manifest(size=len(body), sha=hashlib.sha256(body).hexdigest(), **fields)
        (self.base / "release.json").write_text(json.dumps(data), encoding="utf-8")
        handler = lambda *a, **k: SimpleHTTPRequestHandler(*a, directory=str(self.base), **k)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, "http://127.0.0.1:%d" % server.server_port

    def test_formal_build_downloads_and_stages(self):
        server, url = self._serve()
        patches = self._patches()
        try:
            with patches[0], patches[1], patches[2], mock.patch.object(update, "_channel_base", return_value=url):
                checked = update.check_now()
                self.assertEqual(checked["state"], "available")
                self.assertTrue(checked["hasUpdate"])
                self.assertFalse(list(self.base.glob("updates/**/*.tgz")))
                got = update.check_now(download=True)
        finally:
            server.shutdown(); server.server_close()
        self.assertEqual(got["state"], "staged")
        self.assertEqual(got["staged_build"], 12)
        self.assertEqual(Path(got["archive"]).read_bytes(), b"new")
        self.assertTrue(got["manual_install"])

    def test_development_copy_reports_but_never_downloads(self):
        server, url = self._serve()
        patches = self._patches(("payload:" + "c" * 64, 0, True))
        try:
            with patches[0], patches[1], patches[2], mock.patch.object(update, "_channel_base", return_value=url), \
                    mock.patch.object(update, "_download") as download:
                got = update.check_now(download=True)
        finally:
            server.shutdown(); server.server_close()
        self.assertEqual(got["state"], "dev")
        self.assertTrue(got["hasUpdate"]); download.assert_not_called()

    def test_download_rejects_https_downgrade_on_same_host(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "http://updates.invalid/desk.tgz"
        with mock.patch.object(update.platform, 'machine', return_value='arm64'):
            info = update._manifest(manifest(), "https://updates.invalid")
        with mock.patch.object(update, "_base_dir", return_value=str(self.base)), \
                mock.patch.object(update.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "其他来源"):
                update._download(info, {})

    def test_development_status_overrides_stale_installed_state(self):
        with mock.patch.object(update, "_current", return_value=("payload:" + "c" * 64, 0, True)), \
                mock.patch.object(update, "_load_state", return_value={
                    "state": "staged", "checked_at": 1, "hasUpdate": True}):
            got = update.status()
        self.assertEqual(got["state"], "dev")
        self.assertFalse(got["hasUpdate"])
        self.assertEqual(got["update_mode"], "source")

    def test_status_exposes_full_host_revision_for_update_probe(self):
        host_rev = "c" * 40
        with mock.patch.dict(os.environ, {"DESKTRON_HOST_REV":host_rev, "DESKTRON_HOST_BUILD":"12"}), \
                mock.patch.object(update, "_current", return_value=("payload:" + "a" * 64, 0, True)), \
                mock.patch.object(update, "_load_state", return_value={"state":"dev", "checked_at":1}):
            got = update.status()
        self.assertEqual(got["host_rev"], host_rev)
        self.assertEqual(got["host_current"], host_rev[:12])
        self.assertTrue(got["host_identity_known"])

    def test_state_writes_fsync_file_and_parent_directory(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "state.json"
            with mock.patch.object(update.os, "fsync", wraps=os.fsync) as fsync:
                update._atomic_json(str(target), {"state":"switching"})
            self.assertGreaterEqual(fsync.call_count, 1 if os.name == 'nt' else 2)
            self.assertEqual(json.loads(target.read_text(encoding='utf-8'))["state"], "switching")

    @contextlib.contextmanager
    def _handoff_fixture(self, pending):
        import lifecycle
        import runtime_identity
        identity = {"boot_id":"exact-boot", "host_instance_id":"exact-host",
                    "launch_generation":"exact-gen", "pid":os.getpid(), "host_pid":os.getppid()}
        pending["pending_identity"] = identity
        path = str(self.base / "state.json")
        apply.commit_handoff(path, pending)
        with mock.patch.object(update, "_state_path", return_value=path), \
             mock.patch.object(runtime_identity, "facts", return_value=identity), \
             mock.patch.object(lifecycle, "try_update_lock", return_value=123), \
             mock.patch.object(lifecycle, "release_update_lock"):
            yield

    def test_status_reconciles_pending_candidate_commit(self):
        pending = {"state":"verifying", "pending_protocol":2, "pending_outcome":"idle",
                   "pending_rev":"rev-new", "pending_build":12,
                   "pending_host_rev":"host-new", "pending_host_build":12,
                   "operation_id":"op", "acceptance_unknown":True,
                   "staged_rev":"rev-new", "staged_build":12, "hasUpdate":True}
        with self._handoff_fixture(pending), \
             mock.patch.dict(os.environ, {"DESKTRON_HOST_REV":"host-new",
                                          "DESKTRON_HOST_BUILD":"12"}), \
             mock.patch.object(update, "_current", return_value=("rev-new", 12, False)), \
             mock.patch.object(update, "_load_state", return_value=pending), \
             mock.patch.object(update, "_save_state", side_effect=lambda value: dict(value)) as save:
            got = update.status()
        self.assertEqual(got["state"], "idle")
        self.assertFalse(got["acceptance_unknown"])
        self.assertEqual(got["operation_id"], "")
        self.assertEqual(got["pending_outcome"], "")
        self.assertEqual(got["completion_id"], "op")
        self.assertEqual(got["completion_outcome"], "succeeded")
        self.assertGreater(got["completed_at"], 0)
        save.assert_called_once()

    def test_status_reconciles_pending_rollback_commit(self):
        pending = {"state":"verifying", "pending_protocol":2, "pending_outcome":"rolled_back",
                   "pending_rev":"rev-old", "pending_build":9,
                   "pending_host_rev":"host-old", "pending_host_build":9,
                   "operation_id":"op", "acceptance_unknown":True, "error":"candidate failed"}
        with self._handoff_fixture(pending), \
             mock.patch.dict(os.environ, {"DESKTRON_HOST_REV":"host-old",
                                          "DESKTRON_HOST_BUILD":"9"}), \
             mock.patch.object(update, "_current", return_value=("rev-old", 9, False)), \
             mock.patch.object(update, "_load_state", return_value=pending), \
             mock.patch.object(update, "_save_state", side_effect=lambda value: dict(value)):
            got = update.status()
        self.assertEqual(got["state"], "rolled_back")
        self.assertEqual(got["fix"], "旧版已恢复")
        self.assertEqual(got["error"], "candidate failed")
        self.assertFalse(got["acceptance_unknown"])
        self.assertEqual(got["completion_id"], "op")
        self.assertEqual(got["completion_outcome"], "failed")
        self.assertGreater(got["completed_at"], 0)

    def test_status_keeps_pending_commit_when_runtime_identity_does_not_match(self):
        pending = {"state":"verifying", "pending_protocol":2, "pending_outcome":"rolled_back",
                   "pending_rev":"rev-old", "pending_build":9,
                   "pending_host_rev":"host-old", "pending_host_build":9,
                   "operation_id":"op", "acceptance_unknown":True}
        with mock.patch.dict(os.environ, {"DESKTRON_HOST_REV":"host-other",
                                          "DESKTRON_HOST_BUILD":"9"}), \
             mock.patch.object(update, "_current", return_value=("rev-old", 9, False)), \
             mock.patch.object(update, "_load_state", return_value=pending), \
             mock.patch.object(update, "_save_state") as save:
            got = update.status()
        self.assertEqual(got["state"], "verifying")
        self.assertTrue(got["acceptance_unknown"])
        save.assert_not_called()

    def test_explicit_download_rejects_dev_and_coalesces_busy_requests(self):
        with mock.patch.object(update, "_current", return_value=("payload:abc", 0, True)), \
                mock.patch.object(update.threading, "Thread") as thread:
            self.assertFalse(update.trigger_check(download=True)["ok"])
            thread.assert_not_called()
        with mock.patch.object(update, "_current", return_value=("a" * 40, 1, False)), \
                mock.patch.object(update, "_CHECKING", True), \
                mock.patch.object(update, "_load_state", return_value={"state": "downloading"}), \
                mock.patch.object(update.threading, "Thread") as thread:
            self.assertTrue(update.trigger_check(download=True)["checking"])
            thread.assert_not_called()

    def test_interrupted_download_is_retryable(self):
        with mock.patch.object(update, "_current", return_value=("a" * 40, 1, False)), \
                mock.patch.object(update, "_CHECKING", False), \
                mock.patch.object(update, "_load_state", return_value={
                    "state": "downloading", "checked_at": 1, "hasUpdate": True}):
            state = update.status()
            self.assertEqual(state["state"], "error")
            self.assertTrue(state["hasUpdate"])

    def test_manual_download_cannot_auto_apply_even_if_mandatory(self):
        with mock.patch.object(update, "_current", return_value=("a" * 40, 1, False)), \
                mock.patch.object(update, "_load_state", return_value={
                    "state": "staged", "staged_rev": "abc", "min_supported_build": 2,
                    "manual_install": True}), \
                mock.patch.object(update, "quiet_ready", return_value=True), \
                mock.patch.object(update.subprocess, "Popen") as popen:
            result = update.request_apply("abc", automatic=True)
            self.assertFalse(result["ok"])
            self.assertIn("等待用户确认", result["error"])
            popen.assert_not_called()

    def test_current_does_not_treat_workspace_git_as_desk_development(self):
        (self.base / ".update-rev").write_text("b" * 40, encoding="utf-8")
        (self.base / ".update-build").write_text("10", encoding="utf-8")
        fake_cfg = mock.Mock(REPO_ROOT=str(self.base / "user-workspace"))
        (Path(fake_cfg.REPO_ROOT) / ".git").mkdir(parents=True)
        installed = self.base / "home" / "desktron" / "lib" / "self_update.py"
        with mock.patch.object(update, "_base_dir", return_value=str(self.base)), \
                mock.patch.dict(sys.modules, {"deskcfg": fake_cfg}), \
                mock.patch.object(update, "_payload_root", return_value=str(installed.parents[2])):
            _, _, dev = update._current()
        self.assertFalse(dev)

    def test_channel_base_comes_from_desk_payload_not_user_workspace(self):
        payload = self.base / "home"
        payload.mkdir()
        (payload / "manifest.yaml").write_text('update_base: "https://updates.example.com/desk/"\n',
                                               encoding="utf-8")
        with mock.patch.object(update, "_payload_root", return_value=str(payload)), \
                mock.patch.dict(os.environ, {"DESKTRON_UPDATE_BASE": ""}):
            self.assertEqual(update._channel_base(), "https://updates.example.com/desk")
        with mock.patch.object(update, "_payload_root", return_value=str(payload)), \
                mock.patch.dict(os.environ, {"DESKTRON_UPDATE_BASE": "https://override.example.com"}):
            self.assertEqual(update._channel_base(), "https://override.example.com")

    def test_empty_channel_means_no_update_source(self):
        payload = self.base / "home"
        payload.mkdir()
        (payload / "manifest.yaml").write_text('update_base: ""\n', encoding="utf-8")
        with mock.patch.object(update, "_payload_root", return_value=str(payload)), \
                mock.patch.dict(os.environ, {"DESKTRON_UPDATE_BASE": ""}):
            self.assertEqual(update._channel_base(), "")

    def test_older_build_is_not_an_update(self):
        server, url = self._serve(build=9)
        patches = self._patches()
        try:
            with patches[0], patches[1], patches[2], mock.patch.object(update, "_channel_base", return_value=url):
                got = update.check_now()
        finally:
            server.shutdown(); server.server_close()
        self.assertEqual(got["state"], "idle"); self.assertFalse(got["hasUpdate"])

    def test_new_build_updates_even_when_commit_identity_is_same(self):
        server, url = self._serve(rev="b" * 40, build=12)
        patches = self._patches(("b" * 40, 10, False))
        try:
            with patches[0], patches[1], patches[2], mock.patch.object(update, "_channel_base", return_value=url):
                got = update.check_now(download=True)
        finally:
            server.shutdown(); server.server_close()
        self.assertEqual(got["state"], "staged")
        self.assertEqual(got["staged_build"], 12)

    def test_failed_recheck_keeps_verified_staging(self):
        archive = self.base / "updates" / "12-a" / "desktron.tgz"
        archive.parent.mkdir(parents=True); archive.write_bytes(b"new")
        with mock.patch.object(update, "_base_dir", return_value=str(self.base)):
            update._save_state({"state": "staged", "archive": str(archive),
                                "staged_rev": "a" * 40, "hasUpdate": True})
            with mock.patch.object(update, "_channel_base", return_value="http://127.0.0.1:1"):
                got = update.check_now()
        self.assertEqual(got["state"], "staged")
        self.assertTrue(got["error"]); self.assertTrue(archive.exists())

    def test_failed_check_does_not_overwrite_applying_state(self):
        with mock.patch.object(update, "_channel_base", return_value="http://127.0.0.1:1"), \
                mock.patch.object(update, "_load_state", return_value={
                    "state": "applying", "apply_pid": 123}):
            got = update.check_now()
        self.assertEqual(got["state"], "applying")


class MandatoryQuietWindow(unittest.TestCase):
    def setUp(self):
        update._QUIET_SINCE = 0
        update._LAST_ACTIVITY = 0
        update._DRAINING = False
        update._ACTIVE_WORK = 0

    def test_requires_two_consecutive_quiet_observations(self):
        with mock.patch.object(update, "_work_is_quiet", return_value=True):
            self.assertFalse(update.quiet_ready(1000))
            self.assertFalse(update.quiet_ready(1000 + update.QUIET_CONFIRM_S - 1))
            self.assertTrue(update.quiet_ready(1000 + update.QUIET_CONFIRM_S))

    def test_activity_resets_quiet_window(self):
        with mock.patch.object(update, "_work_is_quiet", return_value=True):
            self.assertFalse(update.quiet_ready(1000))
            with mock.patch.object(update.time, "time", return_value=1100):
                update.note_activity()
            self.assertFalse(update.quiet_ready(2000))

    def test_live_tool_terminal_blocks_quiet(self):
        fake_term = mock.Mock()
        fake_term._term_list.return_value = [{"id": "shell", "busy": False}]
        with mock.patch.dict(sys.modules, {"term": fake_term}):
            self.assertFalse(update._work_is_quiet())

    def test_running_recap_blocks_quiet(self):
        fake_term = mock.Mock()
        fake_term._term_list.return_value = []
        fake_term._term_persisted.return_value = []
        fake_term._agents_targets.return_value = []
        fake_engine = mock.Mock()
        fake_engine._adapters = {"claude": mock.Mock(_recap_runs={"s": object()})}
        fake_engine._adapters_lock = threading.Lock()
        with mock.patch.dict(sys.modules, {"term": fake_term, "engine": fake_engine}):
            self.assertFalse(update._work_is_quiet())

    def test_drain_gate_rejects_new_work_and_activity_resets_quiet(self):
        update._QUIET_SINCE = 123
        update._DRAINING = True
        with update.admit_work(user_activity=True) as admitted:
            self.assertFalse(admitted)
        self.assertEqual(update._QUIET_SINCE, 123)
        update._DRAINING = False
        with update.admit_work(user_activity=True) as admitted:
            self.assertTrue(admitted)
        self.assertEqual(update._QUIET_SINCE, 0)

    def test_tracked_background_work_blocks_quiet_until_exit(self):
        fake_term = mock.Mock()
        fake_term._term_list.return_value = []
        fake_term._term_persisted.return_value = []
        fake_term._agents_targets.return_value = []
        fake_runtime = mock.Mock()
        fake_runtime.runtime.return_value.pending_all.return_value = []
        fake_scheduler = mock.Mock()
        fake_scheduler.has_running.return_value = False
        with mock.patch.dict(sys.modules, {"term": fake_term,
                "app_control.runtime": fake_runtime, "arrangement.scheduler": fake_scheduler}):
            with update.tracked_work() as admitted:
                self.assertTrue(admitted)
                self.assertFalse(update._work_is_quiet())
        self.assertEqual(update._ACTIVE_WORK, 0)

    def test_async_work_is_registered_before_thread_can_start(self):
        self.assertTrue(update.begin_work(user_activity=True))
        self.assertEqual(update._ACTIVE_WORK, 1)
        update._DRAINING = True
        self.assertFalse(update.begin_work())
        update.finish_work()
        self.assertEqual(update._ACTIVE_WORK, 0)

    def test_recent_persisted_switch_blocks_work_after_new_backend_starts(self):
        with mock.patch.object(update, "_load_state", return_value={
                "state": "verifying", "updated_at": int(time.time())}):
            with update.admit_work() as admitted:
                self.assertFalse(admitted)

    def test_stale_switch_state_does_not_lock_desk_forever(self):
        with mock.patch.object(update, "_load_state", return_value={
                "state": "verifying", "updated_at": 1}):
            with update.admit_work() as admitted:
                self.assertTrue(admitted)

    def test_unknown_native_update_stays_closed_across_backend_restart(self):
        with mock.patch.object(update, "_load_state", return_value={
                "state": "applying", "updated_at": 1, "apply_pid": 0,
                "acceptance_unknown": True}):
            with update.admit_work() as admitted:
                self.assertFalse(admitted)

    def test_corrupt_minimum_build_does_not_break_status(self):
        with mock.patch.object(update, "_load_state", return_value={
                "checked_at": 1, "hasUpdate": True, "min_supported_build": "broken"}), \
                mock.patch.object(update, "_current", return_value=("a" * 40, 10, False)):
            got = update.status()
        self.assertFalse(got["mandatory"])

    def test_development_copy_cannot_apply_staged_release(self):
        with mock.patch.object(update, "_current", return_value=("payload:" + "c" * 64, 0, True)), \
                mock.patch.object(update, "_load_state", return_value={
                    "state": "staged", "staged_rev": "a" * 40}):
            got = update.request_apply("a" * 40)
        self.assertFalse(got["ok"])
        self.assertIn("开发副本", got["error"])

    def test_development_app_rejects_missing_build_before_staging(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); stage = base / "updates/dev-host.12345678"
            app = stage / "Desktron.app/Contents"; (app / "MacOS").mkdir(parents=True); (app / "Resources").mkdir()
            (app / "MacOS/desktron").write_text("binary", encoding='utf-8')
            (app / "Resources/release-rev").write_text("c" * 40, encoding='utf-8')
            with mock.patch.object(update.sys, "platform", "darwin"), \
                 mock.patch.object(update, "_base_dir", return_value=str(base)), \
                 mock.patch.object(update, "_current", return_value=("payload:" + "a" * 64, 0, True)), \
                 mock.patch("runtime_identity.facts", return_value={"supervisor":"native"}), \
                 mock.patch.object(update, "_save_state") as save:
                got = update.request_development_app(stage.name, "c" * 40)
            self.assertFalse(got["ok"]); save.assert_not_called()

    def test_development_app_uses_native_update_transaction(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); stage = base / "updates/dev-host.12345678"
            app = stage / "Desktron.app/Contents"; (app / "MacOS").mkdir(parents=True); (app / "Resources").mkdir()
            (app / "MacOS/desktron").write_text("binary", encoding='utf-8'); (app / "Resources/release-rev").write_text("c" * 40, encoding='utf-8')
            (app / "Resources/release-build").write_text("2", encoding='utf-8')
            with mock.patch.object(update.sys, "platform", "darwin"), mock.patch.object(update, "_base_dir", return_value=str(base)), \
                 mock.patch.object(update, "_current", return_value=("payload:" + "a" * 64, 0, True)), \
                 mock.patch("runtime_identity.facts", return_value={"supervisor":"native"}), \
                 mock.patch("macos_codesign.verify_stable_app", return_value="stable"), \
                 mock.patch.object(update, "_request_native_apply", return_value={"ok":True}) as request:
                self.assertTrue(update.request_development_app(stage.name, "c" * 40)["ok"])
            staged = request.call_args.args[0]
            self.assertEqual(staged["mode"], "host-only"); self.assertEqual(staged["staged_host_rev"], "c" * 40)

    @mock.patch.object(update.sys, 'platform', 'darwin')
    def test_native_supervisor_receives_exact_update_operation(self):
        state = {"state": "staged", "staged_rev": "a" * 40}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(update, "_base_dir", return_value=td), \
             mock.patch.object(update, "_current", return_value=("a" * 40, 10, False)), \
             mock.patch.object(update, "_load_state", return_value=state), \
             mock.patch.dict(os.environ, {"DESKTRON_HOST_REV":"host-rev", "DESKTRON_HOST_BUILD":"7"}), \
             mock.patch("runtime_identity.facts", return_value={"supervisor":"native", "boot_id":"boot",
                 "host_instance_id":"host", "launch_generation":"generation"}), \
             mock.patch("lifecycle.prepare_restart",
                        return_value={"ok": True, "state": "stopping"}) as prepare, \
             mock.patch("lifecycle.submit_host_update",
                        return_value={"ok": True, "state": "accepted"}) as submit, \
             mock.patch.object(update, "threading") as watcher:
            got = update.request_apply("a" * 40)
        self.assertTrue(got["ok"])
        self.assertRegex(got["operation_id"], r"^[0-9a-f]{32}$")
        submit.assert_called_once_with(got["operation_id"])
        prepare.assert_called_once_with(got["operation_id"], "boot")
        watcher.Thread.return_value.start.assert_called_once()
        self.assertEqual(state["state"], "applying")
        self.assertEqual(state["previous_host_build"], 7)

    def test_native_apply_rejects_missing_lifecycle_identity(self):
        state = {"state":"staged", "staged_rev":"a" * 40}
        with mock.patch("runtime_identity.facts", return_value={"supervisor":"native", "boot_id":"boot"}), \
             mock.patch.dict(os.environ, {"DESKTRON_HOST_REV":"host-rev"}), \
             mock.patch("lifecycle.prepare_restart") as prepare:
            got = update._request_native_apply(state, automatic=False, mandatory=False)
        self.assertFalse(got["ok"]); self.assertEqual(got["state"], "rejected")
        prepare.assert_not_called()

    def test_native_apply_cas_compares_complete_stage_identity(self):
        original = {"state":"staged", "mode":"host-only", "staged_rev":"payload:same",
                    "staged_app":"/updates/dev-host.first", "staged_host_rev":"host-a",
                    "staged_host_sha256":"sha-a"}
        replaced = {**original, "staged_app":"/updates/dev-host.second",
                    "staged_host_rev":"host-b", "staged_host_sha256":"sha-b"}
        facts = {"supervisor":"native", "boot_id":"boot", "host_instance_id":"host",
                 "launch_generation":"generation"}
        with mock.patch.object(update, "_load_state", return_value=replaced), \
             mock.patch.dict(os.environ, {"DESKTRON_HOST_REV":"host-old", "DESKTRON_HOST_BUILD":"7"}), \
             mock.patch("runtime_identity.facts", return_value=facts), \
             mock.patch("lifecycle.prepare_restart", return_value={"ok":True}), \
             mock.patch("lifecycle.reopen") as reopen, \
             mock.patch("lifecycle.submit_host_update") as submit:
            got = update._request_native_apply(original, automatic=False, mandatory=False)
        self.assertFalse(got["ok"]); self.assertEqual(got["state"], "rejected")
        reopen.assert_called_once(); submit.assert_not_called()

    def test_update_and_restart_share_cross_process_lock(self):
        import lifecycle
        with tempfile.TemporaryDirectory() as root, mock.patch.object(lifecycle,'_control_root',return_value=root):
            fd=lifecycle.try_update_lock();self.assertIsNotNone(fd)
            self.assertIsNone(lifecycle.try_update_lock())
            os.close(fd)
            again=lifecycle.try_update_lock();self.assertIsNotNone(again)
            lifecycle.release_update_lock(again)

    def test_native_preflight_failure_reopens_the_same_operation(self):
        import lifecycle
        operation = "d" * 32
        lifecycle._STATE = "stopping"; lifecycle._OPERATION = operation
        update._DRAINING = True
        facts = {"host_instance_id": "h", "launch_generation": "g",
                 "boot_id": "b", "pid": 42}
        try:
            with tempfile.TemporaryDirectory() as root, \
                 mock.patch.object(lifecycle, "control_root", return_value=root), \
                 mock.patch("runtime_identity.facts", return_value=facts), \
                 mock.patch.object(update, "_base_dir", return_value=root):
                update._save_state({"state": "applying", "operation_id": operation})
                Path(root, "update-response-" + operation + ".json").write_text(
                    json.dumps({"operation_id": operation, "state": "failed",
                        "host_instance_id": "h", "request_launch_generation": "g",
                        "request_boot_id": "b", "request_pid": 42}), encoding="utf-8")
                update._watch_native_apply(operation)
                self.assertEqual(update._load_state()["state"], "error")
            self.assertFalse(update._DRAINING)
            self.assertTrue(lifecycle.admitted())
        finally:
            lifecycle._STATE = "running"; lifecycle._OPERATION = ""
            update._DRAINING = False

    def test_apply_waits_for_inflight_check(self):
        update._CHECKING = True
        try:
            got = update.request_apply("a" * 40)
        finally:
            update._CHECKING = False
        self.assertFalse(got["ok"])
        self.assertIn("尚未完成", got["error"])

    def test_background_updater_only_starts_on_macos(self):
        update._STARTED = False
        with mock.patch.object(update.sys, "platform", "linux"), \
                mock.patch.object(update.threading, "Thread") as thread:
            self.assertFalse(update.start_background())
        self.assertFalse(update._STARTED)
        thread.assert_not_called()


class SafeExtraction(unittest.TestCase):
    STABLE_REQUIREMENT = ('designated => identifier "io.github.desktron" and anchor apple generic '
                          'and certificate leaf[subject.CN] = "Fixture"')

    def setUp(self):
        self.sleep_inhibitor = mock.patch.object(apply, "acquire_sleep_inhibitor", return_value=None)
        self.sleep_inhibitor_alive = mock.patch.object(apply, "require_sleep_inhibitor")
        self.native_parent = mock.patch.object(apply, "require_native_parent")
        self.ready_consumed = mock.patch.object(apply, "wait_native_ready_consumed")
        self.current_contract = mock.patch.object(apply, "verify_current_native_contract")
        self.staged_contract = mock.patch.object(apply, "verify_staged_launch_contract")
        self.stable_signature = mock.patch.object(
            apply, "verify_stable_app", return_value=self.STABLE_REQUIREMENT)
        self.installed_signature = mock.patch.object(
            apply, "installed_requirement_or_legacy", return_value=self.STABLE_REQUIREMENT)
        self.current_contract.start(); self.staged_contract.start(); self.stable_signature.start()
        self.installed_signature.start()
        self.addCleanup(self.current_contract.stop); self.addCleanup(self.staged_contract.stop)
        self.addCleanup(self.stable_signature.stop); self.addCleanup(self.installed_signature.stop)
        accepted = mock.patch.object(apply, "accepted_launch_identity", return_value={
            "boot_id":"accepted-boot", "host_instance_id":"accepted-host",
            "launch_generation":"accepted-gen", "pid":123, "host_pid":456})
        accepted.start(); self.addCleanup(accepted.stop)
        self.release_host = mock.patch.object(apply, "release_exact")
        self.sleep_inhibitor.start(); self.sleep_inhibitor_alive.start(); self.native_parent.start()
        self.ready_consumed.start(); self.release_host.start()

    def tearDown(self):
        self.release_host.stop(); self.ready_consumed.stop(); self.native_parent.stop()
        self.sleep_inhibitor_alive.stop(); self.sleep_inhibitor.stop()

    def _release_fixture(self, root):
        base = Path(root)
        updates = base / "updates" / "12-a"
        updates.mkdir(parents=True)
        package = base / "package"
        payload = package / "payload"
        app_dir = package / "Desktron.app"
        (payload / "desktron" / "bin").mkdir(parents=True)
        (app_dir / "Contents" / "MacOS").mkdir(parents=True)
        (app_dir / "Contents" / "Resources").mkdir(parents=True)
        (payload / "desktron" / "bin" / "desktron").write_text("new-home", encoding="utf-8")
        (payload / ".release-rev").write_text("a" * 40, encoding="utf-8")
        (payload / ".release-build").write_text("12", encoding="utf-8")
        (app_dir / "Contents" / "MacOS" / "desktron").write_text("new-app", encoding="utf-8")
        (app_dir / "Contents" / "Resources" / "release-rev").write_text("a" * 40, encoding="utf-8")
        (app_dir / "Contents" / "Resources" / "release-build").write_text("12", encoding="utf-8")
        archive = updates / "desktron.tgz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(package, arcname="desk")
        home = base / "home"
        app_target = base / "Applications" / "Desktron.app"
        home.mkdir(); app_target.mkdir(parents=True)
        (home / "old").write_text("old-home", encoding="utf-8")
        (app_target / "old").write_text("old-app", encoding="utf-8")
        (base / ".update-rev").write_text("b" * 40, encoding="utf-8")
        (base / ".update-build").write_text("10", encoding="utf-8")
        state_path = base / "updates" / "state.json"
        state_path.write_text(json.dumps({"state": "applying", "archive": str(archive),
            "operation_id": "0" * 32,
            "helper_contract":2, "helper_sha256":apply.sha256(apply.__file__),
            "staged_rev": "a" * 40, "staged_build": 12,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}), encoding="utf-8")
        return base, state_path, home, app_target

    def test_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "bad.tgz"
            with tarfile.open(archive, "w:gz") as tf:
                info = tarfile.TarInfo("../escape"); info.size = 1
                tf.addfile(info, io.BytesIO(b"x"))
            with self.assertRaisesRegex(ValueError, "越界路径"):
                apply.safe_extract(str(archive), str(Path(td) / "out"))

    def test_accepts_single_package_root(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "ok.tgz"
            with tarfile.open(archive, "w:gz") as tf:
                info = tarfile.TarInfo("desk/payload/readme"); info.size = 2
                tf.addfile(info, io.BytesIO(b"ok"))
            got = apply.safe_extract(str(archive), str(Path(td) / "out"))
            self.assertEqual(Path(got).name, "desk")
            self.assertEqual((Path(got) / "payload" / "readme").read_text(encoding='utf-8'), "ok")

    def test_rejects_write_through_archive_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "bad-link.tgz"
            with tarfile.open(archive, "w:gz") as tf:
                link = tarfile.TarInfo("desk/payload/link")
                link.type = tarfile.SYMTYPE; link.linkname = "../../outside"
                tf.addfile(link)
                info = tarfile.TarInfo("desk/payload/link/file"); info.size = 1
                tf.addfile(info, io.BytesIO(b"x"))
            with self.assertRaisesRegex(ValueError, "不安全成员|越界链接"):
                apply.safe_extract(str(archive), str(Path(td) / "out"))

    def test_remote_wss_preflight_rejects_incomplete_receipts(self):
        with self.assertRaisesRegex(ValueError, "不完整"):
            apply.remote_wss_expected({})
        self.assertFalse(apply.remote_wss_expected({"local":True, "enabled":False}))
        self.assertFalse(apply.remote_wss_expected({"local":True, "enabled":False,
            "tailscale":{"enabled":True, "state":"offline"}}))
        self.assertTrue(apply.remote_wss_expected({"local":True, "enabled":False,
            "tailscale":{"enabled":True, "state":"ready", "address":"https://desk.test/"}}))

    def test_write_marker_fsyncs_file_directory_and_deletion(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "marker"
            with mock.patch.object(apply.os, "fsync", wraps=os.fsync) as sync:
                apply.write_marker(str(marker), "value")
                self.assertEqual(sync.call_count, 1 if os.name == "nt" else 2)
            self.assertEqual(marker.read_text(encoding="utf-8"), "value\n")
            with mock.patch.object(apply.os, "fsync", wraps=os.fsync) as sync:
                apply.write_marker(str(marker), "")
                self.assertEqual(sync.call_count, 0 if os.name == "nt" else 1)
            self.assertFalse(marker.exists())

    def test_signature_preflight_rejects_invalid_or_changed_stable_install_before_copy(self):
        for failure in (RuntimeError("stable verification failed"), "different-requirement"):
            with self.subTest(failure=str(failure)), tempfile.TemporaryDirectory() as td:
                _, state_path, home, app_target = self._release_fixture(td)
                operation = "0" * 32
                installed = (mock.patch.object(apply, "installed_requirement_or_legacy",
                                                side_effect=failure)
                             if isinstance(failure, Exception)
                             else mock.patch.object(apply, "installed_requirement_or_legacy",
                                                    return_value=failure))
                with mock.patch.object(apply.os.path, "expanduser", return_value=str(app_target)), installed:
                    self.assertEqual(apply.apply(str(state_path), native_host_pid=4321,
                                                 operation_id=operation), 1)
                self.assertFalse(Path(str(app_target) + ".update-next").exists())
                self.assertTrue((app_target / "old").exists()); self.assertTrue((home / "old").exists())
                saved = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["state"], "error")
                self.assertEqual(saved["completion_outcome"], "failed")
                self.assertEqual(saved["operation_id"], operation)
                self.assertFalse(saved["acceptance_unknown"])

    def test_recorded_identity_rejects_replaced_legacy_install_before_copy(self):
        with tempfile.TemporaryDirectory() as td:
            base, state_path, _, app_target = self._release_fixture(td)
            (base / ".codesign-requirement").write_text(
                self.STABLE_REQUIREMENT + "\n", encoding="utf-8")
            operation = "0" * 32
            with mock.patch.object(apply.os.path, "expanduser", return_value=str(app_target)), \
                 mock.patch.object(apply, "installed_requirement_or_legacy", return_value=""):
                self.assertEqual(apply.apply(str(state_path), native_host_pid=4321,
                                             operation_id=operation), 1)
            self.assertFalse(Path(str(app_target) + ".update-next").exists())
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["state"], "error")
            self.assertIn("已记录的稳定身份", saved["error"])

    def test_signature_preflight_allows_proven_legacy_install_to_reach_native_checks(self):
        with tempfile.TemporaryDirectory() as td:
            _, state_path, _, app_target = self._release_fixture(td)
            operation = "0" * 32
            with mock.patch.object(apply.os.path, "expanduser", return_value=str(app_target)), \
                 mock.patch.object(apply, "installed_requirement_or_legacy", return_value=""), \
                 mock.patch.object(apply, "prepare_managed_pi_extensions"):
                self.assertEqual(apply.apply(str(state_path), native_host_pid=4321,
                                             operation_id=operation), 1)
            self.assertTrue(Path(str(app_target) + ".update-next").exists())
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["state"], "error")
            self.assertIn("原生宿主缺少可验证", saved["error"])

    def test_probe_requires_new_identity_build_and_launched_process_group(self):
        previous = {"boot_id":"old-boot", "host_instance_id":"old-host", "launch_generation":"old-gen"}
        desk = {"desk":True, "rev":"payload:new", "boot_id":"new-boot",
                "host_instance_id":"new-host", "launch_generation":"new-gen",
                "pid":321, "host_pid":111}
        update_status = {"host_rev":"host-new", "host_build":17}
        with mock.patch.object(apply.os, "getpgid", return_value=123), \
             mock.patch.object(apply.os, "getsid", return_value=123), \
             mock.patch.object(apply, "process_parent", return_value=111):
            ok, checks = apply.probe_predicates(desk, update_status,
                {"local":True, "enabled":False, "tailscale":{"enabled":True,
                    "state":"ready", "address":"https://desk.test/"}}, expect_rev="payload:new",
                expect_host_rev="host-new", expect_host_build=17, expect_pgid=123,
                expect_native_pid=111, port_owner_pid=321, process_running=True,
                previous_identity=previous, expect_remote_wss=True)
        self.assertTrue(ok); self.assertTrue(all(checks.values()))
        self.assertFalse(apply.probe_predicates(desk, {**update_status, "host_build":18}, {},
            expect_rev="payload:new", expect_host_rev="host-new", expect_host_build=17,
            previous_identity=previous)[0])
        with mock.patch.object(apply.os, "getpgid", return_value=999), \
             mock.patch.object(apply.os, "getsid", return_value=123), \
             mock.patch.object(apply, "process_parent", return_value=111):
            self.assertFalse(apply.probe_predicates(desk, update_status, {},
                expect_rev="payload:new", expect_host_rev="host-new", expect_host_build=17,
                expect_pgid=123, expect_native_pid=111,
                port_owner_pid=321, process_running=True,
                previous_identity=previous)[0])
        with mock.patch.object(apply.os, "getpgid", return_value=123), \
             mock.patch.object(apply.os, "getsid", return_value=123), \
             mock.patch.object(apply, "process_parent", return_value=111):
            self.assertFalse(apply.probe_predicates(desk, update_status, {},
                expect_rev="payload:new", expect_host_rev="host-new", expect_host_build=17,
                expect_pgid=123, expect_native_pid=111,
                port_owner_pid=999, process_running=True,
                previous_identity=previous)[0])
            self.assertFalse(apply.probe_predicates(desk, update_status, {},
                expect_rev="payload:new", expect_host_rev="host-new", expect_host_build=17,
                expect_pgid=123, expect_native_pid=111,
                port_owner_pid=321, process_running=False,
                previous_identity=previous)[0])
        desk["host_instance_id"] = "old-host"
        self.assertFalse(apply.probe_predicates(desk, update_status, {},
            expect_rev="payload:new", expect_host_rev="host-new", previous_identity=previous)[0])
        self.assertFalse(apply.probe_predicates(desk, update_status, {},
            expect_rev="payload:new", expect_host_rev="host-new",
            previous_identity={"boot_id":"", "host_instance_id":"", "launch_generation":""})[0])

    def test_probe_accepts_backend_in_own_pgid_inside_transaction_session(self):
        # Foundation.Process gives the backend its own PGID; it still inherits the host SID.
        # Observed 2026-09-28: host pgid 55761, backend pgid 55776, same session.
        desk = {"desk":True, "rev":"new", "pid":321, "host_pid":111}
        pgids = {111:123, 321:321}
        def check(sids):
            with mock.patch.object(apply.os, "getpgid", side_effect=lambda pid: pgids[pid]), \
                 mock.patch.object(apply.os, "getsid", side_effect=lambda pid: sids[pid]), \
                 mock.patch.object(apply, "process_parent", return_value=111):
                return apply.probe_predicates(desk, {}, {}, expect_rev="new", expect_pgid=123,
                    expect_native_pid=111, port_owner_pid=321, process_running=True)
        ok, checks = check({111:123, 321:123})
        self.assertTrue(ok); self.assertTrue(checks["process_group"])
        ok, checks = check({111:123, 321:999})
        self.assertFalse(ok); self.assertFalse(checks["process_group"])
        pgids[111] = 999
        ok, checks = check({111:123, 321:123})
        self.assertFalse(ok); self.assertFalse(checks["process_group"])

    def test_tracked_backend_may_own_pgid_but_not_leave_session(self):
        process = mock.Mock(pid=4321, _desk_host_pid=5678, _desk_backend_pid=6789)
        pgids = {5678:4321, 6789:6789}
        with mock.patch.object(apply, "alive", return_value=True), \
             mock.patch.object(apply.os, "getpgid", side_effect=lambda pid: pgids[pid]):
            with mock.patch.object(apply.os, "getsid", return_value=4321):
                apply.require_tracked_pids_contained(process, 4321)
            with mock.patch.object(apply.os, "getsid",
                                   side_effect=lambda pid: 999 if pid == 6789 else 4321):
                with self.assertRaisesRegex(RuntimeError, "backend.*脱离"):
                    apply.require_tracked_pids_contained(process, 4321)

    @mock.patch.object(apply.os, "getsid", return_value=123)
    def test_probe_can_bind_rollback_to_exact_existing_host_and_identity(self, _sid):
        identity = {"boot_id":"old-boot", "host_instance_id":"old-host",
                    "launch_generation":"old-generation"}
        desk = {"desk":True, "rev":"old", "pid":321, "host_pid":111, **identity}
        with mock.patch.object(apply.os, "getpgid", return_value=222), \
             mock.patch.object(apply, "process_parent", return_value=111):
            ok, _ = apply.probe_predicates(desk, {}, {}, expect_rev="old",
                expect_native_pid=111, port_owner_pid=321, expected_identity=identity)
            wrong, _ = apply.probe_predicates({**desk, "host_instance_id":"other"}, {}, {},
                expect_rev="old", expect_native_pid=111, port_owner_pid=321,
                expected_identity=identity)
        self.assertTrue(ok); self.assertFalse(wrong)

    @mock.patch.object(apply, '_process_parent_rows', return_value=[])
    def test_probe_rejects_orphan_backend_even_when_identity_matches(self, _rows):
        process = mock.Mock(pid=123, _desk_pgid=123, _desk_host_pid=111, _desk_log_path="")
        process.poll.return_value = 9
        desk = {"desk":True, "rev":"new", "pid":456, "host_pid":111}
        with mock.patch.object(apply, "endpoint", return_value=desk), \
             mock.patch.object(apply, "port_owner", return_value=456), \
             mock.patch.object(apply.os, "getpgid", return_value=123), \
             mock.patch.object(apply.os, "getsid", return_value=123), \
             mock.patch.object(apply, "active_time", side_effect=[0, .1]), \
             mock.patch.object(apply, "process_group_members", return_value=[(456, "S", "backend")]):
            ok, _ = apply.probe("new", timeout=1, expected_process=process)
        self.assertFalse(ok)

    @mock.patch.object(apply, 'port_owner', return_value=None)
    @mock.patch.object(apply, '_process_parent_rows', return_value=[])
    def test_probe_timeout_uses_awake_clock(self, _rows, _owner):
        clocks = iter([0.0, 0.1, 0.1, 0.2, 0.31])
        replies = iter([OSError("sleep interrupted"), {"desk":True, "rev":"new"}])
        def endpoint(_path):
            value = next(replies)
            if isinstance(value, Exception):
                raise value
            return value
        with mock.patch.object(apply, "active_time", side_effect=lambda: next(clocks)), \
             mock.patch.object(apply, "endpoint", side_effect=endpoint), \
             mock.patch.object(apply.time, "sleep"):
            ok, _ = apply.probe("new", timeout=.3)
        self.assertTrue(ok)

    @unittest.skipIf(os.name == 'nt', 'POSIX .app 宿主启动、inode 与进程组契约')
    def test_launch_log_records_pid_pgid_revision_build_hash_and_inode(self):
        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "Desktron.app"
            binary = app / "Contents/MacOS/desktron"
            resources = app / "Contents/Resources"
            binary.parent.mkdir(parents=True); resources.mkdir(parents=True)
            # Keep the fake host alive through identity registration without
            # spawning a new sleep process every 10 ms under the parallel gate.
            release = Path(td) / "release-test-host"
            binary.write_text("#!" + sys.executable + "\nimport os, time\n"
                              "while not os.path.exists(" + repr(str(release)) +
                              "): time.sleep(0.01)\n", encoding='utf-8')
            binary.chmod(0o755)
            (resources / "release-rev").write_text("host-rev", encoding='utf-8')
            (resources / "release-build").write_text("17", encoding='utf-8')
            log = Path(td) / "host.log"
            process = apply.launch_app_exact(str(app), str(log), "candidate")
            try:
                release.touch()
                # Use the production awake clock: sleep or a wall-clock change
                # must not consume the host-exit observation budget.
                deadline = apply.active_time() + 10
                while apply.alive(process._desk_host_pid) and apply.active_time() < deadline:
                    time.sleep(.01)
                self.assertFalse(apply.alive(process._desk_host_pid))
                self.assertIsNone(process.poll())
                self.assertEqual(os.getsid(process.pid), process.pid)
                text = log.read_text(encoding='utf-8')
                for field in ("purpose=candidate", "rev=host-rev", "build=17", "sha256=", "inode=", "pid=", "host_pid=", "pgid="):
                    self.assertIn(field, text)
            finally:
                apply.stop_exact(process, timeout=1)

    @unittest.skipIf(os.name == 'nt', 'POSIX .app 宿主进程组清理契约')
    def test_launch_initial_descendant_observation_failure_cleans_group(self):
        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "Desktron.app"
            binary = app / "Contents/MacOS/desktron"
            resources = app / "Contents/Resources"
            binary.parent.mkdir(parents=True); resources.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nsleep 60\n", encoding='utf-8'); binary.chmod(0o755)
            (resources / "release-rev").write_text("host-rev", encoding='utf-8')
            (resources / "release-build").write_text("17", encoding='utf-8')
            spawned = []
            real_popen = subprocess.Popen
            def capture(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                if kwargs.get("env", {}).get("DESKTRON_UPDATE_TRANSACTION") == "1":
                    spawned.append(process)
                return process
            with mock.patch.object(apply.subprocess, "Popen", side_effect=capture), \
                 mock.patch.object(apply, "observe_transaction_descendants",
                                   side_effect=RuntimeError("ps unavailable")):
                with self.assertRaisesRegex(RuntimeError, "ps unavailable"):
                    apply.launch_app_exact(str(app), str(Path(td) / "host.log"), "candidate")
            self.assertEqual(len(spawned), 1)
            spawned[0].wait(timeout=3)
            self.assertFalse(apply.process_group_alive(spawned[0].pid))

    @unittest.skipIf(os.name == 'nt', 'POSIX setsid/setpgid；Windows 使用 Job Object')
    def test_launch_accepts_and_stops_backend_that_owns_its_pgid(self):
        # Mirrors Foundation.Process: the backend leads its own PGID inside the host SID.
        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "Desktron.app"
            binary = app / "Contents/MacOS/desktron"
            resources = app / "Contents/Resources"
            binary.parent.mkdir(parents=True); resources.mkdir(parents=True)
            backend_file = Path(td) / "backend"
            code = ("import os,time,pathlib; os.setpgid(0,0); "
                    f"pathlib.Path({str(backend_file)!r}).write_text(str(os.getpid())); time.sleep(60)")
            binary.write_text(f"#!/bin/sh\n{sys.executable} -c {shlex.quote(code)} &\nwait\n", encoding='utf-8')
            binary.chmod(0o755)
            (resources / "release-rev").write_text("host-rev", encoding='utf-8')
            (resources / "release-build").write_text("17", encoding='utf-8')
            process = apply.launch_app_exact(str(app), str(Path(td) / "host.log"), "candidate")
            backend = host = 0
            try:
                deadline = time.monotonic() + 5
                while not (backend_file.exists() and backend_file.read_text(encoding='utf-8')):
                    self.assertLess(time.monotonic(), deadline); time.sleep(.05)
                backend = int(backend_file.read_text(encoding='utf-8')); host = process._desk_host_pid
                self.assertNotEqual(os.getpgid(backend), process._desk_pgid)
                _, checks = apply.probe_predicates({"pid":backend, "host_pid":host}, {}, {},
                    expect_pgid=process._desk_pgid, expect_native_pid=host, port_owner_pid=backend)
                self.assertTrue(checks["process_group"]); self.assertTrue(checks["native_parent"])
                process._desk_backend_pid = backend
                apply.observe_transaction_descendants(process)
                apply.stop_exact(process)
                self.assertFalse(apply.alive(host)); self.assertFalse(apply.alive(backend))
            finally:
                for pid in (backend, host, process.pid):
                    if pid > 1 and apply.alive(pid):
                        try: os.kill(pid, signal.SIGKILL)
                        except OSError: pass

    def test_legacy_stop_does_not_kill_reused_pid(self):
        tokens = iter(["old-start", "old-start", "new-start", "new-start",
                       "new-start", "new-start"])
        with mock.patch.object(apply, "process_start_token", side_effect=lambda _pid: next(tokens)), \
             mock.patch.object(apply, "active_time", side_effect=[0, .1, .2, .3]), \
             mock.patch.object(apply, "port_owner", return_value=0), \
             mock.patch.object(apply.os, "kill") as kill:
            apply.stop([4321])
        kill.assert_called_once_with(4321, signal.SIGTERM)

    @unittest.skipIf(os.name == 'nt', 'POSIX fork 与 killpg 真实用例')
    def test_stop_exact_kills_full_group_while_owned_leader_lives(self):
        with tempfile.TemporaryDirectory() as td:
            child_file = Path(td) / "child"
            code = ("import os,time,pathlib; p=os.fork(); "
                    f"pathlib.Path({str(child_file)!r}).write_text(str(p)) if p else time.sleep(60); "
                    "time.sleep(60) if p else None")
            process = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
            process._desk_pgid = process.pid
            process._desk_host_pid = process.pid
            process._desk_log_path = str(Path(td) / "host.log")
            deadline = time.time() + 3
            while not child_file.exists() and time.time() < deadline:
                time.sleep(.01)
            child = int(child_file.read_text(encoding='utf-8'))
            self.assertEqual(os.getpgid(child), process._desk_pgid)
            apply.stop_exact(process, timeout=1)
            process.wait(timeout=3)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)

    @unittest.skipIf(os.name == 'nt', 'POSIX detached session 身份与进程组收口')
    def test_stop_exact_kills_exact_detached_session_descendant(self):
        with tempfile.TemporaryDirectory() as td:
            child_file = Path(td) / "detached-child"
            code = ("import subprocess,sys,time,pathlib; "
                    "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], "
                    "start_new_session=True); "
                    f"pathlib.Path({str(child_file)!r}).write_text(str(p.pid)); time.sleep(60)")
            process = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
            process._desk_pgid = process.pid
            process._desk_host_pid = process.pid
            process._desk_log_path = str(Path(td) / "host.log")
            child = 0
            try:
                deadline = time.time() + 3
                while not child_file.exists() and time.time() < deadline:
                    time.sleep(.01)
                child = int(child_file.read_text(encoding='utf-8'))
                self.assertNotEqual(os.getsid(child), process._desk_pgid)
                apply.observe_transaction_descendants(process)
                apply.stop_exact(process, timeout=1)
                process.wait(timeout=3)
                deadline = time.time() + 3
                while apply.alive(child) and time.time() < deadline:
                    time.sleep(.01)
                self.assertFalse(apply.alive(child))
            finally:
                for pid in (child, process.pid):
                    if pid > 1 and apply.alive(pid):
                        try: os.kill(pid, signal.SIGKILL)
                        except OSError: pass

    def test_stop_exact_rejects_reused_pgid_after_leader_exit(self):
        process = mock.Mock(pid=4321, _desk_pgid=4321, _desk_log_path="")
        process.poll.return_value = 0
        with mock.patch.object(apply.os, "killpg") as killpg:
            with self.assertRaisesRegex(RuntimeError, "leader"):
                apply.stop_exact(process)
        killpg.assert_not_called()

    def test_stop_exact_rejects_tracked_host_that_escaped_the_session(self):
        process = mock.Mock(pid=4321, _desk_pgid=4321, _desk_host_pid=5678,
                            _desk_backend_pid=6789, _desk_log_path="")
        with mock.patch.object(apply, "owned_group_leader", return_value=True), \
             mock.patch.object(apply, "alive", return_value=True), \
             mock.patch.object(apply.os, "getpgid", side_effect=lambda pid: 999 if pid == 5678 else 4321), \
             mock.patch.object(apply.os, "getsid", return_value=4321), \
             mock.patch.object(apply.os, "killpg") as killpg:
            with self.assertRaisesRegex(RuntimeError, "host.*脱离"):
                apply.stop_exact(process)
        killpg.assert_not_called()

    @mock.patch.object(apply, 'port_owner', return_value=0)
    @mock.patch.object(apply, '_process_parent_rows', return_value=[])
    def test_stop_exact_revalidates_leader_immediately_before_signal(self, _rows, _owner):
        process = mock.Mock(pid=4321, _desk_pgid=4321, _desk_log_path="")
        with mock.patch.object(apply, "owned_group_leader", side_effect=[True, False]), \
             mock.patch.object(apply, "process_group_members",
                               return_value=[(4321, "S", "/safe/wrapper --arg")]), \
             mock.patch.object(apply.os, "killpg") as killpg:
            with self.assertRaisesRegex(RuntimeError, "TERM 前"):
                apply.stop_exact(process)
        killpg.assert_not_called()

    def test_process_diagnostics_redact_arguments_and_endpoint_details(self):
        self.assertEqual(apply.process_group_summary([
            (12, "S", "/usr/bin/python3 --token secret --user private")]),
            [(12, "S", "python3")])
        snapshot = apply.probe_snapshot({
            "desk": {"desk": True, "pid": 12, "secret": "no"},
            "update": {"host_rev": "abc", "download_url": "private"},
            "remote": {"local": True, "url": "private", "tailscale": {
                "enabled": True, "state": "ready", "address": "https://private/",
                "url": "https://private/?token=secret"}},
            "desk_error": "raw private error",
        })
        self.assertEqual(snapshot["desk"], {"desk": True, "pid": 12})
        self.assertEqual(snapshot["update"], {"host_rev": "abc"})
        self.assertEqual(snapshot["remote"], {"local": True, "tailscale_enabled": True,
            "tailscale_state": "ready", "tailscale_https": True})
        self.assertIs(snapshot["desk_error"], True)
        self.assertNotIn("private", json.dumps(snapshot))
        self.assertNotIn("secret", json.dumps(snapshot))

    def test_wait_backend_down_requires_stable_empty_port(self):
        owners = iter([0, 0, 777, 0, 0, 0])
        ticks = iter([0, .05, .1, .15, .2, .25, .3, .35, .4])
        with mock.patch.object(apply, "port_owner", side_effect=lambda: next(owners, 0)), \
             mock.patch.object(apply, "active_time", side_effect=lambda: next(ticks, .5)), \
             mock.patch.object(apply.time, "sleep"):
            self.assertTrue(apply.wait_backend_down(timeout=.5, stable_for=.1))

    def test_rollback_refuses_unknown_port_owner_without_spawning_or_signalling(self):
        with mock.patch.object(apply, "wait_backend_down", return_value=False), \
             mock.patch.object(apply, "port_owner", return_value=999), \
             mock.patch.object(apply, "launch_app_exact") as launch, \
             mock.patch.object(apply.os, "killpg") as killpg:
            ok, detail = apply.recover_native_host("/old.app", "/log", expect_rev="old",
                expect_host_rev="old-host", previous_identity={"boot_id":"b", "host_instance_id":"h",
                "launch_generation":"g"}, expect_remote_wss=False)
        self.assertFalse(ok); self.assertIn("999", detail)
        launch.assert_not_called(); killpg.assert_not_called()

    def test_sleep_inhibitor_requires_pmset_receipt_for_its_exact_pid(self):
        self.sleep_inhibitor.stop()
        process = mock.Mock(pid=321, poll=mock.Mock(return_value=None))
        receipt = "pid 321(caffeinate): PreventUserIdleSystemSleep PreventSystemSleep"
        with mock.patch.object(apply.sys, "platform", "darwin"), \
             mock.patch.object(apply.subprocess, "Popen", return_value=process), \
             mock.patch.object(apply.subprocess, "run", return_value=mock.Mock(stdout=receipt)), \
             mock.patch.object(apply, "active_time", side_effect=[0, .1]):
            self.assertIs(apply.acquire_sleep_inhibitor(), process)

    def test_sleep_inhibitor_exit_is_fail_closed_on_macos(self):
        self.sleep_inhibitor_alive.stop()
        try:
            process = mock.Mock()
            process.poll.return_value = 7
            with mock.patch.object(apply.sys, "platform", "darwin"):
                with self.assertRaisesRegex(RuntimeError, "已失效"):
                    apply.require_sleep_inhibitor(process)
        finally:
            self.sleep_inhibitor_alive = mock.patch.object(apply, "require_sleep_inhibitor")
            self.sleep_inhibitor_alive.start()

    def test_sleep_inhibitor_missing_exact_pmset_assertions_is_fail_closed(self):
        self.sleep_inhibitor_alive.stop()
        try:
            process = mock.Mock(pid=321)
            process.poll.return_value = None
            receipt = "pid 999(caffeinate): PreventUserIdleSystemSleep PreventSystemSleep"
            with mock.patch.object(apply.sys, "platform", "darwin"), \
                 mock.patch.object(apply.subprocess, "run", return_value=mock.Mock(stdout=receipt, returncode=0)):
                with self.assertRaisesRegex(RuntimeError, "已失效"):
                    apply.require_sleep_inhibitor(process)
        finally:
            self.sleep_inhibitor_alive = mock.patch.object(apply, "require_sleep_inhibitor")
            self.sleep_inhibitor_alive.start()

    def test_sleep_inhibitor_pmset_timeout_is_fail_closed(self):
        self.sleep_inhibitor_alive.stop()
        try:
            process = mock.Mock(pid=321)
            process.poll.return_value = None
            with mock.patch.object(apply.sys, "platform", "darwin"), \
                 mock.patch.object(apply.subprocess, "run",
                                   side_effect=subprocess.TimeoutExpired("pmset", 2)):
                with self.assertRaisesRegex(RuntimeError, "已失效"):
                    apply.require_sleep_inhibitor(process)
        finally:
            self.sleep_inhibitor_alive = mock.patch.object(apply, "require_sleep_inhibitor")
            self.sleep_inhibitor_alive.start()

    def test_probe_rechecks_guard_before_accepting_candidate(self):
        guard = mock.Mock(side_effect=[None, RuntimeError("inhibitor lost")])
        with mock.patch.object(apply, "active_time", side_effect=[0, .1]), \
             mock.patch.object(apply, "endpoint", return_value={"desk":True, "rev":"new"}), \
             mock.patch.object(apply, "port_owner", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "inhibitor lost"):
                apply.probe("new", timeout=1, guard=guard)
        self.assertEqual(guard.call_count, 2)

    def test_probe_stable_window_restarts_when_lifecycle_identity_changes(self):
        replies = iter([
            {"desk":True, "rev":"old", "boot_id":"boot-a", "host_instance_id":"host-a",
             "launch_generation":"gen-a"},
            {"desk":True, "rev":"old", "boot_id":"boot-b", "host_instance_id":"host-b",
             "launch_generation":"gen-b"},
            {"desk":True, "rev":"old", "boot_id":"boot-b", "host_instance_id":"host-b",
             "launch_generation":"gen-b"},
        ])
        ticks = iter([0, .01, .02, .03, .04, .1, .3])
        with mock.patch.object(apply, "active_time", side_effect=lambda: next(ticks)), \
             mock.patch.object(apply, "endpoint", side_effect=lambda _path: next(replies)), \
             mock.patch.object(apply, "port_owner", return_value=0), \
             mock.patch.object(apply.time, "sleep"):
            ok, _ = apply.probe("old", timeout=1, stable_for=.2)
        self.assertTrue(ok)

    def test_native_switch_acquires_sleep_inhibitor_before_ready(self):
        with tempfile.TemporaryDirectory() as td:
            base, state_path, _home, app_target = self._release_fixture(td)
            operation = "7" * 32
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state.update(operation_id=operation, previous_boot_id="old-boot",
                         previous_host_instance_id="old-host", previous_launch_generation="old-gen",
                         previous_host_rev="b" * 40, previous_host_build=9)
            state_path.write_text(json.dumps(state), encoding='utf-8')
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            events = []
            lock_fd = os.open(str(base / "lock"), os.O_CREAT | os.O_RDWR, 0o600)
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), \
                 mock.patch.object(apply, "prepare_managed_pi_extensions"), \
                 mock.patch.object(apply, "prepare_installed_providers"), \
                 mock.patch.object(apply, "endpoint", return_value={"local":True, "enabled":False}) as endpoint, \
                 mock.patch.object(apply, "acquire_sleep_inhibitor", side_effect=lambda _log: events.append("inhibit") or mock.Mock()), \
                 mock.patch.object(apply, "release_sleep_inhibitor"), \
                 mock.patch.object(apply, "signal_native_ready", side_effect=lambda _op: events.append("ready")), \
                 mock.patch.object(apply, "wait_native_host_exit"), \
                 mock.patch.object(apply, "acquire_lifecycle_lock", return_value=lock_fd), \
                 mock.patch.object(apply, "launch_app_exact", return_value=mock.Mock()), \
                 mock.patch.object(apply, "probe", return_value=(True, "healthy")):
                self.assertEqual(apply.apply(str(state_path), native_host_pid=4321, operation_id=operation), 0)
        self.assertEqual(events, ["inhibit", "ready"])
        self.assertIn(mock.call("/desk-lan-access/status"), endpoint.call_args_list)

    def test_main_preserves_uncertain_pending_state_on_unhandled_error(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            state_path.write_text(json.dumps({"state":"accepted_pending_commit",
                "operation_id":"op", "acceptance_unknown":True}), encoding='utf-8')
            with mock.patch.object(apply, "apply", side_effect=RuntimeError("unexpected")), \
                 mock.patch.object(apply.sys, "argv", ["apply", "--state", str(state_path)]):
                self.assertEqual(apply.main(), 1)
            saved = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertEqual(saved["state"], "accepted_pending_commit")
            self.assertTrue(saved["acceptance_unknown"])
            self.assertEqual(saved["operation_id"], "op")

    def test_native_helper_rejects_wrong_parent_before_writing_state(self):
        self.native_parent.stop()
        with tempfile.TemporaryDirectory() as td:
            _base, state_path, _home, _app = self._release_fixture(td)
            state = json.loads(state_path.read_text(encoding='utf-8')); state["operation_id"] = "x"
            state_path.write_text(json.dumps(state), encoding='utf-8'); before = state_path.read_bytes()
            with mock.patch.object(apply.os, "getppid", return_value=999), \
                 mock.patch.object(apply.sys, "argv", ["apply", "--state", str(state_path),
                     "--native-host-pid", "4321", "--operation-id", "x"]):
                self.assertEqual(apply.main(), 1)
            self.assertEqual(state_path.read_bytes(), before)

    def test_native_ready_must_be_consumed_before_waiting_for_host_exit(self):
        with tempfile.TemporaryDirectory() as td:
            _base, state_path, _home, app_target = self._release_fixture(td); operation = "7" * 32
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state.update(operation_id=operation, previous_boot_id="old-boot",
                         previous_host_instance_id="old-host", previous_launch_generation="old-gen",
                         previous_host_rev="b" * 40, previous_host_build=9)
            state_path.write_text(json.dumps(state), encoding='utf-8')
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            original_exists = os.path.exists
            self.ready_consumed.stop()
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), \
                 mock.patch.object(apply, "prepare_managed_pi_extensions"), \
                 mock.patch.object(apply, "endpoint", return_value={"local":True, "enabled":False}), \
                 mock.patch.object(apply, "signal_native_ready", return_value="/still/present"), \
                 mock.patch.object(apply.os.path, "exists", side_effect=lambda p: True if p == "/still/present" else original_exists(p)), \
                 mock.patch.object(apply, "active_time", side_effect=[0, 0, 6]), \
                 mock.patch.object(apply.os, "getppid", return_value=4321), \
                 mock.patch.object(apply, "probe", return_value=(True, "old healthy")), \
                 mock.patch.object(apply, "wait_native_host_exit") as wait_host:
                self.assertEqual(apply.apply(str(state_path), native_host_pid=4321, operation_id=operation), 1)
            wait_host.assert_not_called()

    def test_apply_switches_both_home_and_app_after_probe(self):
        with tempfile.TemporaryDirectory() as td:
            base, state_path, home, app_target = self._release_fixture(td)
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), \
                    mock.patch.object(apply, "prepare_managed_pi_extensions"), \
                    mock.patch.object(apply, "running_desk", return_value=(101, 102, str(app_target))), \
                    mock.patch.object(apply, "stop"), mock.patch.object(apply, "open_app"), \
                    mock.patch.object(apply, "probe", return_value=(True, "a" * 40)):
                self.assertEqual(apply.apply(str(state_path)), 0)
            self.assertEqual((home / "desktron" / "bin" / "desktron").read_text(encoding='utf-8'), "new-home")
            self.assertEqual((app_target / "Contents" / "MacOS" / "desktron").read_text(encoding='utf-8'), "new-app")
            self.assertEqual((base / ".codesign-requirement").read_text(encoding='utf-8').strip(),
                             self.STABLE_REQUIREMENT)
            saved = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertEqual(saved["state"], "idle")
            self.assertEqual(saved["completion_id"], "0" * 32)
            self.assertEqual(saved["completion_outcome"], "succeeded")
            self.assertGreater(saved["completed_at"], 0)

    def test_native_apply_waits_for_exact_host_and_never_scans_or_signals(self):
        with tempfile.TemporaryDirectory() as td:
            base, state_path, home, app_target = self._release_fixture(td)
            operation = "c" * 32
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state.update(operation_id=operation, previous_boot_id="old-boot",
                         previous_host_instance_id="old-host", previous_launch_generation="old-gen",
                         previous_host_rev="b" * 40, previous_host_build=9)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            lock_fd = os.open(str(base / "lifecycle.lock"), os.O_CREAT | os.O_RDWR, 0o600)
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), \
                    mock.patch.object(apply, "prepare_managed_pi_extensions"), \
                    mock.patch.object(apply, "prepare_installed_providers"), \
                    mock.patch.object(apply, "endpoint", return_value={"local":True, "enabled":False}), \
                    mock.patch.object(apply, "wait_native_host_exit") as wait_host, \
                    mock.patch.object(apply, "signal_native_ready") as ready, \
                    mock.patch.object(apply, "acquire_lifecycle_lock", return_value=lock_fd), \
                    mock.patch.object(apply, "launch_app_exact", return_value=mock.Mock()), \
                    mock.patch.object(apply, "probe", return_value=(True, "a" * 40)), \
                    mock.patch.object(apply, "running_desk") as running, \
                    mock.patch.object(apply, "stop") as stop:
                self.assertEqual(apply.apply(str(state_path), native_host_pid=4321,
                                             operation_id=operation), 0)
            wait_host.assert_called_once_with(4321)
            ready.assert_called_once_with(operation)
            running.assert_not_called(); stop.assert_not_called()
            self.assertEqual((home / "desktron" / "bin" / "desktron").read_text(encoding='utf-8'), "new-home")

    def test_failed_probe_restores_both_home_and_app(self):
        with tempfile.TemporaryDirectory() as td:
            base, state_path, home, app_target = self._release_fixture(td)
            probes = iter([(False, "wrong"), (True, "b" * 40)])
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), \
                    mock.patch.object(apply, "prepare_managed_pi_extensions"), \
                    mock.patch.object(apply, "running_desk", side_effect=[
                        (101, 102, str(app_target)), (201, 202, str(app_target))]), \
                    mock.patch.object(apply, "stop"), mock.patch.object(apply, "open_app"), \
                    mock.patch.object(apply, "probe", side_effect=lambda *_a, **_k: next(probes)):
                self.assertEqual(apply.apply(str(state_path)), 1)
            self.assertEqual((home / "old").read_text(encoding='utf-8'), "old-home")
            self.assertEqual((app_target / "old").read_text(encoding='utf-8'), "old-app")
            self.assertFalse((base / ".codesign-requirement").exists())
            saved = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertEqual(saved["state"], "rolled_back")
            self.assertEqual(saved["completion_id"], "0" * 32)
            self.assertEqual(saved["completion_outcome"], "failed")
            self.assertGreater(saved["completed_at"], 0)

    @unittest.skipIf(os.name == "nt", "directory fsync is a Unix durability contract")
    def test_identity_directory_sync_failure_restores_app_payload_and_marker(self):
        with tempfile.TemporaryDirectory() as td:
            base, state_path, home, app_target = self._release_fixture(td)
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            original_sync = apply.sync_parent_directory
            calls = 0
            def fail_first_sync(path):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("fixture directory fsync failed")
                return original_sync(path)
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), \
                    mock.patch.object(apply, "prepare_managed_pi_extensions"), \
                    mock.patch.object(apply, "running_desk", return_value=(101, 102, str(app_target))), \
                    mock.patch.object(apply, "stop"), mock.patch.object(apply, "open_app"), \
                    mock.patch.object(apply, "probe", return_value=(True, "b" * 40)), \
                    mock.patch.object(apply, "sync_parent_directory", side_effect=fail_first_sync):
                self.assertEqual(apply.apply(str(state_path)), 1)
            self.assertEqual((home / "old").read_text(encoding="utf-8"), "old-home")
            self.assertEqual((app_target / "old").read_text(encoding="utf-8"), "old-app")
            self.assertFalse((base / ".codesign-requirement").exists())
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["state"],
                             "rolled_back")

    def test_native_failed_probe_stops_only_launched_host_and_restores_both(self):
        with tempfile.TemporaryDirectory() as td:
            base, state_path, home, app_target = self._release_fixture(td)
            operation = "e" * 32
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state.update(operation_id=operation, previous_boot_id="old-boot",
                         previous_host_instance_id="old-host", previous_launch_generation="old-gen",
                         previous_host_rev="b" * 40, previous_host_build=9)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            lock_fd = os.open(str(base / "lifecycle.lock"), os.O_CREAT | os.O_RDWR, 0o600)
            new_host, old_host = mock.Mock(), mock.Mock()
            probes = iter([(False, "wrong"), (True, "b" * 40)])
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), \
                    mock.patch.object(apply, "prepare_managed_pi_extensions"), \
                    mock.patch.object(apply, "prepare_installed_providers"), \
                    mock.patch.object(apply, "endpoint", return_value={"local":True, "enabled":False,
                        "tailscale":{"enabled":True, "state":"ready", "address":"https://desk.test/"}}), \
                    mock.patch.object(apply, "wait_native_host_exit"), \
                    mock.patch.object(apply, "signal_native_ready"), \
                    mock.patch.object(apply, "acquire_lifecycle_lock", return_value=lock_fd), \
                    mock.patch.object(apply, "launch_app_exact", side_effect=[new_host, old_host]) as launch, \
                    mock.patch.object(apply, "stop_exact") as stop_exact, \
                    mock.patch.object(apply, "wait_backend_down"), \
                    mock.patch.object(apply, "probe", side_effect=lambda *_a, **_k: next(probes)) as probe, \
                    mock.patch.object(apply, "running_desk") as running:
                self.assertEqual(apply.apply(str(state_path), native_host_pid=4321,
                                             operation_id=operation), 1)
            stop_exact.assert_called_once_with(new_host); running.assert_not_called()
            self.assertEqual(launch.call_count, 2)
            self.assertTrue(probe.call_args_list[0].kwargs["expect_remote_wss"])
            self.assertTrue(probe.call_args_list[1].kwargs["expect_remote_wss"])
            self.assertEqual((home / "old").read_text(encoding='utf-8'), "old-home")
            self.assertEqual((app_target / "old").read_text(encoding='utf-8'), "old-app")

    def test_success_state_failure_after_release_never_rolls_back_live_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            base, state_path, _home, app_target = self._release_fixture(td)
            operation = "3" * 32
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state.update(operation_id=operation, previous_boot_id="old-boot",
                         previous_host_instance_id="old-host", previous_launch_generation="old-gen",
                         previous_host_rev="b" * 40, previous_host_build=9)
            state_path.write_text(json.dumps(state), encoding='utf-8')
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            lock_fd = os.open(str(base / "lock"), os.O_CREAT | os.O_RDWR, 0o600)
            process = mock.Mock()
            original_patch = apply.state_patch
            def fail_idle(path, value, **patch):
                if patch.get("state") == "idle":
                    raise OSError("persist failed")
                return original_patch(path, value, **patch)
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), \
                 mock.patch.object(apply, "prepare_managed_pi_extensions"), \
                 mock.patch.object(apply, "prepare_installed_providers"), \
                 mock.patch.object(apply, "endpoint", return_value={"local":True, "enabled":False}), \
                 mock.patch.object(apply, "signal_native_ready"), \
                 mock.patch.object(apply, "wait_native_host_exit"), \
                 mock.patch.object(apply, "acquire_lifecycle_lock", return_value=lock_fd), \
                 mock.patch.object(apply, "launch_app_exact", return_value=process), \
                 mock.patch.object(apply, "probe", return_value=(True, "healthy")), \
                 mock.patch.object(apply, "state_patch", side_effect=fail_idle), \
                 mock.patch.object(apply, "stop_exact") as stop_exact:
                self.assertEqual(apply.apply(str(state_path), native_host_pid=4321,
                                             operation_id=operation), 1)
            apply.release_exact.assert_called_once_with(process)
            stop_exact.assert_not_called()
            saved = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertEqual(saved["state"], "verifying")
            self.assertEqual(saved["pending_protocol"], 2)
            self.assertEqual(saved["pending_outcome"], "idle")
            self.assertTrue(saved["acceptance_unknown"])
            self.assertEqual(saved["operation_id"], operation)

    def test_partial_directory_replace_failure_cannot_report_rolled_back(self):
        with tempfile.TemporaryDirectory() as td:
            base, state_path, _home, app_target = self._release_fixture(td)
            operation = "2" * 32
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state.update(operation_id=operation, previous_boot_id="old-boot",
                         previous_host_instance_id="old-host", previous_launch_generation="old-gen",
                         previous_host_rev="b" * 40, previous_host_build=9)
            state_path.write_text(json.dumps(state), encoding='utf-8')
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            lock_fd = os.open(str(base / "lock"), os.O_CREAT | os.O_RDWR, 0o600)
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), \
                 mock.patch.object(apply, "prepare_managed_pi_extensions"), \
                 mock.patch.object(apply, "endpoint", return_value={"local":True, "enabled":False}), \
                 mock.patch.object(apply, "signal_native_ready"), \
                 mock.patch.object(apply, "wait_native_host_exit"), \
                 mock.patch.object(apply, "acquire_lifecycle_lock", return_value=lock_fd), \
                 mock.patch.object(apply, "replace_dir", side_effect=[None, OSError("app replace failed")]), \
                 mock.patch.object(apply, "restore_dir", side_effect=[OSError("app restore failed"), None]), \
                 mock.patch.object(apply, "launch_app_exact") as launch:
                self.assertEqual(apply.apply(str(state_path), native_host_pid=4321,
                                             operation_id=operation), 1)
            saved = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertEqual(saved["state"], "rollback_failed")
            self.assertTrue(saved["acceptance_unknown"])
            self.assertEqual(saved["operation_id"], operation)
            launch.assert_not_called()

    def test_candidate_cleanup_failure_stops_rollback_and_keeps_operation(self):
        with tempfile.TemporaryDirectory() as td:
            base, state_path, home, app_target = self._release_fixture(td)
            operation = "5" * 32
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state.update(operation_id=operation, previous_boot_id="old-boot",
                         previous_host_instance_id="old-host", previous_launch_generation="old-gen",
                         previous_host_rev="b" * 40, previous_host_build=9)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            lock_fd = os.open(str(base / "lifecycle.lock"), os.O_CREAT | os.O_RDWR, 0o600)
            new_host = mock.Mock()
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), \
                 mock.patch.object(apply, "prepare_managed_pi_extensions"), \
                 mock.patch.object(apply, "prepare_installed_providers"), \
                 mock.patch.object(apply, "endpoint", return_value={"local":True, "enabled":False}), \
                 mock.patch.object(apply, "wait_native_host_exit"), \
                 mock.patch.object(apply, "signal_native_ready"), \
                 mock.patch.object(apply, "acquire_lifecycle_lock", return_value=lock_fd), \
                 mock.patch.object(apply, "launch_app_exact", return_value=new_host) as launch, \
                 mock.patch.object(apply, "stop_exact", side_effect=RuntimeError("cleanup uncertain")), \
                 mock.patch.object(apply, "probe", return_value=(False, "wrong")):
                self.assertEqual(apply.apply(str(state_path), native_host_pid=4321,
                                             operation_id=operation), 1)
            saved = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(saved["state"], "rollback_failed")
            self.assertEqual(saved["operation_id"], operation)
            self.assertTrue(saved["acceptance_unknown"])
            self.assertTrue((app_target / "Contents/MacOS/desktron").exists())
            self.assertFalse((home / "old").exists())

    def test_lock_failure_after_native_exit_relaunches_old_host(self):
        with tempfile.TemporaryDirectory() as td:
            base, state_path, _home, app_target = self._release_fixture(td); operation = "f" * 32
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state.update(operation_id=operation, previous_boot_id="old-boot",
                         previous_host_instance_id="old-host", previous_launch_generation="old-gen",
                         previous_host_rev="b" * 40, previous_host_build=9)
            state_path.write_text(json.dumps(state), encoding='utf-8')
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), \
                 mock.patch.object(apply, "prepare_managed_pi_extensions"), mock.patch.object(apply, "endpoint", return_value={"local":True, "enabled":False}), \
                 mock.patch.object(apply, "signal_native_ready"), mock.patch.object(apply, "wait_native_host_exit"), \
                 mock.patch.object(apply, "acquire_lifecycle_lock", side_effect=RuntimeError("lock timeout")), \
                 mock.patch.object(apply, "alive", return_value=False), mock.patch.object(apply, "wait_backend_down", return_value=True), \
                 mock.patch.object(apply, "launch_app_exact", return_value=mock.Mock()) as launch, \
                 mock.patch.object(apply, "probe", return_value=(True, "old healthy")):
                self.assertEqual(apply.apply(str(state_path), native_host_pid=4321, operation_id=operation), 1)
            self.assertEqual(launch.call_args.args[2], "rollback-1")
            self.assertEqual(json.loads(state_path.read_text(encoding='utf-8'))["state"], "rolled_back")

    def test_rollback_launch_retries_after_spawn_failure(self):
        process = mock.Mock(); before_release = mock.Mock()
        with mock.patch.object(apply, "wait_backend_down", return_value=True), \
             mock.patch.object(apply.time, "sleep"), \
             mock.patch.object(apply, "launch_app_exact", side_effect=[OSError("spawn"), process]) as launch, \
             mock.patch.object(apply, "probe", return_value=(True, "healthy")) as probe, \
             mock.patch.object(apply, "stop_exact") as stop:
            ok, _ = apply.recover_native_host("/old.app", "/log", expect_rev="old",
                expect_host_rev="old-host", previous_identity={"boot_id":"b", "host_instance_id":"h",
                "launch_generation":"g"}, expect_remote_wss=True,
                before_release=before_release)
        self.assertTrue(ok); self.assertEqual(launch.call_count, 2); stop.assert_not_called()
        self.assertEqual(probe.call_args.kwargs["stable_for"], .5)
        before_release.assert_called_once_with(process)

    def test_rollback_stops_after_process_cleanup_failure(self):
        process = mock.Mock()
        with mock.patch.object(apply, "wait_backend_down", return_value=True), \
             mock.patch.object(apply, "launch_app_exact", return_value=process) as launch, \
             mock.patch.object(apply, "probe", return_value=(False, "unhealthy")), \
             mock.patch.object(apply, "stop_exact", side_effect=RuntimeError("cleanup uncertain")):
            ok, detail = apply.recover_native_host("/old.app", "/log", expect_rev="old",
                expect_host_rev="old-host", previous_identity={"boot_id":"b", "host_instance_id":"h",
                "launch_generation":"g"}, expect_remote_wss=True)
        self.assertFalse(ok); self.assertIn("cleanup uncertain", detail)
        self.assertEqual(launch.call_count, 1)

    def test_native_remote_preflight_failure_does_not_stop_old_host(self):
        with tempfile.TemporaryDirectory() as td:
            base, state_path, _home, app_target = self._release_fixture(td); operation = "9" * 32
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state.update(operation_id=operation, previous_boot_id="old-boot",
                         previous_host_instance_id="old-host", previous_launch_generation="old-gen",
                         previous_host_rev="b" * 40, previous_host_build=9)
            state_path.write_text(json.dumps(state), encoding='utf-8')
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), \
                 mock.patch.object(apply, "prepare_managed_pi_extensions"), \
                 mock.patch.object(apply, "endpoint", side_effect=OSError("remote unavailable")), \
                 mock.patch.object(apply, "signal_native_ready") as ready, \
                 mock.patch.object(apply, "wait_native_host_exit") as wait_host, \
                 mock.patch.object(apply.os, "getppid", return_value=4321), \
                 mock.patch.object(apply, "probe", return_value=(True, "old healthy")) as probe:
                self.assertEqual(apply.apply(str(state_path), native_host_pid=4321, operation_id=operation), 1)
            ready.assert_not_called(); wait_host.assert_not_called()
            probe.assert_not_called()
            saved = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertEqual(saved["state"], "error")
            self.assertFalse(saved["acceptance_unknown"])
            self.assertTrue((app_target / "old").exists())

    def test_sleep_inhibitor_cleanup_never_overwrites_transaction_result(self):
        process = mock.Mock(pid=321)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("caffeinate", 2), RuntimeError("wait failed")]
        with mock.patch.object(apply, "_append_host_log", side_effect=OSError("log failed")):
            apply.release_sleep_inhibitor(process, "/unwritable/log")
        process.terminate.assert_called_once()
        process.kill.assert_called_once()

    def test_published_ready_cannot_accept_still_healthy_host_as_rolled_back(self):
        with tempfile.TemporaryDirectory() as td:
            _base, state_path, _home, app_target = self._release_fixture(td); operation = "4" * 32
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state.update(operation_id=operation, previous_boot_id="old-boot",
                         previous_host_instance_id="old-host", previous_launch_generation="old-gen",
                         previous_host_rev="b" * 40, previous_host_build=9)
            state_path.write_text(json.dumps(state), encoding='utf-8')
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), \
                 mock.patch.object(apply, "prepare_managed_pi_extensions"), \
                 mock.patch.object(apply, "endpoint", return_value={"local":True, "enabled":False}), \
                 mock.patch.object(apply, "signal_native_ready", side_effect=OSError("ready write uncertain")), \
                 mock.patch.object(apply.os, "getppid", return_value=4321), \
                 mock.patch.object(apply, "probe", return_value=(True, "old host healthy")) as probe:
                self.assertEqual(apply.apply(str(state_path), native_host_pid=4321,
                                             operation_id=operation), 1)
            saved = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertEqual(saved["state"], "rollback_failed")
            self.assertTrue(saved["acceptance_unknown"])
            self.assertEqual(saved["operation_id"], operation)
            probe.assert_not_called()

    def test_reused_old_host_pid_does_not_suppress_rollback_launch(self):
        with tempfile.TemporaryDirectory() as td:
            base, state_path, _home, app_target = self._release_fixture(td); operation = "6" * 32
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state.update(operation_id=operation, previous_boot_id="old-boot",
                         previous_host_instance_id="old-host", previous_launch_generation="old-gen",
                         previous_host_rev="b" * 40, previous_host_build=9)
            state_path.write_text(json.dumps(state), encoding='utf-8')
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            process = mock.Mock()
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), \
                 mock.patch.object(apply, "prepare_managed_pi_extensions"), \
                 mock.patch.object(apply, "endpoint", return_value={"local":True, "enabled":False}), \
                 mock.patch.object(apply, "signal_native_ready"), \
                 mock.patch.object(apply, "wait_native_host_exit"), \
                 mock.patch.object(apply, "acquire_lifecycle_lock", side_effect=RuntimeError("lock timeout")), \
                 mock.patch.object(apply.os, "getppid", return_value=1), \
                 mock.patch.object(apply, "alive", return_value=True), \
                 mock.patch.object(apply, "wait_backend_down", return_value=True), \
                 mock.patch.object(apply, "launch_app_exact", return_value=process) as launch, \
                 mock.patch.object(apply, "probe", return_value=(True, "old healthy")):
                self.assertEqual(apply.apply(str(state_path), native_host_pid=4321, operation_id=operation), 1)
            self.assertEqual(launch.call_args.args[2], "rollback-1")

    def test_exhausted_rollback_stays_failed_and_keeps_operation_identity(self):
        with tempfile.TemporaryDirectory() as td:
            base, state_path, _home, app_target = self._release_fixture(td); operation = "8" * 32
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state.update(operation_id=operation, previous_boot_id="old-boot",
                         previous_host_instance_id="old-host", previous_launch_generation="old-gen",
                         previous_host_rev="b" * 40, previous_host_build=9)
            state_path.write_text(json.dumps(state), encoding='utf-8')
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), \
                 mock.patch.object(apply, "prepare_managed_pi_extensions"), \
                 mock.patch.object(apply, "endpoint", return_value={"local":True, "enabled":False}), \
                 mock.patch.object(apply, "signal_native_ready"), mock.patch.object(apply, "wait_native_host_exit"), \
                 mock.patch.object(apply, "acquire_lifecycle_lock", side_effect=RuntimeError("lock timeout")), \
                 mock.patch.object(apply, "alive", return_value=False), \
                 mock.patch.object(apply, "wait_backend_down", return_value=True), \
                 mock.patch.object(apply, "launch_app_exact", side_effect=OSError("spawn")) as launch, \
                 mock.patch.object(apply.time, "sleep"):
                self.assertEqual(apply.apply(str(state_path), native_host_pid=4321, operation_id=operation), 1)
            saved = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertEqual(launch.call_count, 8); self.assertEqual(saved["state"], "rollback_failed")
            self.assertEqual(saved["operation_id"], operation); self.assertTrue(saved["acceptance_unknown"])

    def test_host_only_switch_does_not_touch_home_or_delete_stage(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); updates = base / "updates"; stage = updates / "dev-host.12345678"
            app = stage / "Desktron.app/Contents"; (app / "MacOS").mkdir(parents=True); (app / "Resources").mkdir()
            binary = app / "MacOS/desktron"; binary.write_text("new-host", encoding='utf-8')
            (app / "Resources/release-rev").write_text("c" * 40, encoding='utf-8')
            (app / "Resources/release-build").write_text("2", encoding='utf-8')
            home = base / "home"; home.mkdir(); (home / "keep").write_text("payload", encoding='utf-8')
            app_target = base / "Applications/Desktron.app"; app_target.mkdir(parents=True); (app_target / "old").write_text("old", encoding='utf-8')
            (base / ".update-rev").write_text("payload:" + "a" * 64, encoding='utf-8'); (base / ".update-build").write_text("0", encoding='utf-8')
            operation = "e" * 32; state_path = updates / "state.json"
            state_path.write_text(json.dumps({"state":"applying", "mode":"host-only", "operation_id":operation,
                "helper_contract":2, "helper_sha256":apply.sha256(apply.__file__),
                "staged_app":str(stage), "staged_rev":"payload:" + "a" * 64, "staged_build":0,
                "staged_host_rev":"c" * 40, "staged_host_build":2,
                "staged_host_sha256":hashlib.sha256(binary.read_bytes()).hexdigest(),
                "previous_boot_id":"old-boot", "previous_host_instance_id":"old-host",
                "previous_launch_generation":"old-gen", "previous_host_rev":"b" * 40,
                "previous_host_build":9}), encoding='utf-8')
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            lock_fd = os.open(str(base / "lock"), os.O_CREAT | os.O_RDWR, 0o600)
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), mock.patch.object(apply, "endpoint", return_value={"local":True, "enabled":False}), \
                 mock.patch.object(apply, "signal_native_ready"), mock.patch.object(apply, "wait_native_host_exit"), \
                 mock.patch.object(apply, "acquire_lifecycle_lock", return_value=lock_fd), \
                 mock.patch.object(apply, "launch_app_exact", return_value=mock.Mock()), \
                 mock.patch.object(apply, "probe", return_value=(True, "healthy")):
                self.assertEqual(apply.apply(str(state_path), native_host_pid=4321, operation_id=operation), 0)
            self.assertEqual((home / "keep").read_text(encoding='utf-8'), "payload"); self.assertTrue(stage.exists())

    def test_managed_extension_failure_happens_before_stopping_or_switching(self):
        with tempfile.TemporaryDirectory() as td:
            _base, state_path, home, app_target = self._release_fixture(td)
            expand = lambda p: str(app_target) if p == "~/Applications/Desktron.app" else os.path.expanduser(p)
            with mock.patch.object(apply.os.path, "expanduser", side_effect=expand), \
                    mock.patch.object(apply, "prepare_managed_pi_extensions",
                                      side_effect=RuntimeError("dependency unavailable")), \
                    mock.patch.object(apply, "running_desk") as running, \
                    mock.patch.object(apply, "stop") as stop, \
                    mock.patch.object(apply, "replace_dir") as replace:
                with self.assertRaisesRegex(RuntimeError, "dependency unavailable"):
                    apply.apply(str(state_path))
            running.assert_not_called()
            stop.assert_not_called()
            replace.assert_not_called()
            self.assertEqual((home / "old").read_text(encoding='utf-8'), "old-home")
            self.assertEqual((app_target / "old").read_text(encoding='utf-8'), "old-app")


class DevelopmentAppClient(unittest.TestCase):
    def test_exact_executable_match_and_ps_failure_is_not_safe(self):
        binary = "/Applications/Desktron.app/Contents/MacOS/desktron"
        with mock.patch.object(dev_app, "app_binary", return_value=binary), \
             mock.patch.object(dev_app.subprocess, "run", return_value=mock.Mock(
                 returncode=0, stdout=binary + " --flag\n/other/Desktron.app/Contents/MacOS/desktron\n")):
            self.assertTrue(dev_app.is_running_app("/Applications/Desktron.app"))
        with mock.patch.object(dev_app.subprocess, "run", side_effect=TimeoutError("ps timeout")):
            with self.assertRaisesRegex(RuntimeError, "无法确认"):
                dev_app.is_running_app("/Applications/Desktron.app")

    def test_launcher_preserves_stage_after_submission_and_guards_any_target(self):
        launcher = (ROOT / "desktron/bin/desktron").read_text(encoding="utf-8")
        submit = launcher[launcher.index('if $ACTIVATE; then', launcher.index('case "$IMPL"')):]
        self.assertLess(submit.index('ACTIVATE_STAGE=""'), submit.index('dev_app_update.py" activate'))
        self.assertIn('if [[ -e "$APPDIR" ]]', launcher)
        self.assertIn('*) echo "desktron: 无法安全确认', launcher)

    def test_release_and_apply_do_not_modify_signed_app_after_copy(self):
        release = (ROOT/'scripts/dev/release.sh').read_text(encoding='utf-8')
        updater = (LIB/'self_update_apply.py').read_text(encoding='utf-8')
        self.assertIn('"signed": true', release)
        self.assertIn('verify-compatible \"$_APP_NEW\" \"$_CURRENT_APP\"', release)
        self.assertIn('--requirement-marker \"$_MARKER_SIGNATURE\"', release)
        self.assertIn('record-requirement \\', release)
        self.assertIn('for _marker in rev build signature; do', release)
        self.assertIn("verify_stable_app(os.path.join(root, 'Desktron.app'))", release)
        self.assertLess(release.index('verify-compatible \"$_APP_SOURCE\"'),
                        release.index('rsync -a --delete \"$HERE/payload/\"'))
        self.assertLess(release.index('install-check'), release.index('_committed=true'))
        self.assertNotIn('open(os.path.join(app_next, "Contents", "Resources", "desktron_home")', updater)
        self.assertIn('verify_stable_app(app_next)', updater)

    @unittest.skipIf(os.name == 'nt', 'macOS shell installer requires POSIX symlinks and rsync; Windows installer tested separately')
    def test_release_installer_rolls_back_payload_app_and_markers_after_validation_failure(self):
        release = (ROOT/'scripts/dev/release.sh').read_text(encoding='utf-8')
        marker = "cat > \"$STAGE/install.sh\" <<'INSTALL'\n"
        installer = release.split(marker, 1)[1].split('\nINSTALL\n', 1)[0]
        for fail_on, expected_code in (('prepare',19), ('install-check',19), ('app-move',23)):
            with self.subTest(fail_on=fail_on), tempfile.TemporaryDirectory() as folder:
                base=Path(folder);home=base/'user';package=base/'package';payload=package/'payload'
                localbin=home/'.local/bin';localbin.mkdir(parents=True)
                commands={
                    'uname':'#!/bin/sh\necho Darwin\n',
                    'python3':'#!/bin/sh\nexit 0\n',
                }
                if fail_on == 'app-move':
                    commands['mv']=('#!/bin/sh\ncase "$2" in */.Desktron.app.prev) exit 23;; esac\n'
                                    'exec /bin/mv "$@"\n')
                for name, body in commands.items():
                    path=localbin/name;path.write_text(body,encoding='utf-8');path.chmod(0o755)
                runtime=payload/'desktron/lib/provision/python_runtime.sh'
                runtime.parent.mkdir(parents=True)
                runtime.write_text('desk_python_ensure() { :; }\n',encoding='utf-8')
                (payload/'desktron/lib/macos_codesign.py').write_text('fixture\n',encoding='utf-8')
                command=payload/'desktron/bin/desktron';command.parent.mkdir(parents=True)
                command.write_text(
                    '#!/bin/sh\n[ "$1" = '+shlex.quote(fail_on)+' ] && exit 19\nexit 0\n',
                    encoding='utf-8');command.chmod(0o755)
                (payload/'new-payload').write_text('new',encoding='utf-8')
                (payload/'.release-rev').write_text('new-rev',encoding='utf-8')
                (payload/'.release-build').write_text('22',encoding='utf-8')
                app=package/'Desktron.app';app.mkdir(parents=True)
                (app/'new-app').write_text('new',encoding='utf-8')
                installed=home/'.local/share/desktron/home';installed.mkdir(parents=True)
                (installed/'old-payload').write_text('old',encoding='utf-8')
                applications=home/'Applications/Desktron.app';applications.mkdir(parents=True)
                (applications/'old-app').write_text('old',encoding='utf-8')
                data=home/'.local/share/desktron'
                (data/'.update-rev').write_text('old-rev',encoding='utf-8')
                (data/'.update-build').write_text('11',encoding='utf-8')
                (data/'.codesign-requirement').write_text('old-identity',encoding='utf-8')
                script=package/'install.sh';script.write_text(installer,encoding='utf-8');script.chmod(0o755)
                env={**os.environ,'HOME':str(home),'SHELL':'/bin/bash',
                     'PATH':str(localbin)+os.pathsep+os.environ.get('PATH',''),
                     'DESKTRON_DATA_HOME':str(data/'data')}
                result=subprocess.run(['bash',str(script)],env=env,capture_output=True,text=True,
                                      encoding='utf-8',timeout=30,check=False)
                self.assertEqual(result.returncode,expected_code,result.stdout+result.stderr)
                self.assertEqual((installed/'old-payload').read_text(encoding='utf-8'),'old')
                self.assertFalse((installed/'new-payload').exists())
                self.assertEqual((applications/'old-app').read_text(encoding='utf-8'),'old')
                self.assertFalse((applications/'new-app').exists())
                self.assertEqual((data/'.update-rev').read_text(encoding='utf-8'),'old-rev')
                self.assertEqual((data/'.update-build').read_text(encoding='utf-8'),'11')
                self.assertEqual((data/'.codesign-requirement').read_text(encoding='utf-8'),
                                 'old-identity')


class Wiring(unittest.TestCase):
    def test_http_routes_and_background_start_are_wired(self):
        api = (LIB / "provision" / "api.py").read_text(encoding="utf-8")
        server = (LIB / "ui_server.py").read_text(encoding="utf-8")
        for path in ("/workbench/update-check", "/workbench/update/check",
                     "/workbench/update/download", "/workbench/update/apply",
                     "/workbench/update/dev-app-apply", "/workbench/update/activity"):
            self.assertIn(path, api)
        self.assertIn('__import__("self_update").start_background()', server)

    def test_frontend_reports_activity_and_confirms_apply(self):
        init = (ROOT / "frontend" / "app-init.js").read_text(encoding="utf-8")
        settings = (ROOT / "frontend" / "app-settings-sections.js").read_text(encoding="utf-8")
        self.assertIn("/workbench/update/activity", init)
        self.assertIn("/workbench/update/apply", settings)
        self.assertIn("重启会中断所有会话", settings)
        self.assertIn("_pollUpdateStatus(st)", settings)
        self.assertIn("}, 1000)", settings)

    def test_session_and_arrangement_entries_share_drain_gate(self):
        contract = (LIB / "ui_contract.py").read_text(encoding="utf-8")
        scheduler = (LIB / "arrangement" / "scheduler.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(contract.count("self_update.admit_work"), 2)
        self.assertIn("self_update.admit_work", scheduler)
        self.assertIn("def has_running():", scheduler)
        handler = (LIB / "larkbot" / "handler.py").read_text(encoding="utf-8")
        self.assertIn("self_update.tracked_work", handler)
        session_api = (LIB / "session" / "api.py").read_text(encoding="utf-8")
        self.assertIn("self_update.admit_work", session_api)
        memory = (LIB / "memory_scheduler.py").read_text(encoding="utf-8")
        remote = (LIB / "remote" / "api.py").read_text(encoding="utf-8")
        self.assertIn("self_update.tracked_work", memory)
        self.assertIn("memory_scheduler.dream_tick()", remote)
        self.assertNotIn('agent == "lark"', remote)

    def test_fixed_apply_helper_bootstraps_product_lib_without_pythonpath(self):
        with tempfile.TemporaryDirectory() as folder:
            helper = Path(folder) / "self_update_apply.py"
            helper.write_bytes((LIB / "self_update_apply.py").read_bytes())
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env["DESKTRON_CANONICAL_HOME"] = str(ROOT)
            result = subprocess.run(
                [sys.executable, str(helper), "--describe-contract"], cwd=folder,
                env=env, capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            metadata = json.loads(result.stdout)
            self.assertEqual(metadata["helper_contract"], 2)
            self.assertEqual(metadata["sha256"], hashlib.sha256(helper.read_bytes()).hexdigest())

    def test_apply_script_is_copied_outside_replaceable_home(self):
        source = (LIB / "self_update.py").read_text(encoding="utf-8")
        self.assertIn('script = os.path.join(_updates_dir(), "self_update_apply.py")', source)
        self.assertIn('os.replace(script_tmp, script)', source)
        self.assertIn('pass_fds=(update_lock,)', source)

    def test_running_app_requires_explicit_transaction_and_stage_is_never_deleted_from_state(self):
        launcher = (ROOT / "desktron/bin/desktron").read_text(encoding="utf-8")
        updater = (LIB / "self_update_apply.py").read_text(encoding="utf-8")
        swift = (ROOT / "desktop/native/main.swift").read_text(encoding="utf-8")
        helper = swift[swift.index("func launchUpdateHelper"):swift.index("func launchServer")]
        self.assertIn("app-install --activate", launcher)
        self.assertIn('if [[ "${1:-}" == "--activate" ]]', launcher)
        self.assertNotIn('if [[ "${2:-}" == "--activate" ]]', launcher)
        self.assertIn("dev_app_update.py\" running", launcher)
        self.assertNotIn('rmtree(os.path.realpath(str(state.get("staged_app")', updater)
        self.assertIn("seekToEndOfFile", helper)
        self.assertNotIn("truncateFile", helper)


if __name__ == "__main__":
    unittest.main()
