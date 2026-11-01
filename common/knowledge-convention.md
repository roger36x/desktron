# 知识写入约定（已并入记忆）

> 2026-08-03 起本地知识库退役。领域事实并入工作流记忆，与判断共用一个实例、一套命令和一份索引，但通过独立的 `fact` 语义入口写入。本文只保留分流判据与迁移指引，写入纪律见 `memory-convention.md`。

## 为什么并

知识与记忆到最后只差 frontmatter 的一个 `kind` 字段与目录位置，而两套 ontology、两套命令、两套索引的维护成本高于它区分出的价值。领域事实的权威来源本来就在飞书等原始渠道，本地这一份的意义是让 agent 在人不在场时也知道同样的事——记忆分层同样能承担。

当前知识与记忆用法见 `desk-docs/knowledge-memory.md`。

## 一条信息写哪里

- **实体 ID 与需要反查的归属关系**（谁汇报给谁、什么依赖什么、谁 own 什么）→ 骨架 `graph`。手工录入、机器校验，关系事实以它为准。
- **领域事实**（术语、别名、接口约定、字段口径、领域全景、排查维度）→ fact 入口，写入时加 `--kind fact` 与 `--doc-type`、`--keywords`。
- **判断与状态**（某方案现在不可靠、某块谁负责、认知怎么走到这一步）→ 当前项目模型；只有用户确认且不得被自动压缩覆盖的约束才进入 pinned。
- **性格、动机、政治、对具名个人的评价** → `_sealed`，不出本机。
- **流水**（今天做了什么、遇到什么）→ journal 层，免闸。

## 写入

```bash
python .claude/skills/memory/scripts/mem_add.py --tier fact --ws <工作流> \
  --kind fact --doc-type <glossary|aliases|landscape|reference|runbook> \
  --keywords k1,k2 --source "<出处>" --id <kebab-slug> \
  --summary "一句话" --body "正文"
```

`kn_add` / `kn_update` / `kn_verify` 已退役，执行时会打印等价写法。

## 外部知识分两类

外部知识的路线是不迁移数据:各来源保留自己的存储平台,统一的是字段规范与 Skill 登记方式,消费方经 skill 按需查。Desk 的外部知识因此有两类挂载。

- **git 类**:用户显式安装 markdown 仓到 `~/.local/share/desktron/kb/`,通过 `kb/<name>/` 只读入口使用。没有自动 monorepo 同步。
- **skill 类**:知识库在平台上,只接查询能力,数据不落盘。登记表在 `common/knowledge-sources.yaml`。

skill 类是新接入的默认方式。两类都在起手注入的知识入口段列出。

## 缓存不受影响

`knowledge/deepwiki/`（仓库 wiki）、`knowledge/alert-rules/`（告警配置）等机器下载的缓存照旧：按来源自己的格式，不强制 frontmatter、不进索引、不背纪律，任意层级都豁免命令闸。

## 存量

原有手写笔记仍可能位于物理兼容目录 `.memory/<ws>/sot/`；该名称不是现行用户入口。`knowledge/` 只可能作为升级用户的存量副本存在，不再随产品发行，也不再写入或自动删除。

跨用户共享先用 kb-review 选择目标：产品通用内容进入 `desktron`，业务事实进入业务自己的权威平台或 Git KB。
