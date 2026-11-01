"""Pi 模型目录：过期先答旧值、后台单飞刷新、跨重启复用、登录变化后强制重探。"""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktron" / "lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", str(ROOT / "frontend"), "7999", "7998", str(ROOT)]
try:
    import settings  # noqa: E402
finally:
    sys.argv = _argv

MODEL = {"id": "deepseek/deepseek-v4", "provider": "deepseek", "model": "deepseek-v4",
         "name": "DeepSeek", "reasoning": False, "thinking_levels": ["off"],
         "input": ["text"], "context_window": 100000}
FRESH = {"models": [MODEL], "current_model": MODEL["id"], "thinking": "medium", "unknown": False}


class PiCatalogCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "cache", "pi-model-catalog.json")
        patches = [
            mock.patch.object(settings, "_pi_catalog_path", return_value=self.path),
            mock.patch.object(settings, "_pi_env", return_value={"PATH": "fixture"}),
            mock.patch.dict(settings._PI_PROBE_CACHE, {"at": 0.0, "value": None}, clear=True),
            mock.patch.dict(settings._PI_CRED_CACHE, {"at": 0.0, "value": None}, clear=True),
            mock.patch.dict(settings._PI_BACKGROUND, {"probe": False, "credentials": False}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_stale_catalog_returns_immediately_and_refreshes_once_in_background(self):
        old = dict(FRESH, current_model="old/x")
        settings._PI_PROBE_CACHE.update(at=1.0, value=old)
        gate, calls = threading.Event(), []

        def slow_probe(*_args):
            calls.append(1)
            gate.wait(5)
            return FRESH

        with mock.patch.object(settings, "_pi_probe_once", side_effect=slow_probe):
            first = settings._pi_probe()
            second = settings._pi_probe()
            self.assertEqual(first["current_model"], "old/x")
            self.assertEqual(second["current_model"], "old/x")
            gate.set()
            for thread in threading.enumerate():
                if thread.name == "pi-probe-refresh":
                    thread.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(settings._PI_PROBE_CACHE["value"]["current_model"], MODEL["id"])
        self.assertTrue(os.path.exists(self.path))

    def test_saved_catalog_serves_first_read_after_restart(self):
        settings._pi_catalog_save(1.0, FRESH)
        with mock.patch.object(settings, "_pi_background") as background, \
                mock.patch.object(settings, "_pi_probe_once",
                                  side_effect=AssertionError("首次读取不应冷探测")):
            result = settings._pi_probe()
        self.assertEqual(result["models"][0]["id"], MODEL["id"])
        background.assert_called_once()
        import oscompat
        if oscompat.IS_WINDOWS:
            self.assertFalse(oscompat.loose_mode(os.stat(self.path)))
        else:
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_unknown_or_empty_catalog_is_never_saved(self):
        settings._pi_catalog_save(1.0, dict(FRESH, unknown=True))
        settings._pi_catalog_save(1.0, dict(FRESH, models=[]))
        self.assertFalse(os.path.exists(self.path))

    def test_auth_change_forgets_saved_catalog_and_probes_synchronously(self):
        settings._pi_catalog_save(1.0, FRESH)
        settings.invalidate_auth_probe_caches()
        self.assertFalse(os.path.exists(self.path))
        with mock.patch.object(settings, "_pi_probe_once", return_value=FRESH) as once:
            settings._pi_probe()
        once.assert_called_once()

    def test_stale_credentials_answer_immediately(self):
        settings._PI_CRED_CACHE.update(at=1.0, value={"logged_in": True, "unknown": False})
        with mock.patch.object(settings, "_pi_background") as background:
            state = settings._pi_credentials()
        self.assertTrue(state["logged_in"])
        background.assert_called_once()


class PiCodexVisibleCatalogTest(unittest.TestCase):
    setUp = PiCatalogCacheTest.setUp

    @staticmethod
    def model(provider, model):
        return dict(MODEL, id=f"{provider}/{model}", provider=provider, model=model,
                    name=model, thinking_levels=["off", "high"], reasoning=True)

    def test_intersection_preserves_pi_routes_capabilities_and_other_providers(self):
        rows = [self.model("openai", "gpt-codex"), self.model("openai", "gpt-api-only"),
                self.model("openai-codex", "gpt-codex"), self.model("deepseek", "gpt-api-only")]
        with mock.patch("codex_appserver.catalog", return_value={"models": [
                {"id": "catalog-id", "model": "gpt-codex"}, {"model": "codex-only"}]}) as catalog:
            result = settings._pi_filter_codex_models(rows)
        self.assertEqual(result, [rows[0], rows[2], rows[3]])
        self.assertIs(result[0], rows[0])
        self.assertEqual(len(rows), 4)
        catalog.assert_called_once_with(binary="codex")

    def test_id_fallback_and_hidden_models(self):
        rows = [self.model("openai", model) for model in ("visible", "hidden", "hidden2")]
        with mock.patch("codex_appserver.catalog", return_value={"models": [
                {"id": "visible"}, {"model": "hidden", "hidden": True},
                {"model": "hidden2", "isHidden": True}]}):
            self.assertEqual(settings._pi_filter_codex_models(rows), rows[:1])

    def test_model_id_is_exact_not_a_family_or_substring_match(self):
        rows = [self.model("openai", model) for model in
                ("gpt-codex", "gpt-codex-mini", "gpt-codex-2026", "gpt")]
        with mock.patch("codex_appserver.catalog", return_value={"models": [{"model": "gpt-codex"}]}):
            self.assertEqual(settings._pi_filter_codex_models(rows), rows[:1])

    def test_no_codex_probe_for_unrelated_providers(self):
        with mock.patch("codex_appserver.catalog", side_effect=AssertionError("not needed")):
            self.assertEqual(settings._pi_filter_codex_models([MODEL]), [MODEL])
            self.assertEqual(settings._pi_filter_codex_models([]), [])

    def test_empty_failed_or_malformed_codex_catalog_never_falls_back_to_full_pi_list(self):
        rows = [self.model("openai", "gpt-codex"), MODEL]
        for catalog in ({"models": []}, None, {"models": [None, {}, {"model": ""}]}):
            with self.subTest(catalog=catalog), \
                    mock.patch("codex_appserver.catalog", return_value=catalog), \
                    mock.patch.object(settings, "_pi_probe_log") as log:
                self.assertEqual(settings._pi_filter_codex_models(rows), [MODEL])
                log.assert_called_once()
        with mock.patch("codex_appserver.catalog", side_effect=OSError("unavailable")), \
                mock.patch.object(settings, "_pi_probe_log") as log:
            self.assertEqual(settings._pi_filter_codex_models(rows), [MODEL])
            log.assert_called_once()

    def test_filter_reapplies_to_old_pi_disk_cache_without_rewriting_it(self):
        rows = [self.model("openai", "removed"), self.model("openai", "visible"), MODEL]
        raw = dict(FRESH, models=rows, current_model="openai/removed")
        settings._pi_catalog_save(1.0, raw)
        before = Path(self.path).read_bytes()
        with mock.patch.object(settings, "_pi_background"), \
                mock.patch("codex_appserver.catalog", return_value={"models": [{"model": "visible"}]}):
            self.assertEqual(settings._pi_model_catalog(), rows[1:])
            self.assertEqual(settings._pi_current_model(), "openai/removed")
            self.assertEqual(settings._PI_PROBE_CACHE["value"]["models"], rows)
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_codex_removal_and_addition_apply_without_refreshing_raw_pi_cache(self):
        rows = [self.model("openai", "old"), self.model("openai", "new")]
        with mock.patch.object(settings, "_pi_probe", return_value=dict(FRESH, models=rows)), \
                mock.patch("codex_appserver.catalog", side_effect=[
                    {"models": [{"model": "old"}]}, {"models": [{"model": "new"}]}]):
            self.assertEqual(settings._pi_models(), ["openai/old"])
            self.assertEqual(settings._pi_models(), ["openai/new"])

    def test_cached_credentials_do_not_advertise_removed_models(self):
        settings._PI_CRED_CACHE.update(at=settings.time.time(), value={
            "logged_in": True, "providers": ["openai"],
            "models": ["openai/visible", "openai/removed", MODEL["id"]],
            "current_model": "openai/removed"})
        with mock.patch("codex_appserver.catalog", return_value={"models": [{"model": "visible"}]}):
            value = settings._pi_credentials()
        self.assertEqual(value["models"], ["openai/visible", MODEL["id"]])
        self.assertEqual(value["current_model"], "openai/removed")
        self.assertTrue(value["logged_in"])

    def test_fresh_credentials_use_visible_models_without_changing_authentication(self):
        rows = [self.model("openai", "visible"), self.model("openai", "removed")]
        with mock.patch.object(settings, "_pi_probe", return_value=dict(FRESH, models=rows)), \
                mock.patch.object(settings, "_pi_auth_ready", return_value=True), \
                mock.patch("codex_appserver.catalog", return_value={"models": [{"model": "visible"}]}):
            value = settings._pi_credentials_refresh(settings.time.time())
        self.assertEqual(value["models"], ["openai/visible"])
        self.assertEqual(value["providers"], ["deepseek", "openai"])
        self.assertTrue(value["logged_in"])

    def test_gui_options_use_filtered_catalog_and_preserve_saved_selection(self):
        from engine_adapters.pi import PiAdapter
        rows = [self.model("openai", "visible"), self.model("openai", "removed"), MODEL]
        adapter = PiAdapter(self.tmp.name, self.tmp.name, mock.Mock())
        with mock.patch.object(settings, "_pi_probe", return_value=dict(
                FRESH, models=rows, current_model="openai/removed")), \
                mock.patch("codex_appserver.catalog", return_value={"models": [{"model": "visible"}]}):
            result = adapter.options_catalog()
        self.assertEqual([row["id"] for row in result["models"]], ["openai/visible", MODEL["id"]])
        self.assertFalse(any(row["default"] for row in result["models"]))
        self.assertEqual(result["models"][0]["defaultEffort"], "high")
        self.assertEqual(adapter.normalize_options({"model": "openai/removed"})["model"], "openai/removed")


if __name__ == "__main__":
    unittest.main()
