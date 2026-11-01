"""项目记忆共用表达规范；新中文结构兼容旧英文输入，不改写历史。"""
import os

import pytest
from curator.note import read_note, write_note
from memory_engine import project_memory as pm
from memory_engine.providers import Host


def model(path, ws="demo", legacy=False, capsule=None):
    heads = pm.LEGACY_HEADINGS if legacy else pm.HEADINGS
    body = "\n\n".join(f"## {h}\n\n第{i}段内容，保留已排除做法和原因。" for i, h in enumerate(heads)) + "\n"
    write_note(str(path), {"id": pm.model_id(ws), "type": "project", "tier": "notes",
        "kind": "judgment", "confidence": "reported", "role": "rollup",
        "summary": "测试项目",
        "created": "2020-01-01", "updated": "2020-01-01"}, body)
    if capsule is not None:
        fm, _ = read_note(str(path))
        fm["capsule"] = capsule
        write_note(str(path), fm, body)
    return body


def test_both_channels_receive_full_authoritative_style(tmp_path, mem_root):
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    item = {"staging": str(tmp_path / "new.md")}
    style = pm._style_text()
    for prompt in (pm.build_prompt(host, [("demo", item)]), pm.build_inline_prompt(host, "demo", item)):
        assert style in prompt
        assert "不沿用它的标题、句式或冗长段落" in prompt
        assert " → ".join(pm.HEADINGS) in prompt
        assert "不要输出英文分类标题" in prompt
        assert "覆盖扫描只用于调度，不写入正文" in prompt
        assert "不把多层条件用分号串成一段" in prompt
        assert str(tmp_path / "prompts/style.md") not in prompt
        assert "不生成 capsule 字段或独立摘要" in prompt
        assert "会话直接注入完整项目正文与检索 id" in prompt
        assert "存在则优先作为骨架" not in prompt


def test_style_and_contract_changes_invalidate_source_fingerprint(tmp_path, mem_root, monkeypatch):
    style = tmp_path / "style.md"
    style.write_text("句子完整，结论先行。", encoding="utf-8")
    monkeypatch.setattr(pm, "STYLE_FILE", str(style))
    first = pm.source_fingerprint(mem_root, "demo")
    style.write_text("句子完整，结论先行。事实与判断分开。", encoding="utf-8")
    second = pm.source_fingerprint(mem_root, "demo")
    assert first != second
    monkeypatch.setattr(pm, "FORMAT_VERSION", "future-contract")
    assert pm.source_fingerprint(mem_root, "demo") != second


def test_old_english_body_is_readable_but_only_staging_is_normalized(tmp_path):
    old, new = tmp_path / "old.md", tmp_path / "new.md"
    body = model(old, legacy=True)
    new.write_bytes(old.read_bytes())
    before = old.read_bytes()
    assert pm._validate(str(old), "demo") == (True, "")
    assert [h for h, _ in pm._section_bodies(body)] == list(pm.HEADINGS)
    pm._stamp_dates(str(new), str(old))
    _, rewritten = read_note(str(new))
    assert pm._validate(str(new), "demo") == (True, "")
    for old_head, new_head in zip(pm.LEGACY_HEADINGS, pm.HEADINGS):
        body = body.replace("## " + old_head, "## " + new_head)
    assert rewritten == body
    assert old.read_bytes() == before


def test_generated_model_needs_no_capsule(tmp_path):
    path = tmp_path / "new.md"
    model(path)
    assert "capsule" not in read_note(str(path))[0]
    assert pm._validate(str(path), "demo") == (True, "")


def test_missing_style_fails_before_engine_call_and_preserves_model(tmp_path, mem_root, monkeypatch):
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    path = pm.model_path(mem_root, "demo")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    model(path)
    before = open(path, "rb").read()
    monkeypatch.setattr(pm, "STYLE_FILE", str(tmp_path / "missing.md"))
    monkeypatch.setattr(pm, "run_engine", lambda *_: (_ for _ in ()).throw(AssertionError("must not launch")))
    result = pm.refresh(host, workspaces=["demo"], force=True)
    assert "本轮失败（input" in result
    assert open(path, "rb").read() == before
    assert pm.load_state(pm.state_path(mem_root))["last_error"]["reason"] == "input"


def test_legacy_model_injects_entire_body_not_capsule_without_rewriting(mem_root):
    path = pm.model_path(mem_root, "demo")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = "旧项目的稳定情况与边界。" * 60
    body = model(path, legacy=True, capsule=text)
    before = open(path, "rb").read()
    context = pm.project_context(mem_root, "demo")
    assert context.endswith(body.strip())
    assert text not in context
    assert "更新时间：2020-01-01" in context
    assert open(path, "rb").read() == before


def test_contract_change_queues_unchanged_model_for_regeneration(tmp_path, mem_root, monkeypatch):
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    path = pm.model_path(mem_root, "demo")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    model(path, legacy=True)
    before = open(path, "rb").read()
    with monkeypatch.context() as old:
        old.setattr(pm, "FORMAT_VERSION", "legacy")
        fingerprint = pm.source_fingerprint(mem_root, "demo")
    state = {"fingerprints": {"demo": fingerprint}}
    due = pm.candidates(host, state=state, workspaces=["demo"])
    assert [ws for ws, _ in due] == ["demo"]
    assert due[0][1]["fingerprint"] != fingerprint
    state["fingerprints"]["demo"] = due[0][1]["fingerprint"]
    assert pm.candidates(host, state=state, workspaces=["demo"]) == []
    assert open(path, "rb").read() == before


def test_multiline_legacy_capsule_is_ignored_without_rewriting(mem_root):
    path = pm.model_path(mem_root, "demo")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = ("当前项目有明确的使用边界。" * 9 + "\n" + "已定方案以正式来源为准。" * 9)
    body = model(path, capsule=text)
    before = open(path, "rb").read()
    assert pm._validate(path, "demo") == (True, "")
    assert pm.project_context(mem_root, "demo").endswith(body.strip())
    assert "当前项目有明确的使用边界" not in pm.project_context(mem_root, "demo")
    assert open(path, "rb").read() == before


def test_empty_style_is_an_explicit_input_error(tmp_path, mem_root, monkeypatch):
    style = tmp_path / "style.md"
    style.write_text(" \n", encoding="utf-8")
    monkeypatch.setattr(pm, "STYLE_FILE", str(style))
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    result = pm.refresh(host, workspaces=["demo"], force=True)
    assert "本轮失败（input" in result
    assert "为空" in pm.load_state(pm.state_path(mem_root))["last_error"]["detail"]


def test_missing_style_in_both_engine_paths_never_launches_model(tmp_path, mem_root, monkeypatch):
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    item = {"staging": str(tmp_path / "staging.md")}
    monkeypatch.setattr(pm, "STYLE_FILE", str(tmp_path / "missing.md"))
    monkeypatch.setattr(pm, "env_get", lambda *_: "custom")
    monkeypatch.setattr(pm, "_command", lambda: ["unused"])
    monkeypatch.setattr(pm.cli_path, "which", lambda _: "unused")
    def forbidden(*args, **kwargs):
        raise AssertionError("must not launch")
    monkeypatch.setattr(pm.subprocess, "run", forbidden)
    monkeypatch.setattr(pm, "_run_pi", forbidden)
    for engine in (lambda: pm.run_engine(host, [("demo", item)]),
                   lambda: pm._run_pi_engine(host, [("demo", item)], ["provider/model"])):
        item_path = item["staging"]
        open(item_path, "w", encoding="utf-8").write("old staging")
        ok, reason, _ = engine()
        assert (ok, reason) == (False, "input")
        assert not os.path.exists(item_path)
        assert not os.path.exists(os.path.join(pm._state_dir(mem_root), "running"))


@pytest.mark.parametrize("inline", [False, True])
def test_style_change_during_generation_remains_due(tmp_path, mem_root, monkeypatch, inline):
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    path = pm.model_path(mem_root, "demo")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    model(path)
    style = tmp_path / "style.md"
    style.write_text("规范 A：使用完整短句。", encoding="utf-8")
    monkeypatch.setattr(pm, "STYLE_FILE", str(style))
    item = pm.candidates(host, workspaces=["demo"])[0][1]
    prompt = (pm.build_inline_prompt(host, "demo", item) if inline
              else pm.build_prompt(host, [("demo", item)]))
    assert "规范 A" in prompt
    style.write_text("规范 B：删除固定开场。", encoding="utf-8")
    monkeypatch.setattr(pm, "_reindex", lambda *_: True)
    state = {}
    pm._record_success(host, state, [("demo", item)])
    assert [ws for ws, _ in pm.candidates(host, state=state, workspaces=["demo"])] == ["demo"]
    assert state["fingerprints"]["demo"] == pm.source_fingerprint(mem_root, "demo", style_contract=item["style_contract"])


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("change", ["journal", "pinned", "candidate"])
def test_material_arriving_during_generation_remains_due(tmp_path, mem_root, monkeypatch, inline, change):
    pending = []
    host = Host(repo_root=str(tmp_path), mem_root=mem_root, memory_candidates=lambda _ws: pending)
    path = pm.model_path(mem_root, "demo")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    model(path)
    item = pm.candidates(host, workspaces=["demo"])[0][1]
    if inline:
        pm.build_inline_prompt(host, "demo", item)
    else:
        pm.build_prompt(host, [("demo", item)])
    if change == "journal":
        journal = pm.journal.path_of(mem_root, "demo")
        os.makedirs(os.path.dirname(journal), exist_ok=True)
        with open(journal, "a", encoding="utf-8") as f:
            f.write("\n12:00 生成期间的新材料，尚未进入提示。\n")
    elif change == "pinned":
        pinned = os.path.join(pm.layout.workspace_layout(mem_root, "demo")["sot"], "late.md")
        os.makedirs(os.path.dirname(pinned), exist_ok=True)
        write_note(pinned, {"kind": "judgment", "user_confirmed": True}, "生成期间的新约束。")
    else:
        pending.append({"kind": "decision", "statement": "迟到的候选", "receipts": ["receipt-test"]})
    monkeypatch.setattr(pm, "_reindex", lambda *_: True)
    state = {}
    pm._record_success(host, state, [("demo", item)])
    assert [ws for ws, _ in pm.candidates(host, state=state, workspaces=["demo"])] == ["demo"]


def test_success_accounts_only_output_change_and_consumed_memos(tmp_path, mem_root, monkeypatch):
    memos = {"demo": [{"nid": "memo-test", "content": "待吸收的备忘"}]}
    host = Host(repo_root=str(tmp_path), mem_root=mem_root, memos=lambda: memos,
                memos_done=lambda ws, _items: memos.pop(ws))
    path = pm.model_path(mem_root, "demo")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    model(path)
    item = pm.candidates(host, workspaces=["demo"])[0][1]
    pm.build_inline_prompt(host, "demo", item)
    model(path)
    fm, body = read_note(path)
    write_note(path, fm, body.replace("第0段内容", "生成后更新的稳定背景"))
    monkeypatch.setattr(pm, "_reindex", lambda *_: True)
    state = {}
    pm._record_success(host, state, [("demo", item)])
    assert pm.candidates(host, state=state, workspaces=["demo"]) == []
    memos["demo"] = [{"nid": "new-memo", "content": "生成后新到的备忘"}]
    assert [ws for ws, _ in pm.candidates(host, state=state, workspaces=["demo"])] == ["demo"]


@pytest.mark.parametrize("change", ["style", "format"])
def test_changed_contract_retries_paused_workspace_and_resets_failure_budget(tmp_path, mem_root, monkeypatch, change):
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    path = pm.model_path(mem_root, "demo")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    model(path)
    before = open(path, "rb").read()
    style = tmp_path / "style.md"
    style.write_text("规范 A。", encoding="utf-8")
    monkeypatch.setattr(pm, "STYLE_FILE", str(style))
    due = pm.candidates(host, workspaces=["demo"])
    state = {}
    for _ in range(pm.FAIL_STOP):
        pm._mark_workspace_error(state, due, "invalid-output", "bad output")
    pm.save_state(pm.state_path(mem_root), state)
    calls = []
    def invalid(_host, items):
        calls.append(items)
        return False, "invalid-output", "bad output"
    monkeypatch.setattr(pm, "run_engine", invalid)
    assert "已暂停" in pm.refresh(host, workspaces=["demo"])
    assert calls == []
    if change == "style":
        style.write_text("规范 B。", encoding="utf-8")
    else:
        monkeypatch.setattr(pm, "FORMAT_VERSION", "next-format")
    assert "输出无效已丢弃" in pm.refresh(host, workspaces=["demo"])
    assert len(calls) == 1
    error = pm.load_state(pm.state_path(mem_root))["workspace_errors"]["demo"]
    assert error["fails"] == 1
    assert error["fingerprint"] == calls[0][0][1]["fingerprint"]
    assert error["fingerprint"] != due[0][1]["fingerprint"]
    assert open(path, "rb").read() == before
