"""Prompt registry is strict, complete and observational until adapters opt in."""
import copy
import os
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "desktron" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import prompt_governance
from prompt_governance import (EXPECTED_TARGETS, PromptRegistryError, _strict_yaml,
                               compile_registry, delivery_plan, load_registry)
from prompt_sources import PromptSource


def test_registry_compiles_all_execution_modes_and_segments():
    compiled = compile_registry(ROOT)
    assert set(compiled["targets"]) == EXPECTED_TARGETS
    ids = {row["id"] for row in compiled["segments"]}
    assert len(ids) == len(compiled["segments"]) == 17
    assert 'desk.session-name' not in ids
    versions = {row["id"]: row["version"] for row in compiled["segments"]}
    changed = {
        "desk.style": 4, "desk.grounding": 7, "desk.scene": 4, 'desk.task-review': 3,
        "claude.memory-boundary": 4, "claude.tool-intent": 3, "claude.bash-cwd": 3,
        "codex.channel": 4, "codex.tool-intent": 3, "codex.skill-discovery": 4,
        "pi.runtime-rules": 4,
        "pi.native-default": 2, "shepherd.history": 3,
    }
    assert all(versions[name] == version for name, version in changed.items())
    assert all(version == 1 for segment, version in versions.items() if segment not in changed)
    assert {"desk.style", "desk.grounding", "desk.scene", "shepherd.history"} <= ids


def test_semantic_hash_ignores_yaml_comments_but_source_hash_does_not(tmp_path):
    import shutil
    shutil.copytree(ROOT / "prompts", tmp_path / "prompts")
    shutil.copytree(ROOT / "desktron/lib", tmp_path / "desktron/lib")
    original = compile_registry(tmp_path)
    manifest = tmp_path / "prompts/manifest.yaml"
    manifest.write_text("# formatting-only change\n" + manifest.read_text(encoding="utf-8"),
                        encoding="utf-8")
    changed = compile_registry(tmp_path)
    assert changed["manifest_sha256"] == original["manifest_sha256"]
    assert changed["manifest_source_sha256"] != original["manifest_source_sha256"]


def test_resolved_material_matches_authoritative_sources():
    compiled = compile_registry(ROOT)
    by_id = {row["id"]: row for row in compiled["segments"]}
    assert by_id["desk.style"]["material"]["content"] == \
        (ROOT / "prompts/style.md").read_text(encoding="utf-8").strip()
    assert "`description`" in by_id["claude.tool-intent"]["material"]["content"]
    assert "kn_add" not in by_id["claude.memory-boundary"]["material"]["content"]
    assert "`final_answer` 会结束本回合" in by_id["codex.channel"]["material"]["content"]
    assert by_id["desk.grounding"]["material"]["state"] == "runtime"
    assert by_id["pi.runtime-rules"]["material"]["resolver"] == "runtime_prompt_rules"
    assert by_id["shepherd.history"]["material"]["resolver"] == "history_prompt"
    assert len(by_id["shepherd.history"]["material"]["sha256"]) == 64
    assert by_id["claude.native-default"]["material"]["state"] == "native"


def test_capability_differences_expand_to_expected_targets():
    compiled = compile_registry(ROOT)
    targets = compiled["targets"]
    assert set(targets) == EXPECTED_TARGETS
    assert "system_append" in targets["claude-native"]["carriers"]
    assert "system_append" not in targets["claude-shepherd"]["carriers"]
    assert {"system_snapshot", "user_hidden_context"} <= \
        set(targets["claude-shepherd"]["carriers"])
    assert "application_context" in targets["codex-shepherd"]["carriers"]
    assert "application_context" not in targets["codex-native"]["carriers"]
    assert targets["pi-native"]["native_sources"]["repo_skills"] is False
    assert targets["pi-native"]["native_sources"]["context_files"] is False
    assert targets["pi-shepherd"]["native_sources"]["repo_skills"] is False
    assert targets["pi-shepherd"]["native_sources"]["context_files"] is False


def test_current_delivery_plan_excludes_planned_gaps():
    compiled = compile_registry(ROOT)
    current = delivery_plan(compiled, "claude-shepherd")
    all_rows = delivery_plan(compiled, "claude-shepherd", include_planned=True)
    current_ids = {row["segment"] for row in current["deliveries"]}
    all_ids = {row["segment"] for row in all_rows["deliveries"]}
    assert {"claude.tool-intent", "claude.memory-boundary"} <= current_ids
    assert all_ids == current_ids


def test_every_delivery_uses_supported_carrier_and_one_plan_per_target():
    manifest, capabilities = load_registry(ROOT)
    for segment in manifest["segments"].values():
        seen = set()
        for delivery in segment["deliveries"]:
            for target in delivery["targets"]:
                assert target not in seen
                seen.add(target)
                assert delivery["carrier"] in capabilities["targets"][target]["carriers"]
        assert seen == set(segment["applies_to"])


def test_strict_yaml_rejects_duplicate_anchor_and_ambiguous_boolean(tmp_path):
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text("version: 1\nversion: 2\n", encoding="utf-8")
    with pytest.raises(PromptRegistryError, match="duplicate YAML key"):
        _strict_yaml(duplicate)
    anchor = tmp_path / "anchor.yaml"
    anchor.write_text("a: &x [1]\nb: *x\n", encoding="utf-8")
    with pytest.raises(PromptRegistryError, match="anchors"):
        _strict_yaml(anchor)
    ambiguous = tmp_path / "ambiguous.yaml"
    ambiguous.write_text("enabled: on\n", encoding="utf-8")
    with pytest.raises(PromptRegistryError, match="ambiguous YAML boolean"):
        _strict_yaml(ambiguous)
    merge = tmp_path / "merge.yaml"
    merge.write_text("base: &base {enabled: true}\nvalue: {<<: *base}\n", encoding="utf-8")
    with pytest.raises(PromptRegistryError, match="anchors|aliases|merge"):
        _strict_yaml(merge)
    quoted = tmp_path / "quoted.yaml"
    quoted.write_text("enabled: 'on'\n", encoding="utf-8")
    assert _strict_yaml(quoted) == {"enabled": "on"}


def test_generated_material_hash_tracks_generator_source(tmp_path):
    _copy_registry_fixture(tmp_path)
    original = {row["id"]: row for row in compile_registry(tmp_path)["segments"]}
    builder = tmp_path / "desktron/lib/grounding/builder.py"
    builder.write_text(builder.read_text(encoding="utf-8") + "\n# behavior version marker\n",
                       encoding="utf-8")
    changed = {row["id"]: row for row in compile_registry(tmp_path)["segments"]}
    assert changed["desk.grounding"]["material"]["sha256"] != \
        original["desk.grounding"]["material"]["sha256"]
    assert changed["desk.style"]["material"]["sha256"] == \
        original["desk.style"]["material"]["sha256"]


def _copy_registry_fixture(tmp_path):
    import shutil
    shutil.copytree(ROOT / "prompts", tmp_path / "prompts")
    shutil.copytree(ROOT / "desktron/lib", tmp_path / "desktron/lib")


def test_source_registry_rejects_path_escape_bad_resolver_and_oversize(tmp_path, monkeypatch):
    _copy_registry_fixture(tmp_path)
    sources = dict(prompt_governance.SOURCES)
    sources["desk.style"] = PromptSource("file", path="../outside.md")
    monkeypatch.setattr(prompt_governance, "SOURCES", sources)
    with pytest.raises(PromptRegistryError, match="path escapes repository"):
        compile_registry(tmp_path)

    sources["desk.style"] = PromptSource("file", path="prompts/style.md")
    sources["desk.grounding"] = PromptSource(
        "generated", path="desktron/lib/grounding/builder.py", resolver="missing_resolver")
    with pytest.raises(PromptRegistryError, match="not found"):
        compile_registry(tmp_path)

    sources["desk.grounding"] = PromptSource(
        "generated", path="desktron/lib/grounding/builder.py", resolver="constant")
    (tmp_path / "prompts/style.md").write_text("x" * 3000, encoding="utf-8")
    with pytest.raises(PromptRegistryError, match="exceeds 2500"):
        compile_registry(tmp_path)


def test_capability_patch_rejects_unknown_or_conflicting_carrier(tmp_path):
    _copy_registry_fixture(tmp_path)
    path = tmp_path / "prompts/capabilities.yaml"
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    value["engines"]["claude"]["carriers"] = {"add": ["bogus"]}
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(PromptRegistryError, match="invalid add/remove"):
        load_registry(tmp_path)
    value["engines"]["claude"]["carriers"] = {"add": ["system_append"],
                                                "remove": ["system_append"]}
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(PromptRegistryError, match="invalid add/remove"):
        load_registry(tmp_path)


def test_validator_rejects_unsupported_carrier(tmp_path):
    _copy_registry_fixture(tmp_path)
    path = tmp_path / "prompts/manifest.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    segment = manifest["segments"]["pi.runtime-rules"]
    segment["delivery"]["default"]["carrier"] = "developer_instructions"
    path.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(PromptRegistryError, match="does not support"):
        load_registry(tmp_path)


def test_manifest_uses_stable_source_ids_and_compact_selectors():
    raw = yaml.safe_load((ROOT / "prompts/manifest.yaml").read_text(encoding="utf-8"))
    assert raw["version"] == 2
    for segment_id, segment in raw["segments"].items():
        assert segment["source"] == segment_id
        assert isinstance(segment["targets"], dict)
        assert "path" not in json.dumps(segment["source"], ensure_ascii=False)
        assert "applies_to" not in segment and "deliveries" not in segment


def test_capabilities_are_differences_not_expanded_rows():
    raw = yaml.safe_load((ROOT / "prompts/capabilities.yaml").read_text(encoding="utf-8"))
    assert raw["version"] == 2
    assert set(raw["engines"]) == {"claude", "codex", "pi"}
    assert set(raw["runtimes"]) == {"native", "shepherd"}
    assert set(raw["overrides"]) == {"claude-shepherd", "codex-shepherd"}
    assert not any(target in raw for target in EXPECTED_TARGETS)


def test_source_locations_exist_only_in_code_registry():
    manifest_text = (ROOT / "prompts/manifest.yaml").read_text(encoding="utf-8")
    capabilities_text = (ROOT / "prompts/capabilities.yaml").read_text(encoding="utf-8")
    assert "desktron/lib/" not in manifest_text + capabilities_text
    from prompt_sources import SOURCES
    assert set(SOURCES) == set(yaml.safe_load(manifest_text)["segments"])
    for source in SOURCES.values():
        if source.kind != "native":
            assert source.path.startswith(("prompts/", "desktron/lib/"))
            assert (ROOT / source.path).is_file()


def test_cli_output_is_metadata_only_by_default():
    command = ROOT / "desktron/bin/desk-prompt"
    if os.name != 'nt':
        assert command.stat().st_mode & 0o111
    result = subprocess.run([sys.executable, str(command), "--root", str(ROOT), "--target", "pi-shepherd"],
                            text=True, capture_output=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["target"] == "pi-shepherd"
    assert value["deliveries"]
    assert all("content" not in row for row in value["deliveries"])
    checked = subprocess.run([sys.executable, str(command), "--root", str(ROOT), "--check"],
                             text=True, capture_output=True, encoding='utf-8')
    assert checked.returncode == 0
    assert "6 targets, 17 segments" in checked.stdout


def test_repository_validate_includes_prompt_registry():
    result = subprocess.run([sys.executable, str(LIB / "validate_manifest.py"), str(ROOT)],
                            text=True, capture_output=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Prompt registry 校验通过:6 执行方式,17 注入段" in result.stdout


def test_prompt_registry_files_are_in_distribution_boundary():
    excludes = (ROOT / "scripts/payload-excludes.txt").read_text(encoding="utf-8").splitlines()
    for path in ("prompts", "schemas", "desktron/lib/prompt_governance.py",
                 "desktron/lib/prompt_sources.py", "desktron/bin/desk-prompt"):
        assert f"/{path}" not in excludes and path not in excludes
        assert (ROOT / path).exists()


def test_schema_json_has_no_duplicate_keys():
    def no_duplicates(pairs):
        out = {}
        for key, value in pairs:
            assert key not in out, key
            out[key] = value
        return out
    for name in ("prompt-manifest.schema.json", "prompt-capabilities.schema.json"):
        json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"),
                   object_pairs_hook=no_duplicates)


def test_phase_one_does_not_wire_runtime_imports():
    for relative in ("desktron/lib/engine_adapters", "desktron/lib/shepherd"):
        for path in (ROOT / relative).glob("*.*"):
            if path.name == "prompt_governance.py" or path.suffix not in {".py", ".mjs", ".ts"}:
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            assert "prompt_governance" not in source, path
            assert "prompt_sources" not in source, path
