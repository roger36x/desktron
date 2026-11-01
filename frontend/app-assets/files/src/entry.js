// 文件浏览 APP:左侧懒加载目录树 + 右侧 Monaco 编辑器。
//
// 后端复用工作台既有的 /workbench/fs(列目录 / 读文件 / 递归搜文件名 / 保存),不新开接口。
// 工作区 id 从页面 URL 的 domain 参数取(pageUrl 拼的),作为 fs 的 root。
import * as monaco from "monaco-editor";
import { setiFileIcon } from "./seti-icons.js";

const VENDOR = "/app-assets/files/vendor";
// Monaco 的语言服务跑在 worker 里。esbuild 把它们打成独立文件,这里按 label 指路径。
self.MonacoEnvironment = {
  getWorkerUrl(_id, label) {
    const map = { json: "json.worker.js", css: "css.worker.js", scss: "css.worker.js",
                  less: "css.worker.js", html: "html.worker.js", handlebars: "html.worker.js",
                  razor: "html.worker.js", typescript: "ts.worker.js", javascript: "ts.worker.js" };
    return `${VENDOR}/${map[label] || "editor.worker.js"}`;
  },
};

const qs = new URLSearchParams(location.search);
const ROOT = qs.get("domain") || qs.get("root") || "";
const KEY = "cd_files_app_" + ROOT;

// 与会话过程区的脚本 / 代码展开共用一套正文度量。普通编辑器与 diff 必须读同一个对象，
// 否则从「正文」切到「差异」时字号、行高会跳一档。字体文件由 files.html 注册。
const IDE_CODE_METRICS = {
  fontFamily: '"Desk Mono", "Desk Mono CJK", Menlo, monospace',
  fontSize: 11,
  lineHeight: 18,
  fontWeight: "400",
  fontLigatures: false,
};

// 代码正文直接对齐会话过程区：中性 surface、GitHub highlight.js 的 token 色与相同灰阶。
// 编辑器 chrome 仍由 files.html 管，品牌紫只保留在光标、选区和焦点等交互状态。
const GITHUB_THEME_NAME = { dark: "github-dark-default", light: "github-light-default" };
const GITHUB_PALETTE = {
  dark: {
    base: "vs-dark", bg: "232323", fg: "D1D1D1", muted: "8F8F8F",
    subtle: "7F7F7F", constant: "79C0FF", entity: "D2A8FF", keyword: "FF7B72",
    string: "A5D6FF", variable: "79C0FF", tag: "7EE787",
    colors: {
      "editor.background": "#232323", "editor.foreground": "#d1d1d1",
      "editorGutter.background": "#232323", "editorLineNumber.foreground": "#7f7f7f",
      "editorLineNumber.activeForeground": "#d1d1d1", "editorCursor.foreground": "#97a3f7",
      // Monaco 的 IME 合成层会直接复用当前行底色。半透明色会让下层正式文本透出，
      // 与 textarea 里的整行合成文本叠成双影；使用过程 surface 上的一档实色高亮。
      "editor.lineHighlightBackground": "#2b2b2b", "editor.selectionBackground": "#97a3f740",
      "editor.inactiveSelectionBackground": "#97a3f71f",
      "editor.selectionHighlightBackground": "#97a3f72b",
      "editorIndentGuide.background1": "#ffffff17", "editorIndentGuide.activeBackground1": "#ffffff30",
      "editorWhitespace.foreground": "#535353", "editorBracketMatch.background": "#97a3f72b",
      "editorBracketMatch.border": "#97a3f799", "editor.findMatchBackground": "#b68a32",
      "editor.findMatchHighlightBackground": "#d5b45b66", "editor.foldBackground": "#ffffff0a",
      "editorWidget.background": "#282828", "editorWidget.foreground": "#d1d1d1",
      "editorWidget.border": "#3a3a3a", "editorHoverWidget.background": "#282828",
      "editorHoverWidget.foreground": "#d1d1d1", "editorHoverWidget.border": "#3a3a3a",
      "editorSuggestWidget.background": "#282828", "editorSuggestWidget.foreground": "#d1d1d1",
      "editorSuggestWidget.border": "#3a3a3a", "editorSuggestWidget.highlightForeground": "#aab3fa",
      "editorSuggestWidget.selectedBackground": "#ffffff14", "input.background": "#232323",
      "input.foreground": "#d1d1d1", "input.border": "#3a3a3a",
      "input.placeholderForeground": "#7f7f7f", "focusBorder": "#97a3f7",
      "list.hoverBackground": "#ffffff0a", "list.activeSelectionBackground": "#97a3f728",
      "list.inactiveSelectionBackground": "#97a3f71f", "list.focusBackground": "#97a3f71f",
      "scrollbar.shadow": "#00000026", "scrollbarSlider.background": "#8f8f8f33",
      "scrollbarSlider.hoverBackground": "#8f8f8f4d", "scrollbarSlider.activeBackground": "#8f8f8f5c",
      "minimap.background": "#232323", "minimapSlider.background": "#8f8f8f24",
      "minimapSlider.hoverBackground": "#8f8f8f3d", "minimapSlider.activeBackground": "#8f8f8f4d",
      "editorOverviewRuler.border": "#232323", "editorError.foreground": "#f2686b",
      "editorWarning.foreground": "#c79a43", "editorInfo.foreground": "#97a3f7",
      "editorLink.activeForeground": "#aab3fa", "diffEditor.border": "#3a3a3a",
      "diffEditor.diagonalFill": "#2b2b2b", "diffEditor.insertedLineBackground": "#23863626",
      "diffEditor.insertedTextBackground": "#3fb9504d", "diffEditor.removedLineBackground": "#da363326",
      "diffEditor.removedTextBackground": "#ff7b724d",
    },
  },
  light: {
    base: "vs", bg: "F9F9F9", fg: "393939", muted: "777777",
    subtle: "868686", constant: "0550AE", entity: "8250DF", keyword: "CF222E",
    string: "0A3069", variable: "0550AE", tag: "116329",
    colors: {
      "editor.background": "#f9f9f9", "editor.foreground": "#393939",
      "editorGutter.background": "#f9f9f9", "editorLineNumber.foreground": "#868686",
      "editorLineNumber.activeForeground": "#393939", "editorCursor.foreground": "#4f46b8",
      // 与暗色同理，给 IME 一个不透底的当前行表面。
      "editor.lineHighlightBackground": "#f1f1f1", "editor.selectionBackground": "#4f46b829",
      "editor.inactiveSelectionBackground": "#4f46b814",
      "editor.selectionHighlightBackground": "#4f46b81f",
      "editorIndentGuide.background1": "#00000014", "editorIndentGuide.activeBackground1": "#0000002b",
      "editorWhitespace.foreground": "#b1b1b1", "editorBracketMatch.background": "#4f46b81f",
      "editorBracketMatch.border": "#4f46b873", "editor.findMatchBackground": "#d4a72c",
      "editor.findMatchHighlightBackground": "#fae17d80", "editor.foldBackground": "#6e77811a",
      "editorWidget.background": "#ffffff", "editorWidget.foreground": "#393939",
      "editorWidget.border": "#d9d9d9", "editorHoverWidget.background": "#ffffff",
      "editorHoverWidget.foreground": "#393939", "editorHoverWidget.border": "#d9d9d9",
      "editorSuggestWidget.background": "#ffffff", "editorSuggestWidget.foreground": "#393939",
      "editorSuggestWidget.border": "#d9d9d9", "editorSuggestWidget.highlightForeground": "#4f46b8",
      "editorSuggestWidget.selectedBackground": "#0000000a", "input.background": "#f9f9f9",
      "input.foreground": "#393939", "input.border": "#d9d9d9",
      "input.placeholderForeground": "#777777", "focusBorder": "#4f46b8",
      "list.hoverBackground": "#00000008", "list.activeSelectionBackground": "#4f46b81f",
      "list.inactiveSelectionBackground": "#4f46b814", "list.focusBackground": "#4f46b814",
      "scrollbar.shadow": "#0000001f", "scrollbarSlider.background": "#86868633",
      "scrollbarSlider.hoverBackground": "#86868647", "scrollbarSlider.activeBackground": "#8686865c",
      "minimap.background": "#f9f9f9", "minimapSlider.background": "#86868624",
      "minimapSlider.hoverBackground": "#8686863d", "minimapSlider.activeBackground": "#8686864d",
      "editorOverviewRuler.border": "#f9f9f9", "editorError.foreground": "#cf222e",
      "editorWarning.foreground": "#8a6400", "editorInfo.foreground": "#4f46b8",
      "editorLink.activeForeground": "#4f46b8", "diffEditor.border": "#d9d9d9",
      "diffEditor.diagonalFill": "#ececec", "diffEditor.insertedLineBackground": "#aceebb4d",
      "diffEditor.insertedTextBackground": "#6fdd8b80", "diffEditor.removedLineBackground": "#ffcecb4d",
      "diffEditor.removedTextBackground": "#ff818266",
    },
  },
};

function githubRules(p) {
  return [
    { token: "", foreground: p.fg, background: p.bg },
    { token: "comment", foreground: p.muted },
    { token: "string", foreground: p.string },
    { token: "string.escape", foreground: p.constant },
    { token: "regexp", foreground: p.string },
    { token: "number", foreground: p.constant },
    { token: "constant", foreground: p.constant },
    { token: "variable", foreground: p.variable },
    { token: "variable.predefined", foreground: p.constant },
    { token: "keyword", foreground: p.keyword },
    { token: "storage", foreground: p.keyword },
    { token: "type", foreground: p.entity },
    { token: "type.identifier", foreground: p.entity },
    { token: "class", foreground: p.entity },
    { token: "namespace", foreground: p.entity },
    { token: "function", foreground: p.entity },
    { token: "tag", foreground: p.tag },
    { token: "attribute.name", foreground: p.variable },
    { token: "metatag", foreground: p.keyword },
    { token: "annotation", foreground: p.entity },
    { token: "delimiter", foreground: p.fg },
  ];
}

let ideThemeMode = "dark";
function hostThemeRoot() {
  try { return parent !== window ? parent.document.documentElement : null; } catch { return null; }
}
function desiredThemeMode() {
  const host = hostThemeRoot();
  const asked = (host && host.dataset.theme) || qs.get("theme") || "";
  if (asked) return asked === "light" ? "light" : "dark";
  return window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}
function ideThemeName() { return GITHUB_THEME_NAME[ideThemeMode]; }
function applyIdeTheme(mode) {
  ideThemeMode = mode === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = ideThemeMode;
  monaco.editor.setTheme(ideThemeName());
}
function initGithubThemes() {
  for (const mode of ["dark", "light"]) {
    const p = GITHUB_PALETTE[mode];
    monaco.editor.defineTheme(GITHUB_THEME_NAME[mode], {
      base: p.base, inherit: true, rules: githubRules(p), colors: p.colors,
    });
  }
  applyIdeTheme(desiredThemeMode());

  // Monaco 在同源 iframe 里。直接观察宿主的 data-theme,切换时只换 theme，不刷新 iframe；
  // 已开的 model、光标和未保存内容因此都留在原地。独立打开页面时则跟系统明暗。
  const host = hostThemeRoot();
  if (host) {
    new MutationObserver(() => applyIdeTheme(desiredThemeMode())).observe(host, {
      attributes: true, attributeFilter: ["data-theme"],
    });
  } else if (!qs.has("theme") && window.matchMedia) {
    const mq = matchMedia("(prefers-color-scheme: light)");
    mq.addEventListener?.("change", () => applyIdeTheme(desiredThemeMode()));
  }
}

const $ = (s) => document.querySelector(s);
// 宽度小不等于手机：IDE 住进 Desk 右侧栏时同样会小于 820px，但应继续使用主区全屏版的
// 文件树、标签、面包屑、minimap 与 diff。只有窄屏触控设备才进入抽屉和触控放大模式。
const MOBILE_MEDIA = matchMedia("(pointer: coarse) and (max-width: 820px), (pointer: coarse) and (max-height: 520px)");
const mobileIDE = () => MOBILE_MEDIA.matches;
const ideCodeMetrics = () => mobileIDE()
  // 可见正文与会话正文同为 14.5px；真正接收焦点的 textarea 由 files.html 单独保持 16px，
  // 因而视觉不显大，同时仍不会触发 iOS 的聚焦缩放。
  ? { ...IDE_CODE_METRICS, fontSize: 14.5, lineHeight: 23 }
  : IDE_CODE_METRICS;
const ideChromeMetrics = () => mobileIDE()
  ? { lineNumbersMinChars: 2, glyphMargin: false, folding: false, lineDecorationsWidth: 6 }
  : { lineNumbersMinChars: 5, glyphMargin: true, folding: true, lineDecorationsWidth: 10 };
function appLayoutFromHost() {
  let mode = document.documentElement.dataset.layout || "";
  try { mode = frameElement?.closest("[data-layout]")?.dataset.layout || mode; } catch {}
  return mode === "compact" ? "compact" : "main";
}
let appLayoutMode = appLayoutFromHost();
// 主区和紧凑侧栏各自记一份手动选择。紧凑侧栏首次进入默认收树；用户在其中展开后，
// 重复的布局通知与临时切回主区都不能再次替他收掉。
const sideVisibleByLayout = { main: null, compact: false };
function setSideVisible(open) {
  const on = !!open;
  if (mobileIDE()) {
    document.body.classList.remove("side-collapsed");
    document.body.classList.toggle("side-open", on);
  } else {
    document.body.classList.remove("side-open");
    document.body.classList.toggle("side-collapsed", !on);
  }
  if (appLayoutMode === "compact") {
    sideVisibleByLayout.compact = on;
  } else if (mobileIDE()) {
    sideVisibleByLayout.main = on;
  } else {
    st.sideCollapsed = !on; save();
  }
  const btn = $("#m-side");
  if (btn) btn.setAttribute("aria-expanded", on ? "true" : "false");
}
const api = async (u, opt) => {
  const r = await fetch(u, opt);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.headers.get("content-type", "").includes("json") ? r.json() : r.text();
};

const st = (() => { try { return JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch { return {}; } })();
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(st)); } catch {} };
const expanded = new Set(st.expanded || []);

// ---- 编辑器 ----
// 标签页:path → {name, model, view, dirty, ro}。model 常驻不 dispose,切回来时
// 连同光标与滚动位置一起还原;这也是 TS 语言服务能跨文件解析的前提——它只认已加载的 model。
let editor = null, current = null;
const tabs = new Map();
const tabOf = () => (current ? tabs.get(current) : null);
const isDirty = () => !!(tabOf() || {}).dirty;
let mobileLineFrame = 0;

function applyAppLayout(mode, force = false) {
  const next = mode === "compact" ? "compact" : "main";
  if (!force && next === appLayoutMode) return;
  appLayoutMode = next;
  document.documentElement.dataset.layout = next;
  const visible = next === "compact" ? sideVisibleByLayout.compact
    : (mobileIDE() ? (sideVisibleByLayout.main ?? !tabs.size) : !st.sideCollapsed);
  setSideVisible(visible);
}
window.addEventListener("message", (e) => {
  if (e.source !== parent || e.origin !== location.origin || e.data?.type !== "desktron-layout") return;
  applyAppLayout(e.data.mode);
});

function mobileBuffer(t = tabOf()) {
  if (!t) return "";
  return t.mobileValue == null ? t.model.getValue() : t.mobileValue;
}

function paintMobileLines() {
  cancelAnimationFrame(mobileLineFrame);
  mobileLineFrame = requestAnimationFrame(() => {
    const input = $("#mobile-input"), measure = $("#mobile-measure"), track = $("#mobile-line-track");
    if (!input || !measure || !track) return;
    const rows = input.value.split("\n");
    const measured = document.createDocumentFragment();
    rows.forEach((line) => {
      const row = document.createElement("div");
      row.textContent = line || "\u200b";
      measured.appendChild(row);
    });
    measure.replaceChildren(measured);
    const nums = document.createDocumentFragment();
    [...measure.children].forEach((row, i) => {
      const n = document.createElement("span");
      n.textContent = String(i + 1); n.style.top = row.offsetTop + "px";
      nums.appendChild(n);
    });
    track.replaceChildren(nums);
    track.style.transform = `translateY(${-input.scrollTop}px)`;
  });
}

function captureMobile(t = tabOf()) {
  const input = $("#mobile-input");
  if (!t || !input || $("#mobile-ed").hidden) return;
  t.mobileValue = input.value;
  t.mobileView = { start: input.selectionStart, end: input.selectionEnd, scrollTop: input.scrollTop };
}

function syncMobileModel(t = tabOf()) {
  if (!t || t.mobileValue == null || t.model.getValue() === t.mobileValue) return;
  t.model.setValue(t.mobileValue);
}

function loadMobile(t) {
  const input = $("#mobile-input");
  if (!input || !t) return;
  input.value = mobileBuffer(t); t.mobileValue = input.value;
  paintMobileLines();
  requestAnimationFrame(() => {
    const v = t.mobileView || {};
    const end = input.value.length;
    input.setSelectionRange(Math.min(v.start ?? 0, end), Math.min(v.end ?? v.start ?? 0, end));
    input.scrollTop = v.scrollTop || 0;
    $("#mobile-line-track").style.transform = `translateY(${-input.scrollTop}px)`;
  });
}

function showEditorSurface() {
  const mobile = mobileIDE(), active = !!current && !diffOn;
  $("#ed").hidden = !active || mobile;
  $("#mobile-ed").hidden = !active || !mobile;
  if (active && mobile) loadMobile(tabOf());
}

function initMobileEditor() {
  const input = $("#mobile-input");
  input.addEventListener("input", () => {
    const t = tabOf(); if (!t) return;
    t.mobileValue = input.value;
    if (!t.dirty) { t.dirty = true; paintBar(); paintTabs(); }
    paintMobileLines();
  });
  input.addEventListener("scroll", () => {
    $("#mobile-line-track").style.transform = `translateY(${-input.scrollTop}px)`;
  }, { passive: true });
  input.addEventListener("keydown", (ev) => {
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "s") {
      ev.preventDefault(); doSave();
    }
  });
  new ResizeObserver(paintMobileLines).observe($("#mobile-ed"));
}

const EXT_LANG = {
  js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  ts: "typescript", tsx: "typescript", py: "python", rb: "ruby", go: "go", rs: "rust",
  java: "java", kt: "kotlin", swift: "swift", c: "c", h: "c", cpp: "cpp", hpp: "cpp",
  cs: "csharp", php: "php", sh: "shell", bash: "shell", zsh: "shell", fish: "shell",
  json: "json", yaml: "yaml", yml: "yaml", toml: "ini", ini: "ini", conf: "ini",
  md: "markdown", markdown: "markdown", html: "html", htm: "html", xml: "xml",
  css: "css", scss: "scss", less: "less", sql: "sql", lua: "lua", r: "r",
  dockerfile: "dockerfile", makefile: "makefile", proto: "proto", graphql: "graphql",
};
function langOf(name) {
  const base = name.toLowerCase();
  if (base === "dockerfile" || base === "makefile") return EXT_LANG[base];
  const ext = base.includes(".") ? base.split(".").pop() : "";
  return EXT_LANG[ext] || "plaintext";
}

// 全角标点在中文正文里是常态。Monaco 默认开着 unicodeHighlight.ambiguousCharacters——
// 把「长得像 ASCII 但不是」的字符逐个框出来,而（）， 正是这一类。于是一篇中文文档里
// 每个括号、每个逗号都套一个黄框,整屏都是噪声(2026-08-19 用户实见,orientation.md)。
// 散文类(markdown / 纯文本)关掉;代码类留着——一个全角逗号混进 .py 是真会出错的,
// 那时这个框恰恰是要看的东西。判据按模型的语言走,切标签时跟着换。
const PROSE_LANG = new Set(["markdown", "plaintext"]);
function unicodeOpt(lang) {
  return { ambiguousCharacters: !PROSE_LANG.has(String(lang || "")) };
}

function initEditor() {
  editor = monaco.editor.create($("#ed"), {
    value: "", language: "plaintext", theme: ideThemeName(),
    automaticLayout: true, ...ideCodeMetrics(), ...ideChromeMetrics(),
    // Sublime 式 minimap：绘制真实的微缩字符，而不是一排等宽色块；限制列宽与 1x 缩放
    // 让长行不会把缩略图撑得过宽，正文轮廓仍足够细密可辨。
    minimap: {
      enabled: !mobileIDE(), side: "right", size: "proportional", showSlider: "mouseover",
      renderCharacters: true, scale: 1, maxColumn: 100,
    },
    wordWrap: mobileIDE() ? "on" : "off",
    scrollBeyondLastLine: false, renderWhitespace: "selection",
    smoothScrolling: true, cursorBlinking: "smooth", padding: { top: 10 },
    // 不设只读:VS Code 里文件本来就是可编辑的,双击那一下管的是「标签从预览转常驻」,
    // 不是解锁编辑。原来这里打开即只读、点「编辑」才解锁,于是多出一个按钮和一个「什么时候
    // 退出编辑」的问题(2026-08-19 用户提)。改动不落盘就不生效:脏标记摆在标签与面包屑上,
    // 保存要 Cmd+S 或那枚「保存」,关掉带改动的标签会拦一次。
    unicodeHighlight: unicodeOpt("plaintext"),
  });
  editor.onDidChangeModelContent(() => {
    const t = tabOf();
    if (t && !t.dirty) { t.dirty = true; paintBar(); paintTabs(); }
  });
  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => doSave());
  initMobileEditor();
}

// ---- 顶栏:面包屑 + 编辑 / 保存 ----
function paintBar() {
  const segs = $("#crumb");
  segs.innerHTML = "";
  const parts = (current || "").split("/").filter(Boolean);
  const rootEl = document.createElement("span");
  rootEl.className = "seg root";
  rootEl.textContent = ROOT || "工作区";
  segs.appendChild(rootEl);
  parts.forEach((p, i) => {
    const sep = document.createElement("span"); sep.className = "sep"; sep.textContent = "/";
    const s = document.createElement("span"); s.className = "seg"; s.textContent = p;
    if (i === parts.length - 1 && isDirty()) s.classList.add("dirty");
    segs.appendChild(sep);
    if (i === parts.length - 1) {
      const fileIcon = document.createElement("span");
      fileIcon.className = "crumb-file-icon";
      fileIcon.innerHTML = setiFileIcon(p);
      segs.appendChild(fileIcon);
    }
    segs.appendChild(s);
  });
  const df = typeof diffOn !== "undefined" && diffOn;
  // 差异视图里只留「差异」这一枚(此时是返回)。保存与取消只在真有改动时出现:
  // 没改动时它们无事可做,常驻在那儿只是让人多认两个钮。
  $("#btn-diff").hidden = !current;
  $("#btn-diff").textContent = df ? "返回" : "差异";
  $("#btn-save").hidden = !current || !isDirty() || df;
  $("#btn-cancel").hidden = !current || !isDirty() || df;
}

// ---- 标签页 ----
// 切走前把光标与滚动位置存进上一个标签,切回来照原样还原——否则每次切换都从头开始读。
function activate(rel) {
  const t = tabs.get(rel);
  if (!t) return;
  if (typeof diffOn !== "undefined" && diffOn) diffClose();   // 差异绑着上一个标签的 model
  const prev = tabOf();
  if (prev && prev !== t) {
    captureMobile(prev); syncMobileModel(prev);
    prev.view = editor.saveViewState();
  }
  current = rel;
  editor.setModel(t.model);
  if (t.view) editor.restoreViewState(t.view);
  editor.updateOptions({ unicodeHighlight: unicodeOpt(t.model.getLanguageId()) });
  st.last = rel; st.tabs = [...tabs.keys()]; save();
  paintBar(); paintTabs(); markRow(rel);
  if (mobileIDE()) setSideVisible(false);
  showEditorSurface();
  (mobileIDE() ? $("#mobile-input") : editor).focus();
}

// 关一个标签。脏的先问一句——这里没有草稿箱,关掉就没了。
function closeTab(rel) {
  const t = tabs.get(rel);
  if (!t) return;
  if (t.dirty && !confirm(`「${t.name}」还有未保存的改动,关掉就丢了。确定关闭?`)) return;
  const order = [...tabs.keys()];
  const i = order.indexOf(rel);
  tabs.delete(rel);
  t.model.dispose();
  if (current === rel) {
    const next = order[i + 1] || order[i - 1];
    current = null;
    if (next) activate(next);
    else {
      editor.setModel(null);
      st.last = ""; st.tabs = []; save();
      paintBar(); paintTabs(); markRow(null);
      showEditorSurface();
    }
  } else {
    st.tabs = [...tabs.keys()]; save(); paintTabs();
  }
}

function paintTabs() {
  const host = $("#tabs");
  host.innerHTML = "";
  host.hidden = tabs.size === 0;
  for (const [rel, t] of tabs) {
    const el = document.createElement("div");
    el.className = "tab" + (rel === current ? " on" : "") + (t.dirty ? " dirty" : "");
    el.title = rel;
    el.innerHTML = `<span class="tb-ic">${setiFileIcon(t.name)}</span>`
      + '<span class="tb-n"></span><span class="tb-x" title="关闭">×</span>';
    el.querySelector(".tb-n").textContent = t.name;
    el.onclick = (ev) => { if (!ev.target.classList.contains("tb-x")) activate(rel); };
    el.querySelector(".tb-x").onclick = (ev) => { ev.stopPropagation(); closeTab(rel); };
    // 中键关闭:与浏览器标签同一手感
    el.onauxclick = (ev) => { if (ev.button === 1) { ev.preventDefault(); closeTab(rel); } };
    host.appendChild(el);
  }
  const on = host.querySelector(".tab.on");
  if (on) on.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function markRow(rel) {
  document.querySelectorAll(".row.on").forEach((e) => e.classList.remove("on"));
  if (!rel) return;
  const row = document.querySelector(`.row[data-path="${CSS.escape(rel)}"]`);
  if (row) row.classList.add("on");
}

function revealLine(line) {
  if (!line) return;
  if (mobileIDE()) {
    const input = $("#mobile-input");
    const lines = input.value.split("\n");
    const at = lines.slice(0, Math.max(0, line - 1)).reduce((n, s) => n + s.length + 1, 0);
    input.focus(); input.setSelectionRange(at, at); paintMobileLines();
    requestAnimationFrame(() => {
      const row = $("#mobile-measure").children[Math.max(0, line - 1)];
      if (row) input.scrollTop = Math.max(0, row.offsetTop - input.clientHeight / 2);
    });
    return;
  }
  editor.revealLineInCenter(line);
  editor.setPosition({ lineNumber: line, column: 1 });
  editor.setSelection({ startLineNumber: line, startColumn: 1, endLineNumber: line, endColumn: 1 });
}

async function openFile(rel, name, line) {
  if (tabs.has(rel)) { activate(rel); revealLine(line); return; }   // 已开过就切过去,不重复拉取
  try {
    const txt = await api(`/workbench/fs?root=${encodeURIComponent(ROOT)}&path=${encodeURIComponent(rel)}&file=1`);
    const model = monaco.editor.createModel(String(txt), langOf(name),
                                            monaco.Uri.parse("cdfile:///" + rel));
    tabs.set(rel, { name: name || rel.split("/").pop(), model, view: null,
                    mobileValue: null, mobileView: null, dirty: false });
    activate(rel);
    revealLine(line);
  } catch (e) {
    msg(`打不开:${e.message}`, true);
  }
}

async function doSave() {
  if (!current) return;
  const t = tabOf();
  captureMobile(t); syncMobileModel(t);
  const text = t ? t.model.getValue() : editor.getValue();
  try {
    await api("/workbench/fs", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ root: ROOT, path: current, text }),
    });
    if (t) t.dirty = false;   // 存完不再回到只读:没有那个模式了
    paintBar(); paintTabs(); msg("已保存");
  } catch (e) { msg(`保存失败:${e.message}`, true); }
}

let msgTimer = null;
function msg(t, bad) {
  const el = $("#msg");
  el.textContent = t; el.classList.toggle("bad", !!bad);
  clearTimeout(msgTimer); msgTimer = setTimeout(() => { el.textContent = ""; }, 2600);
}


// ---- 差异视图 ----
// 比的是「当前缓冲区」与「git HEAD 里的那一份」,回答的是「相对上次提交,这份文件改了什么」。
// 原来比的是缓冲区与磁盘:改了没存时它说得出话,存下之后两边一样,一句也说不出——
// 而后一种才是平时打开差异的时刻(2026-08-19 用户提)。
// 不在仓里、或这个版本里还没有这份文件时,左边给空,整份读作新增,并在下方说明缘由。
// 独立一个容器盖在编辑器上,不复用 #ed:diff 编辑器是另一个实例,来回 setModel 会丢视图状态。
let diffEd = null, diffOn = false, diffOrig = null;

function diffHost() {
  let el = document.getElementById("dif");
  if (!el) {
    el = document.createElement("div");
    el.id = "dif"; el.hidden = true;
    document.getElementById("main").appendChild(el);
  }
  return el;
}

async function diffOpen() {
  const t = tabOf();
  if (!t || !current) return;
  captureMobile(t); syncMobileModel(t);
  let disk = "", note = "";
  try {
    const r = await api(`/workbench/fs?root=${encodeURIComponent(ROOT)}`
                        + `&path=${encodeURIComponent(current)}&git=HEAD`);
    disk = String((r && r.text) || "");
    note = String((r && r.note) || "");
  } catch (e) { msg(`取不到 git 里的那一份:${e.message}`, true); return; }
  const host = diffHost();
  if (!diffEd) {
    diffEd = monaco.editor.createDiffEditor(host, {
      theme: ideThemeName(), automaticLayout: true, ...ideCodeMetrics(), ...ideChromeMetrics(),
      renderSideBySide: !mobileIDE(), readOnly: true, originalEditable: false,
      scrollBeyondLastLine: false, minimap: { enabled: false },
      renderOverviewRuler: false, ignoreTrimWhitespace: false,
    });
  }
  if (diffOrig) diffOrig.dispose();
  diffOrig = monaco.editor.createModel(disk, t.model.getLanguageId());
  diffEd.setModel({ original: diffOrig, modified: t.model });
  // 差异视图是两个内嵌编辑器,选项要分别给——diffEd.updateOptions 收的是差异自己那几项。
  const uo = { ...ideCodeMetrics(), ...ideChromeMetrics(),
               unicodeHighlight: unicodeOpt(t.model.getLanguageId()) };
  diffEd.getOriginalEditor().updateOptions(uo);
  diffEd.getModifiedEditor().updateOptions(uo);
  if (note) msg(note);   // 「HEAD 里还没有这份文件」这类:左边为空是有原因的,说一句
  host.hidden = false; $("#ed").hidden = true; $("#mobile-ed").hidden = true;
  diffOn = true; paintBar();
  const same = disk === t.model.getValue();
  msg(same ? "与磁盘一致" : "");
}

function diffClose() {
  diffOn = false;
  diffHost().hidden = true;
  showEditorSurface();
  // 模型交回普通编辑器:diff 那侧持有的是同一个 model,不能 dispose
  if (diffEd) diffEd.setModel(null);
  if (diffOrig) { diffOrig.dispose(); diffOrig = null; }
  paintBar();
  (mobileIDE() ? $("#mobile-input") : editor).focus();
}

function diffToggle() { diffOn ? diffClose() : diffOpen(); }

// ---- 目录树(懒加载) ----
// 侧栏使用原创彩色目录图标；开合切换独立素材，空目录与展开状态保留原有语义。
function folderIcon(open, hasChildren) {
  const state = `${open ? "is-open" : "is-closed"} ${hasChildren ? "has-children" : "is-empty"}`;
  const art = open ? "/icons/ide-folder-open-original-b95c700a1abe.png"
    : "/icons/ide-folder-closed-original-905d2778fcb2.png";
  return `<img class="folder-icon ${state}" src="${art}" alt="" aria-hidden="true" draggable="false">`;
}

function twistyIcon(type, open = false, hasChildren = false) {
  const cls = `tw${type !== "dir" ? " leaf" : ""}${open ? " open" : ""}${hasChildren ? "" : " empty"}`;
  return `<span class="${cls}" aria-hidden="true"><svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5">
    <path d="m4 2 4 4-4 4"/>
  </svg></span>`;
}

function icon(type, name = "", open = false, hasChildren = false) {
  if (type === "dir") return folderIcon(open, hasChildren);
  return setiFileIcon(name);
}

async function loadDir(rel, host, depth) {
  let items = [];
  try { items = (await api(`/workbench/fs?root=${encodeURIComponent(ROOT)}&path=${encodeURIComponent(rel)}`)).items || []; }
  catch { host.innerHTML = '<div class="tip">读不到这个目录</div>'; return; }
  host.innerHTML = "";
  for (const it of items) {
    // 分步更新时旧后端没有这个字段；缺省按“有内容”处理，避免把所有目录误画成空目录。
    const hasChildren = it.type === "dir" && it.has_children !== false;
    const row = document.createElement("div");
    row.className = "row"; row.dataset.path = it.path; row.dataset.type = it.type;
    if (it.type === "dir") row.dataset.hasChildren = hasChildren ? "true" : "false";
    row.style.paddingLeft = 7 + depth * 13 + "px";
    const isOpen = expanded.has(it.path);
    row.classList.toggle("open", isOpen);
    row.innerHTML = `${twistyIcon(it.type, isOpen, hasChildren)}<span class="ic">${icon(it.type, it.name, isOpen, hasChildren)}</span><span class="nm"></span>`;
    row.querySelector(".nm").textContent = it.name;
    host.appendChild(row);
    if (it.type === "dir") {
      const sub = document.createElement("div"); sub.className = "sub"; sub.hidden = true;
      host.appendChild(sub);
      row.onclick = async () => {
        const on = !sub.hidden;
        if (on) { sub.hidden = true; expanded.delete(it.path); }
        else {
          sub.hidden = false; expanded.add(it.path);
          if (!sub.dataset.loaded) { sub.dataset.loaded = "1"; await loadDir(it.path, sub, depth + 1); }
        }
        row.classList.toggle("open", !on);
        row.querySelector(".tw").outerHTML = twistyIcon("dir", !on, hasChildren);
        row.querySelector(".ic").innerHTML = icon("dir", it.name, !on, hasChildren);
        st.expanded = [...expanded]; save();
      };
      if (expanded.has(it.path)) {
        sub.hidden = false; sub.dataset.loaded = "1";
        row.querySelector(".tw").outerHTML = twistyIcon("dir", true, hasChildren);
        row.querySelector(".ic").innerHTML = icon("dir", it.name, true, hasChildren);
        await loadDir(it.path, sub, depth + 1);
      }
    } else {
      row.onclick = () => openFile(it.path, it.name);
    }
  }
}

// ---- 搜索(走后端递归,懒加载树自己搜不到未展开的) ----
// 一个框跑两趟:文件名与内容。不给「名称 / 内容」两个挡位——那是让人替系统做一个
// 它自己能判断的决定,打完字才发现挡位不对是这类控件的常态。两组结果各带小标题,
// 哪一组空就不出现;名称那趟快,先渲染,内容那趟回来再补在下面,不互相等。
function initSearch() {
  const input = $("#q"), results = $("#results"), tree = $("#tree");
  let timer = null, seq = 0;

  const group = (title, n) => {
    const h = document.createElement("div");
    h.className = "grp";
    h.textContent = n == null ? title : `${title} · ${n}`;
    return h;
  };

  function nameRow(it) {
    const row = document.createElement("div");
    row.className = "row"; row.style.paddingLeft = "6px";
    row.innerHTML = `<span class="ic">${icon("file", it.name || it.path)}</span><span class="nm"></span>`;
    row.querySelector(".nm").textContent = it.path || it.name;
    row.title = it.path || it.name;
    row.onclick = () => openFile(it.path, it.name || it.path);
    return row;
  }

  function hitRow(it) {
    const row = document.createElement("div");
    row.className = "row hit"; row.style.paddingLeft = "6px";
    row.innerHTML = '<span class="hit-w"><span class="hit-p"></span><span class="hit-t"></span></span>';
    row.querySelector(".hit-p").textContent = `${it.path}:${it.line}`;
    row.querySelector(".hit-t").textContent = (it.text || "").trim().slice(0, 160);
    row.title = `${it.path}:${it.line}`;
    row.onclick = () => openFile(it.path, it.name || it.path, it.line);
    return row;
  }

  async function run(kw) {
    const my = ++seq;
    results.innerHTML = '<div class="tip">搜索中…</div>';
    results.hidden = false; tree.hidden = true;
    const q = (u) => api(u).catch(() => null);
    const rName = await q(`/workbench/fs?root=${encodeURIComponent(ROOT)}&q=${encodeURIComponent(kw)}`);
    if (my !== seq) return;   // 已经有更新的一次搜索,这次的结果作废
    results.innerHTML = "";
    const names = (rName && (rName.items || rName.results)) || [];
    if (names.length) {
      results.appendChild(group("文件", names.length + (rName.truncated ? "+" : "")));
      names.forEach((it) => results.appendChild(nameRow(it)));
    }
    const pending = document.createElement("div");
    pending.className = "tip"; pending.textContent = "正在搜内容…";
    results.appendChild(pending);

    const rGrep = await q(`/workbench/fs?root=${encodeURIComponent(ROOT)}&grep=${encodeURIComponent(kw)}`);
    if (my !== seq) return;
    pending.remove();
    const hits = ((rGrep && rGrep.items) || []).filter((x) => x.line);   // 无 line = 后端还没认 grep(改了没重启)
    if (hits.length) {
      results.appendChild(group("内容", hits.length + (rGrep.truncated ? "+" : "")));
      hits.forEach((it) => results.appendChild(hitRow(it)));
    }
    if (!names.length && !hits.length) results.appendChild(group("没有匹配"));
  }

  input.oninput = () => {
    clearTimeout(timer);
    const kw = input.value.trim();
    if (!kw) { seq++; results.hidden = true; tree.hidden = false; return; }
    timer = setTimeout(() => run(kw), 260);
  };
}

// ---- 中缝拖拽 ----
function initDivider() {
  const side = $("#side"), div = $("#divider");
  side.style.width = Math.min(Math.max(st.sideW || 240, 150), 620) + "px";
  let dragging = false;
  div.onmousedown = (e) => {
    dragging = true; div.classList.add("dragging");
    e.preventDefault(); document.body.style.cursor = "col-resize";
  };
  window.onmousemove = (e) => {
    if (!dragging) return;
    const w = Math.min(Math.max(e.clientX, 150), 620);
    side.style.width = w + "px"; st.sideW = w;
  };
  window.onmouseup = () => {
    if (!dragging) return;
    dragging = false; div.classList.remove("dragging");
    document.body.style.cursor = ""; save();
  };
}

async function main() {
  initGithubThemes(); initEditor(); initSearch(); initDivider(); paintBar(); paintTabs();
  $("#m-side").onclick = () => setSideVisible(mobileIDE()
    ? !document.body.classList.contains("side-open")
    : document.body.classList.contains("side-collapsed"));
  $("#side-mask").onclick = () => setSideVisible(false);
  const fitMobile = () => {
    const t = tabOf();
    captureMobile(t);
    const mobile = mobileIDE();
    if (!mobile) syncMobileModel(t);
    if (mobile) {
      document.body.classList.remove("side-collapsed");
      setSideVisible(appLayoutMode === "compact" ? sideVisibleByLayout.compact : false);
    } else {
      document.body.classList.remove("side-open");
      setSideVisible(appLayoutMode === "compact" ? sideVisibleByLayout.compact : !st.sideCollapsed);
    }
    if (editor) editor.updateOptions({
      ...ideCodeMetrics(),
      ...ideChromeMetrics(),
      minimap: { enabled: !mobile, side: "right", size: "proportional", showSlider: "mouseover",
                 renderCharacters: true, scale: 1, maxColumn: 100 },
      wordWrap: mobile ? "on" : "off",
    });
    if (diffEd) {
      diffEd.updateOptions({ renderSideBySide: !mobile });
      diffEd.getOriginalEditor().updateOptions({ ...ideCodeMetrics(), ...ideChromeMetrics() });
      diffEd.getModifiedEditor().updateOptions({ ...ideCodeMetrics(), ...ideChromeMetrics() });
    }
    if (!diffOn) showEditorSurface();
  };
  MOBILE_MEDIA.addEventListener?.("change", fitMobile);
  $("#btn-save").onclick = () => doSave();
  $("#btn-diff").onclick = () => diffToggle();
  // 取消 = 丢掉改动重新读盘。丢的是这一个标签的内容,得先问
  $("#btn-cancel").onclick = async () => {
    const t = tabOf(); if (!t || !current) return;
    if (t.dirty && !confirm(`丢掉「${t.name}」的改动?`)) return;
    const rel = current, name = t.name;
    tabs.delete(rel); t.model.dispose(); current = null;
    await openFile(rel, name);
  };
  await loadDir("", $("#tree"), 0);
  // 恢复上次开着的那几个标签;last 放最后开,它会成为当前项。
  // last 必须先抄下来:每开一个标签都会走 openTab,那里把 st.last 改成刚开的这个并落盘
  // (它记的是「当前在看哪个」)。原来循环之后再读 st.last,读到的已经是 want 里最后那个,
  // 于是从会话里点开一份文件,出来的是上一次看的那份——交接来的那个文件根本没被打开
  // (2026-08-19 用户实见:点 AGENTS.md 出来 domain.py)。
  const last = st.last;
  const want = Array.isArray(st.tabs) ? st.tabs.filter((x) => x !== last) : [];
  for (const rel of want) await openFile(rel, rel.split("/").pop());
  if (last) await openFile(last, last.split("/").pop());
  applyAppLayout(appLayoutMode, true);
}
main();
