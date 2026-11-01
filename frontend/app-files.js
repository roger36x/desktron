// desktron 前端:文件应用(界面模型改造第三步)
// 文件查看不再按工作流各占一张全局标签:它们收进「文件」这个应用,应用只有一个页面,
// 同一时刻显示一个工作流的文件树。从别处再点另一个工作流的文件,就在这一个页面里换过去。
//
// 与终端应用(app-terminal.js)同构,差别都源于内容是一张同源页面(frontend/pages/files.html,
// Monaco 编辑器)而不是 xterm:
//   1. 那一页自己按 URL 上的 domain 参数拉目录树,所以换工作流就是换 iframe 的地址;
//   2. 展开的目录、打开的文件由那一页按工作区自己存(cd_files_app_<root>),这里只记「在看哪个」;
//   3. 顶栏是项目目录的标签条(形制与浏览器/终端应用共用 .ps-item):开着哪几个目录、
//      当前在看哪个。每个目录自己的文件标签与展开态由那一页按 root 存,与这里无关。
const FILES_PAGE_KEY = "files";                 // pages[].url 里的页 key
const FILES_APP_UID = "desktron.files.0001";   // 与应用注册表里那条一致,程序坞按它认运行态
const FILE_BROWSER_PAGE_KEY = "file-browser";    // 只读浏览器:对话里的文件链接统一落到这一页
const FILE_BROWSER_STATE_KEY = "cd_file_browser";
const FILE_OPEN_LAST_APP_KEY = "cd_file_open_last_app";
let filesRoots = [];         // [{id, name}] 顶栏上开着的项目目录,次序即标签次序
let filesCurrent = null;     // {id, name} 当前显示的那个(仍是它,各处调用点不变)
let _filesRestoring = false;
let _filesLegacy = [];       // 旧版本存在 cd_pages 里的 files 条目,迁移用
let fileBrowserCurrent = (() => {
  try {
    const x = JSON.parse(localStorage.getItem(FILE_BROWSER_STATE_KEY) || "null");
    if (x && typeof x === "object") return {
      root: String(x.root || "_desktop"),
      path: String(x.path || "").replace(/^\/+/, ""),
      line: String(x.line || ""),
      dir: !!x.dir,
    };
  } catch {}
  return { root: "", path: "", line: "" };
})();

// ---- 极简文件浏览器 ---------------------------------------------------------
// Monaco IDE 是编辑工具；对话里的文件链接首先是阅读动作。把阅读做成单例应用：连续点链接
// 只更新这一页里的目录与预览，不再为每个文件制造一张互不相干的裸页面。
function fileBrowserAppDef() {
  const desk = (typeof desktopApps !== "undefined" ? desktopApps : []) || [];
  const doms = (typeof domains !== "undefined" ? domains : []) || [];
  const hit = desk.find((a) => a.page === FILE_BROWSER_PAGE_KEY);
  if (hit) return hit;
  for (const d of doms) {
    const a = (d.apps || []).find((x) => x.page === FILE_BROWSER_PAGE_KEY);
    if (a) return a;
  }
  return { id: FILE_BROWSER_PAGE_KEY, uid: FILE_BROWSER_PAGE_KEY, label: "文件浏览器", page: FILE_BROWSER_PAGE_KEY };
}
function fileBrowserPage() { return pages.find((x) => x.url === FILE_BROWSER_PAGE_KEY) || null; }
function fileBrowserFrame() {
  const p = fileBrowserPage();
  return p && p.el ? p.el.querySelector(".fbr-frame") : null;
}
function fileBrowserSrc(x) {
  let u = `/workbench/page?id=${encodeURIComponent(FILE_BROWSER_PAGE_KEY)}&root=${encodeURIComponent(x.root || "_desktop")}`;
  if (x.path) u += `&path=${encodeURIComponent(x.path)}`;
  if (x.line) u += `&line=${encodeURIComponent(x.line)}`;
  if (x.dir) u += `&dir=1`;
  return u;
}
function fileBrowserRemember(root, path = "", line = "", dir = false) {
  fileBrowserCurrent = {
    root: String(root || "_desktop"),
    path: String(path || "").replace(/^\/+/, ""),
    line: String(line || ""),
    dir: !!dir,
  };
  try { localStorage.setItem(FILE_BROWSER_STATE_KEY, JSON.stringify(fileBrowserCurrent)); } catch {}
  return fileBrowserCurrent;
}
function _fileBrowserBuild(host) {
  host.classList.add("file-browser-host");
  // 普通 iframe 没有尺寸时会退回浏览器默认的 300×150。这个应用与 Monaco IDE 一样是
  // 满铺型宿主，宽高必须从 overlay-content 一路传到 iframe；写在专属宿主上，避免依赖
  // 全局 style.css 的加载次序，也不影响其他普通 HTML 应用的留白。
  host.style.padding = "0";
  host.style.overflow = "hidden";
  host.innerHTML = '<div class="file-browser-app" style="width:100%;height:100%;min-height:0">'
    + '<iframe class="fbr-frame" title="文件浏览器" style="display:block;width:100%;height:100%;border:0"></iframe></div>';
  const f = host.querySelector(".fbr-frame");
  f.onload = () => {
    // 首次赋 src 时 URL 已带当前文件，再 postMessage 一遍会让 iframe 重复读取文件。
    // 只在加载期间目标又发生变化时补发最新位置。
    try {
      const u = new URL(f.contentWindow.location.href);
      const same = (u.searchParams.get("root") || "_desktop") === (fileBrowserCurrent.root || "_desktop")
        && (u.searchParams.get("path") || "") === (fileBrowserCurrent.path || "")
        && (u.searchParams.get("line") || "") === (fileBrowserCurrent.line || "")
        && (!!u.searchParams.get("dir")) === !!fileBrowserCurrent.dir;
      if (!same) f.contentWindow.postMessage({ type: "file-browser-open", ...fileBrowserCurrent }, "*");
    } catch { try { f.contentWindow.postMessage({ type: "file-browser-open", ...fileBrowserCurrent }, "*"); } catch {} }
  };
}
function fileBrowserEnsurePage(show = false) {
  let p = fileBrowserPage();
  if (!p) {
    const a = fileBrowserAppDef();
    openContentPage(FILE_BROWSER_PAGE_KEY, a.label || "文件浏览器", "F", _fileBrowserBuild, false,
                    { app: true, appid: a.uid || a.id });
    p = fileBrowserPage();
  }
  if (show && p) showOverlay(p);
  return p;
}
function openFileBrowser(root, path = "", line = "", show = true, dir = false) {
  // 手机与桌面共用完整文件页；目录、返回入口和最近文件状态保持一致。
  const p = fileBrowserEnsurePage(false);
  if (!p) return null;
  fileBrowserRemember(root, path, line, dir);
  const f = fileBrowserFrame();
  if (f) {
    if (!f.getAttribute("src")) f.setAttribute("src", fileBrowserSrc(fileBrowserCurrent));
    else {
      try { f.contentWindow.postMessage({ type: "file-browser-open", ...fileBrowserCurrent }, "*"); }
      catch { f.setAttribute("src", fileBrowserSrc(fileBrowserCurrent)); }
    }
  }
  if (show) showOverlay(p);
  return p;
}
function openFileBrowserApp(show = true, domainId = "") {
  // 从程序坞重新打开或整页刷新时，应用应回到最后阅读的文件。这里不能再用空 path
  // 覆盖已经持久化的状态；真正从对话打开新文件仍走 openFileBrowser，正常替换它。
  if (fileBrowserCurrent.path) {
    return openFileBrowser(fileBrowserCurrent.root, fileBrowserCurrent.path, fileBrowserCurrent.line, show, fileBrowserCurrent.dir);
  }
  const root = domainId && domainId !== "_desktop"
    ? domainId : ((typeof curDomainId === "string" && curDomainId) || "_desktop");
  return openFileBrowser(root, "", "", show);
}

function filesPage() { return pages.find((x) => x.url === FILES_PAGE_KEY) || null; }
function filesFrameEl() { const p = filesPage(); return p && p.el ? p.el.querySelector(".fa-frame") : null; }
function filesPageSrc(id) { return `/workbench/page?id=files&domain=${encodeURIComponent(id)}`; }
// 应用注册表里那条文件应用的 uid:程序坞按 appid 认运行态,取不到就用内置默认值
function filesAppUid() {
  let uid = FILES_APP_UID;
  const desk = (typeof desktopApps !== "undefined" ? desktopApps : []) || [];
  const doms = (typeof domains !== "undefined" ? domains : []) || [];
  for (const d of doms) for (const a of d.apps || []) if (a.page === "files") uid = a.uid || a.id || uid;
  for (const a of desk) if (a.page === "files") uid = a.uid || a.id || uid;
  return uid;
}
// 工作流名以工作流列表为准(改过名的情况);列表里没有这条(会话 cwd 那路的 abs: 目录)就用调用方给的
function _filesName(id, fallback) {
  const d = (typeof domains !== "undefined" ? domains : []).find((x) => x.id === id);
  return (d && d.name) || fallback || id;
}

// ---- 应用页宿主 ----
function filesEnsurePage(show = false) {
  let p = filesPage();
  if (!p) {
    // 先建后显:showOverlay 要读 p.files 才知道该把页面装载进来
    openContentPage(FILES_PAGE_KEY, "文件", "F", _filesBuild, false, { app: true, appid: filesAppUid() });
    p = filesPage();
    if (p) p.files = true;
  }
  if (show && p) showOverlay(p);
  return p;
}
// 程序坞与“最近应用”打开的是文件应用本身：有上次目录就原样回来，没有才落到当前工作流
//（全局态则是 Desk 根目录）。不能再直接开一张裸 files.html，否则会绕过应用内部的目录标签。
function openFilesApp(show = true, domainId = "") {
  const p = filesEnsurePage(false);
  if (!p) return null;
  if (!filesCurrent) {
    const id = domainId && domainId !== "_desktop"
      ? domainId
      : ((typeof curDomainId === "string" && curDomainId) || "_desktop");
    return openFilesPage({ id, name: _filesName(id, id === "_desktop" ? "Desktron" : id) }, show);
  }
  if (show) showOverlay(p);
  else if (typeof appPageVisible === "function" && appPageVisible(p)) filesShown();
  return p;
}
function _filesBuild(host) {
  host.classList.add("files-host");
  // 顶栏经 _headNode 插槽住进 overlay 页头(#overlay-title 的位置),右侧沿用页头自带的
  // 收起/关闭。标签条的形制与浏览器、终端两个应用共用(.ps-item 那一套),不另画一种。
  const head = document.createElement("span");
  head.className = "fa-head";
  head.innerHTML = '<span class="fa-tabs"></span>';
  host._headNode = head;
  host.innerHTML = '<div class="files-app"><iframe class="fa-frame" title="文件"></iframe></div>';
  filesRenderHead();
}
// 各处入口(工作流面板的文件夹钮、右键菜单、会话 cwd、刷新后的恢复)都走这一个函数,签名不变。
// 已经开着别的工作流:同一个页面换过去,不再多开一页。
function openFilesPage(project, show = true) {
  const id = String((project && project.id) || "");
  if (!id) return null;
  const p = filesEnsurePage(false);
  if (!p) return null;
  const row = { id, name: _filesName(id, project && project.name) };
  // 已经开着就切过去,不重复加一条(同一个目录从会话、面板、右键三处都进得来)
  const at = filesRoots.findIndex((x) => x.id === id);
  if (at < 0) filesRoots.push(row); else filesRoots[at] = row;
  filesCurrent = row;
  filesRenderHead();
  persistFilesPage();
  if (show) showOverlay(p);   // 真正装载那一页是在 filesShown(应用显示出来那一刻)
  else if (overlayOpen && pActive === p) filesShown();
  return p;
}
// 交给编辑器打开某个文件。root 与 openFilesPage 的 id 同一套(域 id 或 abs:<绝对目录>),
// rel 是相对该 root 的路径。
//
// 那一页是随包发的 Monaco 打包产物(app-assets/files/vendor),源码不在这个仓里,也没有
// 「打开这个路径」的入口:URL 上只认 domain / root 两个参数,消息通道只有 Monaco 自己的
// 调度帧。但它启动时会按 `cd_files_app_<domain>` 恢复上次的标签——
//     tabs 里逐个打开,最后打开 last 并激活。
// 而它与我们同源,那份 localStorage 就在手边。所以这里不改它,只是先把「该开哪个文件」
// 写进它自己的恢复态,再让它启动。行号带不过去:它只持久化标签,不持久化光标位置。
function openFileInIde(root, rel) {
  const id = String(root || ""), path = String(rel || "").replace(/^\/+/, "");
  if (!id || !path) return null;
  const key = "cd_files_app_" + id;
  let st = {};
  try { st = JSON.parse(localStorage.getItem(key) || "{}") || {}; } catch {}
  const tabs = (Array.isArray(st.tabs) ? st.tabs : []).filter((x) => x && x !== path);
  tabs.push(path);
  st.tabs = tabs.slice(-12);      // 别把它的标签条撑满:每交接一次就多一个
  st.last = path;
  // 展开到这个文件所在的那一层,否则树是收着的,人还得自己点开一路
  const dirs = new Set(Array.isArray(st.expanded) ? st.expanded : []);
  const segs = path.split("/");
  for (let i = 1; i < segs.length; i++) dirs.add(segs.slice(0, i).join("/"));
  st.expanded = [...dirs];
  try { localStorage.setItem(key, JSON.stringify(st)); } catch {}
  const same = filesCurrent && filesCurrent.id === id;
  const p = openFilesPage({ id, name: id.startsWith("abs:") ? id.slice(4).split("/").pop() : id });
  // 已经开在同一个 root 上:iframe 早就启动过了,src 不变就不会重读上面那份恢复态。
  // 同源,直接让它重新走一遍启动流程。
  if (same) {
    const f = filesFrameEl();
    try { if (f && f.contentWindow) f.contentWindow.location.replace(filesPageSrc(id)); } catch {}
  }
  return p;
}

// 「用其他应用打开」只认注册表里的显式能力声明。应用出现在程序坞不等于能接文件；
// file_open.accept 用扩展名或 * 声明范围，handler 则落到 Desk 已知、可校验的入口。
// iframe 只拿到展示所需的字段，真正执行时还会再查一次注册表，不能借消息调用任意函数。
function fileOpenCandidates(path) {
  const ext = String(path || "").split(".").pop().toLowerCase();
  const rows = [];
  const seen = new Set();
  const push = (a) => {
    const cap = a && a.file_open;
    const accept = cap && Array.isArray(cap.accept) ? cap.accept.map((x) => String(x).toLowerCase().replace(/^\./, "")) : [];
    const key = String((a && (a.uid || a.id)) || "");
    if (!key || seen.has(key) || !cap || !["ide", "browser"].includes(cap.handler)
        || (!accept.includes("*") && !accept.includes(ext))) return;
    seen.add(key);
    rows.push({ key, label: a.label || a.id || "应用", icon: a.icon || "", handler: cap.handler || "" });
  };
  for (const a of ((typeof desktopApps !== "undefined" ? desktopApps : []) || [])) push(a);
  for (const d of ((typeof domains !== "undefined" ? domains : []) || [])) for (const a of (d.apps || [])) push(a);
  return rows;
}

function openFileInApp(key, root, rel) {
  const app = fileOpenCandidates(rel).find((x) => x.key === String(key || ""));
  if (!app) return null;
  let opened = null;
  if (app.handler === "ide") opened = openFileInIde(root, rel);
  else if (app.handler === "browser" && typeof browserOpenTab === "function") {
    const path = String(rel || "").replace(/^\/+/, "");
    const url = `/workbench/view?root=${encodeURIComponent(root || "_desktop")}&path=${encodeURIComponent(path)}`;
    opened = browserOpenTab(url, path.split("/").pop() || path, {}, true);
  }
  if (opened) {
    try { localStorage.setItem(FILE_OPEN_LAST_APP_KEY, app.key); } catch {}
  }
  return opened;
}

// 应用被显示出来(showOverlay 末尾调):这时才把 iframe 指到当前工作流。
// 隐藏时不装载:Monaco 那一页不轻,而刷新之后未必会打开它。
function filesShown() {
  const f = filesFrameEl();
  if (!f || !filesCurrent) return;
  const want = filesPageSrc(filesCurrent.id);
  if ((f.getAttribute("src") || "") !== want) f.setAttribute("src", want);
  filesRenderHead();
}
function filesRenderHead() {
  const p = filesPage();
  if (!p || !p.el || !p.el._headNode) return;
  const strip = p.el._headNode.querySelector(".fa-tabs");
  if (!strip) return;
  strip.innerHTML = "";
  for (const r of filesRoots) {
    const d = document.createElement("div");
    d.className = "ps-item fa-tab" + (filesCurrent && r.id === filesCurrent.id ? " active" : "");
    d.dataset.id = r.id;
    d.title = `${r.name}\n${r.id}`;
    d.innerHTML = '<span class="ps-ic"><img src="/icon-folder.png" alt=""></span>'
      + `<span class="ps-title">${escapeHtml(r.name)}</span>`
      + `<span class="ps-x" title="关闭">${ICONS.close}</span>`;
    d.onclick = () => filesActivateRoot(r.id);
    d.querySelector(".ps-x").onclick = (ev) => { ev.stopPropagation(); filesCloseRoot(r.id); };
    strip.appendChild(d);
  }
  psTabsBindDrag(strip, (ids) => {
    filesRoots.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));
    filesRenderHead(); persistFilesPage();
  });
  const add = document.createElement("button");
  add.className = "fa-new"; add.type = "button"; add.title = "打开另一个目录";
  add.innerHTML = ICONS.plus || "+";
  add.onclick = (ev) => { ev.stopPropagation(); filesPickRoot(ev.currentTarget); };
  strip.appendChild(add);
  p.title = filesCurrent ? filesCurrent.name : "文件";
}

// 切到另一个已开着的目录。一个 iframe 换地址,不为每个目录各留一份 Monaco:
// 那一页启动时会按 cd_files_app_<root> 把上次的文件标签与展开态恢复回来,所以切回去看到的
// 还是原样。代价是切换要重走一次启动(资源已在缓存里,通常不到一秒),换来的是不管开几个
// 目录都只有一份编辑器实例——聊天页那次卡死就是几十份重活页面攒出来的。
function filesActivateRoot(id) {
  const row = filesRoots.find((x) => x.id === id);
  if (!row || (filesCurrent && filesCurrent.id === id)) return;
  filesCurrent = row;
  filesRenderHead();
  persistFilesPage();
  filesShown();
}

// 关掉一个目录标签。关的是当前那个就落到邻座上;一个都不剩时整页收掉——
// 空着的文件应用没有可看的东西,留一张空页比关掉更难理解。
function filesCloseRoot(id) {
  const at = filesRoots.findIndex((x) => x.id === id);
  if (at < 0) return;
  filesRoots.splice(at, 1);
  if (!filesRoots.length) {
    const p = filesPage();
    filesCurrent = null;
    if (p && typeof closePage === "function") closePage(p);
    else filesPageClosed();
    return;
  }
  if (filesCurrent && filesCurrent.id === id) {
    filesCurrent = filesRoots[Math.min(at, filesRoots.length - 1)];
    filesRenderHead();
    persistFilesPage();
    filesShown();
    return;
  }
  filesRenderHead();
  persistFilesPage();
}

// 「+」:列出还没开的目录。工作流本身算一个,它登记的每个代码仓各算一个——
// 后者才是平时真正要看的那一层(一个工作流下常挂着好几个仓)。
function filesRootCandidates() {
  const out = [];
  const seen = new Set(filesRoots.map((x) => x.id));
  for (const d of (typeof domains !== "undefined" ? domains : []) || []) {
    if (d.id && !seen.has(d.id)) { seen.add(d.id); out.push({ id: d.id, name: d.name || d.id }); }
    for (const r of d.repos || []) {
      const id = r.path ? "abs:" + r.path : "";
      if (!id || seen.has(id)) continue;
      seen.add(id);
      out.push({ id, name: r.path.replace(/\/+$/, "").split("/").pop() || r.path });
    }
  }
  return out;
}
function filesPickRoot(anchor) {
  const items = filesRootCandidates();
  if (!items.length) { alert("没有别的目录可开了。"); return; }
  const r = anchor.getBoundingClientRect();
  openCtxMenu(r.left, r.bottom + 6,
    items.map((x) => ({ label: x.name, fn: () => openFilesPage(x) })), "rp-wide");
}
// 应用页被关掉(页头的 × / 程序坞的关闭):这个应用没有后端资源要收,只把「在看哪个」清掉,
// 下次打开从工作流面板重新指定,不再恢复上一次那个。
function filesPageClosed() {
  filesRoots = [];
  filesCurrent = null;
  try { localStorage.removeItem("cd_files"); } catch {}
}

// ---- 持久化 ----
// 应用页本身不进 cd_pages(persistPages 对 app 页返回 null),当前工作流自己存一份:
// 不存的话刷新之后这一页凭空消失,用户得回工作流面板再点一次。
function persistFilesPage() {
  if (_filesRestoring || (typeof _restoringPages !== "undefined" && _restoringPages)) return;
  if (!filesRoots.length) { try { localStorage.removeItem("cd_files"); } catch {} return; }
  try {
    localStorage.setItem("cd_files", JSON.stringify({
      roots: filesRoots.map((x) => ({ id: x.id, name: x.name })),
      cur: filesCurrent ? filesCurrent.id : "",
    }));
  } catch {}
}
// 旧版本把文件页存在 cd_pages 里(kind: "files",每个工作流一条),restorePages 在这里把它们
// 转交过来,迁移只发生一次
function filesAdoptLegacy(items) { _filesLegacy = (items || []).slice(); }
function restoreFilesPage() {
  let d;
  try { d = JSON.parse(localStorage.getItem("cd_files") || "null"); } catch {}
  // 三种存法都认:新的 {roots,cur};上一版的 {id,name}(只有一个目录);
  // 再往前是每个工作流一条的 cd_pages 条目,由 filesAdoptLegacy 转交过来。
  let rows = [];
  let cur = "";
  if (d && Array.isArray(d.roots)) {
    rows = d.roots.filter((x) => x && x.id).map((x) => ({ id: String(x.id), name: x.name || "" }));
    cur = String(d.cur || "");
  } else if (d && d.id) {
    rows = [{ id: String(d.id), name: d.name || "" }];
  }
  if (!rows.length) {
    rows = _filesLegacy.filter((x) => x && x.id).map((x) => ({ id: String(x.id), name: x.name || "" }));
  }
  _filesLegacy = [];
  if (!rows.length) return;
  _filesRestoring = true;
  for (const r of rows) openFilesPage(r, false);
  const hit = rows.find((x) => x.id === cur);
  if (hit) { filesCurrent = filesRoots.find((x) => x.id === hit.id) || filesCurrent; filesRenderHead(); }
  _filesRestoring = false;
  persistFilesPage();
}
