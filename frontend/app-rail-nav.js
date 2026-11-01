// 由 app-rail.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 1/5 片
// desktron 前端(app.js 拆分):侧栏导航(两级:项目 → 会话)+ 固定入口 + 搜索
// 经典脚本按序加载共享全局作用域,加载顺序见 index.html。勿单独重排。
// 重构说明:原左侧图标栏(rail)退役,与第二栏面板合并成单侧栏;
// renderRail/selectNav 保名(其他文件在调),语义改为「刷新项目树 / 路由主区」。

function renderRail() {
  // 保名兼容:域列表变化后(reloadDomains 等)刷新项目树，侧栏不被管理页面接管。
  renderSessionPanel();
}

function persistExpanded() { try { localStorage.setItem("cd_expanded", JSON.stringify([...expandedProjects])); } catch {} }

function _deskNotice(message) {
  if (typeof _toast === "function") _toast(message);
  else alert(message);
}

// 管理入口独立于主区导航；节点随入口保留，关闭不销毁输入、筛选和详情状态。
const _resourcePages = new Map();
let _resourceActive = "", _resourceReturnFocus = null;
const _RESOURCE_GROUPS = {
  arrangements: { title: "安排", key: "nav.arrangements", items: [["local", "本地"]] },
  apps: { title: "应用", key: "nav.apps", items: [["all", "全部"], ["official", "官方"], ["local", "本地"]] },
};
function _resourceHeader(title, description, actions = "") {
  return '<header class="sm-view-head resource-view-head"><div>'
    + `<h1 class="wf-h1">${escapeHtml(title)}</h1><p>${escapeHtml(description)}</p></div>${actions}</header>`;
}
function _resourceSearchInput() {
  return _resourceBody(_resourceActive)?.querySelector('.arr-search input, .ap-q');
}
function _resourceSyncSearch(force = false) {
  const search = document.querySelector("#resource-search");
  if (!search) return;
  const input = _resourceSearchInput();
  search.disabled = !input;
  // Mirror the page's own placeholder binding so a later language switch repaints both alike.
  const own = input ? JSON.parse(input.getAttribute("data-desk-i18n") || "{}").placeholder : ["resource.noSearch", {}];
  const spec = JSON.parse(search.getAttribute("data-desk-i18n") || "{}");
  if (own) DeskI18n.bind(search, own[0], own[1] || {}, "placeholder", own[2]);
  else {
    delete spec.placeholder; search.setAttribute("data-desk-i18n", JSON.stringify(spec));
    search.placeholder = input.placeholder;
  }
  if (force || document.activeElement !== search) search.value = input?.value || "";
}
function _resourcePaintNav() {
  const modal = document.getElementById("resource-modal");
  if (!modal) return;
  const group = _RESOURCE_GROUPS[_resourceActive];
  if (!group) { closeResourcePage(false); return; }
  const selected = _resourceActive === "arrangements" ? _arrSection
    : _resourceActive === "apps" ? (_resourceBody("apps")?._appState?.pool || "all") : _resourceActive;
  DeskI18n.bind(modal.querySelector("#resource-title"), group.key);
  const host = modal.querySelector(".resource-nav-items");
  DeskI18n.bind(host, "resource.categories", {group: DeskI18n.t(group.key)}, "aria-label", group.title + "分类");
  // 不重建未变的目录，分类切换与数据轮询都不会打断键盘焦点。
  if (host.dataset.group !== group.title) {
    host.dataset.group = group.title;
    host.replaceChildren(...group.items.map(([id, label]) => {
      const button = document.createElement("button");
      button.type = "button"; button.className = "sm-nav-row"; button.dataset.category = id;
      button.innerHTML = `<span class="ai-title">${escapeHtml(label)}</span>`;
      DeskI18n.bind(button.querySelector(".ai-title"), "resource." + id);
      button.onclick = () => {
        if (group === _RESOURCE_GROUPS.arrangements) _arrSelectSection(id);
        else if (group === _RESOURCE_GROUPS.apps) _resourceBody("apps")?._selectPool?.(id);
      };
      return button;
    }));
  }
  host.querySelectorAll("button").forEach((button) => {
    const on = button.dataset.category === selected;
    button.classList.toggle("active", on); button.setAttribute("aria-current", on ? "page" : "false");
  });
  _resourceSyncSearch(true);
}
function _resourceBody(key) { return _resourcePages.get(key)?.body; }
function _resourceScroll(key) { return _resourcePages.get(key)?.scroll; }
function _resourceIsOpen(key) {
  return _resourceActive === key && !document.getElementById("resource-modal")?.hidden;
}
function _resourceGoBack() {
  const back = _resourceBody(_resourceActive)?._resourceBack;
  if (typeof back === "function") back();
  else closeResourcePage();
}
function closeResourcePage(restoreFocus = true) {
  const modal = document.getElementById("resource-modal");
  if (!modal || modal.hidden) return;
  const page = _resourcePages.get(_resourceActive);
  if (page) page.top = page.scroll.scrollTop;
  modal.hidden = true;
  document.body.classList.remove("resource-dialog-open");
  if (restoreFocus && _resourceReturnFocus?.isConnected) _resourceReturnFocus.focus();
}
function openResourcePage(key, title) {
  let modal = document.getElementById("resource-modal");
  if (!modal) {
    modal = document.createElement("div"); modal.id = "resource-modal"; modal.hidden = true;
    modal.setAttribute("data-app-control-protected", "true");
    modal.innerHTML = '<div class="resource-backdrop sm-backdrop"></div><section class="sm-panel resource-panel" role="dialog" aria-modal="true" aria-labelledby="resource-title">'
      + '<button class="resource-close" aria-label="关闭" type="button"><svg viewBox="0 0 32 32" data-desk-scattered="24" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="m7 7 18 18M25 7 7 25"/></g></svg></button>'
      + '<div class="settings-center-live resource-center"><div class="sm-page">'
      + '<aside class="sm-inner-nav resource-nav"><button class="resource-back" type="button" aria-label="返回上一级或退出"><svg viewBox="0 0 32 32" data-desk-scattered="25" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><g transform="translate(32 0) scale(-1 1)"><g transform="translate(-1.65323 -1.08592) scale(1.06787)"><path d="m11 3.5 10.2 10.6c1.1 1.1 1.1 2.7 0 3.8L11 28.5" stroke-width="2.6"/></g></g></g></svg><span>返回</span></button>'
      + '<label class="sm-settings-search"><span class="sm-settings-search-icon" aria-hidden="true"><svg viewBox="0 0 32 32" data-desk-scattered="26" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><circle cx="13.7" cy="13.7" r="8.7"/><path d="m20.1 20.1 6.7 6.7"/></g></svg></span>'
      + '<input id="resource-search" type="search" aria-label="搜索当前分类" autocomplete="off" spellcheck="false"><kbd>⌘ K</kbd></label>'
      + '<div class="sm-nav-title" id="resource-title"></div><nav class="resource-nav-items"></nav></aside>'
      + '<section class="sm-settings-content resource-content"></section></div></div></section>';
    document.body.appendChild(modal);
    DeskI18n.bind(modal.querySelector(".resource-close"), "common.close", {}, "aria-label");
    DeskI18n.bind(modal.querySelector(".resource-back"), "resource.backLabel", {}, "aria-label");
    DeskI18n.bind(modal.querySelector(".resource-back span"), "resource.back");
    DeskI18n.bind(modal.querySelector("#resource-search"), "resource.search", {}, "aria-label");
    modal.querySelector(".resource-close").onclick = () => closeResourcePage();
    modal.querySelector(".resource-backdrop").onclick = () => closeResourcePage();
    modal.querySelector(".resource-back").onclick = _resourceGoBack;
    const search = modal.querySelector("#resource-search");
    const query = (e) => {
      if (e.isComposing) return;
      const input = _resourceSearchInput();
      if (input) { input.value = search.value; input.dispatchEvent(new Event("input", { bubbles: true })); }
    };
    search.oninput = query; search.oncompositionend = query;
    document.addEventListener("keydown", (e) => {
      if (modal.hidden) return;
      // 子表单/菜单拥有本次 Escape；不能把底下管理页一起关掉。
      const menu = document.querySelector('.rp-ctx, .resume-pop');
      if (menu && e.key === "Escape") {
        e.preventDefault(); e.stopImmediatePropagation();
        if (menu.matches('.rp-ctx')) openCtxMenu.close?.(); else menu.remove();
        return;
      }
      const child = document.querySelector('.arr-modal, .pkg-modal, .nw-modal, .auth-tui-back, #settings-modal:not([hidden])');
      if (child) return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        const input = search.getClientRects().length ? search : _resourceSearchInput();
        if (input && !input.disabled) { e.preventDefault(); e.stopImmediatePropagation(); input.focus(); }
        return;
      }
      if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); _resourceGoBack(); }
      if (e.key === "Tab") {
        const nodes = [...modal.querySelectorAll('button, input, select, textarea, a[href], [tabindex="0"]')]
          .filter((el) => !el.disabled && el.getClientRects().length);
        const first = nodes[0], last = nodes[nodes.length - 1];
        if (e.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
          e.preventDefault(); last?.focus();
        } else if (!e.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) {
          e.preventDefault(); first?.focus();
        }
      }
    }, true);
  }
  if (modal.hidden) _resourceReturnFocus = document.activeElement;
  let page = _resourcePages.get(key);
  const fresh = !page;
  if (!page) {
    const scroll = document.createElement("div"); scroll.className = "resource-scroll";
    const body = document.createElement("div"); body.className = "md";
    scroll.appendChild(body); page = { body, scroll, top: 0 }; _resourcePages.set(key, page);
    body.addEventListener("input", () => { if (_resourceActive === key) _resourceSyncSearch(); });
  }
  if (_resourceActive !== key) {
    const previous = _resourcePages.get(_resourceActive);
    if (previous) previous.top = previous.scroll.scrollTop;
    modal.querySelector(".resource-content").replaceChildren(page.scroll);
    _resourceActive = key;
  }
  modal.hidden = false; document.body.classList.add("resource-dialog-open");
  _resourcePaintNav();
  if (page.top) page.scroll.scrollTop = page.top;
  const close = modal.querySelector(".resource-close");
  (close.getClientRects().length ? close : modal.querySelector(".resource-back")).focus();
  return { ...page, fresh };
}

// 导航路由。兼容旧形态 id:desktop/coding/campaigns/容器/「容器:子区」复合键一律归一——
// 复合键取子区段(平铺后它就是普通项目),其余退化为「无选中项目」(主区维持终端跟随)。
function selectNav(type, id, opts = {}) {
  if (type === "page") {
    // 存量资源页与工作流详情页均已下线，旧书签统一回到欢迎页。
    _restoreEngineWelcome();
    return;
  }
  let raw = String(id || "");
  if (raw.includes(":")) raw = raw.split(":")[1] || raw;   // 容器:子区 → 子区(平铺后即普通项目)
  const d = domains.find((x) => x.id === raw);
  const plain = d && !d.kind;   // 项目/容器/管理器全部退役,域只有工作流一种
  nav = { type: "domain", id: plain ? d.id : DESKTOP };
  try { localStorage.setItem("cd_nav", JSON.stringify(nav)); } catch {}
  _sideEnterTreeMode();
  _sideHighlight(plain ? d.id : null);
  renderTaskDock();   // 任务源随 nav 变(单域添加框归属)
  if (!opts.noTermSync) syncTermView();   // 切工作流只恢复会话或欢迎页，不再打开详情页
}

function _sideEnterTreeMode() {
  $("#panel").dataset.sideMode = "tree";
  $("#proj-head").hidden = false;
}
function _sideHighlight(id) {
  document.querySelectorAll("#panel-list .proj-fold > summary").forEach((s) => {
    s.classList.toggle("sel", !!id && s.parentElement.dataset.pid === id);
  });
}

// 分组标题与工作流名称行一致：文字和空白处都可展开 / 折叠，内嵌按钮独立操作。
function bindSidebarSectionToggle(head, toggle) {
  head.setAttribute("role", "button");
  head.tabIndex = 0;
  head.onclick = (ev) => {
    if (ev.target.closest("button")) return;
    toggle();
  };
  head.onkeydown = (ev) => {
    if (ev.target !== head || !["Enter", " "].includes(ev.key)) return;
    ev.preventDefault();
    toggle();
  };
}

// ---- 固定入口 / 搜索(节点常驻,只绑一次) ----
(function initSidebar() {
  const fxNew = $("#fx-new");
  if (!fxNew) return;   // 兜底:骨架异常时不绑,避免整文件抛错拖垮后续脚本
  // 固定「聊天」不是“先建一条记录再等人输入”：直接打开一张前端临时空会话，
  // 第一条消息真正发送时才落后端。只看一眼就切走，不会在聊天列表留下空记录。
  // 工作流里的「新建会话」仍是明确的创建动作，继续走 newTerminal，不与这里合并。
  fxNew.onclick = () => openDraftChat();
  // 固定入口再点关闭管理弹窗，主区导航始终保持原位。
  $("#fx-apps").onclick = () => _resourceIsOpen("apps") ? closeResourcePage() : openStore("app");
  // 工作流区头「+」:直接新建工作流(填名字建目录+注册),与「项目」头的「打开代码仓」对称
  $("#proj-add").onclick = () => openNewWorkspace();
  // 工作流标题整行收起 / 展开本组，保留各工作流内部的展开状态。
  const projHead = $("#proj-head");
  const applyProjCollapsed = (on) => {
    $("#panel-list").classList.toggle("projects-collapsed", on);
    $("#proj-fold-all").classList.toggle("folded", on);
    projHead.setAttribute("aria-expanded", String(!on));
  };
  applyProjCollapsed(localStorage.getItem("cd_projects_collapsed") === "1");
  const toggleProjects = () => {
    const on = !$("#panel-list").classList.contains("projects-collapsed");
    applyProjCollapsed(on);
    try { localStorage.setItem("cd_projects_collapsed", on ? "1" : "0"); } catch {}
  };
  $("#proj-fold-all").onclick = toggleProjects;
  bindSidebarSectionToggle(projHead, toggleProjects);
  // 安排:只放 Agent 的自动触发。工作流任务仍留在各工作流会话的常驻任务栏里，
  // 两者不聚合，避免“要做的事”与“何时自动执行”混成一张清单。
  $("#fx-tasks").onclick = (ev) => {
    ev.stopPropagation();
    $("#engine-menu").hidden = true;
    if (_resourceIsOpen("arrangements")) { closeResourcePage(); return; }
    openArrangementsPage();
  };
  // 列表滚动时「项目」头亮出分隔线+阴影(滚回顶部消失)
  const pl = $("#panel-list");
  pl.addEventListener("scroll", () => {
    $("#proj-head").classList.toggle("scrolled", pl.scrollTop > 0);
  }, { passive: true });
  // 搜索态用一张独立结果页替换工作流树：标题即时本地匹配，正文走后端全文索引。
  $("#side-search-btn").onclick = () => {
    const row = $("#side-search");
    row.hidden = !row.hidden;
    if (!row.hidden) {   // 按图标实际中线定位输入框(行高受 logo 行盒影响,静态偏移不可靠)
      const btn = $("#side-search-btn").getBoundingClientRect();
      const brand = document.querySelector(".side-brand").getBoundingClientRect();
      row.style.top = (btn.top - brand.top + btn.height / 2 - 15) + "px";
      $("#side-search-inp").focus();
    } else { $("#side-search-inp").value = ""; applySideFilter(); }
  };
  $("#side-search-inp").oninput = () => applySideFilter();
  $("#side-search-inp").onkeydown = (e) => { if (e.key === "Escape") $("#side-search-btn").onclick(); };
})();

let _sideSearchTimer = 0, _sideSearchSeq = 0, _sideSearchPoll = 0, _sideSearchKey = "";

// 结果排序:相关度(默认)或时间。存 localStorage——搜索是随手开随手关的动作,
// 每次都要重新拨一次开关等于没有这个开关。
let _sideSearchSort = "";
try { _sideSearchSort = localStorage.getItem("cd_side_search_sort") || ""; } catch {}

function sideSearchHost() {
  let host = $("#side-search-results");
  if (host) return host;
  host = document.createElement("div");
  host.id = "side-search-results";
  host.hidden = true;
  $("#proj-head").insertAdjacentElement("afterend", host);
  return host;
}

function sideSearchDomain(id) {
  if (!id || id === DESKTOP) return "聊天";
  const hit = (domains || []).find((d) => d.id === id);
  return (hit && hit.name) || id;
}

// 命中的时间取「这条消息自己的时间」,不是会话的更新时间。两者经常差很远:实测同一批
// 结果里,`desktron ui` 的会话更新在今天 17:39,两条命中却分别是今天 16:19 与 7月30日;
// `Desktron Delivery` 更新在今天,命中在 8月10 与 8月23。按会话时间标的话,每张卡片
// 都写着「今天」,而人想知道的恰恰是「这句话是什么时候说的」。同一张卡里的两条命中按
// 相关度排、先后也不按时间,不标出来看不出它们跨了三周。
//
// 没有复用 _arrAgo / _agpRel 那几个:它们的入参是秒,而这里的 at 是毫秒(见后端
// session_search.py 的 _at_ms);它们也都止步于「N 天前」,会话历史动辄跨月,得落到具体日期。
function sideSearchAt(ms) {
  const t = Number(ms || 0);
  if (!t) return "";
  const d = new Date(t), now = new Date();
  const sec = (now.getTime() - t) / 1000;
  if (sec < 60) return "刚刚";
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  // 今天之内报钟点:比「N 小时前」更容易和自己这一天的记忆对上号
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const gap = Math.round((day(now) - day(d)) / 86400000);
  if (gap <= 0) return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (gap === 1) return "昨天";
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日`;
  return `${d.getFullYear()}年${d.getMonth() + 1}月`;
}

function sideSearchMark(el, text, query) {
  text = String(text || ""); query = String(query || "");
  const low = text.toLowerCase(), needle = query.toLowerCase();
  let from = 0, at = needle ? low.indexOf(needle) : -1;
  while (at >= 0) {
    el.appendChild(document.createTextNode(text.slice(from, at)));
    const mark = document.createElement("mark"); mark.textContent = text.slice(at, at + query.length);
    el.appendChild(mark); from = at + query.length; at = low.indexOf(needle, from);
  }
  el.appendChild(document.createTextNode(text.slice(from)));
}

function sideSearchOpen(item, hit) {
  const domain = item.domain || DESKTOP;
  attachTerminal(item.session_id || item.id, item.engine || curEngine, {
    domain, title: item.title || "对话",
    anchor: hit ? { message_id: hit.message_id, turn_from_end: hit.turn_from_end,
                    engine: item.engine || curEngine } : null,
  });
  navPush({ kind: "sess", id: item.session_id || item.id, domain });
  _sideHighlight(domain !== DESKTOP ? domain : null);
}

function sideSearchTitleHits(query) {
  const q = query.toLowerCase();
  return (_termsList || []).filter((s) => sessionVisibleInCurrentMode(s)
    && stripTitleGlyph(s.title || "").toLowerCase().includes(q)).slice(0, 20);
}

function sideSearchPaint(query, response) {
  const host = sideSearchHost();
  if (($("#side-search-inp").value || "").trim() !== query) return;
  host.textContent = "";
  const titles = sideSearchTitleHits(query);
  const section = (label, count, extra) => {
    const head = document.createElement("div"); head.className = "ss-head";
    head.innerHTML = `<span class="ss-head-label">${escapeHtml(label)}</span>`;
    if (extra) head.appendChild(extra);
    const n = document.createElement("span"); n.className = "ss-head-count";
    n.textContent = String(count); head.appendChild(n);
    host.appendChild(head);
  };
  // 排序开关。切换要重新问后端:命中的先后由 SQL 定,前端手上只有每段会话的前十条,
  // 拿它重排会得出「按时间排的前十条」而不是「时间上最新的十条」。
  const sortBox = () => {
    const box = document.createElement("div"); box.className = "ss-sort";
    for (const [v, label] of [["", "相关"], ["time", "时间"]]) {
      const b = document.createElement("button"); b.type = "button";
      b.className = "ss-sort-b" + (v === _sideSearchSort ? " on" : "");
      b.textContent = label;
      b.onclick = () => {
        if (v === _sideSearchSort) return;
        _sideSearchSort = v;
        try { localStorage.setItem("cd_side_search_sort", v); } catch {}
        _sideSearchKey = "";        // 查询没变但排序变了,得让 applySideFilter 放行这一次
        applySideFilter();
      };
      box.appendChild(b);
    }
    return box;
  };
  if (titles.length) {
    section("会话标题", titles.length);
    for (const s of titles) {
      const row = document.createElement("button"); row.className = "ss-title"; row.type = "button";
      const main = document.createElement("span"); main.className = "ss-title-main";
  // 排序开关。切换要重新问后端:命中的先后由 SQL 定,前端手上只有每段会话的前十条,
  // 拿它重排会得出「按时间排的前十条」而不是「时间上最新的十条」。
  const sortBox = () => {
    const box = document.createElement("div"); box.className = "ss-sort";
    for (const [v, label] of [["", "相关"], ["time", "时间"]]) {
      const b = document.createElement("button"); b.type = "button";
      b.className = "ss-sort-b" + (v === _sideSearchSort ? " on" : "");
      b.textContent = label;
      b.onclick = () => {
        if (v === _sideSearchSort) return;
        _sideSearchSort = v;
        try { localStorage.setItem("cd_side_search_sort", v); } catch {}
        _sideSearchKey = "";        // 查询没变但排序变了,得让 applySideFilter 放行这一次
        applySideFilter();
      };
      box.appendChild(b);
    }
    return box;
  };
      sideSearchMark(main, stripTitleGlyph(s.title || "对话"), query);
      const meta = document.createElement("span"); meta.className = "ss-meta";
      meta.textContent = sideSearchDomain(s.domain);
      row.append(main, meta); row.onclick = () => sideSearchOpen(s);
      host.appendChild(row);
    }
  }
  const results = (response && response.results) || [];
  section("搜索结果", results.length, sortBox());
  if (!response) {
    const wait = document.createElement("div"); wait.className = "ss-empty";
    wait.textContent = query.length < 2 ? "再输入一个字搜索正文" : "正在搜索正文…";
    host.appendChild(wait); return;
  }
  for (const item of results) {
    const card = document.createElement("div"); card.className = "ss-card";
    const cap = document.createElement("button"); cap.className = "ss-cap"; cap.type = "button";
    const title = document.createElement("span"); title.className = "ss-cap-title";
    title.textContent = item.title || "对话";
    const meta = document.createElement("span"); meta.className = "ss-meta";
    meta.textContent = sideSearchDomain(item.domain);
    cap.append(title, meta); cap.onclick = () => sideSearchOpen(item);
    card.appendChild(cap);
    const hits = item.hits || [];
    const rest = [];
    hits.forEach((hit, i) => {
      const row = document.createElement("button"); row.className = "ss-hit"; row.type = "button";
      // 时间写在片段前面,不另起一行:片段本来就窄,多一行会把每张卡片都撑高一截;
      // 前缀的左缘是固定的,一列扫下来能直接比出哪条更早。
      const at = sideSearchAt(hit.at);
      if (at) {
        const stamp = document.createElement("span");
        stamp.className = "ss-hit-at"; stamp.textContent = at;
        row.appendChild(stamp);
      }
      sideSearchMark(row, hit.snippet || "", query);
      row.onclick = () => sideSearchOpen(item, hit);
      if (i < 2) card.appendChild(row);
      else { row.hidden = true; rest.push(row); card.appendChild(row); }
    });
    // 这一行原来是个 div,只报数、点不动:卡片上写着「搜索到 37 处」,能看的却只有两条
    // (2026-09-04 用户实见)。改成能展开——其余那几条本来就在这次结果里,展开不再打接口。
    if (rest.length || item.count > hits.length) {
      const more = document.createElement("button"); more.className = "ss-more"; more.type = "button";
      const left = () => Math.max(0, item.count - hits.length);
      const paint = (open) => {
        if (!rest.length) { more.textContent = `搜索到 ${item.count} 处相关内容`; return; }
        // 后端每段最多留十条,更多的只报数不给正文。摊开之后要说清楚剩下的在哪儿看,
        // 否则「展开了却还是不全」比不给展开更让人疑惑。
        more.textContent = open
          ? (left() ? `其余 ${left()} 处打开会话查看` : "收起")
          : `搜索到 ${item.count} 处相关内容`;
        more.classList.toggle("open", !!open);
      };
      paint(false);
      if (rest.length) {
        more.onclick = () => {
          const open = rest[0].hidden;
          rest.forEach((r) => { r.hidden = !open; });
          paint(open);
        };
      } else {
        more.disabled = true;
      }
      card.appendChild(more);
    }
    host.appendChild(card);
  }
  if (!results.length && !response.indexing) {
    const empty = document.createElement("div"); empty.className = "ss-empty";
    empty.textContent = response.error ? "历史索引暂不可用，稍后重试" : "没有搜索结果";
    host.appendChild(empty);
  }
  if (response.indexing) {
    const p = response.progress || {};
    const wait = document.createElement("div"); wait.className = "ss-indexing";
    wait.textContent = `正在整理历史 ${p.done || 0}/${p.total || 0}`;
    host.appendChild(wait);
  }
}

async function sideSearchFetch(query, seq) {
  if (seq !== _sideSearchSeq) return;
  let response = null;
  try {
    const searchEngine = shepherdMode ? "shepherd" : curEngine;
    response = await api(`/session/search?q=${encodeURIComponent(query)}&engine=${encodeURIComponent(searchEngine)}`
                         + (_sideSearchSort ? `&sort=${encodeURIComponent(_sideSearchSort)}` : ""),
                         { timeout: 30000 });
  } catch {}
  if (seq !== _sideSearchSeq) return;
  sideSearchPaint(query, response || { results: [], indexing: false });
  clearTimeout(_sideSearchPoll);
  if (response && response.indexing)
    _sideSearchPoll = setTimeout(() => sideSearchFetch(query, seq), 700);
}

// 供 renderSessionPanel 重渲后重放；搜索态不改原树的展开状态，退出即可原样回来。
function applySideFilter() {
  const q = (($("#side-search-inp") || {}).value || "").trim();
  const el = $("#panel-list");
  if (!el) return;
  const host = sideSearchHost();
  el.hidden = !!q; $("#proj-head").hidden = !!q; host.hidden = !q;
  // 会话状态每 1.5 秒保鲜一次，末尾也会调用这里。查询没变时结果页已经有效，不能把它
  // 清回「正在搜索」再重建；那会让整列结果持续闪烁，滚动位置与 hover 也一并丢失。
  const key = q ? `${shepherdMode ? "shepherd" : curEngine}\n${_sideSearchSort}\n${q}` : "";
  if (key === _sideSearchKey) return;
  _sideSearchKey = key;
  clearTimeout(_sideSearchTimer); clearTimeout(_sideSearchPoll);
  const seq = ++_sideSearchSeq;
  if (!q) {
    host.textContent = "";
    return;
  }
  sideSearchPaint(q, null);
  if (q.length >= 2)
    _sideSearchTimer = setTimeout(() => sideSearchFetch(q, seq), 160);
}

function saveState(patch) {
  const partial = patch && typeof patch === "object";
  const fullSave = () => {
    const state = { ...wbState };
    delete state.sessionOrderPins;   // 清理由旧服务误存进去的瞬时操作字段
    return api("/workbench/state", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(state),
    });
  };
  if (!partial) return fullSave().catch(() => {});
  // 新服务端走专用局部更新端点；旧服务端会返回 404，随即回退为完整 PUT。
  // 不能把局部对象 POST 到旧的 /workbench/state：旧进程会将它当完整状态覆盖。
  return api("/workbench/state/patch", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(patch),
  }).catch(fullSave).catch(() => {});
}

// 按存储的顺序数组稳定排序;不在顺序里的保持原相对序、排在后面。order 空则原样返回。
function applyOrder(items, order, keyOf) {
  if (!order || !order.length) return items;
  const idx = new Map(order.map((k, i) => [k, i]));
  return items.map((it, i) => [it, i]).sort((a, b) => {
    const ia = idx.has(keyOf(a[0])) ? idx.get(keyOf(a[0])) : Infinity;
    const ib = idx.has(keyOf(b[0])) ? idx.get(keyOf(b[0])) : Infinity;
    return ia - ib || a[1] - b[1];   // 同序保持原始下标(稳定)
  }).map((p) => p[0]);
}

// 拖拽区通常只渲染完整顺序的一部分（前五条、当前引擎等）。只替换这些可见 id
// 占据的位置，不能拿可见数组覆盖完整数组，否则其余会话会在下次轮询时被当成新会话。
function mergeOrderedSubset(existing, orderedSubset) {
  const base = [...new Set((existing || []).filter(Boolean).map(String))];
  const subset = [...new Set((orderedSubset || []).filter(Boolean).map(String))];
  if (!subset.length) return base;
  const moving = new Set(subset);
  let cursor = 0;
  const merged = base.map((id) => moving.has(id) && cursor < subset.length ? subset[cursor++] : id);
  while (cursor < subset.length) merged.push(subset[cursor++]);
  return [...new Set(merged)];
}

// 长按拖拽排序:container 上对 itemSel 子项启用。按住 ~600ms(无明显移动)进入拖拽。
// 效果=半透明悬浮:拖拽时克隆"把手行"position:fixed 跟光标走,原位留淡占位、随拖动重排显示落点。
// opts.rowSel:把手/比较行(默认 item 本身)。项目嵌套会话时设为 "summary",只许从标题起拖,
//   避免拖会话冒泡误触发拖项目。onDrop(新顺序 key 数组);keyOf(el)=标识。
//
// 触屏没有单独的 600ms 候选态——那 600ms 是防"点一下"误判成拖拽,而触屏这边
// bindLongPressMenu 已经用它自己的 500ms 长按判过一次意图(先当作要弹菜单)。
// 这里只认它转发来的 cd-longpress-drag(菜单弹出后手指仍在推,判定成想拖拽,见
// frontend/app-events.js),不会再等一轮 600ms——叠两次长按,反应会明显滞后。
function makeReorderable(container, itemSel, keyOf, onDrop, opts = {}) {
  if (container._reorderable) return;   // 幂等:常驻节点(#panel-list)/复用 body 只绑一次,处理器读实时 DOM
  container._reorderable = true;
  const rowOf = (it) => (opts.rowSel ? it.querySelector(opts.rowSel) : it) || it;
  let holdT = 0, dragging = null, ghost = null, offX = 0, offY = 0, startX = 0, startY = 0,
      justDragged = false, moved = false, touchId = null, mousePending = false;
  const items = () => [...container.querySelectorAll(itemSel)];
  // 主键是否仍按着。mouseup 有可能压根收不到——原生 HTML5 拖拽一旦接管(残留选区、可拖子元素)
  // 就不再派发 mouseup,长按候选与拖拽态因此卡住,表现为「点一下就把会话拖走了」。
  // mousemove 的 buttons 位能直接看出真实状态,拿它做自愈,不依赖 up 事件送达。
  const held = (e) => (e.buttons === undefined ? true : !!(e.buttons & 1));
  const PRE_OFF = ["mouseup", "pointerup", "pointercancel", "dragstart"];
  const cleanupPre = () => {
    clearTimeout(holdT);
    document.removeEventListener("mousemove", preMove, true);
    for (const ev of PRE_OFF) document.removeEventListener(ev, preUp, true);
    window.removeEventListener("blur", preUp, true);
    // 会话状态轮询会在集合变化时替换整行 DOM。鼠标长按候选期也要像触屏长按一样
    // 锁住重绘，否则 600ms 到点时 item 已经脱离文档，表现为同样的手势随机失效。
    // 这里只管理鼠标自己加的锁，不改 bindLongPressMenu 的触屏判定与时序。
    if (mousePending) {
      mousePending = false;
      document.body.classList.remove("reorder-pending");
    }
  };
  const preMove = (e) => { if (!held(e) || Math.abs(e.clientY - startY) > 6) cleanupPre(); };   // 移动=滚动/拖选,或键已松开:取消候选
  // blur 挂在 window 的捕获阶段,收到的不只是窗口失焦——页面里任何元素失焦都从 window 往下
  // 走捕获,一样落到这里。聊天输入框(#hs-inp)几乎总是聚焦的,点会话行那一下就让它 blur,
  // 600ms 候选在按下后 3ms 被这条掐掉;只有页面里什么都没聚焦时长按才起得来
  // (2026-09-04 WebKit 探针实证:无焦点能拖,输入框聚焦后每次都在 3ms 收到 DIV#hs-inp 的 blur)。
  // 干净页面里怎么测都通,所以此前几轮修的都是别处。只认窗口自己的失焦。
  const winBlur = (e) => e.target === window;
  const preUp = (e) => { if (e && e.type === "blur" && !winBlur(e)) return; cleanupPre(); };
  const onDown = (e) => {
    if (e.button !== 0 || e.target.closest("button, input, a, textarea")) return;
    const item = e.target.closest(itemSel);
    if (!item || !container.contains(item)) return;
    if (opts.rowSel && !(e.target.closest(opts.rowSel) && rowOf(item).contains(e.target))) return;   // 只从把手行起拖
    cleanupPre();                 // 上一次按下若没收尾(up 丢失),先清干净,不让状态叠起来
    endDrag(false);              // 残留态就地清掉,但不提交次序、不吞这次点击
    startX = e.clientX; startY = e.clientY;
    mousePending = true;
    document.body.classList.add("reorder-pending");
    document.addEventListener("mousemove", preMove, true);
    for (const ev of PRE_OFF) document.addEventListener(ev, preUp, true);
    window.addEventListener("blur", preUp, true);
    holdT = setTimeout(() => { cleanupPre(); startDrag(item); }, 600);   // 长按阈值:够长以避免点击误触
  };
  // viaTouch:这次拖拽由触屏发起(见文件下方 cd-longpress-drag)。两条路径的收尾信号不同,
  // 不能共用一套监听——尤其 pointercancel,它正是"浏览器把这次手势收走了"的信号,而触屏
  // 拖拽本来就是我们自己用 touch 事件驱动的,收到它只该忽略,不该当成手指松开把拖拽掐掉。
  const startDrag = (item, viaTouch) => {
    dragging = item; moved = false;
    document.body.classList.remove("reorder-pending");
    const row = rowOf(item), rect = row.getBoundingClientRect();
    offX = startX - rect.left; offY = startY - rect.top;
    ghost = item.cloneNode(true);   // 克隆整项(保留 class→样式正确),按把手行高度裁剪(项目只露 summary)
    ghost.removeAttribute("open");
    ghost.className = (ghost.className || "") + " reorder-ghost";
    ghost.style.cssText = `position:fixed;left:${rect.left}px;top:${rect.top}px;width:${rect.width}px;height:${rect.height}px;margin:0;`;
    document.body.appendChild(ghost);
    item.classList.add("reorder-placeholder");
    document.body.classList.add("reorder-active");
    window.getSelection()?.removeAllRanges();   // 清掉按下时可能起的选区
    if (!viaTouch) {
      document.addEventListener("mousemove", dragMove, true);
      for (const ev of PRE_OFF) document.addEventListener(ev, dragUp, true);
    }
    window.addEventListener("blur", dragUp, true);   // 两条路径都要:切走了就地收尾
  };
  // ghost 跟随 + 落点判定:鼠标/触屏走到这里就共用同一份,坐标来源不同而已。
  const dragMoveTo = (x, y) => {
    if (Math.abs(x - startX) > 4 || Math.abs(y - startY) > 4) moved = true;
    window.getSelection()?.removeAllRanges();   // 拖拽中持续清,防划选背景文本
    ghost.style.left = (x - offX) + "px";
    ghost.style.top = (y - offY) + "px";
    let placed = false;
    for (const it of items()) {
      if (it === dragging) continue;
      const rect = rowOf(it).getBoundingClientRect();   // 按把手行位置判落点(项目展开很高时也准)
      if (y < rect.top + rect.height / 2) { container.insertBefore(dragging, it); placed = true; break; }
    }
    if (!placed) {   // 落到末尾:插在最后一个可排序项之后,不越过「展开显示/收起」等非项目行(appendChild 会跑到它们下面)
      const all = items();
      const last = all[all.length - 1];
      if (last && last !== dragging) last.after(dragging);
    }
  };
  const dragMove = (e) => {
    if (!held(e)) { dragUp(); return; }   // 键已松开却没收到 up:就地收尾,不让 ghost 粘在指针上
    e.preventDefault();
    dragMoveTo(e.clientX, e.clientY);
  };
  // 触屏版:touchId 钉住发起这次拖拽的那根手指,changedTouches 里找不到就不是它,忽略
  // (另一根手指的滚动/点击不该打断正在拖的这一根)。
  const touchMove = (e) => {
    const t = Array.from(e.changedTouches).find((x) => x.identifier === touchId);
    if (!t) return;
    e.preventDefault();   // 已经在拖了,不能再让页面跟着滚
    dragMoveTo(t.clientX, t.clientY);
  };
  const touchEnd = (e) => {
    if (!Array.from(e.changedTouches).some((x) => x.identifier === touchId)) return;
    endDrag(moved);
  };
  // commit=false:只收拾现场,不提交次序、也不抑制这一次 click。
  // 用在两处——按下时清理上一次没收尾的残留;以及长按到点了但指针根本没动(用户只是点得慢),
  // 那本就是一次点击,吞掉它会让人「点一下没反应,得再点一次」。
  const endDrag = (commit) => {
    if (!dragging) return;   // 幂等:up / pointerup / blur / touchend / 下一次 mousedown 都会调到这里
    document.removeEventListener("mousemove", dragMove, true);
    for (const ev of PRE_OFF) document.removeEventListener(ev, dragUp, true);
    window.removeEventListener("blur", dragUp, true);
    // 两条路径只会挂一边,但都无条件 remove——移除没挂过的监听器是安全的空操作,
    // 不必记"这次是鼠标发起的还是触屏发起的"再分支。
    document.removeEventListener("touchmove", touchMove, true);
    document.removeEventListener("touchend", touchEnd, true);
    document.removeEventListener("touchcancel", touchEnd, true);
    ghost?.remove(); ghost = null;
    dragging.classList.remove("reorder-placeholder");
    document.body.classList.remove("reorder-active");
    const order = items().map(keyOf);
    dragging = null; touchId = null;
    if (!commit) return;
    justDragged = true; setTimeout(() => { justDragged = false; }, 0);   // 抑制紧随的 click(行 onclick / details 展开)
    onDrop(order);
  };
  const dragUp = (e) => { if (e && e.type === "blur" && !winBlur(e)) return; endDrag(moved); };   // 同 preUp:元素失焦不算
  container.addEventListener("mousedown", onDown);
  container.addEventListener("click", (e) => { if (justDragged) { e.preventDefault(); e.stopPropagation(); } }, true);
  // bindLongPressMenu 转发来的拖拽意图:el 是它认的宿主(会话行本身,或项目行的 summary),
  // 用 itemSel 网上找真正要挪的那一项——不归这个 container 管就当没听见,同一个事件会
  // 广播给页面上所有 makeReorderable 实例,各管各的。命中就直接进拖拽态,起点坐标是
  // "已经判定为推动"那一刻的触点,不必再走一遍 600ms 候选(那是给鼠标防误触用的)。
  window.addEventListener("cd-longpress-drag", (e) => {
    const { el, x, y, touchId: tid } = e.detail || {};
    // el 是长按那一层认的宿主(会话行本身,或项目行的 summary)。优先按它往上找要挪的那一项;
    // 找不到再按触点现取一次——侧栏每 1.5~5s 轮询一次,集合变了会整块重渲,而长按 500ms
    // 加人反应过来推动,前后跨着一秒多,这中间被换掉过的话 el 已经脱离文档了(见
    // app-panel.js 的 startTermPoll;它只在 reorder-active 时停手,那时拖拽还没起来)。
    let item = el && el.closest ? el.closest(itemSel) : null;
    if (item && !container.contains(item)) item = null;
    if (!item) {
      const at = document.elementFromPoint(x, y);
      item = at && at.closest ? at.closest(itemSel) : null;
    }
    if (!item || !container.contains(item)) return;   // 不归这个 container 管:同一个事件广播给所有实例,各管各的
    // 把手行的限制这里也要认一遍,鼠标那条路在 onDown 里已经认过(见 opts.rowSel)。
    // 漏掉它的后果不是"少一道校验":项目行与会话行各绑了一个实例,而会话行躺在项目行
    // 里面——从会话行往上 closest("details.proj-fold") 一样能摸到它所属的项目,于是
    // 长按一条会话再推,会话和整个工作流被同时拖起来,两个实例抢同一个 ghost 与
    // body.reorder-active,落下来的次序两边都不对。
    if (opts.rowSel && !rowOf(item).contains(el)) return;
    cleanupPre(); endDrag(false);
    startX = x; startY = y; touchId = tid;
    startDrag(item, true);
    document.addEventListener("touchmove", touchMove, { passive: false, capture: true });
    document.addEventListener("touchend", touchEnd, true);
    document.addEventListener("touchcancel", touchEnd, true);
  });
}
