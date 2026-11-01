# 引擎认证提示与恢复

认证提示记录实际 Producer 引擎、Provider、发生时间和请求起点。Shepherd 切换引擎或 Pi 切换 Provider 后，只展示与当前选择相关的错误；来源不明的旧 Shepherd auth 提示保留为诊断，不作为当前引擎的登录故障展示。

登录按钮使用错误所属来源。登录终端关闭后重新核验登录状态，失败或未知不清除提示。Claude 订阅检查不能证明 API Key 通道可用，Pi 的任一 Provider 可用也不能证明其他 Provider 可用。仅有配置记录不能证明认证恢复，须由后续成功请求确认。

同一来源的成功原生回合会清除较早认证错误，但取消和传输退出不构成恢复依据。清除边界持久保存，旧请求迟到的失败不能重新挂出提示，新的失败仍可显示。认证提示维护不改变原生回合的成败、重试或恢复行为。SSE 与配置回读使用相同来源过滤和 revision，空的权威快照会撤掉旧提示。

## Claude 长期令牌

Shepherd 经 Agent SDK 恢复 Claude 会话时，SDK 从钥匙串复制一份删去 refreshToken 的凭证，常驻进程不能续期；`claude_auth.ensure_fresh_subscription` 只能在派发前续钥匙串，覆盖不了进程常驻期间副本到期。2026-10-07 实例：22:29:58 复制的令牌 23:29:44 到期，23:30:32 回合以 `OAuth token revoked` 失败。

[claude_token.py](../desktron/lib/claude_token.py) 管理 `claude setup-token` 生成的一年期令牌，存于 `~/.config/desktron/claude-oauth-token`（0600），不放入注入所有引擎的 `~/.config/desktron/env`。Shepherd 主回合与 recap 仅对 Claude Binding 注入 `CLAUDE_CODE_OAUTH_TOKEN`；调用方已设置时不覆盖，文件无效时记日志并回到钥匙串登录。该变量优先于钥匙串凭证，设置后 `ensure_fresh_subscription` 跳过。两个 Claude runner 的 BASH_ENV 脚本把它从 Bash 工具子进程中清除。长期令牌只能请求模型，不能用 claude.ai 连接器和远程控制。Windows 的 `login` 自动获取入口不提供 POSIX PTY，会在启动子进程前明确拒绝；需要时由用户在外部终端运行 `claude setup-token`，再通过 `set` 的标准输入保存。Windows 不以 POSIX 0600 模式位断言证明 ACL 已验收。测试同时隔离 `HOME` 和 `USERPROFILE`，不得写入真实用户目录。回归：`test_claude_token.py`。

会话生命周期见[引擎与会话](engine-integration.md)。
