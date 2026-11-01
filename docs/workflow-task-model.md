# 工作流与任务数据契约

完整契约已移入随产品分发的[执行参考](../desk-docs/reference/task-format.md)。本路径保留兼容源码与历史引用；开发和运行时共用同一正文。

`domains/<workflow>/TASKS.md` 仍是人和 AI 共享的任务事实源。全局任务端点 `/workbench/tasks?scope=all` 聚合所有工作流，不再依赖 `_desktop/TASKS.md` 镜像。私有 `DATA_HOME/task-ledger.json` 只保存有界当前态：`done/next/blocker` 各最多 160 字、最近 5 个会话、revision、关闭原因和幂等 receipt；历史过程归 journal。

Agent 通过 `desk-task create|claim|progress|block|complete|release` 维护账本。任务与会话为多对多，用户/UI 修改优先于 Agent，Agent 明确操作优先于 recap 补漏；revision 冲突必须重读。recap 只补充本轮快照中已关联任务的有证据进展，不自动创建、认领或重开任务；写入须通过捕获的 revision 校验。解析或投影失败不能阻断 journal。

完成条件见[证据契约](../desk-docs/reference/task-evidence.md)，查重与收口见[任务治理](task-governance.md)。返回[开发文档目录](00-index.md)。

回归入口：[节点关联](../desktron/tests/test_task_node_link.py)、[仓库引用](../desktron/tests/test_workflow_repos.py)。
