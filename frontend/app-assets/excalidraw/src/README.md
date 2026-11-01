# Excalidraw 应用的构建源

`vendor/` 下那份 8MB 的 `app.bundle.js` 是这里打出来的产物,不入 git(见 .gitignore)。
换机器或升级库版本时按下面重打一份。

```bash
npm init -y
npm i @excalidraw/excalidraw@0.18.1 react@18 react-dom@18 esbuild
cp <本目录>/{entry.jsx,build.mjs} .
node build.mjs <repo>/frontend/app-assets/excalidraw/vendor
```

产物三样:`app.bundle.js`(应用本体,IIFE,自己挂到 `#app`)、`index.css`(库自带样式)、
`fonts/` + `locales/`(库的字体与语言包,13MB,靠页面里的 `window.EXCALIDRAW_ASSET_PATH`
指到 `/app-assets/excalidraw/vendor/` 才走本地,不设这个全局变量库会兜底去 esm.sh 取)。

页面在运行态 `.workbench/pages/excalidraw.html`(不在 git 里),只负责引这三样 + 画布列表的样式。

## 结构

- `entry.jsx`:原版全屏 Excalidraw + 多画布管理。画布存 IndexedDB(`desk-excalidraw`
  库的 `canvases` 表,一条记录一张画布),onChange 防抖 800ms 落盘,切画布前先把当前这张落盘。
  画布列表挂在 Excalidraw 自己的 `<Sidebar>` 里,顶栏触发器走 `<Sidebar.Trigger>`,
  保持原生外观。命令式 API 另挂一份到 `window.__excalidrawAPI`,供排查和自动化驱动。
- `build.mjs`:esbuild 打包 + 把库的 CSS/字体/语言包拷进产物目录。
