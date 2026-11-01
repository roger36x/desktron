"""接入记录存于临时目录；不登录飞书、不创建真实应用或终端。"""
import importlib.util
import contextlib
import io
import json
import os
from _platform import assert_private_file
import sys
import runpy
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from larkbot import onboarding as jobs


class Onboarding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.live = set()
        self.commands = []
        def launch(**kw):
            self.commands.append(kw["command"])
            sid = "test-terminal-" + str(len(self.commands))
            self.live.add(sid)
            return {"id": sid}
        modules = {"term": types.SimpleNamespace(_shell_term_open=launch),
                   "deskcfg": types.SimpleNamespace(UI_PORT=17765),
                   "termpty": types.SimpleNamespace(
                       pty_list=lambda: [{"id": sid, "alive": True} for sid in self.live],
                       pty_kill=lambda sid: self.live.discard(sid))}
        for p in (patch.object(jobs, "ROOT", Path(self.tmp.name)), patch.dict(sys.modules, modules)):
            p.start(); self.addCleanup(p.stop)

    def test_double_click_reuses_job_and_process(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: jobs.start("server"), range(5)))
        self.assertEqual(len({j["id"] for j in results}), 1)
        self.assertEqual(len(self.commands), 1)
        self.assertIn("--backend server", self.commands[0])
        self.assertIn("--port 17765", self.commands[0])

    def test_refresh_rediscovers_running_job_without_launch(self):
        job = jobs.start("server")
        jobs.checkpoint(job["id"], job["generation"], app_id="cli_test_app", name="Example")
        restored = jobs.listing()[0]
        self.assertEqual(restored["terminal"], job["terminal"])
        self.assertEqual(restored["app_id"], "cli_test_app")
        self.assertTrue(restored["alive"])
        self.assertEqual(len(self.commands), 1)

    def test_process_loss_resumes_known_application_and_target(self):
        first = jobs.start("server")
        jobs.checkpoint(first["id"], first["generation"], create_requested=True, app_id="cli_test_app")
        self.live.clear()
        self.assertEqual(jobs.get(first["id"])["phase"], "interrupted")
        second = jobs.start(jid=first["id"])
        self.assertEqual(second["backend"], "server")
        self.assertEqual(second["app_id"], "cli_test_app")
        self.assertEqual(second["generation"], first["generation"] + 1)
        self.assertNotEqual(second["terminal"], first["terminal"])

    def test_late_process_cannot_overwrite_resumed_job(self):
        old = jobs.start()
        self.live.clear()
        new = jobs.start(jid=old["id"])
        with self.assertRaises(RuntimeError):
            jobs.checkpoint(old["id"], old["generation"], phase="complete")
        self.assertEqual(jobs.get(new["id"])["phase"], "running")

    def test_cancel_only_kills_its_terminal(self):
        first, other = jobs.start(), jobs.start("server")
        jobs.cancel(first["id"])
        self.assertNotIn(first["terminal"], self.live)
        self.assertIn(other["terminal"], self.live)
        with self.assertRaises(RuntimeError):
            jobs.checkpoint(first["id"], first["generation"], phase="complete")
        self.assertEqual(len(jobs.listing()), 1)

    def test_completed_job_does_not_relaunch(self):
        job = jobs.start()
        jobs.checkpoint(job["id"], job["generation"], phase="complete", agent_id="example")
        self.live.clear()
        self.assertEqual(jobs.start(jid=job["id"])["phase"], "complete")
        self.assertEqual(len(self.commands), 1)

    def test_secrets_are_not_checkpoint_fields(self):
        job = jobs.start()
        with self.assertRaises(ValueError):
            jobs.checkpoint(job["id"], job["generation"], secret="never-store-this")
        path = Path(self.tmp.name) / (job["id"] + ".json")
        self.assertNotIn("never-store-this", path.read_text(encoding='utf-8'))
        assert_private_file(path)

    def test_unknown_create_result_is_preserved_on_resume(self):
        job = jobs.start()
        jobs.checkpoint(job["id"], job["generation"], create_requested=True)
        self.live.clear()
        self.assertTrue(jobs.start(jid=job["id"])["create_requested"])
        wizard = (Path(__file__).resolve().parents[1] / "bin/lark-bot-wizard.py").read_text(encoding='utf-8')
        self.assertIn('bool(_saved.get("app_id") or _saved.get("create_requested"))', wizard)

    def test_rejects_path_traversal_and_command_injection(self):
        for jid in ("../anything", "", "/tmp/other"):
            with self.assertRaises(ValueError): jobs.get(jid)
        with self.assertRaises(ValueError): jobs.start("server; echo nope")
        self.assertFalse(self.commands)

    def run_wizard(self, job, declarations=None, secret_ok=True):
        """执行真实脚本，仅替换飞书、Desk HTTP 与用户输入边界。"""
        import urllib.parse
        import urllib.request
        wizard = Path(__file__).resolve().parents[1] / "bin/lark-bot-wizard.py"
        calls = []
        declarations = list(declarations or [])
        result = {"app_id": "cli_test_app", "name": "Example", "secret": "TEST_SECRET_ONLY",
                  "version": "1", "missing": []}
        def auto_run(*args, **kwargs):
            kwargs["on_create"]()
            kwargs["on_app"](result["app_id"], result["name"])
            return result
        def auto_adopt(*args, **kwargs):
            chosen = kwargs["choose"]([dict(result, desc="", has_bot=True)], 1)
            self.assertEqual(chosen["app_id"], "cli_test_app")
            kwargs["on_app"](result["app_id"], result["name"])
            return result
        auto = types.SimpleNamespace(run=Mock(side_effect=auto_run), adopt=Mock(side_effect=auto_adopt))
        def request(req, **kwargs):
            parsed = urllib.parse.urlsplit(req.full_url)
            path = parsed.path
            calls.append((path, req.method, parsed.query))
            self.assertIn("backend=server", parsed.query)
            if path.endswith("/deps"): data = {"ok": True}
            elif path.endswith("/engines"): data = {"items": [{"name": "claude", "ok": True}]}
            elif path.endswith("/models"): data = {"items": []}
            elif path.endswith("/agents"):
                data = {"items": declarations, "backend": "server"} if req.method == "GET" else {"ok": True}
            elif path.endswith("/secret"): data = {"ok": secret_ok, "detail": "test validation"}
            elif path.endswith("/start"): data = {"pid": 123}
            else: raise AssertionError(path)
            response = io.BytesIO(json.dumps(data).encode())
            return response
        with patch.dict(sys.modules, {"lark_app_auto": auto}), \
             patch.object(sys, "argv", [str(wizard), "--auto", "--backend", "server",
                                         "--onboarding", job["id"], "--generation", str(job["generation"])]), \
             patch("builtins.input", return_value=""), patch("atexit.register"), \
             patch.object(urllib.request, "urlopen", side_effect=request), contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(str(wizard), run_name="__main__")
        return auto, calls

    def test_whole_wizard_saves_completion_and_never_checkpoints_secret(self):
        job = jobs.start("server")
        auto, calls = self.run_wizard(job)
        saved = jobs.get(job["id"])
        self.assertEqual(saved["phase"], "complete")
        self.assertEqual(saved["agent_id"], "example")
        self.assertNotIn("TEST_SECRET_ONLY", json.dumps(saved))
        auto.run.assert_called_once()
        self.assertTrue(any(path.endswith("/start") for path, _, _ in calls))

    def test_whole_wizard_after_process_loss_adopts_instead_of_creating(self):
        job = jobs.start("server")
        jobs.checkpoint(job["id"], job["generation"], create_requested=True, app_id="cli_test_app", agent_id="example")
        self.live.clear()
        job = jobs.start(jid=job["id"])
        auto, calls = self.run_wizard(job, [{"id": "example", "lark_app_id": "cli_test_app", "backend": "server"}])
        auto.run.assert_not_called()
        auto.adopt.assert_called_once()
        self.assertFalse(any(path.endswith("/agents") and method == "POST" for path, method, _ in calls))
        self.assertEqual(jobs.get(job["id"])["phase"], "complete")

    def test_failed_secret_check_keeps_bot_visible_for_followup(self):
        job = jobs.start("server")
        with self.assertRaises(SystemExit):
            self.run_wizard(job, secret_ok=False)
        saved = jobs.get(job["id"])
        self.assertEqual(saved["phase"], "needs_attention")
        self.assertEqual(saved["agent_id"], "example")


class CreationCheckpoint(unittest.TestCase):
    def test_app_identity_saved_before_configuring(self):
        path = Path(__file__).resolve().parents[1] / "bin/lark_app_auto.py"
        spec = importlib.util.spec_from_file_location("onboarding_auto_test", path)
        auto = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(auto)
        order = []
        console = Mock(who={})
        console.upload_icon.return_value = "icon"
        console.create_app_tpl.side_effect = lambda *a: order.append("create") or "cli_example"
        console.enable_bot.side_effect = auto.Failed("simulated failure")
        console.set_scopes.side_effect = auto.Failed("simulated failure")
        with patch.object(auto, "login", return_value={}), patch.object(auto, "Console", return_value=console):
            with self.assertRaises(auto.Failed):
                auto.run("feishu", "Example", "", on_create=lambda: order.append("requested"),
                         on_app=lambda aid, name: order.append((aid, name)))
        self.assertEqual(order, ["requested", "create", ("cli_example", "Example")])


class OnboardingApi(unittest.TestCase):
    def test_routes_resume_on_the_coordinator_not_the_selected_backend(self):
        from larkbot import api
        h = types.SimpleNamespace(headers={}, _send_json=Mock(), rfile=io.BytesIO(b"{}"))
        with patch.object(jobs, "get", return_value={"id": "a" * 32}) as get, \
             patch.object(api.peers, "find") as forward:
            api.handle(h, api.PREFIX + "/onboarding/" + "a" * 32, "backend=server", "GET")
        get.assert_called_once_with("a" * 32)
        forward.assert_not_called()

    def test_unknown_job_never_launches_a_new_one(self):
        from larkbot import api
        h = types.SimpleNamespace(headers={}, _send_json=Mock(), rfile=io.BytesIO(b"{}"))
        with patch.object(jobs, "get", side_effect=FileNotFoundError), patch.object(jobs, "start") as start:
            api.handle(h, api.PREFIX + "/onboarding/" + "a" * 32, "", "GET")
        start.assert_not_called()
        self.assertEqual(h._send_json.call_args.args[1], 400)


if __name__ == "__main__":
    unittest.main()
