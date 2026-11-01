# 飞书 Bot 接入与验收

入口是 **代理** → 飞书 Bot。可新建应用，也可导入已有应用；Bot 的代号和飞书 App ID 是不同字段。

## 配置顺序

1. 按接入向导准备飞书应用，开启机器人能力。
2. 填写 App ID，App Secret 通过专用凭证入口保存，不进入声明 JSON。
3. 开通运行时所需权限，订阅消息事件，完成飞书侧发布和可用范围设置。
4. 选择引擎、模型、工作目录以及允许的用户和群聊。
5. 启动连接，发送测试消息，核对执行记录与回复。

## 权限与事件契约

当前向导与运行时使用以下权限：

| 权限 | 用途 |
| --- | --- |
| im:message | 接收消息 |
| im:message:send_as_bot | 以 Bot 身份回复 |
| im:chat:read | 获取群聊形态，决定消息路由 |
| im:resource | 读取消息中的图片与文件 |

事件为 `im.message.receive_v1`，使用长连接接收。权限名称与平台版本可能变化；修改向导时同时核对运行时与测试，不依赖旧截图。

## 验收与排障

分别检查连接、接收事件、消息路由、引擎执行、内容检查和回复。连接在线只能证明其中一段可用。遇到重复回复，核对是否有多台机器同时启用了同一 App ID。

本机配置不会因代码同步自动变成远端配置。迁移时检查原端启停状态、目标端引擎登录和独立凭证。禁止将 env 文件或 App Secret 放入导出声明。

实现：[向导](../desktron/bin/lark-bot-wizard.py)、[长连接入口](../desktron/bin/lark-ws.mjs)。契约检查：[test_larkbot_wizard.py](../desktron/tests/test_larkbot_wizard.py)。运行机制见[Bot 运行链路](agent-hosting-design.md)。
