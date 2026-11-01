# 引擎与会话

当前 GUI 引擎注册为 `claude`、`codex`、`pi`。模型是引擎内部的选择，不因使用同一 CLI 的不同模型而新增”引擎”。

Shepherd Mode 是独立的会话模式，不是额外的执行引擎。它保留当前执行引擎，使用同一个 Desk Conversation 管理隐藏的原生 Binding。旧 Exp 实现已退役，运行态数据保持原样，不自动删除或原地迁移。

## 接入边界

[engine.py](../desktron/lib/engine.py)负责适配器的创建和查找，具体协议在 [engine_adapters](../desktron/lib/engine_adapters) 目录下。Claude、Codex、Pi 的子进程协议、会话发现和恢复方式各自独立实现，不能假定它们共享 transcript 格式或登录目录。

适配器将引擎事件转换为 Desk 的会话状态和界面事件，应明确区分回答生成、工具运行、等待授权、等待用户输入、完成、中断与失败。连接重建不能伪造新一轮执行，也不能用文本尾部模式代替引擎提供的结束信号。

原生重试、维护操作、终态确认与刷新恢复须遵守[生命周期投影契约](native-lifecycle-projection.md)。新适配器必须覆盖请求送达不明时的不重发边界。

## 新会话配置

`engine_opts.<引擎>` 保存最近选择的模型、effort、自动压缩等默认值，Shepherd 创建时将其归一化为会话快照。`shepherd_engine` 独立保存下一段 Shepherd 会话的执行引擎，不修改普通引擎书桌的 `engine` 偏好。创建入口显式指定引擎时优先，尚无有效偏好时沿用当前引擎。前端设置成功回包后同时更新 `sessionOpts`、当前选项及默认快照，不依赖 SSE 来完成本地同步。Pi 的显式空压缩值表示”自动”，必须保留，以免后续全局阈值污染已有会话。角色与 Shepherd 路由字段不写入引擎默认值。

依据：[设置与创建回归](../desktron/tests/test_shepherd_settings_inheritance.py)、[模式回归](../desktron/tests/test_shepherd_mode.py)。

## Pi 的 OpenAI 模型目录

Desk 的 Pi 模型菜单仅展示 Pi 已注册且在 Codex `model/list` 可见目录中存在的 OpenAI 模型，按精确模型 ID 求交集。该规则同时覆盖 `openai` 与旧 `openai-codex` provider，其他 provider 不变。模型路由、能力与 effort 仍取 Pi 自报，不把 Codex 的条目凭空注册到 Pi，也不把候选条目视为已经过调用验证。

过滤在读取菜单与设置状态时执行，原始 Pi 目录缓存保持不变，Codex 目录变化后重新求交集。目录读取使用 Codex 既有的非阻塞元数据缓存，不发送模型请求。来源为空或不可读时隐藏 OpenAI 条目并记录原因，不退回通用 API 清单。浏览器目录缓存升级到 `desk.modelCatalog.v2`，不复用旧清单。Pi 的共享过滤结果（含空目录）优先于视图旧目录。每次打开 Pi 菜单时先即时展示缓存再刷新目录，避免多 provider 中的部分目录失败阻止后续恢复。模型增删时更新仍打开的主菜单，标签变化则保留节点与滚动，不覆盖已关闭菜单或二级菜单。既有会话保存的模型及执行路由不会被自动替换，终端独立启动的 Pi 原生菜单不受此规则影响。

依据：[目录与缓存回归](../desktron/tests/test_pi_catalog_cache.py)、[浏览器缓存回归](../desktron/tests/test_frontend_model_catalog_cache.py)。

## 会话生命周期

[term.py](../desktron/lib/term.py)及 `term_sessions.py` 管理会话注册、恢复和历史接入。PTY 交互与 GUI 流式回答是不同通路。操作助手的首次命令必须写入成功后才清除 pending，避免出现”窗口已开但脚本未执行”的情况。

跨会话 `ask` 使用 [peer_context.py](../desktron/lib/peer_context.py) 的独立只读进程，最多运行 5 秒，不取得目标回合锁，不启动引擎，也不写 mailbox。Shepherd 会固定稳定的 ledger head，已有索引只选候选，正文通过 ReadProjector 校验来源，结果携带引用、覆盖范围和缺口。普通会话只提供近期收口消息投影。旧版进行中的请求仍由 peer_mailbox 持久化，`wait` 可继续领取，不自动回投普通回合。历史证据不构成用户授权。

Shepherd 的普通 `send` 不保证忙时排队。未收口请求阻止新请求准入，返回 409 并明确消息未被接收。上一条仍在受理、执行或收尾时返回 `request_pending`，界面把消息留在待发队首，等会话再报 ready 后重发。结果待核验（uncertain）时返回 `session_busy`，不得自动重放。未知程序错误仍返回 500。回合结束的 ready 必须在本轮请求回执收口后才发布。

Shepherd Pi 的旧协议模型型 peer 问询支线（不同于当前 `desk-msg ask` 的只读历史检索）以只读方式复用主回合的工作检查点、尾部和上下文准入，不再展开已覆盖的全量历史。准备后的临时原生文件回读一致后才启动无工具生成。源 archive、检查点、请求与普通历史均不写入。降级后缺失的精确材料只能回答无法确认，不得假装检索。生成超时与准备超时分别保留 `generation_timeout` 和 `preparation_timeout`，不与用户取消混同。前台等待超时仍不取消后台生成。普通 recap 的同前缀内存宿主与该无工具问询路径不同，见[recap 契约](shepherd-recap.md)。

## 会话名称

Desk 右键重命名是名称的唯一权威来源（SOT），保存在 `.workbench/custom-titles.json`。原生引擎名称是单向同步副本，不反向覆盖 Desk。改名与清空会同时清理已知 ID 别名。索引保存改名前的名称来源，避免清空后误读原生同步副本而恢复旧名字。

下一轮发送用户消息前，Codex 调用 `thread/name/set`，Claude 发送 `rename_session` 控制请求，Pi 发送 `set_session_name`。这些命令不启动模型回合，保存改名时也不额外启动引擎。原生不支持时记录 `name_sync=unsupported`，其他失败单独记录，不把 `/rename` 伪装成用户提示词。

侧栏、适配器列表与 `desk-msg` 共用名称投影。Agent 可用 `desk-session rename "新名称"`，指定其他会话时传 `--session`。原生名称同步在下一轮执行时进行，失败或 Binding 重建后可重试。名称不进入模型提示，不改变用户气泡，也不额外启动模型。

首轮受理时，同步保存首条真实用户消息的最多 24 字作为默认名。它只是自动命名失败、取消或超时时的回落，不覆盖自动名或手动名。名称优先级依次为：Desk 手动名、原生手动名、自动名、首条用户默认名、兼容旧名。

Codex 的会话列表优先读原生标题索引和上述默认名，不遍历 rollout。只有旧会话或外部导入会话缺少这两类索引时，请求线程才读取总计最多 256 KiB 的头尾窗口。旧文件中部的 `/rename` 由后台游标分批补扫，完成后续扫只处理新增内容。文件替换或截断时游标自动失效并重建。

依据：[名称实现](../desktron/lib/session_names.py)、[同步与通知](../desktron/lib/engine_adapters/session_naming.py)、[rollout 标题索引](../desktron/lib/term_titles.py)、[名称回归](../desktron/tests/test_session_names.py)、[有界索引回归](../desktron/tests/test_rollout_title_bounds.py)。

## 记忆与上下文

系统约定中的文档导航只提供实际存在的使用手册及适用的开发目录入口，不解析目录主题列表，文章正文按需读取。应用上下文另含完整项目模型、更新时间、约束入口和检索方式，经系统/developer 载体投递，不拼入用户原话。详见[上下文投递](context-delivery.md)。Pi 普通会话和 Shepherd 会话都关闭原生技能与上下文文件发现，以免技能清单和 `AGENTS.md` 重复进入系统提示，技能按需查找并直接读取 `SKILL.md`。recap 使用隔离分支，不回写主会话历史。新一轮到来或前序上下文失效时可以取消 recap，但不得为了提取记忆再次触发原任务工具。

## 专项入口

子代理见[执行与回投](pi-subagents.md)，延迟测量见[响应延迟](engine-response-latency.md)，认证见[认证提示与恢复](engine-auth-recovery.md)。

## 修改后的检查

验证首轮、续聊、取消、授权、进程退出、历史重开和断线恢复。引擎切换还要检查模型标签与当前引擎一致。

依据：[GUI 适配回归](../desktron/tests/test_engine_adapters_gui.py)、[事件契约](../desktron/tests/test_engine_ui_contract.py)、[recap](../desktron/tests/test_recap.py)、[mailbox](../desktron/tests/test_peer_mailbox.py)。统一测试命令见[开发与验证](development.md)。

## GUI 待发消息队列

队列与草稿由浏览器保存，页面级调度支持切换会话后续发；后端保留回合互斥和回执。详见[前端队列与旧数据迁回](frontend-message-queue.md)。
