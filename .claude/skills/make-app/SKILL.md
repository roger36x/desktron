---
name: make-app
description: 把当前会话形成的做法整理成 Desktron 应用，加入指定工作流或桌面。用于用户明确要求“做成应用”时；不用于飞书妙搭应用。
---

# make-app

把 vibing 产出沉淀为工作区应用。应用 = 资产的消费包装，矩阵构成一个工作区的解决方案。

## 两类应用（只分这一刀，不预设更细类型）

- **agentic**：调模型跑一段处置。用 5W 声明 + 参数表单，执行路径确定。
- **non-agentic**：一个本地 html 页，确定性运行，不调模型（计算器/看板/嵌入页/观测面）。

## Agent 可操作性（每个应用都必须选择）

应用是否调模型（agentic）与应用是否能被 Agent 操作（agent operability）是两条独立轴。
创建或更新 app.json 时必须声明 `agent_operability`：

- `none`：本版不提供结构化控制，同时填写具体的 `agent_operability_reason`。不能只因实现麻烦默认选 none；应说明缺少哪类观察或动作 adapter。
- `L1`：可打开、可观察。
- `L2`：增加本地、可撤回修改。
- `L3`：增加经过确认闸的外部写入。
- `L4`：增加后台执行、事件等待与恢复。

选择 L1-L4 时必须提供同等级 `agent_control`，遵守 `desk-docs/reference/app-control.md` 与
`schemas/agent-control.schema.json`；不得开放任意 JavaScript，不得把凭证或敏感值暴露给模型。
注册接口会强制校验，缺少选择、none 无原因、等级不一致都会拒绝。

## agentic 应用：5W 声明

字段（who/what/how 必填，when/where/params/skill 可选）：

- `who`：角色定位与纪律（"你是…，只读，守 X 限流纪律"）
- `what`：产出规范（输出什么、什么格式、标注什么）
- `how`：执行步骤（固定流程骨架，可引用 skill 命令；用 `${参数名}` 占位）
- `when`：触发，manual（默认）/scheduled/event
- `where`：范围约束，含 `${参数名}` 占位
- `params`：占位的表单声明，每项 `{name, label, required?, default?, enum?[], placeholder?}`
- `skill`：引用的 skill 名（须存在于 .claude/skills/）

**校验硬约束**（注册时强制，不满足直接拒）：
- `id` 仅 `a-z0-9_-`；`label` 非空
- `agent_operability` 必须是 `none` 或 `L1`-`L4`；none 必须写原因，L1-L4 必须提供同等级 `agent_control`
- 每个 param 的 `${name}` 必须在 who/what/how/where 文本里真实出现
- `skill` 若填，必须是已存在的 skill

执行时：参数填进占位 → 【角色】【范围】【步骤】【产出】四段组装发给 agent。

## non-agentic 应用：一个 html 页

写到 `.workbench/pages/<id>.html`（本地，覆盖同名发行版页）。
- 底色用 `transparent`（融入悬浮层毛玻璃）；深色文字配色见现有 pages 样例。
- 纯前端：计算逻辑、表单、SVG/canvas 图都行；**不要**外发数据到非内网域、不要 eval、不要读凭证。
- 真实使用动作发生时（如点了计算、成功拉到数）可 `window.parent.postMessage({type:'app-used'},'*')` 上报，供使用计数（可选）。

## 注册（让应用出现在面板）

1. non-agentic：先写好 `.workbench/pages/<id>.html`。
2. POST 注册（domain 用一个**已存在**的工作区 id：分发版 incident/risk，或 make-workspace 新建的工作区 id，或 `_desktop`。工作区还不存在就先用 make-workspace 建，本 skill 不建工作区）：

```bash
bash .claude/skills/make-app/scripts/register_app.sh <domain> <app.json 文件路径>
```

app.json 例（agentic）：
```json
{"id":"meeting-review","label":"会议回顾","desc":"整理指定日期的会议与待办","icon":"clipboard",
 "agentic":true,"agent_operability":"none",
 "agent_operability_reason":"通过会话执行，没有独立的可观察与操作界面","when":"manual",
 "who":"你是办公助理，只读检索日历和待办，不替用户修改外部状态。",
 "what":"按时间顺序列出会议、未完成待办和需要跟进的事项，并标注来源。",
 "how":"用已安装的日历与任务能力查询 ${date}，合并重复事项后输出。",
 "where":"目标日期=${date}。",
 "params":[{"name":"date","label":"日期","required":true,"placeholder":"2026-08-31"}]}
```

app.json 例（non-agentic）：
```json
{"id":"my-calc","label":"我的计算器","desc":"算个临时值","icon":"monitor","agentic":false,"page":"my-calc",
 "agent_operability":"none","agent_operability_reason":"尚未提供计算表单观察与输入 adapter"}
```

注册成功返回一个 **uid**（形如 `作者.短名.随机`，全局唯一、稳定、不变）。告诉用户这个 uid，并记住它：
- **优化/迭代已有应用**：在 app.json 里带上原 uid（`"uid":"作者.短名.随机"`）再注册，会按 uid 覆盖更新同一应用（保留 uid/作者/创建时间），而不是新建。会话里用户说"优化 X 应用"时，用 uid 定位，不靠名字（名字会改、会重名）。
- **新应用**：app.json 不带 uid，注册时自动生成。

注册失败会返回具体错误，按错误修正 app.json 再注册。

## 应用产生的数据

应用产生的结果默认留在应用或原平台，不直接写入记忆文件。项目进展和决定由 Desk 的正常记录流程更新；必须立即保存本轮要点时，按 `journal` 的说明发出请求。只有敏感信息或禁止事项需要留在本机时，才按 `memory` 的说明写入。

应用不得保存凭证、open_id、chat_id、飞书 token、具名个人评价或其他不必要的个人信息。

## 提供给其他用户

本 skill 只产出**本机** App（落运行态 `.workbench/`）。要让所有新安装的 Desk 自带它，使用 `contribute` skill 将稳定 uid、清单、页面和依赖提升进 `desktron-infra/desktron`，经人审后随下一版发行。

## 纪律

- 不替用户决定"做成应用"，先问清楚：解决什么场景、输入输出、是否只读/调模型。
- 一个应用只做一件事；与已有应用重复就别造，改已有的。
- id 用短横线小写；label 用中文短名。
- icon 不填默认 `monitor`；可选值同 make-workspace：`zap bell shield clipboard activity layers list book monitor code memory`。
- 创建阶段只写 `.workbench/`；只有明确执行官方提升时，才由 `contribute` 把它迁入 `frontend/`。
