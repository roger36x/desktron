# 暂存区(temp band)

temp 是免闸暂存 band,在成品桶光谱之外,不是某个桶。agent 在此放临时 Markdown、工具输出、拉取的派生物、晋升前的草稿。

## 特性

- 免门禁:不需要 frontmatter,不受置信度纪律约束,不进 INDEX.md。引擎的 add、lint、gate 不扫此目录。
- 不进版本控制:除本说明文件外,temp 的内容被 gitignore 排除,且被提交期准入闸(`curator gate-staged`)挡住,永不上远端。
- 未成品:temp 兜"还不是成品"的东西。要长期保留,把它做成成品经 `curator add` 过 add 闸写入某个桶,进入光谱并受纪律约束。

## 常见内容

- 下载的外部材料(仓库 wiki 导出、文档抓取等),可重新获取,删后能再生。
- agent 的工作草稿、任务中间结果、晋升前的候选内容。

## 清理

随时可清,不留版本历史。`curator temp list` 看当前内容,`curator temp clean --yes` 清除(保留本说明)。适合长期保留的内容经 add 进桶,不要依赖 temp 当归档。
