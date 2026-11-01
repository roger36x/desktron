import * as esbuild from "esbuild";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const OUT = process.argv[2];
if (!OUT) { console.error("用法: node build.mjs <输出目录>"); process.exit(1); }
const HERE = path.dirname(fileURLToPath(import.meta.url));
const DEPS = path.resolve(HERE, "../..", "node_modules");
fs.mkdirSync(OUT, { recursive: true });

await esbuild.build({
  entryPoints: [path.join(HERE, "entry.jsx")],
  bundle: true,
  minify: true,
  format: "iife",
  target: ["es2020"],
  jsx: "automatic",
  loader: { ".js": "jsx" },
  define: {
    "process.env.NODE_ENV": '"production"',
    "process.env.IS_PREACT": '"false"',
  },
  outfile: path.join(OUT, "app.bundle.js"),
});

// 库自带 CSS 与字体原样拷出(字体路径由 window.EXCALIDRAW_ASSET_PATH 指向本目录)
const pkg = path.join(DEPS, "@excalidraw/excalidraw/dist/prod");
fs.copyFileSync(path.join(pkg, "index.css"), path.join(OUT, "index.css"));
for (const sub of ["fonts", "locales"]) {
  const src = path.join(pkg, sub);
  if (fs.existsSync(src)) fs.cpSync(src, path.join(OUT, sub), { recursive: true });
}
console.log("built ->", OUT);
