# 核心功能盘点

本页列当前入口与权威契约；测试链接表示验证入口，不表示已完成真实场景验收。

## 会话与协作

| 能力 | 当前行为 | 契约／实现依据 |
| --- | --- | --- |
| 三引擎 | Claude、Codex、Pi 的执行、授权、协议准入、原生终态与恢复 | [引擎接入](engine-integration.md) |
| Shepherd | 同一公开会话切换引擎；统一归档、单 Writer、来源校验和恢复栅栏 | [架构及边界](shepherd.md) |
| 上下文 | 主动／自动压缩、原生检查点、有界历史降级和超窗安全恢复 | [检查点](shepherd-context.md)、[准入](shepherd-admission.md) |
| 历史 | 先搜索候选，再按预算读取和复验引用；不盲扫完整会话 | [历史检索](shepherd-history.md) |
| 切换与等待 | 派发前预检；语义进展与连接活动分开；软停顿不直接终止 | [切换](shepherd-switch-safety.md)、[执行预算](shepherd-execution.md) |
| 待发队列 | 浏览器持有草稿与待发项，页面级调度在当前回合结束后核验回执再续发 | [队列](frontend-message-queue.md) |
| 跨会话协作 | `send` 单向通知；`ask` 有界读取稳定历史并附引用，不唤起目标模型 | [用户手册](../desk-docs/sessions.md) |
| Pi 子代理 | 受控只读执行、状态详情、持久回投、5 分钟无进展和 30 分钟总时限 | [子代理](pi-subagents.md) |

## 工作组织与工具

| 能力 | 当前行为 | 契约／实现依据 |
| --- | --- | --- |
| 首次设置 | 环境、三引擎与工作工具检查 | [操作](../desk-docs/first-run.md) |
| 工作流与任务 | 资料和代码仓引用；TASKS.md 与持久当前态账本；完成证据试点 | [任务](workflow-task-model.md) |
| 记忆 | 系统对话与 recap journal、自动项目模型和本机 sealed 红线；保留旧 pinned 输入兼容；会话注入完整项目模型与检索入口，无独立记忆摘要 | [记忆](knowledge-memory-management.md) |
| Prompt | 六种“引擎 × 运行方式”的声明、来源和投递治理；运行时仍由各适配器实际装配 | [Prompt 治理](system-prompt.md) |
| Web 查证 | Pi 的搜索使用后台匿名隔离原生浏览器（macOS WKWebView／Windows WebView2），抓取保持逐跳校验的直连 HTTP；Codex 使用原生搜索；Claude 使用原生 Web 工具 | [Web 工具](web-tools.md) |
| App Control | 默认匿名隔离标签；按站点授权使用身份；持久操作、审批与回执 | [契约](agent-operable-app.md) |
| macOS Desktop Control | 独立应用身份、限时能力票据、Accessibility 只读探针，以及人选定窗口后的 L1 只读结构观察（内置 Shadowrocket）；不能执行任何原生写动作 | [安全边界](desktop-control-runtime.md) |
| 应用 | 全部／官方／本地；7 个随发行版提供的桌面应用 | [用户手册](../desk-docs/apps.md) |
| 安排 | 定时／手动执行、系统维护和本机运行记录 | [调度](arrangements.md) |
| 飞书 Bot | 接入配置、连接与对话 | [接入](agent-hosting-guide.md) |

## 运行、界面与交付

| 能力 | 当前行为 | 契约／实现依据 |
| --- | --- | --- |
| Token 用量 | 本机 Provider usage 标量账本；按日期、工作流、会话和回合归因，不保存正文 | [操作](../desk-docs/settings.md)、[实现](../desktron/lib/usage_stats.py) |
| 原生浏览器 | 用户页和 Agent 标签分层；隔离标签不带身份；飞书 applink、客户端协议和证书质询由原生壳处理 | [操作](../desk-docs/browser-control.md)、[回归](../desktron/tests/test_native_browser_feishu_auth.py) |
| 更新与重启 | macOS 原生宿主事务更新 payload 与 `.app`；Windows 用新版安装包更新，尚无应用内更新；两平台原生 Supervisor、Linux systemd 监督后端换代 | [发布](release.md)、[重启](restart-policy.md) |
| 诊断 | 运行日志、会话与连接状态；实现与场景验收分层判断 | [排障](../desk-docs/diagnostics.md) |

## 能力边界

普通会话不会自动转为 Shepherd。分支按引擎能力开放；Shepherd 可从已完成且验证通过的回复复制独立历史并创建同引擎分支，不重跑历史工具或继承授权。文件回退由 Claude 普通会话提供，只回退文件、不回退对话；Shepherd 不支持文件回退。Claude 子代理未开放。Pi 子代理只读，来信不能批准操作。跨引擎图片、附件和检查点兼容受目标引擎能力限制。

网页工具不携带登录态；Pi 搜索使用固定域名的后台 App Control 隔离标签，抓取仍走受控直连 HTTP。需要登录、JavaScript 交互、点击或截图时显式改用 App Control。内置浏览器不强制改写网页深浅，页面外观由网站自身决定。飞书设备授权依赖新版原生壳和飞书客户端；企业策略要求客户端证书时，还依赖本机具有匹配证书。代码存在不等于当前安装包已经生效。

手机或其他设备访问需自行配置 `DESKTRON_REMOTE_TOKEN` 并保证网络可信。HTTPS 访问依赖用户自行安装的 Tailscale Serve（`DESKTRON_SERVE_PORT`），只在 tailnet 内可达；Desk 不提供公网入口，也不使用 Funnel。同步代码、重启后端、安装新原生壳和发布发行包是不同步骤，均需分别验收。
