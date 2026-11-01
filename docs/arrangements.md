# 安排调度

安排声明、调度领取与运行记录分别维护。配置和运行结果保存在本机。

## 声明

[API](../desktron/lib/arrangement/api.py) 提供 `/workbench/arrangements` 的增改删、test、run、toggle 及运行记录查询。声明由 [scheduler.py](../desktron/lib/arrangement/scheduler.py) 校验。

| kind | 主要字段 |
| --- | --- |
| manual | 无时间字段 |
| once | date、time |
| interval | minutes，1–10080 |
| daily、weekdays | time |
| weekly | time、weekday，周一为 0 |
| custom | unit 为 day/week/month；every、start_date、time；按需 weekdays、end_date |

时间格式 HH:MM，日期 YYYY-MM-DD；计算使用本机时区。这里不是任意 cron 表达式接口。

`agent_id=_desk` 表示默认 Desk，也可引用当前端已有 Bot。引用只复用执行配置，不创建消息触发或外发规则。

## 执行与存储

[store.py](../desktron/lib/arrangement/store.py) 原子写入 `.workbench/arrangements.json` 与 `arrangement-runs.json`。领取到期项时先推进 `next_at`，减少同一进程内重复触发。这不是跨机器分布式的 exactly-once 承诺。

执行记录保存 queued、running 和结束状态，当前上限 200 条。重启后悬空的 queued/running 记录改记为中断。试运行使用临时声明，不持久化安排本身。单次日期过期或自定义规则已结束时，启用操作明确拒绝。

系统安排通过各自维护模块呈现，不能将其与普通时间调度记录视为同一数据源。调度变更应覆盖暂停、过期、并发、重启中断与错误结果，不只验证下一次时间计算。

依据：[scheduler 回归](../desktron/tests/test_arrangement_scheduler.py)、[API 回归](../desktron/tests/test_arrangement_api.py)、[前端入口](../desktron/tests/test_frontend_arrangements.py)。
