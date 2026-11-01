---
name: make-arrangement
description: 在 Desktron 中创建定时或手动安排。先确认要执行的内容和时间，再按说明创建。
---

# 创建安排

安排是一级对象，默认由 Desk 当前引擎执行，不要求用户先创建 Agent。

## 流程

1. 从对话中确认“每次做什么”和触发方式。缺少必要信息时继续询问，不猜时间。
2. 触发方式只使用以下一种：
   - `manual`：仅手动，不需要时间。
   - `once`：指定日期，仅一次，需要 `--date YYYY-MM-DD --time HH:MM`，必须是未来时间。
   - `interval`：每隔一段时间，需要 `--minutes 1..10080`；从保存或恢复开始计时。
   - `daily`：每天，需要本地时间 `HH:MM`。
   - `weekdays`：工作日，需要本地时间 `HH:MM`。
   - `weekly`：每周，需要星期 `0` 至 `6`（周一为 `0`）和本地时间 `HH:MM`。
3. 在真正创建前，用一句话向用户复述执行内容和触发时间并取得确认。用户已在同一轮明确给全信息并要求立即创建时，可直接执行。
4. 调用脚本。默认不传 `--agent-id`，即由 Desk 执行：

```bash
python3 .claude/skills/make-arrangement/scripts/create_arrangement.py \
  --instruction "汇总昨天的项目变化，列出需要处理的问题" \
  --kind weekdays --time 09:00
```

每周示例：

```bash
python3 .claude/skills/make-arrangement/scripts/create_arrangement.py \
  --name "周五回顾" --instruction "汇总本周进展" \
  --kind weekly --weekday 4 --time 17:00
```

时间按所选运行端的本地时区解释；用户指定了其他时区时，先核对运行端时区再换算。脚本成功后报告名称、执行方式和时间。不要把定时运行另建成聊天会话；运行结果由安排页承载。

## 边界

- 只有用户明确指定现有 Agent 或飞书 Bot 时才传 `--agent-id`。通过 `/workbench/lark-bot/agents` 核对代号和运行端，不猜代号，不代用户创建 Agent。
- 绑定飞书 Bot 复用其引擎、目录与上下文配置；结果回到安排页，不会自动向飞书发送。
- 当前只支持本机 Desk API；`--backend` 用于选择已连接的运行端。
