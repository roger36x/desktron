"""commands 纪律层:tier 解析、预检、查重、add/update/lint 全链路(真 curator CLI)。"""
import os

import pytest

from memory_engine import commands, layout
from memory_engine.commands import EngineError
from curator import cli as curator_cli
from curator.note import read_note


def test_curator_target_alias(mem_root):
    assert commands.curator_target(mem_root, "incident", "sot") == ("sot", "incident")
    assert commands.curator_target(mem_root, "incident", "shared") == ("sot", "incident")
    assert commands.curator_target(mem_root, "incident", "private") == ("sealed", "")
    with pytest.raises(EngineError):
        commands.curator_target(mem_root, "incident", "nope")


def test_pinned_requires_explicit_user_confirmation_for_all_legacy_aliases(mem_root):
    for tier in ("pinned", "sot", "shared"):
        with pytest.raises(EngineError, match="用户明确确认"):
            commands.add(mem_root, "incident", tier, f"{tier}-one", "锁定约束", "正文")
    commands.add(mem_root, "incident", "pinned", "pinned-one", "锁定约束", "正文",
                 confirmed_by_user=True)
    assert os.path.exists(os.path.join(layout.workspace_layout(mem_root, "incident")["sot"],
                                       "pinned-one.md"))
    with pytest.raises(EngineError, match="用户明确确认"):
        commands.update(mem_root, "pinned-one", body="未确认的修改")
    commands.update(mem_root, "pinned-one", body="确认后的修改", confirmed_by_user=True)


def test_direct_curator_cannot_bypass_pinned_confirmation(mem_root):
    base = ["--root", mem_root]
    topic = ["--topic", "desk-iter"]
    assert curator_cli.main(base + ["add", "--band", "sot", "--kind", "judgment", *topic,
                                    "--id", "direct-one", "--summary", "锁定约束", "--body", "正文"]) == 2
    assert curator_cli.main(base + ["add", "--band", "sot", "--kind", "judgment", *topic,
                                    "--id", "direct-one", "--summary", "锁定约束", "--body", "正文",
                                    "--user-confirmed"]) == 0
    path = os.path.join(layout.workspace_layout(mem_root, "desk-iter")["sot"], "direct-one.md")
    assert read_note(path)[0]["user_confirmed"] is True
    fm, body = read_note(path)
    fm.pop("tier")  # 不能靠篡改 frontmatter 绕过实际 sot 路径的确认闸
    from curator.note import write_note
    write_note(path, fm, body)
    assert curator_cli.main(base + ["update", "--id", "direct-one", "--body", "未确认修改"]) == 2
    assert curator_cli.main(base + ["update", "--id", "direct-one", "--body", "确认修改",
                                    "--user-confirmed"]) == 0
    assert curator_cli.main(base + ["rm", "--id", "direct-one", "--yes"]) == 2
    assert curator_cli.main(base + ["rm", "--id", "direct-one", "--yes",
                                    "--user-confirmed"]) == 0

    # promote is also a final write path: moving a judgment into or out of
    # physical sot changes pinned state and therefore requires confirmation.
    assert curator_cli.main(base + ["add", "--band", "sealed", "--kind", "judgment",
                                    "--id", "move-one", "--summary", "待迁移", "--body", "正文"]) == 0
    promote = ["promote", "--id", "move-one", "--to-band", "sot", *topic, "--confirm"]
    assert curator_cli.main(base + promote) == 2
    assert curator_cli.main(base + promote + ["--user-confirmed"]) == 0
    assert curator_cli.main(base + ["promote", "--id", "move-one", "--to-band", "sealed"]) == 2
    assert curator_cli.main(base + ["promote", "--id", "move-one", "--to-band", "sealed",
                                    "--user-confirmed"]) == 0


def test_fact_tier_does_not_require_pinned_confirmation(mem_root):
    commands.add(mem_root, "incident", "fact", "fact-one", "术语定义", "事实正文",
                 kind="fact", doc_type="note", keywords="术语")
    assert os.path.exists(os.path.join(layout.workspace_layout(mem_root, "incident")["sot"],
                                       "fact-one.md"))
    commands.update(mem_root, "fact-one", body="更新后的事实")
    with pytest.raises(EngineError, match="必须配 --kind fact"):
        commands.add(mem_root, "incident", "fact", "bad-fact", "错误入口", "正文")


def test_add_precheck(mem_root):
    with pytest.raises(EngineError, match="id 非法"):
        commands.add(mem_root, "incident", "pinned", "Bad_ID", "s", "b", confirmed_by_user=True)
    with pytest.raises(EngineError, match="正文为空"):
        commands.add(mem_root, "incident", "pinned", "ok-id", "s", "  ", confirmed_by_user=True)
    with pytest.raises(EngineError, match="emoji"):
        commands.add(mem_root, "incident", "pinned", "ok-id", "s", "含表情🙂", confirmed_by_user=True)


def test_add_update_lint_roundtrip(tmp_path):
    root = str(tmp_path / ".memory")
    layout.ensure_instance(root)
    commands.add(root, "incident", "pinned", "m-one", "阈值判断", "结论正文",
                 source="https://example", confirmed_by_user=True)
    lay = layout.workspace_layout(root, "incident")
    assert os.path.exists(os.path.join(lay["sot"], "m-one.md"))
    commands.add(root, "incident", "sealed", "m-red", "红线判断", "封存正文")
    assert os.path.exists(os.path.join(lay["sealed"], "m-red.md"))
    # 旧名 tier 走别名落 sot，但 judgment 仍须显式确认
    commands.add(root, "risk", "shared", "m-legacy", "旧名写入", "x", force=True,
                 confirmed_by_user=True)
    assert os.path.exists(os.path.join(layout.workspace_layout(root, "risk")["sot"], "m-legacy.md"))
    # 查重:同 summary 不加 force 拒
    with pytest.raises(EngineError, match="疑似重复"):
        commands.add(root, "incident", "pinned", "m-dup", "阈值判断", "重复",
                     confirmed_by_user=True)
    commands.update(root, "m-one", body="更新后的正文", confirmed_by_user=True)
    # 补第二个独立来源 → 顶档 corroborated(2026-08-21 起 verify 退役,档位不由人签)
    commands.update(root, "m-one", source="https://a.example/doc,https://b.example/doc",
                    confirmed_by_user=True)
    fm = read_note(os.path.join(lay["sot"], "m-one.md"))[0]
    assert fm["confidence"] == "corroborated", fm
    with pytest.raises(EngineError, match="已退役"):
        commands.verify(root, "m-one", by="tester")
    commands.lint(root)   # 不抛即过(ERROR 会非零 → EngineError)
