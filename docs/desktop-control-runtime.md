# macOS Desktop Control Runtime

本文定义 Desk 对 macOS 原生应用的 provider-neutral 控制边界。第一切片提供协议、安全数据模型和 Accessibility 只读状态探针，第二切片开放**人选定窗口后的 L1 只读观察**。两者都**不申请系统权限、不执行任何原生写动作、不合成输入**。

## 复用 App Control，而非另建控制面

Desktop Control 是现有 App Control Runtime 的原生应用 adapter 扩展。模型提供方只接触统一的观察、动作和回执语义；lease、`observation_version`、locator、幂等、审批、用户抢占、operation/receipt 和后置条件仍由 App Control 统一治理。Swift bridge、MCP 和模型进程都不能自行作权限判断或绕过 Runtime。

浏览器继续使用 WKWebView adapter；Desk 自身继续使用受保护的 navigation surface adapter。Desktop Control 不创建可控制任意应用的通用 `Desktop` app。每个原生应用必须有独立 `app_id` 和 manifest：

```json
{
  "native_application": {
    "bundle_id": "com.example.Application",
    "target_binding": ["bundle_id", "pid", "window_id"],
    "code_requirement": "anchor apple generic and identifier \"com.example.Application\"",
    "projector": "ax-structural/v1",
    "allowed_roles": ["AXWindow", "AXCheckBox"],
    "allowed_semantics": ["window", "toggle"],
    "allowed_values": ["on", "off"],
    "label_values": {"Connected": "status_connected"}
  }
}
```

`bundle_id` 不允许为 `*`；资源 scope 只能是 `delegated-application-window`。`code_requirement` 必须包含同一 bundle 的 `identifier` 子句，宿主用它对运行中进程做代码签名校验，bundle ID 本身可仿冒，不单独作为身份。`label_values` 只能把固定界面标签映射到 `allowed_values`。L1 manifest 不得声明动作；原生观察必须是无参数的 `desk.native-window/v1`。

原生 manifest 只从发行版 `frontend/workbench.json` 的 `nativeApps` 读取。运行态 `.workbench/apps.json` 即使声明 `native_application` 也会被拒绝，避免借宿主的全局 Accessibility 权限扩展到任意应用。当前内置 `shadowrocket`（L1、无动作），签名要求固定为 App Store 分发证书加 `identifier "com.liguangming.Shadowrocket"`。

## 原生宿主权限边界

Accessibility / Input Monitoring 等系统权限只能由 Desk 原生宿主持有。Python Runtime、Agent、Provider CLI 及其子进程不得直接取得全局桌面权限。

原生宿主只调用无提示的 `AXIsProcessTrusted()` 判断信任状态，并在以下时机上报：

- 后端进入 healthy 后；
- Desk 再次成为 active application 时；
- 相同状态至少间隔 10 秒，避免高频轮询；
- 存在原生目标时每 10 秒强制重报一次，维持 30 秒有效期。

禁止调用带授权提示的查询、打开系统设置、修改 TCC、调用 AX 写接口或注入输入。AX 读取只发生在下文 L1 链路中、且只针对人选定的那个窗口。宿主通过既有 `X-Desk-App-Control-Host` 令牌调用：

```text
POST /workbench/app-control/host/desktop-capability
GET  /workbench/app-control/desktop/status
```

Runtime 只接受固定 Desk bundle identity，并在受原生壳拉起时校验启动时绑定的宿主 instance ID 与父进程 PID；这些字段不能替代 host bearer 鉴权。原生 bundle identity 缺失时上报直接停止，不回退自称。状态只使用：

- `trusted`：当前宿主进程受 Accessibility 信任；
- `not_trusted`：当前宿主进程不受信任，不能推断是未请求、拒绝或撤销；
- `not_reported`：当前后端尚未收到有效宿主探针。

上报同时携带 `monitoring_status`（`ready`/`unavailable`），表示宿主是否已安装人介入监测。`execution.status=ready` 需要同时满足：宿主状态 30 秒内有效、`trusted`、`monitoring_status=ready`。否则原因分别为 `host_status_stale`、`accessibility_not_trusted`、`user_intervention_monitor_unavailable`。未受信任时监测状态一律按 `unavailable` 记录。不得静默退化到 `osascript`、猜测 URL Scheme、OCR/坐标点击或给 Agent 子进程授权。

## 预授权能力票据

预授权 capability ticket 与单 operation 的人工 approval ticket 是不同凭据：

- approval ticket 只批准一条已经存在的 operation；
- capability ticket 在有限时间内授权某会话针对某个原生目标使用一组动作，但本身不创建、不批准、不派发 operation。

能力票据 claims 至少包括：

```text
app_id
bundle_id
pid
window_id
allowed_actions[]
session_id
task_id
issued_by
created_at
expires_at
```

签发端点仅限原生宿主：

```text
POST /workbench/app-control/host/desktop-tickets
```

安全约束：

- `bundle_id` 必须与该 `app_id` 的 manifest 完全一致；
- `allowed_actions` 必须是该应用声明动作的非空子集；
- `pid`、`window_id`、`session_id`、`task_id` 均必填；
- TTL 最大 900 秒；
- bearer 使用 `dcap_` 独立前缀，只在签发回执出现一次；SQLite 只保存 SHA-256；
- 校验时所有 claims 和动作必须精确匹配；过期立即拒绝；
- 第一切片没有“用票据创建动作”的 HTTP 路由；带 `native_application` 的 manifest 也被现有 `/open`、委托、认领、观察、动作和宿主取活路径统一拒绝，因此签票不会进入 lease 或 operation 队列。Runtime 重启会撤销仍 active 的票据。

后续动作执行仍须同时校验 active lease、能力票据、当前 `{bundle_id,pid,window_id}`、`observation_version`、动作 schema 和审批策略。任一变化都必须停止并重新观察。

## L1 只读观察链路

### 建立目标

1. 用户在 Desk 设置“调试”区打开某原生应用的观察开关，前端经 `nativeControl` 桥请求原生壳。
2. 原生壳安装人介入监测并重报能力，再列出该 bundle 且通过代码签名要求的进程窗口（CGWindowID）。窗口标题只在本机选择框显示，不发给 Runtime 或 Agent。
3. 用户选定后，宿主调用 `POST /host/desktop-target`，提交 `bundle_id`、`code_requirement`、`pid`、进程启动时间令牌、`window_id`、宿主 instance/pid 和会话。Runtime 校验 manifest、宿主就绪和身份后创建 `native_delegated` lease（仅限该会话认领）和 `desktop_targets` 记录，生成新的 `target_generation`，`interaction_epoch=0`。同一应用再次选择会撤销旧目标。

Agent 看不到窗口或进程列表，只能 `claim` 已绑定到本会话的目标。

### 观察与回执

- Agent 走通用 `claim/inspect`，观察名为 manifest 声明的 `window`，不接受任何参数，只读、不需审批。`act` 一律拒绝。
- 宿主经 `GET /host/desktop-next` 取活，拿到完整目标绑定；每次读取前后都复核 bundle、签名要求、pid、启动令牌和窗口是否存在，前后任一不符即以枚举错误回报。
- 宿主经 `POST /host/desktop-result` 回执，必须携带 `dispatch_id`、宿主 instance、`target_generation` 与派发时的 `interaction_epoch`。通用 `/host/result` 对原生动作返回 409。
- Runtime 对输出做白名单投影：顶层只允许 `nodes`、`truncated`；节点只允许 `ref`、`parent`、`role`、`semantic`、`value`、`enabled`、`selected`；`role`/`semantic`/`value` 必须在 manifest 白名单内；`parent` 必须指向已出现节点；最多 200 个节点。任何偏差都整份拒绝为 `native_observation_rejected`，不做部分截取。
- 宿主失败原因只接受固定枚举；自由文本统一记为 `native_observation_failed`，避免夹带 AX 原文。

宿主投影器 `ax-structural/v1` 只把白名单角色映射为语义节点，开关取 `on/off`，其余文字只有与 `label_values` 的键完全相等时才转成令牌。安全文本框、文本输入、Web 区域整棵跳过；深度 16、节点 200、读取时限 5 秒，超出即标 `truncated`。

### 人介入、漂移与失效

| 事件 | Runtime 处理 |
| --- | --- |
| 用户点击、按键或滚动目标窗口 | `interaction_epoch+1`，lease 暂停，取消在途读取；迟到回执因 epoch 不符被拒 |
| 用户空闲 3 秒 | lease 恢复；需重新 inspect |
| 窗口关闭、进程退出、身份变化、宿主释放 | 目标失效并撤销 lease |
| 宿主 instance 变化、失去信任或监测 | 撤销该宿主的全部目标 |
| manifest 指纹变化、lease 过期、Runtime 重启 | 目标失效；重启后不重放任何读取 |

过期的 `target_generation` 或其他宿主的事件返回 409，不影响新目标。

## 恢复与验收语义

未知副作用不得自动重放。Runtime 重启时，已有 App Control 规则保持不变：只读后置条件探测可恢复；执行中的写动作落为失败/不确定，不能因没有成功回执而再次点击。

后续完整 adapter 必须分别证明：

1. action delivered；
2. 应用状态已更新；
3. 目标节点或配置可用；
4. 业务场景验收通过。

这些状态不能互相替代。截图、AX 文本、剪贴板、订阅 URL、节点凭证、密码和验证码默认不得进入模型上下文；观察层必须先做结构化 allowlist 和 redaction。

## 当前未实现

没有任何原生写动作：不点击、不输入、不更新订阅、不测试或切换节点、不重连，也没有网络连通性验收。`label_values` 中的 Shadowrocket 标签来自常见界面文字，尚未在真实 AX 树上核对；真实窗口观察取决于当前安装宿主的 Accessibility 授权与目标窗口选择状态，须在实际安装端另行验收，不能把某台开发机的权限状态当作产品固定状态。源码实现、专项测试通过、安装部署和真实用户场景可用是四种不同证据。