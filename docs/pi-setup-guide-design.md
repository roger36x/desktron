# Pi 配置向导落地设计

> 候选设计，不是当前操作手册。下述新增模块、状态文件和 API 为方案目标，不能据此调用或认定已交付；现有配置入口见[首次设置](../desk-docs/first-run.md)。

## 1. 产品决策

就绪中心标题区保留「配置向导」入口，打开不进入普通会话列表的临时会话。Pi 错误卡和设置页可跳入同一会话；AI 引擎列表保留原生入口。

完成标准：环境可用、Pi 版本兼容、指定 Provider/精确模型可用，并经 Desk 实际路径完成合成最小请求。其余偏好均为可选定制。

## 2. 复用与新增

| 能力 | 现状 | 方案 |
|---|---|---|
| 环境与引擎检查 | `/workbench/setup`、manifest | 直接复用唯一事实源 |
| Pi 安装 | manifest npm 命令 | 由安全 action_id 调用 |
| 任务恢复 | `setup_tasks.py` | 复用 operation_id、PTY、禁止重放 |
| 原生登录 | `openLoginTUI("pi")` | 嵌入临时会话，用户执行 `/login` |
| 模型探测 | `_pi_probe/_pi_auth_ready` | 增加精确 Provider 结论 |
| 真实验证 | 仅有测试脚本 | 新增无会话、无工具、合成输入验证器 |
| 诊断 | `pi_setup.py` | 补 operation、稳定错误码、问题查重 |

## 3. 运行架构

新增 `provision/pi_guide.py` 管理状态机与快照；`provision/pi_actions.py` 把 action_id 映射到 manifest、Provider 和验证器。Agent 与前端不得提交任意 Shell。

状态保存在 `DATA_HOME/pi-guide.json`：会话、revision、阶段、operation、Provider、模型、证据、待用户动作和错误。禁止保存命令、终端输出、凭证、配置正文和模型回复。

核心流程不依赖模型。Agent 负责解释、选择、修复和归因；不可用时退化为模板文案。

## 4. 状态机

1. **resume**：附着现有操作；重启后标记 interrupted，绝不自动重跑。
2. **discover**：检查运行时、Pi 路径/版本、配置权限、Provider、目录和默认模型。
3. **plan**：只列阻塞项，保留有效安装、凭证与用户偏好。
4. **install**：按 manifest 补依赖和 Pi；一次一个操作。
5. **provider**：用户选择 Pi 原生或自定义 Provider。
6. **authenticate**：打开 Pi TUI 完成登录。密码、Key、SSO 和扫码均由用户完成。
7. **model**：读取真实目录，选择精确 ID；不默认改全局模型和 thinking level。
8. **verify**：新建 Pi 进程，用同一 Desk 扩展和模型发送合成输入，关闭工具、会话和项目上下文。
9. **complete/failed**：成功保存证据摘要；失败保留原配置，生成诊断与下一步。

## 5. API 与并发

- `GET /workbench/pi-guide`：读取或恢复。
- `POST .../start`：创建或返回活动会话。
- `POST .../action`：提交会话、revision、action_id 和受限参数。
- `POST .../user-complete`：登录后重检。
- `POST .../dismiss`：隐藏；`cancel` 才终止。

revision 使用乐观并发；全机只允许一个变更操作。结果先落盘再更新 UI；轮询失败显示 uncertain。

## 6. Agent 契约

System prompt 见 Skill reference。输入仅含 snapshot、active_operation、last_user_response 和 available_actions；输出固定结构。

Agent 无权执行 Shell、接收凭证、替用户选 Provider、写默认模型、重放推理或提交论坛。涉及数据、费用、授权和外部写入时暂停。

## 7. UI 与恢复

弹层包含进度、说明、证据和等待用户卡。终端只展示当前操作。隐藏后保留恢复入口；需要用户时发 Desk 提示；完成后可再次检查或诊断。

错误按 ENV、INSTALL、VERSION、AUTH、CATALOG、MODEL、NETWORK、QUOTA、PERMISSION、PROTOCOL、INTERRUPTED、UNKNOWN 分类。只读检查最多重试一次；推理不重放。论坛仅查重并生成草稿。

## 8. 分期与验收

1. **M1**：确定性状态机、受控动作、弹层与恢复；不接 LLM。
2. **M2**：接入 Agent prompt、错误归因和脱敏诊断。
3. **M3**：论坛增加 Desk/Pi 类型、标签和附件契约后开放提交。

验收覆盖已有配置不被覆盖、双击不重复安装、重启不重放、登录暂停恢复、Provider 精确判定、真实请求成功门槛、无凭证落盘、隐藏/恢复、离线与失败分流。实施前还需统一 Pi 官方 `--ignore-scripts` 与 Desk manifest 的安装口径。
