// 由 app-rail.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 2/5 片
// ---- 顶部窗口导航(仿 codex):侧栏开合 + 主区历史后退/前进 ----
const _navHist = { stack: [], idx: -1, replaying: false };
const _ENGINE_VIEWS_KEY = "cd_engine_views";
const _ENGINE_VIEW_REV = 2;
// 冷启动会先按全局 cd_nav 摆一个基础页面、再建 cd_pages 里的旧全局页，最后才按当前
// 引擎快照翻到真正那一页。恢复保护必须从脚本加载起就开启：若等到 restorePages 才开，
// 前面的 selectNav/navPush 已经会把“工坊”等全局页写进目标引擎快照，原位置随即丢失。
let _restoringEngineViewState = true;

function _engineViewsRead() {
  try {
    const d = JSON.parse(localStorage.getItem(_ENGINE_VIEWS_KEY) || "{}");
    if (!d || typeof d !== "object" || Array.isArray(d)) return {};
    // The former trial desk is now part of Shepherd.  Preserve its last open
    // page when this browser first loads the formal unified entry.
    if (!d.shepherd && d.shepherd_trial) d.shepherd = d.shepherd_trial;
    delete d.shepherd_trial;
    return d;
  } catch { return {}; }
}

function _engineViewKey(engine) {
  return typeof shepherdMode !== "undefined" && shepherdMode ? "shepherd" : String(engine || "");
}

function _sessionViewVisible(sid, fallback) {
  const row = typeof _termsList !== "undefined" && Array.isArray(_termsList)
    ? _termsList.find((x) => typeof termMatchesId === "function"
      ? termMatchesId(x, sid)
      : [x.id, x.resume_id, x.session_id].filter(Boolean).map(String).includes(String(sid))) : null;
  return typeof sessionVisibleInCurrentMode !== "function"
    || sessionVisibleInCurrentMode(row || fallback);
}

// 引擎不是一个一次性筛选条件,而是四套并列的工作上下文。切走时记下这一套上下文
// 当时露在主区的东西,切回来再原样摆回去。会话列表本来就按 engine 留着,缺的是
// 「每个引擎最后看哪一页」这一层；只用 cd_pages.active(全局一个)会被后来切到的引擎覆盖。
function rememberEngineView(engine) {
  if (_restoringEngineViewState) return;
  const id = _engineViewKey(engine);
  if (!id) return;
  let view = null;
  const overlay = typeof overlayOpen !== "undefined" && overlayOpen
    && typeof pActive !== "undefined" && pActive;
  if (overlay) {
    const url = String(pActive.url || "");
    if (url.startsWith("chat:")) {
      view = { kind: "chat", id: url.slice(5), title: pActive.title || "对话",
        engine, shepherd_mode: id === "shepherd", shepherd_trial: false };
    } else if (pActive.app && pActive.appid) {
      view = { kind: "app", appid: pActive.appid, url, title: pActive.title || "" };
    } else if (url && !pActive.content) {
      view = { kind: "overlay", url, title: pActive.title || "", glyph: pActive.glyph || "" };
    }
  } else {
    // nav history 是主区页面的完整可重放描述(工作流/工坊/设置/任务/应用列表等)。
    // 只有 asset-view 真在前台时才认它；会话被收起后主区是欢迎页,不能拿栈顶旧会话冒充。
    const asset = document.getElementById("asset-view");
    if (asset && !asset.hidden) {
      let entry = _navHist.stack[_navHist.idx] || null;
      // 这页可能是刷新时按 cd_nav 摆回来的,还没经过 navPush；固定资源页可由 nav 兜底。
      if (!entry && typeof nav !== "undefined" && nav) {
        if (nav.type === "page") entry = { kind: "page", id: nav.id };
      }
      if (entry) view = { kind: "nav", entry: JSON.parse(JSON.stringify(entry)) };
    }
  }
  if (!view) view = { kind: "welcome" };
  view.rev = _ENGINE_VIEW_REV;
  const all = _engineViewsRead();
  all[id] = view;
  try { localStorage.setItem(_ENGINE_VIEWS_KEY, JSON.stringify(all)); } catch {}
}

function _navReplay(e) {
  if (!e || !e.kind) return false;
  $("#fx-tasks")?.classList.toggle("active", e.kind === "arrangements");
  _navHist.replaying = true;
  try {
    // 代理资源页已经下线；工作流详情页也不再存在。旧历史统一安全回到欢迎页。
    if (e.kind === "home") return _restoreEngineWelcome();
    else if (e.kind === "bots" || e.kind === "agents" || (e.kind === "page" && (e.id === "assets" || e.id === "kb"))) return _restoreEngineWelcome();
    else if (e.kind === "page") selectNav("page", e.id);
    else if (e.kind === "arrangements") openArrangementsPage();
    else if (e.kind === "tasks") openTasksPage();
    else if (e.kind === "settings") openSettings();
    else if (e.kind === "store") openStore(e.k, e.target);
    else if (e.kind === "asset") openAsset(e.title, e.path);
    else if (e.kind === "sess") {
      const t = termById(e.id);
      if (!_sessionViewVisible(e.id, e)) return false;
      // 回放的这条可能已经不在了(会话被删、或清单还没拉回来)。找不到就不挂——
      // attachTerminal 会照着一个不存在的 id 去连,连上即断,屏幕上留一句「[终端已退出]」。
      if (!t && (_termsList || []).length) return false;
      attachTerminal(e.id, t && t.engine, { domain: e.domain || curDomainId() });
      if (t) setTermHead(t);
      document.querySelectorAll(".term-item").forEach((x) =>
        x.classList.toggle("active", termRowMatchesId(x, e.id)));
    } else return false;
  } finally { _navHist.replaying = false; }
  return true;
}

function _restoreEngineWelcome() {
  if (typeof overlayOpen !== "undefined" && overlayOpen && typeof hideOverlay === "function") hideOverlay(false);
  showView("terminal");
  if (typeof _showWatermark === "function") _showWatermark();
  document.body.classList.remove("term-on");
  return true;
}

async function restoreEngineView(engine) {
  const navigationEpoch = typeof currentMainNavigation === "function"
    ? currentMainNavigation() : null;
  const engineId = _engineViewKey(engine);
  let view = _engineViewsRead()[engineId];
  if (!view || !view.kind) return false;   // 旧数据没有这层时沿用原来的恢复/欢迎页兜底
  // 第一版只在“切走引擎”时记一次。用户留在同一引擎内从会话 A 切到 B 后，
  // 快照仍是 A；而 attachTerminal 持续写的分引擎会话键已经是 B。只对旧版快照做一次迁移，
  // 之后所有可见页变化都会实时写 rev=2，不再让这个兜底覆盖正常快照。
  if (view.kind === "chat" && view.rev !== _ENGINE_VIEW_REV) {
    let sid = "";
    try { sid = localStorage.getItem("cd_term:" + engineId) || ""; } catch {}
    const listed = Array.isArray(_termsList) ? _termsList.find((x) => x.id === sid && (!x.engine || x.engine === engineId)) : null;
    if (sid && (!Array.isArray(_termsList) || !_termsList.length || listed)) {
      view = { kind: "chat", id: sid, title: (listed && listed.title) || view.title || "对话", rev: _ENGINE_VIEW_REV };
    }
  }
  if (typeof ownsMainNavigation === "function" && !ownsMainNavigation(navigationEpoch)) return false;
  if (view.kind === "welcome") return _restoreEngineWelcome();
  if (view.kind === "chat" && view.id) {
    if (!_sessionViewVisible(view.id, view)) return _restoreEngineWelcome();
    let p = pages.find((x) => x.url === "chat:" + view.id);
    if (!p && typeof openHtmlSession === "function") {
      await openHtmlSession(view.id, view.title || "对话", false);
      p = pages.find((x) => x.url === "chat:" + view.id);
      if (typeof ownsMainNavigation === "function" && !ownsMainNavigation(navigationEpoch)) return false;
    }
    if (!p) return _restoreEngineWelcome();
    return showOverlay(p, navigationEpoch) !== false;
  }
  if (view.kind === "nav" && view.entry) {
    _navHist.stack = [view.entry];
    _navHist.idx = 0;
    const ok = _navReplay(view.entry);
    updateNavBtns();
    return ok || _restoreEngineWelcome();
  }
  if (view.kind === "app" && (view.appid || view.url)) {
    // 浏览器/文件等内置应用已由各自 restore* 建成隐藏页,优先只把那一页翻出来。
    // 终端恢复要先问后端哪些 PTY 仍活着,会晚一点出现；短等它,不要误起一个新 shell。
    let p = pages.find((x) => (view.appid && x.appid === view.appid) || (view.url && x.url === view.url));
    if (!p && view.url === "terminal") {
      for (let i = 0; i < 40 && !p; i++) {
        await new Promise((r) => setTimeout(r, 100));
        p = pages.find((x) => x.url === "terminal");
      }
    }
    if (typeof ownsMainNavigation === "function" && !ownsMainNavigation(navigationEpoch)) return false;
    if (p) return showOverlay(p, navigationEpoch) !== false;
    if (view.url === "terminal") return _restoreEngineWelcome();
    const found = typeof _allApps === "function"
      ? _allApps().find((x) => x.key === view.appid) : null;
    if (!found || typeof _openDomainApp !== "function") return _restoreEngineWelcome();
    _openDomainApp(found.a, found.domainId);
    return true;
  }
  if (view.kind === "overlay" && view.url && typeof openPage === "function") {
    if (typeof ownsMainNavigation === "function" && !ownsMainNavigation(navigationEpoch)) return false;
    openPage(view.url, view.title || view.url, { glyph: view.glyph || "" });
    return true;
  }
  return _restoreEngineWelcome();
}
// 顶层入口互斥：点了别的入口先收起当前入口，不叠加管理页。
function _toggleGroup(e) {
  if (!e) return null;
  if (e.kind === "store" && e.k === "app") return "apps";
  if (e.kind === "settings") return "settings";
  if (e.kind === "arrangements") return "arrangements";
  if (e.kind === "tasks") return "tasks";
  return null;
}
function navPush(entry) {
  $("#fx-tasks")?.classList.toggle("active", !!entry && entry.kind === "arrangements");
  if (_navHist.replaying) return;
  const cur = _navHist.stack[_navHist.idx];
  if (cur && JSON.stringify(cur) === JSON.stringify(entry)) {
    // 栈不用重复压，但主区可能刚从应用/会话切回这张同名功能页；最后页面仍要刷新。
    if (typeof curEngine === "string") rememberEngineView(curEngine);
    return;
  }
  const g = _toggleGroup(entry);
  if (g) {
    while (_navHist.idx >= 0) {
      const tg = _toggleGroup(_navHist.stack[_navHist.idx]);
      if (tg && tg !== g) _navHist.idx--;   // 跨组:栈顶的前一个顶层页当作已收起,跳过它
      else break;                            // 同组(如工坊标签间)或非顶层页:正常叠栈
    }
  }
  _navHist.stack = _navHist.stack.slice(0, _navHist.idx + 1);
  _navHist.stack.push(entry);
  _navHist.idx = _navHist.stack.length - 1;
  updateNavBtns();
  // 最后页面是一个持续状态，不是“切引擎”这个动作的副产物。每次导航落稳就更新，
  // 这样直接刷新、Desk 重启、手机后台被系统回收后都能回到刚才那一页。
  if (typeof curEngine === "string") rememberEngineView(curEngine);
}
function navGo(d) {
  const i = _navHist.idx + d;
  if (i < 0 || i >= _navHist.stack.length) return false;
  _navHist.idx = i;
  const e = _navHist.stack[i];
  const ok = _navReplay(e);
  updateNavBtns();
  if (ok && typeof curEngine === "string") rememberEngineView(curEngine);
  return ok;
}

function updateNavBtns() {
  const b = $("#tn-back"), f = $("#tn-fwd");
  if (b) b.disabled = _navHist.idx <= 0;
  if (f) f.disabled = _navHist.idx >= _navHist.stack.length - 1;
}
(function initTopNav() {
  const side = $("#tn-side");
  if (!side) return;
  // 侧栏开合态持久化
  if (localStorage.getItem("cd_side_collapsed") === "1") document.body.classList.add("side-collapsed");
  // 手机宽度下侧栏是浮在内容之上的抽屉,同一个按钮改开抽屉;桌面仍是收起/展开。
  const narrow = () => (typeof isMobileView === "function" ? isMobileView() : window.innerWidth <= 820);
  // 手机抽屉原来只靠 body.mo-drawer 表示，整页刷新时 DOM 重建，这个状态便凭空丢了。
  // 只跨当前标签页的刷新保留，不写 localStorage：新开窗口仍从主区开始，不继承旧窗口
  // 偶然停留的抽屉。pagehide 比 beforeunload 更适合 iOS，也覆盖引擎切换触发的 reload。
  const drawerReloadKey = "cd_mobile_drawer_open";
  try {
    if (narrow() && sessionStorage.getItem(drawerReloadKey) === "1") {
      document.body.classList.add("mo-drawer");
    }
  } catch {}
  window.addEventListener("pagehide", () => {
    try {
      if (narrow() && document.body.classList.contains("mo-drawer")) {
        sessionStorage.setItem(drawerReloadKey, "1");
      } else sessionStorage.removeItem(drawerReloadKey);
    } catch {}
  });
  // 收抽屉统一走这里,并广播一声:从抽屉里唤出来的浮层(工作流行的「…」菜单等)挂在 body 上,
  // 抽屉一收它们就没了依托,悬在半空还关不掉。让它们听这个事件自己退场。
  const closeDrawer = () => {
    if (!document.body.classList.contains("mo-drawer")) return;
    document.body.classList.remove("mo-drawer");
    try { window.dispatchEvent(new Event("cd-drawer-closed")); } catch {}
  };
  side.onclick = () => {
    if (narrow()) {
      if (document.body.classList.contains("mo-drawer")) closeDrawer();
      else document.body.classList.add("mo-drawer");
      return;
    }
    const on = document.body.classList.toggle("side-collapsed");
    try { localStorage.setItem("cd_side_collapsed", on ? "1" : "0"); } catch {}
    window.dispatchEvent(new Event("resize"));   // 主区变宽:让 xterm/原生浏览面板重排
  };
  // 抽屉收起的两条路:点遮罩,或在抽屉里选中了什么(选完就该看主区)。
  document.addEventListener("click", (ev) => {
    if (!document.body.classList.contains("mo-drawer")) return;
    const inPanel = ev.target.closest && ev.target.closest("#panel");
    if (!inPanel) {
      // 右键菜单统一挂在 body，不是 #panel 的后代。会话“重命名”点下去后会把输入框
      // 放回侧栏原行；若这里先收抽屉，输入框虽已创建却看不见，再开抽屉又因失焦提交。
      // 只豁免显式标记的就地动作，其余点遮罩、点主区仍按原逻辑收起。
      if (ev.target.closest && ev.target.closest(".rp-drawer-keep")) return;
      if (!ev.target.closest || !ev.target.closest("#tn-side")) closeDrawer();
      return;
    }
    // 选中了什么就收:会话行和去主区开页的固定入口。工作流 summary 只展开/折叠列表，保留抽屉。
    const nav = ev.target.closest(".term-item, .side-item, #fx-tasks, #fx-apps, #settings-btn");
    if (!nav) return;
    // 但行里还嵌着就地操作的控件:工作流行的「…」、会话行的 ×。它们弹的菜单锚在抽屉里,
    // 抽屉一收菜单就悬空,孤零零浮在会话上(2026-08-24 用户实见)。
    // 这些控件自己的 stopPropagation 拦不住这里:本监听挂在 document 的捕获阶段,
    // 比它们的 onclick 先跑完。
    //
    // 判据是「这个控件是不是行本身」,而不是「是不是 button」——上面那四个入口
    // (#fx-tasks/#fx-apps/#settings-btn)本身就是 button,一刀切会把
    // 「点任务/应用/工坊/设置该收起抽屉」也一并挡掉。嵌在行里头的那些才是就地操作。
    //
    // 这样也不必再逐个记名字:逐个排除的清单每次都是漏一个才发现。
    const act = ev.target.closest("button, .ti-x");
    if (act && act !== nav) return;
    closeDrawer();
    setTimeout(() => window.dispatchEvent(new Event("resize")), 240);   // 抽屉动画收完再让 xterm 重排
  }, true);
  // 边缘手势:从左缘往右拖 = 拉出抽屉,抽屉开着往左拖 = 推回去。
  //
  // 手机上左缘右滑本来是浏览器的「返回」,而这一处正是人想拉侧栏的地方(2026-08-17 实见)。
  // 系统手势能不能压住,取决于浏览器认不认 touchstart 上的 preventDefault:iOS 上认,
  // 所以要在 touchstart 就 preventDefault(且必须 passive:false,否则调用被忽略),
  // 不能等到 touchmove——那时返回动画已经起来了。压不住的浏览器至多是「拖出来的同时也返回」,
  // 不比现在更糟;要彻底避开,把页面加到主屏用独立窗口打开,那里没有浏览器手势。
  //
  // 只吃左缘 22px 那一条,别处的横向滑动照常交给页面(正文里的代码块要能横向滚)。
  const EDGE = 22, OPEN_AT = 52;   // 起手区宽度 / 判定为「要开」的位移
  let gx = 0, gy = 0, gOn = false, gWasOpen = false, gEdge = false, gInp = false;
  // 输入区:那里右滑同样该是「拉侧栏」,不是后退。有的浏览器的返回手势不止认屏幕边缘,
  // 整页右滑都算,而人正在打字时手就停在输入框上——一滑就退出去,写的字还在,页面却没了。
  // 这一路不在 touchstart 拦(拦了点不进输入框),等 touchmove 判明是横向再拦:
  // 那类全页手势要滑到一定距离才触发,来得及。代价是输入框内横向拖动光标不再可用,
  // 选字改用长按放大镜——比「一滑就退出去」划算。
  const inComposer = (t) => !!(t && t.closest && t.closest(".hs-composer, .hs-box"));
  // 起手点落在能横向滚的东西上就不抢:那里右滑的意思是「把它滚回去」。档位行(.hs-row)
  // 选项一多就得横着滚,少了这条会变成左滑滚得动、右滑滚不回来——右滑全被这一路吃掉,
  // 拉出来的是侧栏抽屉。往上找到 body 为止,代码块一类同理。
  const inHScroll = (t) => {
    for (let e = t; e && e !== document.body; e = e.parentElement) {
      if (e.scrollWidth > e.clientWidth + 2) return true;
    }
    return false;
  };
  document.addEventListener("touchstart", (ev) => {
    if (!narrow() || ev.touches.length !== 1) return;
    const t = ev.touches[0];
    gWasOpen = document.body.classList.contains("mo-drawer");
    // 起手点:抽屉关着时看左缘那一条;开着时抽屉本身就是可推的面
    const panel = $("#panel");
    const w = gWasOpen && panel ? panel.getBoundingClientRect().right : EDGE;
    gInp = !gWasOpen && inComposer(ev.target) && !inHScroll(ev.target);
    if (t.clientX > w && !gInp) return;
    gOn = true; gx = t.clientX; gy = t.clientY;
    // 压住浏览器的边缘返回。判据是「起手点落在左缘热区」,与抽屉开没开无关——
    // 抽屉开着时那一条被抽屉盖住了,人以为在划抽屉,浏览器仍按边缘手势算,于是又退了回去。
    // 代价是这 22px 宽的一条里纵向滚动失效;抽屉左侧本来就是内边距,滚动从它右边起手即可。
    gEdge = t.clientX <= EDGE;
    if (gEdge) ev.preventDefault();
  }, { passive: false });
  document.addEventListener("touchmove", (ev) => {
    if (!gOn || ev.touches.length !== 1) return;
    const t = ev.touches[0], dx = t.clientX - gx, dy = t.clientY - gy;
    if (Math.abs(dy) > Math.abs(dx)) { gOn = false; return; }   // 纵向为主:是在滚页面
    // 输入区那一路只拦向右:向左滑不触发返回,留给它原本的行为
    if (gEdge || (gInp && dx > 6)) ev.preventDefault();
  }, { passive: false });
  document.addEventListener("touchend", (ev) => {
    if (!gOn) return;
    gOn = false;
    const t = (ev.changedTouches || [])[0];
    if (!t) return;
    const dx = t.clientX - gx;
    if (!gWasOpen && dx > OPEN_AT) {
      document.body.classList.add("mo-drawer");
      if (gInp && document.activeElement && document.activeElement.blur) {
        document.activeElement.blur();   // 抽屉盖上来了,键盘还杵在下面挡着它
      }
    }
    else if (gWasOpen && dx < -OPEN_AT) {
      closeDrawer();
      setTimeout(() => window.dispatchEvent(new Event("resize")), 240);
    }
  }, { passive: true });

  $("#tn-back").onclick = () => navGo(-1);
  $("#tn-fwd").onclick = () => navGo(1);
})();
