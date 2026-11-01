# Shepherd Codex/Pi 切换修复（80a5）

> 历史修复记录：下文保留当时的实现和测试结论，不作为当前部署状态或操作授权。Codex 提示投递已继续演进，现行载体见[上下文投递](context-delivery.md)和[切换安全](shepherd-switch-safety.md)；不能据本文重新启用旧投递路径。

## 工具历史

Codex `custom_tool_call.input` 是原始字符串，不是 JSON；Pi 使用标准 `toolCall.arguments={"input":raw}` 承载。相邻 `shepherd-custom-tool-v1` 原生 custom entry 保存调用类型，SessionManager 不把该标记放入模型上下文。回读校验标记、名称、调用 ID、输入及结果配对，恢复 custom 类型；普通 function call 仍严格要求 JSON 对象。不根据参数外形推断来源类型。

追平只追加已经提交、通过来源哈希核验的历史，不执行工具。测试覆盖 offset 1 已追平、2–9 待追平，以及重复 append 幂等。

## Codex 系统提示

本机 0.154.0 实测：预创建 Thread 后，仅向 resume 传 `developerInstructions` 不足以让模型收到规则；`collaborationMode.settings.developer_instructions` 也未在此路径可靠生效，已撤回该尝试。

采用原生 `turn/start.additionalContext.desktron={kind:"application",value:...}`，模型请求中为 developer 内容。仅 Shepherd Codex 显式启用，普通引擎与 Plan 模式配置不变。

现有 Thread 无需更换 Binding：下一次合法回合即可带入完整规则。每回合明确标注当前快照优先于历史快照；原生审计记录保留旧快照，不能声称它完全不落盘。公开 source 投影过滤系统元数据，不把它当作公共对话或跨引擎历史。新增本地模拟模型测试验证真实请求、两轮刷新与公共投影隔离，不消耗线上模型额度。

## 恢复栅栏

用户选回旧引擎，不代表旧 Producer 可以复活。发送前必须先恢复持久化 pending_switch，即使旧 UI 已清空 next_engine；原目标 complete_switch 成功后，再执行最新选择。失败则拒绝发送，用户消息不落盘。

真实会话恢复、发布、同步及重启须单独获授权。当前修复不删除旧私有 Thread，不改真实 pending_switch，不重放历史工具。

## 验证记录

合并后 Shepherd 与 app-server 联合回归 250 通过、17 跳过；全量除单独运行的 Codex 长文件为 5534 通过、69 跳过，长文件 19 项通过。原生提示专项显式启用，配合恢复门面共 30 项通过。前端静态检查、Node 语法检查及 diff-check 通过。真实故障会话尚未恢复，不能将临时测试库的恢复结果表述为线上恢复完成。
