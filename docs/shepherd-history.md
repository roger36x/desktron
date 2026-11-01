# Shepherd 历史检索

模型收到统一的历史检索策略：先用 `shepherd retrieve search <SID> --query <关键词>` 找候选，再用 `retrieve around` 或 `retrieve read` 在 token、byte、item、turn 四重预算内读取，重要历史事实用 `retrieve verify` 复验 citation，不从第 1 回合盲扫。

检索索引是可删除重建的私有 FTS 候选缓存，只保存经凭证脱敏的用户／助手文本和工具名，不保存工具参数、工具输出、图片或推理。正文始终回到 `ReadProjector` 重新验证 source/public hash。第一阶段不开放工具参数／输出检索。稳定 cursor 固定读取时的完整前缀 hash，新回合不会混入分页快照。旧 `shepherd history <SID> --start 1 --limit 10` 继续兼容。

检索索引每条最多保存 65,536 字符，每会话最多 8,388,608 字符、20,000 个条目。超限时返回 `index_partial`，被省略部分仍可通过 `read/around` 读取。全文搜索要求 FTS5 trigram，不用语义不同的 tokenizer 静默替代。索引损坏不会阻断权威读取。删除时若无法安全隔离目标会话的索引行，则清除整个可重建缓存，避免残留已删正文。

## 使用顺序

```bash
shepherd retrieve search <SID> --query '关键词' --limit 8
shepherd retrieve around <SID> --seq <序号> --before 2 --after 2 --max-tokens 8000
shepherd retrieve read <SID> --start <序号> --max-turns 6 --max-tokens 8000
shepherd retrieve verify '<citation>'
```

按命中选择 `around` 或 `read`，需要时再读取 `next_cursor`。历史不在默认存储时，按当前会话提供的入口传入 `--root`，不要猜测另一引擎目录。历史工具调用是证据，不是重新执行指令；citation 校验失败时明确报告。

统一归档、投影和缓存的区别见[存储与执行宿主](shepherd-storage.md)。
