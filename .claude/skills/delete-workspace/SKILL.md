---
name: delete-workspace
description: 从 Desktron 中删除一个工作流。删除无法撤销，必须按说明进行两次确认；只删除工作流，不用于删除其中的单个应用。
---

# delete-workspace

删除一个工作流(登记在 `.workbench/domains.json` 的域;工作流=主动创建的工作文件夹,与「项目=打开的代码仓文件夹」对称)。这是不可逆动作,按下面两步走,不要跳过预览。

## 边界(先确认)

- 所有工作流都是运行态条目,一律可删(2026-07 起发行版不再内置应急响应/风险管控等,原「分发版不能删只能隐藏」的限制随之取消)。
- 删除**只摘注册表条目**:工作流的独立目录(`domains/<id>/`)和它名下的会话**不会**被删。如果用户要连目录/会话一起清,单独说明并另行处理,别默认删数据。

## 怎么做

1. 先弄清要删哪个:问清工作区名/id;不确定就列一遍运行态工作区让用户指认。
2. **预览(dry-run)**:跑脚本不带 `--yes`,它会显示将删什么、以及"目录与会话不动"的提醒。把这个预览给用户看。
3. **拿到用户明确确认**后,才加 `--yes` 真删。用户没明确说"确认删"就停在预览。

```bash
# 预览(只看,不删)
bash .claude/skills/delete-workspace/scripts/delete_workspace.sh <id>
# 用户确认后真删
bash .claude/skills/delete-workspace/scripts/delete_workspace.sh <id> --yes
```

删完工作流从左栏消失(刷新或重进工作台)。
