# App Control 契约

App Control Runtime 统一管理应用发现、观察、动作、租约、授权和回执。CLI、MCP 与浏览器桥是接入通道，不能分别实现一套绕过 Runtime 的权限判断。

## 应用声明

新建或更新应用必须声明 `agent_operability: none|L1|L2|L3|L4`。none 需给出 `agent_operability_reason`；其他等级须有匹配的 `agent_control`。等级说明能力范围，不证明某个 adapter 已实现全部能力。

| 等级 | 能力 |
| --- | --- |
| L1 | 打开与观察 |
| L2 | 增加可撤回的本地修改 |
| L3 | 增加需授权的外部写入 |
| L4 | 增加后台执行、事件等待与恢复 |

结构以 [app-definition schema](../../schemas/app-definition.schema.json) 和 [agent-control schema](../../schemas/agent-control.schema.json)为准。控制声明包含 identity、资源、observations、actions、permissions、events、receipts；不要从历史示例推断默认权限。

## 身份与执行

[capability.py](../../desktron/lib/app_control/capability.py)使用会话随机能力令牌和进程身份校验。公开 session id 不构成授权；Unix Socket 请求还核对进程归属。令牌不得进入源码、日志正文或永久会话历史。

[Runtime](../../desktron/lib/app_control/runtime.py)持久化租约、操作、审批与回执。动作检查资源范围、当前状态、预期观察版本及幂等条件。导航后旧节点或观察可能失效，调用方应重新观察，不能盲目重放点击。

Agent 自开页面与接管用户页面分别处理；设置和授权界面受保护。浏览器原生桥负责观测和动作，后台执行与等待由对应 operation 状态恢复，不能拿前台 DOM 租约冒充长期授权。

## 验证

至少检查未授权、资源越界、页面更新、重复请求、人工接管、断线恢复和敏感证据处理。截图与正文仍需按实际内容判断，不能因结构化字段已脱敏就保证整份材料可公开。

开发验证入口在源码仓的 `docs/agent-operable-app.md`。
