# 任务完成条件与证据试点

旧任务保持原有行为。新任务或已有任务可以显式启用完成契约；一旦启用，Agent 不得随进展改变验收范围。试点每项任务最多一个代码仓交付提交、八个完成条件。用户仍可在面板直接调整任务状态。

## 声明完成条件

`contract.json` 示例：

```json
{"criteria":[
  {"id":"code","kind":"commit","summary":"修复已提交"},
  {"id":"tests","kind":"test","summary":"相关测试通过"}
]}
```

```bash
desk-task create --title '修复相对链接' --contract-file contract.json
desk-task show --task-id <id>
```

类型包括 commit、test、deploy、acceptance、artifact。按用户要求选择：代码修复任务不必包含上线；上线任务须列明目标端和运行验收。研究任务可只声明 artifact。不要把未来优化加进当前交付条件。

已有任务通过 `progress --contract-file` 首次启用，必须带 `--revision`。不自动给旧任务推导条件。

## 登记证据

证据使用独立 JSON 数组，不挤占进度摘要：

```json
[
  {"criterion":"code","summary":"修复两处行号点击", "ref":"提交地址或说明",
   "repo":"/absolute/repo","commit":"完整提交SHA"},
  {"criterion":"tests","result":"passed","summary":"15项通过", "ref":"/absolute/test-result.json",
   "subject":"与code一致的完整提交SHA"}
]
```

```bash
desk-task progress --task-id <id> --revision <版本> \
  --evidence-file evidence.json --done '修复并验证完成' --next '' --blocker ''
desk-task review --task-id <id> --revision <新版本>
```

Git 对象由本机核实；其他证据须声明 result 为 passed 或 failed，是 Agent 登记的验收报告，系统不将非空引用等同于已独立验证业务。证据仅含摘要与引用，不保存凭证、日志正文或敏感数据。代码任务的其他证据必须用 subject 绑定代码提交；新提交替换旧提交后，旧验证不会继续算作通过。跨会话发布沿用原任务 id 登记部署证据并 review。

## 自动核对

正常回合结束时，系统核对该回合实际更新的契约任务；全部证据齐备、交付摘要非空且 next/blocker 为空才关闭。取消、失败和交付状态不确定时不自动关闭。此过程不依赖 recap，也不新增模型调用。

有缺项则写入账本 review，下一次普通消息尝试发送时附带最多三项定向提醒。同一证据状态只提醒一次；斜杠命令不附加。用户调整、并发 revision 冲突和损坏状态不会被旧核对覆盖。recap 不再修改契约任务，只记录日志。

`show` 可查看条件、证据、revision 和缺项；尚未启用契约的任务依旧按原流程管理。试点未接入发布器自动采集，也不会扫描所有仓库或替 Agent 执行验收。
