// 由 app-settings.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 1/4 片
// desktron 前端(app.js 拆分):设置面板:逐引擎凭证 / 网关配置
// 经典脚本按序加载共享全局作用域,加载顺序见 index.html。勿单独重排。
// ---- 设置面板：凭证写本机密钥文件与各引擎自己的配置层，不碰 repo ----
// Engine status strings are generated in Chinese; only the known fixed ones are translated.
const _SM_ENGINE_SUMMARY_KEYS = {"订阅或 API key": "engines.summary.claude", "登录或 API key": "engines.summary.codex",
  "Provider 管理": "engines.summary.pi", "登录与配置": "engines.summary.default"};
const _SM_ENGINE_STATE_KEYS = {"订阅已登录": "engines.state.subscription", "使用 API key": "engines.state.usesKey",
  "状态未知": "engines.state.unknown", "未配置": "engines.state.unset", "已登录": "engines.state.signedIn",
  "已有可用 provider": "engines.state.providerReady", "未配置 provider": "engines.state.providerUnset"};
const _SM_ENGINE_ACTION_KEYS = {"配置": "engines.action.configure", "查看": "engines.action.view", "登录": "engines.action.signIn"};
function _smBindKnown(node, keys, zh) {
  if (node && keys[zh]) DeskI18n.bind(node, keys[zh], {}, "text", zh);
}

function openSettings() {
  // 设置是临时配置任务，不再替换主区导航。保留原页面在背后，关闭后会话、滚动位置与
  // 右侧应用都原样在场；配置表单和接口仍复用原来的真实节点。
  const modal = $("#settings-modal");
  const body = $("#sm-body");
  modal.hidden = false;
  document.body.classList.add("settings-dialog-open");
  body.className = "settings-center-live";
  body.innerHTML = '<div class="sm-page"><nav id="sm-nav" class="sm-inner-nav" aria-label="' + DeskI18n.txh("设置目录") + '"></nav>'
    + '<section class="sm-settings-content">'
    + '<div id="sm-settings-scroll"><div id="sm-host"><div class="sm-loading">' + DeskI18n.txh("加载中…") + '</div></div></div>'
    + '</section></div>';
  _renderSettingsNav();
  _wireSettingsSearch(body);
  // 目录与正文分开落地。正文某一节渲染失败时，主区目录仍可见并保持可理解的页面结构。
  const paintNav = () => {
    if (!document.getElementById("sm-host") || !document.getElementById("sm-nav")) return;
    _activateSettingsView(_smActiveView);
  };
  renderSettings().then(paintNav, (e) => {
    const host = document.getElementById("sm-host");
    if (host) host.innerHTML = `<div class="sm-loading">${DeskI18n.txh("设置渲染失败：{error}", { error: e && e.message || e })}</div>`;
    paintNav();
    console.error("renderSettings", e);   // 不吞:只剩一行提示的话,这类错误没有现场可查
  });
  $("#sm-settings-scroll").scrollTop = 0;
  const back = $("#sm-back");
  (back.getClientRects().length ? back : $("#sm-close")).focus();
}

let _smActiveView = "engines";
let _smSearchKeyHandler = null;
let _smEngineDialog = null;

function _closeSettingsEngineDialog() {
  const modal = _smEngineDialog;
  if (!modal) return;
  modal.hidden = true;
  document.body.classList.remove("sm-engine-dialog-open");
  if (modal._smOnKey) document.removeEventListener("keydown", modal._smOnKey);
  _smEngineDialog = null;
}

function _clearSettingsEngineDialogs() {
  _closeSettingsEngineDialog();
  document.querySelectorAll(".sm-engine-modal").forEach((modal) => modal.remove());
}

function _openSettingsEngineDialog(modal) {
  if (!modal) return;
  _closeSettingsEngineDialog();
  _smEngineDialog = modal;
  modal.hidden = false;
  document.body.classList.add("sm-engine-dialog-open");
  modal._smOnKey = (event) => { if (event.key === "Escape") _closeSettingsEngineDialog(); };
  document.addEventListener("keydown", modal._smOnKey);
  modal.querySelector(".sm-engine-dialog-close")?.focus();
}
const _SM_NAV_MODEL = [
  { items: [
    { id: "appearance", title: "外观与行为" },
  ] },
  { title: "模型与引擎", items: [{ id: "engines", title: "AI 引擎" }] },
  { title: "连接", items: [
    { id: "network", title: "网络与代理" },
    { id: "browser", title: "浏览器操控" },
    { id: "phone", title: "手机连接" },
    { id: "intranet", title: "工作工具" },
  ] },
  { title: "系统", items: [
    { id: "system-arrangements", title: "系统安排" },
    { id: "setup", title: "首次设置" },
    { id: "usage", title: "Token 用量" },
    { id: "stats", title: "任务统计" },
    { id: "debug", title: "调试" },
  ] },
];
const _SM_VIEW_COPY = {
  appearance: ["外观与行为", "调整当前设备的主题与开发选项。"],
  engines: ["AI 引擎", "查看可用状态；登录与高级选项在各引擎中配置。"],
  network: ["网络与代理", "配置本机的网络连接方式，统一浏览器、AI 引擎与安装下载。"],
  browser: ["浏览器操控", "查看内置浏览器能力、授权路径与站点权限。"],
  phone: ["手机连接", "通过可信局域网或 Tailscale，将手机连接到 Desk。"],
  intranet: ["工作工具", "管理 lark-cli 的登录状态。"],
  "system-arrangements": ["系统安排", "查看随 Desk 运行的后台任务及其状态。"],
  setup: ["首次设置", "检查运行环境与登录状态。"],
  usage: ["Token 用量", "从日期定位异常，再按工作流和会话追溯来源。"],
  stats: ["任务统计", "查看工作流的任务完成情况。"],
  debug: ["调试", "查看运行日志与诊断信息。"],
};

function _leaveSettings() {
  document.querySelectorAll('.sm-phone-section .sm-phone-cards').forEach((cards) => cards.replaceChildren());
  _clearSettingsEngineDialogs();
  $("#settings-modal").hidden = true;
  document.body.classList.remove("settings-dialog-open");
  if (_smSearchKeyHandler) {
    document.removeEventListener("keydown", _smSearchKeyHandler);
    _smSearchKeyHandler = null;
  }
}

// 设置页目录只保留可操作项。分组仍留在数据模型里供搜索与维护使用，视觉上不再插入
// 不可点击的二级标题，避免窄版面被标题和组间距拉得过长。
function _renderSettingsNav() {
  const el = $("#sm-nav");
  if (!el) return;
  el.innerHTML = "";
  const title = document.createElement("div");
  title.className = "sm-nav-title";
  title.textContent = "设置";
  DeskI18n.bind(title, 'nav.settings');
  el.innerHTML = '<div class="sm-settings-search"><span class="sm-settings-search-icon"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"></circle><path d="M20 20l-3.5-3.5"></path></svg></span>'
    + '<input id="sm-settings-search" type="search" placeholder="搜索设置" autocomplete="off" aria-label="搜索设置">'
    + '<kbd>' + (document.documentElement.classList.contains('desk-windows-shell') ? 'Ctrl K' : '⌘ K')
    + '</kbd><div class="sm-settings-search-results" hidden></div></div>';
  DeskI18n.bind(el.querySelector('#sm-settings-search'), 'settings.search', {}, 'placeholder');
  DeskI18n.bind(el.querySelector('#sm-settings-search'), 'settings.search', {}, 'aria-label');
  el.appendChild(title);
  const back = document.createElement("button");
  back.type = "button";
  back.id = "sm-back";
  back.setAttribute("aria-label", "返回，关闭设置");
  back.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="m10 3-5 5 5 5"/></svg><span>返回</span>';
  DeskI18n.bind(back, 'settings.close', {}, 'aria-label');
  DeskI18n.bind(back.querySelector('span'), 'settings.return');
  back.onclick = _leaveSettings;
  el.appendChild(back);
  for (const group of _SM_NAV_MODEL) {
    for (const item of group.items) {
      const row = document.createElement("button"); row.type = "button";
      row.className = "asset-item sm-nav-row";
      row.dataset.settingsView = item.id;
      row.innerHTML = `<span class="ai-title">${escapeHtml(item.title)}</span>`;
      DeskI18n.bind(row.querySelector('.ai-title'), 'settings.' + item.id, {}, 'text', item.title);
      row.onclick = () => _activateSettingsView(item.id);
      el.appendChild(row);
    }
  }
  const update = document.createElement("div");
  update.className = "sm-nav-update";
  update.innerHTML = '<button type="button" id="smp-update" aria-label="检查更新" title="检查更新">'
    + '<span class="sm-update-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M12 3v11m-4-4 4 4 4-4"/><path d="M5 15v4h14v-4"/></svg></span>'
    + '<small id="smp-update-status" role="status" aria-live="polite"></small></button>';
  DeskI18n.bind(update.querySelector('#smp-update'), 'settings.update', {}, 'title');
  DeskI18n.bind(update.querySelector('#smp-update'), 'settings.update', {}, 'aria-label');
  update.querySelector("#smp-update").onclick = () => openUpdateTUI();
  el.appendChild(update);
  _paintSettingsNav();
}

function _paintSettingsNav() {
  document.querySelectorAll("#sm-nav .sm-nav-row").forEach((row) => {
    row.classList.toggle("active", row.dataset.settingsView === _smActiveView);
  });
}

function _wireSettingsSearch(host) {
  const input = host.querySelector("#sm-settings-search");
  const results = host.querySelector(".sm-settings-search-results");
  const flat = _SM_NAV_MODEL.flatMap((group) => group.items);
  const close = () => { results.hidden = true; results.innerHTML = ""; };
  const open = () => {
    const q = input.value.trim().toLowerCase();
    if (!q) { close(); return; }
    const hits = flat.filter((item) => (item.title + ' ' + DeskI18n.t('settings.' + item.id)).toLowerCase().includes(q)).slice(0, 6);
    results.innerHTML = hits.length ? hits.map((item) =>
      `<button type="button" data-settings-result="${escapeHtml(item.id)}">${escapeHtml(DeskI18n.t('settings.' + item.id))}</button>`
    ).join("") : `<span>${escapeHtml(DeskI18n.t('settings.noMatch'))}</span>`;
    results.hidden = false;
  };
  input.oninput = open;
  input.onkeydown = (event) => {
    if (event.key === "Escape") { close(); input.blur(); }
    if (event.key === "Enter") {
      const first = results.querySelector("[data-settings-result]");
      if (first) { event.preventDefault(); _activateSettingsView(first.dataset.settingsResult); close(); }
    }
  };
  results.onclick = (event) => {
    const hit = event.target.closest("[data-settings-result]");
    if (!hit) return;
    _activateSettingsView(hit.dataset.settingsResult); close(); input.value = "";
  };
  input.onblur = () => setTimeout(close, 120);
  if (_smSearchKeyHandler) document.removeEventListener("keydown", _smSearchKeyHandler);
  _smSearchKeyHandler = (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k" && document.getElementById("sm-host")) {
      event.preventDefault(); input.focus(); input.select();
    }
  };
  document.addEventListener("keydown", _smSearchKeyHandler);
}
// 设置页按中文原文成批接入翻译（DeskI18n.tx）。切换语言时按缓存重绘整页并回到原位置；
// 未保存的输入会随重绘清空，与重新打开设置一致。
globalThis.addEventListener?.("desk-locale", () => {
  const modal = document.getElementById("settings-modal");
  if (!modal || modal.hidden || !document.getElementById("sm-host") || !_smCache) return;
  const top = document.getElementById("sm-settings-scroll")?.scrollTop || 0;
  const focusLanguage = document.activeElement?.matches?.("#sm-host [data-desk-language]");
  renderSettings().then(() => {
    const scroller = document.getElementById("sm-settings-scroll");
    if (scroller) scroller.scrollTop = top;
    if (focusLanguage) document.querySelector("#sm-host [data-desk-language]")?.focus();
  });
});
let _smCache = null;   // 设置数据缓存:再次打开直接渲染,不再闪"加载中";保存/登录后置空重取
// 启动后台预热设置缓存:后端逐引擎登录探测首访 2s+,预热后用户点开设置命中 _smCache 秒开
async function prefetchSettings() {
  if (_smCache) return;
  try {
    const s = await api("/workbench/settings", { timeout: 15000 });
    if (!_smCache) _smCache = { s };
  } catch {}
}
async function renderSettings() {
  const body = document.getElementById("sm-host") || $("#sm-body");
  let s;
  if (_smCache) {
    ({ s } = _smCache);   // 命中缓存:跳过请求与占位,直接渲染
  } else {
    body.innerHTML = '<div class="sm-loading">' + DeskI18n.txh("加载中…") + '</div>';
    try {
      s = await api("/workbench/settings", { timeout: 15000 });
    } catch (e) { body.innerHTML = `<div class="sm-loading">${DeskI18n.txh("读取失败：{error}", { error: e.message })}</div>`; return; }
    _smCache = { s };
  }
  applyTheme(s.ui && s.ui.theme);
  _clearSettingsEngineDialogs();
  body.innerHTML = "";
  // 分组:偏好 / AI 引擎 / 工具与连接 / 统计。11 个小节原来是写入顺序平铺,偏好、
  // 凭证、基础设施、统计四类混在一起,新功能都是往末尾接(2026-08-24 审阅发现)。
  // 分组只改容器,各小节内部的渲染逻辑一行没动;四个容器提前建好并立刻按目标顺序
  // 挂上 body,下面各小节该往哪个容器 appendChild,只是把原来的 body.appendChild(x)
  // 换成 g某某.appendChild(x)——容器此刻还是空的,填的时机不影响它在页面里的位置。
  const gPref = smGroup("偏好");
  const gEngine = smGroup("AI 引擎");
  const gTools = smGroup("工具与连接");
  const gStats = smGroup("统计");
  body.append(gPref, gEngine, gTools, gStats);
  const systemArrangements = smSection("系统安排", "记忆引擎跟随 Desk 运行。", ARR_SYS_ICONS.memory);
  _routeSettingsSection(systemArrangements, "system-arrangements");
  gTools.appendChild(systemArrangements);
  renderSystemArrangementsSettings(systemArrangements);
  const put = (payload, btn, after) => {
    if (btn) { btn.disabled = true; btn.textContent = DeskI18n.tx("保存中…"); }
    return api("/workbench/settings", { method: "PUT", headers: { "content-type": "application/json" },
                                        body: JSON.stringify(payload), timeout: 25000 })
      .then(() => { _smCache = null; if (after) after(); else renderSettings(); })   // 改完置空缓存,重取最新
      .catch((e) => { _smCache = null; alert(DeskI18n.tx("保存失败：{error}", { error: e.message })); renderSettings(); });
  };

  // ===== 外观:当前端的深浅偏好 =====
  // 这项刻意不走 /workbench/settings:桌面壳与各浏览器各自有独立 localStorage,
  // 因而可以分别固定为浅色或深色,不会由其中一端覆盖另外两端。
  const ap = smSection("外观", "仅当前设备，不随系统外观变化。", ICONS.moon);
  const apChoices = document.createElement("div"); apChoices.className = "sm-theme-list";
  const apCurrent = themePreference() || ((s.ui && s.ui.theme) === "light" ? "light" : "dark");
  [
    ["light", "浅色", "#f4f4f4"],
    ["dark", "深色", "#292929"],
  ].forEach(([id, label, swatch]) => {
    const choice = document.createElement("button"); choice.type = "button";
    choice.className = "sm-theme" + (id === apCurrent ? " on" : "");
    const dot = document.createElement("span"); dot.className = "sm-theme-dot";
    dot.style.setProperty("--swatch", swatch);
    const text = document.createElement("span"); text.textContent = label;
    DeskI18n.bind(text, 'theme.' + id, {}, 'text', label);
    choice.append(dot, text);
    choice.onclick = () => {
      setThemePreference(id);
      apChoices.querySelectorAll(".sm-theme").forEach((el) => el.classList.toggle("on", el === choice));
      applyTheme(s.ui && s.ui.theme);                           // 立即生效,不等重开
      refreshStandaloneThemeChrome();                           // iOS 主屏状态栏只在启动时取色
    };
    apChoices.appendChild(choice);
  });
  const apLanguage = DeskI18n.picker(); apLanguage.className = "sm-language-row";
  ap.appendChild(apLanguage);
  ap.appendChild(apChoices);
  _routeSettingsSection(ap, "appearance");
  gPref.appendChild(ap);

  // ===== claude code:订阅或 key =====
  const cl = smSection("Claude Code", "订阅登录或 API key，二选一。", BRAND_ICONS.claude);
  const st = document.createElement("div"); st.className = "sm-prov-head";
  // 查询失败不等于未配置；已确认的订阅或 key 仍优先显示。
  const stText = s.claude.subscription ? "订阅已登录" : (s.claude.api_key_set ? "使用 API key" : (s.claude.unknown ? "状态未知" : "未配置"));
  _routeSettingsSection(cl, "engines", {
    engine: "claude", summary: "订阅或 API key", state: stText,
    ready: s.claude.subscription || s.claude.api_key_set,
  });
  st.innerHTML = _pvState(s.claude.subscription || s.claude.api_key_set, stText);
  const loginBtn = document.createElement("button"); loginBtn.className = "sm-link-btn";
  loginBtn.textContent = s.claude.subscription ? DeskI18n.tx("重新登录") : DeskI18n.tx("登录订阅");
  loginBtn.onclick = () => put({ login: "claude" }, null, () => {});
  st.appendChild(loginBtn); cl.appendChild(st);

  const ckRow = document.createElement("div"); ckRow.className = "sm-row sm-prov-key";
  const ckf = keyField("ANTHROPIC_API_KEY（可选）", s.claude.api_key_mask, s.claude.api_key);
  const ckSave = document.createElement("button"); ckSave.className = "sm-save"; ckSave.textContent = DeskI18n.tx("保存");
  ckSave.onclick = (ev) => { const v = ckf.newKey(); if (v) put({ claude: { api_key: v } }, ev.target); };
  ckRow.append(ckf, ckSave);
  if (s.claude.api_key_set) {
    const ckDel = document.createElement("button"); ckDel.className = "sm-del"; ckDel.textContent = DeskI18n.tx("删");
    ckDel.onclick = () => { if (confirm(DeskI18n.tx("删除 {name}？", { name: "ANTHROPIC_API_KEY" }))) put({ claude: { api_key: null } }); };
    ckRow.appendChild(ckDel);
  }
  cl.appendChild(ckRow);
  const cbRow = document.createElement("div"); cbRow.className = "sm-row";
  cbRow.innerHTML = `<input class="sm-input cb-url" placeholder="${DeskI18n.txh("Anthropic 兼容网关（可选）")}" value="${escapeHtml(s.claude.base_url)}">
    <button class="sm-save cb-save">${DeskI18n.txh("保存")}</button>`;
  cbRow.querySelector(".cb-save").onclick = (ev) =>
    put({ claude: { base_url: cbRow.querySelector(".cb-url").value.trim() } }, ev.target);
  cl.appendChild(cbRow);
  // Desk 阅读主题:CC 自定义主题(柔化正文对比+舒适 diff 配色),开关写 ~/.claude(新会话生效)。
  // 异步回填;旧后端 404(路由需重启 Desk)时整行不出现。
  (async () => {
    let st2 = null;
    try { st2 = await api("/workbench/claude-theme", { timeout: 8000 }); } catch { return; }
    if (!st2 || !cl.isConnected) return;
    const row = document.createElement("div"); row.className = "sm-prov-head it-login-row";
    row.innerHTML = '<span class="it-name-cell"><span class="pv-name">' + DeskI18n.txh("Desk 阅读主题") + '</span>' +
      '<span class="pv-id-sub">' + DeskI18n.txh("新会话生效") + '</span></span>';
    const state = document.createElement("span");
    const paint = (on) => { state.className = "pv-state " + (on ? "on" : "off"); state.innerHTML = (on ? _PV_CHECK : "") + (on ? DeskI18n.txh("已启用") : DeskI18n.txh("未启用")); };
    paint(st2.enabled);
    const sw = document.createElement("button"); sw.type = "button";
    sw.className = "sm-switch" + (st2.enabled ? " on" : "");
    sw.onclick = async () => {
      const v = !sw.classList.contains("on");
      sw.disabled = true;
      try {
        await api("/workbench/claude-theme", { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ enable: v }) });
        sw.classList.toggle("on", v); paint(v);
      } catch (e) { alert(DeskI18n.tx("切换失败：{error}", { error: e.message })); }
      sw.disabled = false;
    };
    row.append(state, sw);
    cl.insertBefore(row, ckRow);   // 放登录态行之后、key 输入行之前
  })();
  gEngine.appendChild(cl);

  // ===== codex:与 claude 同构(本机登录 codex login 或 OPENAI_API_KEY) =====
  const cx = smSection("Codex", "本机登录或 API key，二选一。", BRAND_ICONS.codex);
  const cxst = document.createElement("div"); cxst.className = "sm-prov-head";
  const cxText = s.codex.logged_in ? "已登录" : (s.codex.api_key_set ? "使用 API key" : (s.codex.unknown ? "状态未知" : "未配置"));
  _routeSettingsSection(cx, "engines", {
    engine: "codex", summary: "登录或 API key", state: cxText,
    ready: s.codex.logged_in || s.codex.api_key_set,
  });
  cxst.innerHTML = _pvState(s.codex.logged_in || s.codex.api_key_set, cxText);
  const cxLogin = document.createElement("button"); cxLogin.className = "sm-link-btn";
  cxLogin.textContent = s.codex.logged_in ? DeskI18n.tx("重新登录") : DeskI18n.tx("登录");
  cxLogin.onclick = () => put({ login: "codex" }, null, () => {});
  cxst.appendChild(cxLogin); cx.appendChild(cxst);

  const xkRow = document.createElement("div"); xkRow.className = "sm-row sm-prov-key";
  const xkf = keyField("OPENAI_API_KEY（可选）", s.codex.api_key_mask, s.codex.api_key);
  const xkSave = document.createElement("button"); xkSave.className = "sm-save"; xkSave.textContent = DeskI18n.tx("保存");
  xkSave.onclick = (ev) => { const v = xkf.newKey(); if (v) put({ codex: { api_key: v } }, ev.target); };
  xkRow.append(xkf, xkSave);
  if (s.codex.api_key_set) {
    const xkDel = document.createElement("button"); xkDel.className = "sm-del"; xkDel.textContent = DeskI18n.tx("删");
    xkDel.onclick = () => { if (confirm(DeskI18n.tx("删除 {name}？", { name: "OPENAI_API_KEY" }))) put({ codex: { api_key: null } }); };
    xkRow.appendChild(xkDel);
  }
  cx.appendChild(xkRow);
  const xbRow = document.createElement("div"); xbRow.className = "sm-row";
  xbRow.innerHTML = `<input class="sm-input xb-url" placeholder="${DeskI18n.txh("OpenAI 兼容网关（可选）")}" value="${escapeHtml(s.codex.base_url)}">
    <button class="sm-save xb-save">${DeskI18n.txh("保存")}</button>`;
  xbRow.querySelector(".xb-save").onclick = (ev) =>
    put({ codex: { base_url: xbRow.querySelector(".xb-url").value.trim() } }, ev.target);
  cx.appendChild(xbRow);
  gEngine.appendChild(cx);

  // ===== Pi：凭证与 provider 均由 Pi 自己管理，Desk 只展示探测结果 =====
  if (s.pi) {
    const pi = smSection("Pi Coding", "凭证与 Provider 由 Pi 管理。", BRAND_ICONS.pi);
    const pist = document.createElement("div"); pist.className = "sm-prov-head";
    const piReady = !!s.pi.logged_in;
    const piText = s.pi.unknown ? "状态未知" : (piReady ? "已有可用 provider" : "未配置 provider");
    _routeSettingsSection(pi, "engines", {
      engine: "pi", summary: "Provider 管理", state: piText, ready: piReady,
    });
    pist.innerHTML = _pvState(piReady, piText);
    const piLogin = document.createElement("button"); piLogin.className = "sm-link-btn";
    piLogin.textContent = piReady ? DeskI18n.tx("管理凭证") : DeskI18n.tx("登录");
    piLogin.onclick = () => openLoginTUI("pi", DeskI18n.tx("Pi 登录 · 进入后输入 /login"));
    pist.appendChild(piLogin); pi.appendChild(pist);
    const providers = Array.isArray(s.pi.providers) ? s.pi.providers : [];
    const details = [];
    if (providers.length) details.push("Provider " + providers.join(DeskI18n.tx("、")));
    if (s.pi.current_model) details.push(DeskI18n.tx("当前模型 {model}", { model: s.pi.current_model }));
    if (Array.isArray(s.pi.models) && s.pi.models.length) details.push(DeskI18n.tx("{count} 个可用模型", { count: s.pi.models.length }));
    for (const detail of details) {
      const row = document.createElement("div"); row.className = "sm-prov-head";
      row.innerHTML = '<span class="sm-prov-line">' + escapeHtml(detail) + "</span>";
      pi.appendChild(row);
    }
    gEngine.appendChild(pi);
  }

  const network = smSection("网络与代理", "保存本机配置，不修改系统代理。", ICONS.browser);
  _routeSettingsSection(network, "network");
  gTools.appendChild(network);
  renderNetworkSection(network);

  // ===== Desk 内置浏览器操控：常驻能力，权限在每次使用的正确边界上确认 =====
  const brw = smSection("浏览器操控",
    "Agent 可直接使用隔离网页；登录态网页按站点授权，当前标签页由你主动交给 Agent。",
    ICONS.browser);
  _routeSettingsSection(brw, "browser");
  gTools.appendChild(brw);
  renderBrowserSection(brw);   // 异步填充：App Control 状态与授权（不进 _smCache）

  const phone = smSection("手机连接", "", ICONS.laptop);
  phone.classList.add("sm-phone-section");
  // 连接方式各用独立卡片，外层仅负责卡片间距。
  phone.querySelector(".sm-card").className = "sm-phone-cards";
  _routeSettingsSection(phone, "phone");
  gTools.appendChild(phone);

  // ===== 首次设置:下面几节的总入口。它按依赖顺序把「装 → 登录 → 远程」串起来,
  // 并显示当前该做哪一步;各节自己的操作仍在下面(见 app-settings-setup.js)。=====
  const su = smSection("首次设置", "", ICONS.laptop);
  _routeSettingsSection(su, "setup");
  {
    const row = document.createElement("div"); row.className = "sm-prov-head it-login-row";
    row.innerHTML = '<span class="it-name-cell"><span class="pv-name">' + DeskI18n.txh("设置向导") + '</span>'
      + '<span class="pv-id-sub">' + DeskI18n.txh("环境与登录检查") + '</span></span>';
    const pill = document.createElement("span"); pill.className = "pv-state off"; pill.textContent = DeskI18n.tx("检测中…");
    const b = document.createElement("button"); b.className = "sm-link-btn"; b.textContent = DeskI18n.tx("打开");
    b.onclick = () => openSetupGuide({});
    row.append(pill, b);
    su.appendChild(row);
    // 进度异步回填:这一条要问四处状态,不挡设置页其余部分渲染。旧后端没有这个端点就不显示。
    // 这一条同样不挤启动窗:它要问装机、登录、远程访问四处状态,本机实测 117~161ms,
    // 经隧道再加一次往返。设置页此刻多半还没被打开,晚一点回填看不出来。
    _idle(() => api("/workbench/setup", { timeout: 20000 }).then((st) => {
      if (!st || !st.total) return;
      pill.className = "pv-state " + (st.complete ? "on" : "off");
      pill.textContent = st.complete ? DeskI18n.tx("已就绪") : `${st.done} / ${st.total}`;
    }).catch(() => { pill.textContent = ""; }));
  }
  // smSection 只建节点不挂载(它重载了 appendChild 让内容落进卡内,但自己要调用方挂)。
  // 漏了这一行的后果是"代码全在、端点正常、页面上没有"——最难查的一类:
  // 从合包里 grep 得到、从接口取得到,唯独看不见(2026-09-01 用户实见)。
  gTools.appendChild(su);

  // ===== 工作工具登录:lark-cli。点按钮开预填命令的终端,回车执行,跟随浏览器/二维码完成 =====
  const it = smSection("登录状态", "", ICONS.lock);
  _routeSettingsSection(it, "intranet");
  const loginBtns = [
    ["lark", "lark-cli", DeskI18n.tx("飞书文档与消息"), "lark-cli auth login", DeskI18n.tx("lark-cli 登录 · auth login")],
  ];
  const itPills = {};
  for (const [key, name, sub, cmd, head] of loginBtns) {
    // 三列 grid 共享列线:名称 1fr / 状态定宽 / 按钮定宽(拉伸等宽),状态与按钮跨行对齐
    const row = document.createElement("div"); row.className = "sm-prov-head it-login-row";
    row.innerHTML = `<span class="it-name-cell"><span class="pv-name">${escapeHtml(name)}</span><span class="pv-id-sub">${escapeHtml(sub)}</span></span>`;
    const pill = document.createElement("span"); pill.className = "pv-state off"; pill.textContent = DeskI18n.tx("检测中…");
    const b = document.createElement("button"); b.className = "sm-link-btn"; b.textContent = DeskI18n.tx("登录");
    b.onclick = () => openLoginTUI(cmd, head);
    row.append(pill, b);
    itPills[key] = { pill, btn: b };
    it.appendChild(row);
  }
  // 登录态异步回填(接口不存在(旧后端)时静默降级为不显示状态)。
  // 后端只读缓存立刻回,真正去问 CLI 的活在它的后台线程里,可能要几十秒。
  // 所以第一次打开会拿到 pending,这里隔一会儿再取一次,取到结果为止,最多试几轮。
  let itTries = 0;
  const itPaint = (st2) => {
    let pending = false;
    for (const [key, { pill, btn }] of Object.entries(itPills)) {
      const r2 = (st2 || {})[key];
      if (!r2) { pill.remove(); continue; }
      if (r2.pending) {
        pending = true;
        pill.textContent = DeskI18n.tx("查询中…");
        pill.className = "pv-state off";
        continue;
      }
      if (!r2.installed) { pill.textContent = DeskI18n.tx("未安装"); pill.className = "pv-state off"; btn.disabled = true; continue; }
      // 钥匙串读不到:登着,但凭证取不出来。显示成「未登录」的话人会去点登录,
      // 而那条路修不好这件事——按钮要换成 lark-cli 自己给的那条修复命令。
      if (r2.keychain) {
        pill.textContent = DeskI18n.tx("钥匙串受阻");
        pill.className = "pv-state off";
        pill.title = r2.why || DeskI18n.tx("读不到 macOS 钥匙串");
        btn.textContent = DeskI18n.tx("修复");
        btn.onclick = () => openLoginTUI(r2.fix || "lark-cli config keychain-downgrade",
                                         DeskI18n.tx("解除钥匙串限制 · {command}", { command: r2.fix || "config keychain-downgrade" }));
        continue;
      }
      // 「查不到」单成一档,不并进「未登录」:状态查询超过后端上限时问不出结果。
      // 吞成未登录的话,登着也显示未登录,照着那枚「登录」钮去做是白跑一趟。
      if (r2.unknown) {
        pill.textContent = DeskI18n.tx("查不到");
        pill.className = "pv-state off";
        pill.title = r2.why || DeskI18n.tx("状态查询超时");
        btn.textContent = DeskI18n.tx("登录");
        continue;
      }
      pill.innerHTML = (r2.logged_in ? _PV_CHECK : "") + (r2.logged_in ? DeskI18n.txh("已登录") : DeskI18n.txh("未登录"));
      pill.className = "pv-state " + (r2.logged_in ? "on" : "off");
      btn.textContent = r2.logged_in ? DeskI18n.tx("重新登录") : DeskI18n.tx("登录");
    }
    // 还在查就再等一轮。上限 8 次 × 10 秒 = 80 秒;到点还没有就停,
    // 让「查询中」留在那儿——比反复空转好。
    if (pending && ++itTries < 8 && it.isConnected) setTimeout(itFetch, 10000);
  };
  const itFetch = () => api("/workbench/intranet-status", { timeout: 30000 })
    .then(itPaint)
    .catch(() => { for (const { pill } of Object.values(itPills)) pill.remove(); });
  itFetch();
  gTools.appendChild(it);

  // ===== Debug:开关在小节标题行右侧,开了右下角常驻 DEBUG 悬浮钮(app-logdock.js) =====
  const dbg = smSection("调试", "", ICONS.terminal);
  _routeSettingsSection(dbg, "debug");
  const dbgTitle = dbg.querySelector(".sm-title");
  dbgTitle.style.display = "flex"; dbgTitle.style.alignItems = "center";
  const sw = document.createElement("button"); sw.type = "button";
  sw.className = "sm-switch" + (s.ui && s.ui.log_overlay ? " on" : "");
  sw.title = DeskI18n.tx("日志悬浮窗"); sw.style.marginLeft = "auto";
  sw.onclick = () => {
    const v = !sw.classList.contains("on");
    sw.classList.toggle("on", v);
    if (window.LogDock) LogDock.setEnabled(v);   // 立即生效,不等重开
    wbState.ui = { ...(wbState.ui || {}), log_overlay: v };   // 同 theme:同步内存态防 saveState 整份回写覆盖
    put({ ui: { log_overlay: v } }, null, () => {});
  };
  dbgTitle.appendChild(sw);
  gPref.appendChild(dbg);
  // Desk 导航控制:与调试同版式的独立小节(标题行 + 开关),挂在调试之后,同属 debug 视图。
  // 不再塞进调试的卡片里;卡片空了会被 .sm-card:empty 隐藏。
  window.mountDeskControlSettings?.(gPref);
  // ===== Token 用量:本机 Provider usage 标量，不读取或上传会话正文 =====
  const usage = smSection("Token 用量", "按日期、工作流和会话查看本机模型用量。", ICONS.chart);
  _routeSettingsSection(usage, "usage");
  const usageBox = document.createElement("div");
  usage.appendChild(usageBox);
  gStats.appendChild(usage);
  if (typeof renderTokenUsageSettings === "function") renderTokenUsageSettings(usageBox);
  else usageBox.innerHTML = '<div class="sm-prov-line">' + DeskI18n.txh("统计不可用。") + '</div>';

  // ===== 任务统计:原来长在任务页顶上 =====
  // 挪到这里的理由:任务页要回答的是「现在有什么待办」,那几个数回答的是「最近干得怎么样」。
  // 后者是偶尔回看一次的东西,压在待办前面每次都要先翻过去。放在设置的最后一节。
  // 异步填:它要逐个工作流读任务清单,不该让设置页整体等它。
  const ts = smSection("任务统计", "", ICONS.chart);
  _routeSettingsSection(ts, "stats");
  const tsBox = document.createElement("div");
  tsBox.className = "sm-tasks";
  tsBox.innerHTML = '<div class="sm-prov-line">' + DeskI18n.txh("读取中…") + '</div>';
  ts.appendChild(tsBox);
  gStats.appendChild(ts);
  renderTaskStatsSection(tsBox);
  _finishSettingsCenter(body, s);
  refreshUpdateStatus();
}

function _routeSettingsSection(section, view, meta) {
  section.dataset.settingsView = view;
  if (!meta) return section;
  if (meta.engine) section.dataset.settingsEngine = meta.engine;
  if (meta.summary) section.dataset.settingsSummary = meta.summary;
  if (meta.state) section.dataset.settingsState = meta.state;
  section.dataset.settingsReady = meta.ready ? "true" : "false";
  return section;
}

function _finishSettingsCenter(host, settings) {
  const engineSections = [...host.querySelectorAll('.sm-section[data-settings-view="engines"]')];

  const viewHead = document.createElement("header");
  viewHead.className = "sm-view-head";
  viewHead.innerHTML = '<div><h1></h1><p></p></div>'
    + '<span class="sm-view-state"></span>';

  host.prepend(viewHead);

  const engineGroup = engineSections[0]?.closest(".sm-group");
  if (engineGroup) {
    const overview = document.createElement("section");
    overview.className = "sm-section sm-engine-overview";
    overview.dataset.settingsView = "engines";
    overview.innerHTML = '<div class="sm-engine-list-head"><h2>可用引擎</h2><p>选择一个引擎管理登录与高级选项。</p></div>';
    DeskI18n.bind(overview.querySelector("h2"), "engines.list");
    DeskI18n.bind(overview.querySelector("p"), "engines.listHint");
    const list = document.createElement("div");
    list.className = "sm-engine-list";
    for (const section of engineSections) {
      const title = section.querySelector(".sm-title")?.textContent.trim() || DeskI18n.tx("引擎");
      const icon = section.querySelector(".sm-title-ic")?.innerHTML || "";
      const state = section.dataset.settingsState || "状态未知";
      const summary = section.dataset.settingsSummary || "登录与配置";
      const ready = section.dataset.settingsReady === "true";
      const actionLabel = ready ? "配置" : state === "状态未知" ? "查看" : "登录";

      const row = document.createElement("button"); row.type = "button";
      row.className = "sm-engine-entry";
      row.dataset.settingsReady = ready ? "true" : "false";
      row.dataset.settingsEngine = section.dataset.settingsEngine;
      row.innerHTML = `<span class="sm-engine-entry-brand">${icon}</span>`
        + `<span class="sm-engine-entry-copy"><b>${escapeHtml(title)}</b><small>${escapeHtml(summary)}</small></span>`
        + `<span class="sm-engine-entry-state ${ready ? "ready" : ""}">${ready ? _PV_CHECK : ""}<span>${escapeHtml(state)}</span></span>`
        + `<span class="sm-engine-entry-action">${actionLabel}</span>`;
      _smBindKnown(row.querySelector(".sm-engine-entry-copy small"), _SM_ENGINE_SUMMARY_KEYS, summary);
      _smBindKnown(row.querySelector(".sm-engine-entry-state > span"), _SM_ENGINE_STATE_KEYS, state);
      _smBindKnown(row.querySelector(".sm-engine-entry-action"), _SM_ENGINE_ACTION_KEYS, actionLabel);

      const modal = document.createElement("div");
      modal.className = "nw-modal sm-engine-modal";
      modal.hidden = true;
      modal.innerHTML = `<div class="nw-backdrop"></div><section class="nw-box sm-engine-dialog" role="dialog" aria-modal="true" aria-label="${escapeHtml(title)}配置">`
        + `<header class="sm-engine-dialog-head"><span class="sm-engine-dialog-brand">${icon}</span>`
        + `<span class="sm-engine-dialog-copy"><h2>${escapeHtml(title)}</h2><p>${DeskI18n.txh(summary)} · ${DeskI18n.txh(state)}</p></span>`
        + '<button class="sm-engine-dialog-close" type="button" aria-label="关闭">×</button></header>'
        + '<div class="sm-engine-dialog-body"></div></section>';
      DeskI18n.bind(modal.querySelector(".sm-engine-dialog"), "engines.dialog", {name: title}, "aria-label", title + "配置");
      DeskI18n.bind(modal.querySelector(".sm-engine-dialog-close"), "common.close", {}, "aria-label");
      section.classList.add("sm-engine-config");
      modal.querySelector(".sm-engine-dialog-body").appendChild(section);
      modal.querySelector(".nw-backdrop").onclick = _closeSettingsEngineDialog;
      modal.querySelector(".sm-engine-dialog-close").onclick = _closeSettingsEngineDialog;
      document.body.appendChild(modal);
      row.onclick = () => _openSettingsEngineDialog(modal);
      list.appendChild(row);
    }
    overview.appendChild(list);
    engineGroup.appendChild(overview);
  }

  host.onclick = (event) => {
    const open = event.target.closest("[data-open-settings-view]");
    if (open) _activateSettingsView(open.dataset.openSettingsView);
  };
  _activateSettingsView(_smActiveView);
}

function _activateSettingsView(view) {
  _smActiveView = view || "engines";
  const host = document.getElementById("sm-host");
  _paintSettingsNav();
  if (!host) return;
  const copy = _SM_VIEW_COPY[_smActiveView] || ["设置", ""];
  const head = host.querySelector(".sm-view-head");
  if (head) {
    head.querySelector("h1").textContent = copy[0];
    head.querySelector("p").textContent = copy[1];
    DeskI18n.bind(head.querySelector('h1'), 'settings.' + _smActiveView, {}, 'text', copy[0]);
    DeskI18n.bind(head.querySelector('p'), 'settings.' + _smActiveView + '.description', {}, 'text', copy[1]);
    const state = head.querySelector(".sm-view-state");
    if (_smActiveView === "engines") {
      const entries = [...host.querySelectorAll(".sm-engine-entry")];
      DeskI18n.bind(state, 'settings.ready', {count: entries.filter((entry) => entry.dataset.settingsReady === "true").length});
    } else { state.removeAttribute('data-desk-i18n'); state.textContent = ""; }
  }

  host.querySelectorAll("[data-settings-view]").forEach((node) => {
    node.hidden = node.dataset.settingsView !== _smActiveView;
  });
  const phone = host.querySelector('.sm-phone-section');
  if (phone) {
    if (_smActiveView === "phone") renderPhoneConnectionSettings(phone);
    else phone.querySelector('.sm-phone-cards').replaceChildren();
  }
  host.querySelectorAll(".sm-group").forEach((group) => {
    group.hidden = !group.querySelector(".sm-section:not([hidden])");
  });
  const scroller = document.getElementById("sm-settings-scroll");
  if (scroller) scroller.scrollTop = 0;
}

let _phoneQrPromise = null;
function _phoneQrLibrary() {
  if (typeof window.qrcode === "function") return Promise.resolve(window.qrcode);
  if (_phoneQrPromise) return _phoneQrPromise;
  _phoneQrPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "/vendor/qrcode.js";
    let settled = false;
    const finish = (error) => {
      if (settled) return;
      settled = true; clearTimeout(timer);
      if (error) { _phoneQrPromise = null; script.remove(); reject(error); }
      else resolve(window.qrcode);
    };
    const timer = setTimeout(() => finish(new Error(DeskI18n.tx("二维码组件加载超时"))), 10000);
    script.onload = () => finish(typeof window.qrcode === "function"
      ? null : new Error(DeskI18n.tx("二维码组件不可用")));
    script.onerror = () => finish(new Error(DeskI18n.tx("二维码组件加载失败")));
    document.head.appendChild(script);
  });
  return _phoneQrPromise;
}

function _phoneSettingsError(card, title, message) {
  // The original file is kept; active pairing entries above stay usable.
  const panel = document.createElement('div');
  panel.className = 'sm-card sm-phone-connection-card';
  panel.innerHTML = '<b>' + title + '</b><p class="sm-prov-line" role="alert"></p>';
  panel.querySelector('p').textContent = message;
  card.prepend(panel);
}

function _phoneLanControls(card, config, section, error) {
  if (!config) { if (error) _phoneSettingsError(card, DeskI18n.txh('局域网连接设置'), error); return; }
  const panel = document.createElement('div');
  panel.className = 'sm-card sm-phone-connection-card sm-phone-lan-settings';
  panel.innerHTML = '<b>' + DeskI18n.txh('局域网连接设置') + '</b>'
    + '<label><input type="checkbox" class="sm-phone-lan-enabled"> ' + DeskI18n.txh('启用可信局域网入口') + '</label>'
    + '<label>' + DeskI18n.txh('本机 IPv4/前缀') + ' <input type="text" class="sm-input sm-phone-lan-interface" list="sm-phone-lan-interfaces" placeholder="192.168.1.10/24" spellcheck="false"></label>'
    + '<datalist id="sm-phone-lan-interfaces"></datalist>'
    + '<p class="sm-prov-line">' + DeskI18n.txh('仅监听所选私有网卡并限制来源网段，访问必须携带令牌。HTTP 不加密，只适用于可信 Wi-Fi；不要做公网端口映射。') + '</p>'
    + '<p class="sm-prov-line">' + DeskI18n.txh('保存后需重启 Desk；停用也需重启才能撤销监听。不会自动修改防火墙。入口实际生效后显示二维码。') + '</p>'
    + '<button type="button" class="sm-link-btn sm-phone-lan-save">' + DeskI18n.txh('保存局域网设置') + '</button> '
    + '<button type="button" class="sm-link-btn sm-phone-lan-refresh">' + DeskI18n.txh('刷新状态') + '</button>'
    + '<p class="sm-prov-line sm-phone-lan-feedback" role="status" aria-live="polite"></p>';
  card.prepend(panel);
  const enabled = panel.querySelector('.sm-phone-lan-enabled');
  const address = panel.querySelector('.sm-phone-lan-interface');
  const save = panel.querySelector('.sm-phone-lan-save');
  const feedback = panel.querySelector('.sm-phone-lan-feedback');
  for (const value of config.candidates || []) {
    const option = document.createElement('option'); option.value = value;
    panel.querySelector('datalist').appendChild(option);
  }
  const paint = () => {
    enabled.checked = config.enabled;
    address.value = config.interface || ((config.candidates || []).length === 1 ? config.candidates[0] : '');
    enabled.disabled = address.disabled = !config.managed;
    save.disabled = !config.managed || Boolean(section._phoneLanSaveUnknown);
    const legacy = DeskI18n.tx('此入口由 DESKTRON_REMOTE_BIND 管理；请先移除旧配置再使用此设置。');
    feedback.textContent = config.active_missing
      ? DeskI18n.tx('本机地址已变化，局域网入口仍在监听 {address}，手机无法连接。请更新地址并重启 Desk。', {address: config.active_interface})
        + (config.managed ? '' : ' ' + legacy)
      : !config.managed ? legacy
      : config.restart_required ? DeskI18n.tx('设置已保存，重启 Desk 后生效。')
      : config.active_interface ? DeskI18n.tx('局域网入口正在监听：{address}', {address: config.active_interface})
      : config.detection_error ? DeskI18n.tx(config.detection_error) : DeskI18n.tx('局域网入口尚未启用。');
  };
  paint();
  panel.querySelector('.sm-phone-lan-refresh').onclick = () => renderPhoneConnectionSettings(section);
  save.onclick = async () => {
    if (section._phoneSaving || section._phoneLanSaveUnknown) return;
    const nextInterface = address.value.trim();
    if (enabled.checked && !nextInterface) {
      feedback.textContent = DeskI18n.tx('请先选择本机私有 IPv4/前缀。'); return;
    }
    section._phoneSaving = true; save.disabled = true;
    let confirmed = false;
    try {
      const response = await fetch('/workbench/lan', {method: 'PUT', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'}, signal: AbortSignal.timeout(15000),
        body: JSON.stringify({config: {enabled: enabled.checked, interface: nextInterface}, expected_revision: config.revision})});
      const result = await response.json();
      confirmed = result.state !== 'unknown' && response.status < 500;
      if (!response.ok) throw new Error(result.error || DeskI18n.tx('连接设置保存失败。'));
      config = result; paint();
      feedback.textContent = result.restart_required ? DeskI18n.tx('设置已保存，重启 Desk 后生效。') : DeskI18n.tx('连接设置已保存。');
      if (!result.enabled) card.querySelector('[data-entry="lan"]')?.remove();
    } catch (error) {
      if (!confirmed) section._phoneLanSaveUnknown = true;
      feedback.textContent = !confirmed ? DeskI18n.tx('保存结果未知，请刷新状态核对后再操作。') : error.message;
    } finally {
      section._phoneSaving = false;
      save.disabled = !config.managed || Boolean(section._phoneLanSaveUnknown);
      _phoneResumeRender(section);
    }
  };
}

function _phoneTailscaleControls(card, config, section, error) {
  if (!config) { if (error) _phoneSettingsError(card, DeskI18n.txh('Tailscale 连接设置'), error); return; }
  const panel = document.createElement('div');
  panel.className = 'sm-card sm-phone-connection-card sm-phone-ts-settings';
  panel.innerHTML = '<b>' + DeskI18n.txh('Tailscale 连接设置') + '</b>'
    + '<label><input type="checkbox" class="sm-phone-ts-enabled"> ' + DeskI18n.txh('启用 Tailscale 专用入口') + '</label>'
    + '<label>' + DeskI18n.txh('本机转发端口') + ' <input type="number" class="sm-input sm-phone-ts-port" min="1024" max="65535" step="1"></label>'
    + '<p class="sm-prov-line">' + DeskI18n.txh('只监听本机环回地址；令牌自动生成且不显示。保存后需重启 Desk，不会自动修改 Tailscale 或开放公网。') + '</p>'
    + '<p class="sm-prov-line">' + DeskI18n.txh('电脑和手机需安装 Tailscale 并登录同一账号。入口生效后按下方命令配置 Serve，再刷新状态获取二维码。') + '</p>'
    + '<button type="button" class="sm-link-btn sm-phone-ts-save">' + DeskI18n.txh('保存连接设置') + '</button> '
    + '<button type="button" class="sm-link-btn sm-phone-ts-refresh">' + DeskI18n.txh('刷新状态') + '</button>'
    + '<p class="sm-prov-line sm-phone-ts-feedback" role="status" aria-live="polite"></p>';
  card.prepend(panel);
  const enabled = panel.querySelector('.sm-phone-ts-enabled');
  const port = panel.querySelector('.sm-phone-ts-port');
  const save = panel.querySelector('.sm-phone-ts-save');
  const feedback = panel.querySelector('.sm-phone-ts-feedback');
  const paint = () => {
    enabled.checked = config.enabled; port.value = config.port;
    enabled.disabled = port.disabled = !config.managed;
    save.disabled = !config.managed || Boolean(section._phoneSaveUnknown);
    feedback.textContent = !config.managed ? DeskI18n.tx('此入口由 DESKTRON_SERVE_PORT 管理；请先移除旧配置再使用此设置。')
      : config.restart_required ? DeskI18n.tx('设置已保存，重启 Desk 后生效。') : '';
  };
  paint();
  panel.querySelector('.sm-phone-ts-refresh').onclick = () => renderPhoneConnectionSettings(section);
  save.onclick = async () => {
    if (section._phoneSaving) return;
    const nextPort = Number(port.value);
    if (!Number.isInteger(nextPort) || nextPort < 1024 || nextPort > 65535) {
      feedback.textContent = DeskI18n.tx('请输入 1024–65535 的整数端口。'); return;
    }
    section._phoneSaving = true; save.disabled = true;
    let confirmed = false;
    try {
      const response = await fetch('/workbench/tailscale', {method: 'PUT', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'}, signal: AbortSignal.timeout(10000),
        body: JSON.stringify({config: {enabled: enabled.checked, port: nextPort}, expected_revision: config.revision})});
      const result = await response.json();
      confirmed = result.state !== 'unknown' && response.status < 500;
      if (!response.ok) throw new Error(result.error || DeskI18n.tx('连接设置保存失败。'));
      config = result; paint();
      feedback.textContent = result.restart_required ? DeskI18n.tx('设置已保存，重启 Desk 后生效。') : DeskI18n.tx('连接设置已保存。');
      if (!result.enabled) card.querySelector('[data-entry="tailscale"]')?.remove();
    } catch (error) {
      // A timeout is not permission to replay a write with an unknown outcome.
      if (!confirmed) section._phoneSaveUnknown = true;
      feedback.textContent = !confirmed
        ? DeskI18n.tx('保存结果未知，请刷新状态核对后再操作。') : error.message;
    } finally {
      section._phoneSaving = false;
      save.disabled = !config.managed || Boolean(section._phoneSaveUnknown);
      _phoneResumeRender(section);
    }
  };
}

function _phoneResumeRender(section) {
  if (!section._phoneRenderPending || section._phoneLoading || section._phoneSaving) return;
  section._phoneRenderPending = false;
  if (section.isConnected && !section.hidden && !document.getElementById('settings-modal')?.hidden) {
    void renderPhoneConnectionSettings(section);
  }
}

async function renderPhoneConnectionSettings(section) {
  if (section._phoneLoading || section._phoneSaving) {
    section._phoneRenderPending = true;
    return;
  }
  section._phoneLoading = true;
  const card = section.querySelector(".sm-phone-cards");
  card.innerHTML = '<div class="sm-card"><div class="sm-loading" role="status">' + DeskI18n.txh("检查连接状态…") + '</div></div>';
  const visible = () => section.isConnected && !section.hidden && !document.getElementById("settings-modal")?.hidden;
  try {
    const response = await fetch("/desk-lan-access/status", { cache: "no-store", credentials: "same-origin", signal: AbortSignal.timeout(20000) });
    if (!response.ok) throw new Error(DeskI18n.tx("连接状态读取失败（{status}）", { status: response.status }));
    const state = await response.json();
    if (!visible()) return;
    const ts = state.tailscale || null;
    section._phoneSaveUnknown = false;  // A fresh read resolves an uncertain save.
    section._phoneLanSaveUnknown = false;
    if (!state.local) {
      const via = location.protocol === "https:" && ts?.enabled ? DeskI18n.txh("已通过 Tailscale 连接。")
        : state.enabled ? DeskI18n.txh("已通过局域网连接。") : DeskI18n.txh("局域网入口未开放。");
      card.innerHTML = '<div class="sm-card"><div class="sm-prov-line" role="status">' + via
        + DeskI18n.txh("二维码和连接链接仅在电脑本机设置页显示。") + '</div></div>';
      return;
    }
    const entries = [];
    // A listener on an address this machine no longer owns yields a dead QR code.
    const lanStale = Boolean(state.lan_config?.active_missing);
    if (state.enabled && !lanStale) entries.push({ key: "lan", title: DeskI18n.txh("局域网连接"), address: state.address, url: state.url,
      note: DeskI18n.txh("当前使用 HTTP，仅适合可信局域网；更换网络后需重新配置。关闭入口需停用局域网监听并重启。") });
    if (ts?.url) entries.push({ key: "tailscale", title: DeskI18n.txh("Tailscale 连接（HTTPS）"), address: ts.address, url: ts.url,
      note: DeskI18n.txh("手机需安装 Tailscale 并登录同一账号。可在手机浏览器中添加到主屏幕。") });
    const tsHint = !ts ? "" : ts.url ? "" : ({
      missing: DeskI18n.tx("Tailscale 入口已开启，但本机未检测到 Tailscale。请安装并登录后，在终端执行："),
      offline: DeskI18n.tx("Tailscale 未登录或未运行。登录后在终端执行："),
      unconfigured: DeskI18n.tx("Tailscale Serve 尚未指向 Desk。请在终端执行："),
      error: DeskI18n.tx("Tailscale 状态读取失败。请先在本机检查 CLI，再刷新状态。"),
      public: DeskI18n.tx("检测到 Tailscale Funnel 公网入口。请先关闭 Funnel；Desk 不生成公网配对链接。"),
    }[ts.state] || DeskI18n.tx("Tailscale 入口状态未知。"));
    if (!entries.length && !tsHint) {
      card.innerHTML = '<div class="sm-card"><div class="sm-prov-line" role="status">' + (lanStale
        ? DeskI18n.txh("局域网入口监听的地址已不属于本机，二维码已停用。请按下方提示更新地址并重启 Desk。")
        : DeskI18n.txh("局域网入口未开放。请检查监听配置；配置变更后需要重启 Desk。")) + '</div></div>';
      _phoneTailscaleControls(card, state.tailscale_config, section, state.tailscale_config_error);
      _phoneLanControls(card, state.lan_config, section, state.lan_config_error);
      return;
    }
    card.innerHTML = entries.map((entry) => '<div class="sm-card sm-phone-connection-card" data-entry="' + entry.key + '"><div class="sm-phone-pairing">'
      + '<div class="sm-phone-qr" role="img" aria-label="' + (entry.key === "lan" ? DeskI18n.txh("手机连接二维码") : DeskI18n.txh("Tailscale 连接二维码")) + '"></div>'
      + '<div class="sm-phone-copy"><b>' + entry.title + '</b><p class="sm-prov-line">' + DeskI18n.txh("用手机相机扫描二维码，在浏览器中打开。") + '</p>'
      + '<p class="sm-phone-address"></p><button type="button" class="sm-link-btn sm-phone-copy-link">' + DeskI18n.txh("复制连接链接") + '</button>'
      + '<p class="sm-prov-line sm-phone-feedback" role="status" aria-live="polite"></p>'
      + '<p class="sm-prov-line">' + entry.note + '</p></div></div>'
      + '<p class="sm-prov-line">' + DeskI18n.txh("二维码和链接包含访问凭据，请勿公开。") + '</p></div>').join("")
      + (tsHint ? '<div class="sm-card sm-phone-connection-card"><b>' + DeskI18n.txh("Tailscale 连接（HTTPS）") + '</b>'
        + '<p class="sm-prov-line sm-phone-ts-hint" role="status"></p><p class="sm-phone-address sm-phone-ts-command"></p></div>' : "");
    _phoneTailscaleControls(card, state.tailscale_config, section, state.tailscale_config_error);
    _phoneLanControls(card, state.lan_config, section, state.lan_config_error);
    if (tsHint) {
      card.querySelector(".sm-phone-ts-hint").textContent = tsHint;
      card.querySelector(".sm-phone-ts-command").textContent = ts.command || "";
    }
    if (!entries.length) return;
    // 链接与复制先独立可用；二维码组件加载不能阻塞这条连接路径。
    for (const entry of entries) {
      const block = card.querySelector('[data-entry="' + entry.key + '"]');
      block.querySelector(".sm-phone-qr").textContent = DeskI18n.tx("正在生成二维码…");
      block.querySelector(".sm-phone-address").textContent = entry.address;
      const feedback = block.querySelector(".sm-phone-feedback");
      block.querySelector(".sm-phone-copy-link").onclick = async () => {
        try {
          await navigator.clipboard.writeText(entry.url);
          feedback.textContent = DeskI18n.tx("连接链接已复制。");
        } catch (_) {
          feedback.textContent = DeskI18n.tx("无法自动复制，请手动复制下面的链接。");
          let input = block.querySelector(".sm-phone-link");
          if (!input) {
            input = document.createElement("input"); input.className = "sm-input sm-phone-link";
            input.readOnly = true; input.setAttribute("aria-label", DeskI18n.tx("手机连接链接"));
            block.querySelector(".sm-phone-copy").appendChild(input);
          }
          input.value = entry.url; input.focus(); input.select();
        }
      };
    }
    let makeQr = null, qrError = null;
    try { makeQr = await _phoneQrLibrary(); } catch (error) { qrError = error; }
    if (!visible()) return;
    for (const entry of entries) {
      const block = card.querySelector('[data-entry="' + entry.key + '"]');
      if (!block) continue;  // A save may have removed this entry while QR loading was pending.
      const feedback = block.querySelector(".sm-phone-feedback");
      try {
        if (qrError) throw qrError;
        const qr = makeQr(0, "M"); qr.addData(entry.url); qr.make();
        block.querySelector(".sm-phone-qr").innerHTML = qr.createSvgTag({ cellSize: 4, margin: 16, scalable: true });
      } catch (error) {
        block.querySelector(".sm-phone-qr").textContent = DeskI18n.tx("二维码不可用");
        feedback.textContent = DeskI18n.tx("{error}，可复制链接连接。", { error: error.message });
        console.error("phone QR", error.message);
      }
    }
  } catch (error) {
    if (visible()) {
      card.innerHTML = '<div class="sm-card"><div class="sm-prov-line" role="status"></div><button type="button" class="sm-link-btn">' + DeskI18n.txh("重试") + '</button></div>';
      card.querySelector(".sm-prov-line").textContent = error.message;
      card.querySelector("button").onclick = () => renderPhoneConnectionSettings(section);
    }
  } finally {
    section._phoneLoading = false;
    _phoneResumeRender(section);
  }
}

function _paintSystemArrangementsSettings(section, status, detailKind = "") {
  const card = section.querySelector(".sm-card") || section;
  if (!section.isConnected) return;
  const items = _arrSysCards(status || {}).filter((item) => item.kind === "memory");
  const active = items.find((item) => item.kind === detailKind);
  if (active) {
    section.classList.add("sm-system-arrangement-detail-open");
    card.innerHTML = '<div class="sm-system-arrangement-detail"></div>';
    const body = card.querySelector(".sm-system-arrangement-detail");
    const back = () => {
      _paintSystemArrangementsSettings(section, status, "");
      document.getElementById("sm-settings-scroll")?.scrollTo({ top: 0 });
      requestAnimationFrame(() => card.querySelector(`[data-kind="${active.kind}"] .arr-sys-head`)?.focus());
    };
    const repaint = (localOnly) => localOnly
      ? _paintSystemArrangementsSettings(section, status, active.kind)
      : renderSystemArrangementsSettings(section, active.kind);
    _arrSysRenderResource(body, active, back, repaint, {
      bodyClass: "sm-system-arrangement-detail arrangements-page arr-sys-resource-page", backLabel: DeskI18n.tx("系统安排"),
    });
    document.getElementById("sm-settings-scroll")?.scrollTo({ top: 0 });
    return;
  }
  section.classList.remove("sm-system-arrangement-detail-open");
  card.innerHTML = items.length ? `<div class="sm-system-arrangements">${items.map(_arrSysCard).join("")}</div>`
    : '<div class="sm-loading">' + DeskI18n.txh("暂无系统安排状态。") + '</div>';
  card.querySelectorAll(".arr-sys-agent").forEach((node) => {
    const item = items.find((candidate) => candidate.kind === node.dataset.kind);
    if (!item) return;
    node.querySelector(".arr-sys-head").onclick = () => _paintSystemArrangementsSettings(section, status, item.kind);
  });
}

async function renderSystemArrangementsSettings(section, detailKind = "") {
  const card = section.querySelector(".sm-card") || section;
  card.innerHTML = '<div class="sm-loading">' + DeskI18n.txh("读取中…") + '</div>';
  let status = null;
  try { status = await api("/workbench/agents/status", { timeout: 20000 }); }
  catch (error) { card.innerHTML = `<div class="sm-loading">${DeskI18n.txh("读取失败：{error}", { error: error.message || error })}</div>`; return; }
  _paintSystemArrangementsSettings(section, status, detailKind);
}

// 设置页里那一节的正文。取数与 HTML 都复用任务页那套(tpTaskRows / tpStatsHtml),
// 不另写一份——两处显示的数一旦各算各的,对不上时没人说得清哪个对。
async function renderTaskStatsSection(box) {
  if (typeof tpTaskRows !== "function") { box.innerHTML = '<div class="sm-prov-line">' + DeskI18n.txh("统计不可用。") + '</div>'; return; }
  let rows = [];
  try { rows = await tpTaskRows(); }
  catch (e) { box.innerHTML = '<div class="sm-prov-line">' + DeskI18n.txh("读取失败：{error}", { error: e.message }) + "</div>"; return; }
  if (!box.isConnected) return;
  if (!rows.length) { box.innerHTML = '<div class="sm-prov-line">' + DeskI18n.txh("还没有工作流。") + '</div>'; return; }
  const byOpen = rows.slice().sort((a, b) => b.open.length - a.open.length);
  box.innerHTML = tpStatsHtml(byOpen, _tpAgg(rows));
  _tpWireTrend(box, byOpen);   // 柱状图的悬停提示:与任务页同一套接线
}
// 登录态呈现(B 方案):已登录=灰色细线对勾+次级文本,未登录=纯文本;颜色只留给异常。
const _PV_CHECK = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
function _pvState(on, text) {
  return `<span class="pv-state ${on ? "on" : "off"}">${on ? _PV_CHECK : ""}${DeskI18n.txh(text)}</span>`;
}
// 分组容器:纯排版用,不像 smSection 那样重载 appendChild——组里装的是整段 smSection
// 节点,不是零散的行,调用方直接 append 就够,不需要再找一层卡片。
function smGroup(title) {
  const g = document.createElement("div"); g.className = "sm-group";
  const h = document.createElement("div"); h.className = "sm-group-title"; h.textContent = DeskI18n.tx(title);
  g.appendChild(h);
  return g;
}
function smSection(title, hint, icon) {
  // 仿 codex:标题/说明在卡外,配置正文装进圆角卡片(.sm-card)。
  // 返回的节点重载 appendChild/append:调用方无感,内容自动落卡内。
  // icon 可选:各家引擎的品牌图标(取自 BRAND_ICONS,与引擎选择器同一份),挂在标题文字前——
  // 设置页要认出"这一节是哪家",原来全靠一行纯文字,而选引擎的地方到处都在用图标认牌子,
  // 这里补上同一份,不是重画一套(2026-08-24 用户实报)。没传就是原来的纯文字标题。
  const d = document.createElement("div"); d.className = "sm-section";
  d.innerHTML = `<div class="sm-title">${icon ? `<span class="sm-title-ic">${icon}</span>` : ""}${DeskI18n.txh(title)}</div>` + (hint ? `<div class="sm-hint">${DeskI18n.txh(hint)}</div>` : "");
  const card = document.createElement("div"); card.className = "sm-card";
  HTMLElement.prototype.appendChild.call(d, card);
  d.appendChild = (n) => card.appendChild(n);
  d.append = (...n) => card.append(...n);
  return d;
}
