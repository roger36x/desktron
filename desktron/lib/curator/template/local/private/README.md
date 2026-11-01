# private(local/private · git 外 · containment)

`local/` 是用户全局工作容器(git 外,不上远端)。private 是其中的敏感 containment 区:过 add 闸、带 frontmatter、受置信度纪律,但内容最私有。

- git 外:gitignore + 提交期准入闸挡住,永不上远端。本目录除本说明外不进 git。
- containment:出桶(promote 到 shared)必须脱敏,引擎打印脱敏清单并要求 `--confirm`。脱不掉的红线内容(对个人的评价、私聊摘要、政治判断)永远留在 private,不外移。

## 归档:按 topic 分子目录

笔记按 topic 分子目录:`curator add --tier private --topic <topic> ...` → `local/private/<topic>/<id>.md`,不要平铺在层根。id 全局唯一,与子目录无关。lint 对直接躺在层根的笔记给 WARN。
