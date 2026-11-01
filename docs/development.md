# 开发与验证

在源码仓工作前读取 AGENTS.md，并盘点整个共享树。未提交改动可能属于其他会话；按文件和 hunk 确认范围，不覆盖或批量恢复。

## 本地入口

最低 Python 为 3.11。源码开发可显式使用合格的本机原生解释器；发行安装固定使用 Desk 托管 Python 3.12.14。`desktron prepare` 准备托管 Python、固定的 PyYAML/Pillow 与兼容架构 Node，不改系统解释器。测试环境在所选解释器的虚拟环境中执行 `python3 -m pip install -r scripts/dev/python-test-requirements.txt`，安装固定的运行依赖与 pytest，不能借用另一版本 Python 的 site-packages。CI 在 runner 临时目录复用产品的 SHA-256 校验安装入口准备 Python 3.12.14；不依赖 `actions/setup-python` 的 macOS 构建供应。离线 Pi SessionManager 存储测试需要 SDK：CI 另在临时 npm prefix 安装固定的 Pi 1.0.2（禁止安装脚本），不登录、不调用模型；真实引擎与桌面用例仍默认排除。

从源码仓根使用启动器。以下命令分别做声明校验、依赖检查和启动；检查环境后再启动需要的实例，避免重复占用端口。

```bash
bash desktron/bin/desktron validate
bash desktron/bin/desktron deps
bash desktron/bin/desktron ui
```

运行目录与产品源目录分离，见[数据归属](runtime-data.md)。不要通过直接 import ui_server/deskcfg 来探测状态，它们依赖启动参数和进程环境。

## 测试

日常入口按风险分四级，桌面浏览器验收单独显式运行。`full/release` 先执行带精确审核清单的公开树扫描；原始扫描仍可单独运行以查看全部待审命中。清单按路径、SHA-256、规则和理由记录非敏感样例及二进制安全审核，不证明图标或其他资源的再分发许可；内容变化、未审文件及不安全路径仍阻断。各入口均先检查 Python 下限、前端静态规则及真实后端隔离启动（含 `/desk-probe` 身份核验、页面入口和 `shepherd status --offline`（仅连接器声明，不启动引擎协议探测））；后端导入失败必须在进入 pytest 之前阻断。隔离验收不发送模型请求，不使用开发者凭证，也不运行后台安排或 Bot。pytest 按文件隔离并最多 8 路运行，重叠选集自动去重。

```bash
bash scripts/dev/refactor-gate.sh quick    # 编辑中：当前 Git 改动的直接测试
bash scripts/dev/refactor-gate.sh module   # 功能完成：加跑分拆模块的邻接测试
bash scripts/dev/refactor-gate.sh full     # 准备合并：可重复、离线的全量门禁
bash scripts/dev/refactor-gate.sh release  # 准备发布：full + 原生协议慢测 + 桌面验收
bash scripts/dev/refactor-gate.sh desktop  # 显式桌面验收：可能显示窗口和程序坞图标
```

需要精确定位时，可直接使用底层运行器：

```bash
python3 scripts/dev/run-tests.py desktron/tests/test_deploy_diagnostics.py
python3 scripts/dev/run-tests.py --changed --list
python3 scripts/dev/run-tests.py --module --list
python3 scripts/dev/run-tests.py --native --list
python3 scripts/dev/run-tests.py --desktop --list  # 仅列出，不启动浏览器
python3 scripts/dev/run-tests.py --desktop        # 显式运行，固定单路
```

运行器需要 pytest。日常改动从 `quick` 开始；跨分拆文件、公共契约或风险边界的功能完成后运行 `module`；同一代码快照已通过的层级不重复运行。`full` 每个待合并快照一次，`release` 每个待发布快照一次。权限、安全、恢复、更新安装、数据迁移等高风险变更即使选集较小，也应显式补对应测试文件。

不要直接将所有 pytest 模块放进一个进程替代正式门禁；原生进程、端口、全局生命周期及更新状态需隔离，混跑失败不能直接归因于待发布代码。

Windows CI 在 `PYTHONUTF8=0/1` 两种模式分别执行 `full`，再执行交付专项与 ZIP 验收。`PYTHONIOENCODING=utf-8` 仅约束 Python 标准流，不替代文件或子进程编码契约。仓库源码、JSON、合成夹具与 Node JSON 管道必须显式使用 UTF-8；不要在测试入口全局替换默认编码来掩盖运行代码遗漏。`test_utf8_protocol_contracts.py` 补测中文及 emoji，并验证无环境标准流覆盖的历史读取、非法字节拒绝和 CI 双模式合同。

没有测试映射的运行时代码改动会让 `quick/module` 失败，而不是静默放行。纯文档改动可直接返回无需 pytest。`quick/module/full` 与底层默认运行均排除 `desktop` 标记的用例，保留同文件内的静态及后台测试。安装 Playwright 不会自动启用桌面测试；直接 pytest 也默认跳过它们。`--desktop` 固定单路，忽略并发参数及环境值，启用浏览器和原生 WebKit 验收。它仍可能影响窗口或程序坞，不承诺无界面。新增浏览器启动或原生窗口用例必须标记 `@pytest.mark.desktop`；无浏览器测试通过不能代替界面验收。真实 CLI、登录与远端连接仍归发布或对应环境验收。

## 交付说明

说明触发问题、最终行为、验证范围与未覆盖场景。不要把存在测试文件写成“测试已通过”，不要把本机检查结果写成“所有用户已恢复”。文档变更检查索引、链接、源码引用和分发边界；不为纯文案增加重复实现的测试。

构建、同步、发布和重启见[交付流程](release.md)。
