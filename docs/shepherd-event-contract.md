# Shepherd 事件契约与产品链路门禁

Shepherd 的原生 Runner 负责执行、原始归档和终态确认；`engine_adapters/shepherd_events.py` 负责传入界面翻译器的事件格式。两者不共享成功判据。

## 入口契约

正式 `_turn_dispatch` 的三引擎回调都经过 `EventProjection`。Claude 的字符串消息在展示入口转成单个文本块，内容数组、Pi 事件和 Codex 通知按实际消费的容器和字段类型校验。只在需要转换时浅复制消息容器，不修改原始事件或复制历史正文，也不重建原生 Harness 的完整 schema，不拒绝未消费的附加字段。

Pi 0.86 的 system transcript，以及 user/custom 消息，允许字符串正文；展示入口将其浅复制为文本块，原始事件保持不变。助手终态和工具结果仍要求内容数组，合法系统消息不会进入助手回复。

原生历史中的 Pi system 消息经格式校验后保留于原始来源，不投影为公共聊天；目标引擎的系统指令仍由 Desk 重新生成。此兼容仅接纳此前被拒绝的角色，不改变旧成功回合的投影或历史 hash，也不放宽未知角色、损坏内容和工具配对校验。

## 异常边界

允许降级的事件类型由 `presentation_only` 明确列举，包括文本增量、工具展示、压缩进度和用量通知。格式错误与内置的类型、属性、下标异常会产生可跨刷新读取的回合警告，Runner 继续接收原生结果并完成归档。警告不会把本轮设为失败，新回合清理旧回合警告。

权限、扩展控制、终态、原始来源和未知事件不在降级名单，它们的异常继续传播。存储、来源校验、状态冲突以及未分类异常也不被吞掉。告警写入本身失败仍然传播。取消和重试策略不由此模块决定。

每回合最多记录 8 种错误签名，同一签名只写一次日志，界面只发一次警告。日志包含引擎、事件名、字段类型及不含局部变量的堆栈位置，不记录事件正文、工具参数或异常携带的原文。异常计数保存在活动上下文中。

Pi 长任务复核通过 `session.execution_review` 投影；属性包含 `sessionID`、当前 generation 和裁剪后的 `review` 快照。`/state.execution_review` 提供同一快照，页面刷新可恢复显示。事件只用于展示，不授权续租；执行层仅接受匹配的 Pi 原生 `steer` 回执及后续原生生命周期。其他引擎当前不产生该事件。

## 提示与执行的归属

提示继续复用 `error_notice`，不另建错误状态机。原生引擎和 `lifecycle` 提供请求结果、重试及取消事实；Shepherd 台账和恢复控制负责来源校验与放行。提示条的严重程度、关闭或撤销不参与执行控制。

切换准备提示与普通回合提示共用存储和修订号。需处理的权限／恢复提示优先，其余错误优先于普通警告；隐藏的问题仍保留，对应阶段恢复后重新投影。`session.error.environment` 携带后端已选好的提示，前端不再以“已有任意警告”阻止显示回合失败。新回合按原作用域清除旧回合提示。

Pi 明确的 `info` 通知仅记不含正文的诊断；警告和未知级别仍可见。Provider 兼容提示属于当前回合。手动压缩的取消以已提交 `interrupted` 终态为依据；Pi 桥持续接收 abort 并调用原生 `abortCompaction`，原生取消终态确认后结束，不以发送取消指令替代确认。取消不替换旧检查点，不生成成功分隔线；真实失败仍报告错误。原生明确 declined 映射为 noop，SSE 与 HTTP 展示均区分取消、无需压缩和成功。

Claude 2.1.268 的手动压缩取消返回 `compact_result=failed`，随后 `local_command=compact` 的调用结果却为 success；适配器采用前者，不从英文文案猜测取消。失败命令的版本化 `system/local_command` 记录按格式和会话身份校验，保留于来源、不进入模型历史；不能再要求它有助手正文。该路径保留失败提示，旧检查点不变且允许正常续聊。

## 校验分层与自动恢复

原始归档与工作上下文缓存不是同一层。身份、所有权、哈希、工具配对、回滚和派发边界仍是硬校验；已知原生类型只校验消费字段，不因附加诊断字段阻断。Codex 子代理返回投影为带来源标记的历史助手消息，加密块留原始来源；Claude 隐藏思考和 Pi 已执行 Bash 记录不变成新指令或待执行工具。

Codex 普通回合收到匹配的完成事件后，若仅工作快照格式不兼容，可自动从独立、完整校验的公共历史继续。先持久化原始 JSONL、经校验展开的外置内容及恢复事件，再淘汰旧宿主；下轮使用已验证历史重建上下文，不重派本轮。界面提示“已自动恢复，可以继续”。手动压缩不以这个路径伪造成功。证据写入失败仍阻断，但关闭 Writer 时保留整个原生 HOME，后续启动不能删除它。

Pi 0.85.1 在助手生成以 `error`／`aborted` 结束时不会派发其中的工具调用。统一归档自 projection v3 起，将这类残留调用保留为“生成未完成、未派发”的历史文本，不补造工具结果；同 ID 若存在真实结果则拒绝矛盾来源。正常调用缺结果仍阻断。已存在的 v2 或未标版本回执继续按 v2 回读，避免改变旧历史 hash。该升级只用于统一归档，不更改原始 Pi 记录。

新 Pi 回执采用 projection v4，保留 v1/v2/v3 的既有回读语义。分支摘要和扩展自定义消息投影为带来源标记的历史上下文，保留文本、图片及顺序；`display:false` 只影响原生界面，不删除上下文。扩展状态 `custom` 仍不进入正文，未知块继续拒绝。真实用户输入是否已在来源中保存，按原生消息类型判断，不能被扩展上下文的 user 角色替代。

底层 `begin_reconciliation` 在状态事务中确认原 Writer 已退出后才隔离换绑；存活、身份不明及已派发但身份缺失均阻断。未派发的 reserved 回合无进程身份时可结束。该检查不停止进程，也不调用模型或工具。

旧回合的投影兼容修复后，Controller 可在每个进程生命周期内对同一 attempt 自动重验一次，包括已有终态但提交失败或 `source_shape_unsupported` 的来源；必须确认旧 Writer 已退出。回执、原始终态和历史不重写，未知身份、损坏来源仍阻断。恢复 locator 必须指向当前精确回合与派发后缀；已有 unknown 回执不能绕过 Writer 身份检查。

Claude 压缩段按语义链校验，不要求边界与摘要物理相邻：边界之后只允许经过审核的内部附件类型，每条记录的 UUID 父链必须连续，摘要需与边界声明的 anchor 一致。未知内部附件归类为 `source_shape_unsupported` 并携带 Claude Code 版本，提示升级解析器；断链、错锚和缺失摘要仍视为来源校验失败。兼容性诊断只记录压缩边界、摘要及链内附件的类型和数量，不记录内容。

Claude Code 新增、尚未登记的条目类型按内容分级。只有同时满足以下条件的记录才作为未审核元数据放行：会话号与当前会话一致；类型不是 `user`、`assistant`、`system` 或 `summary`；不含消息、内容、附件、工具结果或 UUID 父链字段；每个值都是不超过 256 字符的标量或不超过 128 项的标量列表；整条记录不超过 4096 字节。放行记录不进入公共对话，并以 `tolerated_unreviewed_entry`、`unreviewed/<类型>` 写入兼容诊断。其他未知条目归类为 `source_shape_unsupported`，隔离提示附带具体条目类型，不再笼统显示为 `commit_verification_failed`。经过审核的类型仍应登记到连接器并按专门形状校验，例如 2.1.311 起的 `tools-used`。

统一 Claude 回合在派发前归档独立 `native_anchor`：其 attempt 与 Coordinator 当前 attempt 对账，parent UUID 与首个可链接增量交叉校验，Grounding 存在时必须与 runtime 元数据交叉校验；store size 只作定位，不作身份依据。Bridge 的 prepared 必须唯一且 `startSeq == storeSize`；终态的 start/end、Grounding、状态及 revision 格式必须覆盖已确认归档的精确增量。每个 archive 帧同时携带结构化 `entries` 与 Bridge 执行 `JSON.stringify(entry)` 得到的 `entryPayloads`；主进程先逐条解析并对照语义，再用原始 UTF-8 载荷重算 `transitionRevision`，不得用 Python JSON 序列化模拟 JavaScript。完整 `nativeRevision` 只证明 Bridge Store 当前状态并用于同一 resident 相邻回合连续性，不从统一归档镜像重建。锚点缺失、字段非法、ACK 失败或证据冲突均不得越过 dispatch fence，也不得产生成功终态。锚点不进入公开正文或兼容性诊断，旧归档没有该事件时继续按既有版本读取。

## 验证

- `test_shepherd_event_contract.py`：脱敏的 `/compact` 字符串回执、三引擎格式回放、原始事件不变、告警去重、过期回合隔离、权限／存储异常不得吞掉。
- `test_shepherd_product_native.py`：真实三引擎经正式后端派发入口、事件契约、翻译器和归档执行；覆盖发送、主动压缩、切换、宿主适配器重建、取消后续聊及三引擎压缩中取消。故意注入展示异常，核验警告、完整来源、终态、旧检查点保留及下一轮。
- `test_shepherd_unified_native.py`：三引擎工具图片、压缩后切回与恢复同时连接展示翻译器；不得以警告代替测试成功，也不得重跑历史工具。

原生用例使用本地假模型，不调用线上模型，不证明线上服务性能。这里验证后端派发和事件输出，不代替浏览器渲染或真实鼠标键盘验收。

```bash
python3 scripts/dev/run-tests.py desktron/tests/test_shepherd_event_contract.py
python3 scripts/dev/run-tests.py --native desktron/tests/test_shepherd_product_native.py desktron/tests/test_shepherd_unified_native.py
```

这些原生测试随既有 `refactor-gate.sh release` 自动执行。引擎版本升级时先跑 release；出现新格式故障时先补脱敏回放，再扩展入口契约与相应真实场景。
