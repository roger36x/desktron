# Desk 运行后端

本目录包含可运行的启动器、Python 服务、引擎适配、业务模块与测试。Desk 在运行时管理会话、事件、上下文、数据及授权，具体模型执行交给对应引擎。

| 目录 | 内容 |
| --- | --- |
| bin/ | CLI、部署、终端和工具入口 |
| lib/ | HTTP/WebSocket、引擎、任务、Bot、记忆和远程模块 |
| tests/ | 按功能划分的回归测试 |

从源码仓根使用 `bash desktron/bin/desktron`。`ui` 启动工作台；`deps` 检查依赖；`validate` 校验声明；`prepare/install` 准备安装环境；`sync-home/release` 处理源码交付。

后端的 REPO_ROOT 指运行工作台根，不一定指 Git 源码仓。导入 deskcfg 需要有效启动参数；使用统一测试运行器提供隔离环境。

继续阅读：[架构](../docs/architecture.md)、[数据归属](../docs/runtime-data.md)、[测试](../docs/development.md)、[交付](../docs/release.md)。
