# Desk 开发与维护文档

源码结构、运行契约与维护入口。用户操作见[使用手册](../desk-docs/00-index.md)。

源码实现、测试通过和用户场景验收是不同层面的证据，不得互相替代。

## 先了解全貌

- [核心功能盘点](feature-inventory.md)
- [运行架构](architecture.md)
- [目录和数据归属](runtime-data.md)
- [界面与交互](ui-architecture.md)
- [System Prompt 装配与治理](system-prompt.md)

## 按功能查契约

- [工作流与任务](workflow-task-model.md)
- [GUI 待发消息队列](frontend-message-queue.md)
- [引擎与会话](engine-integration.md)及[认证恢复](engine-auth-recovery.md)
- [Project Shepherd](shepherd.md)：多引擎统一会话
- [Shepherd 存储与执行宿主](shepherd-storage.md)
- [检查点与压缩](shepherd-context.md)、[上下文准入](shepherd-admission.md)及[历史检索](shepherd-history.md)
- [进展、等待与时限](shepherd-execution.md)、[切换安全](shepherd-switch-safety.md)及[事件契约](shepherd-event-contract.md)
- [Pi 子代理](pi-subagents.md)
- [Jev 判定](jev.md)
- [WebSearch / WebFetch](web-tools.md)
- [记忆生成与消费](knowledge-memory-management.md)
- [安排调度](arrangements.md)
- [Bot 接入](agent-hosting-guide.md)及[运行链路](agent-hosting-design.md)
- [App Control](agent-operable-app.md)及[macOS Desktop Control Runtime](desktop-control-runtime.md)
- [应用、知识库与技能扩展](extensions.md)
- [退出与恢复](exit-recovery.md)及[原生生命周期投影](native-lifecycle-projection.md)

## 开发和交付

- [开发与验证](development.md)
- [安装、更新、同步与发布](release.md)
- [Windows 版](windows.md)：结构、兼容层、构建安装与验收
- [宿主监督重启](restart-policy.md)
- [响应延迟](engine-response-latency.md)与 [Shepherd 性能](shepherd-performance.md)
- [会话读模型与持久化性能](session-storage-performance.md)
- [Pi KV 预热边界](pi-kv-warmup.md)：可选优化，默认关闭
- [日志体系](logging.md)
- [文档维护规则](documentation-maintenance.md)及[本轮整体复核结果](documentation-review.md)

每个主题只维护一份当前正文。`docs/` 不随 payload 分发，因此不能作为发行用户完成基本操作的唯一依赖。

## 调研与候选方案

[Pi 配置引导方案](pi-setup-guide-design.md)仍属设计材料，不作为已开放功能的操作说明。

[Shepherd Codex/Pi 切换修复记录](shepherd-codex-pi-recovery.md)保留特定版本的调查与测试结果，不替代现行投递和恢复契约。
