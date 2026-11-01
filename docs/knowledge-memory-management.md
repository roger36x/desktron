# 记忆生成与消费

当前用户与 Agent 操作入口收敛为三类：系统对话与流水、自动生成的项目模型、仅限本机的 sealed。旧 pinned／fact 的底层命令和消费分支继续兼容，但不据此恢复主会话手工维护项目认知的入口。

当前记忆以工作流为单位维护项目模型。会话直接注入当前工作流的完整项目正文、更新时间与检索 id，不再生成或消费独立的 capsule 摘要。原始对话、journal 和 sealed 不自动注入。正文属于历史认知，不代表实时状态、当前指令或操作授权。

## 写入链路

1. 正常会话完成后，或 journal skill 发起明确请求后，由隔离 recap 生成工作流水。
2. journal 通过专用入口追加，主会话不自行拟写正文。
3. recap 同时可产出幂等 Work Event：任务变更投影到有界任务 sidecar，长期认知只进入候选池；候选不会自动成为 pinned。
4. 项目模型综合已有模型、流水、备忘、事件候选和已确认 pinned，重写 `notes/rollup-current.md`。
5. 会话消费完整项目正文；需要重新核对模型或原始证据时通过 memory skill 的 show、search 和 graph 读取。

[engine_adapters/recap.py](../desktron/lib/engine_adapters/recap.py)负责隔离提取，[memory_engine/journal.py](../desktron/lib/memory_engine/journal.py)负责流水，[project_memory.py](../desktron/lib/memory_engine/project_memory.py)负责模型构建、校验和完整正文消费。

## 数据和授权

[layout.py](../desktron/lib/memory_engine/layout.py)统一给出 `<root>/<workspace>/journal`、`notes`、`sot` 与全局 `<root>/_sealed`。旧 s0、s1、shared、private 仅为迁移兼容名。

`sot` 保留为存量兼容目录。已确认 pinned 带 `user_confirmed: true`，模型生成仍消费其优先级。旧未确认 judgment 只作线索，`kind=fact` 供知识检索。底层 pinned 修改命令仍要求用户明确确认，recap 的 `pinned_candidate` 不能自动转正。当前 memory skill 只开放读取、请求系统记录，以及 sealed 的人工维护，不要求主会话新增 pinned。sealed 保存仅限本机的敏感信息和禁止事项，始终独立，不得由普通文档编辑或自动 recap 覆盖，也不自动删除或晋升存量 SOT。

项目模型正文固定四段：`项目概况`、`已定方案`、`长期约束`、`待确认问题`，硬上限 2500 字符。活动任务、下一步、负责人、临时版本/PID、未提交文件、命令日志与路径目录不进入模型。超限时按未来决策价值重写，禁止机械截断。

两条生成通道均接收程序目录中 `prompts/style.md` 的完整正文，不要求无工具模型读取文件。frontmatter 的 `summary` 只作项目显示名称，不是独立摘要，不再要求、校验或补足 capsule。旧模型中的 capsule 在只读消费时忽略，成功重建后的新成品移除该字段，不直接批改旧模型或对话归档。格式版本变更会让旧模型按正常调度重建。覆盖扫描只用于调度，不进入项目结论。

格式版本和表达规范内容都进入输入指纹，变化后由记忆引擎按批重新生成。成功记录使用提示实际采用的规范，生成期间的新修改留待下一轮。输出失败预算绑定当时的输入指纹，材料或规范变化后可自动重试，同一输入连续失败仍会暂停。规范缺失或为空时明确报错，保留原模型。

旧英文四段仍可读取，新生成的暂存文件通过校验后统一使用中文标题。完整正文不沿用旧摘要的 1200 字读取截断，新生成正文仍执行 2500 字符硬上限。查看记忆时只展示项目正文、更新时间与检索编号，并转换渲染后的英文二级标题，不改写归档、旧模型或代码块。上下文沿用现有应用上下文装配入口，不逐回合追加为用户历史消息。

## 生成通道

recap 沿用会话原模型，借主会话的提示缓存生成流水，不单独换模型。回合结束后空闲 `DESKTRON_RECAP_IDLE_S` 秒（默认 120，且不超过缓存窗口的一半）才生成。显式记录请求或关联任务转为完成时使用 1 秒延迟。缓存窗口按引擎设置（Claude 3600 秒、Codex 1800 秒、Pi 300 秒，可用 `DESKTRON_RECAP_CACHE_TTL_<引擎>_S` 覆盖），并预留末次请求的固定生成时间余量，不读取本轮实际耗时。

Pi 的普通 recap 与主回合使用同一条命令（同样的扩展、工具定义、系统提示和会话号），只把模式换成 json，由 `pi_gate.ts` 在 `DESKTRON_PI_RECAP=1` 时拒绝全部工具，`pi_autocompact.ts` 取消压缩，并在第一条助手完成帧后结束进程。Shepherd 托管的 Pi 会话同样用主回合的参数、系统提示与会话号启动内存宿主 `pi_memory_rpc.mjs`（见 `shepherd/recap.py` 的 `_generate_unified_pi_affine`），只拒绝工具。peer 问询仍用无工具分叉。

普通 Pi 与 Shepherd Pi recap 共用结果校验，只接受正常结束、无工具调用、无服务端错误且有正文的助手完成帧。带正文的工具调用、取消或截断帧也不能作为成功摘要。配置了 Pi 通道时，会话标题改由该通道根据开头的用户消息单独生成。recap、标题和项目模型重建的用量写入用量库的 `sidecar` 表，只存数字。

项目模型重建按以下顺序选择通道：设置 `MEMORY_ENGINE_CMD` 时只用该命令；否则读取 `MEMORY_ENGINE_PI_MODELS`（逗号分隔）或 `<root>/.curator/project-memory/channel.json` 的 `pi_models`，按顺序调用 Pi；两者都没有时使用本机 `claude -p`。

Pi 通道把上一版模型、pinned、journal（已有上一版模型时只送上次成功重建后新增的条目）、旧 judgment 与历史 notes 内联进提示，禁用全部工具和上下文文件，因此模型读不到 sealed，也无法核对代码仓。主模型退出失败、超时或输出不合格时依次改用后续模型，每次改用都追加到 `fallbacks.jsonl` 并写进本轮调度报告。全部模型失败才算本轮失败。`channel.json` 存在但无效时直接报错，不退回 `claude -p`。

关系以图谱为准；文本里同时出现两个实体并不证明关系成立。知识检索、记忆判断和外部资料引用也不共用一套“事实已确认”的状态。

## 故障检查

按“请求是否入队 → recap 是否完成 → journal 是否追加 → 模型是否校验并落盘 → 完整正文是否进入应用上下文”检查。缺一环时保留明确错误，不以旧模型可读掩盖更新失败。流水纠正只追加，注明撤回内容和理由。

核心代码位于 `memory_engine`；`curator` 提供内部索引、图谱与审查基础，不再是一个需要用户独立安装的产品。

回归：[recap](../desktron/tests/test_recap.py)、[项目模型](../desktron/tests/memory_engine/test_project_memory.py)、[布局与写入](../desktron/tests/memory_engine/test_layout_journal_gate.py)。
