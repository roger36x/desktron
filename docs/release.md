# 安装、更新、同步与发布

源码提交、发行构建、运行目录同步与重启是四个不同步骤。每步记录对应版本和结果，不能用其中一步代替其余验收。

## 安装与更新

发行产物包含 payload、安装脚本和桌面壳。Python 最低版本为 3.11；发行安装始终准备 Desk 托管的原生 Python 3.12.14（独立构建标签 `20260814`），不沿用系统解释器。安装器和启动器共用 `provision/python_runtime.sh`，校验下载 SHA-256、版本与架构，并准备固定的 PyYAML 6.0.3。Node 复用前也检查 `process.arch`，优先使用合格的托管版本，不接受仅版本合格的模拟架构二进制。

构建器依据 [payload-excludes.txt](../scripts/payload-excludes.txt)排除工作区数据、测试和开发文档，保留 `desk-docs/`。用户通过发行安装入口准备环境。

安装在打开 `.app` 前执行 `desktron install-check`，失败返回非零，不宣称安装完成。结果原子保存至产品数据根下的 `install-check.json`。验收在空白临时 HOME、数据和工作区内启动真实后端，使用随机环回端口请求 `/desk-probe` 并核对 PID 与 `boot_id`，再检查前端入口和 `shepherd status --offline`。该状态命令只读取连接器声明，不启动真实引擎协议探测。环境不继承凭证或引擎覆盖项，停用预热、Bot、安排、记忆调度及自更新；只清理本次启动的子进程。

这项离线验收不证明正式原生 Supervisor、现有登录、模型调用或浏览器交互可用。报告明确保留 `model_requests=not_run`；真实引擎请求需另获授权，不随安装或离线门禁偷偷执行。

[self_update.py](../desktron/lib/self_update.py)处理检查、下载与状态。发行版安装时，后端先关闭新准入，再把更新 operation 交给当前原生宿主；宿主启动固定位置的更新器，收到准备完成回执后停止自己的精确后端并退出。[apply](../desktron/lib/self_update_apply.py)原子替换 payload 与 `.app`，启动并核对新版本，失败时恢复两者。它不按名称扫描进程，也不终止非本事务启动的宿主。

发行构建把同一 `rev` 与 `build_number` 写入 payload 和 `.app`。运行后端同时报告完整宿主 revision 与载荷 revision，便于识别只更新了一侧的异常。macOS `app-install` 在提交更新事务前必须用稳定证书签署 `io.github.desktron`，并验证签名元数据含证书授权链、不是 ad-hoc，且指定要求基于证书、不含 `cdhash`；优先使用唯一 Developer ID Application；不存在时复用或创建一次专用 Desktron 自签身份。可用 `DESKTRON_CODESIGN_IDENTITY` 在多身份机器上显式选择。此签名用于稳定 TCC 身份，不代替发行公证。

发布包安装器在写入 payload 前验证并复制复验候选 App；已有稳定安装时要求 designated requirement 完全一致。旧 ad-hoc／未签名安装仅在尚无 `.codesign-requirement` 时允许迁移；首次成功安装或更新把稳定要求原子写入并同步到磁盘，此后当前 App 即使被替换为 ad-hoc 也会失败关闭，必须走人工身份恢复。签名元数据读取失败、证书签名严格校验失败或 bundle id 不符不能伪装成旧版迁移。payload、App、版本标记和签名身份标记在 `prepare`、`install-check` 成功前属于同一回滚事务；最终归档解包后再次验证 App 签名，才可声明 `signed:true`。

稳定身份发生真实变化时没有自动覆盖开关。自签转 Developer ID、丢失自签私钥或更换开发团队，必须先取得用户对“既有 TCC 授权将失效并需重新授予”的明确同意，再从 Desk 外停止应用、分别核验当前与候选指定要求、保留可回退副本并人工安装候选；启动及 TCC 验收完成后才能恢复普通更新。Developer ID 证书续期若仍生成完全相同的 designated requirement，不属于身份迁移。专用本地自签私钥保存在用户钥匙串，并授权 `/usr/bin/codesign` 使用；同一用户下能使用该私钥的进程可生成相同身份，因此仅用于本机开发，不作为公开发行信任来源。

开发 checkout 显示“开发版 · 源码同步”，不下载发行包，也不把远端发行版误报成待安装更新。开发壳需要生效时，在获得重启授权后运行 `bash desktron/bin/desktron app-install --activate`；该命令构建到同卷暂存目录、完成稳定签名，再复用正式宿主更新事务，裸 `app-install` 不覆盖正在运行的默认壳。

### 原生更新契约首次迁移

`e8997b8b`／`35a5efd4` 的旧更新器错误地要求后端与壳 PGID 相同。**不能用它们执行第一次修复版激活**，也不能仅重打候选壳就认为 helper 已更新：helper 来自当前运行 payload，不是候选包。

首次迁移须单独授权，在 Desk 外退出旧应用、确认生命周期事务及端口归属后安装已验证的新壳和 payload，再重新打开；状态未知时先核对事务，不清除标志或重放。另一种引导路径必须先独立证明实际运行后端、固定 helper 摘要与原生宿主均已支持新契约，不能仅凭 `sync-home` 文件同步成功判断。当前源码更新器契约为 2，候选启动自检契约为 1，规则见 [重启协议](restart-policy.md)。

测试发布快照时，至少包括真实 Foundation 启动关系、候选拒绝后的完整清理与旧壳恢复、ready 前失败、迟到 ready、pending/release 持久失败及工作准入。隔离测试通过不能替代真实安装后在原会话发送新回合的验收。

## 源码交付

先盘点共享树和其他会话改动，完成验证，再按变更范围提交。默认本地产物与站点产物都拒绝脏工作树。发布从干净克隆或干净快照构建，避免混入本机运行数据；构建器先重建内置应用并执行 `full` 离线门禁，不能凭开发机残留资产打包。删除模块时搜索残余引用，后端启动验收应覆盖其真实导入路径。

待发布快照还需执行一次发布级门禁；它包含离线全量测试和显式标记的真实引擎原生协议测试，不应在每个开发小改动后重复运行。

```bash
bash scripts/dev/refactor-gate.sh release
bash desktron/bin/desktron release
```

该命令默认只生成本地产物。需要自行托管安装页时，传入静态托管地址：

```bash
bash desktron/bin/desktron release --site https://example.com/desktron
```

[release.sh](../scripts/dev/release.sh)会在 `dist/site/` 下生成 `release.json`、`install` 与 `index.html`，并把安装脚本的读取地址写成该托管地址。脚本不上传任何内容；上传到哪里、由谁授权，由发布者自行决定。上传后下载线上包，核对版本、架构和 SHA-256。

## Windows 离线交付门禁

Windows 使用 `python scripts/dev/release_windows.py`，必须在原生目标架构、干净快照和托管 Python 上构建。该入口先执行公共树审查及 `windows_release_gate.py`，然后构建壳、重建内置资产、写入逐文件 SHA-256 与版本清单。候选 ZIP 暂存于临时目录；解包验证后对实际 payload 执行 `install_check.py`，必须通过所有离线检查且 `model_requests=not_run`，才发布同一个 ZIP。构建期间源码变脏或 revision 改变会拒绝发布；已有同名包不会覆盖。

每个包附带 `.sha256` 和 `.acceptance.json`，包内含 `release-manifest.json`；证据记录 commit、build、架构、Python/Node/Pi SDK 测试版本、必测项执行数量和最终 payload 验收，不保存 pytest 输出、用户凭证或会话正文。摘要用于完整性和证据绑定，不代替发行签名或可信下载来源。`--allow-dirty` 只生成 `-experimental` 试验包，清单与证据明确 `release_eligible=false`。

CI 在 Windows 的 Python UTF-8 模式开/关两种配置下运行此入口。必测模块拒绝 skip、deselect、xfail/xpass、空收集、缺失关键参数场景及线程/清理错误。平台通用回归里的 POSIX 专属跳过另计；Windows 排除不适用的 POSIX 信号中断语义，不默认执行真实引擎请求或 GUI 测试。

**上述通过仅获得离线候选资格，不证明安装器、原生 GUI 冷启动或模型访问。** 正式交付前仍须在干净 Windows 账户/虚拟机安装同一个包，从开始菜单冷启动、完全退出重开、验证升级与回滚；获明确授权后用合成提示做最小模型回合，并把阶段结果绑定 ZIP 哈希。不得用开发终端偶然继承的代理或已有 SDK 代替用户安装环境。安装器会在修改运行时或 payload 前拒绝目标应用仍运行的安装；`-SkipInstallCheck` 仅用于排障，不能作为验收通过证据。

可用 `scripts/dev/windows_vm_acceptance.py` 在显式确认的一次性 Windows 账户完成真实安装、开始菜单冷启动、完全退出重开、原生重启 completed 回执及同包重装。工具拒绝已有产品/登录目录、占用端口和借用引擎 CLI；冷启动不继承调试终端的临时代理/凭证。只关闭本次精确宿主，不强杀，不重试结果未知的外部操作。`restart-desk` 新增可选 `--expected-boot-id`、`--expected-host-instance-id`，调用方身份不匹配时在提交重启前拒绝。它仍只调用原生 Supervisor，旧入口保持兼容。

独立的 `.github/workflows/windows-vm-acceptance.yml` 仅手动触发，选择已审查候选包的 CI run ID 和可信 ZIP 摘要。证据绑定同一个 ZIP SHA-256，只上传结构化报告；不上传原生日志、凭证或会话文件。通过只证明窗口/后端/安装生命周期，不代替视觉交互、跨版本回滚或联网模型验收；报告将后两者保留为 `not_run`。此工具尚未在当前工作账户执行真实安装，不能用单元测试代替 VM 运行证据。

详见 [Windows 验收清单](windows.md#6-验证)。

## 同步与生效

源码 checkout 的 `sync-home` 把产品内容同步到规范家目录；发行态运行 `sync-home` 会被拒绝。执行开发态同步前必须盘点全部共享改动并核对版本；它不等于同步凭证、Bot 声明和用户资料。

先读取 `/desk-probe`。Mac 若没有 `supervised=true`，须从 Desk 外安装新版原生壳、重新打开并验收 `supervisor=native`，不能调用旧重启器。

经用户明确同意重启后，先在源码仓根运行：

```bash
bash desktron/bin/desktron sync-home
```

再从 Desk 内调用：

```bash
"$HOME/.local/share/desktron/home/desktron/bin/restart-desk" --detach --yes
```

`--detach` 是旧调用兼容参数；CLI 只向本机后端提交一个重启 operation，不创建 launchd job、不扫描或终止进程。原生 Swift 壳是 macOS 后端唯一生命周期所有者，按精确 child 身份执行 TERM、4 秒后必要时 KILL，再用新的 launch generation 拉起并验收。Linux 上由 systemd 执行同样的退出重拉语义并配置失败限流。

手动重启不等待会话自然完成；只为短本地临界区保留最多 2 秒，外部副作用立即阻断。完整接管、Writer 恢复及未知回执规则见[重启协议](restart-policy.md)。

重启会中断当前终端与 Agent。CLI 的 accepted 只表示宿主已经持久接管；最终验收以新 `boot_id`、新 launch generation、运行版本与原会话状态为准。
