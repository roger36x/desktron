# Shepherd 存储与执行宿主

`ShepherdAdapter → ExecutionHarness → 临时原生上下文 → 统一事件归档 → 来源校验 → Turn Index`

每个会话在 `shepherd/archive/<SID>/history.sqlite3` 中保存当前引擎新产生的原生事件、逐事件哈希链、终态、Binding 游标及检查点。SQLite 事务采用 FULL 同步。Claude 的 SessionStore 写入等待归档确认，Pi 和 Codex 家族的事件在主进程收到后持久化。

Turn Index 和 receipt 保存引用、哈希与执行状态，不复制正文。派发前的用户输入单独保存为不可变哈希引用；取消后重试可更换输入，旧尝试仍可审计。

Claude 回合在 dispatch fence 前，将既有 `runtime` 事件和独立 `native_anchor` 事件写入同一归档事务。锚点只保存 attempt、派发前父 UUID、原生 revision、store size、Bridge 协议、实际 CLI 版本及可选 Grounding UUID，普通事件追加接口不能创建锚点。重复写入只有在字段及 Coordinator 提供的可信 attempt 完全相同时才幂等，冲突时拒绝派发。若进程在锚点提交后、dispatch fence 前崩溃，启动恢复先把锚点移入 `abandoned_turns`，再取消 reserved 回合，不得重发。公开 JSONL 是界面投影，原生锚点不进入正文。锚点声明 Grounding 时，缺少匹配 runtime 元数据的用户输入或输出会被封锁。早期消息的分页读取不需要打开引擎私有文件。

Turn Inputs 的配额按整轮公共输入序列化后的 UTF-8 字节计量，包含文本、图片 base64、JSON 转义及分隔符，不按单张图片大小推算。公共输入上限为 64 MiB，v1 引用封套另预留 1 KiB。

Shepherd 的三个 prompt HTTP 别名在读取正文前检查独立的 65 MiB 传输预算，超限返回 `prompt_body_too_large`（HTTP 413）并关闭连接，不消费剩余正文。准入、持久写入和回读使用同一契约，显式 context 在准入和落盘后也使用同一截取与渲染规则。写入在创建输入文件、保留回合及原生派发前核验；回读有界读取并复核公共输入配额。超限单独报 `turn_input_too_large`（准入 HTTP 413），不能归类为权限错误。

附件保存失败不能静默丢弃：未接收批次回收自身新建文件，显式 context 在用户气泡写入前完成回读，文件名按 UTF-8 字节截短并保留落盘前缀空间。附件准备失败通过 `invalid_attachment`（HTTP 400）或带 request ID 的 `prompt_not_started` 拒绝事件反馈。前端将这些明确未接收的拒绝恢复为可编辑草稿，不进入未知回执猜测或自动重发。单图 12 MiB、图像尺寸与模型准入限制仍独立生效。既有 v1 输入在同一契约内直接验证，不改写正文、引用或哈希。此前因回读失败封锁的已完成回合仍走既有终态恢复链，必须核验来源、Writer 退出及 receipt 后只补提交，不能重派模型或清除未知栅栏。

## Claude 图片执行副本

存储配额不代表目标引擎可以接收同样大小的图片。`image_transport.py` 对当前 Claude Code 链路采用保守策略：单图 base64 不超过 4,000,000 字节，派生文件不超过 3,000,000 字节，最长边不超过 2000 像素；整个执行视图（历史、checkpoint、当前输入、系统提示、Grounding）序列化不超过 24,000,000 字节，图片不超过 100 张。这些是 Desk 的安全预算，不宣称所有 Claude 平台上限相同，也不能代替原生 token 准入。

小图保持原字节。超预算图片先用固定 Pillow 12.1.1 解码（最多 3200 万像素），遵循 EXIF 方向及首动画帧规则。优先无损 PNG，再依次尝试 JPEG 95/90/85/80，透明底合成白色，必要时等比缩小，不裁图或放大。不能满足预算时明确拒收：HTTP 返回 `image_transport_too_large`（413），异步派发前失败走已有 `prompt_not_started`，前端恢复草稿，不自动重发。

统一存储的 Claude 执行视图超过整请求预算或图片张数上限时，`fit_entries` 只调整较早的历史图片，当前输入的图片和所有非图片内容保持不变。处理分三步：先按尺寸读取每张历史图片的像素数，按每像素 0.30 字节的偏大估计计算缩到最长边 1024 像素后的大小；从最新一张往前累加，第一张放不下的图片连同更早的图片全部省略，不再编码。随后从最早一张开始，把保留的图片压成最长边 1024 像素的 JPEG 80（PNG 来源更小时用 PNG），总量合格即停止。最后按实际字节核对，仍超出时继续压缩下一张，没有可压缩的图片时省略最早的一张，直到满足预算。被省略的图片在执行副本中替换为一段文字说明，写明原尺寸和 SHA-256 前缀；超出 3200 万像素、无法压缩的历史图片直接省略。这类派生件使用独立策略 `HISTORY_POLICY_VERSION`，与单图派生件分开缓存，并照常写入派生清单。省略或压缩的来源也计入常驻宿主的来源集合，变化时冷启宿主。当前输入与文字历史本身超过预算时仍明确拒收。本轮有压缩或省略时，回合状态提示会写明张数。旧私有 SessionStore 不走这条路径。

派生件位于 `image-variants/<SID>/`，按策略、原图 MIME 与 SHA-256 寻址；私有目录及文件禁止跟随符号链接，读取验证所有权、权限、有界长度、原图身份及派生哈希。原子发布后回读校验，重复使用同一件；已有损坏文件报错，不以重新压缩掩盖。会话删除一并清理派生目录。派生清单与 Claude native anchor 在派发闸前原子归档。写入与撤销共用预派发字段校验；派发闸拒绝及 reserved 启动恢复按固定顺序识别可选 Grounding 与派生清单，完整保留 abandoned evidence 后释放预留回合。错误清单、错序、重复记录、原生输出后缀或哈希错误继续封锁，不因增加记录数量而放行。常驻宿主身份含策略版本，新增需处理的历史图片会使未包含对应派生件的宿主冷启。

无损 codec、既有 archive/source/public hash、原图附件和 TurnInputs 不改写。历史用户图、工具图及原生 checkpoint 只在验证后生成执行副本。本轮新增原生 source/public 如实保存模型实际收到的压缩图片，原图权威仍在不可变 TurnInputs；不能日后无条件换回原图、重算 public hash。提交或恢复只核验已归档来源，不依赖压缩器或缓存，不重跑模型。旧私有 SessionStore 超预算时明确拒绝，须经现有统一存储迁移后重建，不原位改写同 UUID 的旧图片。

## 原生宿主

Claude 使用内存 SessionStore 和临时磁盘镜像，Pi 使用 SessionManager.inMemory，Codex 使用私有临时线程并在停止 Writer 后读取工作上下文。检查点和完成终态一同持久化，随后清理临时文件；导出或保存失败的副本保留待核验。私有线程关闭原生后台记忆生成，不修改普通引擎配置。切换只推进游标，不维护多套永久正文。自动摘要不改变归档或 Producer。

Claude Host 与 JS Bridge 在启动时固定为同一份字节快照。Claude CLI 版本先从本轮选定的精确 executable 以无 shell、5 秒截止和 4 KiB 输出上限探测，再由同一 query 进程的派发前 `get_binary_version` 控制响应核对。字段缺失、超时或错配均不输出 prepared。用户输入在 prepared 和 dispatch fence 完成前保持 gated 状态，控制初始化可能产生的 SessionStore 写入仅暂存，start 后才纳入本轮归档。实际 turn 若返回 `system/init.claude_code_version`，还须与已探测版本一致。一次性 Host 每次探测一次，常驻 Host 每个进程探测一次，不以 SDK 包版本或旧历史代替。

Claude 完整 `nativeRevision` 由 Bridge 对完整 Store 计算，用于锚点和同一 resident 的相邻回合连续性；统一归档不包含 bootstrap 私有前缀，不能据此重建完整 revision。跨语言终态对账使用 `transitionRevision`：Bridge 在 archive 帧中同时发送结构化 `entries` 与逐条 `JSON.stringify(entry)` 的 `entryPayloads`，主进程逐条解析对照后直接哈希原始 UTF-8 字符串。Python 不以 `json.dumps()` 模拟 JavaScript 序列化。旧 SQLite SessionStore 的来源 revision 也从数据库中已保存的原始 payload 计算，不对解析对象重新序列化。

同一 Binding 的连续正常回合可复用原生 Host：Claude／Pi 使用 Shepherd native 池，Codex 使用 app-server 池。内存状态不是恢复依据。Binding、模型参数、系统提示、cwd、权限模式或历史覆盖边界发生变化，以及失败和中断，都会淘汰 Host 并从持久证据冷启。生成新检查点后不保留旧历史所在的 Host。Claude Host 在已提交回合后若超过单 Host RSS 软上限（默认 384MB），也会安全换代，下一轮从 Working Context 恢复，不切换引擎。Host 空闲 30 分钟回收，且受共享进程预算约束。`DESKTRON_SHEPHERD_PERSIST=0` 可关闭 native Host 常驻，`DESKTRON_SHEPHERD_HOST_RSS_MB=0` 可关闭单 Host RSS 换代。

早期原生 Binding 的已提交来源不依赖当前 CLI／SDK 准入：Codex 从私有 HOME 读取唯一 Rollout；Pi 按 Session v3 格式验证身份、树结构与范围；Claude 从本地 SessionStore 回读。保留所有权、完整记录、receipt、来源及公开投影哈希校验，不启动原生引擎、不迁移历史，也不回退到用户 HOME。CLI 发布版本仅为来源元数据，不代替实际结构与语义校验。持久 codec 中的旧版本名称不等同当前运行版本；不重标旧 Binding 或改写归档哈希。

归档、用户输入、公开显示投影和检查点承担不同职责，不能将“统一存储”理解为磁盘永远只有一份字节。启动及派发前回收已登记的临时执行目录：核对目录归属、进程创建标识、进程组和租约锁，仅在宿主释放或死亡且子进程全部停止时删除。无法确定存活、未登记或进程创建中断窗口的目录保留，不按年龄删除；临时目录不作为恢复依据。Windows 常驻 HOME 换代清理仅在删除单链接、非重解析的普通只读文件遇到 `WinError 5` 时清除该文件的只读位并重试一次；链接、目录、共享冲突及其他权限错误仍阻断，不修改 ACL、不忽略残留。

删除按”Coordinator 墓碑 → 无正文删除标记 → 内容清理”的顺序执行。先持会话 Turn/Switch 双锁并证明所有 Writer 已停止，再依次清除统一归档、Turn Inputs、来源与 Ownership 收据、检查点、原生 Binding 存储、常驻 HOME、迁移／对账备份、请求／压缩回执和检索缓存。状态文件最后删除，清理中断时可作为启动补偿清单。

长期只保留 `deleted/<SID>.json`，字段为版本、会话 ID 和 deleted 状态，不保存正文、Binding、请求或执行证据。请求准入和同 ID 新建与删除标记共用锁，迟到请求稳定返回 `conversation_deleted`，不能复活会话。删除不撤销文件修改或外部系统副作用。


依据：[统一归档](../desktron/lib/shepherd/unified_archive.py)。恢复边界见 [Shepherd](shepherd.md)，上下文见[检查点与压缩](shepherd-context.md)。
