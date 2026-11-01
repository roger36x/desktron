# 目录和数据归属

`REPO_ROOT` 在后端表示工作台运行根，不保证是源码仓。源码分支、workspace 的 Git 分支和正在运行的 payload 版本是三个不同概念，必须分别判断。

| 层 | 默认位置或来源 | 归属 |
| --- | --- | --- |
| 源码 | 当前 Git checkout | 程序、测试、文档与构建输入 |
| 安装程序 | `~/.local/share/desktron/home` | 同步或更新得到的产品内容 |
| 工作台根 | `~/.local/share/desktron/workspace` | 项目目录、注册表和用户材料 |
| 引擎与服务状态 | `~/.local/share/desktron/data` | 会话附加状态、mailbox、App Control 等 |
| 本机配置 | `~/.config/desktron` | 凭证、远程访问令牌等私有内容 |
| 外部代码仓 | 工作流绑定的目录 | 代码直接在对应仓库修改 |

路径可由启动配置覆盖，排障先读取实际进程参数。不要将开发机绝对路径写入发行配置。

## 工作台文件

[workbench.py](../desktron/lib/workbench.py)管理 `.workbench/domains.json`、`apps.json`、`state.json`、`hidden.json`、`installed.json` 等运行注册表。`frontend/workbench.json` 是官方声明，运行时不改写它。

任务标题、状态与任务流放在各工作流的 `TASKS.md`；模型维护的轻量进展、完成条件和关联会话放在私有 `task-ledger.json`，任务详情合并读取两者。长篇补充资料可选关联备忘录，未填写时不创建空文档。安排声明和记录为 `.workbench/arrangements.json` 与 `arrangement-runs.json`。Bot 声明在 `.workbench/agents/<id>.json`，会话映射在 `_state.json`，凭证不写声明。

Git 知识库的注册信息和 clone 分离。记忆目录由 [memory_engine/layout.py](../desktron/lib/memory_engine/layout.py)计算，禁止在调用方猜测旧层级路径。

## 会话与执行证据

正式 Shepherd 的统一归档位于私有数据根的 `shepherd/archive/<SID>/history.sqlite3`。Turn Index、输入、检查点、请求与公开投影分别承担执行证据和展示职责；普通引擎历史不自动并入。早期试用空间继续隔离，路径见[Shepherd](shepherd.md)。

子代理回投属于运行态，不能作为程序内容同步。任务的 `TASKS.md` 是工作流事实源；私有 `task-ledger.json` 保存 revision 和有界当前快照，不替代 journal。

## 普通引擎私有存储基础（尚未接入运行通路）

[engine_profiles.py](../desktron/lib/engine_profiles.py)提供 `EngineProfile`：在调用者指定的 `DATA_HOME/engine-profiles/<engine>/ordinary` 准备私有目录、代次与作用域；返回经验证的启动环境、Pi 显式 session-dir 和缓存身份。它不复制登录凭证、不改全局 HOME，也不自动接管旧 managed HOME。存储准备失败必须拒绝，不能回落全局。当前仅实现 POSIX 目录描述符后端；Windows 明确拒绝，不以弱化检查替代 ACL 实现。CLI 在校验返回后仍需由执行宿主控制 Writer 与目录租约，校验不能替代操作系统沙箱。

[session_ownership.py](../desktron/lib/session_ownership.py)按 Desk SID 登记 engine、scope、generation、native ID 与 SHA-256 revision；提供显式归属列表、revision CAS 和无正文墓碑。首次派发前可登记 `reserved` 创建栅栏；未确认创建不进入已归属列表，重开后也不得自动续聊或重新派发，只有核验对应原生文件后才能发布为 `owned`。`DATA_HOME` 中的根锁与 `session-ownership-anchor.json` 锚定 `session-ownership` 目录身份；已初始化索引丢失、JSON null、损坏或目录被替换均拒绝，不重建空库。首次初始化中断后也不自动接纳半成品。墓碑仅是元数据栅栏，尚不承担原生内容清理。

[native_session_store.py](../desktron/lib/native_session_store.py)仅从已登记 profile 读取有界只读原生快照，核实唯一原生身份、文件与路径稳定性和内容哈希。它不按 cwd 接管会话、不扫描外部 HOME、不选择最新的重复记录，也不启动引擎。快照的头部身份核验不代表完整原生格式或 resume 能力通过。

[ordinary_engine_runtime.py](../desktron/lib/ordinary_engine_runtime.py)把 profile、归属与快照组合为启动／认证上下文及非阻塞 Writer 租约。根目录锁 inode anchor 与 profile 中的独立初始化回执共同固定租约身份；锁与 anchor 同时丢失也视为损坏，不重建。新建 Claude/Pi 先预登记原生身份；续聊必须读取已登记私有快照；Writer 持续持有 profile 级租约，持久化稳定后以 CAS 发布新 revision。异常退出保留未确认状态，不自动重发。Claude/Codex 登录及状态命令使用实际执行 profile，不共享可写认证文件；Codex 创建回执、Pi 登录及并发续期尚未接入，明确拒绝对应操作。租约不替代原生 Writer 终态证据，常驻宿主必须在存活期间持有，不能在回合间释放。

普通适配器基类已有显式 `ordinary_storage_runtime()` 桥接入口，按实例的数据根构造并缓存 runtime；Shepherd 隐藏 Worker 不得进入普通 scope。该入口**不是上线开关**：引擎工厂没有启用它，普通派发、列表、搜索、恢复与设置页登录仍未切换。上述模块及离线测试仍是开发阶段实现，不能据此认定现有会话已经隔离。认证、启动宿主、旧会话迁移和显式导入接入完成并验收前，不执行真实历史迁移或清理。来源不明记录保持原样；已有外部副本须另行清理。

## 分发与同步

[scripts/payload-excludes.txt](../scripts/payload-excludes.txt)是发行边界：排除源码开发材料、测试、用户注册表、记忆及凭证，保留程序、官方资产、技能与 `desk-docs/`。

`sync-home` 同步程序内容，不是复制所有用户状态。工作流资料、Bot 声明、登录凭证和引擎历史有各自来源。看到终端提示 `git:main` 时，应核对终端 cwd，不能据此认定上传代码来自 main。
