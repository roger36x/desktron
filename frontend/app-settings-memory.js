// 由 app-settings.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 3/4 片
// 存量共享编码子区 → 转为独立 worktree(子区「更多」菜单入口)
// 串行翻篇:在隔离态编码子区的 worktree 里换分支(feat1 合入后接着开 feat2);会话/记忆延续本子区(cwd 不变)。
// 不带 fivew——说明 md 由落地页单独编辑(/workbench/workspace),这里重生成会把用户改过的说明覆盖掉。
let _pendingMem = null;   // panel 点「关键结论」条目时记下路径:进总览后自动就地展开该条(去掉"再开一页"那一跳)
let _cprojGen = 0;   // 总览页异步拉取世代号:快速切换时丢弃过期结果
// Context 区(仿 Claude project):上半=用户添加的文档与飞书链接(经 /workbench/domains/<id>/context,
// 清单注入 AGENTS.md 供 agent 按需读);下半=imports 知识库挂载(只读,复用 _loadImportsList)。
// Context 节的渲染与接线(上传文档 / 贴飞书链接)。2026-08-16 工作流页把这一节撤掉之后
// 暂时没有调用方了;后端端点仍在,留着以便换个位置再挂。要清就连同后端一起清。
async function _wireContextSection(host, addBtn, wsId, importSources, alive) {
  const fmtSize = (b) => (b >= 1024 * 1024 ? (b / 1024 / 1024).toFixed(1) + "MB" : Math.max(1, Math.round(b / 1024)) + "KB");
  const reload = () => _wireContextSection(host, addBtn, wsId, importSources, alive);
  let ctx = { files: [], links: [] };
  try { ctx = await api(`/workbench/domains/${encodeURIComponent(wsId)}/context`); } catch {}
  if (!alive() || !host.isConnected) return;
  host.innerHTML = "";
  // Context 正文:备忘编辑器同款(contenteditable 实时编辑,链接标记 [标题](url),自动保存到 context/.notes.md)
  const ed = document.createElement("div");
  ed.className = "wf-note-ed cp-ctx-ed";
  ed.contentEditable = "true";
  ed.dataset.ph = DeskI18n.tx("暂无上下文");
  _wfEdSet(ed, ctx.notes || "", wsId);
  ed.onclick = (ev) => ev.stopPropagation();
  const edErr = document.createElement("div");
  edErr.className = "wf-note-err";
  edErr.hidden = true;
  let edTimer = null;
  const edSave = async () => {
    const text = _wfNoteRead(ed);
    let r = null;
    try { r = await api(`/workbench/domains/${encodeURIComponent(wsId)}/context`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ op: "notes", text }) }); } catch { r = null; }
    edErr.hidden = !(!r || r.error);
    if (!edErr.hidden) edErr.textContent = DeskI18n.tx("保存失败：{error}", { error: (r && r.error) || DeskI18n.tx("后端不可达(改动后需重启)") });
  };
  ed.oninput = () => { if (edTimer) clearTimeout(edTimer); edTimer = setTimeout(edSave, 600); };
  ed.addEventListener("paste", (ev) => {
    ev.preventDefault();
    const items = [...((ev.clipboardData && ev.clipboardData.items) || [])];
    const img = items.find((x) => x.type && x.type.startsWith("image/"));
    if (img) { _ctxEdInsertImage(ed, wsId, img.getAsFile()); return; }
    const txt = ((ev.clipboardData && ev.clipboardData.getData("text/plain")) || "");
    if (!txt) return;
    const one = txt.trim();
    if (/^https?:\/\/\S+$/.test(one)) { _wfEdPasteLark(ed, one); return; }
    document.execCommand("insertText", false, txt);
  });
  host.appendChild(ed);
  host.appendChild(edErr);
  const delRow = (label, fn) => async (ev) => {
    ev.stopPropagation();
    if (!confirm(DeskI18n.tx("移除「{name}」？", { name: label }))) return;
    try { await fn(); } catch (e) { alert(DeskI18n.tx("移除失败：{error}", { error: e.message })); return; }
    reload();
  };
  for (const f of (ctx.files || [])) {
    const row = document.createElement("div"); row.className = "pv-sub"; row.style.cursor = "default";
    row.innerHTML = `<span class="pv-fic">${ICONS.clipboard}</span><span class="pv-n">${escapeHtml(f.name)}</span><span class="pv-reftag">${fmtSize(f.size)}</span><button class="ti-x cp-ctx-x" title="${DeskI18n.txh("移除")}">×</button>`;
    row.querySelector(".cp-ctx-x").onclick = delRow(f.name, () =>
      api(`/workbench/domains/${encodeURIComponent(wsId)}/context?name=${encodeURIComponent(f.name)}`, { method: "DELETE" }));
    host.appendChild(row);
  }
  for (const l of (ctx.links || [])) {
    const row = document.createElement("div"); row.className = "pv-sub";
    row.title = l.url;
    row.innerHTML = `<span class="pv-fic">${ICONS.book}</span><span class="pv-n">${escapeHtml(l.title || l.url)}</span><span class="pv-reftag">${DeskI18n.txh("飞书")}</span><button class="ti-x cp-ctx-x" title="${DeskI18n.txh("移除")}">×</button>`;
    row.onclick = () => { if (typeof openPage === "function") openPage(l.url, l.title || l.url); };   // 内置标签页打开,回 Desk 不迷路
    row.querySelector(".cp-ctx-x").onclick = delRow(l.title || l.url, () =>
      api(`/workbench/domains/${encodeURIComponent(wsId)}/context?url=${encodeURIComponent(l.url)}`, { method: "DELETE" }));
    host.appendChild(row);
  }
  host.ondragover = (ev) => {
    if ([...((ev.dataTransfer && ev.dataTransfer.items) || [])].some((x) => x.kind === "file")) {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "copy";
      host.classList.add("ctx-dropping");
    }
  };
  host.ondragleave = (ev) => { if (!host.contains(ev.relatedTarget)) host.classList.remove("ctx-dropping"); };
  host.ondrop = async (ev) => {
    host.classList.remove("ctx-dropping");
    const files = [...((ev.dataTransfer && ev.dataTransfer.files) || [])];
    if (!files.length) return;
    ev.preventDefault();
    const imgs = files.filter((f) => f.type && f.type.startsWith("image/"));
    const docs = files.filter((f) => !f.type || !f.type.startsWith("image/"));
    for (const f of imgs) _ctxEdInsertImage(ed, wsId, f);   // 图片内联进正文
    if (!docs.length) return;
    if (edTimer) { clearTimeout(edTimer); await edSave(); }   // 正文未落盘先冲掉,reload 不吞输入
    await _ctxUploadFiles(wsId, docs);
    reload();
  };
  if (importSources.length) {
    const impHost = document.createElement("div"); impHost.className = "cp-imports";
    host.appendChild(impHost);
    _loadImportsList(impHost, importSources, () => alive() && impHost.isConnected);
  }
  addBtn.onclick = (ev) => openAddMenu(ev.currentTarget, [
    { label: DeskI18n.tx("上传文档…"), fn: () => _ctxPickFiles(wsId, reload) },
    { label: DeskI18n.tx("贴飞书文档 / 表格链接…"), fn: async () => {
      let url = ""; try { url = (window.prompt(DeskI18n.tx("粘贴飞书文档 / 表格 / 多维表格链接")) || "").trim(); } catch {}
      if (!url) return;
      let title = ""; try { title = (window.prompt(DeskI18n.tx("标题（可选，便于识别）")) || "").trim(); } catch {}
      try {
        const r = await api(`/workbench/domains/${encodeURIComponent(wsId)}/context`, {
          method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ op: "link", url, title }) });
        if (r && r.error) { alert(DeskI18n.tx("添加失败：{error}", { error: r.error })); return; }
      } catch (e) { alert(DeskI18n.tx("添加失败：{error}", { error: e.message })); return; }
      reload();
    } },
  ]);
}
// 上传文档:浏览器 file input 多选 → 读内容 base64 直传(不依赖壳层文件对话框);单文件限 20MB
function _ctxPickFiles(wsId, done) {
  const inp = document.createElement("input");
  inp.type = "file"; inp.multiple = true; inp.style.display = "none";
  document.body.appendChild(inp);
  inp.onchange = async () => {
    const files = [...(inp.files || [])];
    inp.remove();
    await _ctxUploadFiles(wsId, files);
    done();
  };
  inp.click();
}
async function _ctxEdInsertImage(ed, wsId, file) {   // Context 正文贴图:传 context/assets/ 后内联(备忘同款标记与缩放)
  if (!file) return;
  const rd = new FileReader();
  rd.onload = async () => {
    const b64 = String(rd.result).split(",")[1] || "";
    let r = null;
    try {
      r = await api(`/workbench/domains/${encodeURIComponent(wsId)}/context`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ op: "file", sub: "assets", name: file.name || "贴图.png", data_b64: b64 }), timeout: 60000 });
    } catch { return; }
    if (!r || r.error || !r.rel) return;
    const rel = `domains/${wsId}/context/` + r.rel;
    const img = document.createElement("img");
    img.src = "/workbench/view?raw=1&path=" + encodeURIComponent(rel);
    img.dataset.rel = rel;
    ed.focus();
    try { document.execCommand("insertHTML", false, img.outerHTML); } catch { ed.appendChild(img); }
    ed.querySelectorAll("img").forEach((im) => { if (!im.onclick) _wfImgWire(im, ed); });
    ed.dispatchEvent(new Event("input"));
  };
  rd.readAsDataURL(file);
}
async function _ctxUploadFiles(wsId, files) {   // 逐个 base64 直传;选择器与拖拽共用
  for (const f of files) {
    if (f.size > 20 * 1024 * 1024) { alert(DeskI18n.tx("「{name}」超过 20MB，已跳过", { name: f.name })); continue; }
    const b64 = await new Promise((res, rej) => {
      const rd = new FileReader();
      rd.onload = () => res(String(rd.result).split(",")[1] || "");
      rd.onerror = rej;
      rd.readAsDataURL(f);
    }).catch(() => null);
    if (b64 === null) { alert(DeskI18n.tx("「{name}」读取失败，已跳过", { name: f.name })); continue; }
    try {
      const r = await api(`/workbench/domains/${encodeURIComponent(wsId)}/context`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ op: "file", name: f.name, data_b64: b64 }), timeout: 60000 });
      if (r && r.error) alert(DeskI18n.tx("「{name}」上传失败：{error}", { name: f.name, error: r.error }));
    } catch (e) { alert(DeskI18n.tx("「{name}」上传失败：{error}", { name: f.name, error: e.message })); }
  }
}
// 知识·数据源列表:并发拉多个域的 imports/ 挂载,聚合渲染(工作区页单源,专项页多源)。
// 同名跨来源合并成一行、来源标签并列:挂载是全局清单驱动(kbcache._active_subkbs),各工作区多为
// 同一套,逐源重复列没有信息量;真实差异(某源多挂/残留旧链)仍靠该行标签数量可见。
async function _loadImportsList(host, sources, alive) {
  const bySource = await Promise.all(sources.map(async ({ id, tag }) => {
    try {
      const r = await api(`/workbench/fs?root=${encodeURIComponent(id)}&path=imports`);
      return (r.items || []).filter((x) => x.type === "dir").map((x) => ({ name: x.name, tag }));
    } catch { return []; }
  }));
  if (!alive() || !host.isConnected) return;
  const merged = new Map();   // name → [tag,...];按 sources 声明序聚合(本专项在前),不受各请求返回先后影响
  for (const list of bySource) for (const { name, tag } of list) {
    const tags = merged.get(name) || [];
    if (!tags.includes(tag)) tags.push(tag);
    merged.set(name, tags);
  }
  if (!merged.size) { host.innerHTML = '<div class="cd-proj-empty">' + DeskI18n.txh("没有挂载的知识库。点「+」去知识页挂载，或经包安装（填 git url）带入。") + '</div>'; return; }
  host.innerHTML = "";
  for (const [name, tags] of merged) {
    const row = document.createElement("div"); row.className = "pv-sub"; row.style.cursor = "default";
    row.innerHTML = `<span class="pv-fic">${ICONS.book}</span><span class="pv-n">${escapeHtml(name)}</span>` +
      tags.map((t) => `<span class="pv-reftag">${escapeHtml(t)}</span>`).join("");
    host.appendChild(row);
  }
}
// 项目记忆:读容器共享 band(.memory/<容器>/shared)的成品笔记,列成可就地内联展开的卡片。
// 点标题就地展开正文(在总览内、不再新开一页);panel 点条目会带 _pendingMem 进来,自动展开对应卡片。
const _MEM_SRCN = { lark: "群聊", repo: "代码仓", doc: "文档", other: "其它" };
const _cssEsc = (s) => ((window.CSS && CSS.escape) ? CSS.escape(String(s)) : String(s));
// frontmatter 拆分:开头 --- 到下一个 --- 之间为 yaml,其余为正文;没有 frontmatter 时 yaml 为空。
// 与 _stripFm 同一套边界判定。缺了它会让文件浏览器渲染 md 时抛 ReferenceError、右栏留白(2026-07-30 修)。
function _splitFm(t) {
  const s = String(t || "");
  if (!s.startsWith("---")) return { yaml: "", body: s };
  const e = s.indexOf("\n---", 3);
  if (e < 0) return { yaml: "", body: s };
  const first = s.indexOf("\n");
  const nl = s.indexOf("\n", e + 4);
  return { yaml: s.slice(first + 1, e), body: nl < 0 ? "" : s.slice(nl + 1) };
}
function _stripFm(t) { if (!t || !t.startsWith("---")) return t; const e = t.indexOf("\n---", 3); if (e < 0) return t; const nl = t.indexOf("\n", e + 4); return nl < 0 ? "" : t.slice(nl + 1); }
function _memCardHtml(m) {
  const chip = m.rollup ? `<span class="pvmem-rtag">${DeskI18n.txh("汇总")}</span>` : `<span class="pvmem-chip ${escapeHtml(m.srcType)}">${DeskI18n.txh(_MEM_SRCN[m.srcType] || "其它")}</span>`;
  return `<div class="pvmem-card ${m.rollup ? "roll" : ""}" data-mid="${escapeHtml(m.id)}" data-path="${escapeHtml(m.path)}" data-links="${escapeHtml((m.links || []).join(","))}" data-meta="${escapeHtml(DeskI18n.tx(_MEM_SRCN[m.srcType] || "其它") + " · " + (m.srcLabel || "") + (m.updated ? " · " + DeskI18n.tx("更新 {time}", { time: m.updated }) : "") + " · id " + m.id)}"><div class="cap"><span class="g">${ICONS.memory}</span><span class="n">${escapeHtml(m.summary)}</span>${chip}<span class="chev">${ICONS.enter}</span></div><div class="doc"><div class="doc-in md"></div></div></div>`;
}
// 平铺行(仿 Claude project 页 Memory):摘要 2 行截断 + 更新时间,无来源分组/无卡片框;结构保持
// .pvmem-card/.cap/.doc,点行仍走 _toggleMemCard 就地展开
function _memFlatRowHtml(m) {
  // 来源(originName,聚合自哪个子区工作区)不在行上单独占角标,收进展开后的元信息行,溯源不丢
  return `<div class="pvmem-card mf${m.rollup ? " roll" : ""}" data-mid="${escapeHtml(m.id)}" data-path="${escapeHtml(m.path)}" data-links="${escapeHtml((m.links || []).join(","))}" data-meta="${escapeHtml(DeskI18n.tx(_MEM_SRCN[m.srcType] || "其它") + " · " + (m.srcLabel || "") + (m.originName ? " · " + DeskI18n.tx("来自 {name}", { name: m.originName }) : "") + " · id " + m.id)}"><div class="cap"><div class="mf-tx"><div class="n">${escapeHtml(m.summary)}</div>${m.updated ? `<div class="mf-meta">${DeskI18n.txh("更新 {time}", { time: m.updated })}</div>` : ""}</div><span class="chev">${ICONS.enter}</span></div><div class="doc"><div class="doc-in md"></div></div></div>`;
}
// A(可读化)+ B(分组/汇总):走 /workbench/project-memory 拿解析好的可读列表。
// 容器总览用分组视图(按来源分组、组顶汇总条);工作区落地页/子区工作面传 flat=true 走平铺视图。
// emptyHint 可选,覆盖容器味的默认空态文案。
const _memMoreOpen = new Set();   // 记忆区「展开显示」态(按域)
async function _loadContainerMemory(cid, host, emptyHint, flat) {
  if (!host) return;
  let items;
  try { items = (await api(`/workbench/project-memory?domain=${encodeURIComponent(cid)}`)).items || []; } catch { host.innerHTML = '<div class="pv-hint">' + DeskI18n.txh("读取项目记忆失败") + '</div>'; _pendingMem = null; return; }
  if (!items.length) {
    host.innerHTML = emptyHint || '<div class="pv-hint">还没有沉淀。让会话把关键结论/资产用记忆 skill 存成 shared——职能子区自动落到本项目(各子区同源、起手即读);编码子区(cwd 在真实代码仓)默认落到 desk 的 _desktop 区、不写进真实仓,要归到本项目让会话加 <code>--ws ' + escapeHtml(cid) + '</code>。</div>';
    _pendingMem = null; return;
  }
  let h = "";
  if (flat === "card") {
    host.className = "pv-mem wf-memcard";
    h = _memFlatRowHtml(items[0]);
  } else if (flat) {
    host.className = "pv-mem cd-memflat";
    const more = _memMoreOpen.has(cid);
    const shown = more ? items : items.slice(0, 5);   // 默认收敛前 5 条,超出折叠(后端已排:sot/汇总在前、其余按新→旧)
    h = shown.map(_memFlatRowHtml).join("");
    if (items.length > 5) {
      h += `<div class="wf-expand mem-expand">${more ? DeskI18n.txh("收起") : DeskI18n.txh("展开显示 {count}", { count: items.length - 5 })}</div>`;
    }
  } else {
    host.className = "pv-mem";
    const roll = {}; const grp = {};
    for (const m of items) { if (m.rollup) roll[m.srcType] = m; else (grp[m.srcType] = grp[m.srcType] || []).push(m); }
    for (const st of ["lark", "repo", "doc", "other"]) {
      const list = grp[st] || []; const r = roll[st];
      if (!list.length && !r) continue;
      h += `<div class="pv-mgroup"><div class="pv-mghead"><span>${_MEM_SRCN[st]}</span><span class="pv-mgc">${list.length}</span></div>`;
      if (r) h += _memCardHtml(r);
      for (const m of list) h += _memCardHtml(m);
      h += `</div>`;
    }
  }
  host.innerHTML = h;
  const mx = host.querySelector(".mem-expand");
  if (mx) mx.onclick = () => {
    if (_memMoreOpen.has(cid)) _memMoreOpen.delete(cid); else _memMoreOpen.add(cid);
    _loadContainerMemory(cid, host, emptyHint, flat);
  };
  host.querySelectorAll(".pvmem-card > .cap").forEach((cap) => { cap.onclick = () => _toggleMemCard(cap.parentElement); });
  const want = _pendingMem; _pendingMem = null;
  if (want) {
    const c = host.querySelector(`.pvmem-card[data-path="${_cssEsc(want)}"]`) || host.querySelector(`.pvmem-card[data-mid="${_cssEsc(want)}"]`);
    if (c) { _toggleMemCard(c, true); setTimeout(() => c.scrollIntoView({ block: "center", behavior: "smooth" }), 60); }
  }
}
// 工作流右键「查看记忆」(2026-10-04)：只读展示记忆引擎为该工作流生成的项目模型。
// 完整项目正文直接进入会话上下文，界面展示同一正文，不再维护独立摘要。
// 不提供编辑：项目模型由记忆引擎定期整篇重写，界面上改了也会被覆盖；需要长期保留的约束走 pinned。
function projectMemoryBodyHtml(text) {
  const holder = document.createElement("div");
  holder.innerHTML = md.render(_stripFm(String(text || "")));
  const headings = new Map([["Project context", "项目概况"], ["Decisions & rejections", "已定方案"],
    ["Constraints & principles", "长期约束"], ["Open questions", "待确认问题"]]);
  // 仅处理渲染后的二级标题，不改代码块、正文或持久记录。
  holder.querySelectorAll("h2").forEach((node) => {
    const title = headings.get(node.textContent.trim());
    if (title) node.textContent = DeskI18n.tx(title);
  });
  return holder.innerHTML;
}

function projectMemoryReaderHtml(p, item, text) {
  const updated = item.updated ? `<p class="wf-memory-meta">${DeskI18n.txh("更新于 {time}", { time: item.updated })}</p>` : "";
  return `<header class="wf-memory-heading"><h1>${escapeHtml(p.name || p.id)}</h1>${updated}</header>` +
    `<div class="md wf-memory-body" aria-label="${DeskI18n.txh("项目详情")}">${projectMemoryBodyHtml(text)}</div>` +
    `<p class="wf-memory-source">${DeskI18n.txh("检索编号")} <code>${escapeHtml(item.id || "")}</code></p>`;
}

async function openWorkflowMemory(p) {
  const { back, sheet, body } = openFloatSheet({ title: DeskI18n.tx("项目记忆") });
  sheet.classList.add("wf-memory-sheet");
  _wfSheetFit(back);
  const view = document.createElement("article");
  view.className = "wf-memory-view";
  view.innerHTML = '<div class="pv-hint">' + DeskI18n.txh("正在读取…") + '</div>';
  body.appendChild(view);
  let item;
  try {
    item = ((await api(`/workbench/project-memory?domain=${encodeURIComponent(p.id)}`)).items || [])[0];
  } catch {
    view.innerHTML = '<div class="pv-hint">' + DeskI18n.txh("读取项目记忆失败。") + '</div>';
    return;
  }
  if (!item) {
    view.innerHTML = '<div class="pv-hint">' + DeskI18n.txh("这个工作流尚无项目记忆。会话记录保存后，由记忆引擎生成。") + '</div>';
    return;
  }
  let txt = "";
  try { txt = await api(`/workbench/memory?name=${encodeURIComponent(item.path)}`); }
  catch { view.innerHTML = '<div class="pv-hint">' + DeskI18n.txh("读取项目模型正文失败。") + '</div>'; return; }
  view.innerHTML = projectMemoryReaderHtml(p, item, txt);
}

// 记忆卡改成弹浮层(2026-08-16,与任务详情同一套):就地展开会把下面的卡片顶下去半屏,
// 而一条记忆常常是整篇文档。旧卡片通过搬运 `.doc` 保留溯源跳转，关掉时还回卡片里。
// 当前工作流入口使用上面的只读阅读器，不挂接旧编辑铅笔。
async function _toggleMemCard(card, forceOpen) {
  await _memCardLoad(card);
  const doc = card.querySelector(".doc");
  const home = card;                         // 关掉时还回这里
  const title = (card.querySelector(".n") || {}).textContent || DeskI18n.tx("记忆");
  const { body } = openFloatSheet({
    title,
    onClose: () => {
      if (doc && !doc.isConnected) home.appendChild(doc);
      home.classList.remove("open");        // 箭头跟着复位
    },
  });
  body.appendChild(doc);                     // 借走正文节点
  card.classList.add("open");                // 卡片上的箭头仍指着「已打开」
}

async function _memCardLoad(card) {
  if (!card.dataset.loaded) {
    const docIn = card.querySelector(".doc-in");
    try { const txt = await api(`/workbench/memory?name=${encodeURIComponent(card.dataset.path)}`); docIn.innerHTML = md.render(_stripFm(txt)); }
    catch { docIn.innerHTML = md.render("_" + DeskI18n.tx("读取失败") + "_"); }
    docIn.insertAdjacentHTML("beforeend", `<div class="pvmem-meta">${escapeHtml(card.dataset.meta || "")}</div>`);
    const links = (card.dataset.links || "").split(",").filter(Boolean);
    if (links.length) {
      const host = card.closest(".pv-mem");
      const label = (id) => { const c = host && host.querySelector(`.pvmem-card[data-mid="${_cssEsc(id)}"] .n`); return c ? c.textContent : id; };
      const t = document.createElement("div"); t.className = "pvmem-trace";
      t.innerHTML = DeskI18n.txh("溯源：") + links.map((id) => `<a data-jump="${escapeHtml(id)}">${escapeHtml(label(id))}</a>`).join(" · ");
      t.querySelectorAll("[data-jump]").forEach((a) => a.onclick = (ev) => {
        ev.stopPropagation(); const host2 = card.closest(".pv-mem");
        const c = host2 && host2.querySelector(`.pvmem-card[data-mid="${_cssEsc(a.dataset.jump)}"]`);
        if (c) _toggleMemCard(c, true);   // 换成那一条的浮层(同一时刻只开一张)
      });
      docIn.appendChild(t);
    }
    card.dataset.loaded = "1";
  }
}

// 记忆就地编辑(#75 拍板:不做待审,用户编辑 = verified)。正文整体替换,frontmatter 服务端治理
async function _memCardEdit(card) {
  const docIn = card.querySelector(".doc-in");
  let raw = "";
  try { raw = await api(`/workbench/memory?name=${encodeURIComponent(card.dataset.path)}`); }
  catch { return; }
  const ed = document.createElement("div");
  ed.className = "mem-edit";
  ed.innerHTML = '<textarea class="wf-note-ta mem-edit-ta" spellcheck="false"></textarea>' +
    '<div class="mem-edit-foot"><span class="pv-save mem-edit-save">' + DeskI18n.txh("保存") + '</span><span class="pv-save mem-edit-cancel">' + DeskI18n.txh("取消") + '</span>' +
    '<span class="mem-edit-hint">' + DeskI18n.txh("保存即核实（verified）") + '</span><span class="wf-note-err" hidden></span></div>';
  ed.querySelector("textarea").value = _stripFm(raw).trim() + "\n";
  docIn.replaceChildren(ed);
  const ta = ed.querySelector("textarea");
  ta.style.height = Math.min(420, Math.max(180, ta.scrollHeight + 8)) + "px";
  ta.focus();
  const reload = () => { card.dataset.loaded = ""; card.classList.remove("open"); _toggleMemCard(card, true); };
  ed.querySelector(".mem-edit-cancel").onclick = (ev) => { ev.stopPropagation(); reload(); };
  ed.querySelector(".mem-edit-save").onclick = async (ev) => {
    ev.stopPropagation();
    const err = ed.querySelector(".wf-note-err");
    try {
      const r = await api("/workbench/memory/edit", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ path: card.dataset.path, body: ta.value }) });
      if (!r || r.ok === false) throw new Error((r && r.error) || DeskI18n.tx("保存失败"));
      reload();
    } catch (e) { err.hidden = false; err.textContent = String(e.message || e).slice(0, 120); }
  };
}
