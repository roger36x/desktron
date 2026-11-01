# Bot 运行链路

飞书 Bot 将消息接入和引擎执行连接起来。声明、长连接、会话映射、执行上下文与外发检查分属不同模块。

## 调用关系

| 模块 | 责任 |
| --- | --- |
| larkbot/api.py | 配置、连接控制、后台监测和多端查询 |
| sidecar.py 与 lark-ws.mjs | 长连接进程与事件接收 |
| route.py、handler.py | 消息过滤、锚点路由、执行组织 |
| context.py | 工作目录、角色、记忆和知识上下文 |
| runner.py | 调用选定引擎并返回执行结果 |
| outbound.py、redline.py | 外发内容检查与结果处理 |
| larkapi.py | 飞书接口与凭证读取 |
| store.py | 声明与会话映射持久化 |

## 数据契约

`.workbench/agents/<id>.json` 保存声明，`_state.json` 保存消息锚点到会话的映射。代号用于本地文件标识，App ID 用于飞书应用；App Secret 只由凭证存储提供。

声明包含引擎、模型、cwd、启停、允许用户/群聊、上下文和外发选项。`created_on` 用于提示同一声明被复制到另一端的情况，不是跨端单实例锁。两端分别保存运行态，复制声明不保证迁移完成。

## 执行边界

消息过滤通过后才构建上下文并启动引擎；消息内容不代表 Desk 所有者授权。Bot 自行组装上下文，调用 Pi 时关闭原生技能和上下文文件发现，避免额外注入技能清单或仓库规则。外发检查可能阻止或保留结果，不应将“引擎已完成”直接显示成“用户已收到”。Bot 的会话映射和普通桌面会话各有生命周期。

排查顺序为接收、路由、执行、外发。需要记录同一请求的阶段和失败位置，同时避免记录 App Secret、认证响应和无关消息正文。

依据：[实现目录](../desktron/lib/larkbot)、[路由测试](../desktron/tests/test_larkbot_route.py)、[执行链测试](../desktron/tests/test_larkbot_chain.py)、[两端查询](../desktron/tests/test_larkbot_peers.py)。接入步骤见[Bot 接入](agent-hosting-guide.md)。
