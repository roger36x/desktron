// 由 app-assets.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 5/6 片
function curDomainId() { return nav.type === "domain" ? nav.id : DESKTOP; }   // 终端归属的工作区
function _welcomeEngine(wantShepherd = typeof shepherdMode !== "undefined" && !!shepherdMode) {
  const e = typeof engineInfo !== "undefined" ? engineInfo : null;
  if (wantShepherd
      && e && ["claude", "codex", "pi"].includes(e.shepherd_engine)
      && (!e.available || e.available[e.shepherd_engine])) return e.shepherd_engine;
  if (!e || !e.available || e.available[curEngine]) return curEngine;
  return Object.keys(e.engines || {}).find((id) => e.available[id]) || curEngine;
}
function _welcomeHTML() {
  const e = typeof engineInfo !== "undefined" ? engineInfo : null;
  const baseEngine = _welcomeEngine();
  const meta = e && e.engines && e.engines[baseEngine];
  const shepherdOn = typeof shepherdMode !== "undefined" && !!shepherdMode;
  const label = shepherdOn ? "Shepherd" : ((meta && meta.label) || baseEngine || DeskI18n.t("engines.fallback"));
  const available = !e || !e.available || !!e.available[baseEngine];   // Shepherd 也经当前引擎执行
  const attachIcon = HS_ICONS.clip;   // 与会话输入区共用字形、16px 尺寸及描边。
  const sendIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M6.5 10.5 12 5l5.5 5.5"/></svg>';
  const alertIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4 3.8 19h16.4zM12 9v4M12 16h.01"/></svg>';
  const engineRows = e && e.engines ? Object.entries(e.engines)
    .filter(([id]) => !e.available || !!e.available[id])
    .map(([id, item]) => {
      const on = !shepherdOn && id === baseEngine;
      return `<button class="frw-engine-item${on ? " selected" : ""}" type="button" data-engine="${escapeHtml(id)}" role="option" aria-selected="${on ? "true" : "false"}"><span class="frw-engine-icon">${BRAND_ICONS[id] || ICONS[ENGINE_ICON[id]] || ""}</span><span>${escapeHtml(item.label || id)}</span><span class="frw-engine-check">${on ? RP_CHECK : ""}</span></button>`;
    }).join("") : "";
  // Shepherd 是联合引擎，不在 engines 表里；与侧栏引擎菜单一样单列在分隔线之后。
  const shepherdRow = engineRows
    ? `<div class="frw-engine-sep" role="separator"></div><button class="frw-engine-item${shepherdOn ? " selected" : ""}" type="button" data-engine="" data-shepherd="1" role="option" aria-selected="${shepherdOn ? "true" : "false"}"><span class="frw-engine-icon">${BRAND_ICONS.shepherd}</span><span>Shepherd</span><span class="frw-engine-check">${shepherdOn ? RP_CHECK : ""}</span></button>`
    : "";
  return `<div class="vibe-empty welcome-empty frw-welcome" data-engine-ready="${available ? "1" : "0"}">
    <div class="frw-frame">
      <h1 class="frw-title"><span>今天想让 Desk 做什么？</span></h1>
      ${available ? "" : `<div class="frw-notice">${alertIcon}<span>${escapeHtml(label)} 尚未完成配置，发送前请先检查设置。</span><button type="button">去设置</button></div>`}
      <div class="frw-composer">
        <textarea class="frw-input" rows="1" maxlength="20000" placeholder="写点什么..." aria-label="描述你想完成的事情"></textarea>
        <div class="frw-composer-row">
          <div class="frw-tools"><button class="frw-attach" type="button" aria-label="添加文件夹">${attachIcon}</button><label class="frw-destination"><select class="frw-domain" aria-label="选择聊天或工作流"><option value="">聊天</option>${(domains || []).filter((d) => d && d.id && d.id !== DESKTOP && !d.kind).map((d) => `<option value="${escapeHtml(d.id)}">${escapeHtml(d.name || d.id)}</option>`).join("")}</select></label><button class="frw-context" type="button" aria-label="移除文件夹" hidden></button></div>
          <button class="frw-engine" type="button" aria-label="选择引擎，当前为 ${escapeHtml(label)}" aria-haspopup="listbox" aria-expanded="false"><span class="frw-engine-icon">${shepherdOn ? BRAND_ICONS.shepherd : (BRAND_ICONS[baseEngine] || ICONS[ENGINE_ICON[baseEngine]] || "")}</span><span class="frw-engine-label">${escapeHtml(label)}</span></button>
          <button class="frw-send" type="button" aria-label="发送">${sendIcon}</button>
        </div>
        <div class="frw-engine-menu" role="listbox" aria-label="选择引擎" hidden>${engineRows ? engineRows + shepherdRow : '<div class="frw-picker-empty">还没有可用引擎</div>'}</div>
      </div>
      <div class="frw-error" role="alert" aria-live="polite"></div>
    </div>
  </div>`;
}

// Fixed welcome text; the template keeps Chinese so the page renders before binding.
function _welcomeI18n(root) {
  const bind = (selector, key, params = {}, attr = "text", zh) =>
    DeskI18n.bind(root.querySelector(selector), key, params, attr, zh);
  const engine = root.querySelector(".frw-engine-label")?.textContent || "";
  bind(".frw-title span", "welcome.title");
  bind(".frw-notice > span", "welcome.notConfigured", { engine });
  bind(".frw-notice button", "welcome.openSettings");
  bind(".frw-input", "composer.placeholder", {}, "placeholder", "写点什么...");
  bind(".frw-input", "welcome.inputLabel", {}, "aria-label");
  bind(".frw-attach", "welcome.addFolder", {}, "aria-label");
  bind(".frw-domain", "welcome.destination", {}, "aria-label");
  bind('.frw-domain option[value=""]', "nav.chat");
  bind(".frw-context", "welcome.removeFolder", {}, "aria-label");
  bind(".frw-engine", "welcome.engineCurrent", { name: engine }, "aria-label");
  bind(".frw-send", "composer.send", {}, "aria-label");
  bind(".frw-engine-menu", "welcome.chooseEngine", {}, "aria-label");
  bind(".frw-picker-empty", "welcome.noEngines");
}

function vibeWorkingHTML() {
  // 主区没有打开内容时只提供一条真实入口；首次配置继续由独立设置向导负责。
  return _welcomeHTML();
}

function _wireWelcome(host) {
  const root = host.querySelector(".frw-welcome");
  if (!root) return;
  const input = root.querySelector(".frw-input");
  const send = root.querySelector(".frw-send");
  const error = root.querySelector(".frw-error");
  const destination = root.querySelector(".frw-domain");
  const engineButton = root.querySelector(".frw-engine");
  const engineMenu = root.querySelector(".frw-engine-menu");

  const context = root.querySelector(".frw-context");
  _welcomeI18n(root);
  const state = { path: "", domain: "", engine: _welcomeEngine(),
                  shepherd: typeof shepherdMode !== "undefined" && !!shepherdMode, busy: false };
  const baseName = (path) => String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || DeskI18n.t("welcome.myProject");
  const resize = () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  };
  const engineReady = () => !engineInfo || !engineInfo.available || !!engineInfo.available[state.engine];
  const sync = () => {
    const ready = !!input.value.trim() && !state.busy && engineReady();
    send.classList.toggle("ready", ready);
    send.disabled = state.busy || !engineReady();
    destination.disabled = state.busy;
    engineButton.disabled = state.busy;
    root.querySelector(".frw-attach").disabled = state.busy;
    context.disabled = state.busy;
    context.hidden = !state.path;
    if (state.path) {
      context.innerHTML = `${ICONS.folder}<span></span><b aria-hidden="true">×</b>`;
      context.querySelector("span").textContent = baseName(state.path);
      DeskI18n.bind(context, "welcome.removeFolderPath", { path: state.path }, "title");
    }
  };
  const closeEngineMenu = () => {
    engineMenu.hidden = true;
    engineButton.setAttribute("aria-expanded", "false");
  };
  const chooseFolder = async () => {
    closeEngineMenu();
    if (state.busy) return;
    if (!state.domain) { error.textContent = DeskI18n.t("welcome.needDomain"); destination.focus(); return; }
    const domain = state.domain;
    const path = await pickFolder();
    if (!path || !root.isConnected || state.busy || state.domain !== domain) return;
    state.path = path; error.textContent = ""; sync(); input.focus();
  };
  const linkFolder = async (domain) => {
    const r = await api(`/workbench/domains/${encodeURIComponent(domain)}/repos`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ op: "add", path: state.path }), timeout: 60000 });
    if (!(r && r.ok)) throw new Error(DeskI18n.t("welcome.linkFailed", { error: (r && r.error) || DeskI18n.t("welcome.unknownError") }));
  };
  const submit = async () => {
    const text = input.value.trim();
    if (!text) {
      error.textContent = DeskI18n.t("welcome.needText");
      root.querySelector(".frw-composer").classList.remove("invalid");
      requestAnimationFrame(() => root.querySelector(".frw-composer")?.classList.add("invalid"));
      input.focus(); return;
    }
    if (state.busy) return;
    if (!engineReady()) { error.textContent = DeskI18n.t("welcome.needEngine"); sync(); return; }
    state.busy = true; error.textContent = ""; closeEngineMenu(); sync();
    try {
      const domain = state.domain || DESKTOP;
      if (state.path && state.domain) await linkFolder(domain);
      const session = await newHtmlSessionWithPrompt({ domain, engine: state.engine, shepherd_mode: state.shepherd }, text);
      if (!session) throw new Error(DeskI18n.t("welcome.notCreated"));
    } catch (e) {
      error.textContent = e && e.message ? e.message : DeskI18n.t("welcome.createFailed");
      state.busy = false; sync(); input.focus();
    }
  };
  input.oninput = () => { error.textContent = ""; root.querySelector(".frw-composer").classList.remove("invalid"); resize(); sync(); };
  input.onkeydown = (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); submit(); }
  };
  send.onclick = submit;
  root.querySelector(".frw-attach").onclick = chooseFolder;
  destination.onchange = () => {
    closeEngineMenu();
    state.domain = destination.value;
    state.path = "";
    error.textContent = ""; sync(); input.focus();
  };
  context.onclick = () => { state.path = ""; sync(); input.focus(); };
  engineButton.onclick = () => {
    engineMenu.hidden = !engineMenu.hidden;
    engineButton.setAttribute("aria-expanded", engineMenu.hidden ? "false" : "true");
  };
  engineMenu.querySelectorAll("[data-engine]").forEach((row) => {
    row.onclick = () => {
      if (state.busy) return;
      state.shepherd = row.dataset.shepherd === "1";
      state.engine = row.dataset.engine || state.engine || _welcomeEngine();
      if (state.shepherd) state.engine = _welcomeEngine(true);
      const meta = engineInfo && engineInfo.engines && engineInfo.engines[state.engine];
      engineButton.querySelector(".frw-engine-icon").innerHTML = state.shepherd
        ? BRAND_ICONS.shepherd : (BRAND_ICONS[state.engine] || ICONS[ENGINE_ICON[state.engine]] || "");
      const selectedLabel = state.shepherd ? "Shepherd" : ((meta && meta.label) || state.engine);
      engineButton.querySelector(".frw-engine-label").textContent = selectedLabel;
      DeskI18n.bind(engineButton, "welcome.engineCurrent", { name: selectedLabel }, "aria-label");
      const notice0 = root.querySelector(".frw-notice > span");
      if (notice0) DeskI18n.bind(notice0, "welcome.notConfigured", { engine: selectedLabel });
      const notice = root.querySelector(".frw-notice");
      if (notice) notice.hidden = !engineInfo || !engineInfo.available || !!engineInfo.available[state.engine];
      engineMenu.querySelectorAll("[data-engine]").forEach((item) => {
        const selected = item === row;
        item.classList.toggle("selected", selected);
        item.setAttribute("aria-selected", selected ? "true" : "false");
        item.querySelector(".frw-engine-check").innerHTML = selected ? RP_CHECK : "";
      });
      closeEngineMenu(); error.textContent = ""; sync(); input.focus();
    };
  });
  root.onkeydown = (event) => {
    if (event.key === "Escape" && !engineMenu.hidden) { event.preventDefault(); closeEngineMenu(); engineButton.focus(); }
  };
  const settings = root.querySelector(".frw-notice button");
  if (settings) settings.onclick = () => openSettings();
  resize(); sync(); setTimeout(() => { if (root.isConnected) input.focus(); }, 0);
}
function clearTerminalView() {   // 主区无终端可显示:隐藏所有 pane + 显示水印,但不销毁(保活,切回来直接显示)
  activeTermId = null; _term = null; _termWs = null; _termRo = null;
  try { localStorage.removeItem("cd_term"); } catch {}
  _showWatermark();
  // term-on 的语义是「终端在场」,手机端的附加键栏(Esc/Tab/方向键)只认它。showView 按视图模式
  // 打这个标,而空态是终端视图却没有终端——手机上就会在起始页底部凭空多出一条键栏。清空即摘标。
  document.body.classList.remove("term-on");
}

// 终端拖入文件:Tauri 拦截 OS 拖放(DOM drop 拿不到文件路径),走 Tauri 事件拿 paths。
// 落点在终端区且有活动会话时,把文件全路径(含特殊字符则 shell 引号)当作键入发给 PTY,尾随空格。
// 对齐系统终端(Terminal.app/iTerm)拖文件即贴路径的行为。
function _shellQuotePath(p) {
  if (/^[\w@%+=:,.\/-]+$/.test(p)) return p;
  return "'" + String(p).replace(/'/g, "'\\''") + "'";
}
function _handleFileDrop(paths, position) {
  if (!paths || !paths.length || !activeTermId) return;
  const host = document.getElementById("term-host");
  if (!host || host.offsetWidth === 0) return;
  const x = (position && position.x) || 0, y = (position && position.y) || 0;
  const el = document.elementFromPoint(x, y);
  if (!el || !host.contains(el)) return;
  const pane = _termPanes[activeTermId];
  if (!pane || !pane.ws || pane.ws.readyState !== 1) return;
  const text = paths.map(_shellQuotePath).join(" ") + " ";
  try { pane.ws.send((pane.enc || new TextEncoder()).encode(text)); pane.term.focus(); } catch {}
}
(function wireTermFileDrop() {
  const T = window.__TAURI__;
  if (!T) { console.warn("[desk] __TAURI__ unavailable, file-drop disabled"); return; }
  // 优先 event.listen:走通用事件总线,core:default 通常已覆盖
  if (T.event && T.event.listen) {
    T.event.listen("tauri://drag-drop", (ev) => {
      const p = ev && ev.payload;
      _handleFileDrop(p && p.paths, p && p.position);
    }).then(() => console.info("[desk] file-drop: listening via event.listen"))
      .catch((e) => console.warn("[desk] event.listen drag-drop failed:", e));
    return;
  }
  // 回退:webview.onDragDropEvent(需 core:webview 权限)
  if (T.webview && T.webview.getCurrentWebview) {
    try {
      const wv = T.webview.getCurrentWebview();
      if (wv && wv.onDragDropEvent) {
        wv.onDragDropEvent((ev) => {
          const p = ev && ev.payload;
          if (!p || p.type !== "drop") return;
          _handleFileDrop(p.paths, p.position);
        }).then(() => console.info("[desk] file-drop: listening via onDragDropEvent"))
          .catch((e) => console.warn("[desk] onDragDropEvent failed:", e));
        return;
      }
    } catch (e) { console.warn("[desk] getCurrentWebview failed:", e); }
  }
  console.warn("[desk] no file-drop API found on __TAURI__:", Object.keys(T));
})();
async function newTerminal(engine, domainId) {   // 在指定工作区(默认当前)开一段新会话
  // TUI 退场后只有 GUI 这一条路。函数名留着:任务行、欢迎页、侧栏「+」都在调它,
  // 而它们要的语义没变——「在这个工作区开一段新会话,并把主区切过去」。
  const useShepherd = typeof shepherdMode !== "undefined" && shepherdMode;
  return newHtmlSession({ domain: domainId || curDomainId(),
    engine: useShepherd && (!engine || engine === curEngine) ? hsNewSessionEngine(true) : (engine || curEngine) });
}

// ---- Store:卡片式应用市场,在主区 asset-view 里直接展示 ----
// ---- 右缘程序坞:自动隐藏 Dock ----
// 钉住列表落 localStorage(cd_dock_pins,存应用 uid);首次默认钉前 8 个已安装应用。
// 运行点 = 该应用有打开的页面(非 agentic);点运行中的切回其页面,未运行的直接启动。
function _dockPins() {
  try {
    const v = JSON.parse(localStorage.getItem("cd_dock_pins") || "null");
    if (Array.isArray(v)) return v;
  } catch {}
  return null;
}
function _dockSavePins(p) { try { localStorage.setItem("cd_dock_pins", JSON.stringify(p)); } catch {} }
function _dockAppKey(a) { return a.uid || a.id || a.label; }
// 全部应用,按 key 去重。程序坞按「钉住」筛,顶栏那三枚按「最近打开」筛,查的是同一张表。
function _allApps() {
  const all = [], seen = new Set();
  const push = (a, domainId) => {
    if (a.page === "lark-events") return;   // 不恢复已下线应用的旧注册或本机缓存。
    const key = _dockAppKey(a);
    if (seen.has(key)) return;
    seen.add(key);
    all.push({ a, key, domainId });
  };
  // 全局应用(targets=_desktop)先排:应用已不跟随工作流,只认 domains[].apps 会让它们钉不进程序坞
  for (const a of desktopApps || []) push(a, DESKTOP);
  for (const d of domains || []) for (const a of d.apps || []) push(a, d.id);
  return all;
}
function _dockApps() {
  const all = _allApps();
  let pins = _dockPins();
  if (!pins) { pins = all.slice(0, 8).map((x) => x.key); _dockSavePins(pins); }
  const byKey = new Map(all.map((x) => [x.key, x]));
  return pins.map((k) => byKey.get(k)).filter(Boolean);
}
function _dockRunningPage(x) {
  return pages.find((pg) => pg.app && pg.appid && pg.appid === (x.a.uid || x.a.id));
}
// ⌘1..8 = 程序坞上第 n 个应用,⌘9 = 最后一个(n 传 -1)。全局标签条撤掉之后「第 n 张标签」
// 没有了着落,而跨应用切换本来就归程序坞:钉住的次序就是这几个键的次序。
// 开着的切回去,没开的按它自己的入口开起来——与点坞上那枚图标同一条路。
// 返回 false = 这一档没有应用,调用方据此把按键交回给系统。
function dockActivateNth(n) {
  const items = _dockApps();
  if (!items.length) return false;
  const x = n < 0 ? items[items.length - 1] : items[n];
  if (!x) return false;
  const pg = _dockRunningPage(x);
  if (typeof hideAppSide === "function") hideAppSide();
  if (pg) showOverlay(pg); else _openDomainApp(x.a, x.domainId, "main");
  return true;
}
function initDock() {
  if (document.getElementById("app-dock")) return;
  const sensor = document.createElement("div"); sensor.id = "dock-sensor";
  const dock = document.createElement("div"); dock.id = "app-dock";
  document.body.append(sensor, dock);
  let hideT = null, dwellT = null;
  const openDock = () => { clearTimeout(hideT); _renderDock(dock); document.body.classList.add("dock-open"); };
  const closeDock = () => { hideT = setTimeout(() => document.body.classList.remove("dock-open"), 350); };
  // 驻留判定:在感应带停 150ms 才开,鼠标路过不误触
  sensor.onmouseenter = () => { clearTimeout(dwellT); dwellT = setTimeout(openDock, 150); };
  dock.onmouseenter = () => clearTimeout(hideT);
  dock.onmouseleave = closeDock;
  sensor.onmouseleave = (e) => { clearTimeout(dwellT); if (!dock.contains(e.relatedTarget)) closeDock(); };
  // 首次亮相:自动滑出 3s 配说明气泡,让用户知道右缘有程序坞;演过落标记不再演
  if (!localStorage.getItem("cd_dock_intro")) {
    setTimeout(() => {
      _renderDock(dock);
      const tip = document.createElement("div");
      tip.id = "dock-intro-tip";
      tip.textContent = DeskI18n.t("dock.intro");
      dock.prepend(tip);
      document.body.classList.add("dock-open");
      setTimeout(() => {
        document.body.classList.remove("dock-open");
        setTimeout(() => tip.remove(), 400);   // 等收起动画走完再摘气泡
      }, 3000);
      try { localStorage.setItem("cd_dock_intro", "1"); } catch {}
    }, 2500);   // 等首屏安顿好再亮相,不抢注意力
  }
  // 感应痕趋近显现:鼠标进右缘 30px 内才浮现(节流 120ms),平时不与滚动条抢视觉
  let nearT = 0;
  document.addEventListener("mousemove", (e) => {
    const now = Date.now();
    if (now - nearT < 120) return;
    nearT = now;
    document.body.classList.toggle("dock-near", window.innerWidth - e.clientX < 30);
  }, { passive: true });
}
function _renderDock(dock) {
  dock.innerHTML = "";
  const items = _dockApps();
  for (const x of items) {
    const it = document.createElement("div");
    it.className = "dk-item" + (_dockRunningPage(x) ? " running" : "");
    it.dataset.name = x.a.label;
    it.innerHTML = `<div class="dk-ic">${_appIconHTML(x.a.label, x.key, "", x.a.desc, x.a.icon)}</div><div class="dk-dot"></div>`;
    it.querySelector(".dk-ic").onclick = () => {
      document.body.classList.remove("dock-open");
      const pg = _dockRunningPage(x);
      if (typeof hideAppSide === "function") hideAppSide();
      if (pg) showOverlay(pg); else _openDomainApp(x.a, x.domainId, "main");
    };
    it.oncontextmenu = (ev) => {
      ev.preventDefault(); ev.stopPropagation();
      const run = _dockRunningPage(x);
      const items2 = [];
      if (run) items2.push({ label: DeskI18n.t("common.close"), fn: () => { closePage(run); _renderDock(dock); } });
      items2.push({ label: DeskI18n.t("dock.unpin"), fn: () => {
        _dockSavePins((_dockPins() || []).filter((k) => k !== x.key));
        _renderDock(dock);
      } });
      openCtxMenu(ev.clientX, ev.clientY, items2);
    };
    dock.appendChild(it);
  }
  if (!items.length) {
    const e = document.createElement("div"); e.className = "dk-empty"; e.textContent = "无钉住应用";
    dock.appendChild(e);
  }
  const sep = document.createElement("div"); sep.className = "dk-sep";
  const all = document.createElement("button"); all.className = "dk-all"; all.type = "button"; all.title = "全部应用";
  all.innerHTML = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>';
  all.onclick = () => { document.body.classList.remove("dock-open"); openStore("app"); };
  dock.append(sep, all);
}
function openStore(kind, target) {
  // 只剩应用页(官方工作区页已随包安装模型退役:工作流=本地主动创建的文件夹,不从模板/包导入)
  const { body, fresh } = openResourcePage("apps", "应用");
  if (!fresh && body.dataset.target === String(target || "") && body._catalogVersion === _appCatalogVersion()) return;
  body.dataset.target = String(target || "");
  body.className = "md store-host";
  _renderAppsPage(body, target);
}
function _appCatalogVersion() {
  return JSON.stringify([desktopApps, domains.map((d) => [d.id, d.name, d.apps])]);
}
function refreshResourceApps() {
  const body = _resourceBody("apps");
  if (body && _resourceIsOpen("apps")) _renderAppsPage(body, body.dataset.target || undefined);
}
// ---- 应用页:页头+创建下拉 / 搜索 / 已安装图标排 / 官方·本地 chips / 双列行式目录 ----
const _APP_COLORS = ["#d95550", "#4f7fd9", "#8b6cd9", "#46a878", "#d99a3d", "#4a9db8", "#3fa8a0", "#7fae4a", "#c95c7e", "#6f86a8"];
function _appColor(key) {
  let h = 0;
  for (const ch of String(key)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return _APP_COLORS[h % _APP_COLORS.length];
}
// 应用图标:按名称/描述的语义关键词配整块图标瓷贴(SVG 自带渐变底+白色实心图形,参考真应用图标质感);
// 匹配不上才回退"色块+首字"。渐变 id 按类别固定,同类复用同一 defs 无冲突。
// 瓷贴外形:超椭圆(squircle,贝塞尔近似)——直边到圆角曲率连续,贴苹果图标质感;CSS border-radius 是圆弧,做不出
const _AP_SQUIRCLE = "M14 0H34C43.4 0 48 4.6 48 14V34C48 43.4 43.4 48 34 48H14C4.6 48 0 43.4 0 34V14C0 4.6 4.6 0 14 0Z";
function _appTile(gid, c1, c2, glyph) {
  return `<svg viewBox="0 0 48 48"><defs><linearGradient id="apg-${gid}" x1="0" y1="0" x2="0" y2="1">` +
    `<stop offset="0" stop-color="${c1}"/><stop offset="1" stop-color="${c2}"/></linearGradient>` +
    `<clipPath id="apsq"><path d="${_AP_SQUIRCLE}"/></clipPath></defs>` +
    `<g clip-path="url(#apsq)"><rect width="48" height="48" fill="url(#apg-${gid})"/>${glyph}</g>` +
    `<path d="${_AP_SQUIRCLE}" fill="none" stroke="rgba(255,255,255,.22)" stroke-width="1"/></svg>`;
}
// 自带底色与轮廓的整图用这一版:原样画出来,不叠渐变底、不裁圆角、不描高光边——
// 那三样是给「只有一个字形」的图标补外壳的,这里外壳已经在图里,再套一层就是两个圆角
// 相切、边缘露毛刺。k 是整体缩放:各家图标留白宽窄不一,靠它把视觉体量拉齐(见 _APP_TILE_K)。
function _appTilePlain(gid, glyph, k = 1) {
  const g = k === 1 ? glyph
    : `<g transform="translate(12,12) scale(${k}) translate(-12,-12)">${glyph}</g>`;
  return `<svg viewBox="0 0 24 24"${gid === "shell" ? ' class="app-terminal-icon"' : gid === "ide" ? ' class="app-monaco-ide-icon"' : ""}>${g}</svg>`;
}
// 浏览器那枚整图。应用格与浏览器应用的新标签页共用同一份,不各存一遍。
const BROWSER_ART = '<image href="/icons/app-browser-original-5d5d9b828321.png" width="24" height="24"/>';
// 原创正方形终端图标：应用列表、程序坞与终端标签共用同一份素材。
const TERMINAL_ART = '<image href="/icons/app-terminal-3b65e9edf830.png" width="24" height="24"/>';
// 原创极简代码图标：底板沿用应用超椭圆轮廓，代码符号不变，不叠窗口、外框或阴影。
const MONACO_IDE_ART = '<image href="/icons/app-monaco-ide-original-00700ed9f4c1.png" width="24" height="24"/>';
// 已采纳的 TaskNote V3 与备忘录 V19：保留完整48×48图稿，不另套底板。
const _TASKNOTE_REVIEWED_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><defs><linearGradient id="apg-notepaper" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fbfaf8"/><stop offset="1" stop-color="#f1efe9"/></linearGradient><clipPath id="apsq"><path d="M14 0H34C43.4 0 48 4.6 48 14V34C48 43.4 43.4 48 34 48H14C4.6 48 0 43.4 0 34V14C0 4.6 4.6 0 14 0Z"/></clipPath></defs><g clip-path="url(#apsq)"><rect width="48" height="48" fill="url(#apg-notepaper)"/><g stroke="#c9c5bc" stroke-width="2.6" stroke-linecap="round"><line x1="20" y1="14" x2="38" y2="14"/><line x1="20" y1="24" x2="38" y2="24"/><line x1="20" y1="34" x2="38" y2="34"/></g><g fill="none" stroke="#ef5350" stroke-width="2.7" stroke-linecap="round" stroke-linejoin="round"><g transform="translate(12.5 14) scale(.85) translate(-12.5 -14)"><path d="M8.6 14l2.6 2.7 5.1-5.5"/></g><g transform="translate(12.5 24) scale(.85) translate(-12.5 -24)"><path d="M8.6 24l2.6 2.7 5.1-5.5"/></g><g transform="translate(12.5 34) scale(.85) translate(-12.5 -34)"><path d="M8.6 34l2.6 2.7 5.1-5.5"/></g></g></g><path d="M14 0H34C43.4 0 48 4.6 48 14V34C48 43.4 43.4 48 34 48H14C4.6 48 0 43.4 0 34V14C0 4.6 4.6 0 14 0Z" fill="none" stroke="rgba(255,255,255,.22)" stroke-width="1"/></svg>';
const _NOTE_REVIEWED_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" role="img" aria-label="无横线上橙黄下暖白纸色暖浅灰折角备忘录">
<defs>
<linearGradient id="paper" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffd166"/><stop offset=".333333" stop-color="#ffd166"/><stop offset=".333333" stop-color="#f8f6f1"/><stop offset="1" stop-color="#e9e6de"/></linearGradient>
<linearGradient id="fold" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#dedad2"/><stop offset="1" stop-color="#cec8bd"/></linearGradient>
</defs>
<path d="M14 0H34C43.4 0 48 4.6 48 14V30Q48 32 46.6 33.4L33.4 46.6Q32 48 30 48H14C4.6 48 0 43.4 0 34V14C0 4.6 4.6 0 14 0Z" fill="url(#paper)"/>
<path d="M30 48Q32 48 32 46V38Q32 32 38 32H46Q48 32 48 30Q48 32 46.6 33.4L33.4 46.6Q32 48 30 48Z" fill="url(#fold)"/>
</svg>`;
const _APP_GLYPHS = [
  // 浏览器：中央地球采用用户提供的单赤道与椭圆经线图稿，保持白色。
  // 沿用当前主紫色 #5e59e2；PNG 外缘采用 _AP_SQUIRCLE 同款超椭圆，桌面与手机共用，不加底板。
  // 素材可见轮廓为480/512，按16/15补偿透明留白，使外框与其他应用等大。
  // 放在文件浏览关键词之前，应用列表、程序坞和最近应用共用同一份图。
  [/浏览器|browser/i, _appTilePlain("browser", BROWSER_ART, 16 / 15)],
  // 素材自带正方形底板、柔光边缘与白色 >_，不再叠加瓷贴或染色。
  [/终端|terminal|shell/i, _appTilePlain("shell", TERMINAL_ART)],
  // 放在 IDE 与泛 note 匹配之前，保持 TaskNote 及旧名称的匹配优先级。
  [/tasknote|notepaper|taskpaper|任务纸|待办清单/i, _TASKNOTE_REVIEWED_SVG],
  // Monaco IDE 使用已确认的极简代码图标；保留既有名称与关键词匹配。
  [/monaco|代码编辑|编辑器|(^|[^a-z])(code|ide)([^a-z]|$)|文件浏览|explorer/i,
   _appTilePlain("ide", MONACO_IDE_ART)],
  // 品牌图标(有官方 logo 的应用):直接贴官方图,不自己描 SVG。
  // Excalidraw 官方 PNG 为 180px 白底，中央图形占 26–154；放大使图形宽 40/48，
  // 白底铺满瓷贴，由超椭圆裁出圆角，四角不裁到图形。PNG 字节不改。
  [/excalidraw/i, _appTile("excalidraw", "#ffffff", "#ffffff",
    '<image href="/app-assets/excalidraw/icon.png" x="-4.125" y="-4.125" width="56.25" height="56.25"/>')],
  [/便笺|笔记|便签|备忘|note/i, _NOTE_REVIEWED_SVG],
  [/指标|面板|看板|统计|分析|测算|容量|dashboard|metric|report/i, _appTile("metric", "#4f90dc", "#2961bd",
    '<svg x="8" y="8" width="32" height="32" viewBox="0 0 32 32" data-desk-scattered="45" aria-hidden="true"><g fill="none" stroke="#fff" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 3.5v21c0 2.3 1.3 3.5 3.5 3.5h20M11 21v-7M18 21V9M25 21V4"/></g></svg>')],
];
// icon 是应用自己声明的图标。填成站内图片路径时直接用它,不再按名字猜——终端这类有既定
// 形象的应用,一眼认出来比按首字母配色有用。只认站内绝对路径,不认外链:图标随 payload
// 发行,不该在启动时依赖网络。
function _appIconHTML(label, key, extra, desc, icon) {
  const ic = String(icon || "");
  if (/^\/[\w./-]+\.(png|jpe?g|svg|webp)$/i.test(ic)) {
    // 声明的图片已有自己的造型，原样展示，不额外补底板或缩小图形。
    return `<div class="ap-ic img${extra ? " " + extra : ""}"><img src="${escapeHtml(ic)}" alt=""></div>`;
  }
  const hay = `${label} ${desc || ""} ${key || ""}`;
  for (const [re, tile] of _APP_GLYPHS) {
    if (re.test(hay)) return `<div class="ap-ic tile${extra ? " " + extra : ""}">${tile}</div>`;
  }
  const g = escapeHtml((String(label).trim()[0] || "·").toUpperCase());
  return `<div class="ap-ic${extra ? " " + extra : ""}" style="background-color:${_appColor(key || label)}">${g}</div>`;
}
// 最近打开过的应用。与程序坞那份「钉住」不是一回事:钉住是「我常用」,这里是「我刚用过」,
// 顶栏只摆几枚,够顺手回到刚才那个应用即可。记在唯一的打开入口上,各处入口都会经过这里。
const _APP_RECENT_MAX = 8;
const _APP_RECENT_SHOW = 4;      // 顶栏摆几枚
function _appRecent() {
  try {
    const v = JSON.parse(localStorage.getItem("cd_app_recent") || "[]");
    return Array.isArray(v) ? v : [];
  } catch { return []; }
}
function _appRecentPush(key) {
  if (!key) return;
  const list = [key, ..._appRecent().filter((k) => k !== key)].slice(0, _APP_RECENT_MAX);
  try { localStorage.setItem("cd_app_recent", JSON.stringify(list)); } catch {}
  renderHeadRecent();
}
// 顶栏右侧那几枚:最近打开过的应用,点一下回到它。已经开着的切过去,没开的现开——
// 与程序坞同一套判据(_dockRunningPage),免得同一个应用被开成两页。
//
// 只给会话页。应用页自己的页头上摆一排别的应用,读起来像是这个应用的功能;而这一排的用处
// 是「看会话时顺手回到刚用过的那个应用」,它属于会话那一侧。判据取 #overlay 的 hs-head——
// showOverlay 就是按「这一页是不是会话页」切的这个类,不另立一套。
function renderHeadRecent() {
  const host = document.getElementById("ov-recent");
  if (!host) return;
  const ov = document.getElementById("overlay");
  if (!ov || !ov.classList.contains("hs-head")) { host.innerHTML = ""; host.hidden = true; return; }
  const byKey = new Map(_allApps().map((x) => [x.key, x]));
  const rows = _appRecent().map((k) => byKey.get(k)).filter(Boolean).slice(0, _APP_RECENT_SHOW);
  // 不足数就拿程序坞钉住的那几个补齐(默认即前几个应用)。刚用过的排在前面,后面是常用的。
  // 只按「最近」摆的话,头一次用是空的、用过一次是孤零零一枚——而这一排的价值恰恰在这时候。
  const has = new Set(rows.map((x) => x.key));
  for (const x of _dockApps()) {
    if (rows.length >= _APP_RECENT_SHOW) break;
    if (has.has(x.key)) continue;
    has.add(x.key);
    rows.push(x);
  }
  host.innerHTML = "";
  host.hidden = !rows.length;
  for (const x of rows) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ov-rc";
    b.title = x.a.label;
    b.innerHTML = _appIconHTML(x.a.label, x.key, "", x.a.desc, x.a.icon);
    b.onclick = () => {
      const pg = _dockRunningPage(x);
      if (pg) showAppSide(pg); else _openDomainApp(x.a, x.domainId, "side");
    };
    host.appendChild(b);
  }
}
function _openDomainApp(a, domainId, placement = "main") {
  if (a.page === "lark-events") { toast(DeskI18n.tx("此应用已下线")); return null; }
  closeResourcePage(false);
  const compact = placement === "side" && !a.agentic;
  if (!compact && typeof hideAppSide === "function") hideAppSide();
  _appRecentPush(_dockAppKey(a));
  let opened = null;
  // 终端:后端起一个真的 PTY,前端用 xterm 挂上去,不是一张 HTML 页。少了这一支就会当成
  // 普通网页应用去找 pages/shell.html——那个文件从来不存在,于是每次打开都是一句「还没有
  // 此页」(2026-08-13 用户实见)。
  // 多个 shell 并列成这个应用的内部标签(app-terminal.js),所以点应用是「打开终端」,
  // 已经开着就切回去;再要一个 shell 走标签条上的「+」。
  if (a.page === "shell") opened = openTerminalApp({ title: a.label, show: !compact });
  // 浏览器:网页在这个应用内部并列成标签,地址栏与前进后退归它自己(app-browser.js)。
  // 与终端同理,它不是一张 HTML 页,没有 pages/browser.html 可加载。
  else if (a.page === "browser") opened = openBrowserApp(!compact);
  // 文件浏览器是只读单例：从应用面板打开时落到当前工作流；从对话链接打开时由
  // openFileBrowser 直接把 root/path 交给同一页。不要按普通 iframe 应用重复开页。
  else if (a.page === "file-browser") opened = openFileBrowserApp(!compact, domainId);
  // Monaco IDE 有自己的“已开目录”标签与延迟加载宿主，不能按普通 iframe 应用另开一份。
  else if (a.page === "files") opened = openFilesApp(!compact, domainId);
  // glass:这一页自带半透明底,要让浮层放行窗口的 vibrancy(见 style.css 的 #overlay.glass)。
  // 由应用自己声明——大多数页面是不透明的,浮层默认给它们一层实底,免得桌面壁纸透上来晃眼。
  else if (!a.agentic) opened = openPage(pageUrl(a, domainId), a.label,
                                         { app: true, appid: a.uid || a.id, glass: !!a.glass,
                                           chromeless: !!a.chromeless }, !compact);
  else opened = openAgenticApp(a, domainId);
  if (compact) {
    Promise.resolve(opened).then((value) => {
      const p = value && value.app && value.el ? value : _dockRunningPage({ a });
      if (p) showAppSide(p);
    });
  }
  return opened;
}
async function _renderAppsPage(host, target) {
  host._catalogVersion = _appCatalogVersion();
  const scroll = _resourceScroll("apps"), top = scroll?.scrollTop || 0;
  const installedApps = [], seen = new Set();
  const push = (a, domainId) => {
    if (a.page === "lark-events") return;
    const key = a.uid || a.id || a.label;
    if (seen.has(key)) return;
    seen.add(key); installedApps.push({ a, domainId });
  };
  for (const a of desktopApps || []) push(a, DESKTOP);
  for (const d of domains || []) for (const a of d.apps || []) push(a, d.id);
  const st = host._appState || (host._appState = { q: "", pool: "all" });
  const titles = { all: "全部应用", official: "官方应用", local: "本地应用" };
  const actions = '<div class="resource-head-actions"><button class="ap-create" type="button"><span>创建</span>'
    + '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></button>'
    + '<div class="ap-create-menu" hidden><div class="ap-cm-row" data-act="vibe"><span><span class="ap-cm-l">用会话生成</span><div class="ap-cm-d">描述用途，由 Desk 帮你创建</div></span></div>'
    + '<div class="ap-cm-row" data-act="git"><span><span class="ap-cm-l">从 Git 安装</span><div class="ap-cm-d">通过仓库地址安装应用</div></span></div></div></div>';
  host.innerHTML = '<div class="apps-page">'
    + _resourceHeader(titles[st.pool], "打开已安装的应用，或创建自己的常用工具。", actions)
    + '<label class="ap-search"><input class="ap-q" aria-label="搜索应用" placeholder="搜索应用" spellcheck="false" autocomplete="off"></label>'
    + '<div class="ap-cats"></div></div>';
  DeskI18n.bind(host.querySelector(".wf-h1"), "apps.title." + st.pool);
  DeskI18n.bind(host.querySelector(".resource-view-head p"), "apps.description");
  DeskI18n.bind(host.querySelector(".ap-create > span"), "apps.create");
  for (const act of ["vibe", "git"]) {
    DeskI18n.bind(host.querySelector(`[data-act="${act}"] .ap-cm-l`), "apps." + act);
    DeskI18n.bind(host.querySelector(`[data-act="${act}"] .ap-cm-d`), "apps." + act + "Hint");
  }
  DeskI18n.bind(host.querySelector(".ap-q"), "apps.search", {}, "placeholder");
  DeskI18n.bind(host.querySelector(".ap-q"), "apps.search", {}, "aria-label");
  const q = host.querySelector(".ap-q"); q.value = st.q;
  const render = () => {
    const kw = q.value.trim().toLowerCase();
    const cats = host.querySelector(".ap-cats"); cats.innerHTML = "";
    for (const [pool, label] of [["official", "官方"], ["local", "本地"]]) {
      if (st.pool !== "all" && st.pool !== pool) continue;
      const rows = installedApps.filter(({ a }) => (a._source === "official" ? "official" : "local") === pool
        && (!kw || (String(a.label || "") + " " + String(a.desc || "")).toLowerCase().includes(kw)));
      if (!rows.length) continue;
      const sec = document.createElement("section"); sec.className = "ap-cat";
      const heading = document.createElement("div"); heading.className = "ap-cat-title";
      const name = document.createElement("span"); DeskI18n.bind(name, "resource." + pool, {}, "text", label);
      heading.append(name, " · " + rows.length);
      const list = document.createElement("div"); list.className = "ap-rows";
      sec.append(heading, list);
      for (const { a, domainId } of rows) {
        const row = document.createElement("div"); row.className = "ap-row";
        row.innerHTML = _appIconHTML(a.label, a.uid || a.id || a.label, "sm", a.desc, a.icon)
          + '<div class="ap-rinfo"><div class="ap-rname">' + escapeHtml(a.label)
          + '</div><div class="ap-rdesc">' + escapeHtml(a.desc || "") + '</div></div>';
        const act = document.createElement("button"); act.type = "button";
        act.className = "ap-act"; DeskI18n.bind(act, "apps.open");
        act.onclick = () => _openDomainApp(a, domainId);
        // 从旧图标排迁入列表，保留右键钉住/取消钉住的能力。
        row.oncontextmenu = (ev) => {
          ev.preventDefault(); ev.stopPropagation();
          const key = _dockAppKey(a), pins = _dockPins() || [], on = pins.includes(key);
          openCtxMenu(ev.clientX, ev.clientY, [{ label: DeskI18n.t(on ? "apps.unpin" : "apps.pin"),
            fn: () => _dockSavePins(on ? pins.filter((k) => k !== key) : [...pins, key]) }]);
        };
        row.appendChild(act); list.appendChild(row);
      }
      cats.appendChild(sec);
    }
    if (!cats.childElementCount) {
      cats.innerHTML = '<div class="ap-empty"></div>';
      DeskI18n.bind(cats.firstChild, kw ? "apps.noMatch" : "apps.empty." + st.pool);
    }
    _resourceSyncSearch();
  };
  host._selectPool = (pool) => {
    if (!titles[pool] || st.pool === pool) return;
    st.pool = pool; DeskI18n.bind(host.querySelector(".wf-h1"), "apps.title." + pool);
    render(); if (scroll) scroll.scrollTop = 0; _resourcePaintNav();
  };
  q.oninput = () => { st.q = q.value; render(); };
  const cm = host.querySelector(".ap-create-menu");
  host.querySelector(".ap-create").onclick = (e) => { e.stopPropagation(); cm.hidden = !cm.hidden; };
  if (host._closeAppMenu) document.removeEventListener("click", host._closeAppMenu);
  host._closeAppMenu = () => { cm.hidden = true; };
  document.addEventListener("click", host._closeAppMenu);
  cm.querySelector('[data-act="vibe"]').onclick = () => { cm.hidden = true; openNewApp(target); };
  cm.querySelector('[data-act="git"]').onclick = () => { cm.hidden = true; installPackage(); };
  render(); _resourcePaintNav();
  if (scroll) scroll.scrollTop = top;
}
