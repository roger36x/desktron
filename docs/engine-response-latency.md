# 引擎响应延迟测量

分开衡量排队、上下文准备、引擎提交、首个思考／正文事件、流式显示和收尾。后台启动时间不代表用户可操作时间。模型、effort、提示词和上下文不同的结果不能直接比较。

Claude 常驻管道通过空行唤醒空闲读，免去最多 500ms 轮询；已开场的自续轮仍须安全收尾才交接。显式注入生效后，目录供给跳过已退休的旧正文合成，标记过期仍恢复旧路。

Codex 不在提交前查询 MCP 工具清单，直接消费启动状态通知，失败与重新授权仍可见。[官方协议](https://learn.chatgpt.com/docs/app-server)区分清单查询与启动通知；此改动不关闭 MCP 或审批。

前端将同一动画帧内的增量合并显示，不再按剩余字数逐帧排空。长文 markdown 节流、尾部增量更新、取消与收尾补齐继续保留。

2026-09-10 本机可见 WKWebView 对照（仅正文接收至 DOM 布局，非完整请求）：

| 已收到正文 | 旧版 | 修改后 |
| --- | --- | --- |
| 40 字 | 632–633ms | 17–29ms |
| 1,000 字 | 2334–2335ms | 7–17ms |
| 20,000 字 | 4108–4110ms | 26–75ms |

覆盖 900／390px 宽度，390px 不等同于 iPhone 真机。真实队列交接基准从 500.73ms 降至 0.03ms。Codex 一次只读探测：创建进程 2.52ms、初始化 114.31ms、原 MCP 清单查询 142.88ms；未发送模型请求。这些数值不代表模型服务延迟改善。

复现：[调度与协议测试](../desktron/tests/test_engine_response_latency.py)、[WebKit 验收](../desktron/tests/test_chat_latency_webkit.py)。后者需 `DESK_WEBKIT_TESTS=1`，短暂显示测试窗口；`DESK_LATENCY_BASELINE=493bec7` 用此次旧版队列函数对照。

Claude／Codex／Pi／Shepherd 在 `adapter/<engine>/trace/<sid>.jsonl` 记录 `response.queue` 和 `response.timing`。后者以实际开始执行为零点，包含 `stored_ms`、`grounded_ms`、`prepared_ms`、`submitted_ms`、`first_text_ms`／`first_reasoning_ms`、`engine_done_ms`、`ready_ms`。Shepherd 的 `submitted_ms` 只在持久派发栅栏成功后记录。Pi 按原生 `text_delta`／`thinking_delta` 分别计时，不受正文暂存于过程区的展示规则影响。缺阶段即未观测，不填零。trace 只含阶段毫秒、序号和结果，不含正文、工具参数、凭证。

2026-09-18 本机基线：HTTP P50 3.9ms，SSE 热建连 1.8–3.6ms，前端调度交接 0.03ms；短上下文 Provider 首正文 1.30–2.86s。Pi SDK 冷启动到可提交为 1.57–5.50s，提交到首 `text_delta` 为 2.41–2.47s。当前长会话的完整助手消息落盘 P50 13.48s；超过 100k token 的回合为 P50 15.03s、P90 27.84s。完整消息落盘不是 TTFT，只能作为上界；缓存命中与否的完成时间中位数近似（13.32s／13.55s）。

暖回合建议预算：本地 `stored→submitted` P50 ≤0.5s、P95 ≤1.5s，`submitted→first event` P50 ≤4s、P95 ≤10s，首事件到 DOM P95 ≤0.1s；冷启动额外预算 P50 ≤2s、P95 ≤6s。串行等待须单列 `response.queue`，不得混入正常 TTFT。完整发送至可见回复仍需在同模型、effort、上下文与网络条件下测量；用阶段差值定位，不能把首文本前的时间全部归因于模型计算。

## Shepherd 派发前被动埋点

`adapter/shepherd/trace/<sid>.jsonl` 新增 `response.phase`，以 `seq` 对齐已有 `response.timing`。后端阶段使用同一单调时钟零点，记录 `begin` 与 `ok`／`error`、`elapsed_ms`、`duration_ms`、`span` 和 `parent_span`；断开或卡住时，未配对的 `begin` 仅证明进入该阶段。未知、未执行的阶段不填零。

覆盖适配器的准入、Active Producer、目录、系统约定、环境与 Runner 构造；Harness 的状态、恢复边界、输入写入、回合预留和持久派发；统一 Runner 的历史校验与上下文重建、执行目录清理与创建、派发证据；Pi 的 SDK 定位／探测、历史编码、冷启动载荷写入、常驻池准入（含锁、旧宿主退出和容量回收）、进程创建与注册、就绪等待、降级回读校验及 prompt 写入。`pi.resident` 单独记录是否使用、是否复用常驻宿主。

冷宿主在原有 `protocol_ready` 帧附加 `startup_timing`，不新增控制帧；回传 HTTP 初始化、SDK 并行导入、历史恢复、设置加载、服务创建（含资源／扩展装载）、会话创建及就绪校验耗时。后端仅接收固定阶段的有限非负数字，投影为 `host.*`；这些记录用 `clock=pi_startup` 标明子进程独立零点，不与后端时间相减。宿主启动失败时可能没有内部阶段，仍有后端 `pi.ready_wait` 的失败或未完成记录。复用宿主不伪造冷启动阶段。

父阶段包含子阶段，不能直接累加；`pi.pool_acquire` 减去其子阶段后才是剩余准入开销。`submitted_ms` 仍是持久派发栅栏完成，不等于 HTTP 已发出；后续 locator 和 prompt 写入可由新阶段核对。派发到首事件仍不能拆出 Provider 排队、网络与 prefill。

埋点不记录正文、路径、工具参数、配置或异常内容；trace 写入异常不改变业务返回、取消、派发与重试边界。它用于定位等待，不是优化，也不能还原增加埋点前的回合。

验证入口：[阶段诊断回归](../desktron/tests/test_shepherd_phase_timing.py)、[无模型宿主验收](../desktron/tests/test_pi_sdk_discovery.py)。

## Claude 派发后的交付诊断

统一存储的 Claude 两种宿主在同一 `response.phase` trace 记录固定阶段：`claude.start_send`、`start_received`、`store_begin`、`store_ready`、`input_queued`、`input_consumed`、`first_native_event`，以及 `provider_response_started`／`provider_retry`。后两项分别来自上游 `message_start` 与 CLI 原生 `api_retry`，用于区分无响应与有界 Provider 等待；重试不证明请求已被服务端接受。子帧按当前派发 nonce 校验，父进程以同一单调时钟记录接收时刻；迟到旧帧不计入当前回合。`input_consumed` 只证明 SDK 取出了输入，`first_native_event` 也不必含模型正文；两者不能当成 Provider 接收或真实语义进展。

CLI stderr 仅投影每次派发最多四种白名单类别（认证、网络、限流、其他），不记录原文、URL 或配置；类别是启发式诊断，不是已证实根因；它按收取时的当前回合归属，不能证明旧后台 stderr 属于当前请求。心跳与这些通知均不续期。宿主快照同时固定诊断模块，避免源码热替换后混用 Bridge 字节。

当前进展与超时语义见[等待与超时](shepherd-execution.md)。
