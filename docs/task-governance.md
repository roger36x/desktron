# 任务治理

任务表示持续交付事项，检查、测试、同步是步骤。一个事项沿用稳定 ID；新建前用 `desk-task list --all` 查重，不因另一会话接手续做而重复建账。

当前数据源、优先级与 revision 规则见[工作流与任务](workflow-task-model.md)。`done/next/blocker` 各最多 160 字，只保存当前快照；过程写 journal。用户/UI 修改优先，冲突先重读。

完成需有对应证据；等待部署或用户验收不能算完成。显式启用完成契约的任务按[完成条件与证据](../desk-docs/reference/task-evidence.md)核对，未启用的任务不自动推导条件。此机制不替 Agent 执行业务验收。

任务详情直接读取账本的有界当前快照，分区展示 `done/next/blocker`、完成条件和关联会话；它不把这些字段拼接成正文，也不改写备忘录。备忘录降为可选的长篇补充资料，没有关联时不创建空文档。服务端同时监听 `TASKS.md` 与 `task-ledger.json`，账本更新会推送 `task.updated`，已打开详情只替换进展区，不打断备忘编辑。
