"""只提供现行 Desk 文档的入口，不注入正文。"""
from pathlib import Path


PRODUCT_ROOT = Path(__file__).resolve().parents[3]


def navigation(workspace, repositories=(), locale="zh"):
    en = locale == "en"
    lines = ["### Desk documentation" if en else "### Desk 资料"]
    # 英文会话指向 desk-docs/en/;英文手册缺失时回落中文手册。
    rels = ("desk-docs/en/00-index.md", "desk-docs/00-index.md") if en else ("desk-docs/00-index.md",)
    manual = next((root / rel for rel in rels
                   for root in (Path(workspace), PRODUCT_ROOT)
                   if (root / rel).is_file()), None)
    if manual:
        lines.append(f"- User guide: `{manual.resolve()}`." if en
                     else f"- 使用说明：`{manual.resolve()}`。")
    else:
        lines.append("- User guide not found; check the installation first, and do not conclude "
                     "from this that a feature is unsupported." if en
                     else "- 未找到使用说明；请先检查安装内容，不要据此判断功能不支持。")

    seen = set()
    for root in (workspace, *repositories):
        root = Path(root).resolve()
        index = root / "docs/00-index.md"
        # 只识别 Desk 源码仓，避免把普通项目的 docs 当成 Desk 开发说明。
        if root in seen or not all(path.is_file() for path in (
                index, root / "manifest.yaml", root / "desktron/lib/grounding/builder.py")):
            continue
        seen.add(root)
        lines.append(f"- Developer documentation: `{index}`." if en else f"- 开发说明：`{index}`。")

    if en:
        lines.append("Read the user guide only when an answer about Desk operations, features or "
                     "configuration needs documentary support. Pure progress queries and checks "
                     "of existing results do not call for the documentation, but actual state "
                     "must still be verified. When documentation is needed, read the relevant "
                     "section directly if you know it; read the index only when the entry point "
                     "is unknown. Content already read in the same valid context can be reused; "
                     "read again when content is missing or the documentation version is known "
                     "to have changed. When changing code, troubleshooting or releasing, read "
                     "the relevant developer documentation as needed. Historical files only "
                     "explain the past and are not a basis for current operations.")
        return "\n".join(lines)
    lines.append("仅在回答需要文档依据的 Desk 操作、功能或配置问题时读取使用说明。"
                 "纯进度查询、已有结果核验不触发文档读取，但仍须核验实际状态。"
                 "需要文档时，相关章节已知则直接读取；入口未知才读取目录。"
                 "同一有效上下文中的已读内容可以复用，内容缺失或已知文档版本变化时重新读取。"
                 "修改代码、排查故障或发布时，按需读取相关开发说明。"
                 "历史文件只用于了解过去，不作为当前操作依据。")
    return "\n".join(lines)
