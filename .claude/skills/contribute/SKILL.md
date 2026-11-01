---
name: contribute
description: 把本机已经验证的 Desktron 应用或操作说明整理进产品源码，供后续版本发布。只准备代码和验证，不自动提交、推送、发布或重启。
---

# contribute

把本机可复用资产提升到 `desktron`，使它随下一版产品发行。官方资产不再发布到独立 App、Workspace 或 Skill 内容仓。

## 先确认落点

- App：清单进入 `frontend/workbench.json` 的 `desktopApps`；页面、图标与依赖进入 `frontend/` 对应目录。
- skill：完整目录进入 `.claude/skills/<name>/`。
- 工作流、会话、记忆和项目绑定默认不贡献，它们属于用户运行状态。

## 工作方式

1. 从运行态按稳定 uid 定位 App，或从技能目录定位 skill；先确认它经过真实使用，不是占位资产。
2. 找到 `desktron` 源码仓。优先使用当前 git 根；否则读取 `DESKTRON_SOURCE`，再尝试 `~/work/desktron`。
3. 检查源码仓工作树。只修改本资产所需文件；已有脏改动属于用户或其他会话，不覆盖、不整树暂存。
4. 做最小迁移：保留 App 的 id、uid 与页面契约；依赖必须有固定版本和可重复构建入口。官方同 ID 已存在时按 uid 更新，不再并列一份。
5. 扫描凭证、个人绝对路径、具名个人评价、组织判断和合规敏感内容。凭证或敏感内容命中即停止，不提供强行跳过入口。
6. 运行与改动相称的检查：
   - App：`python3 scripts/check_frontend.py`，并对改动 JS 执行 `node --check`；涉及 Excalidraw 或 Monaco 时运行 `bash scripts/dev/build-builtin-apps.sh`。
   - skill：运行 skill-creator 的 `quick_validate.py`；改了脚本则执行其无副作用测试。
   - 发行边界：确认构建不依赖旧内容仓或 KB 仓。
7. 交付文件清单、测试结果和仍需人审的风险。不要自动 commit、push、发布、同步运行目录或重启 Desk。

## 边界

第三方 Git 包继续由 pkgstore 显式安装，不等于官方资产。提升为官方意味着进入产品 git 历史并影响所有新安装，因此需要人审。当前工作树存在并行修改时，优先新增隔离文件或做窄补丁；无法证明不会覆盖他人改动时暂停该文件。
