// 从本机 VS Code 自带的 Seti 主题裁出的常用文件映射。
// 图形与颜色保持 Seti 口径；未命中的文件统一回退 _default。
const ICONS = {
  default: ["E023", "#d4d7d6", "#bfc2c1"], python: ["E07B", "#519aba", "#498ba7"],
  javascript: ["E051", "#cbcb41", "#b7b73b"], typescript: ["E099", "#519aba", "#498ba7"],
  react: ["E07D", "#519aba", "#498ba7"], json: ["E055", "#cbcb41", "#b7b73b"],
  markdown: ["E060", "#519aba", "#498ba7"], yaml: ["E0A7", "#a074c4", "#9068b0"],
  html: ["E048", "#e37933", "#cc6d2e"], css: ["E01D", "#519aba", "#498ba7"],
  sass: ["E084", "#f55385", "#dd4b78"], less: ["E059", "#519aba", "#498ba7"],
  shell: ["E089", "#8dc149", "#7fae42"], config: ["E019", "#6d8086", "#627379"],
  git: ["E034", "#6d8086", "#627379"], docker: ["E025", "#519aba", "#498ba7"],
  go: ["E03A", "#519aba", "#498ba7"], rust: ["E082", "#8a979b", "#627379"],
  java: ["E050", "#cc3e44", "#b8383d"], kotlin: ["E058", "#e37933", "#cc6d2e"],
  swift: ["E092", "#e37933", "#cc6d2e"], c: ["E00C", "#519aba", "#498ba7"],
  cpp: ["E01A", "#519aba", "#498ba7"], csharp: ["E00B", "#519aba", "#498ba7"],
  php: ["E070", "#a074c4", "#9068b0"], ruby: ["E081", "#cc3e44", "#b8383d"],
  db: ["E022", "#f55385", "#dd4b78"], xml: ["E0A5", "#e37933", "#cc6d2e"],
  terraform: ["E093", "#a074c4", "#9068b0"], vue: ["E09D", "#8dc149", "#7fae42"],
  svelte: ["E090", "#cc3e44", "#b8383d"], image: ["E04C", "#a074c4", "#9068b0"],
  svg: ["E091", "#a074c4", "#9068b0"], pdf: ["E06D", "#cc3e44", "#b8383d"],
  archive: ["E0A9", "#cc3e44", "#b8383d"], audio: ["E005", "#a074c4", "#9068b0"],
  video: ["E09B", "#f55385", "#dd4b78"], font: ["E033", "#cc3e44", "#b8383d"],
  csv: ["E01E", "#8dc149", "#7fae42"], spreadsheet: ["E0A4", "#8dc149", "#7fae42"],
  word: ["E0A3", "#519aba", "#498ba7"], lock: ["E05D", "#8dc149", "#7fae42"],
  license: ["E05A", "#cbcb41", "#b7b73b"], info: ["E04D", "#519aba", "#498ba7"],
  clock: ["E012", "#519aba", "#498ba7"], npm: ["E067", "#cc3e44", "#b8383d"],
  make: ["E05F", "#e37933", "#cc6d2e"], tsconfig: ["E097", "#519aba", "#498ba7"],
  eslint: ["E02C", "#a074c4", "#9068b0"], vite: ["E09C", "#cbcb41", "#b7b73b"],
  notebook: ["E066", "#519aba", "#498ba7"], graphql: ["E03E", "#f55385", "#dd4b78"],
  lua: ["E05E", "#519aba", "#498ba7"], r: ["E001", "#519aba", "#498ba7"],
};

const NAMES = {
  "readme": "info", "readme.md": "info", "readme.txt": "info",
  "changelog": "clock", "changelog.md": "clock", "changelog.txt": "clock",
  "license": "license", "license.md": "license", "license.txt": "license",
  "licence": "license", "copying": "license", "contributing.md": "license",
  "package.json": "npm", "package-lock.json": "npm", "npm-shrinkwrap.json": "npm",
  "yarn.lock": "npm", "pnpm-lock.yaml": "npm",
  "tsconfig.json": "tsconfig", "jsconfig.json": "tsconfig",
  "eslint.config.js": "eslint", "eslint.config.mjs": "eslint", ".eslintrc": "eslint",
  ".eslintrc.js": "eslint", ".eslintrc.json": "eslint", ".eslintignore": "eslint",
  "vite.config.js": "vite", "vite.config.ts": "vite", "vite.config.mjs": "vite",
  "dockerfile": "docker", "docker-compose.yml": "docker", "docker-compose.yaml": "docker",
  ".dockerignore": "docker", ".gitignore": "git", ".gitattributes": "git", ".gitmodules": "git",
  "makefile": "make", "cmakelists.txt": "make", "pyproject.toml": "python",
  "requirements.txt": "python", "go.mod": "go", "go.sum": "go",
  "cargo.toml": "rust", "cargo.lock": "rust", "gemfile": "ruby", "pom.xml": "java",
};

const EXTENSIONS = {
  py: "python", pyw: "python", pyi: "python",
  js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "react",
  ts: "typescript", mts: "typescript", cts: "typescript", tsx: "react",
  json: "json", jsonc: "json", jsonl: "json", md: "markdown", mdx: "markdown",
  yaml: "yaml", yml: "yaml", html: "html", htm: "html", css: "css",
  scss: "sass", sass: "sass", less: "less", sh: "shell", bash: "shell", zsh: "shell", fish: "shell",
  toml: "config", ini: "config", conf: "config", config: "config", properties: "config", env: "config",
  go: "go", rs: "rust", java: "java", kt: "kotlin", kts: "kotlin", swift: "swift",
  c: "c", h: "c", cc: "cpp", cpp: "cpp", cxx: "cpp", hpp: "cpp", hxx: "cpp", cs: "csharp",
  php: "php", rb: "ruby", sql: "db", sqlite: "db", db: "db", xml: "xml",
  tf: "terraform", tfvars: "terraform", vue: "vue", svelte: "svelte",
  png: "image", jpg: "image", jpeg: "image", gif: "image", webp: "image", avif: "image", ico: "image",
  svg: "svg", pdf: "pdf", zip: "archive", tar: "archive", gz: "archive", tgz: "archive", rar: "archive", "7z": "archive",
  mp3: "audio", wav: "audio", flac: "audio", ogg: "audio",
  mp4: "video", mov: "video", webm: "video", avi: "video",
  ttf: "font", otf: "font", woff: "font", woff2: "font",
  csv: "csv", xls: "spreadsheet", xlsx: "spreadsheet", doc: "word", docx: "word",
  pem: "lock", key: "lock", crt: "lock", cer: "lock", ipynb: "notebook",
  graphql: "graphql", gql: "graphql", lua: "lua", r: "r", rmd: "r",
};

export function setiIconKey(name) {
  const base = String(name || "").split("/").pop().toLowerCase();
  if (NAMES[base]) return NAMES[base];
  if (base.startsWith(".env")) return "config";
  const parts = base.split(".");
  for (let i = 1; i < parts.length; i++) {
    const key = EXTENSIONS[parts.slice(i).join(".")];
    if (key) return key;
  }
  return EXTENSIONS[parts[parts.length - 1]] || "default";
}

export function setiFileIcon(name) {
  const def = ICONS[setiIconKey(name)] || ICONS.default;
  return `<span class="seti-file-icon" style="--seti-dark:${def[1]};--seti-light:${def[2]}" aria-hidden="true">&#x${def[0]};</span>`;
}
