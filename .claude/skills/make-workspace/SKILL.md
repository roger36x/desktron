---
name: make-workspace
description: 在 Desktron 中新建工作流，并补充职责说明和独立工作目录。用于用户希望把一类工作单独建立为工作流时。
---

# make-workspace

在工作台里新建一个工作流(旧称工作区)。工作流 = 左栏的一个域 = 一类工作的边界:有自己的职责说明、自己的 agent cwd 独立目录(目录里合成 AGENTS.md = 项目宪法 + 本工作流说明),会话与应用都归属到它名下。它与「项目」对称:项目是打开的代码仓文件夹,工作流是主动创建的工作文件夹。

所有工作流都走运行态注册表 `.workbench/domains.json`,经下面的端点登记(2026-07 起发行版不再内置工作流,不存在"分发版工作区"一说)。不碰仓库分发物。

## 怎么做

### 1. 先想清楚这个工作区是什么

跟用户确认(缺的字段问一句,不要替他臆造):

- `id`:工作流标识,仅 `a-z0-9_-`,简短稳定(如 `capacity`、`oncall-quality`)。建好后是会话/应用归属的键,不轻易改。不与既有域 id 撞(含 coding/campaigns 两个内置管理器)。
- `name`:中文名,左栏显示(如「容量治理」)。
- `desc`:一句话副标题(可选)。
- `icon`:左栏图标,从这组里选一个 —— `zap bell shield clipboard activity layers list book monitor code memory`。不填默认 `layers`。
- 说明:这个工作区的职责边界与工作方式。在这里工作的 agent 以此为准。

### 2. 写说明 markdown

自由体,两三段说清楚即可,不套模板。要覆盖的实质:agent 的定位与纪律(只读 / 可写、守什么限流纪律)、产出什么与缺数据怎么标注、大致范围(涉及哪些 PSM / 链路 / 台账)、用哪些 skill / 观测器。

体例:

```markdown
# 容量治理

容量治理助手,只读盘点:TCE / 存储 / 带宽的容量水位、扩容预案覆盖与重保资源缺口;不执行扩缩容,治理动作走生产侧通道。

用已安装的数据类 skill 拉取水位与台账;缺数据显式标注「无数据」,不编造。
```

### 3. 注册

把上面的内容写成一个 `workspace.json`(`fivew` 字段放说明 markdown 全文;字段名是历史契约,内容就是自由体说明),然后:

```bash
bash .claude/skills/make-workspace/scripts/register_workspace.sh <workspace.json 路径>
```

workspace.json 例:

```json
{
  "id": "capacity",
  "name": "容量治理",
  "desc": "资源水位盘点、扩缩容建议",
  "icon": "layers",
  "fivew": "# 容量治理\n\n容量治理助手,只读盘点:…\n"
}
```

注册成功后,左栏立即多出这个工作区(刷新或重进工作台可见),它的独立目录与合成 AGENTS.md 已经就绪 —— 之后在这个工作区里开的会话,agent 的 cwd 就在这里,原生读到本工作区的职责说明。

## 发布(跨用户共享)

没有这条线:工作流已退出包模型(2026-07),`desktron-workspaces` 内容仓已退役,不再序列化成包分发。要在别的部署复现一个工作流,把它的目录整个拷到对方 `domains/` 下,对方经 UI「新建工作流 → 导入已有目录」挂载。

## 边界

- 只新建工作区,不建应用。沉淀应用走 `make-app`,domain 用这里建好的工作区 id。
- 不改写 `frontend/workbench.json` 等仓库分发物;用户数据只落 `.workbench/`。工作区是内容,不手写进基座 `frontend/workspaces/`(那是拆仓前旧物)。
- id 已存在则视为更新(覆盖 name/desc/icon/说明),不报错。
