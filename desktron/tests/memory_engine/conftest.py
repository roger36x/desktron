import os
import sys

import pytest

# 并仓后引擎在 Desk 的 lib 下;插首位,免得串到并仓前 pip 装的旧副本。
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "lib")))

ONTOLOGY = """name: memory-worklayer
instance:
  sub_kbs: []
  work_layer:
    journal: { dir: "{topic}/journal", gated: false }
    notes: { dir: "{topic}/notes", gated: false }
    sot:   { dir: "{topic}/sot" }
    sealed: { dir: "_sealed", containment: true }
doc_types: [note]
"""


@pytest.fixture
def mem_root(tmp_path):
    """已就位的 v3 实例根(带 layout 标记,免迁移扫描)。"""
    root = tmp_path / ".memory"
    (root / ".curator").mkdir(parents=True)
    (root / "ontology.yaml").write_text(ONTOLOGY, encoding="utf-8")
    (root / "rules.yaml").write_text("", encoding="utf-8")
    (root / ".curator" / "layout-v2").write_text("x\n", encoding="utf-8")
    (root / ".curator" / "layout-v3").write_text("x\n", encoding="utf-8")
    return str(root)


@pytest.fixture
def host(tmp_path, mem_root):
    from memory_engine.providers import Host
    return Host(repo_root=str(tmp_path), mem_root=mem_root)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for k in ("MEMORY_ENGINE_PROJECT_MEMORY", "MEMORY_ENGINE_CMD", "MEMORY_ENGINE_DREAM",
              "MEMORY_ENGINE_PI_MODELS", "DESKTRON_PI_MODELS",
              "DESKTRON_PROJECT_MEMORY", "DESKTRON_CMD", "DESKTRON_DREAM",
              "MEMORY_ENGINE_DISTILL", "MEMORY_ENGINE_DISTILL_CMD",
              "DESKTRON_DISTILL", "DESKTRON_DISTILL_CMD"):
        monkeypatch.delenv(k, raising=False)
