"""layout(契约/迁移)、journal(格式单一源)、gate(闸规则单一源)单测。"""
import os
from datetime import date, timedelta

from memory_engine import gate, journal, layout


def test_layout_contract(mem_root):
    lay = layout.workspace_layout(mem_root, "incident")
    assert lay["journal"].endswith(os.path.join("incident", "journal"))
    assert lay["notes"].endswith(os.path.join("incident", "notes"))
    assert lay["sot"].endswith(os.path.join("incident", "sot"))
    assert lay["sealed"].endswith("_sealed")
    # 旧名别名全兼容(s0 是 v4 前的层名)
    assert lay["s1"] == lay["notes"] and lay["shared"] == lay["sot"]
    assert lay["private"] == lay["sealed"] and lay["temp"] == lay["journal"]
    assert lay["s0"] == lay["journal"]


def test_migrate_v3_moves_archives(tmp_path):
    root = tmp_path / ".memory"
    (root / ".curator").mkdir(parents=True)
    (root / ".curator" / "layout-v2").write_text("x\n", encoding='utf-8')
    (root / "incident" / "s1").mkdir(parents=True)
    (root / "incident" / "shared").mkdir(parents=True)
    (root / "incident" / "s1" / "a.md").write_text("a", encoding='utf-8')
    (root / "incident" / "shared" / "b.md").write_text("b", encoding='utf-8')
    (root / "ontology.yaml").write_text("instance:\n  work_layer:\n    s0: {dir: \"{topic}/s0\"}\n", encoding='utf-8')
    layout.migrate(str(root))
    assert (root / "incident" / "notes" / "a.md").exists()
    assert (root / "incident" / "notes" / "b.md").exists()
    assert not (root / "incident" / "s1").exists()
    assert not (root / "incident" / "shared").exists()
    assert not (root / "incident" / "sot").exists()          # sot 从空开始,不预建
    assert "{topic}/notes" in (root / "ontology.yaml").read_text(encoding='utf-8')   # 旧 ontology 换种子
    assert (root / ".curator" / "layout-v3").exists()


def test_ensure_instance_seeds(tmp_path):
    root = str(tmp_path / "m")
    layout.ensure_instance(root)
    assert os.path.exists(os.path.join(root, "ontology.yaml"))
    assert os.path.exists(os.path.join(root, "rules.yaml"))
    assert "{topic}/notes" in open(os.path.join(root, "ontology.yaml"), encoding="utf-8").read()


def test_journal_append_and_key(mem_root):
    p = journal.append(mem_root, "risk", "第一条", key="关键点")
    text = open(p, encoding="utf-8").read()
    today = date.today().isoformat()
    assert f"## d-{today}" in text
    assert f"- {today} → #d-{today}" in text
    assert "- 关键点 → #d-" in text
    journal.append(mem_root, "risk", "第二条")
    text = open(p, encoding="utf-8").read()
    assert text.index("第二条") < text.index("第一条")   # 顶插当天最新条


def test_journal_compact_and_stale(mem_root):
    p = journal.path_of(mem_root, "risk")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    old = [(date.today() - timedelta(days=10 + i)).isoformat() for i in range(3)]
    body = "".join(f"## d-{d}\n10:00 老流水\n\n" for d in old)
    body += f"## d-{date.today().isoformat()}\n09:00 新流水\n\n"
    open(p, "w", encoding="utf-8").write(journal.TPL.format(ws="risk") + body)
    assert journal.stale_days(mem_root, "risk") == sorted(old)
    cands = journal.distill_candidates(mem_root)
    assert cands == {"risk": sorted(old)}
    n = journal.compact(mem_root, "risk", keep_days=7)
    assert n == 3
    text = open(p, encoding="utf-8").read()
    assert text.count(journal.STUB) == 3
    assert "新流水" in text                               # 窗口内保留全文
    assert journal.stale_days(mem_root, "risk") == []     # 折叠后不再是候选


def test_gate_rules():
    for p in ("/x/.memory/incident/sot/a.md", "/x/.memory/_sealed/b.md",
              "/x/.memory/incident/shared/c.md", "/x/.memory/local/private/d.md",
              "/y/memory/private/e.md"):
        assert gate.blocked(p), p
    for p in ("/x/.memory/incident/s0/journal.md", "/x/.memory/incident/notes/a.md",
              "/x/knowledge/common-kb/a.md"):
        assert not gate.blocked(p), p
    assert "sot" in gate.MEM_MSG
    # js 等价规则:把 js 转义还原后应与 py 正则一致
    assert gate.PATTERN_JS.replace("\\/", "/") == gate.PATTERN


def test_migrate_v4_renames_s0_to_journal(tmp_path):
    """s0 → journal:分代编号残留改成语义名,存量目录平移,旧名经 ALIAS 仍可用。"""
    root = tmp_path / ".memory"
    (root / ".curator").mkdir(parents=True)
    for m in ("layout-v2", "layout-v3"):
        (root / ".curator" / m).write_text("x\n", encoding='utf-8')
    (root / "incident" / "s0").mkdir(parents=True)
    (root / "incident" / "s0" / "journal.md").write_text("流水", encoding="utf-8")
    (root / "ontology.yaml").write_text(
        "instance:\n  work_layer:\n    s0: {dir: \"{topic}/s0\"}\n", encoding="utf-8")
    layout.migrate(str(root))
    assert (root / "incident" / "journal" / "journal.md").exists()
    assert not (root / "incident" / "s0").exists()
    assert (root / ".curator" / "layout-v4").exists()
