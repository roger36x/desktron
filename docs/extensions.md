# 应用、知识库与技能扩展

官方资产来自源码清单和构建产物，用户资产来自运行注册表。新增能力先选正确落点，再注册和验证。

## 应用

[frontend/workbench.json](../frontend/workbench.json)声明官方应用；[workbench.py](../desktron/lib/workbench.py)合并本地注册与移除记录。当前官方工具共 8 项，具体名称见[应用手册](../desk-docs/apps.md)。

任务型应用用 `agentic` 配置执行内容和参数，网页应用指向实际页面。稳定 uid 用于更新识别，不能在每次修改时生成新身份。注册必须通过 schema 与 App Control 声明校验，详见[控制契约](agent-operable-app.md)。

包安装由 [pkgstore.py](../desktron/lib/pkgstore.py)处理 `desktron-pkg.yaml`、来源仓和子路径：读取清单、生成计划、确认安装后注册。缺少依赖必须在结果中说明；发现安装器函数不代表所有包类型都有相同 UI 入口。

## 知识库

显式 Git KB 注册在 `.workbench/kb-git.json`，仓库 clone 放在运行数据根旁的 `kb`。挂载与卸载由明确操作触发。

## 技能

[skill/api.py](../desktron/lib/skill/api.py)承接技能接口。市场技能先预览正文，再由用户确认安装；实际依赖和权限按技能内容判断。

## 分发

官方应用构建使用 [build-builtin-apps.sh](../scripts/dev/build-builtin-apps.sh)，随 payload 分发。一次性演示页通过预览发布流程分享，不自动变成工作台应用。贡献流程读取 contribute skill；发布边界见[交付流程](release.md)。
