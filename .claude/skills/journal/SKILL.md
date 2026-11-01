---
name: journal
description: 用户明确要求记录，或关键纠正、缺口与阻碍必须马上保留时，请求 Desk 立即保存本轮要点。
---

# journal

正常情况下，Desk 会在回合结束后自动保存要点。只有用户明确要求立即记录，或关键纠正、缺口与阻碍不能等待时，才执行：

```bash
python3 .claude/skills/journal/scripts/journal_append.py --request
```

该命令只发出本回合的保存请求，不接收正文。当前会话不得直接编辑记忆文件，也不得自行拟写保存内容。

较早的记录和当前项目情况由系统定期整理；普通任务不需要手工压缩或移动文件。
