#!/usr/bin/env python3
"""manifest.yaml 声明层校验器(desktron validate)。

强制 asset-model §3 的声明约束:
- 类型 enum、id 唯一与前缀规范、file 路径存在;
- 闸语义按 class 强制:executor 必须带完整 gate,observer/skill/plugin/agent/config/cache 禁止带 gate。

行为层(声明只读是否真只读)静态不可证,不在本校验器范围——由引擎权限白名单(纵深)
与服务端执行通道(终局)兜底。
"""
import sys
import os

import yaml

MATURITY = {"cache", "formal", "production"}
AUTONOMY = {"manual", "assisted", "approve", "scoped-auto", "full"}
GATE_KEYS = {"autonomy", "dry_run", "reversibility", "approval"}
SKILL_TYPES = {"tool", "knowledge"}

# section -> (id 前缀, class 值, 允许 gate)
SECTIONS = {
    "agents": ("agent.", "agent", False),
    "observers": ("obs.", "observer", False),
    "executors": ("exec.", "executor", True),
    "skills": ("skill.", "skill", False),
    "knowledge_cache": ("cache.", "cache", False),
    "workflows": ("wf.", "workflow", False),
    "plugins": ("plugin.", "plugin", False),
    "config": ("config.", "config", False),
}

errors, warns = [], []
err = lambda m: errors.append(m)
warn = lambda m: warns.append(m)


def check_asset(section, a, root, seen_ids):
    prefix, klass, gate_allowed = SECTIONS[section]
    aid = a.get("id", "")
    where = f"{section}/{aid or '<无 id>'}"

    if not aid:
        err(f"{where}: 缺 id")
    elif not aid.startswith(prefix):
        err(f"{where}: id 前缀应为 {prefix}*")
    if aid in seen_ids:
        err(f"{where}: id 重复")
    seen_ids.add(aid)

    if a.get("class") != klass:
        err(f"{where}: class 应为 {klass},实为 {a.get('class')}")

    if "file" in a:
        path = os.path.join(root, a["file"])
        if not os.path.exists(path):
            err(f"{where}: file 不存在: {a['file']}")
    if "dir" in a:
        # 缓存目录 gitignored,允许不存在,只提示
        if not os.path.isdir(os.path.join(root, a["dir"])):
            warn(f"{where}: dir 尚不存在(缓存目录,本机未拉则正常): {a['dir']}")

    if "maturity" in a and a["maturity"] not in MATURITY:
        err(f"{where}: maturity 非法: {a['maturity']}(允许 {sorted(MATURITY)})")

    # 闸语义:声明层强制
    if gate_allowed:
        gate = a.get("gate")
        if not isinstance(gate, dict):
            err(f"{where}: executor 必须声明 gate{{{', '.join(sorted(GATE_KEYS))}}}")
        else:
            missing = GATE_KEYS - set(gate)
            if missing:
                err(f"{where}: gate 缺字段: {sorted(missing)}")
            if "autonomy" in gate and gate["autonomy"] not in AUTONOMY:
                err(f"{where}: gate.autonomy 非法: {gate['autonomy']}(允许 {sorted(AUTONOMY)})")
    elif "gate" in a:
        err(f"{where}: {klass} 禁止携带闸语义(gate)——读写分离是类型级约束,见 asset-model §3")

    if section == "skills" and a.get("type") not in SKILL_TYPES:
        err(f"{where}: skills.type 应为 {sorted(SKILL_TYPES)},实为 {a.get('type')}")


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    mf = os.path.join(root, "manifest.yaml")
    if not os.path.isfile(mf):
        print(f"manifest 不存在: {mf}")
        return 2
    m = yaml.safe_load(open(mf, encoding="utf-8"))

    for key in ("version", "project", "engines"):
        if key not in m:
            err(f"顶层缺字段: {key}")
    # 引擎版本不再逐个钉死(装/升到最新,不符仅告警),故这里只校验 engines 是个映射。
    if not isinstance(m.get("engines"), dict):
        err("engines 必须是映射(可为空)")

    assets = m.get("assets") or {}
    unknown = set(assets) - set(SECTIONS)
    for u in sorted(unknown):
        err(f"未知资产节: assets.{u}(允许 {sorted(SECTIONS)})")

    seen_ids = set()
    for section in SECTIONS:
        for a in assets.get(section) or []:
            check_asset(section, a, root, seen_ids)

    for w in warns:
        print(f"  warn  {w}")
    for e in errors:
        print(f"  ERROR {e}")
    n_assets = sum(len(assets.get(s) or []) for s in SECTIONS)
    if errors:
        print(f"manifest 校验失败:{len(errors)} 错误 / {n_assets} 资产")
        return 1
    print(f"manifest 校验通过:{n_assets} 资产,{len(warns)} 提示")
    try:
        from prompt_governance import compile_registry
        registry = compile_registry(root)
    except (OSError, ValueError) as exc:
        print(f"Prompt registry 校验失败:{exc}")
        return 1
    print(f"Prompt registry 校验通过:{len(registry['targets'])} 执行方式,"
          f"{len(registry['segments'])} 注入段")
    return 0


if __name__ == "__main__":
    sys.exit(main())
