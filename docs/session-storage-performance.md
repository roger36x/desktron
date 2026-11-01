# 会话读模型与持久化性能设计

## 目标

高频 UI 读取不得反复扫描权威存储；写入优化不得降低崩溃持久性。会话数据分为三层：

1. **权威层**：Shepherd archive、引擎 transcript/rollout、适配器 JSONL；
2. **物化读模型**：侧栏目录快照、JSONL turn-offset sidecar；
3. **动态覆盖层**：busy、unread、resident、agents，只从当前进程状态生成。

读模型均可删除重建，不成为第二事实源。版本、inode、mtime 或 size 不匹配时必须重建，禁止静默返回旧数据。

## 会话目录

`SessionCatalog` 构建静态行并保存版本。适配器索引在一次扫描开始时转成：

- `adapter_sid -> row`
- `engine_sid -> (adapter_sid, row)`

后续映射和去重均为 O(1)。`term-meta.json` 每次构建只读取一次。目录由写路径显式失效；为兼容 Desk 外部生成的 transcript/rollout，增加低频来源指纹探测。请求命中同版本时只复制静态行并叠加动态状态。

`GET /session` 同样一次读取 term meta，不再每行重开同一个 JSON 文件。

## UI 历史

适配器 JSONL 旁放置 `<sid>.jsonl.turn-index.jsonl`（格式版本 2），记录源文件身份、消息 offset、真人回合 offset 和源文件戳。初次或失效时单次扫描重建；稳定读取按 offset 直接 seek：

- 首屏读取最近 `SESSION_WINDOW` 个回合；
- 向前分页按 `have/limit` 定位目标字节区间；
- trim 用流式后缀复制，不再 `readlines()` 整文件驻留。

append、rewrite、trim 和索引更新共享会话锁。全量重建采用临时文件 + replace；正常 append 只追加一条 offset 与新 footer，写入量不随历史长度增长。旧 footer 是崩溃恢复检查点，加载时只接受最终 footer；尾部损坏、版本或源戳失配都从权威 JSONL 重建。索引不 fsync，因为丢失后可重建。重建及快照读取最多重试 4 次，源文件持续被外部替换时显式失败，避免无限递归。

## UnifiedArchive

POSIX 为每个 archive 路径复用进程级有界空闲连接槽（最多 16 个），同一连接只初始化一次 schema 与 PRAGMA；同一槽串行访问，跨 Conversation 仍可并发。槽全忙时使用一次性连接。Windows 只在使用期间共享槽，最后一个使用者退出即关闭连接，不保留空闲句柄。fork 后子进程丢弃继承的槽目录并重新建立连接；文件 inode 替换和进程退出也会失效旧槽。`application_id`/`user_version` 拒绝误库及未来 schema，`synchronous=FULL` 保持不变。

移除与 `UNIQUE(turn_id, ordinal)` 重复的显式索引，新增 `turns(identity, terminal)`。`finish()` 在一个写事务内读取并验证事件、写 terminal/cursor/checkpoint，避免先读后写的第二连接和竞态；返回已验证 revision，供 terminal locator 复用。

不采用定时延迟提交。每个原生 archive frame 仍在 ack 前完成 FULL commit；Codex 单 item 的上游粒度另行通过协议批次演进，不能以丢失崩溃证据为代价合并。

## 非目标

- 不降低 SQLite 或权威 JSON 的持久性等级；
- 不把 TTL 当作正确性来源；
- 不改变外部 API 响应结构；
- 本轮不重构全局历史搜索索引。该索引需要 staging tables/代际切换后再缩短 writer lock，不能在现有表上边投影边暴露半成品；
- JSONL 锁与 `term-meta.json` 原子 mutation 是单服务进程内协议，不承诺多个 Desk 进程并行写；
- 新 Shepherd 会话保留完整公共投影；旧版本已经物理裁掉的 25 回合之外内容不能由 sidecar 恢复，本轮不自动从 archive 重投影；
- `have/limit` 保持现有兼容协议，不新增稳定 cursor。
