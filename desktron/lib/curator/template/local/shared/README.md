# shared(local/shared · git 外)

`local/` 是用户全局工作容器(git 外,不上远端)。shared 是其中的可共享候选区:过 add 闸、受纪律,可在本地流通,但仍不上远端。

- git 外:gitignore + 提交期准入闸挡住,永不上远端。本目录除本说明外不进 git。
- 要对外发布、被其他用户引用,promote 过清洗闸(git 线)提交进某个子 KB(须选目标子 KB)。

## 归档:按 topic 分子目录

笔记按 topic 分子目录:`curator add --tier shared --topic <topic> ...` → `local/shared/<topic>/<id>.md`,不要平铺在层根。promote 默认沿用源 topic,跨到子 KB 时优先并入目标已有 topic。
