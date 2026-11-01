# 记忆写入约定

> 本文件是工作流记忆的通用语义约定。实际命令、参数和准入规则以 memory skill 为准；物理目录名不作为用户接口。

## 四层模型

| 层 | 用途 | 写入者 |
| --- | --- | --- |
| `journal` | recap 流水，记录发生过什么 | Desk 隔离 recap，只追加 |
| `notes/rollup-current.md` | 当前项目模型 | 记忆引擎整篇重写 |
| `pinned` | 用户明确确认、不得被自动压缩覆盖的约束与纠正 | memory skill，经确认闸 |
| `sealed` | 仅限本机的敏感结论和红线 | memory skill |

旧目录名 `sot` 只是 pinned 与 fact 的物理兼容容器，不是现行用户概念。旧 `private/shared/sot` 命令别名仅为存量兼容，不应写入新说明或新流程。

## 事实与判断

- 项目过程进入 journal；当前整体状态由项目模型维护。
- 用户确认的长期约束、纠正和不可压缩判断进入 pinned。
- 术语、接口、字段、口径等领域事实使用 `kind=fact`，通过 fact 入口写入。
- 政治、私聊、具名个人评价及其他不得外流的结论只进入 sealed。
- 人物关系、归属和依赖以 graph 为准，不从叙事正文推断。

记忆是本机单用户运行数据，不随 Git 或发行包流通。需要跨用户共享时，先经人审贡献到产品文档、Skill、App 或业务权威源。

## 写入

不得直接编辑 `.memory/` 的受管文件。新增和更新均使用 memory skill：

```bash
# 用户确认的约束
python .claude/skills/memory/scripts/mem_add.py \
  --tier pinned --user-confirmed --ws <工作流> --id <id> \
  --summary "一句话" --body "正文"

# 领域事实
python .claude/skills/memory/scripts/mem_add.py \
  --tier fact --kind fact --ws <工作流> --id <id> \
  --doc-type <类型> --keywords <关键词> --source <出处> \
  --summary "一句话" --body "正文"

# 更新；目标为 pinned judgment 时同样必须确认
python .claude/skills/memory/scripts/mem_update.py \
  --id <id> [--user-confirmed] [--summary ...] [--source ...] [--body ...]
```

pinned 的新增、修改和删除都必须由用户明确确认。旧 `sot/shared` 别名及直接 curator 写入口最终落到 pinned judgment 时，不能绕过同一确认检查；确认后条目写入 `user_confirmed: true`。历史上没有该标记的 judgment 作为只读的“旧未确认判断”继续可检索，但不具有 pinned 优先级，不会静默丢失或自动升级。recap 只能提出候选，不能自动转正。

## 读取

统一使用 memory skill 的只读入口，不裸读受管文件：

```bash
python .claude/skills/memory/scripts/mem_read.py show --id <id>
python .claude/skills/memory/scripts/mem_read.py search <词> [--tier ...]
python .claude/skills/memory/scripts/mem_read.py list [--tier ...]
python .claude/skills/memory/scripts/mem_read.py graph neighbors <实体>
```

受控入口会保留访问记录，并返回置信度、时效、覆盖和冲突信息。关系事实只读取 graph。

## 证据与置信度

置信度由 `source` 中的独立来源数自动派生：无可核来源为 `reported`，一个独立来源为 `documented`，两个及以上为 `corroborated`。同一文档的多个锚点不算多个来源。置信度表示引用覆盖，不等同于来源权威度，也不能替代实际核验。

追加式 journal 的修正只能新增条目并指向原记录；项目模型可以重写。当前状态应包含版本、时间、任务或下一步等可核对信息。