# Shepherd recap 与自动标题

Recap 属于公开 Conversation，不属于当前 Producer。沿用公共回合收尾调度、取消、journal 与任务事件投影；Shepherd 不是额外的执行引擎。

## 调度与来源

成功提交后按公共调度器等待空闲：`DESKTRON_RECAP_IDLE_S` 默认 120 秒，且不超过预留末次请求生成时间余量后的缓存安全窗口的一半。显式记录请求或关联任务完成使用 1 秒延迟。Recap 沿用本轮实际引擎和选项。配置项目记忆 Pi 通道时，自动标题可独立由该通道根据开头用户消息生成，不必等待 recap。公共配置见[记忆生成与消费](knowledge-memory-management.md#生成通道)。

快照记录 Binding、Producer generation 与已提交来源边界。生成前校验来源，提交结果前复核快照。未决执行或切换时不生成。统一归档路径使用已校验的公共历史或只读工作检查点，旧私有路径继续校验原生记录和 Manifest 哈希，不改写正式来源。

## 隔离执行

- Pi 统一归档的普通 recap：使用主回合的扩展、工具定义、系统提示和会话号，以 `pi_memory_rpc.mjs` 内存宿主执行。`DESKTRON_PI_RECAP=1` 让 gate 拒绝全部工具并禁止压缩；收到第一条助手完成帧即结束，不写归档。只接受正常结束、无工具调用、无服务端错误且有正文的完成帧。
- Pi 旧协议的模型型 peer 问询及旧私有来源：使用临时上下文与 no-tools fork；不将此路径泛化为所有 Pi recap，也不适用于当前 `desk-msg ask` 的只读历史检索。
- Claude：SQLite backup 创建一致性副本，由托管 SDK 从副本恢复；工具和设置来源关闭，写入仅留在临时 SessionStore。统一存储的 Claude 绑定不展开全量归档：以只读方式复用正常回合的工作检查点、准入尾部和降级检查点，写入临时 SessionStore 时按不超过 8 MB 分批，单批上限仍为 16 MB。曾因整段历史一次写入而报“Claude SessionStore 批次过大”。
- Codex：仅连接对应 Binding 的私有 HOME，使用临时 thread fork；覆写为只读沙箱和 untrusted 审批，工具请求拒绝，工具事件触发终止。

支线不经过 ExecutionHarness，不新增 Manifest round 或来源 receipt。临时目录结束即清理；标题、journal 和任务事件仅使用公开 SID。隔离测试证明正式来源与协调状态不变，不代表真实模型的命名质量。

## 命名与取消

没有用户名称和自动名称时请求一次标题；老 Shepherd 会话在下一次成功收尾补命名，不扫描闲置会话触发模型请求。普通引擎保持前两回合的命名窗口。

新回合或实际引擎切换取消普通 recap 支线；该支线的迟到结果、来源变化或生成错误不写标题和 journal。独立 Pi 标题任务不属于此取消机制，保存时仍尊重用户改名及已有自动标题，不写 journal。用户名称显示优先级不变。显式 recap 请求成功后消费，失败或取消保留到下一轮。

## 连续失败提示

`recap-state.json` 的 `fails` 记录连续失败次数，成功一次即清零；被新回合抢占记为取消，不计失败。`fails` 达到 `RECAP_ALERT_FAILS`（2）时，`/config` 在会话没有更具体的环境异常时返回一条 `stage=recap` 的 warning，显示在输入框上方，详情含最近一次错误与最近一次成功时间；recap 恢复成功后该条随下一次 `/config` 消失。计数按引擎记录，不区分会话。

## 验证与能力准入

`test_shepherd_recap.py` 覆盖调度、补命名、私有 ID 隔离、错误、取消、恢复请求与来源栅栏。`DESK_SHEPHERD_NATIVE_RECAP=1` 启用三引擎本地模拟模型验收。`DESK_SHEPHERD_CLAUDE_BINARY` 可指定测试二进制；历史测试使用过 2.1.268，不是产品执行白名单。

实际 CLI 与 SDK 必须通过当前能力探测；持久 codec 中的旧版本标识不代表执行版本限制。不得关闭能力检查或修改用户默认安装来规避依赖问题。真实模型质量、部署、同步与重启分别验收和授权。
