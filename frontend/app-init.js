// desktron 前端(app.js 拆分):init 启动装配 + 开发态自动重载
// 经典脚本按序加载共享全局作用域,加载顺序见 index.html。勿单独重排。
const _deskStartupT0 = performance.now();
// 强制更新只能发生在真人与全部 Agent 都静默时。前端只贡献可信的人类操作时间，
// 会话、子代理、终端与审批由后端独立复核；任一侧缺失都不会自动重启。
(function initUpdateActivity() {
  let lastSent = 0;
  const mark = (event) => {
    if (event && event.isTrusted === false) return;
    const now = Date.now();
    if (now - lastSent < 30000) return;
    lastSent = now;
    fetch("/workbench/update/activity", { method: "POST", keepalive: true }).catch(() => {});
  };
  for (const name of ["pointerdown", "keydown", "wheel", "touchstart"])
    document.addEventListener(name, mark, { capture: true, passive: true });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) mark(); });
  mark();
})();
function deskStartupMark(phase, detail = "") {
  const item = { phase, ms: performance.now() - _deskStartupT0, detail };
  window.__DESK_STARTUP__ = window.__DESK_STARTUP__ || [];
  window.__DESK_STARTUP__.push(item);
  try { window.webkit.messageHandlers.startup.postMessage(item); } catch {}
}

async function deskStartupWaitInteractive() {
  let v = null;
  if (typeof pActive !== "undefined" && pActive && String(pActive.url || "").startsWith("chat:")) {
    v = typeof _hsViews !== "undefined" ? _hsViews.get(String(pActive.url).slice(5)) : null;
  }
  // 历史正文由 hsLoad 异步铺入。init 返回或 WKNavigation didFinish 时输入框虽已在 DOM，
  // 会话仍可能是空的；必须等历史成功或明确失败，才算这一页进入可操作状态。
  while (v && v.el && v.el.isConnected && !v.historyReady && !v.loadFailEl) {
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const inputReady = !v || !!(v.inp && v.inp.isContentEditable && v.inp.getClientRects().length);
  const history = !v ? "none" : (v.historyReady ? "ready" : (v.loadFailEl ? "failed" : "detached"));
  deskStartupMark("interactive", `history=${history} input=${inputReady ? "ready" : "missing"}`);
}

async function init() {
  deskStartupMark("init-start");
  DeskI18n.apply();
  for (const [id, key] of [['tn-side', 'nav.toggle'], ['tn-back', 'nav.back'], ['tn-fwd', 'nav.forward'],
    ['side-search-btn', 'nav.search'], ['proj-add', 'nav.newWorkflow'], ['proj-fold-all', 'nav.collapse'], ['settings-btn', 'nav.settings']]) {
    DeskI18n.bind(document.getElementById(id), key, {}, 'title');
    DeskI18n.bind(document.getElementById(id), key, {}, 'aria-label');
  }
  DeskI18n.bind(document.getElementById('side-search-inp'), 'nav.searchHint', {}, 'placeholder');
  bind();
  // 设置是浮在当前工作之上的临时子页面；再点齿轮、遮罩或关闭按钮都回到原现场。
  $("#settings-btn").onclick = () => {
    if (!$("#settings-modal").hidden) { _leaveSettings(); return; }
    openSettings();
  };
  const settingsClose = $("#sm-close");
  if (settingsClose) settingsClose.onclick = _leaveSettings;
  // 设置的可选节点不应阻断工作流和会话初始化；此改动也刷新曾被错配缓存的启动脚本。
  const settingsBackdrop = $("#settings-modal")?.querySelector(".sm-backdrop");
  if (settingsBackdrop) settingsBackdrop.onclick = _leaveSettings;
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#settings-modal").hidden && !_smEngineDialog) _leaveSettings();
  });
  // 先用上回落盘的那份把界面画出来,再拉新的对账。
  //
  // 首屏因此不等网络:经隧道时那一波要一两秒,在此之前原来是一片空白。缓存里存的是
  // 上一次开机拿到的同一批东西,工作区列表这类隔一会儿才变一次,拿旧的先画完全够用;
  // 真数据回来再 renderRail 一次,不一致的地方静默换掉。
  //
  // 只垫 init 自己赋值的那几个全局。引擎与 desk 角色各有副作用(initEnginePick 会渲染、
  // 可能续开会话),不从缓存里恢复,交给下面那一波的真调用。
  const _BOOT_CACHE = "cd_boot";
  try {
    const c = JSON.parse(localStorage.getItem(_BOOT_CACHE) || "null");
    if (c && Array.isArray(c.domains) && c.domains.length) {
      domains = c.domains;
      desktopApps = c.desktopApps || [];
      hiddenDomains = c.hiddenDomains || [];
      if (c.wbState && typeof c.wbState === "object") wbState = c.wbState;
      if (!wbState.sessions) wbState.sessions = {};
      if (c.wbState && c.wbState.ui) applyTheme(c.wbState.ui.theme);
      try { const a = JSON.parse(localStorage.getItem("cd_expanded") || "[]"); if (Array.isArray(a)) expandedProjects = new Set(a); } catch {}
      renderRail();
    }
  } catch (e) { console.warn("开机垫底绘制失败", e); }
  // 会话标题要扫引擎记录,冷启可达秒级。旧列表单独落盘先画,正常轮询在首屏之后对账。
  try { seedTerms(JSON.parse(localStorage.getItem("cd_terms_snapshot") || "null"), { cached: true }); } catch {}

  // 开机要的这几样彼此没有依赖,一起发,不要一个等一个。
  //
  // 原来是七个 await 排成一串。本机直连时每个几毫秒,看不出来;经隧道就是另一回事——
  // 实测每个请求固定一秒多,且与响应大小无关(2.8KB 的 /config 与 433KB 的 style.css
  // 一样慢,是转发的固定开销)。七个串起来就是七八秒,而它们本可以一起走
  // (2026-08-24 用户实报手机首启与刷新都非常慢)。
  //
  // 只有「合并」这一步有先后:本地应用要并进 domains,所以 domains 得先拼好。
  // 取数没有先后,一并发出去,拿回来再按顺序合。
  // 先试一次合并端点:七样一趟拿全,往返从七次降到一次。
  // 拿不到(老后端没有这条路,或它自己出错)就退回并行那一波——那已经比原来的串行好得多,
  // 而且不必让新前端挑后端版本。
  // 首页把开机数据内联进来了(见 ui_server 的 _serve_index):有它就一个往返都不必再花。
  // index.html 本来就是每次加载都要取的,顺路捎上等于白拿。老后端没内联时照旧去请求。
  let boot = (typeof window !== "undefined" && window.__BOOT__) || null;
  if (!boot) {
    try { boot = await api("/workbench/boot"); } catch {}
  }
  let wb, extraRaw, localApps, st;
  if (boot && typeof boot === "object" && Object.keys(boot).length) {
    // 缺哪项补哪项,不是全有全无。那份开机数据里有一样取不到时,只为它单独跑一趟,
    // 其余六样照用——否则一样缺就退回七个请求,白丢了已经拿到的那些
    // (2026-08-25 实见:workbench.json 是静态文件、不归路由,扇出取不到,
    //  于是整份被判作不可用)。
    const need = async (k) => (boot[k] != null ? boot[k] : api(k).catch(() => null));
    [wb, extraRaw, localApps, st] = await Promise.all([
      need("/workbench.json"), need("/workbench/domains"),
      need("/workbench/apps"), need("/workbench/state"),
    ]);
    const hid = await need("/workbench/hidden");
    hiddenDomains = Array.isArray(hid) ? hid : [];
    // 兼容仍把 terminals 内联进来的旧后端；新后端不为它拖住首页首字节。
    if (Array.isArray(boot["/workbench/terminals"])) seedTerms(boot["/workbench/terminals"]);
    applyDeskRole(await need("/workbench/backends"));
    await applyEnginePick(await need("/workbench/engine"));
  } else {
    [wb, extraRaw, localApps, st] = await Promise.all([
      loadDeskRole(),                                   // 本机还是服务器那份,会话默认视图按它分流
      initEnginePick(),                                 // 定引擎
      loadHidden(),                                     // 本机隐藏的工作区
      api("/workbench.json").catch(() => null),         // 分发版工作区目录
      api("/workbench/domains").catch(() => null),      // 运行态工作区
      api("/workbench/apps").catch(() => null),         // vibing 产出的本地应用
      api("/workbench/state").catch(() => null),        // 界面状态(主题、会话次序等)
    ]).then((r) => [r[3], r[4], r[5], r[6]]);
  }
  const base = (wb && wb.domains) || [];
  if (wb && wb.desktopApps) desktopApps = wb.desktopApps;
  // 运行态工作区:同 id 覆盖分发版,独有的追加
  const extra = Array.isArray(extraRaw) ? extraRaw : [];
  domains = mergeDomains(base, extra);
  await mergeLocalApps(localApps);   // 并入本地注册表(取数已在上面那一波里)
  renderRail();
  // 写法保持 `let stateOk = false` 这一支:test_frontend_statusbar_theme 按它认
  // 「applyTheme 挂在请求成功这个条件上」。那条不变式我同意——请求没成时不能碰主题,
  // 一次失败就会把状态栏钉成黑的。上面垫底那段调 applyTheme 用的是缓存里的真值,
  // 不是默认值,不违反它。
  let stateOk = false;
  if (st && typeof st === "object") { wbState = st; stateOk = true; }
  // 真数据回来了,落一份给下次开机垫底。只存这几样——它们是画出侧栏所需的全部,
  // 而且都不含票据与会话正文。
  try {
    localStorage.setItem(_BOOT_CACHE, JSON.stringify({
      domains, desktopApps, hiddenDomains, wbState: stateOk ? st : null,
    }));
  } catch {}
  // 请求没成时别碰主题。那时 wbState 还是默认值,applyTheme(undefined) 会落回深色,
  // 并把 localStorage 里的 cd_theme 一起覆盖成深色——而 index.html 顶上那段是按这个缓存
  // 决定状态栏 meta 的。手机经隧道访问时这一路本来就容易失败,一次失败就把状态栏钉成黑的:
  // iOS 独立窗口只在启动那一刻读 apple-mobile-web-app-status-bar-style,之后改不动
  // (2026-08-24 用户实报浅色下顶上仍是黑条)。请求失败就保持启动时按缓存落好的那一档。
  if (stateOk) applyTheme(wbState.ui && wbState.ui.theme);
  if (!wbState.sessions) wbState.sessions = {};
  // 后端不知道界面语言。前端上报当前语言，后端自建的会话（安排、Bot、子任务）据此选择
  // 提示语言；界面新建的会话另在请求中直接带上。已建会话的提示语言不随之改变。
  const syncPromptLocale = () => {
    const locale = DeskI18n.locale;
    if (!stateOk || wbState.prompt_locale === locale) return;
    wbState.prompt_locale = locale;
    saveState({ prompt_locale: locale });
  };
  syncPromptLocale();
  globalThis.addEventListener("desk-locale", syncPromptLocale);
  // 恢复展开的项目折叠(还原后重渲树:renderRail 那次跑在恢复前,折叠态是空的)
  try { const a = JSON.parse(localStorage.getItem("cd_expanded") || "[]"); if (Array.isArray(a)) expandedProjects = new Set(a); } catch {}
  renderSessionPanel();
  deskStartupMark("boot-data-rendered");
  // 恢复上次所在页(刷新不再跳回 desktop);失效(工作区已删/页不存在)则回落 desktop
  let startNav = { type: "domain", id: DESKTOP };
  try {
    const s = JSON.parse(localStorage.getItem("cd_nav") || "null");
    const ok = s && s.type === "domain" && (s.id === DESKTOP || domains.some((d) => d.id === s.id));
    if (ok) startNav = s;
  } catch {}
  // init 不让 selectNav 触发 syncTermView:恢复「刷新前看的那一页」由下面的
  // restorePagesAndBrowser 按 cd_pages 的 open/active 做,两条都去 attach 会打架、
  // 刷新后选中漂移。
  //
  // 这句注释原来写的是「下面 restoreTerminal 按 cd_term 单独恢复」。那个函数随 TUI 退场
  // 一起删了,调用点也清了,而这个开关留着——补偿的人没了,闸还关着,于是刷新之后
  // 谁都不恢复会话,落在 cd_nav 记的那一页上(2026-08-24 用户实见:手机上刷新停在工坊)。
  selectNav(startNav.type, startNav.id, { noTermSync: true });
  initDock();       // 右缘程序坞(自动隐藏,感应带唤出)
  // 空闲预热设置缓存(后端登录探测慢,见 app-settings.js)。远端推迟到 20 秒后，
  // 避开首屏恢复的连接高峰；工坊资产预热已随对应页面退役。
  // 审批轮询:后端可能正挂着一个等人点头的动作,而它只在被问到时才说。
  // 两端都要问——人在手机上时,原生壳那条路弹在 Mac 上没人看见(2026-09-02 用户指出)。
  // 5 秒一轮:审批是人在等的事,慢了就成了卡住;这条请求很轻(命中时才有内容)。
  // 页面不可见时不问:看不见的页弹不出审批框,手机切到后台后这条每 5 秒一次的往返是白付的。
  if (typeof apprPoll === "function") {
    setTimeout(apprPoll, 2500);
    setInterval(() => { if (!document.hidden) apprPoll(); }, 5000);
  }

  const _warm = isLocalView() ? 1 : 20000;
  setTimeout(prefetchSettings, 3000 + _warm);
  // 首次设置浮层:这台机器从没打开过就弹一次(后端 seen 标记,打开即记)。之后只从设置页进
  // ——不按「没配完就弹」,那会让不用远程的人每次启动都被拦一道(2026-09-01 用户定)。
  // 只在本机弹:远端访问的浏览器做不了装引擎、登录这类配置动作,
  // 弹一个自己做不了的清单只是打扰。失败静默,不为一个引导打断启动。
  // 新用户底层仍是任务导向欢迎页；设置清单先以浮层完成运行准备，关闭后自然回到欢迎页。
  if (isLocalView()) {
    setTimeout(() => {
      if (typeof openSetupGuide === "function") openSetupGuide({ auto: true }).catch(() => {});
    }, 1200);
  }
  // 关掉首次设置后仍有回程入口；必要项完成即自动消失。
  if (isLocalView() && typeof setupResumeEntry === "function") {
    setTimeout(() => setupResumeEntry(), 1800);
  }
  if (isLocalView() && typeof watchUpdateCompletions === "function") {
    setTimeout(() => watchUpdateCompletions(), 2600);
  }
  // 恢复刷新前打开的悬浮框(文件浏览/链接查看页)、浏览器应用的网页标签,
  // 以及当时正看着的那一页(会话页由它按 cd_pages 的 open/active 重新露面)。
  // 恢复期间禁止页面显示函数反向落盘：restorePages 会先短暂露出旧版全局 active，
  // 它只是建页过程，不是用户新选择的最后页面。
  let engineViewRestored = false;
  _restoringEngineViewState = true;
  try {
    restorePagesAndBrowser();
    // cd_pages.active 是旧版的全局当前位置；不同引擎轮流写它，后来的会覆盖先前的。
    // 会话/应用先在上一步建成隐藏页，再按当前引擎自己的快照把正确那一页翻到前台。
    engineViewRestored = typeof restoreEngineView === "function"
      ? await restoreEngineView(curEngine) : false;
    // 主区那一页摆好之后再摆右侧栏:两者可能指向同一个应用页,而主区的意愿更近
    // (restoreAppSide 遇到 pActive 就跳过那一栏)。放在恢复保护里面,免得中间态被写回引擎快照。
    if (typeof restoreAppSide === "function") { try { await restoreAppSide(); } catch {} }
  } finally {
    _restoringEngineViewState = false;
  }
  // 主区那一页回来了,侧栏那一行的高亮也要跟着回来(见 restoreActiveTerm 的说明)。
  restoreActiveTerm();
  // 旧快照迁移或兜底恢复完成后，把最终可见页写成新版快照；之后刷新只读这一份。
  if (typeof rememberEngineView === "function") rememberEngineView(curEngine);
  // 从飞书通知点进来的那一路(?approve=&session=)放在恢复之后:它要切到指定的那段会话,
  // 而上面几步正按 cd_pages / 引擎快照把「上次看的那一页」翻到前台。原来挂在定时器上
  // (1.2 秒),等于和恢复流程赛跑——切好的页随即被覆盖,人看到的是「闪一下又回到别处,
  // 卡片不见了」,再进一次才好(2026-09-03 用户实见)。按顺序接在这里就没有这个窗口。
  if (typeof apprFromLink === "function") { try { await apprFromLink(); } catch {} }
  // 一页都没露面时,把主区那层浮层收掉,摆回空态。
  //
  // #overlay 在 index.html 里不带 hidden,初始就是显示的,而只有 showView(非 overlay) 会去藏它
  // ——init 这条路带着 noTermSync 走,不经过那里。于是「没有页可恢复」时,主区是一整块空白的
  // 浮层压在上面,侧栏却是好的(2026-08-25 用户实见:主区空、侧栏在)。切引擎之后尤其容易撞上:
  // 那一刻本来就不该恢复上一个引擎的会话,主区正是空的。
  //
  // 不走 clearTerminalView:它会顺手删掉 cd_term,而那一项是侧栏高亮的唯一来源,删了下次就找不回。
  if (!engineViewRestored) {
    if (typeof pActive === "undefined" || !pActive) {
      showView("terminal");
      if (typeof _showWatermark === "function") _showWatermark();
      // term-on 的语义是「终端在场」。空态是终端视图却没有终端,留着这个标,
      // 手机端会在起始页底部凭空多出一条附加键栏(同 clearTerminalView 里那一句)。
      document.body.classList.remove("term-on");
    }
    // 升级前没有分引擎快照。旧恢复链已经把当前页摆好后，顺手把它作为这个引擎的
    // 第一条记录；这样用户只需刷新一次，不必先牺牲一次切换才能开始记忆。
    if (typeof rememberEngineView === "function") rememberEngineView(curEngine);
  }
  // 事件流不再只属于“已经打开一段会话”的页面。它的首帧同时承担服务实例握手:
  // 手机停在设置/工坊/欢迎页时，后端重启也要能自愈。
  if (typeof hsEnsureSSE === "function") hsEnsureSSE();
  // 这里原来还有一句 `await restoreTerminal(curEngine)`。那个函数随 TUI 退场一起删了,
  // 调用点留了下来——init 每次都在最后一行抛 ReferenceError(2026-08-24 发现)。
  // 终端标签的恢复走另一条(restoreTerminalTabs,由 restorePagesAndBrowser 调)。
  deskStartupMark("init-complete");
  deskStartupWaitInteractive();
}
init().catch((error) => {
  console.error("Desk 页面初始化失败", error);
  // 初始化失败不能只留空侧栏，让人误以为工作流和会话被删除。
  const host = $("#panel-list");
  if (!host) return;
  const notice = document.createElement("div");
  notice.setAttribute("role", "alert");
  notice.style.cssText = "padding:16px;line-height:1.6";
  notice.textContent = "页面加载失败，暂时无法显示工作流和会话。";
  const retry = document.createElement("button");
  retry.type = "button";
  retry.textContent = "重新加载";
  retry.onclick = () => location.reload();
  notice.appendChild(retry);
  const detail = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = "错误详情";
  const trace = document.createElement("pre");
  trace.style.cssText = "white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px";
  trace.textContent = `${error?.name || "Error"}: ${error?.message || String(error)}\n${error?.stack || ""}`;
  detail.append(summary, trace);
  notice.appendChild(detail);
  host.appendChild(notice);
});
// 系统安排入口承载记忆引擎和 Desk 后端的健康提醒。
// 修复落盘但尚未重启时，仍需明确显示旧代码状态。
(function agentHealthPoll() {
  const paint = (bad, why) => {
    const el = document.querySelector("#fx-tasks");
    if (!el) return;
    el.classList.toggle("has-alert", !!bad);
    if (bad) el.title = "系统安排需要处理:" + why.join("、");
    else el.removeAttribute("title");
  };
  const tick = async () => {
    let st;
    try { st = await api("/workbench/agents/status"); } catch { return; }   // 取不到不报警,免得后端一抖就红
    const why = [];
    const m = st.memory || {};
    if ((m.project_memory || {}).stopped) why.push("记忆引擎已停机");
    if ((st.desk || {}).stale_code) why.push("Desk 后端在跑旧代码,需重启");
    paint(why.length, why);
  };
  if (isLocalView()) tick();
  else setTimeout(tick, 2000);
  setInterval(() => { if (!document.hidden) tick(); }, 60000);   // 一分钟一次:这几项都是以小时计的状态,不必更密;不可见时不问
})();


// 面板宽度可拖:拖右缘调 --panel-w(180–520px),存 localStorage,双击复位默认。
// 终端侧不用管:term 宿主已挂 ResizeObserver,停手后 debounce 一次 fit+winsize。
(function panelResize() {
  const KEY = "cd_panel_w", DEF = 248, MIN = 180, MAX = 520;
  const setW = (w) => document.documentElement.style.setProperty("--panel-w", `${w}px`);
  const saved = parseInt(localStorage.getItem(KEY), 10);
  if (saved >= MIN && saved <= MAX) setW(saved);
  const bar = $("#panel-resizer");
  if (!bar) return;
  // 改宽度只动 CSS 变量,不会自动惊动那些自己算几何的东西:xterm 按容器宽算列数、
  // 原生 WKWebView 面板不在 DOM 里、靠前端上报矩形。不通知它们,拖完就会看到正文列还是
  // 旧宽度、终端不换行、外链面板盖错位置,得刷新才恢复(2026-08-15 用户实见)。
  // 通知要合并。原来合并到每帧(rAF),那仍是每秒六十次全应用几何同步:一次 resize 事件
  // 要跑遍所有监听者——原生面板重定位(经 webkit.messageHandlers 过一次 JS↔原生边界)、
  // 目录重算位置、每个会话视图对齐输入框——它们各自 getBoundingClientRect 再写样式,
  // 一帧里反复触发同步布局。拖动因此发涩(2026-08-23 用户实见)。
  //
  // 改成两档:CSS 变量每次指针移动都写(那是纯变量,布局由合成器接手,拖起来才跟手),
  // 几何同步降到 100ms 一次,松手时再精确同步一遍。终端与原生面板慢 100ms 跟上,
  // 拖动过程中看不出来;而省下的是每秒五十多次跨边界通知。
  const SYNC_GAP = 100;
  let syncAt = 0, syncTimer = 0;
  const syncNow = () => {
    syncAt = Date.now();
    try { window.dispatchEvent(new Event("resize")); } catch {}
    // browserSyncNative 不在这里再调一次:app-browser.js 已经把它挂在 resize 上,
    // 上面那一行就会触发。原来两处都调,等于每一轮把原生面板重定位做了两遍。
    if (typeof termAppRefit === "function") { try { termAppRefit(); } catch {} }
  };
  const syncGeom = (now = false) => {
    if (syncTimer) { clearTimeout(syncTimer); syncTimer = 0; }
    if (now) { syncNow(); return; }
    const left = SYNC_GAP - (Date.now() - syncAt);
    if (left <= 0) syncNow();
    else syncTimer = setTimeout(() => { syncTimer = 0; syncNow(); }, left);
  };
  const finish = (e) => {
    bar.onpointermove = bar.onpointerup = bar.onpointercancel = null;
    document.body.classList.remove("panel-resizing");
    const w = Math.min(MAX, Math.max(MIN, Math.round(w0v + (e ? e.clientX : lastX) - x0v)));
    setW(w); syncGeom(true);   // 松手这一次要精确,不再延后
    if (w === DEF) localStorage.removeItem(KEY); else localStorage.setItem(KEY, String(w));
  };
  let x0v = 0, w0v = 0, lastX = 0;
  bar.onpointerdown = (ev) => {
    ev.preventDefault();
    bar.setPointerCapture(ev.pointerId);
    x0v = ev.clientX; w0v = $("#panel").getBoundingClientRect().width; lastX = ev.clientX;
    document.body.classList.add("panel-resizing");
    bar.onpointermove = (e) => {
      lastX = e.clientX;
      setW(Math.min(MAX, Math.max(MIN, Math.round(w0v + e.clientX - x0v))));
      syncGeom();
    };
    bar.onpointerup = finish;
    // 指针在窗口外松开、或被系统取消时 pointerup 不一定来。少了这一路,body 会卡在
    // panel-resizing(整页 user-select 关着、iframe 不收事件),表现同样是「要刷新才好」。
    bar.onpointercancel = finish;
  };
  bar.ondblclick = () => { setW(DEF); localStorage.removeItem(KEY); syncGeom(); };
})();

// 注册表保鲜:轮询 /workbench/apps-rev(apps.json + domains.json mtime),变了就重拉。
// 覆盖带外注册——agent 在对话里部署 app、reconcile 补默认工作区依赖,都不经前端 UI,
// 原先要切工作区(selectNav)才显现。此处常驻轮询使其自动出现,无需用户操作。
// 首次只记基线不刷新;之后变更才 reloadDomains + 重渲当前面板。
// 2026-08-25:这一层退成兜底。变化由服务端 stat 那两份文件之后推 apps.updated
// (见 lib/task_watch),到得比轮询快,而且不必每 2.5 秒往返一趟——经隧道实测 30 秒里
// 12 次、每次换回 0 字节,是当时最密的一条。轮询不整个撤:服务端不重放断线窗口里的事件,
// 撤了之后通道一坏就再没人发现。远端拉到 30 秒,本机直连 0~1ms 维持原样。
let _appRev = null;
async function onAppsChanged(rev) {
  if (rev != null && rev === _appRev) return;   // 两条路都会到,同一个版本只处理一次
  _appRev = rev != null ? rev : _appRev;
  await reloadDomains();                                  // 重拉域 + 本地 app 注册表 + 刷左栏
  if (typeof refreshResourceApps === "function") refreshResourceApps();
  if (nav && nav.type === "domain") renderSessionPanel(); // 正看工作区面板:把新 app 渲出来
}

(async function appRevPoll() {
  // 基线先从首页内联的开机数据取(ui_boot.BOOT_PARTS 已带 apps-rev),取不到再问一趟。
  const _pre = (typeof window !== "undefined" && window.__BOOT__) || null;
  const _rev0 = _pre && _pre["/workbench/apps-rev"];
  if (_rev0 && _rev0.rev != null) _appRev = _rev0.rev;
  else try { const r = await api("/workbench/apps-rev"); _appRev = r && r.rev; } catch {}
  setInterval(async () => {
    if (document.hidden) return;   // 后台标签页不问:看不见的东西不必保鲜
    let rev;
    try { const r = await api("/workbench/apps-rev"); rev = r && r.rev; } catch { return; }
    if (rev == null || rev === _appRev) return;
    await onAppsChanged(rev);
  }, isLocalView() ? 2500 : 30000);
})();

// 开发态自动重载:前端文件 mtime 变了就 reload。ui_server 不死、PTY 会话照活,
// reload 后 /term 重连回活会话(缓冲回放),不打断正在跑的 agent。非开发态端点返回 dev:false,不轮询。
//
// 只在本机这一页开。原来的判据是「服务端是不是开发态」,而 dev 与否是服务端一侧的属性:
// 手机经隧道连进来看到的是同一份服务端,于是也跟着每 1.5 秒打一个跨区往返
// (2026-08-25 实测 33 秒里 19 次,每次 240~700ms)。那一侧既没人改前端文件,重载还会把
// 正在看的页面连同滚动位置一起冲掉——代价实打实,收益是零。
(async function devAutoReload() {
  if (!isLocalView()) return;
  let tok;
  try { const r = await api("/dev/token"); if (!r || !r.dev) return; tok = r.token; } catch { return; }
  setInterval(async () => {
    try {
      const r = await api("/dev/token");
      if (r && r.dev && r.token !== tok) {
        if (typeof deskStartupMark === "function") deskStartupMark("reload", `dev_token ${tok}->${r.token}`);
        location.reload();
      }
    } catch {}
  }, 1500);
})();

// 自绘覆盖式滚动条(应用内所有主要滚动容器共用一套,见 SEL):彻底不用 ::-webkit-scrollbar 伪元素
// (它的重绘不完全跟 classList 走,纯 JS 摘 class 那个时机没有真实交互命中滚动条区域就不会重绘,
// 画面会卡在上一次真实交互的样子——反复验证过,弃用)。改画真实 <div> 滑块,position:fixed 悬浮在
// 内容之上不占布局,几何(位置/高度)与显隐全由这里计算,不依赖任何引擎内部的重绘时机。
// 滚动/wheel/悬停三路都能亮起,静止 800ms 隐去,支持拖拽。
(function autoScrollbars() {
  const SEL = [
    "#panel-list", ".xterm-viewport",           // 侧栏、终端(保活池同一时刻多个存在,但只有可见的会触发)
    ".fb-content-scroll", ".fb-left", ".fb-tree", ".fb-right",   // 文件浏览:目录树 + 内容
    ".overlay-content", "#asset-view", ".app-detail", ".store-host",   // 通用页面容器
    ".hs-body", ".hs-agv-b",   // 主正文与子代理只读页共用无占位的自绘滚动条
    ".rp-model-list",   // 模型菜单与 macOS Desk 的共用覆盖式滑块一致
    ".hs-tt-o", ".hs-tt-c", ".hs-pre", ".hs-diff",   // 工具输出、命令、原始数据与读取/改动
    ".hs-current-tasks", ".hs-tasks-list",   // 任务栏弹出卡片：当前会话任务与所有待办
    // 信息卡的审批列表:它自己向右探出 18px 当车道(见 style.css),滑块正好落在那条道里,
    // 卡片右缘因此能与标题行的箭头对齐。不进这张表就只能留着原生条,而原生条要占宽。
    ".hs-sit-approval-host",
    // 同一张卡上的制品列表,展开后限高滚动,用同一条车道(见 style.css)
    ".hs-sit-artifact-list",
    // 常见入口清单用于事件快速匹配；其余含内联 overflow 的容器由计算样式自动接入。
    ".hs-context-status-detail", ".hs-arrangements-list", ".hs-env-d", ".hs-inp",
    ".resource-scroll", "#sm-body", "#sm-settings-scroll", ".sm-inner-nav",
    ".sm-engine-dialog-body", ".setup-workspace", ".appr-notice-body",
    ".fb-tree-body", ".fb-results", "#side-search-results", "#task-dock",
    ".hs-rv-files", ".hs-rv-body", ".hs-lr-body", ".hs-fp", ".hs-fp-md",
    ".wf-sheet-body", ".ri-list", ".arr-sys-logs", ".arr-result-full",
    "#logdock-body",
    ".md pre", ".md table", ".br-tabs", ".fa-tabs", ".tm-tabs", ".tu-heat-scroll",
  ].join(", ");
  // container(滚动元素)→ 滑块 div,懒建复用;每个滚动容器一个滑块,不是每个选择器一个——
  // 文件浏览左树与右内容同时可见、各自独立滚动位置,必须分开。容器从 DOM 摘除(切页/关窗/会话被
  // LRU 淘汰)后由轮询清扫回收对应滑块,不会无界累积。
  const thumbs = new Map();
  // 手机侧栏和聊天正文只随滚动亮起；触摸后的模拟悬停不能让滑块常驻。
  // 与侧栏抽屉的 CSS 断点一致，也覆盖横屏手机。
  function scrollOnly(key) {
    return (key.id === "panel-list" || key.matches?.(".hs-body")) && typeof window !== "undefined" &&
      window.matchMedia?.("(max-width: 820px), (max-height: 520px) and (pointer: coarse)").matches;
  }
  // 正文的聚焦、键盘布局和自动贴底也会产生 scroll，不能误当手指滑动。
  const chatScrollIntent = new WeakMap();
  function noteChatScroll(key) {
    const body = key?.closest?.(".hs-body");
    if (body && scrollOnly(body)) chatScrollIntent.set(body, Date.now() + 1200);
  }
  // 只接管原生 overflow，不碰 Monaco 等组件自绘的滚动条或跨域浏览器页面。
  function scrollable(key) {
    if (!key?.matches || key.closest(".sb-thumb")) return false;
    // 输入框底栏和会话刻度列保留滚动，但不绘制滑块；CSS 已隐藏原生条。
    // 必须在计算 overflow 前排除，否则自动扫描仍会补回覆盖条。
    if (key.matches(".hs-row, .frw-composer-row, .hs-toc")) return false;
    // 手机设置导航保留横向滑动和原生条隐藏样式，不再绘制顶部覆盖条。
    // 桌面左侧设置导航仍保留纵向滑块。
    if (key.matches(".settings-center-live .sm-inner-nav") &&
        window.matchMedia?.("(max-width: 820px), (max-height: 520px) and (pointer: coarse)").matches) return false;
    if (key.matches(SEL)) return true;
    const css = getComputedStyle(key);
    return /(auto|scroll)/.test(css.overflowY) || /(auto|scroll)/.test(css.overflowX);
  }
  function nearest(start) {
    for (let key = start; key && key !== document.body; key = key.parentElement)
      if (scrollable(key)) return key;
    return null;
  }
  // key 三态:宿主滚动容器 / 同源 iframe(根文档滚动,滚动者从其当前文档现取,页内导航会换) /
  // iframe 内部滚动元素(t._frame 记所属 iframe,几何要叠加 iframe 在宿主里的偏移)。
  function scrollerOf(key) {
    if (key.tagName !== "IFRAME") return key;
    try { return key.contentDocument && key.contentDocument.scrollingElement; } catch { return null; }
  }
  function shown(key) {
    return key.offsetParent !== null || (key.getClientRects?.().length > 0 && getComputedStyle(key).position === "fixed");
  }
  function layer(key) {
    let z = 500;
    for (let p = key; p && p !== document.body; p = p.parentElement) {
      const value = Number.parseInt(getComputedStyle(p).zIndex, 10);
      if (Number.isFinite(value)) z = Math.max(z, value + 1);
    }
    return String(z);
  }
  function geom(t) {   // 统一几何:el=真实滚动者;top/right/height=滑道矩形(宿主坐标系);visible
    const key = t._key;
    const r = key.getBoundingClientRect();
    if (key.tagName === "IFRAME")
      return { el: scrollerOf(key), top: r.top, right: r.right, left: r.left,
               bottom: r.bottom, height: r.height, width: r.width, visible: shown(key) };
    if (!t._frame) {
      // #term-host 限宽居中(大窗口下会话页边距很宽,详见其 CSS 注释),滑块车道要贴 #terminal 真实右缘,
      // 不能跟着居中窄栏的 right 走,否则宽屏下滑块会缩在页面中间一截,不贴窗口边框
      const panel = key.closest("#term-host") && key.closest("#terminal");
      const right = panel ? panel.getBoundingClientRect().right : r.right;
      return { el: key, top: r.top + (key.clientTop || 0), right,
               left: r.left || 0, bottom: r.top + (key.clientTop || 0) + key.clientHeight,
               height: key.clientHeight, width: key.clientWidth,
               visible: shown(key) };
    }
    const fr = t._frame.getBoundingClientRect();
    return { el: key, top: r.top + fr.top, right: r.right + fr.left, left: r.left + fr.left,
             bottom: r.bottom + fr.top, height: r.height, width: r.width,
             visible: shown(key) && shown(t._frame) };
  }
  function dragListeners(t, move, up) {
    t._cancelDrag?.();
    const frame = t._frame || (t._key.tagName === "IFRAME" ? t._key : null);
    let doc;
    try { doc = frame?.contentDocument; } catch {}
    const frameMove = (e) => {
      const r = frame.getBoundingClientRect();
      move({clientX: e.clientX + r.left, clientY: e.clientY + r.top});
    };
    document.addEventListener("mousemove", move); document.addEventListener("mouseup", up);
    doc?.addEventListener("mousemove", frameMove); doc?.addEventListener("mouseup", up);
    t._cancelDrag = up;
    return () => {
      document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up);
      doc?.removeEventListener("mousemove", frameMove); doc?.removeEventListener("mouseup", up);
      t._cancelDrag = null;
    };
  }
  function makeThumb(key, frame) {
    const t = document.createElement("div");
    t.className = "sb-thumb";
    const menu = key.matches && key.matches(".rp-model-list");
    const local = key.tagName !== "IFRAME";
    // 包括嵌入页在内统一 8px / 4px，不为个别菜单另设宽度和颜色。
    t._width = 8;
    if (menu) t.classList.add("sb-menu");
    t._key = key;
    t._frame = frame || null;
    document.body.appendChild(t);
    thumbs.set(key, t);
    key.setAttribute?.("data-desk-scrollbar", "");
    t.addEventListener("pointerdown", (e) => e.stopPropagation());
    t.addEventListener("mousedown", (e) => {
      e.stopPropagation?.();
      const el = geom(t).el;
      if (!el) return;
      e.preventDefault();
      // 滑块挂在 document.body，不会经过正文的 pointerdown。先交还滚动控制，
      // 再记录起点（会话解除贴底时可能把裁掉的旧历史挂回来）。
      el.dispatchEvent(new CustomEvent("desk-scroll-start"));
      const startY = e.clientY, startTop = el.scrollTop;
      t.classList.add("dragging");
      clearTimeout(t._hideT);
      const move = (ev) => {
        const g = geom(t);
        const overflow = el.scrollHeight - el.clientHeight;
        const thumbH = Math.min(g.height, Math.max(24, g.height * (el.clientHeight / el.scrollHeight)));
        const travel = g.height - thumbH || 1;
        el.scrollTop = startTop + (ev.clientY - startY) * (overflow / travel);
      };
      const up = () => {
        t.classList.remove("dragging");
        stop(); scheduleHide(t);
      };
      const stop = dragListeners(t, move, up);
    });
    t.addEventListener("mouseenter", () => {
      t._hover = true; if (!scrollOnly(key)) clearTimeout(t._hideT);
    });
    t.addEventListener("mouseleave", () => { t._hover = false; scheduleHide(t); });
    if (local) {
      // 菜单或工具详情出现时鼠标可能已在里面；不必先滚动或命中滑块才看得见。
      t._listHover = key.matches(":hover");
      key.addEventListener("mouseenter", () => { t._listHover = true; reveal(key); });
      key.addEventListener("mouseleave", () => { t._listHover = false; scheduleHide(t); });
    }
    return t;
  }
  function layoutHorizontal(t, g) {
    const el = g.el, overflow = el ? el.scrollWidth - el.clientWidth : 0;
    // 手机输入框只允许纵向滚动。iOS 编辑时光标/组合输入可能留下横向余量，
    // 不能仅凭 scrollWidth 就给 overflow-x:hidden 的输入框补画一条横向滑块。
    // 在每次布局时判断，也清掉从桌面切到手机尺寸前已显示的横条。
    const mobileInput = el?.matches?.(".hs-inp, .frw-input") && typeof window !== "undefined" &&
      window.matchMedia?.("(max-width: 820px), (max-height: 520px) and (pointer: coarse)").matches;
    if (mobileInput || !el || !(overflow > 1) || !(g.width > 0) || !g.visible) {
      t._x?.classList.remove("sb-show"); return false;
    }
    if (!t._x) {
      const x = document.createElement("div");
      x.className = "sb-thumb sb-horizontal";
      if (t.classList.contains("sb-menu")) x.classList.add("sb-menu");
      x._key = t._key; x._frame = t._frame;
      t._x = x; document.body.appendChild(x);
      x.addEventListener("pointerdown", (e) => e.stopPropagation());
      x.addEventListener("mousedown", (e) => {
        e.preventDefault(); e.stopPropagation();
        const el = geom(t).el;
        if (!el) return;
        el.dispatchEvent(new CustomEvent("desk-scroll-start"));
        const startX = e.clientX, startLeft = el.scrollLeft;
        x.classList.add("dragging");
        clearTimeout(t._hideT);
        const move = (ev) => {
          const g = geom(t), travel = g.width - parseFloat(x.style.width);
          const direction = getComputedStyle(el).direction === "rtl" ? -1 : 1;
          if (travel > 0) el.scrollLeft = startLeft + direction * (ev.clientX - startX) * ((el.scrollWidth - el.clientWidth) / travel);
        };
        const up = () => {
          x.classList.remove("dragging");
          stop(); scheduleHide(t);
        };
        const stop = dragListeners(t, move, up);
      });
      x.addEventListener("mouseenter", () => {
        t._xHover = true; if (!scrollOnly(t._key)) clearTimeout(t._hideT);
      });
      x.addEventListener("mouseleave", () => { t._xHover = false; scheduleHide(t); });
    }
    const width = Math.min(g.width, Math.max(24, g.width * (el.clientWidth / el.scrollWidth)));
    // scrollLeft 在 RTL 下为负值；按绝对进度定位，不改变浏览器自身滚动语义。
    const rtl = getComputedStyle(el).direction === "rtl";
    const progress = Math.min(1, Math.abs(el.scrollLeft || 0) / overflow);
    const x = t._x;
    x.style.left = (g.left + (g.width - width) * (rtl ? 1 - progress : progress)) + "px";
    x.style.top = (g.bottom - 10) + "px"; x.style.width = width + "px";
    x.style.zIndex = layer(t._frame || t._key);
    clip(x, t, g);
    return true;
  }
  function clip(bar, t, g) {
    // 两轴都遵守宿主、iframe 及 overflow 祖先的裁剪；固定滑块不穿越卡片边界。
    let top = g.top, bottom = g.top + g.height, left = g.left ?? -Infinity, right = g.right;
    if (t._frame) {
      const fr = t._frame.getBoundingClientRect();
      top = Math.max(top, fr.top); bottom = Math.min(bottom, fr.bottom);
      left = Math.max(left, fr.left); right = Math.min(right, fr.right);
    }
    for (let p = t._key.parentElement; p && p !== document.body; p = p.parentElement) {
      const css = getComputedStyle(p), r = p.getBoundingClientRect();
      const fr = t._frame?.getBoundingClientRect();
      const dy = fr?.top || 0, dx = fr?.left || 0;
      if (/(auto|scroll|hidden|clip)/.test(css.overflowY)) {
        top = Math.max(top, r.top + dy); bottom = Math.min(bottom, r.bottom + dy);
      }
      if (/(auto|scroll|hidden|clip)/.test(css.overflowX)) {
        left = Math.max(left, r.left + dx); right = Math.min(right, r.right + dx);
      }
    }
    const y = parseFloat(bar.style.top), x = parseFloat(bar.style.left);
    const h = parseFloat(bar.style.height) || 8, w = parseFloat(bar.style.width) || 8;
    const cut = (size, value) => Math.min(size, Math.max(0, value));
    const cuts = [cut(h, top - y), cut(w, x + w - right), cut(h, y + h - bottom), cut(w, left - x)];
    bar.style.clipPath = cuts[1] === 0 && cuts[3] === 0
      ? `inset(${cuts[0]}px 0 ${cuts[2]}px 0)` : `inset(${cuts.map(v => v + "px").join(" ")})`;
  }
  function layout(t) {   // 纯几何:位置/高度,不动显隐(轮询兜底用,不该把已隐藏的条重新点亮)
    const g = geom(t), el = g.el;
    t._horizontalOK = layoutHorizontal(t, g);
    const overflow = el ? el.scrollHeight - el.clientHeight : 0;
    t._verticalOK = !!el && overflow > 1 && g.height > 0 && g.visible;
    if (!t._verticalOK) { t.classList.remove("sb-show"); return t._horizontalOK; }
    const thumbH = Math.min(g.height, Math.max(24, g.height * (el.clientHeight / el.scrollHeight)));
    t.style.zIndex = layer(t._frame || t._key);
    t.style.top = (g.top + (g.height - thumbH) * (el.scrollTop / overflow)) + "px";
    // 车道划分(2026-07-14 对调):滑块占最外道(贴窗口右缘时为窗口右 2~10px),
    // 程序坞常驻感应线让进内道(见 style.css #dock-sensor::after,窗口右 12~15px)
    t.style.left = (g.right - t._width - 2) + "px";
    t.style.height = thumbH + "px";
    clip(t, t, g);
    return true;
  }
  function scheduleHide(t) {
    clearTimeout(t._hideT);
    if ((!scrollOnly(t._key) && (t._hover || t._xHover || t._listHover)) ||
        t.classList.contains("dragging") || t._x?.classList.contains("dragging")) return;
    t._hideT = setTimeout(() => { t.classList.remove("sb-show"); t._x?.classList.remove("sb-show"); }, 800);
  }
  function reveal(key, frame, fromScroll = false) {
    if (!key || !shown(key)) return;
    const t = thumbs.get(key) || makeThumb(key, frame);
    if (!layout(t)) return;
    // 注册、打开、聚焦、DOM 更新和悬停不点亮手机侧栏或正文。
    if (scrollOnly(key)) {
      if (!fromScroll) return;
      if (key.matches?.(".hs-body") && !t.classList.contains("dragging")) {
        if (!(chatScrollIntent.get(key) > Date.now())) return;
        noteChatScroll(key); // 连续滚动与惯性滚动延续显示，静止后恢复隐藏。
      }
    }
    if (t._verticalOK) t.classList.add("sb-show");
    if (t._horizontalOK) t._x.classList.add("sb-show");
    scheduleHide(t);
  }
  // 工具详情动态插入，首次移入就创建滑块，避免只隐藏原生条却没有可见替代。
  document.addEventListener("mouseover", (e) => {
    const key = nearest(e.target);
    if (!key || scrollOnly(key) || (e.relatedTarget && key.contains(e.relatedTarget))) return;
    reveal(key);
    const t = thumbs.get(key);
    if (t) { t._listHover = true; clearTimeout(t._hideT); }
  });
  // 局部菜单完成定位/限高或关闭后显式通知，不依赖用户先滚动。
  document.addEventListener("desk-scrollbar-refresh", (e) => {
    const key = e.target;
    if (!scrollable(key)) return;
    const t = thumbs.get(key);
    if (t) {
      t._listHover = key.matches(":hover");
      if (!shown(key)) {
        clearTimeout(t._hideT); t.classList.remove("sb-show"); t._x?.classList.remove("sb-show"); return;
      }
    }
    reveal(key);
  });
  let reflowFrame = 0;
  function reflowShownThumbs() {
    if (reflowFrame) return;
    reflowFrame = requestAnimationFrame(() => {
      reflowFrame = 0;
      // 滑块挂在 body 且 position:fixed；任一祖先滚动都会改变嵌套容器的视口坐标。
      for (const t of thumbs.values())
        if (t.classList.contains("sb-show") || t.classList.contains("dragging") || t._x?.classList.contains("sb-show")) layout(t);
    });
  }
  function canScrollToward(el, deltaY) {
    if (!el) return false;
    const overflow = el.scrollHeight - el.clientHeight;
    if (overflow <= 1 || !deltaY) return overflow > 1;
    return deltaY < 0 ? el.scrollTop > 1 : el.scrollTop < overflow - 1;
  }
  function wheelTarget(start, deltaY) {
    let el = start;
    while (el && !canScrollToward(el, deltaY)) {
      const outer = nearest(el.parentElement);
      if (!outer || outer === el) break;
      el = outer;
    }
    return el;
  }
  // 同源 iframe(会话里点开的网页/本地应用页):它的原生滚动条画在自己文档里,贴 iframe 右缘 =
  // 窗口右缘,会压住程序坞感应线的车道——根文档滚动和页内任意元素滚动(如 demo 页里 overflow
  // 的 div)都一样。收编进本体系:注入样式隐掉 iframe 内全部原生滚动条,scroll 事件从
  // contentWindow 捕获转发(目标是文档 → 根滑块,目标是元素 → 该元素单独一根滑块,几何叠加
  // iframe 偏移),滑块统一由宿主画。跨域 iframe 摸不到 contentDocument,跳过(Swift 壳里外链
  // 本来走原生面板,不经 iframe)。iframe 内导航会换文档、清掉监听与注入,轮询里每次重查重补。
  function hookFrames() {
    for (const f of document.querySelectorAll("iframe")) {
      let doc;
      try { doc = f.contentDocument; } catch { continue; }
      if (!doc || !doc.documentElement) continue;
      if (!doc.getElementById("sb-hide-native")) {
        const s = doc.createElement("style");
        s.id = "sb-hide-native";
        s.textContent = "*{scrollbar-width:none}*::-webkit-scrollbar{display:none}";
        (doc.head || doc.documentElement).appendChild(s);
      }
      const win = f.contentWindow;
      if (win && win._sbDocument !== doc) {
        win._sbDocument = doc;
        discover(doc.body, f); reveal(f);
        // 导航后绑定新 Document，不依赖跨导航仍保留的 WindowProxy 标志。
        doc.addEventListener("mouseover", (e) => reveal(nearest(e.target) || f, f));
        doc.addEventListener("focusin", (e) => reveal(nearest(e.target), f));
        doc.addEventListener("input", (e) => reveal(nearest(e.target), f));
        doc.addEventListener("wheel", (e) => reveal(nearest(e.target) || f, f), {passive: true});
        doc.addEventListener("scroll", (e) => {
          const tgt = e.target;
          if (!tgt || tgt.nodeType === 9 || tgt === win) reveal(f);
          else reveal(tgt, f);
        }, true);
      }
    }
  }
  document.addEventListener("scroll", (e) => {
    const el = e.target;
    if (scrollable(el)) reveal(el, null, true);
    reflowShownThumbs();
  }, true);
  document.addEventListener("wheel", (e) => {
    const inner = nearest(e.target);
    const target = wheelTarget(inner, e.deltaY);
    noteChatScroll(target);
    if (inner && target !== inner) {
      const stale = thumbs.get(inner);
      if (stale && !stale.classList.contains("dragging")) {
        stale.classList.remove("sb-show"); stale._x?.classList.remove("sb-show");
      }
    }
    reveal(target);
    reflowShownThumbs();
  }, { capture: true, passive: true });
  // 动态弹层、内联 overflow 和后续新增页面统一自动注册，避免维护名单继续漏掉入口。
  // 只扫描初始文档和新增子树；流式正文变动不逐帧遍历整个文档。
  function discover(root, frame) {
    if (root?.closest?.(".sb-thumb")) return;
    const candidates = [root, ...(root?.querySelectorAll?.("*") || [])];
    for (const key of candidates) if (scrollable(key) || thumbs.has(key)) {
      const t = thumbs.get(key);
      if (t) { if (!shown(key)) t._listHover = false; layout(t); }
      reveal(key, frame);
    }
  }
  discover(document.body);
  document.addEventListener("touchmove", (e) => noteChatScroll(nearest(e.target)), {capture: true, passive: true});
  document.addEventListener("keydown", (e) => {
    if (e.target?.closest?.("input, textarea, [contenteditable]")) return;
    if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(e.key)) noteChatScroll(nearest(e.target));
  }, {capture: true});
  document.addEventListener("focusin", (e) => reveal(nearest(e.target)));
  document.addEventListener("input", (e) => reveal(nearest(e.target)));
  if (typeof window !== "undefined") window.addEventListener("resize", reflowShownThumbs);
  const pendingRoots = new Set();
  let discoveryFrame = 0;
  if (typeof MutationObserver !== "undefined") {
    new MutationObserver((records) => {
      for (const record of records) {
        if (record.target.closest?.(".sb-thumb")) continue;
        const key = nearest(record.target);
        if (key) pendingRoots.add(key);
        if (record.type === "attributes") pendingRoots.add(record.target);
        else for (const node of record.addedNodes) if (node.nodeType === 1) pendingRoots.add(node);
      }
      if (!pendingRoots.size || discoveryFrame) return;
      discoveryFrame = setTimeout(() => {
        discoveryFrame = 0;
        const roots = [...pendingRoots]; pendingRoots.clear();
        for (const root of roots) {
          if (!root.isConnected) continue;
          // 已绑定的祖先只重排；新增节点和显隐切换才扫描子树。
          if (thumbs.has(root)) { layout(thumbs.get(root)); reveal(root); }
          else discover(root);
        }
      }, 16);
    }).observe(document.body, {subtree: true, childList: true, attributes: true,
                              attributeFilter: ["class", "style", "hidden", "open"]});
  }
  // 兜底轮询:容器尺寸/内容变化不一定伴随 scroll 事件,定期重算已绑定滑块的几何防止位置/高度漂移;
  // 顺带清扫容器已从 DOM 摘除的滑块(切页/关窗/会话被回收),不主动点亮,只矫正/回收
  setInterval(() => {
    for (const [key, t] of thumbs) {
      let gone;
      if (t._frame) {   // iframe 内部元素:iframe 没了或页内导航换了文档都算失效
        let doc = null;
        try { doc = t._frame.contentDocument; } catch {}
        gone = !document.contains(t._frame) || !doc || !doc.contains(key);
      } else {
        gone = !document.contains(key);
      }
      if (gone) { t._cancelDrag?.(); clearTimeout(t._hideT); t._x?.remove(); t.remove(); thumbs.delete(key); continue; }
      if (t.classList.contains("sb-show") || t.classList.contains("dragging") || t._x?.classList.contains("sb-show")) layout(t);
    }
    hookFrames();
  }, 400);
})();

// #proj-head 常驻在 #panel-list 之外,原来固定显示「工作流」;现在跟随 #panel-list 的滚动位置换成
// 当前正在看的分组标题(工作流/项目/聊天)。判定:项目组/聊天组各有一个 in-flow 组头(.loose-head,
// 在 #panel-list 内),一旦滚过 #panel-list 顶部就说明当前在看那一组;工作流组没有独立组头
// (它的"组头"就是 #proj-head 本体,永远在最前)。"+"和"折叠全部"两个按钮的语义(含 tooltip)跟着
// 换,避免标题换了、操作还对着原来那组——项目/聊天两态直接把点击转发给 ph/lh 里对应的真实按钮
// (renderSessionPanel 每次都会重建它们,这里现查不缓存),工作流态则恢复 app-rail 里绑好的原始处理。
(function stickyProjHead() {
  const head = document.getElementById("proj-head");
  const addBtn = document.getElementById("proj-add");
  const foldBtn = document.getElementById("proj-fold-all");
  if (!head || !addBtn || !foldBtn) return;
  const label = head.querySelector("span");
  const origAdd = addBtn.onclick;
  const origFold = foldBtn.onclick;
  let mode = "work";
  // 按分组标记现查（渲染重建后引用会失效）；标题文字会随界面语言变化，不能用来识别分组。
  function looseHead(text) {   // text: "项目" | "聊天"
    const group = text === "聊天" ? "chat" : "project";
    return document.querySelector(`#panel-list .loose-head[data-group="${group}"]`);
  }
  function apply(next) {
    if (next === mode) return;
    mode = next;
    if (next === "work") {
      DeskI18n.bind(label, "nav.workflows");
      DeskI18n.bind(addBtn, "nav.newWorkflow", {}, "title"); addBtn.onclick = origAdd;
      DeskI18n.bind(foldBtn, "nav.collapse", {}, "title"); foldBtn.onclick = origFold;
    } else if (next === "proj") {
      label.textContent = "项目";
      addBtn.title = "打开代码仓…";
      addBtn.onclick = () => looseHead("项目")?.querySelector(".pg-add")?.click();
      foldBtn.title = "全部折叠";
      foldBtn.onclick = () => looseHead("项目")?.querySelector(".sec-fold:not(.sec-more)")?.click();
    } else {
      DeskI18n.bind(label, "nav.chat");
      DeskI18n.bind(addBtn, "nav.newChat", {}, "title");
      addBtn.onclick = () => openDraftChat();   // 先开前端空会话，第一条消息发送时才真正落后端
      DeskI18n.bind(foldBtn, "nav.fold", {}, "title");
      foldBtn.onclick = () => looseHead("聊天")?.querySelector(".sec-fold:not(.sec-more)")?.click();
    }
  }
  function sync() {
    const pl = document.getElementById("panel-list");
    if (!pl) return;
    const top = pl.getBoundingClientRect().top;
    const projH = looseHead("项目"), chatH = looseHead("聊天");
    let next = "work";
    if (projH && projH.getBoundingClientRect().top - top <= 2) next = "proj";
    if (chatH && chatH.getBoundingClientRect().top - top <= 2) next = "chat";
    apply(next);
  }
  document.getElementById("panel-list")?.addEventListener("scroll", sync, { passive: true });
  // 面板重渲(新建/删除会话、切工作流等)可能让当前滚动位置对应的分组变了,借一次 rAF 后重新判定
  const pl0 = document.getElementById("panel-list");
  if (pl0) new MutationObserver(() => requestAnimationFrame(sync)).observe(pl0, { childList: true });
  sync();
})();
