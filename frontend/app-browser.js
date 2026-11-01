// desktron 前端:浏览器应用(界面模型改造第一步)
// 网页不再各占一张全局标签:它们进这个应用,由它自己管地址栏、前进/后退/刷新与内部标签。
// 对外仍然是 openPage(url, title, opts) 那一个入口——终端里的链接、会话里的链接、飞书那几处
// 带 nativeMode 的调用签名都不变,openPage 内部转成「往浏览器应用里开一个标签」。
//
// 原生面板这一条最要紧:外链在 Swift 壳里走 WKWebView 面板(见 app-pages.js 顶部的
// NATIVE_BROWSER 与 nb()),面板是盖在主 webview 之上的独立视图,位置全靠前端上报矩形。
// 它不随 DOM 显隐,所以内部标签切换、应用被收起、应用被别的页盖住这三种情形,都要显式
// 让面板 open / rect / hide,否则会出现「切走了但面板还盖在上面」。本文件里凡是改变
// 「哪一张标签在前台」的地方,末尾都会汇到 browserSyncNative()。
const BROWSER_PAGE_KEY = "browser";              // pages[].url 里的页 key
const BROWSER_APP_UID = "desktron.browser.0001";  // 与 .workbench/apps.json 里那条一致,程序坞按它认运行态
let browserTabs = [];        // {id, url, curUrl, title, native, el, canBack, canFwd, nativeMode, chatId, chatName, tagName}
let browserActiveId = "";
let _browserSeq = 0;
let _browserShownNative = "";   // 当前正显示的原生面板 id(空=没有)。用它判断该 open 还是 rect
let _browserRestoring = false;
const browserControlState = new Map();   // tab id → {status,lease_id,error};宿主令牌始终只在 Swift 内

const _BR_ICON_BACK = "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"15\" height=\"15\" class=\"desk-reviewed-icon\" data-desk-icon=\"_BR_ICON_BACK\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M26.2 16H5.8m8-8.5L5.3 16l8.5 8.5\" /></g></svg>";
const _BR_ICON_FWD = "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"15\" height=\"15\" class=\"desk-reviewed-icon\" data-desk-icon=\"_BR_ICON_FWD\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M5.8 16h20.4m-8-8.5 8.5 8.5-8.5 8.5\" /></g></svg>";
const _BR_ICON_RELOAD = "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"15\" height=\"15\" class=\"desk-reviewed-icon\" data-desk-icon=\"_BR_ICON_RELOAD\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M25.7 12.2A10.4 10.4 0 1 0 25 22.1\" /><path d=\"M26 5.8v7.1h-7.1\" /></g></svg>";
const _BR_ICON_PLUS = "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"13\" height=\"13\" class=\"desk-reviewed-icon\" data-desk-icon=\"_BR_ICON_PLUS\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M16 5.8v20.4M5.8 16h20.4\" /></g></svg>";

function browserPage() { return pages.find((x) => x.url === BROWSER_PAGE_KEY) || null; }
function browserActiveTab() { return browserTabs.find((t) => t.id === browserActiveId) || null; }
// ⌘1..8 跳第 n 张、⌘9 跳最后一张;⌘⇧[ / ⌘⇧] 循环。与终端应用同一套口径,
// 由 app-pages.js 按当前聚焦的应用分派,这里只提供能力。
function browserActivateNth(n) {
  const t = n < 0 ? browserTabs[browserTabs.length - 1] : browserTabs[n];
  if (!t) return false;
  browserActivateTab(t.id);
  return true;
}
function browserCycleTab(dir) {
  if (browserTabs.length < 2) return false;
  const i = browserTabs.findIndex((x) => x.id === browserActiveId);
  if (i < 0) return false;
  browserActivateTab(browserTabs[(i + dir + browserTabs.length) % browserTabs.length].id);
  return true;
}
function browserExternalUrl() { const t = browserActiveTab(); return t ? (t.curUrl || t.url) : ""; }
// 应用注册表里那条浏览器的 uid:程序坞按 appid 认运行态,取不到就用内置默认值
function browserAppUid() {
  let uid = BROWSER_APP_UID;
  const desk = (typeof desktopApps !== "undefined" ? desktopApps : []) || [];
  const doms = (typeof domains !== "undefined" ? domains : []) || [];
  for (const d of doms) for (const a of d.apps || []) if (a.page === BROWSER_PAGE_KEY) uid = a.uid || a.id || uid;
  for (const a of desk) if (a.page === BROWSER_PAGE_KEY) uid = a.uid || a.id || uid;
  return uid;
}
// 哪些地址归浏览器应用:跨源的都归它。同源的本地应用页与文件查看页仍是 iframe,不受影响;
// chat:/term:/files: 这些内部页 key 不是地址,一律不接。
function browserHandlesUrl(url) {
  const s = String(url || "");
  if (!s) return false;
  if (/^(chat|term|files|blob|data|javascript|about):/i.test(s)) return false;
  try { return new URL(s, location.href).origin !== location.origin; } catch { return false; }
}

// ---- 应用页宿主 ----
function browserEnsurePage(show = true) {
  let p = browserPage();
  if (!p) {
    // 先建后显:showOverlay 要读 p.browser 才知道该同步原生面板,所以不让 openContentPage 直接显示
    openContentPage(BROWSER_PAGE_KEY, "浏览器", "◍", _browserBuild, false, { app: true, appid: browserAppUid() });
    p = browserPage();
    if (p) p.browser = true;
  }
  if (show && p) showOverlay(p);
  return p;
}
// 程序坞/应用页点「浏览器」:没有标签时建一张真正的空白标签。这样页头始终有“新标签页”
// 与 +,不会出现内容已经是浏览器、第一层标签栏却完全空着的断层。
function openBrowserApp(show = true) {
  const p = browserEnsurePage(show);
  if (p && !browserTabs.length) browserNewTab(show);
  else if (p && show && !browserActiveTab()) browserActivateTab(browserTabs[0].id, show);
  else if (p && (show || (typeof appPageVisible === "function" && appPageVisible(p)))) browserFocusUrl();
  return p;
}
function browserFocusUrl() {
  const u = browserUrlInput();
  if (!u) return;
  setTimeout(() => { try { u.focus(); u.select(); } catch {} }, 0);
}
function browserHeadNode() { const p = browserPage(); return p && p.el ? p.el._headNode : null; }
function browserUrlInput() { const p = browserPage(); return p && p.el ? p.el.querySelector(".br-url") : null; }
function browserViewEl() { const p = browserPage(); return p && p.el ? p.el.querySelector(".br-view") : null; }


// 空白页只用一枚低对比度地球,与 Claude 的浏览器工作台保持同一层级:它说明这里是网页
// 视口,不再另放一枚应用 Logo 和第二个搜索框,避免页面里出现两个同权重的输入入口。
const _BR_ICON_GLOBE = "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"38\" height=\"38\" class=\"desk-reviewed-icon\" data-desk-icon=\"_BR_ICON_GLOBE\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><circle cx=\"16\" cy=\"16\" r=\"11.2\" /><ellipse cx=\"16\" cy=\"16\" rx=\"5.3\" ry=\"11.2\" /><path d=\"M5.5 12.1h21M5.5 19.9h21\" /></g></svg>";

// 搜索引擎:没有设置页,先放在 localStorage 里,想换的人自己改一行。
// 默认 Google——本机 Chrome 的新标签页用的就是它,两边保持一致。
const BR_ENGINES = {
  google: "https://www.google.com/search?q=",
  bing: "https://www.bing.com/search?q=",
  ddg: "https://duckduckgo.com/?q=",
};
function browserEngine() {
  let k = "";
  try { k = localStorage.getItem("cd_browser_engine") || ""; } catch {}
  return BR_ENGINES[k] || BR_ENGINES.google;
}

// 是地址还是要搜的词。判据从严:带协议、localhost、IP、或者「无空格且含点且点后是字母」
// 才当地址,其余一律去搜。宁可把地址当成词(搜出来第一条多半还是它),也别把一句话
// 拼成 https:// 打开一个不存在的域名——后者是死路,前者还能往下走。
function browserLooksLikeUrl(raw) {
  const v = String(raw || "").trim();
  if (!v || /\s/.test(v)) return false;
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(v)) return true;
  if (browserLocalPath(v)) return true;
  if (/^localhost(:\d+)?(\/|$)/i.test(v)) return true;
  if (/^\d{1,3}(\.\d{1,3}){3}(:\d+)?(\/|$)/.test(v)) return true;
  return /^[^\/\s]+\.[a-z]{2,}(:\d+)?(\/|$)/i.test(v);
}

// 本机路径。原来 `/Users/…/x.html` 会一路落到最后那条域名判据(开头是斜杠,匹配不上),
// 于是被当成一句要搜的话丢给搜索引擎。地址栏里一条以斜杠或 ~ 开头的路径,意图没有第二种。
function browserLocalPath(v) {
  return /^(\/[^\/]|~\/)/.test(String(v || "").trim()) ? String(v).trim() : "";
}

// 本机文件 → 能加载的地址。走后端的 /workbench/view:它按扩展名给 content-type,html 因此
// 渲染成网页而不是源码。file:// 在这里不成立——原生面板用 WKWebView 的 load(URLRequest),
// 那个方法读不了本地文件(要 loadFileURL:allowingReadAccessTo:),给它 file:// 只会白屏。
// 带上 root=abs:<所在目录>:与文件应用同一个口径,这样工作区外的路径(~/work/… 那种)也能开。
// `~` 前端展不开,交给后端 expanduser,那一路不带 root。
function browserLocalUrl(p) {
  const u = location.origin + "/workbench/view?path=";
  const i = p.lastIndexOf("/");
  if (!p.startsWith("/") || i <= 0) return u + encodeURIComponent(p);
  return u + encodeURIComponent(p.slice(i + 1))
    + "&root=" + encodeURIComponent("abs:" + p.slice(0, i));
}

// 地址栏一个入口收两种意图,不让人先选「搜索还是打开」。
function browserSubmitQuery(raw) {
  const v = String(raw || "").trim();
  if (!v) return;
  if (browserLooksLikeUrl(v)) { browserSubmitUrl(v); return; }
  browserSubmitUrl(browserEngine() + encodeURIComponent(v));
}

function _browserBuild(host) {
  host.classList.add("browser-host");
  // 顶栏放标签条,地址栏在它下面——这是浏览器的通常次序:标签是「在看哪几页」,地址栏属于
  // 当前这一页,次序反过来会让人觉得地址栏管着所有标签。标签条经 _headNode 插槽住进 overlay
  // 页头(#overlay-title 的位置),右侧沿用页头自带的收起/关闭;导航钮与地址栏归回应用内部,
  // 它们原来是全局的(#ov-back / #ov-url 那套)。
  const head = document.createElement("span");
  head.className = "br-head";
  head.innerHTML = '<span class="br-tabs"></span>';
  host._headNode = head;
  host.innerHTML =
    '<div class="browser-app">' +
    '<div class="br-bar">' +
    '<span class="head-actions br-nav">' +
    `<button class="br-back" type="button" title="后退" disabled>${_BR_ICON_BACK}</button>` +
    `<button class="br-fwd" type="button" title="前进" disabled>${_BR_ICON_FWD}</button>` +
    `<button class="br-reload" type="button" title="刷新" disabled>${_BR_ICON_RELOAD}</button>` +
    '<button class="br-agent" type="button" title="把当前标签页交给 Agent">交给 Agent</button>' +
    '</span>' +
    '<input class="ov-url br-url" spellcheck="false" autocomplete="off" placeholder="输入网址或搜索">' +
    '<div class="br-progress" role="progressbar" aria-label="网页加载进度" aria-hidden="true"><span></span></div>' +
    '</div>' +
    '<div class="br-view"><div class="br-empty">'
      + '<div class="br-new-page">'
      + `<div class="br-empty-mark">${_BR_ICON_GLOBE}</div>`
      + '<div class="br-empty-title">浏览并验证</div>'
      + '<div class="br-empty-desc">在上方输入网址开始浏览，也可以打开本地页面进行预览。</div>'
      + '<div class="br-empty-local"><span>预览本地应用？</span>'
        + '<button class="br-local-start" type="button">输入本地地址</button>'
      + '</div>'
      + '</div></div></div>' +
    '</div>';

  host.querySelector(".br-back").onclick = () => browserNavAction("back");
  host.querySelector(".br-fwd").onclick = () => browserNavAction("forward");
  host.querySelector(".br-reload").onclick = () => browserNavAction("reload");
  const agentButton = host.querySelector(".br-agent");
  agentButton.style.width = "auto"; agentButton.style.minWidth = "34px"; agentButton.style.padding = "0 8px";
  agentButton.onclick = () => browserToggleAgentControl();
  const u = host.querySelector(".br-url");
  let selGuard = false;   // 鼠标聚焦时,focus 后的 mouseup 会把全选打回光标,吃掉这一次 mouseup
  u.onfocus = () => { const t = browserActiveTab(); u._editingTab = ""; if (t) { u.value = t.curUrl || t.url; } selGuard = true; u.select(); };
  u.oninput = () => { u._editingTab = browserActiveId; };
  u.onmouseup = (e) => { if (selGuard) { selGuard = false; e.preventDefault(); } };
  u.onblur = () => { selGuard = false; u._editingTab = ""; browserRenderNav(); };
  u.onkeydown = (e) => {
    if (e.isComposing || e.keyCode === 229) return;   // 输入法合成态的回车=选字,不是提交
    if (e.key === "Enter") { e.preventDefault(); browserSubmitQuery(u.value); u.blur(); }
    else if (e.key === "Escape") { e.preventDefault(); u.blur(); }
  };
  // 视口尺寸变化(窗口缩放、侧栏开合、标签条增减一行)都要让原生面板跟着改矩形:
  // 面板不在 DOM 里,布局变化不会自动带上它。
  try {
    // 每帧一次就够:拖窗口时 ResizeObserver 会连着回调几十次,每次都往原生发一条 rect,
    // IPC 排起队来主线程就跟着顿。合并到下一帧,矩形取的还是最新值。
    if (window.ResizeObserver) {
      let raf = 0;
      new ResizeObserver(() => {
        if (raf) return;
        raf = requestAnimationFrame(() => { raf = 0; browserSyncNative(); });
      }).observe(host.querySelector(".br-view"));
    }
  } catch {}
  const local = host.querySelector(".br-local-start");
  if (local) local.onclick = () => {
    u.focus();
    if (!u.value) u.value = "http://localhost:";
    try { u.setSelectionRange(u.value.length, u.value.length); } catch {}
  };
  browserRenderTabs();
}

// 网页视口在桌面壳里由独立 WKWebView 覆盖，进度条不能放进 .br-view，否则也会被盖住。
// 它住在地址栏下沿；原生导航事件与 iframe load 事件最终都汇到这里。
function browserRenderLoading() {
  const p = browserPage(), bar = p && p.el && p.el.querySelector(".br-progress");
  if (!bar) return;
  const t = browserActiveTab();
  const progress = t ? Number(t.loadProgress || 0) : 0;
  const shown = progress > 0;
  bar.classList.toggle("active", shown);
  bar.classList.toggle("waiting", !!(t && t.loading));
  bar.classList.toggle("done", shown && !(t && t.loading));
  bar.style.setProperty("--br-progress", String(Math.max(0, Math.min(1, progress / 100))));
  bar.setAttribute("aria-hidden", shown ? "false" : "true");
  if (shown) bar.setAttribute("aria-valuenow", String(Math.round(progress)));
  else bar.removeAttribute("aria-valuenow");
}
function browserSetLoading(t, phase) {
  if (!t) return;
  if (t.loadTimer) { clearTimeout(t.loadTimer); t.loadTimer = 0; }
  if (phase === "start") {
    if (!t.loading) t.loadProgress = 7;
    else t.loadProgress = Math.max(7, Number(t.loadProgress || 0));
    t.loading = true;
    const advance = () => {
      if (!t.loading) return;
      const now = Number(t.loadProgress || 0);
      t.loadProgress = Math.min(90, now + Math.max(0.8, (90 - now) * 0.075));
      if (t.id === browserActiveId) browserRenderLoading();
      t.loadTimer = setTimeout(advance, 320);
    };
    t.loadTimer = setTimeout(advance, 90);
  } else if (phase === "commit") {
    t.loading = true;
    t.loadProgress = Math.max(64, Number(t.loadProgress || 0));
    t.loadTimer = setTimeout(() => browserSetLoading(t, "start"), 320);
  } else if (phase === "finish" || phase === "fail") {
    t.loading = false;
    t.loadProgress = 100;
    t.loadTimer = setTimeout(() => {
      t.loadTimer = 0;
      if (t.loading) return;
      t.loadProgress = 0;
      if (t.id === browserActiveId) browserRenderLoading();
    }, 260);
  }
  if (t.id === browserActiveId) browserRenderLoading();
}
function browserBindFrame(t, frame) {
  frame.addEventListener("load", () => browserSetLoading(t, "finish"));
  frame.addEventListener("error", () => browserSetLoading(t, "fail"));
}

// ---- 标签 ----
function browserOpenTab(url, title, opts = {}, show = true) {
  const p = browserEnsurePage(false);
  if (!p) return null;
  const nat = resolveNativeOpts(url, opts);
  let t = browserTabs.find((x) => !x.agent && x.url === url);
  if (!t) {
    t = {
      id: "browser:" + (++_browserSeq),
      url,
      curUrl: "",
      title: title || url,
      canBack: false,
      canFwd: false,
      nativeMode: nat.nativeMode,
      chatId: nat.chatId,
      chatName: nat.chatName,
      tagName: nat.tagName,
      native: NATIVE_BROWSER,
      el: null,
      loading: false,
      loadProgress: 0,
      loadTimer: 0,
    };
    if (!t.native) {   // 无原生桥(纯浏览器/chrome 壳):退回 iframe,与改造前的外链页一致
      const f = document.createElement("iframe");
      f.className = "overlay-frame br-frame";
      browserBindFrame(t, f);
      browserSetLoading(t, "start");
      f.src = url;
      f.style.display = "none";
      const v = browserViewEl();
      if (v) v.appendChild(f);
      t.el = f;
    }
    browserTabs.push(t);
  } else {
    // 飞书那几处的定制模式原样带过去:同一个地址再打开一次时按新参数更新
    if (nat.feishuMessenger || opts.nativeMode !== undefined) t.nativeMode = nat.nativeMode;
    if (nat.feishuMessenger || opts.chatId !== undefined) t.chatId = nat.chatId;
    if (nat.feishuMessenger || opts.chatName !== undefined) t.chatName = nat.chatName;
    if (nat.feishuMessenger || opts.tagName !== undefined) t.tagName = nat.tagName;
    if (title) t.title = title;
  }
  if (show) browserActivateTab(t.id);
  else {
    if (!browserActiveId) browserActiveId = t.id;
    browserRenderTabs();
    persistBrowserTabs();
  }
  return t;
}
// 点 + 开一张空白标签页:落在浏览器空态并聚焦唯一的地址栏。页面中央不再复制第二个搜索框,
// 这样「标签—地址—网页」三层关系与常规浏览器一致,窄屏也不会多占一层交互空间。
function browserNewTab(show = true) {
  const p = browserEnsurePage(false);
  if (!p) return;
  if (typeof appPageInSide === "function" && appPageInSide(p)) show = false;
  const blank = browserTabs.find((x) => !x.url && !x.curUrl);
  if (blank) { browserActivateTab(blank.id, show); return; }   // 已经有一张空白页就切过去,不重复开
  browserTabs.push({
    id: "browser:" + (++_browserSeq), url: "", curUrl: "", title: "新标签页",
    canBack: false, canFwd: false, nativeMode: "", chatId: "", chatName: "", tagName: "",
    native: NATIVE_BROWSER, el: null, loading: false, loadProgress: 0, loadTimer: 0,
    // 没有地址,不建 iframe / 原生面板,等提交时再落
  });
  browserActivateTab(browserTabs[browserTabs.length - 1].id, show);
  if (show || (typeof appPageVisible === "function" && appPageVisible(p))) browserFocusUrl();
}

function browserActivateTab(id, show = true, source = "user") {
  const t = browserTabs.find((x) => x.id === id);
  if (!t) return;
  // 执行目标由 resource_id 指定；Agent 激活不改变用户的可见标签、面板或焦点。
  if (source === "agent") return;
  const previous = browserActiveTab();
  // Agent 自开/激活不冒充真人接管，也不能借此恢复真人暂停的租约。
  if (source === "user" && previous && previous.id !== id) browserAgentStepAside(previous, "tab_switched");
  browserActiveId = id;
  if (source === "user") browserAgentStepBack(t);
  browserTabs.forEach((x) => { if (x.el) x.el.style.display = x === t ? "" : "none"; });
  browserRenderTabs();
  browserRenderNav();
  persistBrowserTabs();
  const p = browserPage();
  const inSide = typeof appPageInSide === "function" && appPageInSide(p);
  // showOverlay 末尾会把原生面板同步到这张标签;应用已经在前台时不必重走一遍显示流程
  if (show && !inSide && p && (!overlayOpen || pActive !== p)) showOverlay(p);
  else browserSyncNative(true);
}
function browserCloseTab(id, source = "user") {
  const i = browserTabs.findIndex((x) => x.id === id);
  if (i < 0) return;
  const t = browserTabs[i];
  // 关掉的标签留一份摘要,⌘⇧T 走 app-pages.js 的 reopenClosedTab,那边照旧调 openPage → 回到本应用
  if (source !== "agent" && typeof _closedTabs !== "undefined") {
    _closedTabs.push({ kind: "page", url: t.curUrl || t.url, title: t.title, glyph: "", appid: "", badge: "",
                       nativeMode: t.nativeMode, chatId: t.chatId, chatName: t.chatName, tagName: t.tagName });
    if (_closedTabs.length > 10) _closedTabs.shift();
  }
  if (t.native && NATIVE_BROWSER) nb({ action: "close", id: t.id });
  if (t.loadTimer) clearTimeout(t.loadTimer);
  if (_browserShownNative === t.id) _browserShownNative = "";
  if (t.el) t.el.remove();
  browserTabs.splice(i, 1);
  if (!browserTabs.length) {   // 最后一张关掉=这个应用没有页面了,按应用语义整个关掉(程序坞运行点熄灭)
    browserActiveId = "";
    persistBrowserTabs();
    const p = browserPage();
    if (p) closePage(p);
    return;
  }
  if (browserActiveId === t.id) {
    // 关闭恰好正在看的标签仍需显示邻页，但不替 Agent 展开浏览器面板或恢复租约。
    browserActiveId = (browserTabs[i] || browserTabs[i - 1]).id;
    browserActivateTab(browserActiveId, source === "user", source === "agent" ? "close" : "user");
  }
  else { browserRenderTabs(); persistBrowserTabs(); }
}
function browserCloseActiveTab() {
  const t = browserActiveTab();
  if (!t) return false;
  browserCloseTab(t.id);
  return true;
}
// 应用页被关掉(页头的 × / 程序坞的关闭):所有原生面板一并销毁,不留孤儿盖在界面上
function browserPageClosed() {
  for (const t of browserTabs) {
    if (t.native && NATIVE_BROWSER) nb({ action: "close", id: t.id });
    if (t.loadTimer) clearTimeout(t.loadTimer);
    if (t.el) t.el.remove();
  }
  browserTabs = [];
  browserControlState.clear();
  browserActiveId = "";
  _browserShownNative = "";
  persistBrowserTabs();
}
function browserRenderTabs() {
  const p = browserPage();
  if (!p || !p.el) return;
  // 标签条住在页头插槽(_headNode)里,不在页面根下
  const strip = (p.el._headNode || p.el).querySelector(".br-tabs");
  if (!strip) return;
  // 集合没变就地更新。原来每个原生事件(标题、地址、前进后退态)都整条 innerHTML 重建,
  // 一次重建 = N 个节点重造 + N 次 favicon 探测;页面加载期间事件密集,标签条与地址栏就卡。
  const sig = browserTabs.map((t) => t.id).join("|") + "#" + browserActiveId;
  if (strip.dataset.sig === sig) {
    for (const t of browserTabs) {
      const el = strip.querySelector(`.br-tab[data-id="${CSS.escape(t.id)}"]`);
      if (!el) continue;
      const ttl = el.querySelector(".ps-title");
      const txt = t.title || "";
      if (ttl && ttl.textContent !== txt) ttl.textContent = txt;
      const tip = t.title + "\n" + (t.curUrl || t.url);
      if (el.title !== tip) el.title = tip;
    }
    // 空白标签输入网址后标签集合不变，也要在这里收起空态；否则它留在 iframe 上面挡住点击。
    browserRenderEmpty(p);
    return;
  }
  strip.dataset.sig = sig;
  strip.innerHTML = "";
  for (const t of browserTabs) {
    const d = document.createElement("div");
    d.className = "ps-item br-tab" + (t.id === browserActiveId ? " active" : "");
    d.dataset.id = t.id;
    d.title = t.title + "\n" + (t.curUrl || t.url);
    d.innerHTML = '<span class="ps-ic"></span>'
      + `<span class="ps-title">${escapeHtml(t.title)}</span>`
      + `<span class="ps-x" title="关闭">${ICONS.close}</span>`;
    const slot = d.querySelector(".ps-ic");
    if (slot) { slot.innerHTML = _TAB_WINDOW; _tabFav({ url: t.url, curUrl: t.curUrl }, slot); }
    d.onclick = () => browserActivateTab(t.id);
    d.querySelector(".ps-x").onclick = (ev) => { ev.stopPropagation(); browserCloseTab(t.id); };
    strip.appendChild(d);
  }
  psTabsBindDrag(strip, (ids) => {
    browserTabs.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));
    browserRenderTabs(); persistBrowserTabs();
  });
  const add = document.createElement("button");
  add.className = "br-new"; add.type = "button"; add.title = "新建标签";
  add.innerHTML = _BR_ICON_PLUS;
  add.onclick = () => browserNewTab();
  strip.appendChild(add);
  browserRenderEmpty(p);
}
// 空态只在没有标签或当前标签还没有地址时显示。它绝对定位在视口上，无原生桥时盖在
// iframe 之上，所以每次渲染标签都必须同步一次，不能被标签条的就地更新跳过。
function browserRenderEmpty(p) {
  const empty = p && p.el && p.el.querySelector(".br-empty");
  if (!empty) return;
  const cur = browserActiveTab();
  const show = browserTabs.length === 0 || !(cur && (cur.curUrl || cur.url));
  const was = !empty.hidden;
  empty.hidden = !show;
  // 从网页回到空白页时把光标放回唯一的地址栏,下一步可直接输入。
  if (show && !was) browserFocusUrl();
}
function browserRenderNav() {
  // 导航钮与地址栏在应用内部(标签条才在页头插槽里),所以从页面根上找,不是从 _headNode
  const p = browserPage();
  const h = p && p.el;
  if (!h) return;
  const t = browserActiveTab();
  const b = h.querySelector(".br-back"), f = h.querySelector(".br-fwd"), r = h.querySelector(".br-reload");
  if (b) b.disabled = !(t && t.canBack);
  if (f) f.disabled = !(t && t.canFwd);
  if (r) r.disabled = !t;
  const u = h.querySelector(".br-url");
  // 地址栏只显示地址。用页面标题代替 URL 会让它看起来像标题框,也无法直接核验当前位置。
  // 原生网页获得焦点后，宿主 DOM 的 activeElement 可能仍是地址栏。
  // 只保护当前标签正在输入的草稿，单纯聚焦/全选不能阻止真实地址更新。
  if (u && !(document.hasFocus() && document.activeElement === u && t && u._editingTab === t.id)) {
    const value = t ? (t.curUrl || t.url) : "";
    if (u.value !== value) {
      const selected = document.activeElement === u && u.selectionStart === 0 && u.selectionEnd === u.value.length;
      u.value = value;
      if (selected) u.select();
    }
  }
  browserRenderAgentControl();
  browserRenderLoading();
}
function browserNavAction(action) {
  const t = browserActiveTab();
  if (!t) return;
  browserUserTakesControl(t, "user_navigation");
  browserSetLoading(t, "start");
  if (t.native && NATIVE_BROWSER) { nb({ action, id: t.id }); return; }
  if (action === "reload" && t.el) t.el.src = t.el.src;   // iframe 退路:跨源读不到 history,只有刷新做得到
}
function browserSubmitUrl(raw) {
  let url = String(raw || "").trim();
  if (!url) return;
  const local = browserLocalPath(url);
  if (local) url = browserLocalUrl(local);
  else if (!/^[a-z]+:\/\//i.test(url)) url = "https://" + url;
  const t = browserActiveTab();
  if (!t) { openPage(url, _browserHost(url)); return; }
  browserUserTakesControl(t, "user_navigation");
  browserSetLoading(t, "start");
  const wasBlank = !t.url && !t.curUrl;
  t.url = url; t.curUrl = url;
  if (wasBlank || !t.title || t.title === "新标签页") t.title = _browserHost(url);
  if (t.native && NATIVE_BROWSER) nb({ action: "navigate", id: t.id, url });
  else if (t.el) t.el.src = url;
  else if (!t.native) {   // 空白标签此前没有承载体,这会儿才建
    const f = document.createElement("iframe");
    f.className = "overlay-frame br-frame";
    browserBindFrame(t, f);
    f.src = url;
    const v = browserViewEl();
    if (v) v.appendChild(f);
    t.el = f;
  }
  browserRenderTabs();
  persistBrowserTabs();
}

// ---- Agent 控制权：用户显式委托，任何手动切页/导航都收回 ----
function browserRenderAgentControl() {
  const p = browserPage(), button = p && p.el && p.el.querySelector(".br-agent");
  if (!button) return;
  const t = browserActiveTab(), state = t && browserControlState.get(t.id);
  const delegated = !!(state && (state.status === "delegated" || state.status === "active"));
  const paused = !!(state && state.status === "paused");
  // 委托失败要露出来。原生壳发 error 事件时这里原来照旧画成「Agent」,人点了没反应、
  // 也没有任何地方说为什么——2026-09-02 排查「协议跑不起来」时,事实源里租约 0 条,
  // 而这条链上唯一会告诉人失败原因的就是这个事件。
  const failed = !!(state && state.status === "error");
  // 暂停不是失败,也不是没委托:你正在动手,Agent 停手了,静默几秒会自己接回去。
  // 过去这一刻直接把租约销毁、按钮弹回「Agent」,人只看到「点一下就没了」。
  button.textContent = paused ? "已暂停" : (delegated ? "收回" : (failed ? "失败" : "Agent"));
  button.title = paused ? "你正在操作页面,Agent 已暂停;停手后交还控制,须重新观察再操作(点此彻底收回)"
    : (delegated ? "暂停 Agent 并收回当前标签页"
    : (failed ? "委托失败:" + (state.error || "未知原因") + "(再点一次重试)"
    : "把当前可见标签页交给 Agent"));
  button.classList.toggle("err", failed);
  button.classList.toggle("paused", paused);
  button.disabled = !NATIVE_BROWSER || !t || !t.url;
  button.setAttribute("aria-pressed", delegated ? "true" : "false");
}
function browserToggleAgentControl() {
  const t = browserActiveTab();
  if (!NATIVE_BROWSER || !t || !t.url) return;
  const state = browserControlState.get(t.id);
  let sessionId = "";
  const chat = (pActive && String(pActive.url || "").startsWith("chat:")) ? pActive : lastChatPage;
  if (chat && String(chat.url || "").startsWith("chat:")) sessionId = String(chat.url).slice(5);
  // 已让开时点这颗钮 = 明确收回,不是再委托一次
  const holding = !!(state && (state.status === "delegated" || state.status === "paused"));
  nb({ action: holding ? "agentTakeover" : "agentDelegate",
       id: t.id, reason: "user_takeover", sessionId });
}
// 真收回:导航到别的地址、点「收回」。委托的是「这一页的这次任务」,地址一换就可能
// 是另一个站点,授权不该跟着过去,所以这一类不做让开。
function browserUserTakesControl(t, reason) {
  const state = t && browserControlState.get(t.id);
  if (!state || (state.status !== "delegated" && state.status !== "paused")) return;
  nb({ action: "agentTakeover", id: t.id, reason: reason || "user_interaction" });
}
// 让开:人去看别的标签了。那一页还委托着,只是此刻不该被动——切走不扩大授权范围,
// 没有理由销毁它,来回切两下就要重新委托是最容易撞上的一种。
function browserAgentStepAside(t, reason) {
  const state = t && browserControlState.get(t.id);
  if (!state || !["delegated", "paused"].includes(state.status)) return;
  // Agent 自有页允许后台运行；只看一眼再切走不等于真人接管。
  // 已因真实输入暂停的页仍升级为 tab_switched，不能在用户离开后自动抢回。
  if (t.agent && state.status !== "paused") return;
  nb({ action: "agentPause", id: t.id, reason: reason || "tab_switched" });
}
// 接回:人切回被委托的那一页
function browserAgentStepBack(t) {
  const state = t && browserControlState.get(t.id);
  if (!state || state.status !== "paused") return;
  nb({ action: "agentResume", id: t.id });
}
function browserPresentAgentTab(id) {
  browserActivateTab(id, true, "handoff");
  return browserActiveId === id;
}

// Agent 自开标签。原生壳执行 App Control 的 open 动作时调这里——标签模型(顺序、
// 标题、持久化、关闭)都在前端,原生自己建视图会得到一个界面上不存在、用户关不掉的
// 孤儿标签。id 由 Runtime 给,与租约的 resource_id 一致,后续动作据它找视图。
//
// isolated 的标签用隔离存储,不带用户 cookie 与内网 SSO,所以不需要人逐个点头;
// 带身份的那种在 Runtime 侧已经过站点授权或用户确认,到这里时授权已经拿到。
window.__browserAgentOpen = (req) => {
  try {
    if (!req || !req.id || !req.url) return false;
    const p = browserEnsurePage(false);
    if (!p) return false;
    if (!browserTabs.find((x) => x.id === req.id)) {
      browserTabs.push({
        id: req.id, url: req.url, curUrl: req.url, title: req.isolated ? "Agent · 新页" : "Agent",
        canBack: false, canFwd: false, nativeMode: "", chatId: "", chatName: "", tagName: "",
        native: NATIVE_BROWSER, el: null, isolated: !!req.isolated, agent: true,
      });
    }
    // 委托态直接置上:这一页本来就是 Agent 的,不必再走「交给 Agent」那颗钮。
    if (browserControlState.get(req.id)?.status === "paused") return false;
    browserControlState.set(req.id, { status: "delegated", lease_id: "", agent: true });
    browserRenderTabs();
    persistBrowserTabs();
    // 直接创建有布局尺寸的隐藏视图，不走依赖可见面的 browserSyncNative。
    // 用户主动选中此标签时，正常显示路径复用它；创建与执行均不抢用户前台。
    if (NATIVE_BROWSER) {
      nb({ action: "open", id: req.id, url: req.url, background: true,
           rect: { x: 0, y: 0, w: 1280, h: 800 }, isolated: !!req.isolated, browserTab: true,
           allowedMainFrameHosts: Array.isArray(req.allowedMainFrameHosts) ? req.allowedMainFrameHosts : [] });
    }
    return true;
  } catch (e) {
    if (typeof deskLog === "function") deskLog("[app-control] 建标签失败:", (e && e.message) || e);
    return false;
  }
};
// App Control 的标签动作回到前端标签模型执行。Swift 只有 WKWebView 引用，不拥有标签顺序、
// 活动态与持久化；从原生直接隐藏/销毁会让两侧状态分叉。
window.__browserAgentCommand = (req) => {
  try {
    if (!req || !req.id) return false;
    const t = browserTabs.find((x) => x.id === req.id);
    if (!t) return false;
    if (req.action === "activate") {
      if (browserControlState.get(req.id)?.status === "paused") return false;
      // 目标已由租约和 resource_id 确定，不再把“激活执行目标”翻译成 UI 切页。
      return true;
    }
    if (req.action === "handoff") {
      return browserPresentAgentTab(req.id);
    }
    if (req.action === "takeover") {
      browserUserTakesControl(t, "user_handoff");
      return true;
    }
    if (req.action === "close") { browserCloseTab(req.id, "agent"); return true; }
    return false;
  } catch { return false; }
};
window.__browserControlEvent = (event) => {
  if (!event || !event.id) return;
  if (event.type === "delegated") {
    browserControlState.set(event.id, { status: "delegated", lease_id: event.lease_id || "" });
  } else if (event.type === "paused") {
    browserControlState.set(event.id, { status: "paused", lease_id: event.lease_id || "",
                                        error: event.error || "user_interaction" });
  } else if (event.type === "error") {
    browserControlState.set(event.id, { status: "error", error: event.error || "委托失败" });
    if (typeof deskLog === "function") deskLog("[app-control] 委托失败:", event.error || "未知原因");
  } else {
    if (event.type === "revoked" && event.error && typeof deskLog === "function") {
      deskLog("[app-control] 控制权已收回:" + event.error);
    }
    browserControlState.delete(event.id);
  }
  browserRenderAgentControl();
};
// 标签上写什么。域名对网页够用,对本机文件不够:file:// 的 hostname 是空的,
// /workbench/view 的又是 Desk 自己,两者都说明不了在看哪一份。这两种取文件名。
function _browserHost(url) {
  const s = String(url || "");
  const leaf = (p) => { try { return decodeURIComponent(p).replace(/\/+$/, "").split("/").pop() || p; } catch { return p; } };
  try {
    const u = new URL(s);
    if (u.origin === location.origin && u.pathname === "/workbench/view") {
      const p = u.searchParams.get("path") || "";
      if (p) return leaf(p);
    }
    return u.hostname;
  } catch { return s; }
}

// ---- 原生面板:显示/隐藏/改矩形 ----
// 可见 = 浮层开着 + 浏览器应用是当前页 + 宿主节点没被藏起来。三条缺一,面板就该退场。
function browserVisible() {
  const p = browserPage();
  if (!p) return false;
  if (typeof appPageVisible === "function") return appPageVisible(p);
  if (!overlayOpen || pActive !== p) return false;
  const ov = $("#overlay");
  return !!(ov && !ov.hidden && ov.classList.contains("open") && p.el && p.el.isConnected && p.el.style.display !== "none");
}
function browserRect() {
  const v = browserViewEl();
  if (!v) return { x: 0, y: 0, w: 0, h: 0 };
  const r = v.getBoundingClientRect();
  let top = r.top;
  const bottom = r.bottom;
  // 外链正文是盖在 Desk 主 WebView 上方的另一张原生 WKWebView，CSS z-index 再高也盖
  // 不过它。审批在信息卡放不下而迁到右上通知时，若通知落进网页矩形，就把原生正文
  // 的顶边临时让到通知下方；通知收起或处理后，app-approval 会触发一次 rect 恢复。
  const notice = document.getElementById("appr-notice");
  if (notice && !notice.hidden) {
    const n = notice.getBoundingClientRect();
    const right = r.right;
    if (n.width > 0 && n.height > 0 && n.left < right && n.right > r.left
        && n.top < bottom && n.bottom > top) {
      top = Math.min(bottom, Math.max(top, Math.ceil(n.bottom + 8)));
    }
  }
  const p = browserPage();
  // 侧栏网页正文由独立 WKWebView 覆盖在 Desk 主 WebView 上。矩形不能为了露出拖拽柄而
  // 留一条可见缝；把热区宽度随矩形交给原生层，让这 7px 的命中穿透到底下的分隔条。
  const sideResizeHit = typeof appPageInSide === "function" && appPageInSide(p)
    && (!document.body.classList.contains("app-side-double")) ? 7 : 0;
  return {
    x: Math.round(r.left), y: Math.round(top), w: Math.round(r.width),
    h: Math.max(0, Math.round(bottom - top)),
    passThroughLeft: sideResizeHit,
  };
}
// reopen=true:刚切过标签或刚显示这一页,必须发 open(它同时会把别的面板压下去);
// 否则只是尺寸变了,发 rect 即可——open 每次都会把视图提到最前,当作 resize 用太重。
function browserSyncNative(reopen = false) {
  if (!NATIVE_BROWSER) return;
  const t = browserActiveTab();
  const url = t && (t.curUrl || t.url);
  // 空标签由 Desk 自己画空态。若把空 URL 也交给 WKWebView,它会创建一张默认白画布并盖在
  // .br-empty 上；深色主题下看起来就像新标签突然切成白色网页。
  if (!browserVisible() || !t || !t.native || !url) { browserHideNative(); return; }
  const rect = browserRect();
  if (rect.w <= 0 || rect.h <= 0) { browserHideNative(); return; }
  if (reopen || _browserShownNative !== t.id) {
    _browserShownNative = t.id;
    nb({
      action: "open",
      id: t.id,
      url,
      rect,
      nativeMode: t.nativeMode || "",
      chatId: t.chatId || "",
      chatName: t.chatName || "",
      tagName: t.tagName || "",
      isolated: !!t.isolated,   // Agent 自开的隔离标签:原生按它选 ephemeral 存储
      browserTab: true,         // 原生层据此区分浏览器标签与会话内嵌 WKWebView
    });
  } else {
    nb({ action: "rect", id: t.id, rect });
  }
}
// hide 是全局的(原生那侧一次隐藏所有面板),所以只在本应用确实显示过面板时才发,
// 免得把别的页刚打开的内嵌面板一起按下去。
function browserHideNative() {
  if (!NATIVE_BROWSER || !_browserShownNative) return;
  _browserShownNative = "";
  nb({ action: "hide" });
}
// 右侧浏览器可与主区会话里的原生嵌入同时存在。收起右栏时不能发全局 hide，
// 否则主区的飞书/预览面板也会消失；有别的嵌入时只隐藏浏览器当前这一张。
function browserHideNativeForSide() {
  if (!NATIVE_BROWSER || !_browserShownNative) return;
  if (typeof nativeEmbeds !== "undefined" && nativeEmbeds.size) {
    const id = _browserShownNative;
    _browserShownNative = "";
    // 主区还有原生内嵌页时不能用全局 hide；过去退化成 close，导致仅仅收起侧栏就销毁
    // 当前网页的 WKWebView，历史栈、滚动和表单状态全部丢失。按 id 隐藏即可共存。
    nb({ action: "hideOne", id });
    return;
  }
  browserHideNative();
}
// 兜底巡检:有些路径既不过 showOverlay 也不过 hideOverlay(例如别处直接切主区视图),
// 面板会留在屏幕上。每秒查一次「该不该显示」,不该显示且没有别的内嵌面板在用时收掉。
function browserSweepNative() {
  if (!NATIVE_BROWSER || !_browserShownNative) return;
  const t = browserActiveTab();
  if (browserVisible() && t && t.native && (t.curUrl || t.url)) return;
  if (typeof nativeEmbeds !== "undefined" && nativeEmbeds.size) {
    const id = _browserShownNative;
    _browserShownNative = "";
    nb({ action: "hideOne", id });
    return;
  }
  browserHideNative();
}
setInterval(browserSweepNative, 1000);
window.addEventListener("resize", () => browserSyncNative());

// 原生那侧回传标题/地址/前进后退态(app-pages.js 的 window.__browserEvent 转进来)
function browserApplyEvent(e) {
  const t = browserTabs.find((x) => x.id === e.id);
  if (!t) return false;
  // 兼容尚未重启到新原生壳的窗口：旧壳仍会在 commit / finish 回传一次无 phase 的事件。
  // 地址栏导航已经由前端点亮进度，收到这类事件先收尾，避免进度卡在九成。
  if (e.phase && e.phase !== "state") browserSetLoading(t, e.phase);
  else if (!e.phase && t.loading) browserSetLoading(t, "finish");
  if (e.title) t.title = e.title;
  if (e.url) t.curUrl = e.url;
  t.canBack = !!e.canBack;
  t.canFwd = !!e.canFwd;
  if (t.id === browserActiveId) browserRenderNav();
  browserRenderTabs();
  persistBrowserTabs();
  return true;
}

// ---- 持久化 ----
// 应用页本身不进 cd_pages(persistPages 对 app 页返回 null),标签自己存一份,刷新后原样恢复。
// 落盘节流:标题与地址在页面加载期间会连报十几次,每次都 JSON.stringify 加一次
// localStorage 同步写。攒 400ms 写一次,内容一样的直接跳过。
let _persistTimer = 0, _persistLast = "";
function persistBrowserTabs() {
  if (_persistTimer) return;
  _persistTimer = setTimeout(() => { _persistTimer = 0; persistBrowserTabsNow(); }, 400);
}
function persistBrowserTabsNow() {
  if (_browserRestoring || (typeof _restoringPages !== "undefined" && _restoringPages)) return;
  // Agent 自建临时标签不落浏览器标签 localStorage，也不进入“重新打开已关闭标签”；
  // 关闭后不会被浏览器会话恢复。
  const list = browserTabs.filter((t) => !t.agent).map((t) => ({
    url: t.curUrl || t.url,
    title: t.title,
    nativeMode: t.nativeMode || "",
    chatId: t.chatId || "",
    chatName: t.chatName || "",
    tagName: t.tagName || "",
  }));
  const act = browserActiveTab();
  const blob = JSON.stringify({ list, active: act && !act.agent ? (act.curUrl || act.url) : "" });
  if (blob === _persistLast) return;
  _persistLast = blob;
  try { localStorage.setItem("cd_browser", blob); } catch {}
}
function restoreBrowserTabs() {
  let d;
  try { d = JSON.parse(localStorage.getItem("cd_browser") || "null"); } catch {}
  if (!d || !Array.isArray(d.list) || !d.list.length) return;
  _browserRestoring = true;
  for (const x of d.list) {
    browserOpenTab(x.url, x.title, { nativeMode: x.nativeMode, chatId: x.chatId, chatName: x.chatName, tagName: x.tagName }, false);
  }
  _browserRestoring = false;
  const act = browserTabs.find((t) => t.url === d.active) || browserTabs[0];
  if (act) browserActiveId = act.id;
  browserRenderTabs();
  browserRenderNav();
  persistBrowserTabs();
}
