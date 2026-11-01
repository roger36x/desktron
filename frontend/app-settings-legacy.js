// 由 app-settings.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 4/4 片
// openFilesPage 已移进文件应用(app-files.js):文件查看不再按工作流各占一张全局标签,
// 而是一个应用一个页面,同一时刻显示一个工作流。各处入口的签名不变。
// 下面 fbBuild 那套内置文件浏览器已无人调用,确认无回归后再清理。
// ---- 文件浏览态持久化(按项目 id):展开的目录、上次看的文件、左栏宽度 ----
const _fbKey = (id) => "cd_files_" + id;
function _fbLoad(id) { try { return JSON.parse(localStorage.getItem(_fbKey(id)) || "{}") || {}; } catch { return {}; } }
function _fbSave(ctx) {
  ctx.st.expanded = [...ctx.expanded];
  try { localStorage.setItem(_fbKey(ctx.project.id), JSON.stringify(ctx.st)); } catch {}
}
async function fbBuild(root, project) {
  // 左右分栏骨架:左目录树 + 可拖中缝 + 右内容(面包屑 + 滚动区)
  const st = _fbLoad(project.id);
  root.classList.add("fb-split");
  root.innerHTML = "";
  const tree = document.createElement("div"); tree.className = "fb-tree";
  tree.style.width = Math.min(Math.max(st.treeW || 240, 140), 640) + "px";
  // 搜索条 + 结果区:树是懒加载的,只过滤已展开的行等于搜不到,所以走后端 /workbench/fs?q= 递归找
  const sbar = document.createElement("div"); sbar.className = "fb-search";
  sbar.innerHTML = `<span class="fb-search-ic"><svg viewBox="0 0 24 24" width="13" height="13" fill="none"
      stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg></span>`
    + `<input class="fb-search-in" placeholder="搜索文件" spellcheck="false" autocomplete="off">`
    + `<button class="fb-search-x" type="button" hidden title="清空">\u00d7</button>`;
  const treeBody = document.createElement("div"); treeBody.className = "fb-tree-body";
  const results = document.createElement("div"); results.className = "fb-results"; results.hidden = true;
  tree.appendChild(sbar); tree.appendChild(results); tree.appendChild(treeBody);
  const divider = document.createElement("div"); divider.className = "fb-divider";
  const main = document.createElement("div"); main.className = "fb-main";
  const crumb = document.createElement("div"); crumb.className = "fb-crumb fb-crumb-head";
  // 页头左上:收起/展开目录树(状态随浏览态按项目持久化)
  const treeBtn = document.createElement("button");
  treeBtn.className = "fb-tree-toggle"; treeBtn.type = "button"; treeBtn.title = "收起 / 展开目录";
  treeBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><line x1="9" y1="4" x2="9" y2="20"/></svg>';
  crumb.appendChild(treeBtn);
  const crumbSegs = document.createElement("div"); crumbSegs.className = "fb-crumb-segs";
  crumbSegs.innerHTML = `<span class="fb-seg fb-seg-root">${escapeHtml(project.name)}</span>`;
  crumb.appendChild(crumbSegs);
  // 就地编辑条:跟在路径末尾(与 /workbench/view 查看器同款三键),只读态只露「编辑」
  const ebar = document.createElement("div"); ebar.className = "fb-editbar"; ebar.hidden = true;
  ebar.innerHTML = `<button class="fb-eb fb-eb-edit" type="button">编辑</button>`
    + `<button class="fb-eb pri fb-eb-save" type="button" hidden>保存</button>`
    + `<button class="fb-eb fb-eb-cancel" type="button" hidden>取消</button>`
    + `<span class="fb-eb-msg"></span>`;
  crumb.appendChild(ebar);
  root._headNode = crumb;   // 面包屑上提到页头(#overlay-title 位置),右栏不再放独立面包屑条
  const scroll = document.createElement("div"); scroll.className = "fb-content-scroll";
  const view = document.createElement("div"); view.className = "fb-content";
  view.innerHTML = '<div class="empty">选择左侧文件查看内容</div>';
  scroll.appendChild(view);
  main.appendChild(scroll);
  root.appendChild(tree); root.appendChild(divider); root.appendChild(main);

  const ctx = {
    project, st,
    expanded: new Set(Array.isArray(st.expanded) ? st.expanded : []),
    tree, treeBody, results, crumb: crumbSegs, view, scroll, ebar,
    nodes: new Map(),   // relPath → {row, childrenEl, type, depth, loaded, expanded}
    activeRow: null, fileSeq: 0,
    cur: null, editTa: null,   // cur:当前文件 {path,text};editTa:编辑态的 textarea(非空即编辑中)
  };
  ebar.querySelector(".fb-eb-edit").onclick = () => _fbEditEnter(ctx);
  ebar.querySelector(".fb-eb-save").onclick = () => _fbEditSave(ctx);
  ebar.querySelector(".fb-eb-cancel").onclick = () => _fbEditExit(ctx, true);
  const applyTreeHidden = (on) => { tree.hidden = on; divider.hidden = on; treeBtn.classList.toggle("off", on); };
  applyTreeHidden(!!st.treeHidden);
  treeBtn.onclick = () => { ctx.st.treeHidden = !ctx.st.treeHidden; applyTreeHidden(ctx.st.treeHidden); _fbSave(ctx); };
  _fbWireSearch(ctx, sbar);
  _fbWireDivider(ctx, divider, tree);
  await _fbLoadInto(ctx, "", treeBody, 0);
  // 恢复展开态(按层级浅→深:父先展开生成子行,深层行才存在)
  for (const p of [...ctx.expanded].sort((a, b) => a.split("/").length - b.split("/").length)) {
    await _fbExpand(ctx, p);
  }
  if (st.file) fbOpenFile(ctx, st.file);   // 恢复上次看的文件
}
// 文件搜索:输入防抖 220ms 后打后端递归找,结果平铺(带相对路径),点开即在右栏打开。
// 清空即回到目录树,树的展开态原样保留(结果区是另一块 DOM,不动树)。
function _fbWireSearch(ctx, sbar) {
  const inp = sbar.querySelector(".fb-search-in");
  const clr = sbar.querySelector(".fb-search-x");
  let timer = 0, seq = 0;
  const showTree = () => { ctx.results.hidden = true; ctx.treeBody.hidden = false; };
  const run = async (kw) => {
    const my = ++seq;
    let data = null;
    try { data = await api(`/workbench/fs?root=${encodeURIComponent(ctx.project.id)}&q=${encodeURIComponent(kw)}`); } catch {}
    if (my !== seq || !ctx.results.isConnected) return;   // 后发先至:只认最后一次输入的结果
    ctx.treeBody.hidden = true; ctx.results.hidden = false;
    ctx.results.innerHTML = "";
    const items = (data && data.items) || [];
    if (!items.length) {
      const e = document.createElement("div"); e.className = "fb-empty-dir";
      e.textContent = `没有匹配「${kw}」的文件`;
      ctx.results.appendChild(e);
      return;
    }
    for (const it of items) {
      const row = document.createElement("div");
      row.className = "fb-row " + (it.type === "dir" ? "fb-dir" : "fb-file");
      const lead = it.type === "dir" ? `<span class="fb-glyph">${ICONS.folder}</span>`
                                     : `<span class="fb-glyph">${_fbFileIcon(it.name)}</span>`;
      const dir = it.path.slice(0, Math.max(0, it.path.length - it.name.length)).replace(/\/$/, "");
      row.innerHTML = `${lead}<span class="fb-name">${escapeHtml(it.name)}</span>`
        + (dir ? `<span class="fb-res-dir">${escapeHtml(dir)}</span>` : "");
      row.title = it.path;
      if (it.type === "file") row.onclick = () => fbOpenFile(ctx, it.path);
      else row.onclick = () => { inp.value = ""; clr.hidden = true; showTree(); _fbReveal(ctx, it.path); };
      ctx.results.appendChild(row);
    }
    if (data && data.truncated) {
      const m = document.createElement("div"); m.className = "fb-empty-dir";
      m.textContent = "结果过多,只显示前 200 条,再缩小关键词";
      ctx.results.appendChild(m);
    }
  };
  inp.oninput = () => {
    const kw = inp.value.trim();
    clr.hidden = !kw;
    clearTimeout(timer);
    if (!kw) { seq++; showTree(); return; }
    timer = setTimeout(() => run(kw), 220);
  };
  inp.onkeydown = (ev) => { if (ev.key === "Escape") { inp.value = ""; clr.hidden = true; seq++; showTree(); } };
  clr.onclick = () => { inp.value = ""; clr.hidden = true; seq++; showTree(); inp.focus(); };
}
async function _fbLoadInto(ctx, relPath, container, depth) {
  // 懒列一层目录到 container,深度用于缩进
  let items = [];
  try { items = (await api(`/workbench/fs?root=${encodeURIComponent(ctx.project.id)}&path=${encodeURIComponent(relPath)}`)).items || []; } catch {}
  container.innerHTML = "";
  if (!items.length) {
    const e = document.createElement("div"); e.className = "fb-empty-dir";
    e.style.paddingLeft = (depth * 20 + 34) + "px"; e.textContent = "（空）";
    container.appendChild(e); return;
  }
  for (const it of items) container.appendChild(_fbNode(ctx, it, depth));
}
// 扩展名 → 分类图标:文档/图片/配置/脚本/代码/通用,单色不引彩色图标库
function _fbFileIcon(name) {
  const ext = ((name.match(/\.([^.]+)$/) || [])[1] || "").toLowerCase();
  if (["md", "markdown", "txt", "rst", "rtf", "log"].includes(ext)) return ICONS.note;
  if (["png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp", "heic"].includes(ext)) return ICONS.image;
  if (["json", "yaml", "yml", "toml", "ini", "cfg", "conf", "lock", "env", "plist"].includes(ext) || name.startsWith(".")) return ICONS.braces;
  if (["sh", "bash", "zsh", "fish", "command", "bat"].includes(ext)) return ICONS.shellfile;
  if (["js", "mjs", "cjs", "ts", "jsx", "tsx", "py", "swift", "go", "rs", "c", "h", "cpp", "hpp", "java", "rb", "php", "css", "scss", "less", "html", "htm", "sql", "kt", "m", "mm", "vue"].includes(ext)) return ICONS.code;
  return ICONS.file;
}
function _fbNode(ctx, it, depth) {
  // 一个树节点:行 +(目录)子层容器。点目录切展开,点文件加载到右栏
  const wrap = document.createElement("div"); wrap.className = "fb-node";
  const row = document.createElement("div");
  row.className = "fb-row " + (it.type === "dir" ? "fb-dir" : "fb-file");
  row.style.paddingLeft = (depth * 20 + 14) + "px";
  const lead = it.type === "dir"
    ? `<span class="fb-caret">${ICONS.enter}</span>`
    : `<span class="fb-glyph">${_fbFileIcon(it.name)}</span>`;
  row.innerHTML = `${lead}<span class="fb-name">${escapeHtml(it.name)}</span>`;
  wrap.appendChild(row);
  const rec = { row, type: it.type, depth, childrenEl: null, loaded: false, expanded: false };
  ctx.nodes.set(it.path, rec);
  if (it.type === "dir") {
    const ch = document.createElement("div"); ch.className = "fb-children"; ch.hidden = true;
    ch.style.setProperty("--gx", (depth * 20 + 21) + "px");   // 缩进参考线落在本级 caret 中心
    rec.childrenEl = ch; wrap.appendChild(ch);
    row.onclick = () => _fbToggle(ctx, it.path);
  } else {
    row.onclick = () => fbOpenFile(ctx, it.path);
  }
  return wrap;
}
async function _fbToggle(ctx, path) {
  const rec = ctx.nodes.get(path);
  if (!rec || rec.type !== "dir") return;
  if (rec.expanded) {
    rec.expanded = false; rec.childrenEl.hidden = true; rec.row.classList.remove("open");
    ctx.expanded.delete(path);
  } else {
    await _fbExpand(ctx, path);
  }
  _fbSave(ctx);
}
async function _fbExpand(ctx, path) {
  // 强制展开(切换/恢复/面包屑揭示共用):首次展开才懒拉子层
  const rec = ctx.nodes.get(path);
  if (!rec || rec.type !== "dir") return;
  rec.expanded = true; rec.childrenEl.hidden = false; rec.row.classList.add("open");
  ctx.expanded.add(path);
  if (!rec.loaded) { rec.loaded = true; await _fbLoadInto(ctx, path, rec.childrenEl, rec.depth + 1); }
}
function _fbSaveFile(ctx, path, text) {
  return api("/workbench/fs", { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ root: ctx.project.id, path, text }) });
}
// 后端读文件的两种不可编辑回包:原样透出的占位文案,拿到就不给编辑入口(写回会把文件糊成这行字)
const FB_NOEDIT = ["(二进制文件,未预览)", "(文件超过 512KB,未在此预览)"];
async function fbOpenFile(ctx, path) {
  // 右栏看文件:默认只读渲染(md 渲染 / html iframe / 其余语法高亮),点页头「编辑」切原文 textarea
  const seq = ++ctx.fileSeq;   // 快速连点时只让最后一次落地,防陈旧内容覆盖
  if (ctx.activeRow) ctx.activeRow.classList.remove("active");
  const rec = ctx.nodes.get(path);
  if (rec && rec.type === "file") { rec.row.classList.add("active"); ctx.activeRow = rec.row; }
  ctx.st.file = path; _fbSave(ctx);
  _fbCrumb(ctx, path);
  _fbEditExit(ctx, false);   // 切文件先退编辑态,不把上一份的编辑框留在页上
  ctx.cur = null; ctx.ebar.hidden = true;
  ctx.view.className = "fb-content"; ctx.view.innerHTML = '<div class="empty">加载中…</div>';
  ctx.scroll.scrollTop = 0;
  let txt = "";
  try { txt = await api(`/workbench/fs?root=${encodeURIComponent(ctx.project.id)}&path=${encodeURIComponent(path)}&file=1`); }
  catch (e) { if (seq === ctx.fileSeq) ctx.view.innerHTML = `<div class="empty">读取失败：${escapeHtml(e.message)}</div>`; return; }
  if (seq !== ctx.fileSeq) return;
  ctx.cur = { path, text: txt };
  ctx.ebar.hidden = FB_NOEDIT.includes(txt);
  _fbPreview(ctx);
}
function _fbPreview(ctx) {
  // 只读态渲染:按扩展名分三路,内容取 ctx.cur.text(保存后就是刚写回的那份)
  const { path, text } = ctx.cur;
  ctx.view.innerHTML = "";
  if (/\.html?$/i.test(path)) {
    ctx.view.className = "fb-content fb-html";
    const frame = document.createElement("iframe");
    frame.className = "overlay-frame";
    frame.srcdoc = text;
    frame.sandbox = "allow-scripts allow-same-origin";
    ctx.view.appendChild(frame);
  } else if (path.endsWith(".md")) {
    const split = _splitFm(text);   // frontmatter 单独按 yaml 代码块渲染,不混进正文
    ctx.view.className = "fb-content md";
    ctx.view.innerHTML = (split.yaml ? md.render("```yaml\n" + split.yaml + "\n```") : "") + md.render(split.body);
  } else {
    ctx.view.className = "fb-content";
    ctx.view.innerHTML = renderCode(text, path);
  }
}
function _fbEditBtns(ctx, editing) {
  ctx.ebar.querySelector(".fb-eb-edit").hidden = editing;
  ctx.ebar.querySelector(".fb-eb-save").hidden = !editing;
  ctx.ebar.querySelector(".fb-eb-cancel").hidden = !editing;
}
function _fbEditEnter(ctx) {
  // 编辑态:原文(含 frontmatter)进 textarea,填满右栏到底;Cmd/Ctrl+S 保存,Esc 取消
  if (!ctx.cur || ctx.editTa) return;
  ctx.view.className = "fb-content fb-fill";
  ctx.view.innerHTML = "";
  const ta = document.createElement("textarea");
  ta.className = "fb-edit-ta"; ta.spellcheck = false; ta.value = ctx.cur.text;
  ctx.view.appendChild(ta);
  ctx.editTa = ta;
  _fbEditBtns(ctx, true);
  ctx.ebar.querySelector(".fb-eb-msg").textContent = "";
  ta.focus();
  ta.onkeydown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); _fbEditSave(ctx); }
    else if (e.key === "Escape") { e.preventDefault(); _fbEditExit(ctx, true); }
  };
}
// rerender=true 时退回只读渲染;切文件的场景传 false(马上要整体重画,不必多渲一遍)
function _fbEditExit(ctx, rerender) {
  if (!ctx.editTa) return;
  ctx.editTa = null;
  _fbEditBtns(ctx, false);
  ctx.ebar.querySelector(".fb-eb-msg").textContent = "";
  if (rerender && ctx.cur) _fbPreview(ctx);
}
async function _fbEditSave(ctx) {
  if (!ctx.editTa || !ctx.cur) return;
  const msg = ctx.ebar.querySelector(".fb-eb-msg"), text = ctx.editTa.value, path = ctx.cur.path;
  msg.textContent = "保存中…";
  try {
    const r = await _fbSaveFile(ctx, path, text);
    if (r && r.ok === false) throw new Error(r.error || "保存失败");
    if (!ctx.cur || ctx.cur.path !== path) return;   // 存的过程中切走了:只落盘,不动当前视图
    ctx.cur.text = text;
    _fbEditExit(ctx, true);   // 存完退回只读,看到的就是刚写回的内容
  } catch (e) { msg.textContent = "保存失败：" + (e.message || e); }
}
function _fbCrumb(ctx, path) {
  // 面包屑:根 + 各级;点中间目录段在树里揭示(展开各级并滚动到行)
  ctx.crumb.innerHTML = "";
  const root = document.createElement("span"); root.className = "fb-seg fb-seg-root"; root.textContent = ctx.project.name;
  root.onclick = () => { ctx.tree.scrollTop = 0; };
  ctx.crumb.appendChild(root);
  const segs = path ? path.split("/") : [];
  let acc = "";
  segs.forEach((s, i) => {
    acc = acc ? acc + "/" + s : s;
    const sep = document.createElement("span"); sep.className = "fb-sep"; sep.textContent = "/"; ctx.crumb.appendChild(sep);
    const seg = document.createElement("span"); seg.className = "fb-seg"; seg.textContent = s;
    const cur = acc;
    if (i < segs.length - 1) seg.onclick = () => _fbReveal(ctx, cur);
    ctx.crumb.appendChild(seg);
  });
}
async function _fbReveal(ctx, dirPath) {
  // 逐级展开父链,滚动到目标目录行
  let acc = "";
  for (const p of dirPath.split("/")) { acc = acc ? acc + "/" + p : p; await _fbExpand(ctx, acc); }
  _fbSave(ctx);
  const rec = ctx.nodes.get(dirPath);
  if (rec) {
    rec.row.scrollIntoView({ block: "nearest" });
    rec.row.classList.add("fb-flash");
    setTimeout(() => rec.row.classList.remove("fb-flash"), 900);
  }
}
function _fbWireDivider(ctx, divider, tree) {
  let startX = 0, startW = 0;
  const onMove = (e) => { tree.style.width = Math.min(Math.max(startW + (e.clientX - startX), 140), 640) + "px"; };
  const onUp = () => {
    document.removeEventListener("mousemove", onMove); document.removeEventListener("mouseup", onUp);
    document.body.classList.remove("fb-resizing");
    ctx.st.treeW = tree.offsetWidth; _fbSave(ctx);
  };
  divider.onmousedown = (e) => {
    e.preventDefault(); startX = e.clientX; startW = tree.offsetWidth;
    document.body.classList.add("fb-resizing");
    document.addEventListener("mousemove", onMove); document.addEventListener("mouseup", onUp);
  };
}
