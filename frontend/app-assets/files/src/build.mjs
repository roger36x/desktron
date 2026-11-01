// 文件浏览 APP 的 vendor 打包:主包 + Monaco 的各语言 worker。
// 用法见同目录 README.md。产物不入 git。
import * as esbuild from "esbuild";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const OUT = process.argv[2];
if (!OUT) { console.error("用法: node build.mjs <输出目录>"); process.exit(1); }
const HERE = path.dirname(fileURLToPath(import.meta.url));
const DEPS = path.resolve(HERE, "../..", "node_modules");
fs.mkdirSync(OUT, { recursive: true });

// VS Code 自带 Seti 字体以 base64 文本纳入源码，避免构建机依赖本机 VS Code 安装路径。
const setiData = fs.readFileSync(path.join(HERE, "seti.woff.b64"), "utf8").replace(/\s/g, "");
fs.writeFileSync(path.join(OUT, "seti.woff"), Buffer.from(setiData, "base64"));
fs.copyFileSync(path.join(HERE, "SETI-LICENSE.txt"), path.join(OUT, "SETI-LICENSE.txt"));

const common = { bundle: true, minify: true, format: "iife", target: ["es2020"], logLevel: "warning" };

// 主包:entry + Monaco 本体。Monaco 的 CSS 由 esbuild 一并抽成 app.bundle.css,
// 字体走 file loader 拷进产物目录(codicon 图标字体,少了它工具栏图标会是方块)。
await esbuild.build({
  ...common,
  entryPoints: [path.join(HERE, "entry.js")],
  outfile: path.join(OUT, "app.bundle.js"),
  loader: { ".ttf": "file" },
  assetNames: "[name]",
});

// 语言 worker:各自独立入口,运行时由 MonacoEnvironment.getWorkerUrl 按 label 指过来。
await esbuild.build({
  ...common,
  entryPoints: {
    "editor.worker": path.join(DEPS, "monaco-editor/esm/vs/editor/editor.worker.js"),
    "json.worker": path.join(DEPS, "monaco-editor/esm/vs/language/json/json.worker.js"),
    "css.worker": path.join(DEPS, "monaco-editor/esm/vs/language/css/css.worker.js"),
    "html.worker": path.join(DEPS, "monaco-editor/esm/vs/language/html/html.worker.js"),
    "ts.worker": path.join(DEPS, "monaco-editor/esm/vs/language/typescript/ts.worker.js"),
  },
  outdir: OUT,
});

const size = (f) => (fs.statSync(path.join(OUT, f)).size / 1024 / 1024).toFixed(1) + "MB";
console.log("built ->", OUT);
for (const f of fs.readdirSync(OUT)) console.log("  ", f, size(f));
