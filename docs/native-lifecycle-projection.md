# 原生生命周期投影

原生引擎提供结构化生命周期事实，Desk 负责投影、审批与故障保护，不重放历史工具。Codex 容量续跑与 [Shepherd 超窗恢复](shepherd-admission.md)各有资格，不混用。

## 终态与事件

请求或维护结束不等于主回合结束。Pi 以 `agent_settled`、Claude 以主回合 `result`、Codex 以 `turn/completed` 为终点，子代理结果、工具结束、文本停止和 `agent_end` 均不能替代。Desk 先持久化结果，再退出 live 并发布 ready。

[生命周期适配](../desktron/lib/engine_adapters/lifecycle.py)复用现有 ctx、事件和 checkpoint。

| 来源 | 事实与投影 |
| --- | --- |
| Pi auto_retry_start/end | 请求重试；end 不结束主回合 |
| Claude system.api_retry | 次数、退避与原 watchdog 预算 |
| Codex error.willRetry | 重连／恢复，由后续进展清除 |
| 三引擎压缩 | 开始、结果及可查询快照；失败不伪造成功分隔线 |

事件携带公开 sessionID、messageID、generation、scope、active。主请求与压缩／摘要重试分开保存，剩余等待由服务器计算。关闭 ctx、旧 generation 和迟到通知不得覆盖当前回合。刷新清除已消失的重试；重载把旧 checkpoint 收口为中断历史，不自动恢复执行。

Pi 原生队列只投影数量，不重新派发。压缩结果与检查点见[事件契约](shepherd-event-contract.md)。

## Codex 容量续跑

只认结构化 `serverOverloaded`，最多按 10、30、120、300 秒退避。当前尝试不得有 assistant/reasoning item、raw response、工具、服务端请求或审批。续跑时同模型、同 thread 发送空 input，不重发用户内容或推进 Desk 派发闸，新 turn ID 仍归原回执。

等待受硬截止约束，用户中断即停。任何模型输出、投影异常或 Writer 身份变化都会关闭资格。进程退出或 Desk 重启不恢复循环。

## 模型等待观察

Pi 请求开始后只建立预算与诊断；静默 60 秒后显示“等待模型进展”，工具运行状态优先。静默 900 秒升级为可恢复的“长时间无新进展”，不直接终止。明确排队和重试立即展示。

请求身份和序号拒绝迟到状态。语义进展与连接活动分开计时；重复快照、心跳不消除软停顿。Provider 明确期限、工具期限和整轮硬截止仍可终止，见[进展与超时](shepherd-execution.md)。

## 故障与派发恢复

Pi 非 CONTEXT 请求错误和 Claude synthetic API error 先暂存到原生终点，成功恢复后清除候选。Pi CONTEXT 延迟终态与历史修复保持原规则。权限、PROTOCOL、明确 prompt 拒绝和 watchdog 可立即阻断。

普通 Codex 的 idle 或 final_answer 静默只触发有界 `thread/read`。必须匹配 thread/turn、没有活动后续回合或待批工具，并取得明确终态，才能以 thread/read 为来源收口。缺失或损坏不猜成功。Shepherd 仍要求自身原生终点与来源验证，手动压缩还需 checkpoint。

常驻进程仅在用户帧尚未尝试写入时允许更换。write/flush 失败、写入后无回帧或缺原生终点均意味着可能已执行，返回 DELIVERY_UNKNOWN 或 NATIVE_TERMINAL_MISSING，不自动重发。零退出码也不证明成功。

普通 GUI 的无正文 receipt 阶段为 `accepted → storing → stored → dispatched → finished/uncertain`。`storing/stored` 跨重启时先清理 Desk 用户投影，成功后才判定为未派发。`dispatched` 跨重启或缺终态时先转为 uncertain，暂停派发。确认旧原生写者退出后，系统按原生证据自动结案。缺少证据则记为 interrupted 并发布 ready，不重发请求或改写历史。写者仍可能存活时保持封锁并退避重探。长期无法确定时保留人工”保留未知边界并继续”入口，不宣称旧操作成功，也不重发或撤销操作。

原生进程受父死 supervisor 与会话 Writer 锁保护，精确旧 Writer 停止后才能接管。结果与诊断进入 Desk 投影，私有原生证据不改写；恢复见[退出与会话恢复](exit-recovery.md)。

## 验证

[终态故障注入](../desktron/tests/test_native_terminal_authority.py)与[事件投影](../desktron/tests/test_native_lifecycle_projection.py)覆盖终态、快照和迟到帧。真实 CLI／SDK 配合本地模拟模型的门禁不代替线上或浏览器验收，入口见[开发与验证](development.md)。
