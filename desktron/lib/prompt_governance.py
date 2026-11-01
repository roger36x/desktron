"""Strict Prompt declarations compiled into eight concrete delivery targets.

The YAML files contain semantic policy and differences only.  Implementation
locations live in ``prompt_sources.py``.  Phase one is observational: no engine
adapter imports this module or consumes the compiled output yet.
"""
from __future__ import annotations

import argparse
import ast
import copy
import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from prompt_sources import SOURCES, PromptSource


MANIFEST = "prompts/manifest.yaml"
CAPABILITIES = "prompts/capabilities.yaml"
ENGINES = ("claude", "codex", "pi")
RUNTIMES = ("native", "shepherd")
EXPECTED_TARGETS = {f"{engine}-{runtime}" for engine in ENGINES for runtime in RUNTIMES}
TARGET_RE = re.compile(r"^[a-z][a-z0-9-]+$")
SEGMENT_RE = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
CARRIERS = {"system_append", "system_snapshot", "developer_instructions",
            "application_context", "user_hidden_context", "user_prefix", "native_context"}
CADENCES = {"binding", "turn", "conversation_start", "engine_entry", "on_change", "native"}
DELIVERY_STATUSES = {"current", "planned", "external"}
REFRESHES = {"repository", "binding", "turn", "conversation_start", "on_change", "native"}
LIFECYCLES = {"binding", "turn", "engine_entry", "on_change", "native"}
SOURCE_KINDS = {"file", "python_symbol", "generated", "native"}


class PromptRegistryError(ValueError):
    pass


class _StrictLoader(yaml.SafeLoader):
    pass


_StrictLoader.yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for first, resolvers in list(_StrictLoader.yaml_implicit_resolvers.items()):
    _StrictLoader.yaml_implicit_resolvers[first] = [
        item for item in resolvers if item[0] != "tag:yaml.org,2002:timestamp"]


def _mapping(loader, node, deep=False):
    out = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key == "<<":
            raise PromptRegistryError("YAML merge keys are not allowed")
        if key in out:
            raise PromptRegistryError(f"duplicate YAML key: {key}")
        out[key] = loader.construct_object(value_node, deep=deep)
    return out


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _strict_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        for event in yaml.parse(raw):
            if getattr(event, "anchor", None) or isinstance(event, yaml.events.AliasEvent):
                raise PromptRegistryError(f"{path}: YAML anchors and aliases are not allowed")
            if isinstance(event, yaml.events.ScalarEvent) and event.style is None \
                    and event.value.lower() in {"yes", "no", "on", "off"}:
                raise PromptRegistryError(
                    f"{path}: ambiguous YAML boolean {event.value!r}; use true/false or quote it")
        value = yaml.load(raw, Loader=_StrictLoader)
    except (yaml.YAMLError, OSError) as exc:
        raise PromptRegistryError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PromptRegistryError(f"{path}: root must be a mapping")
    _plain_json(value, str(path))
    return value


def _plain_json(value: Any, where: str) -> None:
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, (datetime.date, datetime.datetime)):
        raise PromptRegistryError(f"{where}: implicit dates are not allowed")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _plain_json(item, f"{where}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PromptRegistryError(f"{where}: mapping keys must be strings")
            _plain_json(item, f"{where}.{key}")
        return
    raise PromptRegistryError(f"{where}: unsupported YAML value {type(value).__name__}")


def _keys(value: dict[str, Any], *, required: set[str], allowed: set[str], where: str) -> None:
    missing, unknown = required - set(value), set(value) - allowed
    if missing:
        raise PromptRegistryError(f"{where}: missing fields {sorted(missing)}")
    if unknown:
        raise PromptRegistryError(f"{where}: unknown fields {sorted(unknown)}")


def _repo_path(root: Path, relative: str, where: str) -> Path:
    if not isinstance(relative, str) or not relative or os.path.isabs(relative):
        raise PromptRegistryError(f"{where}: path must be repository-relative")
    root = root.resolve(); path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise PromptRegistryError(f"{where}: path escapes repository")
    if not path.is_file():
        raise PromptRegistryError(f"{where}: file does not exist: {relative}")
    return path


def _semantic_sha(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _source_sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _merge_patch(base: dict[str, Any], patch: dict[str, Any], where: str) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise PromptRegistryError(f"{where}: expected a mapping")
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if key == "carriers":
            if not isinstance(value, dict):
                raise PromptRegistryError(f"{where}.carriers: expected add/remove mapping")
            _keys(value, required=set(), allowed={"add", "remove"}, where=where + ".carriers")
            current = list(out.get("carriers") or [])
            add, remove = value.get("add") or [], value.get("remove") or []
            if not isinstance(add, list) or not isinstance(remove, list) \
                    or any(item not in CARRIERS for item in add + remove) \
                    or len(add) != len(set(add)) or len(remove) != len(set(remove)) \
                    or set(add) & set(remove):
                raise PromptRegistryError(f"{where}.carriers: invalid add/remove values")
            for item in remove:
                current = [existing for existing in current if existing != item]
            for item in add:
                if item not in current:
                    current.append(item)
            out["carriers"] = current
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_patch(out[key], value, where + "." + key)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _validate_target(target_id: str, target: dict[str, Any]) -> None:
    where = f"capabilities.targets.{target_id}"
    _keys(target,
          required={"engine", "runtime", "carriers", "native_sources", "refresh", "compression"},
          allowed={"engine", "runtime", "carriers", "native_sources", "refresh", "compression", "notes"},
          where=where)
    if target_id != f"{target['engine']}-{target['runtime']}":
        raise PromptRegistryError(f"{where}: id must match engine-runtime")
    carriers = target["carriers"]
    if not isinstance(carriers, list) or not carriers or len(carriers) != len(set(carriers)) \
            or any(item not in CARRIERS for item in carriers):
        raise PromptRegistryError(f"{where}.carriers: invalid values")
    native = target["native_sources"]
    _keys(native, required={"default_prompt", "context_files", "repo_skills", "global_skills"},
          allowed={"default_prompt", "context_files", "repo_skills", "global_skills"},
          where=where + ".native_sources")
    if any(not isinstance(item, bool) for item in native.values()):
        raise PromptRegistryError(f"{where}.native_sources: values must be boolean")
    refresh = target["refresh"]
    _keys(refresh, required={"system", "turn", "engine_entry"},
          allowed={"system", "turn", "engine_entry"}, where=where + ".refresh")
    if refresh["system"] not in {"process", "binding", "turn"} \
            or not isinstance(refresh["turn"], bool) or not isinstance(refresh["engine_entry"], bool):
        raise PromptRegistryError(f"{where}.refresh: invalid values")
    compression = target["compression"]
    _keys(compression, required={"preserves_system", "preserves_turn_context", "requires_reinject"},
          allowed={"preserves_system", "preserves_turn_context", "requires_reinject"},
          where=where + ".compression")
    if any(not isinstance(item, bool) for item in compression.values()):
        raise PromptRegistryError(f"{where}.compression: values must be boolean")


def _expand_capabilities(value: dict[str, Any]) -> dict[str, Any]:
    _keys(value, required={"version", "kind", "defaults", "engines", "runtimes", "overrides"},
          allowed={"version", "kind", "defaults", "engines", "runtimes", "overrides"},
          where="capabilities")
    if value["version"] != 2 or value["kind"] != "desktron-prompt-capabilities":
        raise PromptRegistryError("capabilities: unsupported version or kind")
    if set(value["engines"]) != set(ENGINES) or set(value["runtimes"]) != set(RUNTIMES):
        raise PromptRegistryError("capabilities: engines/runtimes must describe the complete product matrix")
    unknown = set(value["overrides"]) - EXPECTED_TARGETS
    if unknown:
        raise PromptRegistryError(f"capabilities.overrides: unknown targets {sorted(unknown)}")
    targets = {}
    for engine in ENGINES:
        for runtime in RUNTIMES:
            target_id = f"{engine}-{runtime}"
            target = copy.deepcopy(value["defaults"])
            target = _merge_patch(target, value["engines"].get(engine) or {}, f"engines.{engine}")
            target = _merge_patch(target, value["runtimes"].get(runtime) or {}, f"runtimes.{runtime}")
            target = _merge_patch(target, value["overrides"].get(target_id) or {},
                                  f"overrides.{target_id}")
            target["engine"], target["runtime"] = engine, runtime
            _validate_target(target_id, target)
            targets[target_id] = target
    return {"version": 1, "kind": "desktron-prompt-capabilities-expanded", "targets": targets}


def _select_targets(selector: dict[str, Any], where: str) -> set[str]:
    if not isinstance(selector, dict):
        raise PromptRegistryError(f"{where}: expected a mapping")
    _keys(selector, required=set(), allowed={"engines", "runtimes", "exclude"}, where=where)
    engines = selector.get("engines", list(ENGINES))
    runtimes = selector.get("runtimes", list(RUNTIMES))
    exclude = selector.get("exclude", [])
    if not isinstance(engines, list) or not engines or any(item not in ENGINES for item in engines):
        raise PromptRegistryError(f"{where}.engines: invalid values")
    if not isinstance(runtimes, list) or not runtimes or any(item not in RUNTIMES for item in runtimes):
        raise PromptRegistryError(f"{where}.runtimes: invalid values")
    if not isinstance(exclude, list) or any(item not in EXPECTED_TARGETS for item in exclude):
        raise PromptRegistryError(f"{where}.exclude: invalid values")
    return {f"{engine}-{runtime}" for engine in engines for runtime in runtimes} - set(exclude)


def _delivery_override_targets(selector: str, applies: set[str], where: str) -> set[str]:
    if selector in EXPECTED_TARGETS:
        matches = {selector}
    elif selector in ENGINES:
        matches = {target for target in applies if target.startswith(selector + "-")}
    elif selector in RUNTIMES:
        matches = {target for target in applies if target.endswith("-" + selector)}
    else:
        raise PromptRegistryError(f"{where}: unknown delivery selector {selector!r}")
    matches &= applies
    if not matches:
        raise PromptRegistryError(f"{where}: selector does not match segment targets")
    return matches


def _delivery(value: dict[str, Any], where: str, *, partial: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PromptRegistryError(f"{where}: expected a mapping")
    if partial and not value:
        raise PromptRegistryError(f"{where}: empty delivery override")
    required = set() if partial else {"carrier", "cadence", "status"}
    _keys(value, required=required, allowed={"carrier", "cadence", "status", "notes"}, where=where)
    if "carrier" in value and value["carrier"] not in CARRIERS:
        raise PromptRegistryError(f"{where}.carrier: invalid value")
    if "cadence" in value and value["cadence"] not in CADENCES:
        raise PromptRegistryError(f"{where}.cadence: invalid value")
    if "status" in value and value["status"] not in DELIVERY_STATUSES:
        raise PromptRegistryError(f"{where}.status: invalid value")
    return copy.deepcopy(value)


def _expand_manifest(value: dict[str, Any], capabilities: dict[str, Any]) -> dict[str, Any]:
    _keys(value, required={"version", "kind", "capabilities", "segments"},
          allowed={"version", "kind", "capabilities", "segments"}, where="manifest")
    if value["version"] != 2 or value["kind"] != "desktron-prompt-manifest" \
            or value["capabilities"] != "prompt.capabilities":
        raise PromptRegistryError("manifest: unsupported version, kind or capabilities id")
    segments = value["segments"]
    if not isinstance(segments, dict) or not segments:
        raise PromptRegistryError("manifest.segments: expected a non-empty mapping")
    source_ids = set()
    expanded = {}
    for segment_id, raw in segments.items():
        where = f"manifest.segments.{segment_id}"
        if not SEGMENT_RE.fullmatch(str(segment_id)) or not isinstance(raw, dict):
            raise PromptRegistryError(f"{where}: invalid segment")
        _keys(raw,
              required={"version", "refresh", "title", "status", "owner", "source", "lifecycle",
                        "priority", "max_chars", "sensitivity", "history", "targets", "delivery"},
              allowed={"version", "refresh", "title", "status", "owner", "source", "lifecycle",
                       "priority", "max_chars", "sensitivity", "history", "targets", "delivery", "notes"},
              where=where)
        if not isinstance(raw["version"], int) or raw["version"] < 1:
            raise PromptRegistryError(f"{where}.version: expected a positive integer")
        if raw["refresh"] not in REFRESHES or raw["lifecycle"] not in LIFECYCLES \
                or raw["status"] not in {"active", "review", "external"} \
                or raw["sensitivity"] not in {"public", "internal", "sensitive"} \
                or raw["history"] not in {"exclude", "private", "public-sanitized", "native"}:
            raise PromptRegistryError(f"{where}: invalid enum value")
        if not isinstance(raw["title"], str) or not raw["title"] \
                or not isinstance(raw["owner"], str) or not raw["owner"]:
            raise PromptRegistryError(f"{where}: title and owner are required strings")
        if not isinstance(raw["source"], str) or raw["source"] not in SOURCES:
            raise PromptRegistryError(f"{where}.source: unknown stable source id")
        if raw["source"] in source_ids:
            raise PromptRegistryError(f"{where}.source: source id is already used")
        source_ids.add(raw["source"])
        if not isinstance(raw["priority"], int) or not 0 <= raw["priority"] <= 1000 \
                or not isinstance(raw["max_chars"], int) or not 0 < raw["max_chars"] <= 200000:
            raise PromptRegistryError(f"{where}: invalid priority or max_chars")
        applies = _select_targets(raw["targets"], where + ".targets")
        delivery = raw["delivery"]
        _keys(delivery, required={"default"}, allowed={"default", "overrides"},
              where=where + ".delivery")
        default = _delivery(delivery["default"], where + ".delivery.default")
        plans = {target: copy.deepcopy(default) for target in applies}
        claimed = set()
        overrides = delivery.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise PromptRegistryError(f"{where}.delivery.overrides: expected mapping")
        # Deterministic precedence: engine, then runtime, then exact target.
        ordered = [key for key in ENGINES if key in overrides]
        ordered += [key for key in RUNTIMES if key in overrides]
        ordered += [key for key in EXPECTED_TARGETS if key in overrides]
        if set(ordered) != set(overrides):
            raise PromptRegistryError(f"{where}.delivery.overrides: unknown selector")
        for selector in ordered:
            patch = _delivery(overrides[selector], where + ".delivery.overrides." + selector, partial=True)
            matches = _delivery_override_targets(selector, applies,
                                                  where + ".delivery.overrides." + selector)
            claimed.update(matches)
            for target in matches:
                plans[target].update(copy.deepcopy(patch))
        deliveries = []
        for target in sorted(applies):
            plan = plans[target]
            if plan["carrier"] not in capabilities["targets"][target]["carriers"]:
                raise PromptRegistryError(
                    f"{where}.delivery: {target} does not support {plan['carrier']}")
            deliveries.append({"targets": [target], **plan})
        expanded[segment_id] = {**copy.deepcopy(raw), "applies_to": sorted(applies),
                                "deliveries": deliveries}
    unused = set(SOURCES) - source_ids
    if unused:
        raise PromptRegistryError(f"prompt source registry has undeclared ids: {sorted(unused)}")
    return {"version": 1, "kind": "desktron-prompt-manifest-expanded", "segments": expanded}


def _safe_expr(node: ast.AST, env: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
        return node.value
    if isinstance(node, ast.Name) and node.id in env:
        return env[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _safe_expr(node.left, env) + _safe_expr(node.right, env)
    if isinstance(node, (ast.List, ast.Tuple)):
        values = [_safe_expr(item, env) for item in node.elts]
        return values if isinstance(node, ast.List) else tuple(values)
    if isinstance(node, ast.Dict):
        return {_safe_expr(key, env): _safe_expr(value, env)
                for key, value in zip(node.keys, node.values)}
    raise PromptRegistryError("Python Prompt constants must be literal values or concatenations")


def _symbol_value(path: Path, symbol: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    scope: list[ast.stmt] = tree.body; env: dict[str, Any] = {}
    parts = symbol.split(".")
    for index, part in enumerate(parts):
        found = None
        for node in scope:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        try:
                            env[target.id] = _safe_expr(node.value, env)
                        except PromptRegistryError:
                            pass
                        if target.id == part:
                            found = env.get(part)
            if isinstance(node, ast.ClassDef) and node.name == part:
                found = node; break
        if found is None:
            raise PromptRegistryError(f"{path}: Python symbol not found: {symbol}")
        if isinstance(found, ast.ClassDef):
            if index == len(parts) - 1:
                raise PromptRegistryError(f"{path}: symbol is a class, not text: {symbol}")
            scope, env = found.body, {}
        elif index != len(parts) - 1:
            raise PromptRegistryError(f"{path}: symbol path is not a class: {symbol}")
        elif isinstance(found, str):
            return found
        else:
            raise PromptRegistryError(f"{path}: symbol is not text: {symbol}")
    raise PromptRegistryError(f"{path}: Python symbol not found: {symbol}")


def _generated_symbol(path: Path, resolver: str, where: str) -> None:
    expected = resolver.rsplit(".", 1)[-1]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    definitions = {node.name for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if expected not in definitions:
        raise PromptRegistryError(f"{where}: function {expected!r} not found in {path}")


def _materialize(root: Path, segment_id: str, segment: dict[str, Any]) -> dict[str, Any]:
    source: PromptSource = SOURCES[segment["source"]]
    if source.kind not in SOURCE_KINDS:
        raise PromptRegistryError(f"{segment_id}: unsupported source kind {source.kind}")
    material = {"kind": source.kind, "state": "runtime" if source.kind == "generated" else source.kind}
    if source.kind == "file":
        path = _repo_path(root, source.path, segment_id + ".source")
        text = path.read_text(encoding="utf-8").strip()
        material.update(state="resolved", content=text, chars=len(text), sha256=_source_sha(text.encode()))
    elif source.kind == "python_symbol":
        path = _repo_path(root, source.path, segment_id + ".source")
        text = _symbol_value(path, source.symbol).strip()
        material.update(state="resolved", content=text, chars=len(text), sha256=_source_sha(text.encode()))
    elif source.kind == "generated":
        path = _repo_path(root, source.path, segment_id + ".source")
        _generated_symbol(path, source.resolver, segment_id + ".resolver")
        declaration = json.dumps(source.__dict__, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode()
        material.update(resolver=source.resolver,
                        sha256=_source_sha(path.read_bytes() + b"\0" + declaration))
    else:
        material.update(state="native", provider=source.provider,
                        sha256=_source_sha(source.description.encode()))
    if material.get("chars", 0) > segment["max_chars"]:
        raise PromptRegistryError(
            f"{segment_id}: resolved source has {material['chars']} chars, exceeds {segment['max_chars']}")
    return material


def load_registry(root: str | os.PathLike[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    root_path = Path(root).resolve()
    raw_capabilities = _strict_yaml(root_path / CAPABILITIES)
    capabilities = _expand_capabilities(raw_capabilities)
    raw_manifest = _strict_yaml(root_path / MANIFEST)
    manifest = _expand_manifest(raw_manifest, capabilities)
    return manifest, capabilities


def compile_registry(root: str | os.PathLike[str]) -> dict[str, Any]:
    root_path = Path(root).resolve()
    raw_manifest = _strict_yaml(root_path / MANIFEST)
    raw_capabilities = _strict_yaml(root_path / CAPABILITIES)
    capabilities = _expand_capabilities(raw_capabilities)
    manifest = _expand_manifest(raw_manifest, capabilities)
    compiled = []
    for segment_id, segment in manifest["segments"].items():
        row = {"id": segment_id, **copy.deepcopy(segment)}
        row["material"] = _materialize(root_path, segment_id, segment)
        compiled.append(row)
    compiled.sort(key=lambda row: (-row["priority"], row["id"]))
    return {
        "version": 2, "kind": "desktron-prompt-registry", "manifest": MANIFEST,
        "manifest_sha256": _semantic_sha(raw_manifest),
        "manifest_source_sha256": _source_sha((root_path / MANIFEST).read_bytes()),
        "capabilities_sha256": _semantic_sha(raw_capabilities),
        "capabilities_source_sha256": _source_sha((root_path / CAPABILITIES).read_bytes()),
        "targets": copy.deepcopy(capabilities["targets"]), "segments": compiled,
    }


def delivery_plan(compiled: dict[str, Any], target: str, *, include_planned: bool = False) -> dict[str, Any]:
    if target not in (compiled.get("targets") or {}):
        raise PromptRegistryError(f"unknown Prompt target: {target}")
    rows = []
    for segment in compiled.get("segments") or []:
        for delivery in segment.get("deliveries") or []:
            if target not in delivery["targets"] or delivery["status"] == "planned" and not include_planned:
                continue
            material = segment["material"]
            rows.append({"segment": segment["id"], "version": segment["version"],
                "status": delivery["status"], "refresh": segment["refresh"],
                "carrier": delivery["carrier"], "cadence": delivery["cadence"],
                "priority": segment["priority"], "chars": material.get("chars"),
                "sha256": material.get("sha256"), "material_state": material["state"]})
    rows.sort(key=lambda row: (-row["priority"], row["segment"], row["carrier"]))
    return {"version": 2, "kind": "desktron-prompt-delivery-plan",
            "target": target, "deliveries": rows}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and inspect the Desk Prompt registry")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--target", choices=sorted(EXPECTED_TARGETS))
    parser.add_argument("--include-planned", action="store_true")
    parser.add_argument("--full", action="store_true",
                        help="include compiled source material; local inspection only")
    parser.add_argument("--check", action="store_true",
                        help="validate only and print one summary line")
    args = parser.parse_args(argv)
    try:
        compiled = compile_registry(args.root)
        if args.check:
            print(f"Prompt registry valid: {len(compiled['targets'])} targets, "
                  f"{len(compiled['segments'])} segments")
            return 0
        if args.target:
            output = delivery_plan(compiled, args.target, include_planned=args.include_planned)
        elif args.full:
            output = compiled
        else:
            output = {"version": compiled["version"], "kind": compiled["kind"],
                "manifest": compiled["manifest"], "manifest_sha256": compiled["manifest_sha256"],
                "manifest_source_sha256": compiled["manifest_source_sha256"],
                "capabilities_sha256": compiled["capabilities_sha256"],
                "capabilities_source_sha256": compiled["capabilities_source_sha256"],
                "targets": sorted(compiled["targets"]),
                "segments": [{"id": row["id"], "version": row["version"],
                    "status": row["status"], "refresh": row["refresh"],
                    "lifecycle": row["lifecycle"], "priority": row["priority"],
                    "material_state": row["material"]["state"],
                    "chars": row["material"].get("chars"),
                    "sha256": row["material"].get("sha256")}
                    for row in compiled["segments"]]}
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, PromptRegistryError) as exc:
        print(f"Prompt registry invalid: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
