# example-kb

子 KB 入口与导航(只放导航,不放事实)。

- `global/` — 概览、术语表、服务与仓库目录(供人阅读)
- `domain/` — 业务事实,按子域划分(只放事实,不放排查步骤 / 判断策略 / CLI;接口 / 字段 / 口径用 doc_type=reference 承载,结构关系进 graph)
- `graph/entities.yaml` — sot:类型化实体、类型化边、指向笔记的反向引用

派生真实子 KB 时,替换本文件为真实导航;新团队加一个子 KB = 新建一个同构目录 + 在 ontology.sub_kbs 加一行 + CODEOWNERS 加一条。
