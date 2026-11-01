# 首次设置

先完成本机环境、AI 引擎和工作工具检查，再开始会话。

## 操作顺序

1. 打开首次设置；已进入 Desk 时，从「设置 → 首次设置」重新打开。
2. 按检查结果准备 Python、Node、Git。Python 最低为 3.11；发行安装器准备 Desk 托管解释器及其依赖，不使用系统自带 Python。Node 必须同时满足版本和本机架构要求。安装完成后重新检查，不以终端窗口关闭判断成功。
3. 在 AI 引擎中选择已安装的 Claude Code、Codex 或 Pi Coding，完成该引擎的登录或 Provider 配置。
4. 按工作需要连接 lark-cli 等工作工具。工具已安装、工具已登录、具备目标资源权限是不同状态。
5. 新建或打开一个工作流，发送一条简单请求，确认引擎能完成一轮并显示结果。

## 安装验收

发行安装会在打开客户端前执行隔离验收，检查真实后端启动、页面入口与 Shepherd 状态；失败时安装返回错误，不能跳过后直接认定可用。结果位于 `~/.local/share/desktron/install-check.json`（自定义数据目录时位于其父目录）。可运行 `desktron install-check` 重新检查，过程不使用现有凭证、不发送模型请求，也不重启当前 Desk。

离线验收不等于已登录或模型可调用。完成引擎配置后，仍需按第 5 步由用户实际发送一条请求验收。

## Windows

Windows 版需要 64 位 Windows 10 1809 或更高版本。解压发行包后，在包目录运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1`，不需要管理员权限。安装器会准备 WebView2 Runtime（缺少时）、Desk 托管 Python、程序目录与开始菜单入口，并把 `%USERPROFILE%\.local\bin` 写入用户 PATH；新开的终端才能看到这一变化。

- Git for Windows 是必备项：技能脚本与 Claude Code 都通过它提供的 bash 执行。首次设置会给出安装命令。
- 内嵌终端默认使用 PowerShell。
- 应用快捷键：macOS 的 ⌘X 对应 `Ctrl+Shift+X`，⌘⇧X 对应 `Ctrl+Shift+Alt+X`；单独的 Ctrl 组合键留给终端程序。
- 暂不支持应用内更新，更新时运行新版安装包。运行前先在应用内退出 Desktron。
- 「原生应用观察」只支持 macOS，Windows 版不显示该设置。

## 操作助手

安装和认证步骤可能打开操作助手。它显示执行进度、终端输出和退出结果。遇到密码或确认提示，在该终端完成输入；关闭窗口不等于操作已完成。

若报告退出码 127，先保存输出和诊断记录，再核对失败命令的路径及环境。即使工具在日常终端可用，Desk 子进程仍可能遇到 PATH 或安装位置变化。

后续：[设置与更新](settings.md)、[日志与故障反馈](diagnostics.md)。
