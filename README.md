# Desktron

Desktron is a desktop AI workbench that lets you switch between multiple coding engines.

When you use several AI coding engines at the same time — say Claude, Codex, and Pi Agent — each one maintains its own sessions and context. Solutions discussed, decisions made, and project knowledge built up in one engine are invisible to the others. Switching engines means starting over.

Desktron sits above these engines and maintains a unified project memory and session history, so different engines can work within the same context. In a Shepherd session you can switch engines mid-conversation, and project memory is shared across sessions. When an engine is no longer the right fit, moving to another one does not require rebuilding context.

## Key Features

- **Multi-engine switching** — switch between Claude, Codex, and Pi Agent within the same Shepherd session without losing context; whether images and attachments carry over depends on the target engine.
- **Unified memory** — project knowledge, decisions, and progress persist across engines; a new engine picks up where the last one left off.
- **Session management** — session history is archived in one place, with compression, checkpoints, and cross-session search.
- **Workflows and tasks** — organize work into workflows, track task status, and reference code repositories.
- **Built-in apps** — ships with 7 desktop apps: Notes, Excalidraw, Terminal, Browser, Monaco IDE, File Explorer, and TaskNote.
- **Scheduled tasks** — set up timed or manually triggered maintenance and execution tasks.
- **Mobile access** — connect from a phone or other device on the local network.

## Status

Early preview, designed for personal use on a single machine. macOS uses a Swift native shell; Windows has a Rust / WebView2 native shell — see [Windows notes](docs/windows.md); Linux can run as a headless service. Desktron does not ship any engine CLI; you need to install and sign in to each engine yourself.

## Quick Start

Source-based setup on macOS / Linux. Requires Python ≥ 3.11, Node ≥ 22.19, npm, and Git.

```bash
git clone https://github.com/roger36x/desktron.git && cd desktron
bash desktron/bin/desktron deps               # check dependencies
bash scripts/dev/build-builtin-apps.sh        # build built-in apps (requires npm)
bash desktron/bin/desktron ui 7765            # open http://127.0.0.1:7765/ for first-run setup
```

First launch does not depend on any external online resources. If runtimes are missing, run `bash desktron/bin/desktron prepare` first to set up the managed environment. For packaged installation and Windows entry points, see [First-run setup](desk-docs/first-run.md).

## Documentation

- [User manual](desk-docs/00-index.md)
- [Feature inventory](docs/feature-inventory.md)
- [Development & maintenance](docs/00-index.md)

## Repository Structure

| Directory | Contents |
| --- | --- |
| desktron/ | Python server, engine adapters, and tests |
| desktop/ | macOS Swift shell and other native shells |
| frontend/ | Workbench UI and built-in apps |
| desk-docs/ | User manual (shipped with the product) |
| docs/ | Development notes (not shipped) |

## Configuration

Network and organization-specific addresses are provided via environment variables or `~/.config/desktron/env`, not hard-coded:

| Variable | Purpose |
| --- | --- |
| `DESKTRON_REMOTE_TOKEN` | Enables LAN access and requires this token |
| `DESKTRON_INTERNAL_DOMAINS` | Comma-separated internal domains; the web tool will not fetch these |
| `DESKTRON_UPDATE_BASE` | Self-hosted update URL; unset means no update checks |
| `DESKTRON_SETUP_PROXY` | Proxy for first-run runtime downloads if direct access fails |

## Development

Start by reading [AGENTS.md](AGENTS.md). Dependency check and tests:

```bash
bash desktron/bin/desktron deps
bash scripts/dev/refactor-gate.sh quick
bash scripts/dev/refactor-gate.sh full
```

Tests are organized into `quick / module / full / release` tiers. See the [development guide](docs/development.md) for details. Sync and restart procedures are in the [release notes](docs/release.md).

## License

Copyright 2026 roger36x

Apache-2.0 — see [LICENSE](LICENSE). Third-party components and their licenses are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

# Desktron

Desktron 是一个可多引擎切换的桌面 AI 工作台。

当你同时使用多种 AI 编程引擎——比如 Claude、Codex、Pi Agent——它们各自维护独立的会话和上下文，彼此之间的工作记忆是隔离的。在一个引擎里讨论过的方案、做过的决定、建立的项目认知，换到另一个引擎就需要从头说起。

Desktron 解决的是这个问题。它在多个引擎之上维护统一的项目记忆和会话历史，使得不同引擎可以在同一个工作上下文中协作。你可以在同一个 Shepherd 会话中切换引擎，项目记忆也在不同会话之间共享。当某个引擎不再适合当前任务时，切换到另一个引擎不需要重建上下文。

## 主要能力

- **多引擎切换**：在同一个 Shepherd 会话中切换 Claude、Codex、Pi Agent，工作上下文不中断；图片和附件能否迁移取决于目标引擎。
- **统一记忆**：项目认知、决策记录和工作进展跨引擎持久保存，新引擎接手时自动获取已有认知。
- **会话管理**：会话历史统一归档，支持压缩、检查点和跨会话检索。
- **工作流与任务**：按项目组织工作流，管理任务状态和代码仓引用。
- **内置工具**：随附 7 个桌面应用——备忘录、Excalidraw、终端、浏览器、Monaco IDE、文件浏览器和 TaskNote。
- **定时安排**：设置定时或手动触发的维护与执行任务。
- **手机访问**：局域网内从手机或其他设备连入工作台。

## 状态

早期预览版，面向个人在本机使用。macOS 使用 Swift 原生壳；Windows 有 Rust／WebView2 原生壳，详见 [Windows 说明](docs/windows.md)；Linux 可作为无界面服务运行。Desktron 不分发任何引擎 CLI，需要用户自行安装和登录。

## 快速开始

macOS／Linux 源码启动，需要 Python ≥ 3.11、Node ≥ 22.19、npm 和 Git。

```bash
git clone https://github.com/roger36x/desktron.git && cd desktron
bash desktron/bin/desktron deps               # 检查依赖
bash scripts/dev/build-builtin-apps.sh        # 构建内置应用（需要 npm）
bash desktron/bin/desktron ui 7765            # 打开 http://127.0.0.1:7765/ 进入首次设置
```

首次启动不依赖额外在线资源。缺少运行时可先运行 `bash desktron/bin/desktron prepare` 准备托管环境。发行安装与 Windows 入口见[首次设置](desk-docs/first-run.md)。

## 文档

- [使用手册](desk-docs/00-index.md)
- [核心功能盘点](docs/feature-inventory.md)
- [开发与维护](docs/00-index.md)

## 仓库结构

| 目录 | 内容 |
| --- | --- |
| desktron/ | Python 服务、引擎适配与测试 |
| desktop/ | macOS Swift 壳及其他原生壳 |
| frontend/ | 工作台界面和内置应用 |
| desk-docs/ | 使用手册（随产品分发） |
| docs/ | 开发说明（不随产品分发） |

## 配置

组织或网络相关的地址通过环境变量或 `~/.config/desktron/env` 提供，不写进代码：

| 变量 | 用途 |
| --- | --- |
| `DESKTRON_REMOTE_TOKEN` | 设置后开放局域网访问，并要求该令牌 |
| `DESKTRON_INTERNAL_DOMAINS` | 逗号分隔的内网域名，网页工具不抓取这些域名 |
| `DESKTRON_UPDATE_BASE` | 自托管的更新地址；不设置则不检查更新 |
| `DESKTRON_SETUP_PROXY` | 首次下载运行时失败时使用的代理 |

## 开发

开工先读 [AGENTS.md](AGENTS.md)。依赖检查与测试：

```bash
bash desktron/bin/desktron deps
bash scripts/dev/refactor-gate.sh quick
bash scripts/dev/refactor-gate.sh full
```

测试分 `quick / module / full / release` 四层，详见[开发指南](docs/development.md)。同步和重启流程见[交付说明](docs/release.md)。

## 许可

Copyright 2026 roger36x

Apache-2.0，见 [LICENSE](LICENSE)。第三方组件及其许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。欢迎贡献，方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。
