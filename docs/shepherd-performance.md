Shepherd 历史回读与副本优化（2026-09-13）

本次处理 Shepherd 自身增加的历史读取、解析和对象复制成本。目标模型接收上下文的预填充成本不计入此项；迁移预算和状态存储重构不在本次改动内。

## 只读与启动准备（2026-09-15）

状态读取移除冗余复制，Pi 热命中免写 bootstrap；保留来源和编码校验、迁移锁、Snapshot 冲突检测及派发栅栏。同进程合成对照：千轮读取 33.6→21.2ms；Pi 4MiB 热准备 24.4→4.0ms（不含历史投影）。这是局部收益；常态不加计时，按需采样。

## 原生常驻 Host（2026-09-14）

同一 Binding 的连续正常回合可复用原生 Host：Claude／Pi 使用 Shepherd native 池，Codex 使用 app-server 池，省去每回合 Node、SDK、Provider 或 app-server 初始化。统一归档、检查点和 Manifest 仍是持久 SoT，常驻内存只是一份可丢弃的加速副本。仅当 Binding、模型参数、系统提示、cwd、权限模式及历史覆盖边界完全相同时复用；跨引擎补史、参数变化、失败、中断和边界不连续均淘汰后冷启。Host 空闲 30 分钟回收，并接入共享常驻进程预算；`DESKTRON_SHEPHERD_PERSIST=0` 可关闭 native Host 常驻。

Claude MemoryStore 使用单份序列化 entry owner，SDK `load()` 边界才生成新对象；Resident Runner 用 size／suffix 接口收尾，不再每轮复制与哈希整份历史。新原生 checkpoint 提交后立即淘汰旧 Host，下一轮只恢复 checkpoint 与尾部；Claude Host 在已提交回合后超过 `DESKTRON_SHEPHERD_HOST_RSS_MB`（默认 384MB）也仅换代 Host，不切换引擎。RSS 无法采样时不回收，正在运行的 Host 永不因预算回收。Claude 会话原有的自动压缩窗口档位继续显式传给冷热 SDK 路径的 `--autocompact`，内存治理不暗改用户的上下文窗口。

本地离线长历史样本（12,001 条 entry、序列化 JSON 25.1MB，加载 SDK 但不发模型请求）中，空闲 Claude Host 进程树从旧实现约 262.6MB 降到 217.8MB，下降约 17%；MemoryStore 自有的 4,000×2KB 样本从约 17.2MB 降到 8.5MB。各测一次，进程树数字包含 Node／SDK 基线，不代表线上分位数；真实回答中的 SDK、工具和请求状态仍会增加 RSS，因此不能用该结果取消软上限。

ReadProjector 按连续的同来源会话划分批次，上限 32 回合或 262,144 个估算 token。单回合不可拆分，允许超过估算预算；这不是整体内存硬上限。源 SDK 仍可能加载完整原生文件，目标写入也仍持有本次迁移的公共历史。

- Pi 每批启动一次 SDK 读取桥。只返回所需来源条目，不生成无关的模型 context/status。每回合一个 JSONL 帧，管道使用背压，避免生成和捕获整批 JSON 字符串。
- Codex 每批复用一条私有回读连接、一次 rollout 解析。连接在返回或异常时关闭；不跨批次或切换栅栏缓存来源。
- Claude 每批加载一次 SessionStore，对所需 receipt 前缀按顺序计算哈希，复用计算结果。
- 批次读取接口交出本次新建的公共对象，Projector 直接接管；旧 Connector 若返回共享对象，仍在边界复制。Claude 编码直接接管自己新建的消息；Codex 投影不再先复制整个原生对象，条目归一化也不再做 JSON 序列化／解析往返。嵌套可变值在公共输出边界仍隔离。
- 预检对象在正式追平前释放，避免同时保留两份完整投影。前后仍各做一次来源与公共哈希核验，防止栅栏前后来源变化被缓存掩盖。
- 后台摘要传入取消信号，在回合／批次边界检查；正在执行的一批仍受读取超时约束。

SourceResolver.resolve_batch 的输入只读；输出 public 的所有权交给调用方，Connector 不得跨调用保留或共享其可变节点。归一化 native view 仅供内部只读使用。持久化格式、来源哈希、回合顺序、幂等写入和 Writer fence 保持原契约。

| 离线样本 | 原协调链路 | 批次协调链路 |
|---|---:|---:|
| 20 回合 Pi→Claude 切换 | 15.428 秒 | 1.001 秒 |
| Pi 来源读取桥调用 | 40 | 2 |
| Python 峰值分配 | 907,548 B | 767,111 B |

两条路径使用相同的当前 Connector／Codec；旧路径加载 HEAD 中的 Projector、SourceResolver、SwitchRunner。原生历史 102,465 字节，均完整导入 20 回合、目标成为 Producer、pending 清空。各测一次，启用 tracemalloc；该内存数字只统计 Python 分配，不包括 Node、SQLite 或进程 RSS，不代表线上分位数。未调用模型。

验证包括：批次与单回合结果一致，receipt／marker 混合读取，Claude 多前缀及逆序查询，Codex 协议，批次预算与取消、栅栏后篡改拒绝、旧接口共享值隔离、编码不修改输入。模块门禁通过 110 文件、2,120 项测试，24 项跳过。真实 Codex 批次协议另在隔离临时线程通过验收，未复制用户配置或调用在线模型。
