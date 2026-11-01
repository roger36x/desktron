# Shepherd 检查点与压缩

压缩时机、摘要和工具循环由原生 Harness 决定。检查点包含原生工作上下文、覆盖 Turn 和 source hash，按引擎、codec、cwd、明确模型及 provider 隔离。调整 effort、窗口或提示不会丢弃工作记忆，当前系统提示由 Runner 重新安装。未解析的默认模型快照保存为证据，不自动复用。

三引擎均恢复”检查点＋其后完整回合”，早期归档不删除。Codex 从完整 replacement history 与后缀读取工作上下文，不能把 compaction 通知当作完整检查点。迟到的快照不得覆盖较新边界。

跨引擎接管使用 `portable-working-context-v3`（继续读取已冻结的 v2，不重写旧载荷。v3 新增 Pi 分支摘要和扩展消息）。它从已验证并提交的 Claude／Pi 原生检查点纯本地派生出”压缩摘要＋摘要后的完整语义尾部”，不再次调用模型。派生记录以原生 checkpoint hash、覆盖 Turn 与 source hash 锚定，正文保存在会话私有统一归档，Coordinator 仍只存引用和哈希。Codex 0.154 在全新私有 thread 中通过 `thread/inject_items` 导入，摘要使用原生支持的 `content_item_kinds=compaction.summary`，工具调用只作为已配对的历史证据，不执行。注入后回读完整批次，partial 或篡改均在真实用户请求派发前阻断。

旧存储升级后，首次恢复可读取升级备份指定的 retired Binding，但覆盖范围、历史前缀及原生 revision 必须一致，不启动旧线程，也不重跑工具。Codex 在同 provider、cwd、codec 范围内可使用其他模型生成的最新工作快照，由原生引擎验证不透明内容。更晚的可验证 Portable Working Context 可替代较旧的目标缓存。保留来源模型，仅在新回合成功后保存目标模型检查点，不因切换模型而展开全历史。旧来源只有在来源校验和准入允许时才使用完整历史；缺少兼容检查点时按[上下文准入](shepherd-admission.md)生成有界历史。

## 手动压缩

`/compact` 由 Desk 路由，三引擎均支持。Codex 在临时线程中恢复检查点与后缀，再调用 `thread/compact/start`。RPC 接受不代表完成，必须核对同线程通知和原生终态，并在停止 Writer 后导出完整 replacement history。成功的检查点与归档一起封存；取消、失败和原生 declined 不替换已有检查点。

Pi 的取消经 `abortCompaction` 确认。Claude 的命令 success 不覆盖较早的 `compact_result=failed`，相关版本记录和事件处理见[事件契约](shepherd-event-contract.md)。

上下文不足时见[准入与超窗恢复](shepherd-admission.md)；执行时间见[等待与超时](shepherd-execution.md)。依据：[工作上下文](../desktron/lib/shepherd/codex_working_context.py)。
