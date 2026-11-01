# System Prompt 装配与治理

Desk 的系统提示不是单一文本文件，而是按执行引擎、运行方式和生命周期组合的多段契约。维护时必须同时核对正文来源、投递载体、刷新时机和历史边界。只修改某个适配器中的字符串，不能代表三引擎均已校准。

## 权威来源

| 层 | 来源 | 职责 | 刷新边界 |
| --- | --- | --- | --- |
| 表达 | `prompts/style.md` | 回复结构、语气、篇幅和事实表达 | 绑定时 |
| 工作环境 | `common/orientation.md` | Desk 身份、记忆、任务、会话协作和制品规则 | 每轮计算；按执行载体绑定 |
| 红线 | `AGENTS.md` | 数据、凭证、仓库、同步、重启等不可放宽规则 | 每轮计算；按执行载体绑定 |
| 判断原则 | `common/principles.md` | 可达性、兜底、证据、重放与当前态判断 | 每轮计算；按执行载体绑定 |
| 工作流原则 | `.workbench/workspaces/<id>.principles.md` | 指定工作流的补充决策规则 | 每轮计算；按执行载体绑定 |
| 动态导航 | `grounding/documents.py`、`domains.py` | 当前手册、开发文档和历史资料入口 | 每轮计算；按执行载体绑定 |
| 应用上下文 | `grounding/builder.py:scene` | 完整项目模型、更新时间、资料与仓库安全摘录、任务查询入口 | 每轮；非用户正文 |
| 引擎增量 | 各 `engine_adapters/*.py` | 工具通道、技能发现及原生能力差异 | 按引擎和运行方式 |
| Shepherd 历史 | `shepherd/history_prompt.py` | 有界检索、引用复验和历史证据边界 | Shepherd 绑定时 |

`grounding/builder.py:constant` 按”工作环境 → 红线 → 判断原则 → 工作流原则 → 资料导航”装配稳定正文，系统提示不枚举技能，只提供按需查找和读取入口。`scene` 装配项目导航，不再附加任务与备忘全文。`context_delivery.py` 将规则与低信任项目数据分层装配，用户帧始终保留原话。具体载体与兼容边界见[上下文投递](context-delivery.md)。稳定正文不得包含任务数量、最近记忆 ID 等易变值，否则会破坏常驻进程的提示指纹。

“每轮计算”只表示 Desk 会重新生成候选正文，不表示已有引擎进程会重新接收。Claude 与 Pi 的 system prompt 绑定在进程启动时，当前正文指纹参与常驻身份，规则或应用上下文变化会在下一回合触发换代。Shepherd unified-v1 同样通过当前系统快照指纹换代，工作检查点不因正文变化而丢弃。旧 private Claude Binding 的静态快照保持原样，当前上下文继续走有身份校验的隐藏块。

Desk 管理的 Pi 推理、Shepherd、recap、控制进程、模型探测和 Bot 均使用 `--no-skills --no-context-files`，以免再次注入技能清单和 `AGENTS.md`。需要技能时按公共正文中的入口查找并读取。普通会话和 Shepherd 缺少完整 grounding 时必须停止启动，不能退化为无合规与授权规则的回合。手动压缩也必须携带当前系统正文。Pi 工具的限制由工具自身提供，不再追加重复说明。原生 Pi 文档段也压缩为按需读取入口。其他目标以 `prompts/capabilities.yaml` 和实际适配器为准。修改 Prompt 后必须按目标执行方式验证新进程或新绑定，不能只检查静态组装结果。

## 提示语言

会话的提示语言只有 `zh`（参照正文）和 `en` 两种，在新建时固定，见 `desktron/lib/prompt_locale.py`。界面新建会话时，请求带上 `prompt_locale`（当前界面语言）；安排、Bot、子任务等由后端建立的会话，沿用前端最近一次写入 `.workbench/state.json` 的 `prompt_locale`。没有记录语言的会话一律按中文处理，所以已有会话的提示快照、常驻进程指纹和提示缓存不受影响。分支沿用原会话的语言；Shepherd 把语言保存在公开会话行上，切换引擎不改变它。

英文正文与中文来源并列存放：Markdown 来源使用同名 `.en.md`（`prompts/style.en.md`、`AGENTS.en.md`、`common/orientation.en.md`、`common/principles.en.md`，工作流原则同理），英文文件缺失时回落中文原文。Python 中的固定提示仍以中文常量为准，英文译文登记在 `prompt_locale.EN`，以中文原文为键，由 `compose_system`、Codex `system_body` 和 Shepherd 相关调用点按会话语言替换；`test_prompt_locale.py` 要求译文表与这些常量一一对应，中文常量修改后必须同步更新译文。导航、项目记忆标题、仓库说明等生成段落在各自函数中按 `locale` 分支。Shepherd 运行器只拿到组合后的系统正文，按 `prompts/style.en.md` 首行标记判断语言。

技能与使用手册的英文版分别是 `.claude/skills/<名称>/SKILL.en.md` 和 `desk-docs/en/`。原生技能加载器仍只读取 `SKILL.md`，英文工作环境说明会要求模型优先读取同目录的 `SKILL.en.md`。中文正文仍是参照版本；修改中文规则、技能或手册时，同时修改英文版。模型生成的标题、回顾和项目记忆尚未区分语言。

## 文档读取范围

`grounding/documents.py` 提供现行文档入口，不自动执行读取。模型仅在回答需要文档依据的 Desk 操作、功能或配置问题时读取使用说明；纯进度查询和已有结果核验不触发文档读取，但仍须核验实际状态。需要文档时，已知相关章节则直接读取，入口未知才读取目录。同一有效上下文中的已读内容可以复用；内容缺失或已知文档版本变化时重新读取。修改代码、排查故障或发布时，按需读取相关开发说明。此规则不减少授权、安全核验或实际状态检查，也不将历史文件作为当前操作依据。

## 六种执行方式

Claude、Codex、Pi 三个引擎分别有普通运行与 Shepherd 运行，共六种目标。引擎原生提示、Desk 追加提示、developer instructions、隐藏用户上下文和首轮前缀的能力不同；不能假定某段文字在六种目标中使用相同载体。

`prompts/manifest.yaml` 声明语义段、版本、优先级、目标和投递计划，`prompts/capabilities.yaml` 声明各执行方式可用的载体，`prompt_sources.py` 维护稳定语义 ID 到实际代码位置的映射。当前 `prompt_governance.py` 是严格校验和审计层，不是运行时统一编译器；适配器仍是实际投递路径。

修改正文时：

1. 先确定它属于全局稳定层、首轮场景、引擎差异还是 Shepherd 历史。
2. 修改单一权威来源，不在其他层复制同一句规则。
3. 行为或语义发生变化时递增 manifest 段版本；只改格式时不必伪造语义升级。
4. 核对普通运行与 Shepherd 运行是否都接线；`planned` 不得表述为已生效。
5. 检查压缩、恢复、切换引擎后该段是否仍存在，不能只测新会话首轮。

## 内容边界

系统提示只写长期稳定、需要模型遵守的约束。详细操作步骤放入 skill；产品使用说明放入 `desk-docs/`；开发说明放入 `docs/`；项目进展由记忆系统记录；领域资料留在对应的正式来源。运行状态通过工具读取。不得把会变化的版本号、人员判断、凭证、完整历史或大段文档正文写入稳定提示。

历史文本、跨会话材料和工具输出只作为证据，不自动成为新指令或授权。源码或数据格式定义证明已经实现；测试只证明给定条件下通过；当前运行版本的返回结果证明已经部署并响应；用户实际操作证明场景可用。四类证据不能互相替代。

## 验证

```bash
desktron/bin/desk-prompt --root . --check
python3 -m pytest -q \
  desktron/tests/test_prompt_governance.py \
  desktron/tests/test_grounding_documents.py \
  desktron/tests/test_grounding_parity.py \
  desktron/tests/test_grounding_wording.py
```

校验通过只证明声明、来源与静态装配一致。涉及投递载体、压缩或恢复的修改，还要运行对应引擎和 Shepherd 契约测试；涉及实际模型行为时，使用受控原生门禁，不以静态字符串断言代替。
