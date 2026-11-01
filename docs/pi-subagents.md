# Pi 子代理执行与回投

Desktron 的 `pi_children.ts` 是固定版 `pi-subagents` 0.78.0 的适配 extension。升级修复了 Pi 1.2.0 已移除 `@earendil-works/chord` 但旧版仍强制要求它导致后台启动失败的问题。后台依赖以宿主 manifest 声明为准，仍声明 chord 却缺包的安装继续报错，不为规避检查补装退役包。通过 `pi-subagents-setup` 安装锁定依赖，独立版本槽不会覆盖旧运行进程。升级不修改用户 Pi 安装或凭证，新增 `@js-temporal/polyfill` 及传递依赖由锁文件校验。未准备依赖时主 Agent 仍可使用，但会提示子代理不可用。

后台子进程通过 `PI_SUBAGENTS_PI_CODING_AGENT_PACKAGE_ROOT` 接收宿主 SDK 根目录。适配器的裸命令名 `pi` 必须先按 PATH 定位，再解析 npm 入口软链，显式文件路径不回退到另一安装。同步调用成功不能代替异步启动验收，原生回归使用与 `PiAdapter.binary` 一致的命令名来覆盖此边界。

`subagent` 提供 `start/status/wait/cancel`，接受 `agent/task/context=fresh|fork`，或最多 3 项独立 `tasks`。`mode=sync` 等待结果并经工具返回；`mode=async`（默认）返回 `run_id`，让父 Agent 继续独立工作。异步默认 `required=true`，属于本次父调用的必需依赖；仅 `required=false` 使用独立通知。`wait` 阻塞等候既有依赖并返回终态，不重新启动任务；`status` 不消费结果。旧通知型运行不能被 `wait` 抢占。同步多任务统一采用上游 runs.all 工作流，三项任务按上游并发上限同时调度，不再按 provider 重塑批次；异步多任务分别调用原生单任务接口，不采用依赖父进程的异步 workflow。部分失败保留其他任务的结果。上游单次运行派生预算和活跃异步上限均为 3，不设累计会话次数上限。`fork` 将当前分支保存为上游可读快照，排除本次未完成工具批次。角色通过上游 runtime-agent API 注册，仅 `desk-scout/desk-reviewer/desk-oracle`，工具仅 `read/grep/find/ls`。显式加载 Provider 和被动观察器；禁用环境扩展、skills、项目及全局上下文、写工具和嵌套。

时限由 Desk 决定，父 Agent 不再选择；工具参数中已移除 `timeoutMs`，旧调用传入时仅计入调用身份，不影响时限。两层判定：**无进展**：连续 5 分钟没有真实进展即终止。进展由子代理观察器写入 `state.json` 的 `progress_at`，流式输出、工具开始或结束、请求开始、排队进出、重试均计入；传输等待心跳只刷新 `updated`，不计入。处于排队（`capacity`）、工具执行（`tool`）或重试（`retry`）阶段时视为存活，已结束（`settled`）的子任务不参与判定；启动后始终没有观察记录的，从调用开始计时。同步调用在父进程内计时，全部未结束子任务都无进展时经工具 AbortSignal 中止；异步调用由 Desk 后端刷新循环判定，经上游 stop inbox 终止，不依赖父 Pi 进程存活。**总时长**：30 分钟，作为上游运行时限传入，同步多任务限制整个工作流，异步批次限制各子任务。无进展终止记为失败，错误码 `IDLE_TIMEOUT`，结果带回子代理已有的部分输出并正常回投，不按用户中断静默。两个阈值可由 `DESKTRON_PI_SUBAGENT_IDLE_MS`、`DESKTRON_PI_SUBAGENT_TOTAL_MS` 覆盖，仅用于测试和排障。

上游负责子进程、调度、超时和取消；Desk 不再维护独立 worker、HTTP 启动服务或续租调度器。同步中断通过原生 AbortSignal 传递；异步取消写入上游持久 stop inbox。Desk 回合中断或退出请求取消该会话异步任务，终态以上游记录为准。后端恢复时读取已有运行和交付记录，不重放启动。

`pi_subagent_observer.ts` 将子任务事件映射到 `subagents/<session>/<child>/`，复用 `/agents`。详情顶部和切换列表显示“排队中”；等待心跳保留排队状态，出队恢复运行，终态优先。排队进度只更新界面，不额外通知父 Agent。

`pi_children.py` 只投影上游状态。同步结果不重复通知；`required=false` 的异步结果通过现有持久队列交付，使用稳定批次 `run_id`，失败也交付。同一时刻的积压结果合并为一次持久通知：最多 3 批，累计结果文本超过 65,536 字符时停止追加，首批不受这个聚合阈值截断；批次成员和请求 ID 在队列 claim 前落盘，崩溃恢复不会吸收新成员或重复投递。通知回合不装载 `subagent` 工具，不能再次派生子代理。用户中断会先建立持久会话栅栏、撤销尚未派发的通知收据，再停止在途任务；即使中断或退出早于上游运行身份写入，观察器也会按启动槽位发现官方运行记录并补发停止。栅栏之前尚未送达的结果即使与停止竞态并最终成功，也只保留在子代理记录中，不再续起父会话。暂时入队失败保留结果重试；已接纳请求持续核对收据，拒绝、取消或不确定记为 `delivery=blocked`，不重放。原会话切换到其他引擎时暂存，真正取得回合锁后再次检查并取消不再属于 Pi 的通知；不会把结果发送给新引擎。


## 父调用内依赖汇合

`pi_child_join.mjs` 管理必需依赖，`pi_children.ts` 在首次发布 `launch.json` 前固定 `deliveryPolicy=join` 与 `parentInvocationId`。Python继续观察、停止和生成 `delivery.json`，但不将必需依赖排入独立通知队列。

`before_agent_start` 创建父调用身份；`turn_end` 仅接收已经完成的依赖，不阻塞独立工作；`agent_end` 使用活动AbortSignal等待剩余依赖，通过follow-up在同一 `prompt()` 内汇总，再结束调用。父调用错误或取消不启动汇总；缺少可取消活动父调用时明确拒绝必需异步任务，不静默降级为通知。

TS单独维护 `join.json`，状态为 `waiting → offered → observed → consumed`。内存branch只能证明observed；仅真实会话文件回读与当前branch精确一致才记为 `consumed/archive_confirmed=true`。Unified内存会话缺少宿主持久化确认握手，保留observed且archive_confirmed=false。崩溃后的offered/observed不自动重投、不改走通知；旧等待记录只允许显式wait恢复，不自动加入新用户请求。

每次父调用最多24批依赖，自动消息正文预算56000字符，wait正文最多32000字符；截断保留所有outcome及完整结果路径。只轮询本次依赖，不扫描全局历史。总时限加15秒仍无终态回执时返回 `DEPENDENCY_TIMEOUT/blocked`，不假作验收通过。取消信号及持久栅栏优先；等待不持有Python桥锁。

回归见 `tests/test_pi_child_join_native.py`（真实SDK与loopback模型）、`tests/pi_child_join.test.mjs`（账本、幂等与预算）、`tests/test_pi_child_join.py`（通知互斥与恢复）。源码及测试通过不代表已部署。

独立通知结果复用 [Agent 来信气泡](ui-architecture.md#agent-来信气泡)，不改走 desk-msg。用户入口见[子代理与结果通知](../desk-docs/subagents.md)。依据：[回投实现](../desktron/lib/pi_children.py)、[状态机回归](../desktron/tests/test_pi_children.py)、[原生验证](../desktron/tests/test_pi_subagents_native.py)。
