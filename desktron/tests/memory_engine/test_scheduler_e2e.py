"""scheduler 端到端：项目记忆状态、候选分析、SOT 提名与通知指纹去重。"""
import os
from datetime import date, timedelta

from memory_engine import journal, scheduler
from memory_engine.providers import Host

ONTOLOGY = """instance:
  sub_kbs: []
  work_layer:
    journal: { dir: "{topic}/journal", gated: false }
    notes: { dir: "{topic}/notes", gated: false }
    sot:   { dir: "{topic}/sot" }
    sealed: { dir: "_sealed", containment: true }
doc_types: [note]
gc:
  aging: "1d"
  evict: { stale: "10d", min_quality: 0.9, min_access: 1 }
"""


def _fixture(tmp_path):
    root = tmp_path / ".memory"
    (root / ".curator").mkdir(parents=True)
    (root / ".curator" / "layout-v2").write_text("x\n")
    (root / ".curator" / "layout-v3").write_text("x\n")
    (root / ".curator" / "layout-v4").write_text("x\n")
    (root / "ontology.yaml").write_text(ONTOLOGY, encoding="utf-8")
    (root / "rules.yaml").write_text("", encoding="utf-8")
    s0 = root / "incident" / "journal"
    s0.mkdir(parents=True)
    (s0 / "aged.md").write_text(
        "---\nid: aged-note\ntier: journal\nkind: judgment\nconfidence: documented\n"
        "summary: 老草稿\nsource: https://ex/a\ncreated: '2026-06-01'\n---\n正文\n", encoding="utf-8")
    (s0 / "ref.md").write_text(
        "---\nid: ref-note\ntier: journal\nkind: judgment\nconfidence: reported\n"
        "summary: 引用者\ncreated: '2026-07-06'\nlinks: [aged-note]\n---\n正文\n", encoding="utf-8")
    junk = s0 / "junk.md"
    junk.write_text("孤儿裸片段,无 frontmatter\n", encoding="utf-8")
    os.utime(junk, (0, 0))
    days = [(date.today() - timedelta(days=10 + i)).isoformat() for i in range(3)]
    jp = journal.path_of(str(root), "risk")
    os.makedirs(os.path.dirname(jp), exist_ok=True)
    open(jp, "w", encoding="utf-8").write(
        journal.TPL.format(ws="risk") + "".join(f"## d-{d}\n10:00 老流水\n\n" for d in days)
        + f"## d-{date.today().isoformat()}\n09:00 新流水(窗口内,不算)\n\n")
    return str(root)


def test_tick_report_and_dedup(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_ENGINE_PROJECT_MEMORY", "off")
    mem_root = _fixture(tmp_path)
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    total = scheduler.tick(host)
    rep = open(os.path.join(mem_root, ".curator", "dream", "latest.md"), encoding="utf-8").read()
    assert total >= 3
    assert "SOT 合入提名" in rep and "aged-note" in rep and "tenuring" in rep
    assert "gc --yes" not in rep.split("知识轴")[0]          # 记忆轴不再给执行指令
    assert "junk.md" in rep and "不可逆" in rep
    assert "## 项目记忆" in rep and "risk" in rep
    assert "自动采集(recap)" in rep and "直接写 journal" in rep
    assert "机械指针" not in rep and "本轮新指针" not in rep
    assert "旧流水" not in rep
    # 未自动执行
    assert os.path.exists(os.path.join(mem_root, "incident", "journal", "aged.md"))
    assert not os.path.exists(os.path.join(mem_root, "incident", "sot", "aged-note.md"))
    assert os.path.exists(os.path.join(mem_root, "incident", "journal", "junk.md"))
    # 通知与指纹去重
    jp = journal.path_of(mem_root, "_desktop")
    noti = open(jp, encoding="utf-8").read()
    assert "dreaming" in noti and "候选待审" in noti and "SOT 提名" in noti
    scheduler.tick(host)
    assert open(jp, encoding="utf-8").read().count("dreaming:") == noti.count("dreaming:")
    # 项目记忆输入变化由引擎自行消费，不产生人审通知，也不会自触发 _desktop 流水。
    journal.append(mem_root, "coding", "x")
    days = [(date.today() - timedelta(days=20 + i)).isoformat() for i in range(3)]
    open(journal.path_of(mem_root, "coding"), "w", encoding="utf-8").write(
        journal.TPL.format(ws="coding") + "".join(f"## d-{d}\nx\n\n" for d in days))
    scheduler.tick(host)
    assert open(jp, encoding="utf-8").read().count("dreaming:") == noti.count("dreaming:")
    # last-run/可观测
    assert os.path.exists(os.path.join(mem_root, ".curator", "dream", "last-run"))
    lines = "\n".join(scheduler.status_lines(host))
    assert "上次 tick" in lines and "最近失败:无" in lines.replace("：", ":")


def test_record_error_and_status(tmp_path, mem_root):
    host = Host(repo_root=str(tmp_path), mem_root=mem_root)
    try:
        raise RuntimeError("模拟竞态")
    except RuntimeError as e:
        scheduler.record_error(host, e)
    le = os.path.join(mem_root, ".curator", "dream", "last-error")
    body = open(le, encoding="utf-8").read()
    assert "模拟竞态" in body and "Traceback" in body
    assert any("模拟竞态" in x for x in scheduler.status_lines(host))
    # 成功 tick 清失败痕
    import memory_engine.scheduler as s
    s.tick(host)
    assert not os.path.exists(le)


def test_retired_capture_cursor_is_inert(tmp_path, mem_root, monkeypatch):
    """升级不删除历史游标,也不再读取或改写它。"""
    monkeypatch.setenv("MEMORY_ENGINE_PROJECT_MEMORY", "off")
    dream = os.path.join(mem_root, ".curator", "dream")
    os.makedirs(dream, exist_ok=True)
    cursor = os.path.join(dream, "capture-cursor.json")
    body = '{"claude:old": {"mtime": 1, "day": "2026-08-27"}}\n'
    open(cursor, "w", encoding="utf-8").write(body)
    scheduler.tick(Host(repo_root=str(tmp_path), mem_root=mem_root))
    assert open(cursor, encoding="utf-8").read() == body
