"""统一记忆引擎：项目模型契约、指纹刷新与安全清理。"""
import json
import os
import sys

from curator.note import read_note, write_note
from memory_engine import journal, project_memory
from memory_engine.providers import Host


def _body():
    return "\n\n".join(f"## {heading}\n\n内容" for heading in project_memory.HEADINGS) + "\n"


def _write(path, ws):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_note(path, {
        "id": project_memory.model_id(ws), "type": "project", "tier": "notes",
        "kind": "judgment", "confidence": "reported", "role": "rollup",
        "summary": f"{ws} 项目记忆",
        "created": "2026-08-31", "updated": "2026-08-31",
    }, _body())


def test_prompt_is_one_current_model_not_note_fanout(tmp_path, mem_root):
    os.makedirs(os.path.dirname(project_memory.model_path(mem_root, "done")), exist_ok=True)
    _write(project_memory.model_path(mem_root, "done"), "done")
    os.makedirs(os.path.join(mem_root, "risk"), exist_ok=True)
    host = Host(repo_root=str(tmp_path), mem_root=mem_root,
                workspace_context=lambda _ws: {"description": "当前项目", "workspace_dir": "/work/ws",
                                               "repos": ["/work/repo"]})
    item = {"fingerprint": "x", "memos": [{"nid": "n1", "task": "任务", "content": "结论" * 30}],
            "staging": str(tmp_path / "staging.md")}
    prompt = project_memory.build_prompt(host, [("risk", item)])
    assert "Desk 记忆引擎" in prompt
    assert "只写每个工作区指定的 staging 文件" in prompt
    assert "不生成 capsule 字段或独立摘要" in prompt
    assert "临时版本/PID" in prompt
    assert "未提交文件" in prompt
    assert " → ".join(project_memory.HEADINGS) in prompt
    assert "越来越多的历史" in prompt
    assert "蒸馏管家" not in prompt
    assert "/work/ws" in prompt and "/work/repo" in prompt
    assert "user_confirmed: true 的 judgment 才是 pinned" in prompt
    assert "会话直接注入完整项目正文与检索 id" in prompt
    assert "旧未确认 judgment 是只读兼容输入" in prompt
    assert f"硬上限 {project_memory.BODY_CHAR_LIMIT:,} 字符" in prompt
    assert "严禁机械截断" in prompt
    assert "活动任务、负责人、下一步" in prompt
    assert "pinned_candidate 未经用户明确确认不得视为约束" in prompt
    assert "已有 rollup：done" in prompt
    assert "尚无 rollup：risk" in prompt
    assert "覆盖扫描只用于调度，不写入正文" in prompt
    assert project_memory._style_text() in prompt


def test_source_files_exclude_facts_and_classify_legacy_judgments(mem_root):
    lay = project_memory.layout.workspace_layout(mem_root, "mixed")
    os.makedirs(lay["sot"], exist_ok=True)
    for nid, kind, confirmed in (("pinned-one", "judgment", True),
                                  ("legacy-one", "judgment", None),
                                  ("fact-one", "fact", None)):
        fm = {"id": nid, "tier": "sot", "kind": kind, "summary": nid}
        if confirmed is not None:
            fm["user_confirmed"] = confirmed
        write_note(os.path.join(lay["sot"], nid + ".md"), fm, nid)
    open(os.path.join(lay["sot"], "plain-legacy.md"), "w", encoding='utf-8').write("无 frontmatter 的旧约束\n")
    confirmed, legacy = project_memory._sot_judgments(mem_root, "mixed")
    assert [os.path.basename(p) for p in confirmed] == ["pinned-one.md"]
    assert [os.path.basename(p) for p in legacy] == ["legacy-one.md", "plain-legacy.md"]
    sources = project_memory._source_files(mem_root, "mixed")
    assert not any(p.endswith("fact-one.md") for p in sources)


def test_work_event_candidates_enter_prompt_without_becoming_pinned(tmp_path, mem_root):
    candidate = {"kind": "pinned_candidate", "statement": "禁止自动重启",
                 "receipts": ["recap-1"], "confirmed": False}
    host = Host(repo_root=str(tmp_path), mem_root=mem_root,
                memory_candidates=lambda ws: [candidate] if ws == "risk" else [])
    due = project_memory.candidates(host, force=True, workspaces=["risk"])
    prompt = project_memory.build_prompt(host, due)
    assert "禁止自动重启" in prompt
    assert "receipt:recap-1" in prompt
    assert "confirmed:False" in prompt
    assert "pinned_candidate 未确认只能忽略" in prompt


def test_legacy_distill_switch_no_longer_disables_project_memory(monkeypatch):
    monkeypatch.delenv("MEMORY_ENGINE_PROJECT_MEMORY", raising=False)
    monkeypatch.delenv("DESKTRON_PROJECT_MEMORY", raising=False)
    monkeypatch.setenv("MEMORY_ENGINE_DISTILL", "off")
    assert project_memory.enabled() is True


def test_model_contract_requires_exact_four_sections_without_capsule(tmp_path):
    path = str(tmp_path / "model.md")
    _write(path, "risk")
    assert project_memory._validate(path, "risk") == (True, "")
    text = open(path, encoding="utf-8").read().replace("## 待确认问题", "## Extra")
    open(path, "w", encoding="utf-8").write(text)
    ok, why = project_memory._validate(path, "risk")
    assert ok is False and "四段标题" in why


def test_legacy_capsule_is_ignored_and_removed_only_from_new_output(tmp_path):
    old, staging = str(tmp_path / "old.md"), str(tmp_path / "staging.md")
    _write(old, "risk")
    fm, body = read_note(old)
    fm["capsule"] = "旧摘要" * 500
    write_note(old, fm, body)
    before = open(old, "rb").read()
    write_note(staging, fm, body)
    assert project_memory._validate(staging, "risk") == (True, "")
    project_memory._stamp_dates(staging, old)
    new_fm, new_body = read_note(staging)
    assert "capsule" not in new_fm
    assert new_body == body
    assert open(old, "rb").read() == before


def test_project_context_missing_model_does_not_use_other_memory(mem_root):
    journal.append(mem_root, "risk", "不能自动注入的流水")
    sealed = os.path.join(mem_root, "_sealed", "local.md")
    os.makedirs(os.path.dirname(sealed), exist_ok=True)
    write_note(sealed, {"id": "local"}, "不能自动注入的本机内容")
    _write(project_memory.model_path(mem_root, "other"), "other")
    assert project_memory.project_context(mem_root, "risk") == ""


def test_project_context_does_not_fall_back_to_legacy_capsule(mem_root):
    path = project_memory.model_path(mem_root, "risk")
    _write(path, "risk")
    fm, _ = read_note(path)
    fm["capsule"] = "仅剩旧摘要"
    write_note(path, fm, "")
    assert project_memory.project_context(mem_root, "risk") == ""


def test_overlong_body_is_rejected_without_mechanical_truncation(tmp_path):
    path = str(tmp_path / "model.md")
    _write(path, "risk")
    fm, _ = read_note(path)
    body = []
    for heading in project_memory.HEADINGS:
        content = ("重要内容。" * 600) if heading == "已定方案" else f"{heading} 内容"
        body.append(f"## {heading}\n\n{content}")
    original = "\n\n".join(body) + "\n"
    write_note(path, fm, original)

    ok, why = project_memory._validate(path, "risk")
    assert ok is False and "超过 2500 字符" in why
    _fm, unchanged = read_note(path)
    assert unchanged == original


def test_refresh_replaces_legacy_only_after_success(tmp_path, mem_root, monkeypatch):
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    lay = project_memory.layout.workspace_layout(mem_root, "risk")
    os.makedirs(lay["notes"], exist_ok=True)
    os.makedirs(lay["sot"], exist_ok=True)
    open(os.path.join(lay["notes"], "old.md"), "w", encoding='utf-8').write("旧判断\n")
    open(os.path.join(lay["sot"], "old-sot.md"), "w", encoding='utf-8').write("旧 SOT\n")
    journal.append(mem_root, "risk", "旧流水")

    def fake_run(h, due):
        for ws, item in due:
            _write(item["staging"], ws)
            target = project_memory.model_path(h.mem_root, ws)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(item["staging"], target)
        return True, "", ""

    monkeypatch.setattr(project_memory, "run_engine", fake_run)
    line = project_memory.refresh(host, workspaces=["risk"], force=True, reset_legacy=True)
    assert "已更新 1 个工作区" in line
    assert os.listdir(lay["notes"]) == [project_memory.MODEL_FILE]
    assert os.listdir(lay["sot"]) == ["old-sot.md"]
    assert open(os.path.join(lay["sot"], "old-sot.md"), encoding='utf-8').read() == "旧 SOT\n"
    assert "旧流水" not in open(journal.path_of(mem_root, "risk"), encoding="utf-8").read()
    state = json.load(open(project_memory.state_path(mem_root), encoding="utf-8"))
    assert state["fails"] == 0 and "risk" in state["fingerprints"]
    assert project_memory.candidates(host, state=state, workspaces=["risk"]) == []


def test_failure_preserves_legacy_and_stops_after_three(tmp_path, mem_root, monkeypatch):
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    lay = project_memory.layout.workspace_layout(mem_root, "risk")
    os.makedirs(lay["notes"], exist_ok=True)
    old = os.path.join(lay["notes"], "old.md")
    open(old, "w", encoding='utf-8').write("旧判断\n")
    monkeypatch.setattr(project_memory, "run_engine", lambda _h, _d: (False, "no-sentinel", "失败"))
    for expected in ("连续 1/3", "连续 2/3", "连续 3/3"):
        assert expected in project_memory.refresh(host, workspaces=["risk"], force=True, reset_legacy=True)
    assert os.path.exists(old)
    assert "已停" in project_memory.refresh(host, workspaces=["risk"])


def test_host_sleep_does_not_advance_fail_stop(tmp_path, mem_root, monkeypatch):
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    monkeypatch.setattr(project_memory, "run_engine",
                        lambda _h, _d: (False, "host-slept/timeout", "合盖"))
    project_memory.refresh(host, workspaces=["risk"], force=True)
    state = json.load(open(project_memory.state_path(mem_root), encoding="utf-8"))
    assert int(state.get("fails") or 0) == 0
    assert state["last_error"]["reason"] == "host-slept/timeout"


def test_invalid_utf8_staging_is_removed_and_reported_recoverable(tmp_path, mem_root, monkeypatch):
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    staging = str(tmp_path / "staging.md")
    script = r'''
import os, sys
path = os.environ["BAD_STAGING"]
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "wb") as f:
    f.write(b"---\nid: project-memory-risk\ntype: project\ntier: notes\nkind: judgment\nconfidence: reported\nrole: rollup\nsummary: x\ncapsule: ")
    f.write(b"\x98")
    f.write(b"\n---\n\n## Project context\n\ntext\n")
'''
    monkeypatch.setenv("BAD_STAGING", staging)
    monkeypatch.setattr(project_memory, "_command", lambda: [sys.executable, "-c", script])

    ok, reason, detail = project_memory.run_engine(host, [("risk", {"staging": staging})])

    assert ok is False
    assert reason == "invalid-output"
    assert "frontmatter 无效" in detail
    assert not os.path.exists(staging)


def test_invalid_output_does_not_trip_global_fail_stop(tmp_path, mem_root, monkeypatch):
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    monkeypatch.setattr(project_memory, "run_engine", lambda _h, _d: (False, "invalid-output", "bad utf8"))

    for _ in range(project_memory.FAIL_STOP + 1):
        line = project_memory.refresh(host, workspaces=["risk"], force=True)

    state = json.load(open(project_memory.state_path(mem_root), encoding="utf-8"))
    assert "输出无效已丢弃" in line
    assert int(state.get("fails") or 0) == 0
    assert state["workspace_errors"]["risk"]["fails"] == project_memory.FAIL_STOP + 1
    assert state["last_error"]["reason"] == "invalid-output"


def test_batch_invalid_output_is_isolated_to_bad_workspace(tmp_path, mem_root, monkeypatch):
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    calls = []

    def fake_run(h, due):
        calls.append([ws for ws, _ in due])
        if len(due) > 1:
            return False, "invalid-output", "batch bad"
        ws, item = due[0]
        if ws == "bad":
            return False, "invalid-output", "bad: frontmatter 无效"
        _write(item["staging"], ws)
        target = project_memory.model_path(h.mem_root, ws)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.replace(item["staging"], target)
        return True, "", ""

    monkeypatch.setattr(project_memory, "run_engine", fake_run)

    line = project_memory.refresh(host, workspaces=["good", "bad"], force=True)

    assert "已更新 1 个工作区（good）" in line
    assert "1 个工作区输出无效已跳过" in line
    assert os.path.exists(project_memory.model_path(mem_root, "good"))
    assert not os.path.exists(project_memory.model_path(mem_root, "bad"))
    state = json.load(open(project_memory.state_path(mem_root), encoding="utf-8"))
    assert int(state.get("fails") or 0) == 0
    assert "good" in state["fingerprints"]
    assert state["workspace_errors"]["bad"]["fails"] == 1
    assert calls == [["good", "bad"], ["good"], ["bad"]]


_FAKE_PI = r'''#!{python}
import os, sys
# The fixture implements the same UTF-8 JSON/text pipe contract as Pi.
sys.stdin.reconfigure(encoding="utf-8")
model = sys.argv[sys.argv.index("--model") + 1]
prompt = sys.stdin.read()
with open(os.environ["FAKE_PI_LOG"], "a", encoding="utf-8") as f:
    f.write(model + "\n")
behaviour = os.environ.get("FAKE_PI_" + model.replace("/", "_").replace("-", "_").replace(".", "_"), "ok")
if behaviour == "fail":
    sys.stderr.write("provider error 503\n")
    sys.exit(1)
import json
def emit(text):
    print(json.dumps({"type": "message_end", "message": {"role": "assistant",
        "content": [{"type": "text", "text": text}],
        "usage": {"input": 900, "output": 300, "cacheRead": 100, "cacheWrite": 0}}}))
    sys.exit(0)
assert "--mode" in sys.argv and sys.argv[sys.argv.index("--mode") + 1] == "json"
if behaviour == "bad":
    emit("这里没有 frontmatter")
assert "--no-tools" in sys.argv and "--no-context-files" in sys.argv
heads = ["Project context", "Decisions & rejections", "Constraints & principles", "Open questions"]
body = "\n\n".join(f"## {h}\n\n内容" for h in heads)
text = ("```markdown\n---\nid: project-memory-risk\ntype: project\ntier: notes\nkind: judgment\n"
        "confidence: reported\nrole: rollup\nsummary: risk\ncapsule: " + "当前项目摘要。" * 40 +
        "\ncreated: 2026-10-02\nupdated: 2026-10-02\n---\n\n" + body + "\n```")
emit(text)
'''


def _fake_pi(tmp_path, monkeypatch, **behaviour):
    script = tmp_path / "pi"
    script.write_text(_FAKE_PI.replace("{python}", sys.executable), encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "pi.log"
    monkeypatch.setenv("FAKE_PI_LOG", str(log))
    for key, value in behaviour.items():
        monkeypatch.setenv("FAKE_PI_" + key, value)
    monkeypatch.setattr(project_memory.cli_path, "which", lambda name: str(script) if name == "pi" else None)
    if os.name == 'nt':
        # Windows does not execute Python shebang fixtures. Keep real protocol
        # I/O, output validation and exit status, but select its native interpreter.
        run = project_memory.subprocess.run
        def native_run(argv, *args, **kwargs):
            if isinstance(argv, list) and argv[0] == str(script):
                argv = [sys.executable, *argv]
            return run(argv, *args, **kwargs)
        monkeypatch.setattr(project_memory.subprocess, 'run', native_run)
    return log


def test_inline_prompt_carries_materials_but_never_sealed(tmp_path, mem_root):
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    journal.append(mem_root, "risk", "本周决定改用便宜模型")
    sealed = os.path.join(mem_root, "_sealed")
    os.makedirs(sealed, exist_ok=True)
    open(os.path.join(sealed, "secret.md"), "w", encoding='utf-8').write("SEALED-SECRET")
    item = {"memos": [{"nid": "n1", "task": "任务", "content": "备忘结论"}], "staging": str(tmp_path / "s.md")}
    prompt = project_memory.build_inline_prompt(host, "risk", item)
    assert "本周决定改用便宜模型" in prompt and "备忘结论" in prompt
    assert "SEALED-SECRET" not in prompt
    assert "不提供文件读取或命令工具" in prompt
    assert "第一行是 ---" in prompt
    assert "staging" not in prompt


def test_read_material_rejects_sealed_path(mem_root):
    sealed = os.path.join(mem_root, "_sealed")
    os.makedirs(sealed, exist_ok=True)
    path = os.path.join(sealed, "x.md")
    open(path, "w", encoding='utf-8').write("x")
    try:
        project_memory._read_material(mem_root, path, 100)
    except ValueError:
        return
    raise AssertionError("sealed 材料必须被拒绝")


def test_pi_primary_success_uses_only_primary(tmp_path, mem_root, monkeypatch):
    log = _fake_pi(tmp_path, monkeypatch)
    monkeypatch.setenv("MEMORY_ENGINE_PI_MODELS", "openrouter/qwen/qwen3.8-27b,deepseek/deepseek-flash")
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    os.makedirs(os.path.join(mem_root, "risk"), exist_ok=True)
    line = project_memory.refresh(host, workspaces=["risk"], force=True)
    assert "已更新 1 个工作区" in line and "备用模型" not in line
    assert log.read_text(encoding='utf-8').split() == ["openrouter/qwen/qwen3.8-27b"]
    assert os.path.isfile(project_memory.model_path(mem_root, "risk"))


def test_pi_fallback_is_recorded_and_reported(tmp_path, mem_root, monkeypatch):
    log = _fake_pi(tmp_path, monkeypatch, openrouter_qwen_qwen3_8_27b="fail")
    monkeypatch.setenv("MEMORY_ENGINE_PI_MODELS", "openrouter/qwen/qwen3.8-27b,deepseek/deepseek-flash")
    recorded = []
    host = Host(repo_root=str(tmp_path), mem_root=mem_root, log=lambda *_a: None,
                record_usage=lambda *a: recorded.append(a))
    os.makedirs(os.path.join(mem_root, "risk"), exist_ok=True)
    line = project_memory.refresh(host, workspaces=["risk"], force=True)
    assert "已更新 1 个工作区" in line
    assert "主模型失败后改用备用模型（risk→deepseek/deepseek-flash）" in line
    assert log.read_text(encoding='utf-8').split() == ["openrouter/qwen/qwen3.8-27b", "deepseek/deepseek-flash"]
    entries = project_memory.fallbacks_since(mem_root, 0)
    assert entries[-1]["failed"][0]["reason"] == "exit"
    assert "provider error 503" in entries[-1]["failed"][0]["detail"]
    assert recorded == [("memory-rebuild", "deepseek/deepseek-flash",
                         {"input": 900, "output": 300, "cached": 100, "cache_write": 0}, "ok")]


def test_pi_all_invalid_is_recoverable_and_keeps_old_model(tmp_path, mem_root, monkeypatch):
    _fake_pi(tmp_path, monkeypatch, openrouter_qwen_qwen3_8_27b="bad", deepseek_deepseek_flash="bad")
    monkeypatch.setenv("MEMORY_ENGINE_PI_MODELS", "openrouter/qwen/qwen3.8-27b,deepseek/deepseek-flash")
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    staging = str(tmp_path / "staging.md")
    ok, reason, detail = project_memory.run_engine(host, [("risk", {"staging": staging})])
    assert ok is False and reason == "invalid-output"
    assert "openrouter/qwen/qwen3.8-27b" in detail and "deepseek/deepseek-flash" in detail
    assert not os.path.exists(staging)
    assert not os.path.exists(project_memory.model_path(mem_root, "risk"))


def test_channel_file_selects_pi_and_bad_file_fails_loudly(tmp_path, mem_root, monkeypatch):
    _fake_pi(tmp_path, monkeypatch)
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    path = os.path.join(project_memory._state_dir(mem_root), project_memory.CHANNEL_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pi_models": ["deepseek/deepseek-flash"]}, f)
    assert project_memory.pi_models(mem_root) == (["deepseek/deepseek-flash"], "")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{bad")
    ok, reason, detail = project_memory.run_engine(host, [("risk", {"staging": str(tmp_path / "s.md")})])
    assert ok is False and reason == "config" and "无法解析" in detail


def test_cmd_env_overrides_pi_channel(tmp_path, mem_root, monkeypatch):
    monkeypatch.setenv("MEMORY_ENGINE_PI_MODELS", "deepseek/deepseek-flash")
    monkeypatch.setenv("MEMORY_ENGINE_CMD", "true")
    called = []
    monkeypatch.setattr(project_memory, "_run_pi_engine", lambda *a: called.append(a))
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    project_memory.run_engine(host, [("risk", {"staging": str(tmp_path / "s.md")})])
    assert called == []


def test_clean_inline_output_restores_missing_frontmatter_close():
    raw = "前言\n---\nid: x\ncapsule: y\n\n## Project context\n\n内容"
    out = project_memory._clean_inline_output(raw)
    assert out.startswith("---\nid: x\ncapsule: y\n---\n\n## Project context")
    fenced = "```markdown\n---\nid: x\n---\n\n## Project context\n```"
    assert project_memory._clean_inline_output(fenced) == "---\nid: x\n---\n\n## Project context\n"


def test_inline_rebuild_sends_only_new_journal_after_success(tmp_path, mem_root, monkeypatch):
    _fake_pi(tmp_path, monkeypatch)
    monkeypatch.setenv("MEMORY_ENGINE_PI_MODELS", "deepseek/deepseek-flash")
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    journal.append(mem_root, "risk", "第一批流水-旧")
    assert "已更新 1 个工作区" in project_memory.refresh(host, workspaces=["risk"], force=True)
    journal.append(mem_root, "risk", "第二批流水-新")
    [(ws, item)] = project_memory.candidates(host, workspaces=["risk"])
    prompt = project_memory.build_inline_prompt(host, ws, item)
    assert "第二批流水-新" in prompt
    assert "第一批流水-旧" not in prompt
    assert "上次重建后新增的 journal" in prompt
    assert item["journal_incremental"] is True


def test_inline_rebuild_without_marks_sends_full_journal(tmp_path, mem_root, monkeypatch):
    _fake_pi(tmp_path, monkeypatch)
    monkeypatch.setenv("MEMORY_ENGINE_PI_MODELS", "deepseek/deepseek-flash")
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    journal.append(mem_root, "risk", "旧流水甲")
    project_memory.refresh(host, workspaces=["risk"], force=True)
    state = project_memory.load_state(project_memory.state_path(mem_root))
    state.pop("journal_marks")
    project_memory.save_state(project_memory.state_path(mem_root), state)
    journal.append(mem_root, "risk", "新流水乙")
    [(ws, item)] = project_memory.candidates(host, workspaces=["risk"])
    prompt = project_memory.build_inline_prompt(host, ws, item)
    assert "旧流水甲" in prompt and "新流水乙" in prompt
    assert item["journal_incremental"] is False
    assert "上次重建后新增的 journal" not in prompt


def test_reindex_seeds_missing_instance_files(tmp_path):
    root = tmp_path / "fresh-memory"
    (root / "ws-a" / "notes").mkdir(parents=True)
    assert project_memory._reindex(str(root)) is True
    assert (root / "ontology.yaml").is_file() and (root / "rules.yaml").is_file()
    custom = "# 用户改过的规则\n"
    (root / "rules.yaml").write_text(custom, encoding="utf-8")
    project_memory._reindex(str(root))
    assert (root / "rules.yaml").read_text(encoding="utf-8") == custom


def test_host_stamps_dates_instead_of_model(tmp_path, mem_root, monkeypatch):
    from datetime import date, timedelta
    _fake_pi(tmp_path, monkeypatch)
    monkeypatch.setenv("MEMORY_ENGINE_PI_MODELS", "deepseek/deepseek-flash")
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    target = project_memory.model_path(mem_root, "risk")
    _write(target, "risk")
    fm, body = read_note(target)
    created = (date.today() - timedelta(days=30)).isoformat()
    fm["created"] = created
    write_note(target, fm, body)
    journal.append(mem_root, "risk", "新流水")
    assert "已更新 1 个工作区" in project_memory.refresh(host, workspaces=["risk"], force=True)
    fm, _body = read_note(target)
    assert fm["updated"] == date.today().isoformat()
    assert fm["created"] == created


def test_host_stamp_rejects_future_creation_date(tmp_path):
    from datetime import date, timedelta
    target = str(tmp_path / "old.md")
    staging = str(tmp_path / "new.md")
    _write(target, "risk")
    _write(staging, "risk")
    fm, body = read_note(target)
    fm["created"] = (date.today() + timedelta(days=30)).isoformat()
    write_note(target, fm, body)
    project_memory._stamp_dates(staging, target)
    new, _ = read_note(staging)
    assert new["created"] == new["updated"] == date.today().isoformat()
