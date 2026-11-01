// desktron 前端(app.js 拆分):浮层的页面容器:打开页/原生浏览/外链
// 经典脚本按序加载共享全局作用域,加载顺序见 index.html。勿单独重排。
// 全局标签条已撤(界面模型改造第五步):顶栏就是当前应用或会话自己的顶栏,跨应用切换归程序坞,
// 多开归应用内部。pages 因此收窄成「每个应用一条」加当前开着的会话页,不再渲染成一排标签。
let pages = [];          // {url, title, app, badge, native, el}  一条=一个应用页或一段会话页
let pActive = null;
let lastChatPage = null;   // 最近看过的会话页,收起浮层时回它
let overlayOpen = false;
let overlayReturnView = null;   // 首个标签打开前的主区视图；切换标签不覆盖，收起时原样恢复
// 异步页面恢复只能在启动时那一代导航里生效。用户点击会话／新建会话会推进代际，
// 已经在路上的旧恢复即使更晚结束，也只能建页，不能再把旧页顶回主区。
let _mainNavigationEpoch = 0;
function claimMainNavigation() { _mainNavigationEpoch += 1; return _mainNavigationEpoch; }
function currentMainNavigation() { return _mainNavigationEpoch; }
function ownsMainNavigation(epoch) {
  return epoch === undefined || epoch === null || epoch === _mainNavigationEpoch;
}
let sideAppPage = null;          // 右侧紧凑宿主当前承载的应用页；与主区复用同一个页面节点
let sideAppPage2 = null;         // 第二个紧凑宿主；两栏同时打开时共同占满主区
let lastSideAppPage = null;      // 收起不销毁：会话页头的侧栏钮再次点开时恢复同一页
let lastSideAppPages = [];       // 整组收起时记住两栏，重新打开仍恢复原组合
let sideAppReturnState = null;   // 侧栏展开进主区前的应用与精确页面；收回/最小化不再猜 activeTermId

function appSidePages() { return [sideAppPage, sideAppPage2].filter(Boolean); }
function appPageInSide(p) { return !!p && (sideAppPage === p || sideAppPage2 === p); }
function _appSideSlot(p) { return sideAppPage === p ? 1 : (sideAppPage2 === p ? 2 : 0); }
function _appSideParts(slot) {
  const suffix = slot === 2 ? "-2" : "";
  return {
    side: $("#app-side" + suffix), body: $("#app-side-body" + suffix),
    title: $("#app-side-title" + suffix),
  };
}

// 内置浏览器:原生 WKWebView 桥(仅 Swift 壳有)。外链页走原生面板,绕过 iframe 的 X-Frame-Options/CSP;
// 本地应用页(同源)仍用 iframe。无桥(纯浏览器/chrome 壳)时全部退回 iframe,行为不变。
const NATIVE_BROWSER = !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.browser);
let _nativeForeground = null, _nativeVeiled = false, _nativeVeilTimer = 0;
// 原生网页画在主 WebView 之上,DOM 里的 z-index 再高也盖不住它:哪一层在前面,只能由前端
// 自己裁。判据原先是两个类名(settings-dialog-open / resource-dialog-open),后加的浮层
// 没人记得回这里登记,于是两头都出过事——浮层被网页盖住,以及引导页里点开一个链接,网页
// 被压着不显示,看上去就是「点了没反应」(2026-09-08 用户实见)。
// 现在换成问几何:面板中心那一点此刻归谁。枚举的是宿主(浏览器视口与应用页 iframe,两类,
// 封闭),不是浮层(层出不穷),新加的浮层因此自动被认出来。
const NATIVE_VEIL_CLASS = {   // 快路:类名已经说明有一层铺满窗口,不必等几何
  "settings-dialog-open": "#settings-modal",
  "resource-dialog-open": "#resource-modal",
};
// 前台开页时可以让路的浮层:用户点的就是这个链接,把浮层收掉比压着网页不显示更合他的意图。
// 阻断式的询问不在此列——那种浮层要的正是先答再走,网页该替它让位。
const NATIVE_VEIL_DISMISS = [
  ["#settings-modal", () => (typeof _leaveSettings === "function" ? _leaveSettings() : null)],
  ["#resource-modal", () => (typeof closeResourcePage === "function" ? closeResourcePage(false) : null)],
];
function _nativeHostEl(id) {
  const s = String(id || "");
  if (s.startsWith("lark-ref:") && typeof hsLarkRefNativeHost === "function") {
    return hsLarkRefNativeHost(s);
  }
  if (s.startsWith("embed:")) {
    const owner = typeof nativeEmbeds !== "undefined" ? nativeEmbeds.get(s.slice(6)) : null;
    const p = owner ? pages.find((x) => x.el && x.el.contentWindow === owner) : null;
    return (p && p.el) || null;
  }
  if (typeof browserActiveTab !== "function" || typeof browserViewEl !== "function") return null;
  const t = browserActiveTab();
  return t && t.id === s ? browserViewEl() : null;
}
// 盖在前台面板上的那一层;没有就是 null。
function nativeVeilEl() {
  if (!NATIVE_BROWSER || !_nativeForeground) return null;
  for (const [cls, sel] of Object.entries(NATIVE_VEIL_CLASS)) {
    if (!document.body.classList.contains(cls)) continue;
    const el = typeof document.querySelector === "function" ? document.querySelector(sel) : null;
    return el || document.body;   // 取不到节点也照样算遮挡:类名本身已经是凭据
  }
  const host = _nativeHostEl(_nativeForeground.id), r = _nativeForeground.rect;
  if (!host || !r || !(r.w > 0) || !(r.h > 0)) return null;
  if (typeof document.elementFromPoint !== "function") return null;
  const x = Math.min(Math.max(r.x + r.w / 2, 1), (window.innerWidth || 2) - 1);
  const y = Math.min(Math.max(r.y + r.h / 2, 1), (window.innerHeight || 2) - 1);
  let el = null;
  try { el = document.elementFromPoint(x, y); } catch { return null; }
  // 宿主自己、宿主里的东西、宿主的祖先(视口不接受命中时落到的正是它们)都不算遮挡。
  if (!el || el === host || host.contains(el) || el.contains(host)) return null;
  return el;
}
function nativeVeilDismiss(veil) {
  if (!veil || typeof veil.closest !== "function") return false;
  for (const [sel, close] of NATIVE_VEIL_DISMISS) {
    if (!veil.closest(sel)) continue;
    try { close(); } catch { return false; }
    return !nativeVeilEl();   // 关不掉就当没让路,仍旧暂藏
  }
  return false;
}
function _nativePost(msg) { try { window.webkit.messageHandlers.browser.postMessage(msg); } catch {} }
function syncNativeVeil() {
  if (!NATIVE_BROWSER) return;
  const veiled = !!nativeVeilEl();
  if (veiled === _nativeVeiled) return;
  _nativeVeiled = veiled;
  if (veiled) _nativePost({ action: "hide", reason: "desk-veil" });
  else if (_nativeForeground) _nativePost(_nativeForeground);
}
// 浮层的出现与消失没有统一事件,而这一层的判据是几何,只能等 DOM 动过之后自己回头看一眼。
// 前台没有面板时连看都不看;body 上的类名变化是弹窗最常见的形状,当场就判,其余动静合并到
// 一次延后的复查里,不让流式输出把命中测试打成每帧一次。
function _nativeVeilPoke() {
  if (!NATIVE_BROWSER || !_nativeForeground || _nativeVeilTimer) return;
  _nativeVeilTimer = setTimeout(() => { _nativeVeilTimer = 0; syncNativeVeil(); }, 120);
}
function nb(msg) {
  // 只暂藏,保留同一张网页;弹窗期间的尺寸与切页请求也在此收口,
  // 避免 resize/open 又把网页提到最前。
  const foregroundOpen = msg.action === "open" && !msg.background;
  if (foregroundOpen) _nativeForeground = { ...msg };
  if (msg.action === "rect" && _nativeForeground?.id === msg.id)
    _nativeForeground = { ..._nativeForeground, rect: msg.rect };
  if (msg.action === "hide" || (["hideOne", "close"].includes(msg.action) && _nativeForeground?.id === msg.id))
    _nativeForeground = null;
  if (foregroundOpen || msg.action === "rect") {
    let veil = nativeVeilEl();
    if (veil && foregroundOpen && nativeVeilDismiss(veil)) veil = null;
    if (veil) { syncNativeVeil(); return; }   // 让不动的浮层(阻断式询问)才把网页暂藏
    _nativeVeiled = false;   // 这一条消息本身会把网页摆到前面,暂藏态就此解除
  }
  _nativePost(msg);
}
if (NATIVE_BROWSER) new MutationObserver((recs) => {
  for (const r of recs) if (r.target === document.body && r.attributeName === "class") { syncNativeVeil(); return; }
  _nativeVeilPoke();
}).observe(document.body, { childList: true, subtree: true, attributes: true,
                           attributeFilter: ["class", "hidden", "style"] });
if (NATIVE_BROWSER) nb({ action: "hide", reason: "frontend-init" });

// 通顶壳拖窗:顶部拖拽区按下→通知原生 performDrag。
// webview 会吃掉鼠标事件,原生 isMovableByWindowBackground 穿不过来,只能前端上报。
// 旧壳没有 windowDrag 通道,postMessage 抛错被吞,行为不变。
// 热区原先挂在全局标签条(#pagestack)上,标签条撤掉后改挂各视图自己的顶栏:浮层页头
// (#overlay-head,应用页与 HTML 会话页共用)、终端会话的标题行(#term-head)、资产页的
// 标题条(#conv-head),再加侧栏品牌行。这三条顶栏都在窗口最上沿,与原来的位置感一致。
// 应用自己的标签(.ps-item,浏览器与终端的内部标签)排除在外:按下要切标签,不是拖窗。
const DRAG_ZONE = ".side-brand, #overlay-head, .app-side-head, #term-head, #conv-head";
function _dragZoneHit(e) {
  if (!e.target.closest(DRAG_ZONE)) return false;
  return !e.target.closest("button, input, a, .ps-item");   // 交互控件不拖
}
document.addEventListener("mousedown", (e) => {
  if (e.button !== 0) return;
  if (!_dragZoneHit(e)) return;
  try { window.webkit.messageHandlers.windowDrag.postMessage({}); } catch {}
}, true);
// 双击顶部拖拽区 = 缩放窗口(原生标题栏双击语义,通顶后由桥补回)
document.addEventListener("dblclick", (e) => {
  if (!_dragZoneHit(e)) return;
  try { window.webkit.messageHandlers.windowZoom.postMessage({}); } catch {}
}, true);
function pickFolder() {
  return new Promise((resolve) => {
    if (!NATIVE_BROWSER) {
      let p = ""; try { p = window.prompt("输入要打开的目录绝对路径") || ""; } catch {}
      resolve(p.trim()); return;
    }
    window.__pickFolderCb = (path) => { window.__pickFolderCb = null; resolve((path || "").trim()); };
    nb({ action: "pickFolder", id: "pick" });
  });
}
function overlayBodyRect() {
  const ob = $("#overlay-body");
  if (!ob) return { x: 0, y: 0, w: 0, h: 0 };
  const r = ob.getBoundingClientRect();
  return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) };
}
function isFeishuMessengerUrl(url) {
  try {
    const u = new URL(url, location.href);
    return (u.hostname === "www.feishu.cn" && u.pathname.startsWith("/messenger"))
      || (u.hostname.endsWith(".feishu.cn") && u.pathname.startsWith("/next/messenger"));
  } catch { return false; }
}
function frameRelativeRect(sourceWindow, rect) {
  const framePage = pages.find((p) => p.el && p.el.contentWindow === sourceWindow);
  if (!framePage || !framePage.el || !rect) return null;
  const fr = framePage.el.getBoundingClientRect();
  return {
    x: Math.round(fr.left + Number(rect.x || 0)),
    y: Math.round(fr.top + Number(rect.y || 0)),
    w: Math.round(Number(rect.w || 0)),
    h: Math.round(Number(rect.h || 0)),
  };
}
function activeEmbedPageForWindow(sourceWindow) {
  const p = pages.find((x) => x.el && x.el.contentWindow === sourceWindow);
  const ov = $("#overlay");
  const overlayActuallyOpen = !!(ov && !ov.hidden && ov.classList.contains("open"));
  const slot = _appSideSlot(p), side = slot ? _appSideParts(slot).side : null;
  const sideActuallyOpen = !!(side && !side.hidden && appPageInSide(p));
  if (!p || p.el.style.display === "none") return null;
  if (overlayOpen && overlayActuallyOpen && pActive === p) return p;
  return sideActuallyOpen ? p : null;
}
const nativeEmbeds = new Map();  // embed id -> iframe window that owns it
function openNativeEmbedFromFrame(sourceWindow, payload) {
  if (!NATIVE_BROWSER || !payload || !payload.id || !payload.url) return;
  if (!activeEmbedPageForWindow(sourceWindow)) {
    closeNativeEmbed(payload.id);
    return;
  }
  const rect = frameRelativeRect(sourceWindow, payload.rect);
  if (!rect || rect.w <= 0 || rect.h <= 0) return;
  nb({ action: "close", id: "embed:" + payload.id });
  nativeEmbeds.set(payload.id, sourceWindow);
  nb({
    action: "open",
    id: "embed:" + payload.id,
    url: payload.url,
    rect,
    nativeMode: payload.nativeMode || "",
    chatId: payload.chatId || "",
    chatName: payload.chatName || "",
    tagName: payload.tagName || "",
  });
}
function rectNativeEmbedFromFrame(sourceWindow, payload) {
  if (!NATIVE_BROWSER || !payload || !payload.id) return;
  const owner = nativeEmbeds.get(payload.id);
  if (!owner && activeEmbedPageForWindow(sourceWindow) && payload.url) {
    openNativeEmbedFromFrame(sourceWindow, payload);
    return;
  }
  if (owner !== sourceWindow || !activeEmbedPageForWindow(sourceWindow)) {
    closeNativeEmbed(payload.id);
    return;
  }
  const rect = frameRelativeRect(sourceWindow, payload.rect);
  if (!rect || rect.w <= 0 || rect.h <= 0) return;
  nb({ action: "rect", id: "embed:" + payload.id, rect });
}
function closeNativeEmbed(id) {
  if (!id) return;
  nativeEmbeds.delete(id);
  if (NATIVE_BROWSER) nb({ action: "close", id: "embed:" + id });
}
function closeNativeEmbedsForWindow(sourceWindow) {
  for (const [id, owner] of Array.from(nativeEmbeds.entries())) {
    if (!sourceWindow || owner === sourceWindow) closeNativeEmbed(id);
  }
}
function closeAllNativeEmbeds() {
  closeNativeEmbedsForWindow();
}
function closeInvisibleNativeEmbeds() {
  if (!NATIVE_BROWSER || nativeEmbeds.size === 0) return;
  const ov = $("#overlay");
  const overlayActuallyOpen = !!(ov && !ov.hidden && ov.classList.contains("open"));
  for (const [id, owner] of Array.from(nativeEmbeds.entries())) {
    const p = pages.find((x) => x.el && x.el.contentWindow === owner);
    const slot = _appSideSlot(p), side = slot ? _appSideParts(slot).side : null;
    const sideActuallyOpen = !!(p && side && !side.hidden && appPageInSide(p) && p.el.style.display !== "none");
    if (!p || ((!overlayOpen || !overlayActuallyOpen || pActive !== p || p.el.style.display === "none") && !sideActuallyOpen)) {
      closeNativeEmbed(id);
    }
  }
}

// 原生面板的定制模式(飞书那几处的 nativeMode/chatId/chatName/tagName)按地址归一。
// openPage 与浏览器应用的开标签共用这一份,免得两处各算一遍算出不同结果。
function resolveNativeOpts(url, opts = {}) {
  const feishuMessenger = isFeishuMessengerUrl(url);
  const requestedNativeMode = opts.nativeMode || "";
  const effectiveNativeMode = feishuMessenger
    ? (requestedNativeMode === "lark-select-chat" || requestedNativeMode === "lark-select-chat-no-collapse" ? "lark-select-chat-no-collapse" : "")
    : requestedNativeMode;
  return {
    feishuMessenger,
    nativeMode: effectiveNativeMode,
    chatId: effectiveNativeMode ? (opts.chatId || "") : (feishuMessenger ? "" : (opts.chatId || "")),
    chatName: effectiveNativeMode ? (opts.chatName || "") : "",
    tagName: effectiveNativeMode ? (opts.tagName || "") : "",
  };
}
// 打开一页。网页(跨源地址)不再各占一张全局标签,转交浏览器应用开成它的内部标签;
// 调用方的签名不变——终端/会话里的链接、飞书那几处带 nativeMode 的调用都照旧走这里。
// 同源的本地应用页、文件查看页仍是本函数自己的 iframe 页,不受影响。
function openPage(url, title, opts = {}, show = true) {
  if (!opts.app && typeof browserHandlesUrl === "function" && browserHandlesUrl(url)) {
    return browserOpenTab(url, title, opts, show);
  }
  const nat = resolveNativeOpts(url, opts);
  const feishuMessenger = nat.feishuMessenger;
  const effectiveNativeMode = nat.nativeMode;
  const effectiveChatId = nat.chatId;
  const effectiveChatName = nat.chatName;
  const effectiveTagName = nat.tagName;
  let p = pages.find((x) => x.url === url);
  if (!p) {
    p = {
      url,
      title: title || url,
      app: !!opts.app,
      badge: opts.badge || "",
      appid: opts.appid || "",
      glyph: opts.glyph || "",
      glass: !!opts.glass,
      chromeless: !!opts.chromeless,
      nativeMode: effectiveNativeMode,
      chatId: effectiveChatId,
      chatName: effectiveChatName,
      tagName: effectiveTagName,
    };
    let sameOrigin = false;
    try { sameOrigin = new URL(url, location.href).origin === location.origin; } catch {}
    p.native = NATIVE_BROWSER && !p.app && !sameOrigin;
    if (p.native) {
      p.el = document.createElement("div");
      p.el.style.display = "none";
    } else {
      const f = document.createElement("iframe");
      f.className = "overlay-frame"; f.src = url; f.style.display = "none";
      p.el = f;
    }
    $("#overlay-body").appendChild(p.el);
    pages.push(p);
  } else {
    if (feishuMessenger || opts.nativeMode !== undefined) p.nativeMode = effectiveNativeMode;
    if (feishuMessenger || opts.chatId !== undefined) p.chatId = effectiveChatId;
    if (feishuMessenger || opts.chatName !== undefined) p.chatName = effectiveChatName;
    if (feishuMessenger || opts.tagName !== undefined) p.tagName = effectiveTagName;
    if (opts.glyph !== undefined) p.glyph = opts.glyph || "";
    if (title) p.title = title;
  }
  if (show) showOverlay(p); else persistPages();
  return p;
}
function openContentPage(key, title, glyph, build, show = true, opts = {}) {
  // opts.app + opts.appid:按应用页打开(页头带收起/关闭,程序坞按 appid 认运行态),备忘录同款
  let p = pages.find((x) => x.url === key);
  if (!p) {
    p = { url: key, title: title || key, content: true, glyph: glyph || "·",
          app: !!opts.app, appid: opts.appid || "" };
    p.el = document.createElement("div");
    p.el.className = "overlay-content";
    p.el.style.display = "none";
    p.el.addEventListener("click", (e) => e.stopPropagation());
    $("#overlay-body").appendChild(p.el);   // 先挂树再 build:build 里立即绘制的页面带 isConnected 守卫,游离时画不上
    if (build) build(p.el);
    pages.push(p);
  } else { p.title = title || p.title; }
  if (show) showOverlay(p); else persistPages();
  return p;
}

function appPageVisible(p) {
  if (!p || !p.el || p.el.style.display === "none") return false;
  const slot = _appSideSlot(p);
  if (slot) {
    const side = _appSideParts(slot).side;
    return !!(side && !side.hidden && p.el.isConnected);
  }
  const ov = $("#overlay");
  return !!(!document.body.classList.contains("app-side-double")
    && overlayOpen && pActive === p && ov && !ov.hidden && ov.classList.contains("open"));
}

// 同一页只在主区与紧凑侧栏之间迁移。iframe、Monaco、xterm 与浏览器标签都不重建；
// 页面只收到布局模式变化,自行决定是否把列表收进抽屉。
function setAppPageLayout(p, mode) {
  if (!p || !p.el) return;
  p.layout = mode;
  p.el.dataset.layout = mode;
  if (p.el._headNode) p.el._headNode.dataset.layout = mode;
  const frames = [];
  if (p.el.tagName === "IFRAME") frames.push(p.el);
  p.el.querySelectorAll?.("iframe").forEach((f) => frames.push(f));
  const sync = (f) => {
    try { f.contentDocument.documentElement.dataset.layout = mode; } catch {}
    try { f.contentWindow.postMessage({ type: "desktron-layout", mode }, location.origin); } catch {}
  };
  for (const f of frames) {
    sync(f);
    if (!f._desktronLayoutBound) {
      f._desktronLayoutBound = true;
      f.addEventListener("load", () => sync(f));
    }
  }
}

function appPageShown(p) {
  if (!p) return;
  // show=false 创建的浏览器 iframe / 终端 pane 仍是隐藏态。迁进侧栏时把当前内部标签
  // 就地激活一次；这只改 display 与尺寸，不会把应用再顶回主区。
  if (p.browser && typeof browserActivateTab === "function" && typeof browserActiveId === "string" && browserActiveId)
    browserActivateTab(browserActiveId, false);
  else if (p.browser && typeof browserSyncNative === "function") requestAnimationFrame(() => browserSyncNative(true));
  if (p.terminal && typeof termAppActivateTab === "function" && typeof termAppActiveId === "string" && termAppActiveId)
    termAppActivateTab(termAppActiveId, false);
  else if (p.terminal && typeof termAppShown === "function") termAppShown();
  if (p.files && typeof filesShown === "function") filesShown();
  window.dispatchEvent(new Event("resize"));
}

function _sideTitle(p, slot = _appSideSlot(p) || 1) {
  const title = _appSideParts(slot).title;
  if (!title) return;
  title.replaceChildren();
  if (p && p.el && p.el._headNode) title.appendChild(p.el._headNode);
  else title.textContent = (p && p.title) || "应用";
}

function _syncAppSideShell() {
  const one = _appSideParts(1), two = _appSideParts(2);
  if (one.side) one.side.hidden = !sideAppPage;
  if (two.side) two.side.hidden = !sideAppPage2;
  const count = appSidePages().length;
  document.body.classList.toggle("app-side-open", count > 0);
  document.body.classList.toggle("app-side-double", count > 1);
}

function _clearAppSideSlot(slot) {
  const parts = _appSideParts(slot);
  if (parts.title) parts.title.replaceChildren();
  if (parts.side) parts.side.classList.remove("glass", "chromeless");
}

function _promoteSecondSide() {
  if (sideAppPage || !sideAppPage2) return;
  const p = sideAppPage2;
  sideAppPage = p;
  sideAppPage2 = null;
  _appSideParts(1).body.appendChild(p.el);
  _sideTitle(p, 1);
  const first = _appSideParts(1).side, second = _appSideParts(2).side;
  first.classList.toggle("glass", !!p.glass);
  first.classList.toggle("chromeless", !!p.chromeless);
  if (second) second.hidden = true;
  _clearAppSideSlot(2);
}

// 侧栏承载的是哪个应用,原先只活在内存里,刷新就没了。而切引擎是整页重载(见 app-events.js
// 的引擎菜单),于是人只是换了一家引擎,右边开着的浏览器/终端也一并消失,回来还得自己再点开
// 一次(2026-09-04 用户实见)。记下两栏各是哪个应用,重载后按同一条路摆回去。
// 只记「哪个应用在侧栏」这一层:标签、滚动位置、未提交输入由各应用自己的键负责,不重复存一份。
// 不分引擎:应用页本来就不属于任何一家引擎(它们不进 cd_pages),侧栏跟着它们走。
const _SIDE_APPS_KEY = "cd_side_apps";
let _restoringAppSide = false;
function persistAppSide() {
  if (_restoringAppSide) return;
  const list = appSidePages().map((p) => ({ url: String(p.url || ""), appid: p.appid || "" }));
  try { localStorage.setItem(_SIDE_APPS_KEY, JSON.stringify(list)); } catch {}
}
// 窄屏下侧栏不是并列的一栏,而是盖满整块的临时视图(见 style.css 的 820px 断点)。
// 在手机上刷新之后径直盖住会话不是人要的,那一档不恢复,记录仍然留着。
function _appSideNarrow() {
  try { return window.matchMedia("(max-width: 820px), (max-height: 520px) and (pointer: coarse)").matches; }
  catch { return false; }
}
async function restoreAppSide() {
  if (_appSideNarrow()) return false;
  let list; try { list = JSON.parse(localStorage.getItem(_SIDE_APPS_KEY) || "[]"); } catch {}
  if (!Array.isArray(list)) return false;
  const want = list.slice(0, 2).filter((d) => d && (d.url || d.appid));
  if (!want.length) return false;
  const find = (d) => pages.find((x) => x.app
    && ((d.url && x.url === d.url) || (d.appid && x.appid === d.appid)));
  // 浏览器与终端的页由各自的 restore* 建出来,而终端要先问后端哪些 PTY 还活着,会晚一点出现。
  // 短等它们,不要因为早了半秒就当作「这个应用没开着」而另起一份(同 restoreEngineView 的终端支)。
  for (let i = 0; i < 15 && want.some((d) => !find(d)); i++) {
    await new Promise((r) => setTimeout(r, 100));
  }
  _restoringAppSide = true;
  try {
    for (const d of want) {
      const p = find(d);
      // 引擎快照把这一页选成了主区:主区的意愿更近,不把它从主区拽回侧栏。
      if (p && pActive === p) continue;
      if (p) { showAppSide(p); continue; }
      // 页没建出来——这个应用没有自己的持久化键(备忘录、画板等)。按它自己的入口重开到侧栏,
      // 与点顶栏「最近应用」同一条路;找不到这个应用(已卸载)就跳过,不留空壳。
      const found = typeof _allApps === "function"
        ? _allApps().find((x) => x.key === d.appid) : null;
      // agentic 应用不认「开到侧栏」这个位置(见 _openDomainApp 的 compact),交给它只会收掉
      // 刚摆好的另一栏、再占住主区。这一类不重开,人再点一次即可。
      if (found && !found.a.agentic && typeof _openDomainApp === "function") {
        _openDomainApp(found.a, found.domainId, "side");
      }
    }
  } finally { _restoringAppSide = false; }
  persistAppSide();
  return !!appSidePages().length;
}

// p 省略时收起整组；传入具体页时只收起那一栏。双栏关掉一栏后，余下页面自动回到
// 原先可拖宽的第一栏，不因“第二栏”这个实现细节改变人的空间预期。
function hideAppSide(p) {
  const targets = p ? (appPageInSide(p) ? [p] : []) : appSidePages();
  if (!targets.length) return p || null;
  if (!p) lastSideAppPages = targets.slice(0, 2);
  for (const page of targets) {
    const slot = _appSideSlot(page);
    if (!slot) continue;
    if (page.browser && typeof browserHideNativeForSide === "function") browserHideNativeForSide();
    else if (page.browser && typeof browserHideNative === "function") browserHideNative();
    closeNativeEmbedsForWindow(page.el && page.el.contentWindow);
    page.el.style.display = "none";
    $("#overlay-body").appendChild(page.el);
    setAppPageLayout(page, "main");
    lastSideAppPage = page;
    if (slot === 1) sideAppPage = null;
    else sideAppPage2 = null;
    _clearAppSideSlot(slot);
  }
  _promoteSecondSide();
  _syncAppSideShell();
  window.dispatchEvent(new Event("resize"));
  syncOverlayCornerAction();
  persistAppSide();
  return targets.length === 1 ? targets[0] : targets;
}

function showAppSide(p, returnPage) {
  if (!p || !p.app || !p.el) return null;
  if (appPageInSide(p)) { appPageShown(p); return p; }
  // 第三个页面不继续挤压：固定保留左栏，替换右栏。正常入口在双栏时已没有主区可继续点，
  // 这条主要兜住快捷键与程序调用，行为保持确定。
  if (sideAppPage && sideAppPage2) hideAppSide(sideAppPage2);
  const slot = sideAppPage ? 2 : 1;
  const { side, body } = _appSideParts(slot);
  if (!side || !body) return null;
  // 从主区缩进侧栏的少见路径:先让主区回到最近会话,再迁移节点,避免 pActive 指向游离页。
  if (pActive === p) {
    const exact = returnPage && returnPage !== p && pages.includes(returnPage) ? returnPage : null;
    const home = exact || overlayHomePage();
    if (home && home !== p) showOverlay(home);
    else hideOverlay(false);
  }
  if (slot === 1) sideAppPage = p;
  else sideAppPage2 = p;
  lastSideAppPage = p;
  body.appendChild(p.el);
  p.el.style.display = "";
  setAppPageLayout(p, "compact");
  _sideTitle(p, slot);
  side.classList.toggle("glass", !!p.glass);
  side.classList.toggle("chromeless", !!p.chromeless);
  _syncAppSideShell();
  if (p.appid && typeof _appRecentPush === "function") _appRecentPush(p.appid);
  if (typeof renderHeadRecent === "function") renderHeadRecent();
  appPageShown(p);
  syncOverlayCornerAction();
  persistAppSide();
  return p;
}

// 会话页右上角只负责给右侧应用栏开合，不再销毁会话。优先恢复刚收起的应用；
// 初次使用时复用“最近应用”第一枚的既有打开路径，桌面与手机共用同一口径。
function toggleAppSide() {
  if (appSidePages().length) { hideAppSide(); return true; }
  const restore = lastSideAppPages.filter((p) => pages.includes(p)).slice(0, 2);
  if (restore.length) {
    lastSideAppPages = [];
    restore.forEach((p) => showAppSide(p));
    return true;
  }
  if (lastSideAppPage && pages.includes(lastSideAppPage)) {
    showAppSide(lastSideAppPage);
    return true;
  }
  if (typeof renderHeadRecent === "function") renderHeadRecent();
  const recent = document.querySelector("#ov-recent .ov-rc");
  if (recent) { recent.click(); return true; }
  return false;
}

function syncOverlayCornerAction() {
  const sideBtn = $("#ov-close"), infoBtn = $("#ov-info"), ov = $("#overlay");
  if (!sideBtn || !infoBtn || !ov) return;
  const chat = ov.classList.contains("hs-head");
  const info = chat && typeof hsSituationCornerState === "function"
    ? hsSituationCornerState() : { available: false, inline: false, open: false };
  const sideLabel = chat ? (appSidePages().length ? "收起侧边栏" : "打开侧边栏") : "关闭";
  sideBtn.disabled = false;
  sideBtn.title = sideLabel;
  sideBtn.setAttribute("aria-label", sideLabel);
  const infoLabel = info.inline ? "信息栏已显示" : (info.open ? "收起信息栏" : "打开信息栏");
  infoBtn.disabled = !chat || !info.available || info.inline;
  infoBtn.classList.toggle("on", !!(chat && info.open));
  infoBtn.setAttribute("aria-expanded", chat ? String(info.inline || info.open) : "false");
  infoBtn.title = infoLabel;
  infoBtn.setAttribute("aria-label", infoLabel);
}

function expandSideApp(p = sideAppPage) {
  if (!p) return;
  // 此刻 pActive 才是人眼里真正露着的主区页面。之后 showOverlay(p) 会把它覆盖，
  // activeTermId 又可能被会话列表的异步恢复改写，不能等到缩回时再猜“上一次会话”。
  sideAppReturnState = {
    app: p,
    page: overlayOpen && pActive && pActive !== p ? pActive : null,
    sides: appSidePages().slice(),
  };
  hideAppSide();
  showOverlay(p);
}

// 主区全屏应用可迁回右侧紧凑宿主。页面节点只换宿主，不重建 iframe / 编辑器，
// 因此标签、滚动位置与未提交输入都保持原样；showAppSide 同时负责回到当前会话。
function collapseMainAppToSide() {
  const p = pActive;
  if (!p || !p.app || appPageInSide(p)) return false;
  if (isMobileView()) { minimizeMainApp(); return true; }
  const home = sideAppReturnState && sideAppReturnState.app === p ? sideAppReturnState.page : null;
  const sides = sideAppReturnState && sideAppReturnState.app === p
    ? (sideAppReturnState.sides || []).filter((x) => pages.includes(x)).slice(0, 2) : [];
  sideAppReturnState = null;
  if (sides.length) {
    sides.forEach((x) => showAppSide(x, x === p ? home : null));
    return true;
  }
  showAppSide(p, home);
  return true;
}
// 手机侧栏与主区同样占满屏幕：收起应用要让出屏幕，并保留同一页供再次打开。
function minimizeMainApp() {
  const p = pActive;
  if (isMobileView() && p && p.app) {
    hideAppSide();
    lastSideAppPage = p;
    lastSideAppPages = [p];
  }
  hideOverlay();
}
function setOverlayTitle(p) {
  const t = $("#overlay-title");
  if (p.el && p.el._headNode) { t.innerHTML = ""; t.appendChild(p.el._headNode); return; }   // 文件浏览等页面自带页头(面包屑)
  // uid 不上页头:它是程序坞认运行态用的内部标识(见 filesAppUid),给人看的是应用名。
  // p.appid 仍然留在页对象上,只是不再渲染。
  t.innerHTML = escapeHtml(p.title) +
    (p.badge ? ` <span class="dt-badge">${escapeHtml(p.badge)}</span>` : "");
}
// 页头上那套全局网页控件(#ov-url 与前进/后退/刷新)与 updateBrowserNav 已随标签条一并撤掉:
// 网页归浏览器应用,地址栏与导航钮是它自己的(见 app-browser.js 的 browserRenderNav),
// 全局这一份不再有页会命中。
function showOverlay(p) {
  let navigationEpoch = arguments.length > 1 ? arguments[1] : undefined;
  // 不带 epoch = 用户明确导航（会话、草稿、浏览器、文件等所有入口），统一推进代际；
  // 带 epoch = 后台恢复，只能展示它启动时那一代页面。这样不靠逐个入口记得 claim。
  if (navigationEpoch === undefined && typeof claimMainNavigation === "function") {
    navigationEpoch = claimMainNavigation();
  }
  if (typeof ownsMainNavigation === "function" && !ownsMainNavigation(navigationEpoch)) return false;
  if (typeof closeResourcePage === "function") closeResourcePage(false);
  if (appPageInSide(p)) hideAppSide(p);
  if (!overlayOpen) overlayReturnView = !$("#asset-view").hidden ? "asset" : "terminal";
  pActive = p;
  // 正文与侧栏共用当前可见会话的 id。这一步紧跟 pActive：后面的历史灌入、应用挂载
  // 即便异常，也不能留下“页头已经是 A、侧栏仍无选中或选中 B”的半切换画面。
  // 页面恢复期间只禁止把中间态写回引擎快照，不禁止最终可见页更新本地选中态。
  if (String(p.url || "").startsWith("chat:")) {
    const sid = p.url.slice(5);
    activeTermId = sid;
    try {
      localStorage.setItem("cd_term", sid);
      localStorage.setItem("cd_term:" + curEngine, sid);
    } catch {}
    document.querySelectorAll(".term-item").forEach((x) =>
      x.classList.toggle("active", termRowMatchesId(x, sid)));
  }
  // 记下最近看的那个会话页。收起浮层要回的是「刚才在看的那段会话」,而标签的排列顺序
  // 是打开顺序,不是浏览顺序——按顺序取最后一个,回去的往往是另一段会话。
  if (String(p.url || "").startsWith("chat:")) lastChatPage = p;
  overlayOpen = true;
  const ext = $("#ov-external");
  ext.hidden = p.app || p.content;
  ext.onclick = (p.app || p.content) ? null : () => openExternal(p.url);
  if (p.browser && typeof browserExternalUrl === "function") {   // 浏览器应用:外开钮指向它当前那张标签的地址
    const bu = browserExternalUrl();
    ext.hidden = !bu;
    ext.onclick = bu ? () => openExternal(bu) : null;
  }
  pages.forEach((x) => { if (!x.native && !appPageInSide(x)) x.el.style.display = x === p ? "" : "none"; });
  setAppPageLayout(p, "main");
  document.querySelectorAll("#overlay-body > .app-detail").forEach((x) => { x.style.display = "none"; });
  const overlay = $("#overlay");
  overlay.classList.toggle("app-page", !!p.app);   // app 页页头显示 收起/关闭(不进标签条,无标签 × 可用)
  // 终端页即使收起也会作为隐藏节点留在 #overlay-body，不能用 :has(.terminal-host) 判断
  // 当前页，否则回到会话后页头仍会继承终端色。由当前 p 显式维护外壳状态。
  overlay.classList.toggle("terminal-page", !!p.terminal);
  setOverlayTitle(p);
  $("#overlay").classList.toggle("no-head", p.url.startsWith("term:"));   // 终端页无页头(标签名即目录,内容自带提示符)
  // 自带半透明底的页面:把浮层的实底撤掉,让窗口 vibrancy 透到它背后去。iframe 里的
  // backdrop-filter 模糊不了框外的东西(各是各的合成层),所以毛玻璃这件事只能由这一层放行,
  // 页面那边负责用半透明色压在上面。
  $("#overlay").classList.toggle("glass", !!p.glass);
  // 自己画了左栏的页面:页头浮起来,内容顶到窗口上沿(见 style.css 的 #overlay.chromeless)
  $("#overlay").classList.toggle("chromeless", !!p.chromeless);
  $("#overlay").classList.toggle("hs-head", !!(p.el && p.el.classList.contains("hs-page")));   // HTML 会话页:页头换成终端会话同款观感 + 收起钮(样式见 style.css 的 hs-page 一节)
  syncOverlayCornerAction();
  // 一次性打开的那种页(同源链接点开的渲染页:/workbench/view 之类)也要有出口。
  // 它不是应用页,拿不到 app-page 那一对钮;又因为同源而不归浏览器应用管(browserHandlesUrl
  // 只接外部源),于是既不进标签条、页头上也只有一个标题——打开之后除了切到别的页没有别的
  // 走法(2026-08-19 用户实见,与当初文件查看页漏掉 app:true 是同一类)。
  // 会话页除外:它自带收起,页头是另一套。
  $("#overlay").classList.toggle("once-page",
    !p.app && !(p.el && p.el.classList.contains("hs-page")) && !p.url.startsWith("term:"));
  if (typeof hsSyncToTerm === "function") hsSyncToTerm(p);   // 「终端视图」钮只属于会话页,切页时重算归属
  // 「刚用过」记在这里,不只记在 _openDomainApp 上:文件、终端、浏览器三个内置应用各有自己的
  // 打开入口,工作流面板的文件夹钮、会话的目录、右键菜单都直接调它们,不经过 _openDomainApp;
  // 而切回一个已经开着的应用同样是「用过」。
  // 「哪个应用页此刻被显示出来」才是这件事的完整口径,而它只有这一处。
  if (p.app && p.appid && typeof _appRecentPush === "function") _appRecentPush(p.appid);
  // 顶栏那三枚最近打开的应用:头部是全局的,每次切页重画一遍即可——三个图标,代价可忽略,
  // 换来的是「打开过什么」随时是最新的(应用列表也可能在这期间才加载完)。
  if (typeof renderHeadRecent === "function") renderHeadRecent();
  if (p.browser && typeof browserRenderNav === "function") browserRenderNav();   // 浏览器应用的地址栏/导航钮随当前标签回填
  showView("overlay");
  const ov = $("#overlay");
  ov.hidden = false;
  ov.classList.add("open");
  if (NATIVE_BROWSER) {
    if (p.native) {
      closeInvisibleNativeEmbeds();
      requestAnimationFrame(() => nb({
        action: "open",
        id: p.url,
        url: p.url,
        rect: overlayBodyRect(),
        nativeMode: p.nativeMode || "",
        chatId: p.chatId || "",
        chatName: p.chatName || "",
        tagName: p.tagName || "",
      }));
    } else if (p.browser) {
      // 浏览器应用:原生面板归它自己管(哪张标签在前台、矩形取它的视口区),这里只触发一次同步。
      // 不能走下面那条 hide:那会把它当前这张网页一起按下去,页面显示出来却是一片空白。
      closeInvisibleNativeEmbeds();
      requestAnimationFrame(() => browserSyncNative(true));
    } else {
      closeInvisibleNativeEmbeds();
      nb({ action: "hide" });
    }
  }
  // 终端应用:页面这时已经可见,当前标签才挂 xterm(隐藏时量不出容器尺寸,开屏就会按错的几何走),
  // 已经挂着的按真实容器重新 fit。放在 ov.classList.add("open") 之后,不能提到前面。
  if (p.terminal && typeof termAppShown === "function") termAppShown();
  // 文件应用:页面显示出来才把 iframe 指到当前工作流(Monaco 那一页不轻,隐藏时不装载)
  if (p.files && typeof filesShown === "function") filesShown();
  // 会话页:历史到这一刻才灌(同理,而且更重——一段长会话上万个 DOM 节点)。
  // 顺带把冷下来的会话页正文清掉,DOM 规模按「同时看得见几页」封顶。见 hsShown。
  if (String(p.url || "").startsWith("chat:") && typeof hsShown === "function") {
    hsShown(typeof _hsViews !== "undefined" ? _hsViews.get(p.url.slice(5)) : null);
  }
  persistPages();
  if (!_restoringEngineViewState && typeof curEngine === "string") rememberEngineView(curEngine);
}
// GUI 偏好下,会话本身就是一个 overlay 页。打开别的应用会把它藏起来,而 overlayReturnView
// 记的是「第一个浮层页打开之前」的终端/资产视图——收起时回那边,在 GUI 偏好下终端里根本没有
// pane(会话进程按回合起,跑完就退),于是主区一片空白。收起前先看有没有该回的会话页。
function overlayHomePage() {
  if (typeof hsViewPref !== "function" || hsViewPref() !== "html") return null;
  if (typeof HS_PAGE !== "function") return null;
  const cur = (typeof activeTermId !== "undefined" && activeTermId)
    ? pages.find((x) => x.url === HS_PAGE(activeTermId)) : null;
  if (cur) return cur;
  // activeTermId 会被清掉(切工作区、主区清空都会清),原来一清就直接返回空,
  // 于是关掉应用页之后落到终端视图——GUI 偏好下那里没有 pane,看到的就是欢迎页。
  // 会话页本身还开着(它仍在 pages 里,只是不再画成标签),从这里找回来即可:
  // 优先回最近看过的那个,它还开着才算数。
  if (lastChatPage && pages.includes(lastChatPage)) return lastChatPage;
  const chats = pages.filter((x) => String(x.url || "").startsWith("chat:"));
  return chats.length ? chats[chats.length - 1] : null;
}

// toHome=false:调用方明确要离开浮层(如切工作流页),别再绕回会话页白跑一趟
function hideOverlay(toHome = true) {
  // 从右侧栏展开过来的应用有一份精确返回页。减号“最小化”与“收回侧栏”是两条入口，
  // 都必须先认这份快照；activeTermId/lastChatPage 是全局兜底，会被异步恢复成别的会话。
  const sideState = toHome && sideAppReturnState &&
    (!pActive || sideAppReturnState.app === pActive) ? sideAppReturnState : null;
  const exact = sideState && sideState.page && pages.includes(sideState.page) ? sideState.page : null;
  if (sideState) sideAppReturnState = null;
  const home = exact || (toHome ? overlayHomePage() : null);
  if (home && home !== pActive) { showOverlay(home); return; }
  const returnView = overlayReturnView || "terminal";
  overlayReturnView = null;
  overlayOpen = false;
  const ov = $("#overlay");
  ov.hidden = true;
  ov.classList.remove("open");
  ov.classList.remove("app-page", "terminal-page");
  closeAllNativeEmbeds();
  if (NATIVE_BROWSER) nb({ action: "hide" });
  showView(returnView);
  persistPages();
  if (!_restoringEngineViewState && typeof curEngine === "string") rememberEngineView(curEngine);
}
// 关掉的页留一份摘要,供 ⌘⇧T 恢复。只留最近十个:再往前的多半已经不想要了,
// 而每一条都带着标题与地址,留太多既没人翻也占着内存。
const _closedTabs = [];

function _tombstone(p) {
  if (p.url.startsWith("chat:")) return { kind: "chat", id: p.url.slice(5), title: p.title };
  if (p.url.startsWith("term:")) return null;   // 终端标签关掉就是结束进程,不该「恢复」
  if (p.content || p.app) return null;          // 应用页与内容页由各自的入口打开,没有通用的重建路径
  return { kind: "page", url: p.url, title: p.title, glyph: p.glyph, appid: p.appid,
           badge: p.badge, nativeMode: p.nativeMode, chatId: p.chatId,
           chatName: p.chatName, tagName: p.tagName };
}

function reopenClosedTab() {
  const d = _closedTabs.pop();
  if (!d) return false;
  if (d.kind === "chat") {
    openHtmlSession(d.id, d.title);
  } else {
    openPage(d.url, d.title, { glyph: d.glyph, app: false, appid: d.appid, badge: d.badge,
                               nativeMode: d.nativeMode, chatId: d.chatId,
                               chatName: d.chatName, tagName: d.tagName });
  }
  return true;
}

function closePage(p) {
  const wasSide = appPageInSide(p);
  if (wasSide) hideAppSide(p);
  if (lastSideAppPage === p) lastSideAppPage = null;
  lastSideAppPages = lastSideAppPages.filter((x) => x !== p);
  const tomb = _tombstone(p);
  if (tomb) { _closedTabs.push(tomb); if (_closedTabs.length > 10) _closedTabs.shift(); }
  // 浏览器应用被关掉:它那几张网页的原生面板一并销毁,不留孤儿盖在界面上
  if (p.browser && typeof browserPageClosed === "function") browserPageClosed();
  // 终端应用被关掉:每张标签背后是一个 PTY,一并结束,不留后台进程
  if (p.terminal && typeof terminalPageClosed === "function") terminalPageClosed();
  // 文件应用被关掉:没有后端资源要收,只把「在看哪个工作流」清掉,不留一条会自己回来的记录
  if (p.files && typeof filesPageClosed === "function") filesPageClosed();
  if (p.el && p.el._hsView && typeof hsLarkRefNativeClose === "function") {
    hsLarkRefNativeClose(p.el._hsView);
  }
  if (typeof hsDiscardDraft === "function") hsDiscardDraft(p);
  closeNativeEmbedsForWindow(p.el && p.el.contentWindow);
  if (p.native && NATIVE_BROWSER) nb({ action: "close", id: p.url });
  if (p.url.startsWith("term:")) { try { api(`/workbench/terminals/${encodeURIComponent(p.url.slice(5))}`, { method: "DELETE" }); } catch {} }
  if (p._cwdTimer) clearInterval(p._cwdTimer);
  p.el.remove();
  pages = pages.filter((x) => x !== p);
  if (pActive === p) { pActive = null; hideOverlay(); }
  persistPages();
}
let _restoringPages = false;
// 只剩会话那一类要落盘:网页、终端、文件三个应用各自持久化(cd_browser / cd_terminal / cd_files),
// 应用页本身在 pages 里也不写进这份记录——它由程序坞与各应用自己的键重建。
// 会话页仍要存:会话在后端有落盘存储,刷新后按 id 重开即可恢复全部历史。
// 切引擎会整页重载(见 app-events.js 的引擎菜单),而这份记录里的会话页不分引擎——
// 重载之后每一页都照开,于是切换引擎后,上一个引擎的那段会话又被恢复出来、还被
// data.active 顶到前台(2026-08-25 用户实见)。记下每页属于哪个引擎,恢复时只开当前这家的;
// 其余的原样留在记录里,切回去时还在。
let _pagesOtherEngines = [];   // 本次恢复时按引擎筛掉的那些,persistPages 要把它们写回去

// keep:这一次写回只收窄 list,open/active 原样留着。
//
// 恢复末尾要落一次盘(旧的 term/files 条目转交完就不该再留在这份记录里),可那一刻的 pActive
// 反映的是「恢复成功没有」,不是人的意图:active 指的那一页要是没建出来(被引擎筛掉、会话已删、
// 建的时候抛了),pActive 就是空,照写便把 active 抹成 null——而这份记录是唯一的来源,
// 抹一次就再也回不去,此后每次刷新都落在空态(2026-08-25 用户实见:刷新总是欢迎页)。
// 一次失败本该只影响这一次。
function persistPages(opts) {
  if (_restoringPages) return;
  const list = pages
    .filter((p) => String(p.url || "").startsWith("chat:"))
    .map((p) => {
      const id = p.url.slice(5);
      // 引擎取自这一页自己的视图(hsEngineSync 从 /config 拿的那份)。还没解析出来就不写,
      // 恢复那侧对没有 engine 的条目一律放行,不会因为写漏而丢页。
      const v = typeof _hsViews !== "undefined" ? _hsViews.get(id) : null;
      const row = { kind: "chat", id, title: p.title };
      if (v && v.engine) row.engine = v.engine;
      if (v && v.shepherdMode) row.shepherd_mode = true;
      if (v && v.shepherdTrial) row.shepherd_trial = true;
      return row;
    });
  const known = new Set(list.map((x) => x.id));
  for (const d of _pagesOtherEngines) if (!known.has(d.id)) list.push(d);
  const keep = opts && opts.keep;
  const data = { list,
                 open: keep ? !!keep.open : overlayOpen,
                 active: keep ? (keep.active || null) : (pActive ? pActive.url : null) };
  try { localStorage.setItem("cd_pages", JSON.stringify(data)); } catch {}
}
function restorePages() {
  const navigationEpoch = typeof currentMainNavigation === "function"
    ? currentMainNavigation() : null;
  let data; try { data = JSON.parse(localStorage.getItem("cd_pages") || "null"); } catch {}
  if (!data || !Array.isArray(data.list) || !data.list.length) return;
  _restoringPages = true;
  _pagesOtherEngines = [];
  const _mine = (d) => shepherdMode
    ? d.shepherd_mode === true
    : d.shepherd_mode !== true && (!d.engine || typeof curEngine !== "string" || d.engine === curEngine);
  for (const d of data.list) {
    if (d.kind === "chat" && !_mine(d)) { _pagesOtherEngines.push(d); continue; }
    if (d.kind === "chat") {
      openHtmlSession(d.id, d.title, false);
      // 把记下过的引擎接回视图。persistPages 只在视图解析出 engine 时才写这个字段,
      // 而恢复出来没打开过的冷页不跑 hsEngineSync、永远解析不出来——上一次记下的那个
      // 值于是在下一次落盘时被丢掉,记录越用越空,按引擎筛那道闸也就越来越形同虚设
      // (2026-08-25 实见:63 条里 45 条没有 engine)。已解析的以视图自己的为准,这里只补空缺。
      const v = typeof _hsViews !== "undefined" ? _hsViews.get(d.id) : null;
      if (v && d.engine && !v.engine) v.engine = d.engine;
      if (v && d.shepherd_mode) v.shepherdMode = true;
      if (v && d.shepherd_trial) v.shepherdTrial = true;
    }
    // 旧版全局标签条留下的网页条目(kind: "page"):openPage 会把它转交浏览器应用,迁移只发生一次。
    // 只认浏览器应用接得住的那些(跨源网页)。本机应用页不在此列——它们由程序坞重开,而其中
    // 还混着一条从来打不开的记录(终端曾登记 page: "shell",可 shell.html 并不存在),
    // 恢复出来只是一个写着「还没有此页」的空壳。
    else if (d.kind === "page" && typeof browserHandlesUrl === "function" && browserHandlesUrl(d.url)) {
      openPage(d.url, d.title, {
        glyph: d.glyph,
        app: false,
        appid: d.appid,
        badge: d.badge,
        nativeMode: d.nativeMode,
        chatId: d.chatId,
        chatName: d.chatName,
        tagName: d.tagName,
      }, false);
    }
  }
  _restoringPages = false;
  // 回到刷新前正看着的那一页。
  //
  // 上面那一圈只把页建出来(show=false),谁都不露面;而 open/active 这两个字段
  // persistPages 一直在写,从来没人读。于是刷新之后落在哪,由侧栏 nav(cd_nav)说了算——
  // 上次点过工坊就停在工坊,而人明明是在一段会话里刷的
  // (2026-08-24 用户在手机上实见)。
  //
  // 判据用 open 而不是「有没有会话页」:人要是正看着工坊、会话只是留在后台没关,
  // 那就该停在工坊。open 记的正是「浮层当时露着没有」。
  if (data.open && data.active) {
    const back = pages.find((x) => x.url === data.active);
    if (back) showOverlay(back, navigationEpoch);   // 用户已开始新导航时，迟到恢复只建页、不抢前台
  }
  // 就地落回收窄后的形状:旧条目转交完就不再留在这份记录里。
  // open/active 原样留着,不拿此刻的 pActive 去覆盖——理由见 persistPages 的 keep。
  persistPages({ keep: { open: data.open, active: data.active } });
  const terms = data.list.filter((d) => d.kind === "term");
  if (terms.length) restoreTermPages(terms);
  const files = data.list.filter((d) => d.kind === "files");
  if (files.length) restoreFilesPages(files);
}
// 浏览器应用的标签自存一份(见 app-browser.js):应用页不进 cd_pages,刷新后由它自己恢复。
// 放在 restorePages 之后调:旧版本存在 cd_pages 里的网页条目此刻已按 openPage 转进本应用,
// 两边按地址去重,迁移只发生一次。
function restorePagesAndBrowser() {
  restorePages();
  if (typeof restoreBrowserTabs === "function") restoreBrowserTabs();
  if (typeof restoreTerminalTabs === "function") restoreTerminalTabs();
  if (typeof restoreFilesPage === "function") restoreFilesPage();
}
// 旧版本把终端存在 cd_pages 里(kind: "term"),restorePages 收集出来交到这里。
// 终端已收进终端应用(app-terminal.js),这些条目转交给它,与它自己的 cd_terminal 按会话 id
// 去重后一起恢复,迁移只发生一次。
function restoreTermPages(items) {
  if (typeof termAppAdoptLegacy === "function") termAppAdoptLegacy(items);
}
// 同理:文件页原先每个工作流各占一张标签(kind: "files"),现在收进文件应用(app-files.js),
// 一个应用只有一个页面。这些条目转交给它,与它自己的 cd_files 合并后只留一个工作流。
function restoreFilesPages(items) {
  if (typeof filesAdoptLegacy === "function") filesAdoptLegacy(items);
}
window.__browserEvent = (e) => {
  // 浏览器应用的标签按自己的面板 id 收事件(id 形如 browser:N),先问它
  if (typeof browserApplyEvent === "function" && browserApplyEvent(e)) return;
  const p = pages.find((x) => x.url === e.id);
  if (!p) return;
  if (e.title) { p.title = e.title; if (pActive === p) setOverlayTitle(p); }
  if (e.url) p.curUrl = e.url;
  p.canBack = e.canBack; p.canFwd = e.canFwd;
};
// 键盘落在哪个应用上。侧栏与主区可以同时开着两页(会话在主区、终端在侧栏),所以
// 「当前应用」不等于 pActive:先按焦点实际所在的那一页算,焦点不在任何应用页里
// (例如落在会话输入框)时回主区那页,主区没开应用再回侧栏那页。
// ⌘W 从前只认 pActive,终端搬进侧栏后那一下就落空了,这个函数是那处的正解。
// strict=true 时只认「键盘焦点真的落在这一页里」,页面开着但焦点在别处一律返回 null。
// ⌘K / ⌘± / ⌘C / ⌘A 这几个作用于内容的键必须用严格档:终端摆在侧栏时它一直可见,
// 不加这道限制,焦点在设置搜索框里按 ⌘K 也会被终端的清屏抢走(app-settings-core.js
// 那个 ⌘K 是 document 冒泡档,先被我们的捕获监听吞掉,根本轮不到它)。
function focusedAppPage(strict) {
  const sides = appSidePages().filter(appPageVisible);
  const main = pActive && appPageVisible(pActive) ? pActive : null;
  const ae = document.activeElement;
  if (ae) {
    for (const side of sides) if (side.el && side.el.contains(ae)) return side;
    if (main && main.el && main.el.contains(ae)) return main;
  }
  return strict ? null : (main || sides[sides.length - 1] || null);
}

// 快捷键口径(2026-09-03 与用户确认):对齐 macOS 原生终端。
//   ⌘ 系归应用——⌘T 新建标签、⌘W 关标签、⌘1..9 跳标签、⌘⇧[ ] 循环、⌘K 清屏、
//   ⌘± 字号、⌘C 有选区才复制、⌘A 全选,一律 preventDefault,不留给宿主浏览器。
//   Ctrl 系一律不拦,原样透传给 shell——入口这一句就把带 ctrlKey 的挡在门外,
//   ⌃C / ⌃D / ⌃Z / ⌃R 因此永远是 shell 的,不会被应用截走。
// 这些键都按 focusedAppPage() 分派到当前那个应用,不再写死给浏览器。捕获阶段接,
// 因为 ⌘W 在宿主里默认是关窗口。
window.addEventListener("keydown", (ev) => {
  if (!ev.metaKey || ev.ctrlKey || ev.altKey) return;
  const k = (ev.key || "").toLowerCase();
  const p = focusedAppPage();
  // ⌘T / ⌘W / ⌘1..9 是窗口层面的动作,页面在前台就算数;⌘K / ⌘± / ⌘C / ⌘A 作用于内容,
  // 要求键盘焦点确实在这一页里。两档分开,见 focusedAppPage 的注释。
  const fp = focusedAppPage(true);
  const eat = () => { ev.preventDefault(); ev.stopPropagation(); };
  if (k === "w") {
    if (!p) return;   // 没有应用在前台:交回给宿主本身,别把关窗口这一下吞掉
    eat();
    // 关的是应用内部的那张标签;关掉最后一张才是关掉这个应用(见各自的 closeTab)
    if (p.browser && typeof browserCloseActiveTab === "function" && browserCloseActiveTab()) return;
    if (p.terminal && typeof termAppCloseActiveTab === "function" && termAppCloseActiveTab()) return;
    closePage(p);
    return;
  }
  if (ev.shiftKey && k === "t") {
    eat();
    reopenClosedTab();
    return;
  }
  if (!ev.shiftKey && k === "t") {
    // 从前无条件开浏览器标签,终端在前台时也一样(2026-09-03 用户实见)。现在归当前应用。
    eat();
    if (p && p.terminal && typeof openShellTerminal === "function") { openShellTerminal(); return; }
    if (typeof browserNewTab === "function") browserNewTab(true);
    return;
  }
  // ⌘⇧[ / ⌘⇧] :标签循环。macOS 上这对键在浏览器与终端里都是同一个意思。
  if (ev.shiftKey && (k === "{" || k === "}" || k === "[" || k === "]")) {
    const dir = (k === "{" || k === "[") ? -1 : 1;
    if (!p) return;
    if (p.terminal && typeof termAppCycleTab === "function" && termAppCycleTab(dir)) { eat(); return; }
    if (p.browser && typeof browserCycleTab === "function" && browserCycleTab(dir)) { eat(); return; }
    return;
  }
  if (!ev.shiftKey && k === "k") {
    if (fp && fp.terminal && typeof termAppClear === "function" && termAppClear()) eat();
    return;
  }
  if (k === "=" || k === "+" || k === "-" || k === "_" || (!ev.shiftKey && k === "0")) {
    // ⌘0 复位。放大那一颗在美式布局上要按住 shift 才出 "+",不加 shift 出 "="——
    // 同一颗物理键的两种写法,两个都收,否则 ⌘⇧+ 会漏掉(缩小同理,"-" 与 "_")。
    if (!fp || !fp.terminal || typeof termAppFontSize !== "function") return;
    const d = k === "0" ? 0 : ((k === "-" || k === "_") ? -1 : 1);
    if (termAppFontSize(d)) eat();
    return;
  }
  if (!ev.shiftKey && k === "c") {
    // 有选区才算复制;没选区就放手,让这一下什么也不发生——中断进程是 ⌃C 的活。
    if (fp && fp.terminal && typeof termAppCopySelection === "function" && termAppCopySelection()) eat();
    return;
  }
  if (!ev.shiftKey && k === "a") {
    if (fp && fp.terminal && typeof termAppSelectAll === "function" && termAppSelectAll()) eat();
    return;
  }
  if (!ev.shiftKey && k >= "1" && k <= "9") {
    const n = k === "9" ? -1 : Number(k) - 1;
    // 应用自己有一排标签时,这几个键先归它——与 macOS 终端、浏览器一致。
    // 它没有标签可跳(或压根没开应用)才落到程序坞:钉住的次序就是键的次序,
    // 开着的切回去,没开的开起来。
    if (p && p.terminal && typeof termAppActivateNth === "function" && termAppActivateNth(n)) { eat(); return; }
    if (p && p.browser && typeof browserActivateNth === "function" && browserActivateNth(n)) { eat(); return; }
    if (typeof dockActivateNth !== "function") return;
    if (!dockActivateNth(n)) return;
    eat();
  }
}, true);

window.addEventListener("message", (e) => {
  if (e.origin !== location.origin) return;
  const d = e.data || {};
  // 应用页改了自己的登记(目前只有改名):重拉一次注册表,把程序坞、应用格与这一页的标题
  // 都换成新名。页面自己已经写过盘了,这里只管让宿主的那几份缓存跟上。
  if (d.type === "desktron-app-updated") {
    (async () => {
      if (typeof mergeLocalApps === "function") await mergeLocalApps();
      const p = pages.find((x) => x.appid && x.appid === d.uid);
      if (p && d.label) {
        p.title = d.label;
        if (pActive === p) $("#overlay-title").textContent = d.label;
        if (appPageInSide(p)) _sideTitle(p);
      }
      const dock = $("#app-dock"); if (dock && typeof _renderDock === "function") _renderDock(dock);
      if (typeof renderHeadRecent === "function") renderHeadRecent();
    })();
    return;
  }
  if (d.type !== "desktron-native-embed") return;
  if (d.action === "open") openNativeEmbedFromFrame(e.source, d);
  else if (d.action === "rect") rectNativeEmbedFromFrame(e.source, d);
  else if (d.action === "close") closeNativeEmbed(d.id);
});
window.addEventListener("resize", () => {
  closeInvisibleNativeEmbeds();
  if (NATIVE_BROWSER && overlayOpen && pActive && pActive.native) {
    nb({ action: "rect", id: pActive.url, rect: overlayBodyRect() });
  }
  for (const side of appSidePages()) {
    if (side.browser && typeof browserSyncNative === "function") browserSyncNative();
    if (side.terminal && typeof termAppRefit === "function") termAppRefit();
  }
  syncOverlayCornerAction();
});
setInterval(closeInvisibleNativeEmbeds, 1000);
{ const c = $("#ov-close"); if (c) c.onclick = () => {
  if ($("#overlay")?.classList.contains("hs-head")) { toggleAppSide(); return; }
  if (pActive) { closePage(pActive); return; }   // 含 app 页:关闭=销毁,Dock 运行点熄灭
  document.querySelectorAll("#overlay-body > .app-detail").forEach((x) => x.remove());   // agentic 详情无页对象
  hideOverlay();
}; }
// 收起:应用保持运行,回到之前那个视图。只对应用页放开(见 style.css 的 #overlay.app-page #ov-min)。
// 会话页没有「上一个视图」可回，右端按钮在上面的 hs-head 分支负责应用侧栏。
{ const m = $("#ov-min"); if (m) m.onclick = () => minimizeMainApp(); }
{ const x = $("#ov-info"); if (x) x.onclick = () => hsSituationToggleActive(); }
{ const x = $("#ov-toside"); if (x) x.onclick = () => collapseMainAppToSide(); }
// 侧栏页只收起、不销毁。关闭应用仍在主区页头完成，避免把“暂时让出空间”画成会丢状态的 ×。
{ const x = $("#app-side-collapse"); if (x) x.onclick = () => hideAppSide(sideAppPage); }
{ const x = $("#app-side-expand"); if (x) x.onclick = () => expandSideApp(sideAppPage); }
{ const x = $("#app-side-collapse-2"); if (x) x.onclick = () => hideAppSide(sideAppPage2); }
{ const x = $("#app-side-expand-2"); if (x) x.onclick = () => expandSideApp(sideAppPage2); }
// 应用侧栏左缘拖宽。宽度只保存在当前端的 localStorage：桌面与其他浏览器往往有不同
// 的窗口尺寸，不该互相覆盖。手机端由 CSS 固定全屏并隐藏拖拽柄。
(function appSideResize() {
  const side = $("#app-side"), bar = $("#app-side-resizer");
  if (!side || !bar) return;
  const KEY = "cd_app_side_w", MIN = 320, MAX = 960, MAX_RATIO = .72;
  const saved = parseInt(localStorage.getItem(KEY) || "", 10);
  if (saved >= MIN && saved <= MAX) side.style.setProperty("--app-side-w", saved + "px");

  const bounds = () => {
    const total = $("#main")?.getBoundingClientRect().width || window.innerWidth;
    const max = Math.max(240, Math.min(MAX, Math.floor(total * MAX_RATIO)));
    return { min: Math.min(MIN, max), max };
  };
  const setW = (w, fixedBounds) => {
    const b = fixedBounds || bounds();
    const next = Math.round(Math.min(b.max, Math.max(b.min, w)));
    side.style.setProperty("--app-side-w", next + "px");
    bar.setAttribute("aria-valuemin", String(b.min));
    bar.setAttribute("aria-valuemax", String(b.max));
    bar.setAttribute("aria-valuenow", String(next));
    return next;
  };

  // iframe/原生网页面板/终端都需要知道容器变宽。CSS 每次移动立即跟手，昂贵的几何同步
  // 限到每 100ms 一次，松手再补一帧精确值，与左栏拖宽保持同一节奏。
  const SYNC_GAP = 100;
  let syncAt = 0, syncTimer = 0;
  const syncNow = () => {
    syncAt = Date.now();
    try { window.dispatchEvent(new Event("resize")); } catch {}
  };
  const syncGeom = (now = false) => {
    if (syncTimer) { clearTimeout(syncTimer); syncTimer = 0; }
    if (now) { syncNow(); return; }
    const wait = SYNC_GAP - (Date.now() - syncAt);
    if (wait <= 0) syncNow();
    else syncTimer = setTimeout(() => { syncTimer = 0; syncNow(); }, wait);
  };

  let dragging = false, pointerId = null, x0 = 0, w0 = 0, lastW = 0, dragBounds = null;
  const finish = (ev) => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove("app-side-resizing");
    bar.onpointermove = bar.onpointerup = bar.onpointercancel = null;
    try { if (pointerId !== null && bar.hasPointerCapture(pointerId)) bar.releasePointerCapture(pointerId); } catch {}
    pointerId = null;
    lastW = setW(lastW || side.getBoundingClientRect().width, dragBounds);
    dragBounds = null;
    try { localStorage.setItem(KEY, String(lastW)); } catch {}
    syncGeom(true);
  };
  bar.onpointerdown = (ev) => {
    if (ev.button !== 0) return;
    ev.preventDefault();
    dragging = true; pointerId = ev.pointerId; x0 = ev.clientX;
    w0 = side.getBoundingClientRect().width; lastW = w0; dragBounds = bounds();
    document.body.classList.add("app-side-resizing");
    try { bar.setPointerCapture(ev.pointerId); } catch {}
    bar.onpointermove = (e) => {
      // 手柄在左缘：向左拖增宽，向右拖减宽。
      lastW = setW(w0 + x0 - e.clientX, dragBounds);
      syncGeom();
    };
    bar.onpointerup = finish;
    bar.onpointercancel = finish;
  };
  bar.ondblclick = () => {
    side.style.removeProperty("--app-side-w");
    try { localStorage.removeItem(KEY); } catch {}
    bar.removeAttribute("aria-valuenow");
    syncGeom(true);
  };
})();
// 网页标签的兜底图标与站点图标(favicon)。全局标签条撤掉之后,用的是浏览器应用里的标签条
// (app-browser.js 的 browserRenderTabs 直接取这两处),形制与原来那条一致。
// 兜底不用线框地球:它是上一代的图示,细线轮廓分不出轻重。改成一个窗口的形状,说的是「这是一页」。
const _TAB_WINDOW = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" '
  + 'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'
  + '<rect x="3" y="4.5" width="18" height="15" rx="3"/><path d="M3 9.5h18"/></svg>';
// 站点自己的图标。兜底那一枚对所有网页长得一样,一排标签压窄之后只剩图标时,
// 谁是谁就分不出来了——favicon 正是为这一步存在的。
// 取法是按来源拼 /favicon.ico:原生嵌入那侧不回传图标(它只给标题与地址),同源 iframe
// 又读不到对方文档,这是两条路都能用的唯一办法。取不到就回落兜底,并记下这个来源,
// 免得每次重画标签条都再请求一次。
// 来源 → "bad"(两条路都拿不到) | 具体的图标地址(拿到过,之后直接用,不再走网络)。
// 只记 "ok" 的话每次重绘都会重新造 <img> 再请求一次;标签条是高频重绘的地方,那等于
// 每个事件都按标签数发一轮请求。
const _favState = new Map();

function _tabOrigin(p) {
  if (p.content || p.app) return "";
  try {
    return new URL(p.curUrl || p.url, location.href).origin;
  } catch { return ""; }
}

function _tabFav(p, slot) {
  const origin = _tabOrigin(p);
  if (!origin || origin === location.origin) return;
  const known = _favState.get(origin);
  if (known === "bad") return;
  if (known) {   // 已经知道地址:直接挂,不再探测。浏览器自己的缓存兜住重复请求
    const img = document.createElement("img");
    img.className = "ps-fav"; img.alt = ""; img.src = known;
    img.onload = () => { slot.textContent = ""; slot.appendChild(img); };
    return;
  }
  const put = (src, onFail) => {
    const img = document.createElement("img");
    img.className = "ps-fav";
    img.alt = "";
    img.onload = () => { _favState.set(origin, src); slot.textContent = ""; slot.appendChild(img); };
    img.onerror = onFail;
    img.src = src;
  };
  // 先试根路径那一份:多数站点有,一次请求就成。
  // 不成再问后端——它抓一次首页,把 HTML 里声明的图标地址解析出来(有的站点根路径返回 204,
  // 真正的图标在别的域的 CDN 上)。两条都不成才记下这个来源,之后不再试。
  put(origin + "/favicon.ico", () => {
    api(`/workbench/favicon?url=${encodeURIComponent(origin)}`, { timeout: 8000 })
      .then((r) => {
        const href = (r && r.href) || "";
        if (!href) { _favState.set(origin, "bad"); return; }
        put(href, () => _favState.set(origin, "bad"));
      })
      .catch(() => _favState.set(origin, "bad"));
  });
}

// openShellTerminal 与 cwd 轮询已移进终端应用(app-terminal.js):shell 不再各占一张全局标签,
// 而是那个应用里的一张内部标签,标签名同样跟着当前目录走。外部调用方的签名不变。
function pageUrl(app, domainId, projectDomain) {
  let u = `/workbench/page?id=${encodeURIComponent(app.page || app.id)}`;
  if (domainId) u += `&domain=${encodeURIComponent(domainId)}`;
  if (projectDomain && projectDomain !== domainId) u += `&project_domain=${encodeURIComponent(projectDomain)}`;
  return u;
}
function openExternal(url) {
  if (!url) return;
  api("/workbench/open-external", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ url }),
  }).catch(() => window.open(url, "_blank"));
}

// ---- 内部标签条的拖动改位 ----------------------------------------------------
// 浏览器 / 终端 / 文件三个应用的标签条共用 .ps-item 那一套形制(见 style.css),拖动这件事
// 同样只写一份。不用 HTML5 的 draggable:它在 WKWebView 里给不出稳定的落点反馈,而这里要的
// 只是「按住往边上挪」这一种手势。
//
// 三处细节:
//   · 挪够 4px 才算拖。否则一次普通点击会被判成零距离的拖,标签切不过去。
//   · 拖动中直接搬 DOM,松手时按 DOM 的次序把 id 交回去,由各应用改自己的模型数组再重画——
//     模型归各应用所有,这里不碰。
//   · 落点只在 .ps-item 之间找,末尾那枚「+」不参与,免得标签被拖到它后面。
function psTabsBindDrag(strip, onOrder) {
  if (!strip || strip._psDrag) return;
  strip._psDrag = true;
  let node = null, x0 = 0, moved = false, justDragged = false;

  const items = () => [...strip.querySelectorAll(".ps-item")];
  const finish = (commit) => {
    if (node) node.classList.remove("ps-drag");
    if (commit && moved) {
      justDragged = true;   // 松手之后还会来一发 click,那一发要吃掉(moved 这时已经清了)
      onOrder(items().map((el) => el.dataset.id).filter(Boolean));
    }
    node = null; moved = false;
  };

  strip.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    const it = ev.target.closest(".ps-item");
    if (!it || !strip.contains(it) || ev.target.closest(".ps-x")) return;   // 关闭钮不参与
    node = it; x0 = ev.clientX; moved = false;
  });

  strip.addEventListener("pointermove", (ev) => {
    if (!node) return;
    if (!moved) {
      if (Math.abs(ev.clientX - x0) < 4) return;
      moved = true;
      node.classList.add("ps-drag");
      // 按下即切标签,拖动因此发生在「已经是当前标签」的那一枚上;指针捕获保证挪出条外也跟手
      try { strip.setPointerCapture(ev.pointerId); } catch {}
    }
    for (const other of items()) {
      if (other === node) continue;
      const r = other.getBoundingClientRect();
      const mid = r.left + r.width / 2;
      // 只在越过邻座中线时换位:按重叠面积算会在两枚之间来回抖
      if (ev.clientX > mid && other.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_PRECEDING) {
        other.after(node); break;
      }
      if (ev.clientX < mid && other.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING) {
        other.before(node); break;
      }
    }
  });

  strip.addEventListener("pointerup", (ev) => {
    try { strip.releasePointerCapture(ev.pointerId); } catch {}
    finish(true);
  });
  strip.addEventListener("pointercancel", () => finish(false));
  // 拖完那一下的 click 不该再切标签:指针停在哪一枚上是拖的结果,不是人点的
  strip.addEventListener("click", (ev) => {
    if (!justDragged) return;
    justDragged = false;
    ev.stopPropagation(); ev.preventDefault();
  }, true);
}
