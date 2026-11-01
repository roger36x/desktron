# 文件浏览 APP 的构建源

`vendor/` 下那约 12MB 的产物是这里打出来的，不入 git（见 `.gitignore`）。换机器或升级 Monaco 时按下面重打一份。

```bash
npm init -y
npm i monaco-editor@0.52.2 esbuild
cp <本目录>/{entry.js,build.mjs} .
node build.mjs <repo>/frontend/app-assets/files/vendor
```

产物：`app.bundle.js`（应用本体，含 Monaco 全部语言的语法高亮）、`app.bundle.css`、`codicon.ttf`（Monaco 的图标字体，少了它工具栏图标是方块），以及五个 worker（`editor` / `json` / `css` / `html` / `ts`）。`ts.worker.js` 单独就有 5.7MB，是 TypeScript 语言服务，去掉它只影响 JS / TS 的智能提示。

页面在 `frontend/pages/files.html`，只负责骨架与样式。

## 结构

- `entry.js`：左树 + 右编辑器。目录树懒加载，后端复用工作台既有的 `/workbench/fs`（列目录 / 读文件 / 递归搜文件名 / 保存），不新开接口；工作区 id 从页面 URL 的 `domain` 参数取，作为 fs 的 `root`。
- 打开即只读，点「编辑」才解锁，与工作台其它查看器同一手感；`Cmd+S` 保存。展开的目录、上次看的文件、左栏宽度按工作区存 localStorage。
- Monaco 的语言服务跑在 worker 里，`MonacoEnvironment.getWorkerUrl` 按 label 指到 vendor 目录。这一步少了的话编辑器能显示但没有诊断与补全。

## 没做的

LSP（跳转定义、悬停、跨文件诊断）没接。要接的话加 `monaco-languageclient`，每种语言另起一个 language server 进程并接 WebSocket，那是独立一步。
