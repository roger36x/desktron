#!/usr/bin/env python3
"""旧桶 → band 模型的一次性迁移(会话 2 / 用户做)。对工作层根(memory_store._memory_root())操作。

映射(对齐冻结契约):
- area/<ws>/*  → local/<ws>/   (本工作区成品;tier=local)
- shared/*     → private/      (跨工作区单用户归 private;tier=private)
- journal/*    → 有 frontmatter 的成品判断 → private;无 frontmatter 的草稿 → temp/_legacy/(免闸原样搬)

frontmatter 重标到 curator 形:加 tier + kind=judgment,去 scope/layer;confidence 重算
(partial→documented 若有 source 否则 reported;旧 verified 缺正文 hash,降回 documented/reported
并提示重新 verify——verified 是对特定正文的签名,迁移不替用户重签)。

默认 dry-run 只打印计划;--apply 才搬运 + 重标 + 删旧。搬运 + 重标,不丢内容。
迁移后建议跑 mem_lint.py 复验。本环境无旧数据时为 no-op。"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mem_common import work_root, read_md, write_md, today


def _reframe(fm, body, tier):
    """旧 frontmatter → curator 形。返回 (new_fm, notes)。"""
    notes = []
    nid = fm.get("id") or fm.get("doc_id")
    conf = fm.get("confidence") or "reported"
    src = fm.get("source")
    if conf == "partial":
        conf = "documented" if src else "reported"
        notes.append("partial→" + conf)
    elif conf == "verified":
        conf = "documented" if src else "reported"
        notes.append("旧 verified 缺正文 hash,降回 " + conf + ",请重新 mem_verify")
    new = {
        "id": nid,
        "tier": tier,
        "kind": "judgment",
        "confidence": conf,
    }
    if src:
        new["source"] = src
    if conf == "documented" and fm.get("gaps"):
        new["gaps"] = fm["gaps"]
    if fm.get("type"):
        new["type"] = fm["type"]
    new["created"] = fm.get("created") or today()
    new["summary"] = fm.get("summary", "")
    if fm.get("links"):
        new["links"] = fm["links"]
    return new, notes


def _legacy_items(root):
    """列旧桶里的待迁项:(src_path, dst_tier, dst_subdir, kind)。"""
    items = []
    area = root / "area"
    if area.is_dir():
        for ws_dir in sorted(p for p in area.iterdir() if p.is_dir()):
            for p in sorted(ws_dir.glob("*.md")):
                if p.name != "MEMORY.md":
                    items.append((p, "local", ws_dir.name, "note"))
    shared = root / "shared"
    if shared.is_dir():
        for p in sorted(shared.glob("*.md")):
            if p.name != "MEMORY.md":
                items.append((p, "private", "", "note"))
    journal = root / "journal"
    if journal.is_dir():
        for p in sorted(journal.rglob("*.md")):
            if p.name == "MEMORY.md":
                continue
            fm, _ = read_md(p)
            if fm and (fm.get("id") or fm.get("doc_id")):
                items.append((p, "private", "", "note"))      # 成品判断
            else:
                items.append((p, "temp", "_legacy", "raw"))   # 草稿,免闸原样搬
    return items


def main():
    ap = argparse.ArgumentParser(description="旧桶 area/shared/journal → band 模型迁移")
    ap.add_argument("--apply", action="store_true", help="执行(默认只 dry-run 打印计划)")
    a = ap.parse_args()

    root = Path(work_root())
    items = _legacy_items(root)
    if not items:
        print("无旧桶数据(area/shared/journal),迁移 no-op。")
        return

    done = 0
    for src, tier, subdir, kind in items:
        dst_dir = root / tier / subdir if subdir else root / tier
        dst = dst_dir / src.name
        rel_src = src.relative_to(root)
        rel_dst = dst.relative_to(root)
        if kind == "raw":
            print(f"[temp] {rel_src} → {rel_dst}(草稿,原样搬)")
            if a.apply:
                dst_dir.mkdir(parents=True, exist_ok=True)
                src.replace(dst)
                done += 1
            continue
        fm, body = read_md(src)
        if fm is None:
            print(f"[跳过] {rel_src}:无 frontmatter,非 note(可手动归 temp)")
            continue
        new_fm, notes = _reframe(fm, body, tier)
        tag = ("  [" + "; ".join(notes) + "]") if notes else ""
        print(f"[{tier}] {rel_src} → {rel_dst}{tag}")
        if a.apply:
            write_md(dst, new_fm, body or "")
            src.unlink()
            done += 1

    if a.apply:
        print(f"\n已迁移 {done} 项。建议跑 mem_lint.py 复验;旧 MEMORY.md 索引由 curator reindex 重建。")
    else:
        print(f"\n[dry-run] 共 {len(items)} 项待迁。确认后加 --apply 执行。")


if __name__ == "__main__":
    main()
