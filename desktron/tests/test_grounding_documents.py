"""目录是唯一主题来源；运行路径、开发范围和四引擎注入保持一致。"""
from pathlib import Path

import pytest

import domains
from grounding import builder, documents


def write(root, name, body=""):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def installation(tmp_path, monkeypatch):
    product = tmp_path / "product"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write(product, "desk-docs/00-index.md", "# 使用手册\n\n- [安排](arrangements.md)\n")
    write(product, "desk-docs/arrangements.md", "不应注入的详细正文")
    monkeypatch.setattr(documents, "PRODUCT_ROOT", product)
    return product, workspace


def source_repo(root):
    write(root, "manifest.yaml")
    write(root, "desktron/lib/grounding/builder.py")
    write(root, "docs/00-index.md", "# 开发\n\n- [核心功能盘点](features.md)\n")
    write(root, "docs/features.md", "不应注入的开发正文")
    return root


def test_navigation_only_points_to_current_manual(installation):
    product, workspace = installation
    first = documents.navigation(workspace)
    assert first.startswith("### Desk 资料\n")
    assert str(product / "desk-docs/00-index.md") in first
    assert "详细正文" not in first
    assert "主题：" not in first
    write(product, "desk-docs/00-index.md", "- [新版安排](arrangements.md)\n")
    assert documents.navigation(workspace) == first


def test_document_reading_policy_is_scoped_and_reusable(installation):
    _, workspace = installation
    nav = documents.navigation(workspace)
    for clause in (
        "仅在回答需要文档依据的 Desk 操作、功能或配置问题时读取使用说明",
        "纯进度查询、已有结果核验不触发文档读取，但仍须核验实际状态",
        "相关章节已知则直接读取；入口未知才读取目录",
        "同一有效上下文中的已读内容可以复用",
        "内容缺失或已知文档版本变化时重新读取",
        "修改代码、排查故障或发布时，按需读取相关开发说明",
        "历史文件只用于了解过去，不作为当前操作依据",
    ):
        assert clause in nav
    assert "回答 Desk 的操作、功能或配置问题前，先读取使用说明" not in nav
    assert "再读取开发说明" not in nav


def test_catalog_contents_are_not_injected(installation):
    product, workspace = installation
    write(product, "desk-docs/history/old.md")
    write(product, "desk-docs/00-index.md", "\n".join([
        "- [安排](arrangements.md)", "- [旧方案](history/old.md)",
        "- [缺失文章](missing.md)", "- [外部网站](https://example.com/page.md)",
    ]))
    nav = documents.navigation(workspace)
    assert all(title not in nav for title in ("安排", "旧方案", "缺失文章", "外部网站"))


def test_developer_docs_require_associated_source_not_product(installation, tmp_path):
    product, workspace = installation
    source_repo(product)  # 安装来源恰好是开发 checkout，也不能让所有用户变成 Desk 开发者。
    ordinary = tmp_path / "unrelated-project"
    write(ordinary, "docs/00-index.md", "# 其他产品文档")
    assert str(ordinary / "docs/00-index.md") not in documents.navigation(workspace, [ordinary])
    source = source_repo(tmp_path / "renamed-checkout")
    nav = documents.navigation(workspace, [source, source])
    assert nav.count(str(source / "docs/00-index.md")) == 1
    assert str(source / "docs/features.md") not in nav
    assert "开发正文" not in nav


def test_direct_source_workspace_receives_development_index(installation, tmp_path):
    source = source_repo(tmp_path / "source")
    assert str(source / "docs/00-index.md") in documents.navigation(source)


def test_missing_manual_is_explicit(installation):
    product, workspace = installation
    (product / "desk-docs/00-index.md").unlink()
    nav = documents.navigation(workspace)
    assert "未找到使用说明" in nav
    assert str(product / "desk-docs/00-index.md") not in nav


@pytest.mark.parametrize("engine", ["claude", "codex", "pi", "shepherd:claude"])
@pytest.mark.parametrize("domain", ["", "_desktop", "work:sub"])
def test_navigation_in_constant_once_and_not_scene(installation, monkeypatch, engine, domain):
    _, workspace = installation
    monkeypatch.setattr(domains, "REPO_ROOT", str(workspace))
    monkeypatch.setattr(domains, "_station_segment", lambda *_: "")
    monkeypatch.setattr(domains, "_domain_entry", lambda *_: {})
    nav = builder.constant(str(workspace), domain, engine)
    assert nav.count("### Desk 资料") == 1
    assert "使用说明" in nav
    policy = documents.navigation(workspace).splitlines()[-1]
    assert nav.count(policy) == 1
    assert "纯进度查询、已有结果核验不触发文档读取" in nav
    assert "### Desk 资料" not in domains._grounding_index("work", "scene")


def test_bound_repo_and_external_workflow_paths(installation, monkeypatch, tmp_path):
    _, workspace = installation
    source = source_repo(tmp_path / "source")
    monkeypatch.setattr(domains, "REPO_ROOT", str(workspace))
    monkeypatch.setattr(domains, "_station_segment", lambda *_: "")
    for entry in ({"repos": [{"path": str(source)}]}, {"dir": str(source)}):
        monkeypatch.setattr(domains, "_domain_entry", lambda *_, entry=entry: entry)
        nav = domains._grounding_index("work", "nav")
        assert str(source / "docs/00-index.md") in nav
