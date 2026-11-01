---
name: pi-agent-setup
description: 配置和排查 Desk 的 Pi，包括环境检查、安装、模型服务登录、请求验证、故障诊断和反馈草稿。
---

# Pi Agent 配置助手

使用本 Skill 时，把确定性检查与操作交给脚本，Agent 负责解释结果和选择下一步。Pi 尚未安装或不可用时，检查和诊断仍应工作。

当本 Skill 作为 Desk 临时配置会话运行时，先阅读 [references/runtime-agent.md](references/runtime-agent.md)。该文档定义完成标准、状态机、系统提示词和结构化输出；普通命令行排查无需加载。

## 工作流

1. 先运行只读检查：

   ```bash
   python3 .claude/skills/pi-agent-setup/scripts/pi_setup.py check --json
   ```

2. 根据 `next_actions` 解释当前状态。只处理未通过项，不覆盖已有 Pi 配置或有效登录态。
3. 安装或修复前，向用户展示动作的 `label`、`command` 和 `effect`。只有用户明确要求执行时才运行。
4. 遇到密码、SSO、浏览器授权或设备确认时，使用 Desk 的交互终端并暂停；完成后重新运行 `check`。
5. 完成状态必须由指定 Provider 的真实最小请求证明。二进制存在、配置文件存在或 Provider 状态正常都不是端到端成功。

## 模式

- `check`：检查 Node、npm、Pi 和本地模型目录，不修改状态。
- `plan`：输出下一步动作。它只给计划，不执行命令。
- `diagnose`：输出可分享的脱敏诊断。不得补充会话正文、用户文件、完整配置或凭证。
- `report-draft`：根据诊断生成问题反馈草稿。用户确认前不得外发。

## Provider 分流

- 打开 `pi` 交互终端，引导用户输入 `/login` 选择 Provider。不要读取或代填 API Key。
- `pi auth check` 必须带明确 Provider，并且不使用 `--credentials`。它只能证明该 Provider 的凭证检查结果。

## 故障与反馈

生成诊断：

```bash
python3 .claude/skills/pi-agent-setup/scripts/pi_setup.py diagnose --json
```

生成草稿：

```bash
python3 .claude/skills/pi-agent-setup/scripts/pi_setup.py report-draft \
  --error-code PI-UNKNOWN --summary "用户可见的故障现象"
```

草稿与诊断文件只保存在本机。是否提交、提交到哪里由用户决定；提交前先检索重复问题并取得用户授权。

## 停止条件

- 状态为 `needs_user`：等待用户完成交互，不自动重试。
- 状态为 `uncertain`：保留操作编号和证据，不重复安装或创建资源。
- 检测到疑似凭证：停止外发，仅保留本机脱敏结果。
