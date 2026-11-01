# journal：当前工作流水

> 每个工作流一份 `.memory/<工作流>/journal/journal.md`。journal 免闸、只追加、gitignored、可折叠；它是 notes 的原料，不是成品记忆。

## 谁写

- 正常会话：Desk 在回合收尾后用隔离 recap 支线自动写 `[recap:<engine>]` 摘要。
- 主动留存：仅在用户明确要求，或缺口、关键纠正、阻碍必须立即留存时，用 journal skill 设置即时 recap 请求；正文仍由隔离支线写。
- 不为普通完成态再补一条同义流水。旧 `[capture]` 指针只作历史兼容，不再新增。

## 形态

```text
# journal — <工作流>

## 日期索引
- 2026-08-28 → #d-2026-08-28

## 关键信息索引
- 决定：自动会话采集统一走 recap → #d-2026-08-28

---

## d-2026-08-28
15:20 [recap:claude]「会话标题」完成迁移；旧 capture 已退役；待同步三端。
```

追加由命令维护日期索引、时间戳和当天段落；`--key` 才写关键信息索引。

## 生命周期与边界

老日子先由蒸馏管家整理成 `.memory/<工作流>/notes/<slug>.md`，保留 journal 锚点作为 provenance；人核后才 compact，折叠会丢全文。成熟且代表当前状态的结论，由人审后合入 `sot`。

具名个人评价、政治判断、私聊与私有理由不得写进仓库；需要成品封存时经 memory skill 写 `_sealed`。
