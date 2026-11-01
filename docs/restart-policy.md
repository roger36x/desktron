# 用户重启与自动更新

常规重启由单一平台宿主监督：macOS 为 Swift 原生壳，Windows 为 Rust／WebView2 原生壳，Linux 为 systemd。它只换代 Python 后端，不退出桌面壳，也不等待会话自然完成。

## 准入与接管

1. CLI 向 `POST /workbench/restart` 提交随机 operation ID 和预期 boot ID；旧 boot 请求拒绝。
2. 后端原子关闭新准入。外部副作用立即阻断；已进入的 `http_write`、`admission`、App Control 入队／派发本地临界区最多等待 2 秒。仍有 blocker 时恢复准入并返回 409。
3. 普通会话、Shepherd Runner、压缩、切换和可重算维护可有界中断，启动后按持久证据恢复。
4. macOS 宿主核对 host、launch generation、boot ID 和精确 child PID，持久保存 accepted 后停止当前 child；4 秒仍未退出时仅终止该 child。新 generation 健康后写 completed。
5. Windows 宿主核对相同的 host、generation、boot 与精确 child 身份，通过命名停止事件请求后端退出；4 秒后仍未退出则结束其 Job Object，新 generation 校验成功后持久写 completed。
6. Linux 将一次性 helper 交给 systemd。helper 核对 expected boot、PID 与 `/proc` start token 后停止旧进程；systemd 负责重拉。`TimeoutStopSec=4`、`StartLimitIntervalSec=60`、`StartLimitBurst=5` 限制停止与连续启动失败。

`restart-desk --detach --yes` 的 `--detach` 仅兼容旧调用；CLI 在 accepted 后返回。它不创建 launchd job、不扫描进程、不自行托管重启。原生 Writer 对双重 fork、脱离进程组后代的清理保障继续保留。

## 阻断与未知回执

安排、Bot 外发、App Control 写动作、后台 Bash、文件或目录切换等不可恢复工作阻断重启；未分类工作默认阻断。

只有明确证明宿主未接管才能重新开放准入。ACK 丢失或状态不明保持封闭并进入 `restart_error`，不能自动重放；回执必须匹配原 host、generation、boot 与 PID。删除状态文件或手工放行不是恢复步骤。

## 首次迁移与应用更新（macOS）

本节 `.app`、flock 与更新器事务适用于 macOS。Windows 当前拒绝应用内更新（`update_launcher_unavailable`），退出应用后运行新版安装包；Linux 服务安装维护见[交付说明](release.md)。

若 `/desk-probe` 未返回 `supervised=true`，先从 Desk 外完成新版原生壳安装并重新打开，验收 `supervisor=native` 后才能使用新重启入口；旧运行版重启器不能升级自身。

应用更新会替换 payload 与原生壳，与后端重启共用跨进程 flock，但使用独立的 `update-request` 身份与回执。后端关闭新准入后提交请求；宿主核对 host、generation、boot、后端 PID 与 token，启动固定更新器。更新器先完成解包、版本校验、依赖准备和目标副本写入，再以 operation 与 helper PID 回报 ready。只有此时宿主才停止自己的后端并退出。

更新器等待该精确宿主退出后取得生命周期锁、切换目录并直接启动新 `.app`。等待退出、取得锁、切换、启动和验收全部位于同一回滚边界；宿主已退出后的任一步失败都会重新启动旧壳。验收分别比较 payload rev 与宿主 rev，并要求新 `boot_id`、`host_instance_id` 和 launch generation；失败时只停止自己启动的新宿主，恢复旧 payload、旧 `.app` 与版本标记，再启动旧宿主。日志只保留版本、进程身份、判据和错误类别，不记录完整 argv 或端点载荷。未接管会恢复准入；接管状态未知时保持封闭，禁止重复提交。

开发态只替换原生壳时使用 `desktron app-install --activate`，复用同一宿主 operation、精确启动、验收与回滚，不使用 `osascript + open` 临时脚本。裸 `app-install` 检测到默认目标正在运行时拒绝覆盖。

## macOS 原生更新安全契约

启动契约 v1：事务 wrapper 是存活的 PID/PGID/SID 锚点；原生壳在该组与 session 内；Foundation `Process` 后端可自成 PGID，但必须是精确宿主的直接子进程且留在同一 session。验收与清理共用此定义；清理跨组后代须绑定已观察的出生身份，未知端口 owner 不获得终止授权。

固定 helper 契约为 2。后端从**当前运行 payload**复制 helper，持久记录协议与 SHA-256，helper 自验后才继续。候选包里有修复不代表执行中的更新器已有修复，首次迁移见 [发布文档](release.md)。

状态边界：

1. **准备**：验证旧宿主的版本、生命周期、监听者、父子与 session 身份。候选 `.app` 必须声明 `update-launch-contract=1`；用候选二进制的无界面入口实际调用生产 Swift 启动原语，读取精确子进程回执并完整清理。自检仅启动 `/bin/sleep`，不启动后端、不占业务端口、不申请权限。失败发生在 ready 前，不进入回滚，也不停止旧宿主。
2. **授权退出**：发布 ready 前持久记录 `switching/acceptance_unknown`。ready 写入异常也视为可能已发布，不因超时自行放行；旧宿主暂时仍健康不等于退出授权已撤销，不能据此报回滚成功。宿主准备超时后先撤销 ready 接收权限，TERM/KILL 精确 helper；确认退出并删除迟到 ready 后才能报告确定失败。
3. **切换与验收**：全程防睡眠，使用 awake uptime。候选和回滚均满足启动契约、版本及精确生命周期身份，并通过稳定窗口；回滚不降低安全校验。旧版验收使用切换前捕获的 payload 身份，不把可能滞后的开发版标记文件当运行身份。
4. **移交与提交**：release 前保存 `verifying + pending_protocol=2` 和完整 boot/host instance/generation/PID 身份；保留旧 reader 认识的封闸状态，避免回滚到旧 payload 时按旧 `accepted_pending_commit` 规则提前放行；release 完成后才持久写 `state.json.commit` 移交凭据。正常路径由 helper 写终态。helper 崩溃后，后端须同时匹配版本、精确身份、operation 与移交凭据，确认 helper 不存活，取得生命周期锁并重读状态，才能归并。只有版本相同不得放行。

release 后、移交凭据持久化前崩溃仍保持未知；不猜测成功、不自动重放。这是保守的恢复边界，不承诺掉电后自动恢复。普通检查、下载或新开发壳请求不得覆盖未知事务。探针健康不是清除封闸的充分条件。

改动此契约须运行 `test_self_update.py`、`test_self_update_transaction.py`、`test_self_update_native_contract.py` 和 `test_backend_supervisor_native.py`。真实 Foundation 拓扑测试在 macOS 标准门禁中执行，不是可选引擎慢测；非 macOS 跳过不能作为原生更新发布证据。还须验证 Swift 双架构链接及实际候选二进制的无界面自检。

## 验收

accepted 只表示接管。验收新 `boot_id`、原生宿主 launch generation、运行版本、生命周期 running 和原会话状态；不能以 CLI 退出码代替验收，也不能承诺中断回合自动继续执行。

依据：[生命周期](../desktron/lib/lifecycle.py)、[macOS 原生宿主](../desktop/native/backend_supervisor.swift)、[Windows Supervisor](../desktop/windows/src/supervisor.rs)、[systemd helper](../desktron/lib/systemd_restart.py)、[重启回归](../desktron/tests/test_lifecycle_restart.py)。
