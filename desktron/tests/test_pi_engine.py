"""Pi Coding Agent RPC 接入的稳定契约。"""
import io
import json
import os
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))

_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import deskcfg
    import engine as engine_mod
    import engine_adapters as ea
    import engines
    from engines.stores import PiSessionStore, pi_session_path, pi_sessions_dir_name
    from memory_engine import gate
    from pi_reasoning import is_openai_summary_titles_only
    from shepherd.runners import PiRunner
finally:
    sys.argv = _argv


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


class Proc:
    def __init__(self):
        self.stdin = io.StringIO()
        self.killed = False

    def poll(self):
        return -9 if self.killed else None

    def kill(self):
        self.killed = True


class RpcProc:
    """只允许上一帧已被读取后再写下一帧的 RPC 假进程。"""

    class Stdout:
        def __init__(self):
            self.lines = queue.Queue()
            self.read_count = 0

        def __iter__(self):
            return self

        def __next__(self):
            line = self.lines.get(timeout=2)
            if line is None:
                raise StopIteration
            self.read_count += 1
            return line

    class Stdin:
        def __init__(self, proc):
            self.proc = proc

        def write(self, line):
            if self.proc.writes != self.proc.stdout.read_count:
                raise AssertionError("上一条 RPC 尚未应答")
            frame = json.loads(line)
            self.proc.frames.append(frame)
            self.proc.writes += 1
            data = ({"sessionId": "cloned-session"}
                    if frame["type"] == "get_state" else {})
            self.proc.stdout.lines.put(json.dumps({
                "type": "response", "id": frame["id"], "command": frame["type"],
                "success": True, "data": data,
            }) + "\n")
            return len(line)

        def flush(self):
            pass

        def close(self):
            self.proc.stdout.lines.put(None)

    def __init__(self):
        self.stdout = self.Stdout()
        self.stdin = self.Stdin(self)
        self.stderr = io.StringIO()
        self.returncode = None
        self.frames = []
        self.writes = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9
        self.stdout.lines.put(None)


def update(kind, index, **extra):
    event = {"type": kind, "contentIndex": index}
    event.update(extra)
    return {"type": "message_update", "assistantMessageEvent": event}


class PiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus()
        self.ad = ea.PiAdapter(self.tmp.name, self.tmp.name, self.bus)

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, sid="adp_test"):
        return {"sid": sid, "msg_id": "msg_a_1", "parts": {}, "order": [],
                "last_tool": None, "t0": 1}


class TelemetryIsolation(unittest.TestCase):
    def test_adapter_startup_survives_telemetry_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch("performance_telemetry.PerformanceTelemetry.cleanup",
                           side_effect=OSError("read only")):
            adapter = ea.PiAdapter(folder, folder, Bus())
            self.assertEqual(adapter.name, "pi")

    def test_usage_survives_telemetry_storage_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter = ea.PiAdapter(folder, folder, Bus())
            ctx = {"sid":"adp_safe", "parts":{}, "order":[], "t0":1}
            adapter.performance.fingerprint = mock.Mock(side_effect=OSError("read only"))
            adapter._pi_usage(ctx, {"usage":{"input":1, "cacheRead":0, "totalTokens":2}})
            self.assertEqual(ctx["_pi_tokens"], 2)


class Registration(unittest.TestCase):
    def test_backend_registry_is_complete(self):
        self.assertIn("pi", engines.ENGINES)
        self.assertEqual(engines.get("pi").display, "Pi Coding")
        self.assertEqual(deskcfg.ENGINES["pi"]["label"], "Pi Coding")
        self.assertIn("pi", engine_mod.GUI_ENGINES)
        self.assertIs(engine_mod._adapter.__globals__["engine_adapters"].PiAdapter, ea.PiAdapter)

    def test_manifest_and_store(self):
        row = engines.manifest()["pi"]
        self.assertEqual(row["settingsShape"], "model_list")
        self.assertIsInstance(engines.get("pi").store, PiSessionStore)

    def test_boot_manifest_does_not_start_pi_probe(self):
        """首屏只取 available + 静态 manifest，模型/凭证探测必须留在按需 /config。"""
        import settings
        with mock.patch.object(settings, "_pi_probe", side_effect=AssertionError("冷探测进入首屏")):
            self.assertIn("pi", engines.manifest())
            self.assertIsInstance(engines.get("pi").available(), bool)

    def test_frontend_brand_and_settings_exist(self):
        events = (Path(ROOT) / "frontend" / "app-events.js").read_text(encoding="utf-8")
        style = (Path(ROOT) / "frontend" / "style.css").read_text(encoding="utf-8")
        settings = (Path(ROOT) / "frontend" / "app-settings-core.js").read_text(encoding="utf-8")
        self.assertIn('pi:', events)
        self.assertIn('class="brand-pi"', events)
        self.assertIn('fill="currentColor"', events)
        self.assertIn('--pi-brand-icon: #e8e8ea', style)
        self.assertIn('--pi-brand-icon: #525252', style)
        self.assertIn('.brand-pi { color: var(--pi-brand-icon); }', style)
        self.assertIn('smSection("Pi Coding"', settings)


class OptionsCatalog(PiCase):
    def test_null_thinking_levels_are_not_advertised(self):
        import settings
        stdout = "\n".join([
            json.dumps({"type": "response", "command": "get_state", "success": True,
                        "data": {"thinkingLevel": "high"}}),
            json.dumps({"type": "response", "command": "get_available_models", "success": True,
                        "data": {"models": [{
                            "provider": "deepseek", "id": "deepseek-v4-pro", "reasoning": True,
                            "thinkingLevelMap": {"off": None, "minimal": None, "low": None,
                                                 "medium": None, "high": "high", "max": "max"},
                        }]}}),
        ])
        completed = mock.Mock(stdout=stdout, stderr="", returncode=0)
        with mock.patch.object(settings.subprocess, "run", return_value=completed) as run, \
                mock.patch.dict(settings._PI_PROBE_CACHE, {}, clear=True):
            model = settings._pi_probe(force=True)["models"][0]
        command = run.call_args.args[0]
        self.assertIn("--no-skills", command)
        self.assertIn("--no-context-files", command)
        self.assertNotIn("--skill", command)
        self.assertEqual(model["thinking_levels"], ["high", "max"])

    @staticmethod
    def _model_probe_stdout(*models):
        return json.dumps({"type": "response", "command": "get_available_models",
                           "success": True, "data": {"models": list(models)}})

    def test_thinking_level_overrides_preserve_defaults_and_native_order(self):
        import settings
        for reasoning, level_map, expected in [
            (True, {"xhigh":"xhigh", "max":"max", "minimal":"low"},
             ["off", "minimal", "low", "medium", "high", "xhigh", "max"]),
            (True, {}, ["off", "minimal", "low", "medium", "high"]),
            (True, {"off":None, "low":None, "minimal":None, "max":"max"},
             ["medium", "high", "max"]),
            (False, {"max":"max"}, ["off"]),
        ]:
            with self.subTest(reasoning=reasoning, level_map=level_map):
                stdout = json.dumps({"type":"response", "command":"get_available_models",
                    "success":True, "data":{"models":[{"provider":"openai",
                        "id":"fixture", "reasoning":reasoning, "thinkingLevelMap":level_map}]}})
                with mock.patch.object(settings.subprocess, "run", return_value=mock.Mock(
                        stdout=stdout, stderr="", returncode=0)), \
                        mock.patch.dict(settings._PI_PROBE_CACHE, {}, clear=True):
                    models = settings._pi_probe(force=True)["models"]
                self.assertEqual(models[0]["thinking_levels"], expected)
                with mock.patch.object(settings, "_pi_model_catalog", return_value=models), \
                        mock.patch.object(settings, "_pi_current_model", return_value=models[0]["id"]), \
                        mock.patch.object(settings, "_pi_thinking_level", return_value="medium"):
                    option = self.ad.options_catalog()["models"][0]
                self.assertEqual([item["id"] for item in option["efforts"]], expected)
                self.assertEqual(option["defaultEffort"], expected[-1])

    def test_options_catalog_preserves_filtered_levels(self):
        import settings
        model = {"id": "deepseek/deepseek-v4-pro", "name": "V4 Pro",
                 "thinking_levels": ["off", "high", "max"]}
        with mock.patch.object(settings, "_pi_model_catalog", return_value=[model]), \
                mock.patch.object(settings, "_pi_current_model", return_value=model["id"]), \
                mock.patch.object(settings, "_pi_thinking_level", return_value="medium"):
            options = self.ad.options_catalog()
        self.assertEqual([item["id"] for item in options["models"][0]["efforts"]],
                         ["off", "high", "max"])
        self.assertEqual(options["models"][0]["defaultEffort"], "max")

    def test_plain_models_remain_flat(self):
        import settings
        model = {"id": "deepseek/deepseek-v4-pro", "provider": "deepseek",
                 "model": "deepseek-v4-pro", "name": "V4 Pro",
                 "thinking_levels": ["off", "high"]}
        with mock.patch.object(settings, "_pi_model_catalog", return_value=[model]), \
                mock.patch.object(settings, "_pi_current_model", return_value=model["id"]), \
                mock.patch.object(settings, "_pi_thinking_level", return_value="high"):
            option = self.ad.options_catalog()["models"][0]
        self.assertEqual(option["id"], model["id"])
        self.assertNotIn("variants", option)

    def test_options_carry_provider_and_readable_label(self):
        import settings
        rows = [{"id": "openai/gpt-6", "provider": "openai", "model": "gpt-6", "name": "GPT-6",
                 "thinking_levels": ["off"]},
                {"id": "qwen-token-plan-individual/qwen3.8-max",
                 "provider": "qwen-token-plan-individual", "model": "qwen3.8-max",
                 "name": "Qwen3.8 Max", "thinking_levels": ["off"]}]
        with mock.patch.object(settings, "_pi_model_catalog", return_value=rows), \
                mock.patch.object(settings, "_pi_current_model", return_value=rows[0]["id"]), \
                mock.patch.object(settings, "_pi_thinking_level", return_value="off"):
            models = self.ad.options_catalog()["models"]
        self.assertEqual([(m["provider"], m["providerLabel"]) for m in models],
                         [("openai", "OpenAI"),
                          ("qwen-token-plan-individual", "Qwen Token Plan Individual")])

    def test_openai_models_sort_by_newest_generation_then_variant(self):
        import settings

        def row(provider, model):
            return {"id": f"{provider}/{model}", "provider": provider, "model": model,
                    "name": model, "thinking_levels": ["off"]}

        rows = [
            row("deepseek", "deepseek-v4-pro"),
            row("openai", "gpt-5.4-mini"),
            row("anthropic", "claude-sonnet-4"),
            row("openai", "gpt-5.3-codex-spark"),
            row("openai", "gpt-5.6-sol"),
            row("openai", "gpt-5.4"),
            row("openai", "gpt-6-astra"),
            row("openai", "gpt-5.6-luna"),
            row("openai", "gpt-5.6-terra"),
            row("openai", "o3"),
            row("openai", "custom-preview"),
        ]
        current = "openai/gpt-5.4-mini"
        with mock.patch.object(settings, "_pi_model_catalog", return_value=rows), \
                mock.patch.object(settings, "_pi_current_model", return_value=current), \
                mock.patch.object(settings, "_pi_thinking_level", return_value="off"):
            options = self.ad.options_catalog()["models"]

        self.assertEqual([option["id"] for option in options], [
            "deepseek/deepseek-v4-pro",
            "openai/gpt-6-astra",
            "anthropic/claude-sonnet-4",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-terra",
            "openai/gpt-5.6-luna",
            "openai/gpt-5.4",
            "openai/gpt-5.4-mini",
            "openai/gpt-5.3-codex-spark",
            "openai/o3",
            "openai/custom-preview",
        ])
        self.assertTrue(next(option for option in options if option["id"] == current)["default"])

    def test_pi_uses_only_native_autocompaction_threshold(self):
        options = self.ad.options_catalog()
        self.assertNotIn("autocompacts", options["ui"])
        self.assertNotIn("autocompact", self.ad.normalize_options({"autocompact": "400000"}))
        source = (Path(ROOT) / "desktron" / "lib" / "engine_adapters" /
                  "pi_autocompact.ts").read_text(encoding="utf-8")
        self.assertNotIn('registerFlag("desk-autocompact"', source)
        self.assertNotIn('pi.on("turn_end"', source)
        self.assertNotIn("ctx.compact(", source)
        self.assertIn('pi.on("session_before_compact"', source)

    def test_compaction_is_provider_independent_without_mechanical_success(self):
        source = (Path(ROOT) / "desktron" / "lib" / "engine_adapters" /
                  "pi_autocompact.ts").read_text(encoding="utf-8")
        self.assertNotIn('ctx.model.provider', source)
        self.assertIn('role: "user"', source)
        self.assertNotIn('mechanicalSummary', source)
        self.assertIn('context_policy.json', source)

    def _recap_proc(self, *messages):
        import io
        lines = "".join(json.dumps({"type": "message_end", "message": m}) + "\n" for m in messages)
        proc = mock.Mock(pid=1234, returncode=0)
        proc.stdout = io.StringIO(lines)
        proc.poll.return_value = None
        return proc

    def _recap_source(self):
        path = os.path.join(self.tmp.name, "2026_pi-engine.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session", "id": "pi-engine"}) + "\n")
        return path

    def test_recap_reuses_main_turn_prefix_and_denies_tools(self):
        from engine_adapters import recap as recap_mod
        proc = self._recap_proc({"role": "assistant",
            "content": [{"type": "text", "text": "完成接入；待验证多端。"}],
            "usage": {"input": 120, "output": 30, "cacheRead": 50000, "cacheWrite": 0}})
        run = recap_mod._RecapRun("adp_test", 1)
        source = self._recap_source()
        opts = {"model": "deepseek/deepseek-v4-pro", "effort": "high",
                "_grounding": "memory/scripts/mem_read.py"}
        seen = {}

        def popen(cmd, **kwargs):
            seen["cmd"], seen["env"] = cmd, kwargs["env"]
            copy = cmd[cmd.index("--session") + 1]
            seen["copy_existed"] = os.path.exists(copy)
            seen["copy"] = copy
            return proc

        with mock.patch.object(self.ad, "_pi_path", return_value=source), \
                mock.patch("engine_adapters.pi.subprocess.Popen", side_effect=popen), \
                mock.patch("engine_adapters.recap._engine_env", return_value={}):
            answer = self.ad.recap_generate("adp_test", run, {
                "engine_sid": "pi-engine", "cwd": self.tmp.name, "opts": opts})
        self.assertEqual(answer, "完成接入；待验证多端。")
        cmd = seen["cmd"]
        main = self.ad.build_cmd("", "", opts)
        self.assertEqual(cmd[cmd.index("--mode") + 1], "json")
        self.assertEqual([x for x in cmd if x != "json"][:len(main) - 1],
                         [x for x in main if x != "rpc"][:len(main) - 1])
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1],
                         main[main.index("--append-system-prompt") + 1])
        self.assertNotIn("--no-tools", cmd)
        self.assertNotIn("--fork", cmd)
        self.assertTrue(seen["copy_existed"])
        self.assertEqual(os.path.basename(seen["copy"]), os.path.basename(source))
        self.assertFalse(os.path.exists(seen["copy"]))
        self.assertEqual(seen["env"]["DESKTRON_PI_RECAP"], "1")
        self.assertEqual(run.cache_usage, {"input": 120, "output": 30, "cacheRead": 50000, "cacheWrite": 0})
        proc.terminate.assert_called_once()

    def test_recap_stops_at_first_assistant_frame_when_tool_attempted(self):
        from engine_adapters import recap as recap_mod
        proc = self._recap_proc(
            {"role": "assistant", "stopReason": "toolUse",
             "content": [{"type": "toolCall", "name": "read", "arguments": {}}]},
            {"role": "assistant", "content": [{"type": "text", "text": "第二次请求"}]})
        run = recap_mod._RecapRun("adp_test", 1)
        with mock.patch.object(self.ad, "_pi_path", return_value=self._recap_source()), \
                mock.patch("engine_adapters.pi.subprocess.Popen", return_value=proc), \
                mock.patch("engine_adapters.recap._engine_env", return_value={}):
            answer = self.ad.recap_generate("adp_test", run, {
                "engine_sid": "pi-engine", "cwd": self.tmp.name,
                "opts": {"model": "openai/gpt-test", "_grounding": "g"}})
        self.assertEqual(answer, "")
        self.assertEqual(run.reason, "Pi recap 试图调用工具，已拒绝")

    def test_recap_rejects_text_with_tools_or_incomplete_stop(self):
        from engine_adapters import recap as recap_mod
        for reason, tools in [('toolUse', True), ('stop', True), ('aborted', False),
                              ('length', False), ('max_tokens', False)]:
            with self.subTest(reason=reason, tools=tools):
                content = [{'type': 'text', 'text': '未完成摘要'}]
                if tools:
                    content.append({'type': 'toolCall', 'name': 'read', 'arguments': {}})
                proc = self._recap_proc({'role': 'assistant', 'stopReason': reason, 'content': content})
                run = recap_mod._RecapRun('adp_test', 1)
                with mock.patch.object(self.ad, '_pi_path', return_value=self._recap_source()), \
                        mock.patch('engine_adapters.pi.subprocess.Popen', return_value=proc), \
                        mock.patch('engine_adapters.recap._engine_env', return_value={}):
                    answer = self.ad.recap_generate('adp_test', run, {
                        'engine_sid': 'pi-engine', 'cwd': self.tmp.name,
                        'opts': {'model': 'openai/gpt-test', '_grounding': 'g'}})
                self.assertEqual(answer, '')
                self.assertEqual(run.reason, 'Pi recap 试图调用工具，已拒绝' if tools else 'Pi recap 未正常结束')
                proc.terminate.assert_called_once()

    def test_peer_recap_keeps_toolless_fork(self):
        from engine_adapters import recap as recap_mod
        proc = mock.Mock(pid=1234, returncode=0)
        proc.communicate.return_value = (json.dumps({
            "type": "message_end", "message": {"role": "assistant",
                "content": [{"type": "text", "text": "答复"}]}}) + "\n", "")
        run = recap_mod._RecapRun("adp_test", 1)
        with mock.patch.object(self.ad, "_pi_path", return_value=self._recap_source()), \
                mock.patch("engine_adapters.pi.subprocess.Popen", return_value=proc) as popen, \
                mock.patch("engine_adapters.recap._engine_env", return_value={}):
            answer = self.ad.recap_generate("adp_test", run, {
                "engine_sid": "pi-engine", "cwd": self.tmp.name, "source": "peer",
                "opts": {"model": "deepseek/deepseek-v4-pro", "_grounding": "memory/scripts/mem_read.py"}})
        self.assertEqual(answer, "答复")
        cmd = popen.call_args.args[0]
        self.assertIn("--fork", cmd)
        self.assertIn("--no-tools", cmd)
        self.assertIn("--no-skills", cmd)
        self.assertIn("--no-context-files", cmd)

    def test_recap_preserves_pi_provider_failure_reason(self):
        from engine_adapters import recap as recap_mod
        proc = self._recap_proc({"role": "assistant", "stopReason": "error",
                                 "errorMessage": "provider quota exhausted"})
        run = recap_mod._RecapRun("adp_test", 1)
        with mock.patch.object(self.ad, "_pi_path", return_value=self._recap_source()), \
                mock.patch("engine_adapters.pi.subprocess.Popen", return_value=proc), \
                mock.patch("engine_adapters.recap._engine_env", return_value={}):
            answer = self.ad.recap_generate("adp_test", run, {
                "engine_sid": "pi-engine", "cwd": self.tmp.name,
                "opts": {"model": "openai/gpt-test", "effort": "high",
                         "_grounding": "memory/scripts/mem_read.py"}})
        self.assertEqual(answer, "")
        self.assertEqual(run.reason, "Pi 服务端错误")

    def test_recap_gate_and_compaction_respect_recap_flag(self):
        lib = Path(ROOT) / "desktron" / "lib" / "engine_adapters"
        gate = (lib / "pi_gate.ts").read_text(encoding="utf-8")
        compact = (lib / "pi_autocompact.ts").read_text(encoding="utf-8")
        self.assertLess(gate.index('DESKTRON_PI_RECAP === "1"'), gate.index("SAFE_READ.has(event.toolName)"))
        self.assertIn('if (process.env.DESKTRON_PI_RECAP === "1") return { cancel: true };', compact)

    def test_recap_without_grounding_does_not_start(self):
        from engine_adapters import recap as recap_mod
        run = recap_mod._RecapRun("adp_test", 1)
        with mock.patch.object(self.ad, "_pi_path", return_value="/tmp/source-pi.jsonl"), \
                mock.patch("engine_adapters.pi.subprocess.Popen") as popen:
            answer = self.ad.recap_generate("adp_test", run, {
                "engine_sid": "pi-engine", "cwd": self.tmp.name, "opts": {}})
        self.assertEqual(answer, "")
        self.assertEqual(run.reason, "Pi recap 缺少系统约定")
        popen.assert_not_called()

    def test_harness_followup_finishes_before_rpc_settles(self):
        source = (Path(ROOT) / "desktron" / "lib" / "engine_adapters" /
                  "pi_harness_loop.ts").read_text(encoding="utf-8")
        self.assertIn('pi.on("agent_end"', source)
        self.assertNotIn('pi.on("agent_settled"', source)
        self.assertIn('deliverAs: "followUp"', source)

class SessionStore(unittest.TestCase):
    def test_cwd_encoding_matches_pi(self):
        cwd = os.path.abspath("/Users/x/.local/share/y")
        expected = "--" + cwd.strip(os.sep).replace(os.sep, "-").replace(":", "-") + "--"
        self.assertEqual(pi_sessions_dir_name(cwd), expected)

    def test_exact_session_lookup_and_store(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {"PI_CODING_AGENT_SESSION_DIR": root}):
            sid = "01a00000-0000-7000-8000-000000000001"
            # PI_CODING_AGENT_SESSION_DIR 是最终目录，不再套 cwd 编码子目录。
            path = os.path.join(root, "2026-08-28T00-00-00_" + sid + ".jsonl")
            rows = [
                {"type": "session", "id": sid, "cwd": "/tmp/project"},
                {"type": "message", "message": {"role": "user", "content": "首问"}},
            ]
            with open(path, "w", encoding="utf-8") as file:
                file.write("".join(json.dumps(row) + "\n" for row in rows))
            self.assertEqual(pi_session_path(sid), path)
            store = PiSessionStore()
            self.assertTrue(store.owns(sid))
            self.assertEqual(store.cwd_of(sid), "/tmp/project")
            self.assertEqual(store.title(sid), "首问")


class LifecycleAndCommand(PiCase):
    def test_missing_grounding_fails_closed(self):
        with mock.patch("grounding.build", side_effect=OSError("fixture unavailable")):
            with self.assertRaisesRegex(RuntimeError, "系统约定构造失败"):
                self.ad.grounding("adp_test", first_turn=True)

    def test_shepherd_pi_missing_grounding_fails_closed(self):
        from engine_adapters.cli_store import SessionStoreMixin
        from shepherd.store import Conflict
        adapter = ea.ShepherdAdapter(os.path.join(self.tmp.name, 'shepherd-data'),
                                     self.tmp.name, self.bus)
        with mock.patch.object(adapter, "session_engine", return_value="pi"), \
                mock.patch.object(SessionStoreMixin, "grounding",
                                  return_value={"constant": "", "scene": ""}):
            with self.assertRaisesRegex(Conflict, "系统约定构造失败"):
                adapter.grounding("adp_test", first_turn=True)

    def test_preassigned_uuid_is_not_started_history(self):
        row = self.ad.create_session("desk-iter")
        self.assertRegex(row["engine_sid"], r"^[0-9a-f-]{36}$")
        self.assertFalse(row["engine_started"])
        self.assertFalse(self.ad.engine_has_history(row["id"], row["engine_sid"]))

        ctx = self.ctx(row["id"])
        with mock.patch("term._term_migrate_identity"), mock.patch("term.term_meta_register"):
            self.ad.handle_event({"type": "response", "command": "prompt", "success": True}, ctx)
        self.assertTrue(self.ad.index[row["id"]]["engine_started"])
        self.assertTrue(self.ad.engine_has_history(row["id"], row["engine_sid"]))

    def test_rpc_command_is_pinned_and_gated(self):
        sid = "01a00000-0000-7000-8000-000000000002"
        cmd = self.ad.build_cmd("x", sid, {
            "model": "deepseek/deepseek-v4-pro", "effort": "high"})
        self.assertEqual(cmd[:3], ["pi", "--mode", "rpc"])
        for flag in ("--offline", "--no-extensions", "--extension", "--no-approve",
                     "--no-skills", "--no-context-files"):
            self.assertIn(flag, cmd)
        self.assertNotIn("--skill", cmd)
        self.assertEqual(cmd[cmd.index("--session-id") + 1], sid)
        self.assertEqual(cmd[cmd.index("--model") + 1], "deepseek/deepseek-v4-pro")
        self.assertEqual(cmd[cmd.index("--thinking") + 1], "high")
        self.assertIn("pi_autocompact.ts", " ".join(cmd))

    def test_system_prompt_only_adds_pi_specific_delta(self):
        cmd = self.ad.build_cmd("x", "pi-session", {"_grounding": "GROUNDING"})
        body = cmd[cmd.index("--append-system-prompt") + 1]
        self.assertIn("GROUNDING", body)
        self.assertIn("Bash 没有 `description` 字段", body)
        self.assertNotIn("工作循环:调查", body)

    def test_pi_gate_replaces_long_native_docs_section(self):
        source = (Path(ROOT) / "desktron/lib/engine_adapters/pi_gate.ts").read_text(
            encoding="utf-8")
        self.assertIn("event.systemPromptOptions.sections.docs", source)
        self.assertIn("仅在用户询问 Pi 本身", source)
        self.assertIn("getReadmePath()", source)
        self.assertIn("getDocsPath()", source)
        self.assertIn("getExamplesPath()", source)

    def test_subagent_notification_turn_cannot_spawn_another_subagent(self):
        extension='/tmp/pi_children.ts'
        with mock.patch('engine_adapters.pi.subagent_extension_args',
                        return_value=['--extension',extension]):
            normal=self.ad.build_cmd('x','pi-session',{})
            notification=self.ad.build_cmd('x','pi-session',{'_subagent_delivery':True})
        self.assertIn(extension,normal)
        self.assertNotIn(extension,notification)
        body=notification[notification.index('--append-system-prompt')+1]
        self.assertIn('不要再次启动子任务',body)
        self.assertNotEqual(self.ad._persist_key(normal),self.ad._persist_key(notification))

    def test_prompt_revision_changes_persistent_process_key(self):
        first = self.ad.build_cmd("x", "pi-session", {"_grounding": "version one"})
        same = self.ad.build_cmd("y", "another-session", {"_grounding": "version one"})
        changed = self.ad.build_cmd("x", "pi-session", {"_grounding": "version two"})
        self.assertEqual(self.ad._persist_key(first), self.ad._persist_key(same))
        self.assertNotEqual(self.ad._persist_key(first), self.ad._persist_key(changed))
        self.assertNotIn("version one", self.ad._persist_key(first))

    def test_retired_autocompact_window_does_not_change_process(self):
        base = self.ad.build_cmd("x", "pi-session", {})
        legacy = self.ad.build_cmd("x", "pi-session", {"autocompact": "200000"})
        self.assertNotIn("--desk-autocompact", base)
        self.assertNotIn("--desk-autocompact", legacy)
        self.assertEqual(self.ad._persist_key(base), self.ad._persist_key(legacy))

    def test_retired_longtask_skill_is_not_loaded_for_any_provider(self):
        with mock.patch.dict(os.environ, {"PI_OLLAMA_PROVIDER": "ollama-remote"}):
            ollama = self.ad.build_cmd("x", "pi-session", {
                "model": "qwen3.8:27b", "provider": "ollama-remote"})
            qualified = self.ad.build_cmd("x", "pi-session", {
                "model": "ollama-remote/qwen3.8:27b"})
            deepseek = self.ad.build_cmd("x", "pi-session", {
                "model": "deepseek-v4-pro", "provider": "deepseek"})
            gateway = self.ad.build_cmd("x", "pi-session", {
                "model": "example-gw/gpt-5.6-sol:gpt-5.6-sol__max"})
            default = self.ad.build_cmd("x", "pi-session", {})
        for cmd in (ollama, qualified, deepseek, gateway, default):
            with self.subTest(cmd=cmd[:3]):
                self.assertNotIn("longtask-progression", " ".join(cmd))
                self.assertIn("pi_autocompact.ts", " ".join(cmd))
        retired = Path(ROOT) / "desktron/lib/engine_adapters/pi_skills/longtask-progression/SKILL.md"
        self.assertFalse(retired.exists())

    def test_kv_warmup_only_loads_for_configured_provider(self):
        with mock.patch.dict(os.environ, {
                "PI_OLLAMA_PROVIDER": "ollama-remote",
                "PI_KV_WARMUP": "1",
                "PI_KV_WARM_PROVIDER": "ollama-remote",
                "PI_KV_WARM_MODEL": "qwen3.8:27b",
        }):
            ollama = self.ad.build_cmd("x", "pi-session", {
                "provider": "ollama-remote", "model": "qwen3.8:27b"})
            qualified = self.ad.build_cmd("x", "pi-session", {
                "model": "ollama-remote/qwen3.8:27b"})
            deepseek = self.ad.build_cmd("x", "pi-session", {
                "provider": "deepseek", "model": "deepseek-v4-pro"})
        for cmd in (ollama, qualified):
            joined = " ".join(cmd)
            self.assertIn("pi_kv_warmup.ts", joined)
            self.assertEqual(cmd[cmd.index("--desk-kv-warm-provider") + 1], "ollama-remote")
            self.assertEqual(cmd[cmd.index("--desk-kv-warm-model") + 1], "qwen3.8:27b")
        self.assertNotIn("pi_kv_warmup.ts", " ".join(deepseek))

    def test_kv_warmup_requires_opt_in_and_exact_provider_and_model(self):
        allowed = {"PI_KV_WARMUP": "1", "PI_KV_WARM_PROVIDER": "ollama-remote",
                   "PI_KV_WARM_MODEL": "qwen3.8:27b"}
        cases = [({}, "ollama-remote/qwen3.8:27b"),
                 ({**allowed, "PI_KV_WARMUP": "0"}, "ollama-remote/qwen3.8:27b"),
                 ({**allowed, "PI_KV_WARM_MODEL": ""}, "ollama-remote/qwen3.8:27b"),
                 ({**allowed, "PI_KV_WARM_PROVIDER": ""}, "ollama-remote/qwen3.8:27b"),
                 (allowed, "ollama-remote/another-model"),
                 (allowed, "example-gw/gpt-5.6-sol:gpt-5.6-sol__max")]
        for env, model in cases:
            with self.subTest(env=env, model=model), mock.patch.dict(os.environ, {
                    "PI_KV_WARMUP": "", "PI_KV_WARM_PROVIDER": "", "PI_KV_WARM_MODEL": "",
                    "PI_OLLAMA_PROVIDER": "ollama-remote", **env}):
                cmd = self.ad.build_cmd("x", "pi-session", {"model": model})
                self.assertNotIn("pi_kv_warmup.ts", " ".join(cmd))

    def test_web_tools_are_default_on_and_can_be_explicitly_disabled(self):
        with mock.patch.dict(os.environ, {"DESKTRON_WEB_TOOLS": ""}):
            enabled = self.ad.build_cmd("x", "pi-session", {})
        joined = " ".join(enabled)
        self.assertIn("pi_web.ts", joined)
        with mock.patch.dict(os.environ, {"DESKTRON_WEB_TOOLS": "0"}):
            disabled = self.ad.build_cmd("x", "pi-session", {})
        self.assertNotIn("pi_web.ts", " ".join(disabled))
        body = enabled[enabled.index("--append-system-prompt") + 1]
        self.assertNotIn("`web_search` 和 `web_fetch` 只用于无需登录的公开网页", body)
        self.assertNotIn("VOLCENGINE_WEB_SEARCH_API_KEY", body)
        self.assertNotEqual(self.ad._persist_key(disabled), self.ad._persist_key(enabled))
        with mock.patch.dict(os.environ, {"DESKTRON_WEB_TOOLS": "1"}):
            qwen = self.ad.build_cmd("x", "pi-session", {
                "model": "office-qwen/Qwen3.8-27B-FP8", "provider": "office-qwen"})
        self.assertIn("pi_web.ts", " ".join(qwen))
        self.assertNotIn("VOLCENGINE_WEB_SEARCH_API_KEY", " ".join(qwen))

    def test_shepherd_fallback_command_disables_native_prompt_sources(self):
        connector = mock.Mock(store_root=self.tmp.name, native_cwd=self.tmp.name,
                              path=os.path.join(self.tmp.name, 'native.jsonl'), native_session_id='native')
        connector.binding = {'binding_id':'binding'}
        connector.ownership_nonce = 'nonce'
        connector.verify.side_effect = [
            {'entries':0, 'native_revision':'before'},
            {'entries':1, 'native_revision':'after'},
        ]
        process = mock.Mock(proc=mock.Mock())
        process.events.return_value = iter([{'type':'agent_settled'}])
        runner = PiRunner(connector, env={}, command=None)
        runner.receipts = mock.Mock()
        with mock.patch('shepherd.runners.NativeProcess', return_value=process) as native:
            result = runner.run(prompt='q', round_id='round', on_dispatched=lambda **_kwargs: None)
        self.assertEqual(result['terminal_state'], 'completed')
        final_command = native.call_args.args[0]
        self.assertIn('--no-skills', final_command)
        self.assertIn('--no-context-files', final_command)
        self.assertNotIn('--skill', final_command)

    def test_shepherd_minimal_custom_command_keeps_isolation_and_system_text(self):
        connector = mock.Mock(store_root=self.tmp.name, native_cwd=self.tmp.name,
                              path=os.path.join(self.tmp.name, 'native.jsonl'), native_session_id='native')
        connector.binding = {'binding_id':'binding'}
        connector.ownership_nonce = 'nonce'
        connector.verify.side_effect = [
            {'entries':0, 'native_revision':'before'},
            {'entries':1, 'native_revision':'after'},
        ]
        process = mock.Mock(proc=mock.Mock())
        process.events.return_value = iter([{'type':'agent_settled'}])
        runner = PiRunner(connector, env={}, system_text='memory/scripts/mem_read.py',
                          command=['pi', '--mode', 'rpc', '--skill', '/fixture/skills',
                                   '--no-session'])
        runner.receipts = mock.Mock()
        with mock.patch('shepherd.runners.NativeProcess', return_value=process) as native:
            runner.run(prompt='q', round_id='round', on_dispatched=lambda **_kwargs: None)
        final_command = native.call_args.args[0]
        self.assertIn('--no-skills', final_command)
        self.assertIn('--no-context-files', final_command)
        self.assertNotIn('--skill', final_command)
        self.assertNotIn('--no-session', final_command)
        self.assertEqual(final_command[final_command.index('--session') + 1], connector.path)
        prompts = [final_command[index + 1] for index, value in enumerate(final_command[:-1])
                   if value == '--append-system-prompt']
        self.assertTrue(any('mem_read.py' in value for value in prompts))

    def test_real_adapter_command_reaches_pi_runner_without_losing_web_extension(self):
        grounded = ea.PiAdapter(self.tmp.name, ROOT, self.bus)
        grounding = grounded.grounding('adp_test', first_turn=False)['constant']
        with mock.patch.dict(os.environ, {"DESKTRON_WEB_TOOLS": "1"}):
            command = grounded.build_cmd("x", "pi-session", {"_grounding": grounding})
        command += ['--skill', '/fixture/skills']
        connector = mock.Mock(store_root=self.tmp.name, native_cwd=self.tmp.name,
                              path=os.path.join(self.tmp.name, 'native.jsonl'), native_session_id='native')
        connector.binding = {'binding_id':'binding'}
        connector.ownership_nonce = 'nonce'
        connector.verify.side_effect = [
            {'entries':0, 'native_revision':'before'},
            {'entries':1, 'native_revision':'after'},
        ]
        process = mock.Mock(proc=mock.Mock())
        process.events.return_value = iter([{'type':'agent_settled'}])
        runner = PiRunner(connector, env={'DESKTRON_WEB_TOOLS':'1',
                                          'VOLCENGINE_WEB_SEARCH_API_KEY':'legacy-secret'},
                          system_text=grounding, command=command)
        runner.receipts = mock.Mock()
        with mock.patch('shepherd.runners.NativeProcess', return_value=process) as native:
            result = runner.run(prompt='q', round_id='round', on_dispatched=lambda **_kwargs: None)
        self.assertEqual(result['terminal_state'], 'completed')
        final_command = native.call_args.args[0]
        self.assertIn('pi_web.ts', ' '.join(final_command))
        self.assertEqual(final_command.count('--extension'), command.count('--extension'))
        self.assertIn('--no-skills', final_command)
        self.assertIn('--no-context-files', final_command)
        self.assertNotIn('--skill', final_command)
        self.assertNotIn('/fixture/skills', final_command)
        prompts = [final_command[index + 1] for index, value in enumerate(final_command[:-1])
                   if value == '--append-system-prompt']
        self.assertTrue(any('mem_read.py' in value for value in prompts))
        self.assertNotIn('VOLCENGINE_WEB_SEARCH_API_KEY', native.call_args.kwargs['env'])

    def test_legacy_web_search_key_does_not_affect_process_fingerprint(self):
        first = self.ad.persist_env_key({"DESKTRON_PI_PERMISSION_MODE": "default",
                                         "VOLCENGINE_WEB_SEARCH_API_KEY": "first-secret"})
        second = self.ad.persist_env_key({"DESKTRON_PI_PERMISSION_MODE": "default",
                                          "VOLCENGINE_WEB_SEARCH_API_KEY": "second-secret"})
        self.assertEqual(first, second)
        self.assertNotIn("first-secret", repr(first))
        self.assertNotIn("second-secret", repr(second))

    def test_existing_record_uses_exact_path(self):
        sid = "01a00000-0000-7000-8000-000000000003"
        with mock.patch("engine_adapters.pi.pi_session_path", return_value="/tmp/pi.jsonl"):
            cmd = self.ad.build_cmd("x", sid, {})
        self.assertNotIn("--session-id", cmd)
        self.assertEqual(cmd[cmd.index("--session") + 1], "/tmp/pi.jsonl")

    def test_prompt_frame_keeps_images(self):
        frame = self.ad.build_user_frame("看图", [
            ("image", "/tmp/a.png", "a.png", "image/png", "AAAA"),
            ("file", "/tmp/a.txt", "a.txt", "text/plain", "BBBB"),
        ])
        self.assertEqual(frame["type"], "prompt")
        self.assertEqual(frame["message"], "看图")
        self.assertEqual(frame["images"], [
            {"type": "image", "data": "AAAA", "mimeType": "image/png"}])

    def test_permission_modes_are_process_env_only(self):
        edit_env = self.ad.spawn_env({"mode": "acceptEdits"})
        bypass_env = self.ad.spawn_env({"mode": "bypassPermissions"})
        self.assertEqual(edit_env[
            "DESKTRON_PI_PERMISSION_MODE"], "acceptEdits")
        self.assertEqual(self.ad.spawn_env({"mode": "invalid"})[
            "DESKTRON_PI_PERMISSION_MODE"], "default")
        self.assertNotEqual(self.ad.persist_env_key(edit_env), self.ad.persist_env_key(bypass_env))

    def test_pi_process_boundaries_strip_legacy_web_search_key(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        with mock.patch("owned_process.command", side_effect=lambda cmd, lock_path: cmd), \
                mock.patch("owned_process.protect"), \
                mock.patch("engine_adapters.pi.subprocess.Popen", return_value=proc) as popen:
            self.ad._spawn_process("adp_test", ["pi"], env={
                "PATH": "fixture", "VOLCENGINE_WEB_SEARCH_API_KEY": "legacy-secret"})
        spawned_env = popen.call_args.kwargs["env"]
        self.assertEqual(spawned_env["PATH"], "fixture")
        self.assertNotIn("VOLCENGINE_WEB_SEARCH_API_KEY", spawned_env)

        from engine_adapters import recap
        with mock.patch.object(recap, "secrets_env", return_value={
                "PATH": "fixture", "VOLCENGINE_WEB_SEARCH_API_KEY": "legacy-secret"}):
            recap_env = recap._engine_env(self.ad, {})
        self.assertEqual(recap_env["PATH"], "fixture")
        self.assertNotIn("VOLCENGINE_WEB_SEARCH_API_KEY", recap_env)

    def test_control_rpc_waits_for_each_response(self):
        proc = RpcProc()
        with mock.patch.object(self.ad, "_pi_path", return_value="/tmp/pi.jsonl"), \
                mock.patch.object(self.ad, "grounding", return_value={
                    "constant": "memory/scripts/mem_read.py", "scene": ""}), \
                mock.patch("engine_adapters.pi.subprocess.Popen", return_value=proc) as popen:
            objects, error = self.ad._rpc_once(
                "adp_test", [{"type": "clone"}, {"type": "get_state"}], timeout=2)
        self.assertEqual(error, "")
        command = popen.call_args.args[0]
        self.assertIn("--no-skills", command)
        self.assertIn("--no-context-files", command)
        self.assertNotIn("--skill", command)
        body = command[command.index("--append-system-prompt") + 1]
        self.assertIn("memory/scripts/mem_read.py", body)
        self.assertIn("Bash 没有 `description` 字段", body)
        self.assertEqual([frame["type"] for frame in proc.frames], ["clone", "get_state"])
        self.assertEqual(len({frame["id"] for frame in proc.frames}), 2)
        self.assertEqual([obj["command"] for obj in objects], ["clone", "get_state"])


class ControlActions(PiCase):
    def _started_session(self):
        row = self.ad.create_session("desk-iter", opts={"effort": "high"})
        self.ad.index[row["id"]]["engine_sid"] = "pi-source"
        self.ad.index[row["id"]]["engine_started"] = True
        return row["id"]

    def test_manual_compact_runs_control_rpc_and_reimports_history(self):
        sid = self._started_session()
        response = [{"type": "response", "command": "compact", "success": True,
                     "data": {"summary": "done"}}]
        with mock.patch.object(self.ad, "_pi_path", return_value="/tmp/pi.jsonl"), \
                mock.patch("engine_adapters.pi._persist_drop"), \
                mock.patch.object(self.ad, "_rpc_once", return_value=(response, "")), \
                mock.patch.object(self.ad, "import_session", return_value={"id": sid}) as imported:
            result = self.ad.compact_session(sid)
        self.assertTrue(result["ok"])
        imported.assert_called_once_with("pi-source", sid=sid, limit=50)
        frames = [e["properties"] for e in self.bus.events if e["type"] == "session.compact"]
        self.assertEqual([frame["running"] for frame in frames], [True, False])

    def test_fork_uses_cloned_engine_id_and_preserves_options(self):
        sid = self._started_session()
        objects = [
            {"type": "response", "command": "clone", "success": True, "data": {}},
            {"type": "response", "command": "get_state", "success": True,
             "data": {"sessionId": "pi-clone"}},
        ]
        with mock.patch.object(self.ad, "_pi_path", return_value="/tmp/pi.jsonl"), \
                mock.patch("engine_adapters.pi._persist_drop"), \
                mock.patch.object(self.ad, "_rpc_once", return_value=(objects, "")), \
                mock.patch.object(self.ad, "_set_engine_sid"), \
                mock.patch.object(self.ad, "import_session", return_value={"id": "fork"}):
            forked = self.ad.fork_session(sid)
        self.assertIsNotNone(forked)
        self.assertEqual(forked["engine_sid"], "pi-clone")
        self.assertEqual(forked["opts"]["effort"], "high")
        self.assertEqual(forked["forked_from"], sid)


class Events(PiCase):
    def test_openai_title_only_thinking_is_buffered_and_hidden(self):
        title = "**Planning eval schema**\n\n**Defining tool policies**"
        ctx = self.ctx()
        self.ad.handle_event({"type": "message_start", "message": {
            "role": "assistant", "provider": "openai", "model": "gpt-test"}}, ctx)
        self.ad.handle_event(update("thinking_delta", 0, delta=title), ctx)
        self.assertEqual(ctx["order"], [])
        self.ad.handle_event(update("thinking_end", 0, content=title), ctx)
        self.assertEqual(ctx["order"], [])
        self.assertFalse([event for event in self.bus.events
                          if event["type"] == "message.part.delta"])

    def test_openai_thinking_with_visible_body_is_kept_after_end(self):
        ctx = self.ctx()
        self.ad.handle_event({"type": "message_start", "message": {
            "role": "assistant", "provider": "openai", "model": "gpt-test"}}, ctx)
        visible = "**Planning eval schema**\n\nThe schema needs two required fields."
        self.ad.handle_event(update("thinking_delta", 0, delta=visible), ctx)
        self.assertEqual(ctx["order"], [])
        self.ad.handle_event(update("thinking_end", 0, content=visible), ctx)
        self.assertEqual(ctx["parts"][ctx["order"][0]]["text"], visible)

    def test_openai_thinking_is_flushed_from_message_end_when_end_frame_is_missing(self):
        ctx = self.ctx()
        self.ad.handle_event({"type": "message_start", "message": {
            "role": "assistant", "provider": "openai", "model": "gpt-test"}}, ctx)
        visible = "**Planning eval schema**\n\nThe schema needs two required fields."
        self.ad.handle_event(update("thinking_delta", 0, delta=visible), ctx)
        self.ad.handle_event({"type": "message_end", "message": {
            "role": "assistant", "provider": "openai", "stopReason": "stop",
            "content": [{"type": "thinking", "thinking": visible}]}}, ctx)
        self.assertEqual(ctx["parts"][ctx["order"][0]]["text"], visible)

    def test_openai_thinking_is_flushed_from_agent_settled(self):
        ctx = self.ctx()
        self.ad.handle_event({"type": "message_start", "message": {
            "role": "assistant", "provider": "openai", "model": "gpt-test"}}, ctx)
        visible = "**Planning eval schema**\n\nThe schema needs two required fields."
        self.ad.handle_event(update("thinking_delta", 0, delta=visible), ctx)
        self.ad.handle_event({"type": "agent_settled"}, ctx)
        self.assertEqual(ctx["parts"][ctx["order"][0]]["text"], visible)

    def test_openai_title_only_thinking_stays_hidden_on_abnormal_finish(self):
        ctx = self.ctx()
        self.ad.handle_event({"type": "message_start", "message": {
            "role": "assistant", "provider": "openai", "model": "gpt-test"}}, ctx)
        self.ad.handle_event(update(
            "thinking_delta", 0, delta="**Planning eval schema**"), ctx)
        with mock.patch("engine_adapters.cli.CliAdapter._finish_turn",
                        return_value="done") as finish:
            self.assertEqual(self.ad._finish_turn(ctx["sid"], ctx, True), "done")
        finish.assert_called_once_with(ctx["sid"], ctx, True)
        self.assertEqual(ctx["order"], [])

    def test_other_provider_thinking_still_streams_immediately(self):
        ctx = self.ctx()
        self.ad.handle_event({"type": "message_start", "message": {
            "role": "assistant", "provider": "deepseek", "model": "deepseek-test"}}, ctx)
        self.ad.handle_event(update("thinking_delta", 0, delta="**Planning eval schema**"), ctx)
        self.assertEqual(ctx["parts"][ctx["order"][0]]["text"], "**Planning eval schema**")

    def test_text_stays_in_process_until_clean_tool_free_end(self):
        ctx = self.ctx()
        start = {"type": "message_start", "message": {"role": "assistant"}}
        self.ad.handle_event(start, ctx)
        self.ad.handle_event(update("text_delta", 0, delta="最终答案"), ctx)
        final = ctx["parts"][ctx["order"][-1]]
        self.assertEqual(final["type"], "reasoning")
        deltas = [e["properties"] for e in self.bus.events if e["type"] == "message.part.delta"]
        self.assertEqual(deltas[-1]["field"], "reasoning")
        self.ad.handle_event({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "stop"}}, ctx)
        self.assertEqual(final["type"], "text")
        changed = [e["properties"]["part"] for e in self.bus.events
                   if e["type"] == "message.part.updated"]
        committed = [p for p in changed if p["id"] == final["id"] and p["type"] == "text"]
        self.assertTrue(committed)
        self.assertTrue(committed[-1]["replace"])

    def test_tool_commentary_and_native_thinking_stay_in_process(self):
        ctx = self.ctx()
        start = {"type": "message_start", "message": {"role": "assistant"}}
        self.ad.handle_event(start, ctx)
        self.ad.handle_event(update("text_delta", 0, delta="先检查配置"), ctx)
        pid = ctx["order"][-1]
        self.ad.handle_event(update("toolcall_start", 1, id="call-1", toolName="read"), ctx)
        self.ad.handle_event({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "toolUse"}}, ctx)
        self.assertEqual(ctx["parts"][pid]["type"], "reasoning")
        # 下一条模型消息重新判定；只迁普通 text，原生 thinking 始终留在过程区。
        self.ad.handle_event(start, ctx)
        self.ad.handle_event(update("thinking_delta", 0, delta="复核结果"), ctx)
        thinking = ctx["parts"][ctx["order"][-1]]
        self.ad.handle_event(update("text_delta", 1, delta="配置正确"), ctx)
        final = ctx["parts"][ctx["order"][-1]]
        self.ad.handle_event({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "stop"}}, ctx)
        self.assertEqual(thinking["type"], "reasoning")
        self.assertEqual(final["type"], "text")

    def test_message_end_can_classify_when_tool_stream_frames_are_missing(self):
        ctx = self.ctx()
        self.ad.handle_event({"type": "message_start", "message": {"role": "assistant"}}, ctx)
        self.ad.handle_event(update("text_delta", 0, delta="检查中"), ctx)
        self.ad.handle_event({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "toolUse"}}, ctx)
        self.assertEqual(ctx["parts"][ctx["order"][0]]["type"], "reasoning")

    def test_text_reasoning_tool_usage_and_settle(self):
        ctx = self.ctx()
        ctx.update(seq=17, _provider_wait_fence={"request_seq": 3},
                   _usage_model="example-gw/fixture", _usage_provider="example-gw",
                   _usage_binding_hash="bindinghash1", _usage_system_hash="systemhash123456",
                   _usage_binding_value="PRIVATE_BINDING", _usage_system_value="PRIVATE_SYSTEM",
                   _usage_resident_reused=True)
        self.ad.handle_event({"type": "message_start", "message": {
            "role": "assistant", "provider": "deepseek", "model": "deepseek-v4-pro"}}, ctx)
        for frame in (
            update("thinking_delta", 0, delta="先想"),
            update("thinking_end", 0, content="先想"),
            update("text_delta", 1, delta="完成"),
            update("toolcall_end", 2, toolCall={"id": "c1", "name": "read",
                                                "arguments": {"path": "/tmp/a"}}),
            {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "read",
             "result": {"content": [{"type": "text", "text": "ok"}]}, "isError": False},
            {"type": "turn_end", "message": {"usage": {
                "input": 30, "cacheRead": 12,
                "totalTokens": 42, "cost": {"total": 0.01}}}},
            {"type": "agent_settled"},
        ):
            self.ad.handle_event(frame, ctx)
        parts = [ctx["parts"][key] for key in ctx["order"]]
        self.assertEqual([part["type"] for part in parts], ["reasoning", "reasoning", "tool"])
        self.assertEqual(parts[-1]["state"]["output"], "ok")
        self.assertEqual(self.ad.last_model, "deepseek/deepseek-v4-pro")
        self.assertTrue(ctx["_close_stdin"])
        self.assertEqual([e for e in self.bus.events if e["type"] == "session.stats"][-1]
                         ["properties"]["tokens"], 42)
        with open(os.path.join(self.ad.dir, "trace", "adp_test.jsonl"), encoding="utf-8") as f:
            usage = [json.loads(line) for line in f if '"ev": "pi.usage"' in line][-1]
        self.assertEqual((usage["input"], usage["cacheRead"], usage["total"]), (30, 12, 42))
        self.assertEqual({key:usage[key] for key in (
            "seq", "request_seq", "model", "provider", "binding", "system_hash",
            "resident_reused")}, {
            "seq":17, "request_seq":3, "model":"deepseek/deepseek-v4-pro",
            "provider":"deepseek", "binding":"bindinghash1",
            "system_hash":"systemhash123456", "resident_reused":True})
        self.assertNotIn("检查", json.dumps(usage, ensure_ascii=False))
        paths = list((Path(self.tmp.name) / "diagnostics" / "performance" / "pi").glob("*.jsonl"))
        rows = [json.loads(line) for path in paths for line in path.read_text().splitlines()]
        self.assertEqual([row for row in rows if row["event"] == "pi_usage"], [])

    def test_provider_trace_is_whitelisted_and_merged_with_matching_usage(self):
        ctx = self.ctx(); ctx["seq"] = 17
        epoch = "12345678-1234-4123-8123-123456789abc"
        payload = {"kind":"request", "request_epoch":epoch, "request_seq":1,
                   "provider":"openai", "model":"gpt-test", "transport":"http_sse",
                   "continuation_used":False, "identity_mode":"native",
                   "fingerprint_epoch_fp":"8" * 32,
                   "request_epoch_fp":"9" * 32, "session_fp":"1" * 32,
                   "cache_key_fp":"2" * 32, "system_segment_fp":"3" * 32,
                   "tools_segment_fp":"4" * 32, "history_segment_fp":"5" * 32,
                   "reasoning_segment_fp":"6" * 32, "tail_segment_fp":"7" * 32,
                   "session_changed":False, "cache_key_changed":False,
                   "change_reason":"new_conversation"}
        send = lambda value: self.ad.handle_event({"type":"extension_ui_request", "method":"notify",
            "message":self.ad.PROVIDER_TRACE_PREFIX + json.dumps(value)}, ctx)
        send(payload)
        payload = {**payload, "request_seq":2, "change_reason":"new_provider_request"}
        send(payload)
        self.assertEqual(ctx["_provider_trace_current"], (epoch, 2))
        self.ad._pi_usage(ctx, {"usage":{"input":100, "cacheRead":80, "totalTokens":120}})
        send({"kind":"usage", "request_epoch":epoch, "request_seq":2,
              "input":100, "cache_read":80, "total":120})
        paths = list((Path(self.tmp.name) / "diagnostics" / "performance" / "pi").glob("*.jsonl"))
        rows = [json.loads(line) for path in paths for line in path.read_text().splitlines()]
        safe = [row for row in rows if row["event"] == "pi_usage"][-1]
        self.assertEqual((safe["request_seq"], safe["cache_read"]), (2, 80))
        self.assertEqual(safe["session_fp"], "1" * 32)
        self.assertEqual(safe["request_epoch_fp"], "9" * 32)
        self.assertEqual(safe["transport"], "http_sse")
        self.assertFalse(safe["continuation_used"])
        self.assertEqual(safe["identity_mode"], "native")
        self.assertNotIn("_provider_trace_current", ctx)

    def test_usage_accumulates_all_provider_requests_in_one_desk_turn(self):
        ctx = self.ctx()
        self.ad._pi_usage(ctx, {"usage": {"input": 100, "output": 20,
            "cacheRead": 80, "cacheWrite": 0, "totalTokens": 120}})
        self.ad._pi_usage(ctx, {"usage": {"input": 40, "output": 10,
            "cacheRead": 30, "cacheWrite": 5, "totalTokens": 50}})
        self.assertEqual(ctx["_token_usage"], {
            "total": 170, "input": 140, "output": 30,
            "cached": 110, "cache_write": 5})
        self.assertEqual(ctx["_pi_tokens"], 170)

    def test_invalid_usage_does_not_consume_request_identity(self):
        ctx = self.ctx(); ctx["seq"] = 17
        epoch = "12345678-1234-4123-8123-123456789abc"
        send = lambda value: self.ad.handle_event({"type":"extension_ui_request", "method":"notify",
            "message":self.ad.PROVIDER_TRACE_PREFIX + json.dumps(value)}, ctx)
        send({"kind":"request", "request_epoch":epoch, "request_seq":1,
              "request_epoch_fp":"9" * 32, "session_fp":"1" * 32})
        send({"kind":"usage", "request_epoch":epoch, "request_seq":1, "input":10})
        self.assertIn((epoch, 1), ctx["_provider_traces"])
        send({"kind":"usage", "request_epoch":epoch, "request_seq":1,
              "input":10, "cache_read":0, "total":12})
        self.assertNotIn((epoch, 1), ctx["_provider_traces"])

    def test_provider_trace_rejects_old_epoch_and_nonmonotonic_sequence(self):
        ctx = self.ctx(); ctx["proc"] = mock.Mock(pid=4321)
        epoch = "12345678-1234-4123-8123-123456789abc"
        other = "87654321-4321-4321-8321-cba987654321"
        def send(value):
            self.ad.handle_event({"type":"extension_ui_request", "method":"notify",
                "message":self.ad.PROVIDER_TRACE_PREFIX + json.dumps(value)}, ctx)
        base = {"kind":"request", "request_epoch":epoch, "request_seq":1,
                "fingerprint_epoch_fp":"8" * 32, "session_fp":"1" * 32}
        send(base); send({**base, "request_seq":2})
        self.assertEqual(ctx["_provider_trace_current"], (epoch, 2))
        send(base)
        send({**base, "request_epoch":other, "request_seq":2})
        wire = {"kind":"wire", "request_epoch":other, "request_seq":1,
                "fingerprint_epoch_fp":"8" * 32, "transport":"http_sse"}
        send(wire)
        self.assertEqual(ctx["_provider_trace_current"], (epoch, 2))
        self.assertNotIn((other, 1), ctx["_provider_traces"])
        send({**base, "request_epoch":other, "request_seq":1})
        self.assertEqual(ctx["_provider_trace_current"], (other, 1))
        send(base)
        self.assertEqual(ctx["_provider_trace_current"], (other, 1))

    def test_provider_trace_rejects_oversized_notification(self):
        ctx = self.ctx()
        message = self.ad.PROVIDER_TRACE_PREFIX + "{" + "x" * 9000
        self.ad.handle_event({"type":"extension_ui_request", "method":"notify",
            "message":message}, ctx)
        self.assertNotIn("_provider_trace_current", ctx)

    def test_provider_trace_rejects_unknown_or_sensitive_fields(self):
        ctx = self.ctx(); epoch = "12345678-1234-4123-8123-123456789abc"
        base = {"kind":"request", "request_epoch":epoch, "request_seq":1,
                "session_fp":"1" * 32}
        for extra in ({"prompt":"SECRET"}, {"session_fp":"RAW_SESSION"},
                      {"transport":"https://private"}, {"continuation_used":1},
                      {"identity_mode":"future"}):
            payload = {**base, **extra}
            self.ad.handle_event({"type":"extension_ui_request", "method":"notify",
                "message":self.ad.PROVIDER_TRACE_PREFIX + json.dumps(payload)}, ctx)
            self.assertNotIn("_provider_trace_current", ctx)

    def test_usage_omits_unavailable_request_and_turn_sequence(self):
        ctx = self.ctx()
        self.ad._pi_usage(ctx, {"usage": {"input": 1, "totalTokens": 2}})
        with open(os.path.join(self.ad.dir, "trace", "adp_test.jsonl"), encoding="utf-8") as f:
            usage = [json.loads(line) for line in f if '"ev": "pi.usage"' in line][-1]
        self.assertNotIn("seq", usage)
        self.assertNotIn("request_seq", usage)

    def test_settled_is_authoritative_timing_boundary(self):
        ctx = self.ctx()
        with mock.patch("engine_adapters.pi.time.time", return_value=2.5):
            self.ad.handle_event({"type": "agent_settled"}, ctx)
        stats = [e for e in self.bus.events if e["type"] == "session.stats"][-1]
        self.assertEqual(stats["properties"], {
            "sessionID": "adp_test", "tokens": 0, "ms": 2499, "cost": 0})
        self.assertEqual(ctx["ms"], 2499)
        self.assertTrue(ctx["_close_stdin"])

    def test_thinking_stream_uses_pi_native_content(self):
        ctx = self.ctx()
        self.ad.handle_event(update("thinking_delta", 0, delta="检查现状"), ctx)
        part = ctx["parts"][ctx["order"][-1]]
        self.assertEqual(part["text"], "检查现状")
        deltas = [event for event in self.bus.events if event["type"] == "message.part.delta"]
        self.assertEqual(deltas[-1]["properties"]["delta"], "检查现状")

    def test_tool_updates_advance_progress_only_for_new_cumulative_snapshots(self):
        ctx = self.ctx()
        ctx["_progress_at"] = 100.0
        start = {"type":"tool_execution_start", "toolCallId":"one",
                 "toolName":"bash", "args":{}}
        empty = {"type":"tool_execution_update", "toolCallId":"one",
                 "toolName":"bash", "partialResult":{"content":[]}}
        first = {"type":"tool_execution_update", "toolCallId":"one",
                 "toolName":"bash", "partialResult":{"content":[
                     {"type":"text", "text":"output"}]}}
        changed = {"type":"tool_execution_update", "toolCallId":"one",
                   "toolName":"bash", "partialResult":{"content":[
                       {"type":"text", "text":"output\nmore"}]}}
        with mock.patch("engine_adapters.pi.time.time", return_value=200.0):
            self.ad.handle_event(start, ctx)
        self.assertEqual(ctx["_progress_at"], 200.0)
        with mock.patch("engine_adapters.pi.time.time", return_value=300.0):
            self.ad.handle_event(empty, ctx)
        self.assertEqual(ctx["_progress_at"], 200.0)
        with mock.patch("engine_adapters.pi.time.time", return_value=400.0):
            self.ad.handle_event(first, ctx)
        self.assertEqual(ctx["_progress_at"], 400.0)
        with mock.patch("engine_adapters.pi.time.time", return_value=500.0):
            self.ad.handle_event(first, ctx)
        self.assertEqual(ctx["_progress_at"], 400.0)
        with mock.patch("engine_adapters.pi.time.time", return_value=600.0):
            self.ad.handle_event(changed, ctx)
        self.assertEqual(ctx["_progress_at"], 600.0)
        with mock.patch("engine_adapters.pi.time.time", return_value=700.0):
            self.ad.handle_event(first, ctx)
        self.assertEqual(ctx["_progress_at"], 600.0)

    def test_tool_update_after_end_is_not_projected_as_progress(self):
        ctx = self.ctx()
        start = {"type":"tool_execution_start", "toolCallId":"one",
                 "toolName":"bash", "args":{}}
        update_event = {"type":"tool_execution_update", "toolCallId":"one",
                        "toolName":"bash", "partialResult":{"content":[
                            {"type":"text", "text":"output"}]}}
        self.ad.handle_event(start, ctx)
        self.ad.handle_event(update_event, ctx)
        with mock.patch("engine_adapters.pi.time.time", return_value=300.0):
            self.ad.handle_event({"type":"tool_execution_end", "toolCallId":"one",
                "toolName":"bash", "result":{"content":[]}, "isError":False}, ctx)
        self.assertEqual(ctx["_progress_at"], 300.0)
        with mock.patch("engine_adapters.pi.time.time", return_value=400.0):
            self.ad.handle_event(update_event, ctx)
        self.assertEqual(ctx["_progress_at"], 300.0)

    def test_complete_assistant_snapshot_is_progress_only_with_content(self):
        ctx = self.ctx(); ctx["_progress_at"] = 100.0
        with mock.patch("engine_adapters.pi.time.time", return_value=200.0):
            self.ad.handle_event({"type":"message_end", "message":{
                "role":"assistant", "stopReason":"stop", "content":[]}}, ctx)
        self.assertEqual(ctx["_progress_at"], 100.0)
        with mock.patch("engine_adapters.pi.time.time", return_value=300.0):
            self.ad.handle_event({"type":"message_end", "message":{
                "role":"assistant", "stopReason":"stop",
                "content":[{"type":"text", "text":"answer"}]}}, ctx)
        self.assertEqual(ctx["_progress_at"], 300.0)

    def test_managed_subagent_identity_is_public_in_live_tool_projection(self):
        ctx = self.ctx()
        internal = "desk-scout--desktron-0123456789abcdef0123456789abcdef"
        self.ad.handle_event({
            "type": "tool_execution_start", "toolCallId": "subagent-1",
            "toolName": "subagent", "args": {
                "agent": internal, "task": "inspect", "agentScope": "user"}}, ctx)
        part = ctx["parts"][ctx["order"][-1]]
        self.assertEqual(part["state"]["input"]["agent"], "desk-scout")
        self.assertNotIn("desktron-", json.dumps(part, ensure_ascii=False))

    def test_subagent_tool_failure_stays_error_in_live_projection(self):
        ctx = self.ctx()
        self.ad.handle_event({
            "type": "tool_execution_start", "toolCallId": "subagent-failed",
            "toolName": "subagent", "args": {"agent": "desk-scout", "task": "inspect"}}, ctx)
        self.ad.handle_event({
            "type": "tool_execution_end", "toolCallId": "subagent-failed",
            "toolName": "subagent", "isError": True,
            "result": {"content": [{"type": "text", "text": "child provider unavailable"}]}}, ctx)
        part = ctx["parts"][ctx["order"][-1]]
        self.assertEqual(part["state"]["status"], "error")
        self.assertEqual(part["state"]["output"], "child provider unavailable")

    def test_tool_progress_is_bounded_and_throttled_but_final_output_is_preserved(self):
        ctx = self.ctx()
        start = {"type": "tool_execution_start", "toolCallId": "child-1",
                 "toolName": "subagent", "args": {"agent": "desk-scout", "task": "inspect"}}
        self.ad.handle_event(start, ctx)
        part = ctx["parts"][ctx["order"][-1]]
        updates = lambda: [e for e in self.bus.events if e["type"] == "message.part.updated"]

        frame = lambda text: {"type": "tool_execution_update", "toolCallId": "child-1",
                              "toolName": "subagent",
                              "partialResult": {"content": [{"type": "text", "text": text}]}}
        with mock.patch("engine_adapters.pi.time.monotonic", return_value=10):
            self.ad.handle_event(frame("a" * 6000), ctx)
        emitted = len(updates())
        self.assertEqual(part["state"]["output"], "a" * 6000)
        sent = updates()[-1]["properties"]["part"]
        self.assertEqual(sent["state"]["output"], "a" * self.ad.TOOL_UPDATE_CAP)
        self.assertEqual(sent["state"]["outputMore"], 6000)
        self.assertTrue(sent["progressOnly"])
        self.assertEqual(sent["state"]["progressRevision"], 1)
        self.assertEqual(sent["state"]["progressTotal"], 6000)
        self.assertNotIn("input", sent["state"])
        self.assertNotIn("call_id", sent)
        self.assertEqual(ctx["_pi_tool_checkpoint_at"][part["id"]], 10)

        with mock.patch("engine_adapters.pi.time.monotonic", return_value=10.1):
            self.ad.handle_event(frame("b" * 7000), ctx)
        self.assertEqual(len(updates()), emitted)
        self.assertEqual(part["state"]["output"], "b" * 7000)

        with mock.patch("engine_adapters.pi.time.monotonic", return_value=11):
            self.ad.handle_event(frame("c" * 8000), ctx)
        self.assertEqual(len(updates()), emitted + 1)
        self.assertEqual(part["state"]["output"], "c" * 8000)
        sent = updates()[-1]["properties"]["part"]
        self.assertEqual(sent["state"]["output"], "c" * self.ad.TOOL_UPDATE_CAP)
        self.assertEqual(sent["state"]["outputMore"], 8000)
        self.assertEqual(sent["state"]["progressRevision"], 2)

        # 等长替换不能靠字符串长度去重；内容变了就推进 revision。
        with mock.patch("engine_adapters.pi.time.monotonic", return_value=12):
            self.ad.handle_event(frame("d" * 8000), ctx)
        self.assertEqual(len(updates()), emitted + 2)
        self.assertEqual(updates()[-1]["properties"]["part"]["state"]["progressRevision"], 3)

        # 无文本 heartbeat 仍推进完整 checkpoint，但不再重复发布同一份浏览器预览。
        before_heartbeat = len(updates())
        with mock.patch("engine_adapters.pi.time.monotonic", return_value=13):
            self.ad.handle_event(frame(""), ctx)
        self.assertEqual(len(updates()), before_heartbeat)
        self.assertEqual(ctx["_pi_tool_checkpoint_at"][part["id"]], 10)
        with mock.patch("engine_adapters.pi.time.monotonic", return_value=16):
            self.ad.handle_event(frame(""), ctx)
        self.assertEqual(len(updates()), before_heartbeat)
        self.assertEqual(ctx["_pi_tool_checkpoint_at"][part["id"]], 16)

        final = "z" * 12000
        self.ad.handle_event({"type": "tool_execution_end", "toolCallId": "child-1",
                              "toolName": "subagent", "isError": False,
                              "result": {"content": [{"type": "text", "text": final}]}}, ctx)
        self.assertEqual(part["state"]["output"], final)
        self.assertNotIn("outputMore", part["state"])

        # 有些工具终帧不重复 result；完整的最后一份 partial 仍须进入记录，而非只剩 2KB 预览。
        self.ad.handle_event({**start, "toolCallId": "child-2"}, ctx)
        with mock.patch("engine_adapters.pi.time.monotonic", return_value=20):
            self.ad.handle_event({**frame("p" * 9000), "toolCallId": "child-2"}, ctx)
        second = ctx["parts"][ctx["order"][-1]]
        self.ad.handle_event({"type": "tool_execution_end", "toolCallId": "child-2",
                              "toolName": "subagent", "isError": False,
                              "result": {"content": []}}, ctx)
        self.assertEqual(second["state"]["output"], "p" * 9000)
        self.assertNotIn("outputMore", second["state"])

        self.ad.handle_event({**start, "toolCallId": "child-3"}, ctx)
        with mock.patch("engine_adapters.pi.time.monotonic", return_value=30):
            self.ad.handle_event({**frame("x" * 11000), "toolCallId": "child-3"}, ctx)
        third = ctx["parts"][ctx["order"][-1]]
        # 没有 tool_execution_end 的强退路径直接持久化 ctx；其中必须已经是完整 partial。
        self.assertEqual(third["state"]["output"], "x" * 11000)
        self.assertNotIn("outputMore", third["state"])

    def test_edit_inputs_follow_review_contract(self):
        ctx = self.ctx()
        self.ad.handle_event(update("toolcall_end", 0, toolCall={
            "id": "write-1", "name": "write",
            "arguments": {"path": "/tmp/note.md", "content": "new\n"}}), ctx)
        write = ctx["parts"][ctx["order"][-1]]
        self.assertEqual(write["state"]["input"]["file_path"], "/tmp/note.md")

        self.ad.handle_event(update("toolcall_end", 1, toolCall={
            "id": "edit-1", "name": "edit", "arguments": {
                "path": "/tmp/note.md",
                "edits": [{"oldText": "old", "newText": "new"}]}}), ctx)
        edit = ctx["parts"][ctx["order"][-1]]
        self.assertEqual(edit["state"]["input"]["file_path"], "/tmp/note.md")
        self.assertEqual(edit["state"]["input"]["edits"][0]["old_string"], "old")
        self.assertEqual(edit["state"]["input"]["edits"][0]["new_string"], "new")

        self.ad.handle_event({
            "type": "tool_execution_end", "toolCallId": "edit-1", "toolName": "edit",
            "result": {"content": [{"type": "text", "text": "ok"}],
                       "details": {"firstChangedLine": 37, "patch": "private source"}},
            "isError": False}, ctx)
        self.assertEqual(edit["state"]["line_start"], 37)
        self.assertNotIn("details", edit["state"])
        self.assertNotIn("patch", edit["state"])

        failed = self.ctx()
        self.ad.handle_event(update("toolcall_end", 0, toolCall={
            "id": "edit-failed", "name": "edit", "arguments": {
                "path": "/tmp/note.md", "edits": [{"oldText": "x", "newText": "y"}]} }), failed)
        self.ad.handle_event({
            "type": "tool_execution_end", "toolCallId": "edit-failed", "toolName": "edit",
            "result": {"details": {"firstChangedLine": 99}}, "isError": True}, failed)
        failed_part = failed["parts"][failed["order"][-1]]
        self.assertNotIn("line_start", failed_part["state"])

    def test_edit_history_restores_only_first_changed_line(self):
        parts = self.ad._pi_parts([{
            "type": "toolCall", "id": "edit-history", "name": "edit",
            "arguments": {"path": "/tmp/note.md",
                          "edits": [{"oldText": "old", "newText": "new"}]},
        }], "msg", "sid", results={"edit-history": {
            "role": "toolResult", "toolCallId": "edit-history", "isError": False,
            "content": [{"type": "text", "text": "ok"}],
            "details": {"firstChangedLine": 42, "patch": "private source"},
        }})
        self.assertEqual(parts[0]["state"]["line_start"], 42)
        self.assertNotIn("details", parts[0]["state"])
        self.assertNotIn("patch", parts[0]["state"])

    def test_model_error_uses_environment_bar_not_message_body(self):
        ctx = self.ctx()
        self.ad.handle_event({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "error",
            "errorMessage": "Request timed out."}}, ctx)
        self.assertEqual(ctx["order"], [])
        self.assertNotIn("engine_error", ctx)
        self.ad.handle_event({"type": "agent_settled"}, ctx)
        self.assertEqual(ctx["engine_error"], "Request timed out.")
        env = [e for e in self.bus.events if e["type"] == "session.environment"][-1]
        self.assertEqual(env["properties"]["label"], "Request timed out.")
        self.assertEqual(env["properties"]["stage"], "turn")
        self.assertEqual(env["properties"]["scope"], "turn")

    def test_success_after_retry_clears_environment_error(self):
        ctx = self.ctx()
        self.ad.handle_event({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "error",
            "errorMessage": "Connection error."}}, ctx)
        self.ad.handle_event({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "stop"}}, ctx)
        self.assertNotIn("engine_error", ctx)
        envs = [e for e in self.bus.events if e["type"] == "session.environment"]
        self.assertFalse(envs, "Recovered attempt must never publish a terminal error")
        self.assertNotIn("_request_failure", ctx)

    def test_history_error_is_notice_not_assistant_text(self):
        path = os.path.join(self.tmp.name, "pi.jsonl")
        rows = [
            {"type": "session", "id": "pi-1", "cwd": self.tmp.name},
            {"type": "message", "id": "u1", "message": {
                "role": "user", "content": [{"type": "text", "text": "继续"}]}},
            {"type": "message", "id": "a1", "message": {
                "role": "assistant", "content": [], "errorMessage": "Request timed out."}},
        ]
        with open(path, "w", encoding="utf-8") as file:
            for row in rows:
                file.write(json.dumps(row) + "\n")
        sink = {}
        messages = self.ad._pi_group(path, "adp_test", sink=sink)
        texts = [part.get("text") for message in messages for part in message["parts"]]
        self.assertNotIn("(Pi 错误：Request timed out.)", texts)
        self.assertTrue(messages[-1]["info"]["failed"])
        self.assertEqual(messages[-1]["info"]["error"], {
            "source": "engine", "code": "UNKNOWN", "message": "Request timed out."})
        self.assertEqual(sink["notice"], "Request timed out.")
        sid = self.ad.create_session()["id"]
        with mock.patch.object(self.ad, "_pi_path", return_value=path):
            self.assertIsNotNone(self.ad.import_session("pi-1", sid=sid))
        env = [e for e in self.bus.events if e["type"] == "session.environment"][-1]
        self.assertEqual(env["properties"]["label"], "Request timed out.")
        self.assertEqual(self.ad.session_environment(sid)["stage"], "engine")

    def test_extension_error_is_fail_closed(self):
        ctx = self.ctx()
        proc = Proc()
        ctx["proc"] = proc
        self.ad.handle_event({"type": "extension_error", "error": "load failed"}, ctx)
        self.assertTrue(proc.killed)
        self.assertEqual(ctx["engine_error"], "load failed")

    def test_compaction_summary_extension_error_is_not_permission_gate(self):
        ctx = self.ctx()
        proc = Proc()
        ctx["proc"] = proc
        err = "Compaction summary failed: Provider 账号额度不足"
        self.ad.handle_event({"type": "extension_error", "error": err}, ctx)
        self.assertFalse(proc.killed)
        self.assertNotIn("engine_error", ctx)
        self.assertNotIn("engine_failure", ctx)
        self.assertEqual(ctx["_pi_compaction_error"], err)
        self.assertEqual(ctx["_pi_compaction_failure"]["code"], "COMPACTION_EXTENSION_ERROR")
        env = [e for e in self.bus.events if e["type"] == "session.environment"][-1]
        self.assertEqual(env["properties"]["stage"], "compaction")
        self.assertEqual(env["properties"]["state"], "error")
        self.assertEqual(env["properties"]["label"], "上下文压缩失败")
        self.assertIn("Provider 账号额度不足", env["properties"]["details"][0])
        self.assertNotEqual(env["properties"].get("label"), "Pi 权限闸异常")


class Permission(PiCase):
    def test_interrupt_cancels_pending_dialog_and_rejects_late_approval(self):
        ctx = self.ctx()
        proc = ctx["proc"] = Proc()
        frame = {"type": "extension_ui_request", "id": "pending", "method": "confirm",
                 "title": self.ad.GATE_PREFIX + "bash", "message": "{}"}
        with mock.patch("engine_adapters.pi.threading.Timer"):
            self.ad.handle_event(frame, ctx)
        ctx["aborted"] = "interrupt"
        self.assertTrue(self.ad._soft_interrupt(ctx["sid"], proc))
        self.assertEqual([json.loads(line) for line in proc.stdin.getvalue().splitlines()], [
            {"type": "abort"}, {"type": "extension_ui_response", "id": "pending", "cancelled": True}])
        self.assertFalse(self.ad.respond_permission("pending", True))
        self.bus.events.clear()
        self.ad.handle_event(dict(frame, id="late"), ctx)
        self.assertFalse(any(e["type"] == "permission.asked" for e in self.bus.events))
        self.assertTrue(json.loads(proc.stdin.getvalue().splitlines()[-1])["cancelled"])

    def test_gate_confirm_round_trip(self):
        ctx = self.ctx()
        proc = Proc()
        ctx["proc"] = proc
        frame = {"type": "extension_ui_request", "id": "req-1", "method": "confirm",
                 "title": self.ad.GATE_PREFIX + "bash",
                 "message": json.dumps({"tool": "bash", "input": {"command": "pwd"}}),
                 "timeout": 300000}
        with mock.patch("engine_adapters.pi.threading.Timer") as timer:
            self.ad.handle_event(frame, ctx)
            timer.return_value.start.assert_called_once()
        asked = [e for e in self.bus.events if e["type"] == "permission.asked"][-1]
        self.assertEqual(asked["properties"]["tool"], "bash")
        self.assertTrue(self.ad.respond_permission("req-1", True))
        self.assertEqual(json.loads(proc.stdin.getvalue()), {
            "type": "extension_ui_response", "id": "req-1", "confirmed": True})

    def test_pending_approval_survives_state_refresh_and_timeout_clears_it(self):
        ctx = self.ctx()
        proc = ctx["proc"] = Proc()
        frame = {"type": "extension_ui_request", "id": "refresh", "method": "confirm",
                 "title": self.ad.GATE_PREFIX + "bash", "message": "{}", "timeout": 30000}
        with mock.patch("engine_adapters.pi.threading.Timer") as timer:
            self.ad.handle_event(frame, ctx)
        self.assertEqual(self.ad.turn_state(ctx["sid"])["asks"][0]["id"], "refresh")
        timer.assert_called_once_with(29, mock.ANY)
        timer.call_args.args[1]()
        self.assertEqual(self.ad.turn_state(ctx["sid"])["asks"], [])
        self.assertEqual(self.bus.events[-1]["properties"]["resolution"], "timeout")
        self.assertEqual(ctx["_pi_permission_resolution"], "timeout")
        self.assertTrue(json.loads(proc.stdin.getvalue())["cancelled"])

    def test_unstructured_error_is_unknown_not_guessed_from_wording(self):
        ctx = self.ctx()
        self.ad._pi_failed(ctx, "authentication timeout network forbidden")
        self.assertNotIn("engine_error", ctx)
        self.ad.handle_event({"type": "agent_settled"}, ctx)
        self.assertEqual(ctx["engine_failure"], {
            "source": "engine", "code": "UNKNOWN",
            "message": "authentication timeout network forbidden"})

    def test_notification_error_recovers_after_successful_model_response(self):
        ctx = self.ctx()
        self.ad.handle_event({"type": "extension_ui_request", "method": "notify",
                              "notifyType": "error", "message": "provider disconnected"}, ctx)
        self.assertEqual(ctx["_request_failure"]["message"], "provider disconnected")
        self.assertNotIn("engine_error", ctx)
        self.ad.handle_event({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "stop", "content": []}}, ctx)
        self.assertNotIn("engine_error", ctx)
        self.assertNotIn("engine_failure", ctx)
        self.assertNotIn("_request_failure", ctx)
        self.assertFalse([e for e in self.bus.events if e["type"] == "session.environment"])

    def test_requested_abort_is_not_an_engine_failure(self):
        ctx = self.ctx()
        self.ad._pi_failed(ctx, "Provider 请求已取消")
        self.assertIn("_request_failure", ctx)  # 不凭文案推断取消，等原生收口。
        ctx["aborted"] = "interrupt"
        self.ad._pi_failed(ctx, "Provider 请求已取消")
        self.assertNotIn("engine_error", ctx)
        self.ad._pi_failed(ctx, "provider disconnected")
        self.assertNotIn("engine_error", ctx)  # 中断后的任意迟到错误都不能重新染红。

    def test_running_tool_has_priority_over_provider_wait(self):
        ctx = self.ctx()
        ctx["_provider_wait"] = {"elapsed_ms":120000, "remaining_ms":3480000}
        ctx["parts"]["tool"] = {"id":"tool", "type":"tool", "tool":"bash",
                                  "state":{"status":"running"}}
        activity = self.ad.turn_activity(ctx["sid"], ctx)
        self.assertEqual(activity["kind"], "tool")
        self.assertEqual(activity["running_tools"], 1)

    def test_compaction_protocol_error_does_not_fail_the_assistant_turn(self):
        ctx = self.ctx()
        self.ad.handle_event({"type": "compaction_start", "reason": "threshold"}, ctx)
        self.ad.handle_event({"type": "extension_ui_request", "method": "notify",
                              "notifyType": "error",
                              "message": "Provider 返回的协议不完整或不受支持（诊断 json-output）"}, ctx)
        self.assertNotIn("engine_error", ctx)
        self.assertNotIn("_pi_notification_error", ctx)
        warning = [e for e in self.bus.events if e["type"] == "session.environment"][-1]
        self.assertEqual(warning["properties"]["stage"], "compaction")
        self.assertEqual(warning["properties"]["state"], "warning")

        self.ad.handle_event({"type": "compaction_end", "reason": "threshold",
                              "errorMessage": "Provider 返回的协议不完整或不受支持",
                              "willRetry": False}, ctx)
        compact = [e for e in self.bus.events if e["type"] == "session.compact"][-1]
        self.assertEqual(compact["properties"]["result"], "failed")
        self.assertIn("协议不完整", compact["properties"]["error"])
        self.assertTrue(ctx["_pi_compaction_failed"])
        self.assertNotIn("engine_error", ctx)
        self.assertFalse(any(e["type"] == "session.environment"
                             and e["properties"].get("stage") == "engine"
                             and e["properties"].get("state") == "error"
                             for e in self.bus.events))

    def test_successful_compaction_always_publishes_recovery_fence(self):
        ctx = self.ctx()
        self.ad.handle_event({"type": "compaction_start", "reason": "manual"}, ctx)
        self.ad.handle_event({"type": "compaction_end", "reason": "manual",
                              "status": "completed", "result": {"tokensBefore": 100}}, ctx)
        recovered = [e for e in self.bus.events if e["type"] == "session.environment"][-1]
        props = recovered["properties"]
        self.assertEqual((props["state"], props["stage"]), ("ready", "compaction"))
        self.assertGreaterEqual(props["resolvedThrough"], props["startedAt"])

    def test_explicit_failed_status_cannot_be_overridden_by_result_payload(self):
        ctx = self.ctx()
        self.ad.handle_event({"type": "compaction_start", "reason": "manual"}, ctx)
        self.ad.handle_event({"type": "compaction_end", "reason": "manual",
                              "status": "failed", "result": {"partial": True},
                              "errorMessage": "WebSocket error"}, ctx)
        compact = [e for e in self.bus.events if e["type"] == "session.compact"][-1]
        self.assertEqual(compact["properties"]["result"], "failed")
        self.assertFalse(any(e["type"] == "session.environment"
                             and e["properties"].get("state") == "ready"
                             and e["properties"].get("stage") == "compaction"
                             for e in self.bus.events))

    def test_success_clears_persisted_legacy_compaction_permission_notice(self):
        ctx = self.ctx()
        self.ad.index[ctx["sid"]] = {"opts":{"model":"example-gw/model"}}
        self.ad._env_publish(ctx["sid"], {"stage":"permission", "state":"error",
            "label":"Pi 权限闸异常", "engine":"pi", "provider":"openai",
            "startedAt":1, "observedAt":1,
            "error":{"source":"engine", "code":"PERMISSION_EXTENSION_ERROR",
                     "message":"Compaction summary failed: WebSocket error"}})
        revision = self.ad.index[ctx["sid"]]["environment_revision"]
        self.ad.handle_event({"type":"compaction_start", "reason":"manual"}, ctx)
        self.ad.handle_event({"type":"compaction_end", "reason":"manual",
                              "status":"completed", "result":{"tokensBefore":100}}, ctx)
        row = self.ad.index[ctx["sid"]]
        self.assertNotIn("permission", row["environment_issues"])
        self.assertNotIn("environment", row)
        self.assertGreater(row["environment_revision"], revision)
        self.assertEqual((self.bus.events[-1]["properties"]["state"],
                          self.bus.events[-1]["properties"]["stage"]),
                         ("ready", "compaction"))

    def test_new_compaction_event_shape_and_later_success_recover_only_compaction(self):
        ctx = self.ctx()
        self.ad.handle_event({"type": "compaction_start", "reason": "threshold"}, ctx)
        self.ad.handle_event({"type": "compaction_end", "reason": "threshold",
                              "status": "failed",
                              "error": {"code": "protocol", "message": "bad finish"}}, ctx)
        failed = [e for e in self.bus.events if e["type"] == "session.compact"][-1]
        self.assertEqual(failed["properties"]["error"], "bad finish")
        self.assertNotIn("engine_error", ctx)

        self.ad.handle_event({"type": "compaction_start", "reason": "threshold"}, ctx)
        self.ad.handle_event({"type": "extension_ui_request", "method": "notify",
                              "notifyType": "error", "message": "transient"}, ctx)
        self.ad.handle_event({"type": "compaction_end", "reason": "threshold",
                              "status": "completed", "result": {"tokensBefore": 100}}, ctx)
        completed = [e for e in self.bus.events if e["type"] == "session.compact"][-1]
        self.assertEqual(completed["properties"]["result"], {"tokensBefore": 100})
        self.assertEqual(completed["properties"]["error"], "")
        self.assertNotIn("_pi_compaction_failed", ctx)
        recovered = [e for e in self.bus.events if e["type"] == "session.environment"][-1]
        props = recovered["properties"]
        self.assertEqual({key: props[key] for key in ("sessionID", "state", "stage", "label")}, {
            "sessionID": ctx["sid"], "state": "ready", "stage": "compaction",
            "label": "上下文压缩已恢复"})
        self.assertEqual(props["scope"], "turn")
        self.assertTrue(props["issueID"])

    def test_aborted_message_end_does_not_commit_process_text_or_publish_error(self):
        ctx = self.ctx()
        ctx["aborted"] = "interrupt"
        self.ad.handle_event({"type": "message_start", "message": {"role": "assistant"}}, ctx)
        self.ad.handle_event(update("text_delta", 0, delta="未完成"), ctx)
        part = ctx["parts"][ctx["order"][-1]]
        self.ad.handle_event({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "aborted", "errorMessage": "Provider 请求已取消"}}, ctx)
        self.assertEqual(part["type"], "reasoning")
        self.assertNotIn("engine_error", ctx)
        self.assertFalse(any(e["type"] == "session.environment"
                             and e["properties"].get("state") == "error"
                             for e in self.bus.events))

    def test_protocol_failure_discards_only_current_unverified_message_parts(self):
        ctx = self.ctx()
        self.ad.handle_event({"type": "message_start", "message": {"role": "assistant"}}, ctx)
        self.ad.handle_event(update("text_delta", 0, delta="已验证的工具前说明"), ctx)
        self.ad.handle_event({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "toolUse",
            "content": [{"type": "toolCall", "id": "c1", "name": "read", "arguments": {}}]}}, ctx)
        kept = list(ctx["order"])
        self.ad.handle_event({"type": "message_start", "message": {"role": "assistant"}}, ctx)
        self.ad.handle_event(update("text_delta", 0, delta="不应持久化的半截"), ctx)
        failed = set(ctx["order"]) - set(kept)
        self.ad.handle_event({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "error",
            "errorMessage": "Provider 返回的协议不完整或不受支持"}}, ctx)
        self.assertTrue(all(pid in ctx["order"] for pid in kept))
        self.assertTrue(all(pid not in ctx["parts"] for pid in failed))
        # This sample has only error text, not a structured PROTOCOL control frame.
        self.assertNotIn("engine_error", ctx)
        self.ad.handle_event({"type": "agent_settled"}, ctx)
        self.assertTrue(ctx["engine_error"].startswith("Provider 返回"))
        replacements = [e["properties"]["part"] for e in self.bus.events
                        if e["type"] == "message.part.updated"
                        and e["properties"]["part"].get("replace")]
        self.assertTrue(any(part.get("text") == "" for part in replacements))

    def test_question_cancel_round_trip(self):
        ctx = self.ctx()
        proc = Proc()
        ctx["proc"] = proc
        frame = {"type": "extension_ui_request", "id": "req-2", "method": "select",
                 "title": "选一个", "options": ["甲", "乙"]}
        with mock.patch("engine_adapters.pi.threading.Timer"):
            self.ad.handle_event(frame, ctx)
        self.assertTrue(self.ad.respond_permission("req-2", False))
        self.assertTrue(json.loads(proc.stdin.getvalue())["cancelled"])


class Transcript(PiCase):

    def test_native_aborted_history_is_interrupted_not_failed(self):
        path = self.write_rows([
            {"type": "message", "id": "u", "message": {
                "role": "user", "timestamp": 100, "content": "停止"}},
            {"type": "message", "id": "a", "message": {
                "role": "assistant", "provider": "example-gw", "timestamp": 101,
                "stopReason": "aborted", "errorMessage": "Provider 请求已取消", "content": []}},
        ])
        sink = {"notice": "old"}
        messages = self.ad._pi_group(path, "adp_test", sink=sink)
        info = messages[-1]["info"]
        self.assertTrue(info["interrupted"])
        self.assertNotIn("failed", info)
        self.assertNotIn("error", info)
        self.assertEqual(sink["notice"], "")

    def test_later_success_in_same_turn_clears_historical_failure(self):
        path = self.write_rows([
            {"type": "message", "id": "u", "message": {
                "role": "user", "timestamp": 100, "content": "重试"}},
            {"type": "message", "id": "a1", "message": {
                "role": "assistant", "provider": "example-gw", "timestamp": 101,
                "stopReason": "error", "errorMessage": "temporary", "content": []}},
            {"type": "message", "id": "a2", "message": {
                "role": "assistant", "provider": "example-gw", "timestamp": 102,
                "stopReason": "stop", "content": [{"type": "text", "text": "已恢复"}]}},
        ])
        sink = {}
        messages = self.ad._pi_group(path, "adp_test", sink=sink)
        info = messages[-1]["info"]
        self.assertNotIn("failed", info)
        self.assertNotIn("error", info)
        self.assertEqual(sink["notice"], "")
        self.assertEqual(messages[-1]["parts"][-1]["text"], "已恢复")

    def test_compaction_retry_success_removes_pre_compaction_error_from_history(self):
        path = self.write_rows([
            {"type": "message", "id": "u", "message": {
                "role": "user", "timestamp": 100, "content": "继续"}},
            {"type": "message", "id": "a1", "message": {
                "role": "assistant", "provider": "example-gw", "timestamp": 101,
                "stopReason": "error", "errorMessage": "context overflow", "content": []}},
            {"type": "compaction", "id": "c1", "timestamp": "1970-01-01T00:00:00.102Z",
             "summary": "压缩摘要", "tokensBefore": 160000},
            {"type": "message", "id": "a2", "message": {
                "role": "assistant", "provider": "example-gw", "timestamp": 103,
                "stopReason": "stop", "content": [{"type": "text", "text": "恢复成功"}]}},
        ])
        messages = self.ad._pi_group(path, "adp_test")
        self.assertFalse(any((m.get("info") or {}).get("failed") for m in messages))
        self.assertEqual([m["info"]["role"] for m in messages], ["user", "user", "assistant"])
        self.assertEqual(messages[-1]["parts"][-1]["text"], "恢复成功")

    def test_refresh_preserves_structured_context_code_for_terminal_failure(self):
        sid = self.ad.create_session()["id"]
        self.ad._append_msg(sid, {"info": {
            "id": "local", "role": "assistant", "sessionID": sid,
            "failed": True, "local_only": True, "turn_t0": 100, "at": 300,
            "error": {"source": "provider", "provider": "example-gw",
                      "code": "CONTEXT", "message": "context overflow"}}, "parts": []})
        path = self.write_rows([
            {"type": "message", "id": "u", "message": {
                "role": "user", "timestamp": 100, "content": "继续"}},
            {"type": "message", "id": "a", "message": {
                "role": "assistant", "provider": "example-gw", "timestamp": 200,
                "stopReason": "error", "errorMessage": "context overflow", "content": []}},
        ])
        with mock.patch.object(self.ad, "_pi_path", return_value=path):
            self.assertIsNotNone(self.ad.import_session("pi-session", sid=sid))
        failed = [m for m in self.ad.messages(sid) if (m.get("info") or {}).get("failed")]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["info"]["error"]["code"], "CONTEXT")

    def test_tool_commentary_matches_live_projection_without_rewriting_history(self):
        path = self.write_rows([
            {"type": "message", "id": "u", "message": {
                "role": "user", "timestamp": 100, "content": "检查"}},
            {"type": "message", "id": "a1", "message": {
                "role": "assistant", "stopReason": "toolUse", "content": [
                    {"type": "text", "text": "检查配置"},
                    {"type": "toolCall", "name": "read", "id": "c1"}]}},
            {"type": "message", "id": "a2", "message": {
                "role": "assistant", "content": [
                    {"type": "text", "text": "验证结果"},
                    {"type": "toolCall", "name": "bash", "id": "c2"}]}},
            {"type": "message", "id": "a3", "message": {
                "role": "assistant", "stopReason": "stop", "content": [
                    {"type": "thinking", "thinking": "核对"},
                    {"type": "text", "text": "检查完成"}]}},
        ])
        original = Path(path).read_bytes()
        parts = self.ad._pi_group(path, "adp_test")[1]["parts"]
        self.assertEqual([p["type"] for p in parts],
                         ["reasoning", "tool", "reasoning", "tool", "reasoning", "text"])
        self.assertEqual([p["text"] for p in parts if p["type"] == "text"], ["检查完成"])
        self.assertEqual(Path(path).read_bytes(), original)

    def write_rows(self, rows):
        path = os.path.join(self.tmp.name, "pi.jsonl")
        with open(path, "w", encoding="utf-8") as file:
            for row in rows:
                if row == "bad":
                    file.write("{malformed\n")
                else:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def test_import_groups_one_tool_turn_and_skips_bad_line(self):
        path = self.write_rows([
            {"type": "session", "id": "sess-1", "cwd": "/tmp"}, "bad",
            {"type": "message", "id": "u1", "message": {
                "role": "user", "timestamp": 1000, "content": "查看文件"}},
            {"type": "message", "id": "a1", "message": {
                "role": "assistant", "timestamp": 1001, "content": [
                    {"type": "thinking", "thinking": "先读"},
                    {"type": "toolCall", "id": "c1", "name": "read",
                     "arguments": {"path": "/tmp/a"}}]}},
            {"type": "message", "id": "r1", "message": {
                "role": "toolResult", "toolCallId": "c1", "isError": False,
                "content": [{"type": "text", "text": "hello"}]}},
            {"type": "message", "id": "a2", "message": {
                "role": "assistant", "timestamp": 1002,
                "content": [{"type": "text", "text": "读完了"}]}},
        ])
        with mock.patch.object(ea.PiAdapter, "_pi_path", staticmethod(lambda _sid: path)), \
                mock.patch.object(self.ad, "_set_engine_sid"):
            result = self.ad.import_session("sess-1")
        messages = self.ad.messages(result["id"])
        self.assertEqual([m["info"]["role"] for m in messages], ["user", "assistant"])
        parts = messages[1]["parts"]
        self.assertEqual([p["type"] for p in parts], ["reasoning", "tool", "text"])
        self.assertEqual(parts[1]["state"]["output"], "hello")
        self.assertTrue(self.ad.index[result["id"]]["engine_started"])

    def test_parent_chain_selects_current_branch(self):
        path = self.write_rows([
            {"type": "session", "id": "sess-2", "cwd": "/tmp"},
            {"type": "message", "id": "u1", "parentId": None,
             "message": {"role": "user", "content": "问"}},
            {"type": "message", "id": "a-old", "parentId": "u1",
             "message": {"role": "assistant", "content": "旧分支"}},
            {"type": "message", "id": "a-new", "parentId": "u1",
             "message": {"role": "assistant", "content": "当前分支"}},
        ])
        grouped = self.ad._pi_group(path, "adp_x")
        text = json.dumps(grouped, ensure_ascii=False)
        self.assertIn("当前分支", text)
        self.assertNotIn("旧分支", text)

    def test_managed_subagent_identity_is_public_after_history_refresh(self):
        internal = "desk-reviewer--desktron-fedcba9876543210fedcba9876543210"
        parts = self.ad._pi_parts([{
            "type": "toolCall", "id": "subagent-history", "name": "subagent",
            "arguments": {"agent": internal, "task": "review", "agentScope": "user"},
        }], "msg", "sid")
        self.assertEqual(parts[0]["state"]["input"]["agent"], "desk-reviewer")
        self.assertNotIn("desktron-", json.dumps(parts, ensure_ascii=False))

    def test_subagent_tool_failure_stays_error_after_history_refresh(self):
        parts = self.ad._pi_parts([{
            "type": "toolCall", "id": "subagent-failed", "name": "subagent",
            "arguments": {"agent": "desk-scout", "task": "inspect"},
        }], "msg", "sid", results={"subagent-failed": {
            "role": "toolResult", "toolCallId": "subagent-failed", "isError": True,
            "content": [{"type": "text", "text": "child provider unavailable"}],
        }})
        self.assertEqual(parts[0]["state"]["status"], "error")
        self.assertEqual(parts[0]["state"]["output"], "child provider unavailable")

    def test_history_edit_inputs_follow_review_contract(self):
        parts = self.ad._pi_parts([
            {"type": "toolCall", "id": "w1", "name": "write",
             "arguments": {"path": "/tmp/a.md", "content": "hello"}},
            {"type": "toolCall", "id": "e1", "name": "edit",
             "arguments": {"path": "/tmp/a.md", "edits": [
                 {"oldText": "hello", "newText": "world"}]}}],
            "m1", "adp_test")
        self.assertEqual(parts[0]["state"]["input"]["file_path"], "/tmp/a.md")
        edit = parts[1]["state"]["input"]
        self.assertEqual(edit["file_path"], "/tmp/a.md")
        self.assertEqual(edit["edits"][0]["old_string"], "hello")
        self.assertEqual(edit["edits"][0]["new_string"], "world")

    def test_history_projects_pi_native_thinking(self):
        raw = "检查现状后再决定下一步"
        parts = self.ad._pi_parts([
            {"type": "thinking", "thinking": raw}], "m1", "adp_test")
        self.assertEqual(parts[0]["text"], raw)

    def test_history_hides_only_openai_title_only_thinking(self):
        titles = "**Planning eval schema**\n\n**Defining tool policies**"
        self.assertTrue(is_openai_summary_titles_only("openai", titles))
        self.assertEqual(self.ad._pi_parts(
            [{"type": "thinking", "thinking": titles}], "m1", "adp_test",
            provider="openai"), [])
        self.assertFalse(is_openai_summary_titles_only(
            "openai", "**Planning** with visible prose **kept**"))
        visible = titles + "\n\nThe schema needs two required fields."
        self.assertEqual(self.ad._pi_parts(
            [{"type": "thinking", "thinking": visible}], "m2", "adp_test",
            provider="openai")[0]["text"], visible)
        self.assertEqual(self.ad._pi_parts(
            [{"type": "thinking", "thinking": titles}], "m3", "adp_test",
            provider="deepseek")[0]["text"], titles)

    def test_grouped_history_applies_openai_title_filter_from_message_provider(self):
        path = self.write_rows([
            {"type": "message", "id": "u", "message": {
                "role": "user", "timestamp": 100, "content": "继续"}},
            {"type": "message", "id": "a", "message": {
                "role": "assistant", "provider": "openai", "timestamp": 101,
                "stopReason": "stop", "content": [
                    {"type": "thinking", "thinking": "**Planning eval schema**"},
                    {"type": "text", "text": "完成"}]}},
        ])
        parts = self.ad._pi_group(path, "adp_test")[1]["parts"]
        self.assertEqual([(part["type"], part.get("text")) for part in parts],
                         [("text", "完成")])

    def test_history_strips_scene_but_preserves_user_text_and_attachments(self):
        for sep in ea.SCENE_SEPS:
            content = [
                {"type": "text", "text": "## 当前上下文\n\n内部情景" + sep + "用户原话"},
                {"type": "image", "data": "AAAA", "mimeType": "image/png"},
            ]
            cleaned = self.ad._pi_user_content(content)
            self.assertEqual(cleaned[0]["text"], "用户原话")
            self.assertEqual(cleaned[1], content[1])
        self.assertEqual(self.ad._pi_user_content("普通消息"), "普通消息")

    def test_grouped_history_never_projects_scene_as_user_message(self):
        path = self.write_rows([
            {"type": "session", "id": "sess-scene", "cwd": "/tmp"},
            {"type": "message", "id": "u1", "message": {
                "role": "user", "timestamp": 1000,
                "content": [{"type": "text", "text":
                    "## 当前上下文\n\n内部情景" + ea.SCENE_SEP + "用户原话"}]}},
            {"type": "message", "id": "a1", "message": {
                "role": "assistant", "timestamp": 1001, "content": "回复"}},
        ])
        messages = self.ad._pi_group(path, "adp_x")
        text = json.dumps(messages, ensure_ascii=False)
        self.assertIn("用户原话", text)
        self.assertNotIn("内部情景", text)


class GateSync(unittest.TestCase):
    def test_memory_rule_stays_in_sync(self):
        source = (Path(ROOT) / "desktron" / "lib" / "engine_adapters" / "pi_gate.ts").read_text(
            encoding="utf-8")
        self.assertTrue(gate.check_js_sync(source))
        self.assertIn('mode === "bypassPermissions"', source)
        self.assertIn("protectedBlock", source)
        self.assertIn('"web_search", "web_fetch"', source)


if __name__ == "__main__":
    unittest.main()
