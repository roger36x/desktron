---
name: delete-app
description: 从 Desktron 工作流中删除一个应用。删除无法撤销，必须按说明进行两次确认；不用于删除整个工作流或飞书妙搭应用。
---

# delete-app

从工作区面板移除一个应用。这是不可逆动作,按两步走,不要跳过预览。

## 边界(先确认)

- 一个应用可被 apply 到多个工作区(targets)。从某个工作区移除 = 只摘这一个 target;**当它最后一个 target 也没了(或对所有工作区移除)时,整条应用定义才真删**。脚本预览会告诉你是哪种。
- 应用按 `uid` 唯一标识(同名也不撞);优先用 uid 定位,没有再用 id。

## 怎么做

1. 弄清要删哪个应用、从哪个工作区移除:问清应用名/uid 和工作区 id;不确定就列一遍让用户指认。
2. **预览(dry-run)**:跑脚本不带 `--yes`,显示将移除什么、是"仅从该工作区移除"还是"整条删除"。给用户看。
3. **拿到用户明确确认**后,才加 `--yes` 真删。

```bash
# 预览(只看,不删)
bash .claude/skills/delete-app/scripts/delete_app.sh <domain> <uid或id>
# 用户确认后真删
bash .claude/skills/delete-app/scripts/delete_app.sh <domain> <uid或id> --yes
```

`<domain>` 用 `*` = 从所有工作区移除(整条删除)。删完面板刷新即不再显示。
