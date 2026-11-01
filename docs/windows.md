# Windows 版

本文说明 Desktron Windows 版的结构、构建、安装和验收方法。macOS 的行为契约不变；Windows 版沿用同一套后端、前端和宿主控制协议，只替换与系统绑定的部分。

当前状态：Windows 真机已验证启动、会话创建、Pi SDK 初始化、模型目录、代理及一次实际模型对话。此单机证据不等同于最终发行包在干净环境的完整用户场景验收；浏览器、原生交互、升级与冷启动仍按第 6 节逐项验收。

## 1. 结构

| 层 | macOS | Windows |
| --- | --- | --- |
| 原生壳 | `desktop/native`（Swift、WKWebView） | `desktop/windows`（Rust、tao + wry、WebView2） |
| 启动器 | `desktron/bin/desktron`（bash） | `desktron/lib/desk_launcher.py`，入口 `desktron/bin/desktron.cmd` |
| 平台差异 | 直接调用 POSIX 接口 | `desktron/lib/oscompat.py` 在进程启动时补齐 |
| 终端 | `pty.fork` | ConPTY（`pywinpty`），缓冲与回放逻辑共用 |
| 安装 | `install.sh` | `scripts/windows/install.ps1` |
| 发行打包 | `scripts/dev/release.sh` | `scripts/dev/release_windows.py` |

目录布局与 macOS 相同，根在 `%USERPROFILE%\.local\share\desktron`；原生壳装在 `%LOCALAPPDATA%\Programs\Desktron`，WebView2 数据在 `%LOCALAPPDATA%\Desktron\WebView2`。

## 2. 平台兼容层

`oscompat.py` 在 macOS 与 Linux 上只做透传，`install()` 不改变任何行为。在 Windows 上，它于模块导入时补齐以下差异：

- `fcntl.flock` 以 `LockFileEx` 实现，锁定 `0x7FFFFFFF_00000000` 处的 1 字节，不妨碍正文读写；原生壳的生命周期锁使用同一范围。
- `os.open` 自动加 `O_BINARY`；`os.replace`、`os.rename` 在目标被短暂占用时有限重试。
- `O_NOFOLLOW` 在 Windows CRT 中不存在，兼容层以 CRT 不用的位传入 `os.open`：打开前拒绝末段为符号链接或 junction 的路径（`O_CREAT`/`O_TRUNC` 不会触及链接目标），打开后再比对路径与句柄的卷号和文件号，期间被换成链接则关闭并报错。只检查末段，与 POSIX 一致；硬链接不在拒绝范围内。普通开发机没有建符号链接的权限，相关拒绝用例会跳过，以 GitHub Windows runner 的实测为准。
- `windows_fs.py` 在文件系统调用边界统一处理长路径：文件读写、状态查询、枚举、创建、移动与删除超过阈值时使用扩展路径，不要求开启系统长路径策略。UNC、字节路径与文件描述符保留各自语义；相对路径先规范化，符号链接的相对目标不改写。枚举返回调用方的路径形式，不把扩展前缀写入会话身份、配置或子进程参数。真实缺失、权限和共享冲突仍报错；macOS/Linux 不安装这些适配。
- Shepherd 常驻目录清理同时核对 HOME 及实际删除的父目录严格位于统一存储之下，并拒绝链接祖先。长路径适配不放宽归属检查、不删除正式历史，也不自动重发失败请求。`test_windows_long_paths.py` 在真实 Windows 临时目录验证两次重建、短父目录／超长文件、读写枚举移动删除和 junction 隔离；Windows 离线交付门禁要求这些场景实际通过，跳过或移除用例不能放行。
- JSON 快照写入使用 `oscompat.fsync_directory`：POSIX 继续同步目录，Windows 不通过 CRT 打开目录（会报 `PermissionError`）。两端都保留文件 `fsync` 与原子替换，文件同步/替换错误仍失败；Windows 不声明等同 POSIX 目录同步的断电持久性保证。
- `os.kill(pid, 0)` 只检查存活。Windows 上信号 0 是 `CTRL_C_EVENT`，不替换会中断目标进程组。
- `start_new_session=True` 的子进程进入独立 Job Object；`os.killpg` 结束整个作业，没有登记作业时按进程树结束。
- 缺少权限创建符号链接时，目录退回 junction，文件退回硬链接或复制；`os.path.islink` 与 `os.unlink` 同样识别 junction。
- 权限位检查改用 `oscompat.loose_mode()`。Windows 用 ACL 管理权限，用户目录默认私有。
- Shepherd 配置/迁移 JSON、Codex 协议 schema、Pi 子代理证据与侧栏投影、Bot 接入进度及原生 JSON 协议管道显式使用 UTF-8，不依赖中文 Windows 的 GBK 默认值。历史读取子进程单独固定 JSON 标准流；Pi 项目记忆以字节管道收发后在调用线程严格解码，避免 Windows 的后台读线程只报告解码警告。常规引擎 recap 及内容整理管道也声明 UTF-8，不改变系统代码页。
- Pi SDK 跟随实际 CLI 定位：支持 npm `.cmd` 的脚本目标，以及官方安装器 `bin/pi-launcher.js` 对应的 `install/current-version`；不猜测或回退到其他 release。`%~dp0pi-launcher.js` 直接经 Node 启动，避免批处理改写参数。两阶段 session factory 按能力在 `core/sdk.js` 或 `core/agent-session-services.js` 中解析，存储桥只导入 SessionManager，避免无关 UI 入口阻塞。
- Shepherd 直接使用 Pi SDK 时也调用 Pi 原生 HTTP dispatcher 初始化：启动时先启用环境代理，加载全局设置后再应用 `httpProxy` 和 `httpIdleTimeoutMs`。沿用 Pi 的环境变量优先级及 `NO_PROXY`，不修改凭证、系统代理或 TLS 校验；SDK 能力验收也检查 HTTP 初始化接口。
- Codex app-server 的 stdout 在 Windows 上由后台字节读取线程和有界队列承接，不对普通管道调用只支持 socket 的 `select()`；JSON-LF 分帧、严格 UTF-8、读取超时及不完整 EOF 的失败语义不变。POSIX 保留 selector 路径。目录刷新失败只记录异常类型，不输出原生 stderr 或凭证；Pi OpenAI 模型仍与 Codex 可见目录取交集。
- Shepherd 输入快照和来源回执在 Windows 上使用身份摘要作为文件名，回合身份与验证字段保持不变，旧合法文件名仍可读取；完成回合的 scratch 在持有 `.gc` 锁并核对身份后，先释放阻止删除的 lease 句柄再清理。

用到上述接口的模块必须导入 `oscompat`，`test_windows_port.py` 会检查这一点。

其他需要显式分支的地方：父进程看护改为等待进程句柄与命名停止事件；App Control 的本地通道从 Unix Socket 改为只绑定 127.0.0.1 的专用端口，对端 PID 取自内核 TCP 连接表；Shepherd 的进程身份改用进程创建时间；目录句柄读取（旧 Codex 历史、图片变体）改为按路径读取并拒绝链接。

## 3. 原生壳

`desktop/windows` 与 macOS 壳逐项对应：

- **Supervisor**：`supervisor.rs` 移植 `backend_supervisor.swift` 的状态机，包括 generation、四元探活、退避、端口占用者等待和重启请求协议。后端放在 `KILL_ON_JOB_CLOSE` 作业中，宿主异常退出时整棵进程树随之结束；正常停止经命名事件通知后端，4 秒后结束作业。
- **前端桥**：注入脚本提供 `window.webkit.messageHandlers.<name>`，前端不区分平台。宿主只接受 `http://127.0.0.1:7765` 主框架发出的消息。
- **内置浏览器**：每个标签是一个子 WebView2，支持隔离（InPrivate）标签、主框架域名白名单、外部协议交给系统、飞书 applink 改写、下载保存对话框与 15 分钟空闲回收。悬浮标签条是按圆形区域裁剪的子窗口。
- **App Control**：页面脚本在 DevTools 协议创建的隔离世界执行；点击、拖拽与按键经 `Input.dispatch*` 送达，页面收到 `isTrusted=true` 的事件；截图用 `Page.captureScreenshot`。
- **公开搜索与代理**：WebView2 公网页面沿用系统代理，不再全局强制 `--no-proxy-server`；本地工作台依赖 Chromium 默认的 loopback 绕过。若系统配置显式取消了 loopback 绕过，需要独立验收本机访问。Windows 的页面安定探测排除初始 `about:blank`，同时比较地址与节点数，超时仍标记 `settled=false`，不伪装成正常搜索结果。CLI 和 MCP 均使用注入的专用回环通道，保留 Runtime 的进程身份校验。
- **页面脚本**：App Control、飞书与网络空闲脚本由 `scripts/dev/extract_native_scripts.py` 从 `main.swift` 提取到 `desktop/shared/js`，两端执行同一份。修改 Swift 中的脚本后需重新运行该命令，测试会检查两边一致。

快捷键：前端按 macOS 口径处理 ⌘ 系应用快捷键，Ctrl 系留给 shell。Windows 壳把 `Ctrl+Shift+X` 转为 ⌘X，`Ctrl+Shift+Alt+X` 转为 ⌘⇧X；页面未处理时原始按键照常交给 WebView2。

## 4. 已知差异

| 项目 | 状态 |
| --- | --- |
| 应用内更新 | 未接入。Windows 壳以 `update_launcher_unavailable` 拒绝更新请求；更新方式为运行新版安装包。 |
| 原生应用观察（L1） | 内置清单只有 macOS 应用。Windows 壳上报 `unsupported`，设置页不显示该区块。 |
| 窗口外观 | 源码采用右侧双顶栏、单侧栏通顶；Windows 11 22H2 及以上通过系统 Desktop Acrylic 与透明主 WebView2 呈现半透明侧栏。API 失败显式记录并回退纯色；系统可因关闭透明效果、节电或失焦自行降低材质效果。安装端实际观感仍需验收。 |
| JS `prompt()` | 使用 WebView2 默认对话框。 |
| 系统通知 | 只用于更新终态，当前不会触发；实现以 PowerShell 应用身份发送。 |
| 隐藏标签截图 | WebView2 隐藏时可能不渲染，截图结果可能为空白；检查与动作不受影响。 |

### 双顶栏实现与验收边界

`src/chrome.rs` 用独立不透明 caption 子 HWND 绘制主区的 32 逻辑像素窗口标题栏；去掉 tao 保留的系统标题样式但保留缩放、工作区最大化与系统消息链。阴影及 Snap Layouts 仍需实际验收。主 WebView 按窗口区域裁剪，仅为右侧标题区让路；八向缩放由独立原生输入层承载。最大化按钮返回 `HTMAXBUTTON`，关闭经 `SC_CLOSE` 进入原有回合保护。窗口行为不能由坐标单测代替真机验收。

`js/window-chrome.js` 只在工作台主框架启用能力标记并上报主区左缘；`frontend/window-chrome.css` 只对该标记生效。侧栏缩放、开合和终端全屏改变标题区左缘，macOS 与旧 Windows 壳不启用新布局。错误显式记录，不静默退回跨窗系统标题栏。完整设计见 [Windows 双顶栏](windows-chrome-design.md)。

### 侧栏原生材质

`src/backdrop.rs` 使用公开 `DwmSetWindowAttribute(DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_TRANSIENTWINDOW)`，将原生材质扩展至客户区；主窗口支持透明，主 WebView2 的背景仅使用全透明或不透明。CSS 的侧栏半透明配色保持共用，聊天主区继续不透明，macOS 实现不变。主题切换同时更新系统深浅外观与 WebView2 背景，不再强制写回不透明颜色。

材质模式下，父窗口擦除和绘制清理材质底图，排除当前不透明 caption，但仍清理透明 WebView 下方及旧标题范围。标题使用 32 位 BGRA DIB 并在 GDI 绘制后明确设置 alpha=255，避免 RGB 正确但最终被混成灰白色。最大化／还原不能重新启用系统标题绘制。普通/最大化均接管全客户区几何，消除默认 NC 灰边及标题两侧亮边；不额外铺整宽 SM_CYFRAME 色条。独立原生 child 输入层只覆盖 DPI 八向缩放热区，使用 layered alpha=1（0 会丢失鼠标命中），不绘制可见边条。它返回原生 resize 命中，将缩放启动与边缘双击/右键转交顶层，光标交给系统；最大化时隐藏。尺寸/DPI 消息同步更新热区，生产 browser/tag 事件后恢复边缘输入叠层，销毁时清空所持 HWND。Windows 11 继续设置 DWM border none（失败记录），保留 WS_THICKFRAME、阴影及系统消息链。最大化先保留 tao 跟踪尺寸约束，再将 OUTER 位置与尺寸限定为显示器有效工作区，自动隐藏任务栏按该显示器边缘预留一像素唤出带。材质状态与 WebView 回退同步，尺寸更新失败记录并停止更新裁剪区。HTML 主区仍不透明。

原生壳以 `__deskWindowsBackdrop` 报告请求模式；兼容 `messageHandlers` 桥本身不证明材质可用。API 失败写入 `native-host.log`，撤销材质及 frame 扩展，页面启用 `no-vibrancy`；回退失败也单独记录，不修改用户的 Windows 透明效果或节电设置。`acrylic_requested` 仅表示原生请求成功，不保证当前系统策略一定呈现模糊。

回归见 `test_windows_port.py` 与 Rust `backdrop::tests`。显式执行 `cargo test --locked isolated_acrylic_webview_theme_and_layout -- --ignored --test-threads=1` 会打开合成窗口，使用独立 WebView2 profile，复用生产 `Chrome` 的绘制、命中和裁剪，检查原生材质属性、主题切换、透明背景、最大化／还原、移动／尺寸变化、侧栏开合坐标及纯色回退／恢复。重复最大化／还原至少 10 次并检查标题深浅色、侧栏及主区像素。PrintWindow 在样式切换时可能返回默认标题打印缓存，父 HWND 的 GetDC 不含 WebView2 合成子层；因此测试仅在像素截取阶段将自建 HWND 置于 TOPMOST，以 ClientToScreen 定位，复制该客户区的实际呈现并断言主区包含 WebView。工作区几何检查使用非置顶窗口，同时核对 GetWindowRect、客户区屏幕坐标和 DWM 可见外框；交替覆盖 tao API 与生产 SC_MAXIMIZE/SC_RESTORE，并检查八向命中消息、WindowFromPoint 选出的真实目标、内部控件与最大化按钮不被输入层遮挡、新 child 置顶后的边缘叠层及顶层先销毁顺序。深浅主题均采样 DWM 可见外框左右下、标题两侧与顶沿：不透明区域按主题底色小容差比较，透明侧栏与相邻像素比较，不再以 <150 放过灰边。普通窗口也断言客户区与外窗无 NC inset。不会截取整个桌面，不启动业务后端或读取真实会话。可设置 `DESK_BACKDROP_TEST_OUTPUT` 保存该合成窗口的 BMP。此测试不是当前安装端的视觉验收，也不证明不同显示器 DPI 或所有系统材质策略正常。

### 统一网络代理

`network_proxy.py` 校验并保存本机 `~/.config/desktron/network.json`；启动器在读取旧 env 后应用冻结的策略，`credstore.secrets_env` 不允许旧代理变量覆盖它，Pi SDK 在已管理的模式下不再采用私有 `httpProxy`。固定命令终端在附着启动后通过 `network-proxy.ps1` 统一 PowerShell 5 的 WebRequest 与 PowerShell 7 的 HttpClient 代理，winget 显式传入代理选项；已管理模式禁用 Node 下载的旧备用代理。未保存配置时保持兼容。

原生壳在创建首个 WebView2 前通过托管 Python 的 `network_proxy.py --native` 解析同一配置，主工作台、普通与隔离标签、悬浮标签条共用同一组启动参数。系统固定代理也解析为显式参数，避免 WebView2 与引擎采用不同规则；PAC 等不受支持策略拒绝保存或启动，不回退直连。仅新增源码尚不代表已安装；启用新壳前必须先同步包含该模块的 payload，不能只替换壳。

`/workbench/network-proxy` 的 GET／PUT 与 `/test` 的 POST 限本机同源，并排除 Tailscale Serve。测试只验证代理 TCP，网页与模型保持 `not_run`。完整退出并重新打开才能统一生效；仅后端重启不足。回归见 `test_network_proxy.py`、`test_network_proxy_ui.py`，用户说明见[网络与代理](../desk-docs/network-proxy.md)。

### 局域网手机连接

设置页通过本机同源 `/workbench/lan` GET／PUT 管理私人 `~/.config/desktron/lan.json`。只接受 RFC1918 IPv4/前缀并验证地址属于本机；Windows 候选网卡用 PowerShell 读取，不回传网卡名称或主机身份。环境变量 `DESKTRON_REMOTE_BIND` 优先且使面板只读。保存使用 revision CAS，提交后失败报告结果未知，不自动重放。

`lan_settings.load_runtime()` 和 `remote_auth.load_runtime_token()` 在创建主监听前固定来源网段和有效令牌；保存、停用或切换 Tailscale 不会热扩大网段、撤销鉴权或使已有 LAN Cookie 意外换令牌。新配置重启后生效，读取损坏时保留文件并使默认主入口保持环回。局域网监听失败不影响本机入口，不生成虚假的局域网二维码。不自动修改防火墙。

回归见 `test_lan_settings.py`、`test_frontend_lan_settings.py`；浏览器覆盖延迟二维码加载期间停用卡片、保存冲突与未知结果、远端隐私及移动视口。手机实机和 Windows 防火墙另行验收。

### Tailscale 手机连接

「设置 → 手机连接」可在 Windows／macOS 保存专用入口启用状态与环回转发端口。`tailscale_settings.py` 用 `durable_json` 在用户目录 `~/.config/desktron/tailscale.json` 原子持久化配置与私人随机令牌，唯一 revision 做并发比较，不把令牌返回设置 API。旧 `DESKTRON_SERVE_PORT` 继续由外部管理，不自动覆盖。

`/workbench/tailscale` 的 GET／PUT 只接受本机环回、正确 Host 与同源请求，并拒绝 Tailscale 转发端口及跨站请求。保存不运行 CLI、不启动监听或重启。主入口默认仍为 loopback，专用入口继续要求令牌；显式非环回策略不能被设置页静默改写。启动时固定 Tailscale 运行策略；配置损坏或不可读会记录错误并关闭该专用入口，默认主入口仍保持环回，本机 HTTP 服务不因此停机；状态读取与保存仍明确报错，原文件保留。Windows CLI 在 PATH 缺失时检查 Program Files，JSON 使用明确 UTF-8，原生错误输出不返回网页。检测到 Serve 配置中的 Funnel 后不生成带凭据的配对链接。

使用流程见[设置与更新](../desk-docs/settings.md#tailscale-手机连接https)。回归：`test_tailscale_settings.py`、`test_tailscale_windows.py`、`test_tailscale_serve_entry.py`、`test_frontend_tailscale_settings.py`。隔离测试不代表已部署，也不证明目标 tailnet 或手机实际连接成功；本地配置文件的 Windows ACL 不能由 POSIX 模式位测试替代。

## 5. 构建与安装

- 原生壳：在 Windows 上 `cd desktop\windows && cargo build --release`。在其他系统上只能做类型检查：`cargo check --target x86_64-pc-windows-msvc`。
- 开发安装：`desktron.cmd app-install` 构建并安装到 `%LOCALAPPDATA%\Programs\Desktron`，`desktron_home` 指向源码仓；目标运行中时拒绝覆盖。
- 开发时可用 `DESKTRON_PYTHON` 指定解释器，但不能指向虚拟环境里的 `python.exe`：它会另起一个进程运行真正的解释器，宿主核对的后端 PID 因此对不上，启动会以身份不匹配失败。
- 发行包：在干净工作树、原生目标架构 Windows 及托管 Python 下运行 `python scripts\dev\release_windows.py`。先审查公共树并执行离线交付门禁，再构建原生壳和资产、生成候选 ZIP、解包核对逐文件摘要与壳/payload revision，最后对解包后的 payload 做隔离验收。全部通过后才把同一个 ZIP 放入 `dist`，附带 `.sha256` 和 `.acceptance.json`。`--allow-dirty` 仅生成带 `-experimental` 后缀且 `release_eligible=false` 的试验包，不能正式交付。
- 用户安装：解压后运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1`。安装器准备托管 Python 与依赖、同步 payload（失败回滚）、安装原生壳与开始菜单入口、把 `~\.local\bin` 写入用户 PATH、准备工作区，并执行隔离验收。

## 6. 验证

证据分四类，不能互相替代：

1. **本机（macOS）**：`cargo clippy --target x86_64-pc-windows-msvc` 无告警；`test_windows_port.py` 覆盖兼容层透传、命令解析、依赖声明、payload 边界、壳脚本同步、桥与快捷键映射、ConPTY 读线程；完整离线门禁确认 macOS 行为不变。
2. **CI**：`windows-checks` 在 Windows runner 上分别使用 `PYTHONUTF8=0/1`，准备 Pi SDK 测试基线 1.0.2（macOS 同一基线），执行完整 Windows 打包入口并上传 ZIP、摘要和验收证据。`windows_release_gate.py` 每文件使用独立 pytest 进程；Pi SDK/本地代理/协议冷启动、Codex 管道、GBK 协议及 Windows 原生测试为必测项，空收集、跳过、deselect、xfail/xpass、线程异常和清理失败均拒绝放行。关键场景还按名称和参数数量检查，删除用例不能静默移除保证。Pi 协议探测采用独立的 45 秒本地预算（不是模型/API 超时），慢加载用例实际延迟模块导入超过旧的 15 秒限制后仍须通过完整合同；卡死仍须超时并清理本次拥有的子进程，不按版本号缓存或跳过能力检查。探测阶段只记录固定阶段名和整数耗时，不记录原生 stderr、配置或提示词；Windows 的 HOME、USERPROFILE、AppData 和临时目录均隔离。同一 CI 矩阵还执行 `refactor-gate.sh full` 全量离线回归，托管 runner 明显慢于本机，单文件超时经 `DESKTRON_TEST_TIMEOUT` 放宽到 300 秒（本地默认仍为 120 秒）；新增 UTF-8 文件与管道合同为专项必测项。通用回归允许 POSIX 专属用例跳过；Windows 不执行 POSIX 信号中断用例，也不执行需独立授权的真实引擎/桌面测试。
3. **真机部署**：安装后 `install-check.json` 为通过；验收在空白沙箱内为 Claude、Codex、Pi 各创建两段空会话，并核对索引与备份（`session_creation_offline=passed`），并关闭 Python UTF-8 模式验证三引擎 Shepherd Runner 的离线初始化（`shepherd_initialization_offline=passed`），不发送模型请求；正式应用 `/desk-probe` 的 `supervised` 为 `true`。
4. **用户验收**：在干净 Windows 虚拟机或新用户账户安装候选 ZIP，不借用开发终端的 PATH、代理环境或凭证。从开始菜单启动，完全退出原生壳再重开，确认 Supervisor、会话与持久代理配置正常；验证升级保留数据及失败回滚。联网需明确授权且只用合成提示，不自动重发失败消息。再依次完成：三引擎各一轮；终端输入、改尺寸与退出；浏览器打开、切换、关闭标签与下载；一次 Agent 点击和输入；设置页重启并恢复；退出确认。人工证据绑定同一 ZIP 的 SHA-256，不包含凭证或会话正文。离线报告必须保留 installer/native cold start/model access 的未验证状态，不能以 `ok=true` 替代这些证据。

### 一次性 Windows 安装与冷启动验收

新增 `scripts/dev/windows_vm_acceptance.py`，**只能在一次性 Windows 虚拟机或新测试账户运行**。必须显式传 `--disposable-profile`，表示同意在该测试账户安装、关闭、重新打开和请求原生重启；不是对当前工作机的授权。工具会拒绝已有 Desk/引擎登录目录、产品/引擎环境覆盖、既有引擎 CLI、占用的 7765 端口，以及与 Windows 真实账户不一致的伪造 USERPROFILE。

```powershell
python scripts/dev/windows_vm_acceptance.py `
  --archive C:\candidate\desktron-windows-x64.zip `
  --sha256 $TrustedZipSha256 `
  --report "$env:TEMP/windows-vm-acceptance.json" `
  --disposable-profile
```

报告路径必须是新的 JSON 文件，不能覆盖产品目录、凭证或旧证据。安装器可以下载公开运行时依赖；产品使用自行安装的托管 Python，不沿用验收调度器的 Python。冷启动从实际开始菜单快捷方式发起，只继承持久的用户/机器 PATH 和代理，不继承调试终端的临时代理或 API key。

验证项包括：包摘要和逐文件完整性、安装后的真实文件及离线报告、原生窗口和 Supervisor 四元身份、托管 Python 后端、各引擎两段合成空会话及备份、完全退出后重开、会话保留、原生重启 completed 回执、新 boot/generation、同一 ZIP 重装与端口释放。关闭只给精确 PID/创建时间/路径一致的本次宿主发送窗口关闭请求，先要求 idle；重启只走安装后的 `restart-desk --detach --yes`，并用预期 boot/host 身份锁定目标。未知退出或重启结果不重试、不强杀；报告标记可能仍有测试宿主，交由测试 VM 操作者核验。

`.github/workflows/windows-vm-acceptance.yml` 提供独立的 **workflow_dispatch** 入口：输入受信的 Offline checks run ID 和独立取得的 ZIP SHA-256，在一次性 Windows hosted runner 下载候选包并验收，仅上传脱敏报告，不上传原生日志、控制请求或会话文件。它不会随 push/PR 自动运行，也不会发布包；不要选择未经审查的 PR 产物。

这项检查证明原生窗口创建与后端链路，不证明画面正确、浏览器/Agent 交互、跨版本升级回滚或模型网络可用。`model_requests=not_run`、`model_access=not_run`、`cross_version_rollback=not_run` 必须保留，后续仍需独立授权与验收。

单独运行离线门禁（不会安装、同步或重启当前应用）：

```powershell
python scripts/dev/windows_release_gate.py --report "$env:TEMP/windows-delivery-gate.json"
```
