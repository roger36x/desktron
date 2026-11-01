// 由 app-panel.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 1/5 片
// desktron 前端(app.js 拆分):panel:会话列表 + 终端会话渲染 + 应用条
// 经典脚本按序加载共享全局作用域,加载顺序见 index.html。勿单独重排。
// ---- panel：工作区会话列表 ----
let _panelGen = 0;

// 「+」的小菜单(锚定 popover,复用 .resume-pop + ws-add-pop 收窄样式)。items: [{label, desc?, fn} | {header}]
function openAddMenu(anchor, items) {
  const existing = document.querySelector(".resume-pop");
  if (existing) {
    if (existing._close) existing._close(); else existing.remove();
    if (existing._anchor === anchor) return;   // 再点同一个入口 = 收起,不重开
  }
  const pop = document.createElement("div");
  pop.className = "resume-pop ws-add-pop" + (items.some((it) => it.header) ? " rp-grouped" : "");
  pop._anchor = anchor;
  for (const it of items) {
    if (it.header) {
      const h = document.createElement("div");
      h.className = "rp-group-head";
      h.textContent = it.header;
      pop.appendChild(h);
      continue;
    }
    const r = document.createElement("div");
    r.className = "rp-row" + (it.desc ? " has-desc" : "") + (it.danger ? " rp-ctx-del" : "");
    // dot:标题前的状态点(运行中呼吸、静默一档、异常一档),给「一列同类东西各自什么状态」用。
    // ico:标题前的图标,与 openCtxMenu 那份同一套写法和样式(.rp-hasico + .rp-ico)。
    // 这两者互斥——同一格位置,一行要么报状态、要么标动作类型。
    if (it.ico && !it.dot) r.classList.add("rp-hasico");
    r.innerHTML = (it.dot ? `<i class="rp-dot rp-dot-${escapeHtml(it.dot)}"></i>`
                          : (it.ico ? `<span class="rp-ico">${it.ico}</span>` : ""))
      + `<span class="rp-title">${escapeHtml(it.label)}</span>` +
      (it.desc ? `<span class="rp-desc">${escapeHtml(it.desc)}</span>` : "");
    r.onclick = () => { close(); it.fn(anchor); };
    pop.appendChild(r);
  }
  document.body.appendChild(pop);
  placePop(pop, anchor.getBoundingClientRect());   // 贴近窗口下缘时会翻到锚点上方,见 app-assets.js
  // 关闭要同时听 pointerdown:触屏上点在别处不发 mousedown,只听它的话菜单关不掉——
  // 而这张菜单挂在 body 下,抽屉一收它就悬在半空(2026-08-18 手机实见)。
  // 抽屉收起时也一并关掉:它是从抽屉里那一行唤出来的,依托没了就不该再留着。
  const close = (e) => {
    const target = e && e.target;
    if (target && target.nodeType && pop.contains(target)) return;
    if (target === anchor) return;
    pop.remove();
    document.removeEventListener("mousedown", close);
    document.removeEventListener("pointerdown", close);
    window.removeEventListener("cd-drawer-closed", close);
  };
  pop._close = close;
  setTimeout(() => {
    document.addEventListener("mousedown", close);
    document.addEventListener("pointerdown", close);
    window.addEventListener("cd-drawer-closed", close);
  }, 0);
}

// 项目沿用原项目列表中的开面文件夹；重命名和删除使用用户指定的单色蒙版。
const WF_MENU_ICONS = Object.freeze({
  conversation: ICONS.plus,
  project: '<span class="ico-folder-open" aria-hidden="true"></span>',
  addProject: ICONS.plus,
  importProject: ICONS.plus,
  rename: '<span class="menu-icon-mask is-rename" aria-hidden="true"></span>',
  memory: '<span class="menu-icon-mask is-memory" aria-hidden="true"></span>',
  removeProject: ICONS.close,
  remove: '<span class="menu-icon-mask is-delete" aria-hidden="true"></span>',
});
const _workflowRepoMenuCache = new Map();

async function _workflowReposForMenu(domId, force = false) {
  if (!force && _workflowRepoMenuCache.has(domId)) return _workflowRepoMenuCache.get(domId);
  const result = await api(`/workbench/domains/${encodeURIComponent(domId)}/repos`, { timeout: 30000 });
  const repos = (result && result.repos) || [];
  _workflowRepoMenuCache.set(domId, repos);
  return repos;
}

function _openWorkflowProjectMenu(p, project, x, y) {
  const name = project.name || String(project.path || "").split("/").filter(Boolean).pop() || DeskI18n.t("wf.project");
  openCtxMenu(x, y, [
    { header: name, value: project.exists === false ? DeskI18n.t("wf.missingDir") : "" },
    { ico: WF_MENU_ICONS.project, label: DeskI18n.t("wf.openProjectFiles"), fn: () =>
      openFilesPage({ id: "abs:" + project.path, name }) },
    { ico: WF_MENU_ICONS.importProject, label: DeskI18n.t("wf.linkSessions"), fn: () =>
      _wfRepoImportPicker(project, p.id, null) },
    { ico: WF_MENU_ICONS.removeProject, label: DeskI18n.t("wf.removeProject"), danger: true, sep: true, fn: async () => {
      if (!await _wfRepoRemove(p.id, project)) return;
      _workflowRepoMenuCache.delete(p.id);
    } },
  ]);
}

async function _openWorkflowMenu(p, x, y) {
  let repos = [], unavailable = false;
  try { repos = await _workflowReposForMenu(p.id); } catch { unavailable = true; }
  const items = [
    { ico: WF_MENU_ICONS.conversation, label: DeskI18n.t("wf.newConversation"), fn: () => newTerminal(curEngine, p.id) },
    { header: DeskI18n.t("wf.projects"), sep: true,
      value: unavailable ? DeskI18n.t("wf.readFailed") : DeskI18n.t("wf.projectCount", { count: repos.length }) },
  ];
  if (repos.length) {
    for (const project of repos) {
      const name = project.name || String(project.path || "").split("/").filter(Boolean).pop() || DeskI18n.t("wf.project");
      items.push({ ico: WF_MENU_ICONS.project, label: name,
        value: project.exists === false ? DeskI18n.t("wf.missingDir") : "", chev: true,
        fn: () => _openWorkflowProjectMenu(p, project, x, y) });
    }
  } else {
    items.push({ label: DeskI18n.t(unavailable ? "wf.projectsUnavailable" : "wf.noProjects"), disabled: true });
  }
  items.push(
    { ico: WF_MENU_ICONS.addProject, label: DeskI18n.t("wf.addProject"), fn: async () => {
      if (await _wfRepoPick(null, p.id)) _workflowRepoMenuCache.delete(p.id);
    } },
    { ico: WF_MENU_ICONS.memory, label: DeskI18n.t("wf.memory"), sep: true, fn: () => openWorkflowMemory(p) },
    { ico: WF_MENU_ICONS.rename, label: DeskI18n.t("wf.rename"), fn: () => renameDomain(p) },
    { ico: WF_MENU_ICONS.project, label: DeskI18n.t("wf.openFiles"), fn: () => openFilesPage({ id: p.id, name: p.name }) },
    { ico: WF_MENU_ICONS.remove, label: DeskI18n.t("wf.delete"), danger: true, sep: true, fn: async () => {
      if (!confirm(DeskI18n.t("wf.deleteConfirm", { name: p.name }))) return;
      expandedProjects.delete(p.id); persistExpanded();
      try { await api(`/workbench/domains/${p.id}`, { method: "DELETE" }); } catch {}
      _workflowRepoMenuCache.delete(p.id);
      await reloadDomains();
      if (nav.type === "domain" && nav.id === p.id) selectNav("domain", DESKTOP);
      else renderSessionPanel();
    } },
  );
  openCtxMenu(x, y, items);
}

// ---- 两级导航项目树(重构:侧栏全局固定,不随 nav 换内容) ----
const _moreOpen = new Set();   // 「展开更多」展开态(按项目 id),跨重渲保留
const SESS_VISIBLE = 5;        // 每项目默认露出的会话行数,其余折进「展开更多」

// 侧栏项目列表:职能工作区 + 代码仓项目混排平铺;容器(专项,UI 已退役)/管理器/伪工作区不进树
function sideProjects() {
  return applyOrder(
    // 项目 / 容器 / coding / campaigns 全部退役:侧栏只列工作流
    domains.filter((d) => !d.kind && d.id !== DESKTOP
      && !hiddenDomains.includes(d.id)),
    wbState.sideProjOrder, (d) => d.id);
}

// 无归属聊天与工作流使用同一条新建会话路径,不再提供 Resume 列表入口。
function _openLooseChatMenu(x, y) {
  openCtxMenu(x, y, [
    { ico: WF_MENU_ICONS.conversation, label: DeskI18n.t("wf.newConversation"), fn: () => newTerminal(curEngine, DESKTOP) },
  ], "sidebar-action-menu");
}

function renderSessionPanel() {
  renderTaskDock();
  const el = $("#panel-list");
  el.innerHTML = "";
  ++_panelGen;
  // 项目实体已退役:侧栏只剩「工作流 + 聊天」两组;代码仓改由工作流的引用清单挂载,
  // 工作流组显示有归属或引用的会话；「聊天」只收纳没有工作流关联的会话。
  const all = sideProjects();
  for (const p of all) el.appendChild(projectFold(p));
  const lh = document.createElement("div");   // 无工作流关联的聊天；临时 shell 终端归终端应用
  lh.className = "side-sec loose-head"; lh.dataset.group = "chat";
  const moreIcoLoose = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" stroke="none"><circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/></svg>';
  lh.innerHTML = '<span>聊天</span>'
    + '<button class="sec-fold sec-more" type="button" title="聊天操作">' + moreIcoLoose + '</button>'
    + '<button class="sec-fold" type="button" title="折叠 / 展开"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></button>';
  DeskI18n.bind(lh.querySelector("span"), "nav.chat");
  DeskI18n.bind(lh.querySelector(".sec-more"), "nav.chatActions", {}, "title");
  DeskI18n.bind(lh.querySelector(".sec-fold:not(.sec-more)"), "nav.fold", {}, "title");
  el.appendChild(lh);
  const secMore = lh.querySelector(".sec-more");
  secMore.onclick = (ev) => {
    ev.stopPropagation();
    const rect = secMore.getBoundingClientRect();
    _openLooseChatMenu(rect.left, rect.bottom + 4);
  };
  lh.oncontextmenu = (ev) => {
    ev.preventDefault(); ev.stopPropagation();
    _openLooseChatMenu(ev.clientX, ev.clientY);
  };
  const loose = document.createElement("div");
  loose.className = "loose-sessions";
  loose.hidden = localStorage.getItem("cd_loose_collapsed") !== "0";   // 默认折叠:归在「聊天」的会话占多数,要看时手动展开(折叠态持久化)
  el.appendChild(loose);
  const secFold = lh.querySelector(".sec-fold:not(.sec-more)");   // 折叠钮是这一行的第二个钮,选择器要排掉「聊天操作」
  secFold.classList.toggle("folded", loose.hidden);
  lh.setAttribute("aria-expanded", String(!loose.hidden));
  const toggleLoose = () => {
    loose.hidden = !loose.hidden;
    secFold.classList.toggle("folded", loose.hidden);
    lh.setAttribute("aria-expanded", String(!loose.hidden));
    try { localStorage.setItem("cd_loose_collapsed", loose.hidden ? "1" : "0"); } catch {}
  };
  secFold.onclick = toggleLoose;
  bindSidebarSectionToggle(lh, toggleLoose);
  renderLooseSessions(loose);
  makeReorderable(el, "details.proj-fold", (d) => d.dataset.pid,
    (order) => { wbState.sideProjOrder = order; saveState({ sideProjOrder: order }); }, { rowSel: "summary" });
  refreshFoldCountsEnsure();
  startTermPoll();
  _sideHighlight(nav.type === "domain" && nav.id !== DESKTOP ? nav.id : null);
  applySideFilter();   // 搜索过滤态在重渲后重放
}

function projectFold(p) {
  const expanded = expandedProjects;
  const persist = persistExpanded;
  const det = document.createElement("details");
  det.className = "pfold proj-fold pf-work";   // 项目实体已退役,行只有工作流一种
  det.dataset.pid = p.id;
  det.dataset.countDom = p.id;
  // 「…」用细点(fill 小圆,无描边):ICONS.more 是 1.8 描边空心圆,视觉偏粗
  const moreIco = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" stroke="none"><circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/></svg>';
  // 工作流恢复原来的曲线四角星。
  // 行尾箭头与分组标题的折叠箭头同一几何(24 画布、1.8 描边);ICONS.enter 是 32 画布满幅大箭头,放这里明显偏大。
  const chevIco = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>';
  const typeIco = '<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor" stroke="none"><path d="M12 2c.6 5.5 4.5 9.4 10 10-5.5.6-9.4 4.5-10 10-.6-5.5-4.5-9.4-10-10 5.5-.6 9.4-4.5 10-10z"/></svg>';
  det.innerHTML = `<summary><span class="pf-ico">${typeIco}</span><span class="pf-name">${escapeHtml(p.name)}</span><span class="pf-btns"><button class="pg-add proj-more" title="工作流操作">${moreIco}</button></span><span class="pf-chev">${chevIco}</span></summary>`;
  // 折叠块下只列会话。任务不在这里——一列里两种东西并排,谁都不突出;任务改在会话视图
  // 输入框上沿的那一条看(hs-tasks),那里才是「一边聊一边对着任务」的位置。
  const body = document.createElement("div"); body.className = "proj-sessions";
  const sessHost = document.createElement("div"); sessHost.className = "pf-sess";
  body.appendChild(sessHost);
  det.appendChild(body);
  const summary = det.querySelector("summary");
  const moreBtn = det.querySelector(".proj-more");
  // 普通单击只展开会话；工作流与项目管理集中在右键 /「…」菜单。
  summary.onclick = (ev) => {
    ev.preventDefault();
    det.open = !det.open;
  };
  moreBtn.onclick = (ev) => {
    ev.preventDefault(); ev.stopPropagation();
    const r = moreBtn.getBoundingClientRect();
    _openWorkflowMenu(p, r.right, r.bottom);
  };
  summary.oncontextmenu = (ev) => {
    ev.preventDefault(); ev.stopPropagation();
    _openWorkflowMenu(p, ev.clientX, ev.clientY);
  };
  det.addEventListener("toggle", () => {
    if (det.open) { expanded.add(p.id); renderProjSessions(sessHost, p.id); }
    else expanded.delete(p.id);
    persist();
  });
  if (expanded.has(p.id)) det.open = true;   // listener 挂好后再展开:toggle 触发渲染
  return det;
}

function _revealWorkflowInSidebar(domId) {
  const det = [...document.querySelectorAll("#panel-list details.proj-fold")]
    .find((el) => el.dataset.pid === domId);
  if (!det) return false;
  if (!det.open) det.open = true;
  det.querySelector(":scope > summary")?.scrollIntoView({ block: "center", behavior: "smooth" });
  return true;
}

// 项目下应显示的会话行(按当前引擎隔离,与重构前口径一致;活跃在前休眠殿后;
// 未「展开更多」时截前 SESS_VISIBLE 条)
// 次序只由用户决定。后端那份列表是按最后落盘时间倒序给的,直接照着渲染的话,
// 谁刚回复谁就跳到最上面——读到一半的位置会在轮询之间自己变。这里在首次见到一段会话时
// 把它钉进 sessionOrder,之后它的位置就不再随活动变化,只有长按拖拽能改。
// 新会话钉在最前:刚开的那一段要看得见;钉住之后同样不再移动。
function _pinSessionOrder(domId, items) {
  let order = ((wbState.sessionOrder || {})[domId] || []).slice();
  // 清掉已经不存在的会话:这份记录会一直长,删掉的会话留在里面只是占位。
  // 比对的是全量 _termsList 而不是本工作流筛过的那一份——按后者清的话,切引擎时
  // 另一个引擎的会话会被当成「已消失」抹掉,切回来次序就散了。
  // 列表为空时不清:那多半是还没拉到,不是真的一段都没有了。
  const live = _termsList && _termsList.length ? new Set(_termsList.map(termOrderId)) : null;
  if (live && order.some((id) => !live.has(id))) order = order.filter((id) => live.has(id));
  const have = new Set(order);
  // 新见到的按后端给的次序(最近活动在前)插到最前,一次多条时保持它们之间的相对次序
  const fresh = items.map(termOrderId).filter((id) => id && !have.has(id));
  if (!fresh.length) {
    const cur = (wbState.sessionOrder || {})[domId] || [];
    if (order.length === cur.length) return order;   // 没新增也没清掉,不落盘
    wbState.sessionOrder = wbState.sessionOrder || {};
    wbState.sessionOrder[domId] = order;
    _pinSave(domId);
    return order;
  }
  const next = fresh.concat(order);
  wbState.sessionOrder = wbState.sessionOrder || {};
  wbState.sessionOrder[domId] = next;
  _pinSave(domId);
  return next;
}
// 落盘去抖:轮询每几秒跑一次,只有集合真的变了才会走到这里,但仍可能连着几条一起进来
let _pinTimer = 0;
const _pinDirty = new Set();
function _pinSave(domId) {
  _pinDirty.add(domId);
  clearTimeout(_pinTimer);
  _pinTimer = setTimeout(() => {
    const patch = {};
    for (const id of _pinDirty) patch[id] = ((wbState.sessionOrder || {})[id] || []).slice();
    _pinDirty.clear();
    // 自动发现新会话只发送“钉住”意图；服务端会保留另一端刚完成的手工排序。
    saveState({ sessionOrderPins: patch });
  }, 800);
}

function visibleSessionsFor(domId) {
  // 归属(domain)之外,还纳入被本工作流引用的会话(refs):引用不是归属,
  // 会话的家不变,工作流只是链接它。sessionBuckets 在列表换代时一次线性分桶，
  // 避免渲染 D 个工作流时每个工作流都重扫 N 条会话。
  const moreOpen = _moreOpen;
  const items = sessionBuckets().byDomain.get(domId) || [];
  const all = applyOrder(items, _pinSessionOrder(domId, items), termOrderId);
  // 不再把活会话提到前面:那也是一种自动排序,会让同一段会话在起停之间上下跳。
  // 「在跑的看得见」改由状态点承担(带晕、会呼吸),不靠位置表达。
  return { all, shown: moreOpen.has(domId) ? all : all.slice(0, SESS_VISIBLE) };
}

// quietEmpty:空集不画提示；列表只展示真实会话。创建动作留在工作流“…”菜单。
function renderProjSessions(container, domId, quietEmpty) {
  const moreOpen = _moreOpen;
  if (_termsList === null) { refreshTerms().then(() => { if (container.isConnected) renderProjSessions(container, domId, quietEmpty); }); return; }
  container.querySelectorAll(".term-item, .group-empty, .sess-more").forEach((x) => x.remove());
  const section = container.closest(".proj-sessions");
  const { all, shown } = visibleSessionsFor(domId);
  if (!all.length) {
    if (section) section.hidden = true;
    return;
  }
  if (section) section.hidden = false;
  for (const s of shown) container.appendChild(termRow(s, domId));
  const hiddenN = all.length - shown.length;
  // 卡片式容器(圆角卡)里,「展开显示」留在卡内会被读成一个条目——它不是一段会话,而是对
  // 这张卡的操作,所以放到卡片外(同「已归档」那处)。侧栏的列表不是卡,留在原处。
  if (hiddenN > 0 || moreOpen.has(domId)) {
    const open = moreOpen.has(domId);
    const more = document.createElement("div");
    more.className = "sess-more";
    more.textContent = open ? "收起" : `展开显示 ${hiddenN}`;
    more.onclick = (ev) => {
      ev.stopPropagation();
      if (moreOpen.has(domId)) moreOpen.delete(domId); else moreOpen.add(domId);
      renderProjSessions(container, domId, quietEmpty);
    };
    container.appendChild(more);
  }
  makeReorderable(container, ".term-item", (el2) => el2.dataset.orderId || el2.title, (order) => {
    wbState.sessionOrder = wbState.sessionOrder || {};
    const merged = mergeOrderedSubset(wbState.sessionOrder[domId], order);
    wbState.sessionOrder[domId] = merged;
    saveState({ sessionOrder: { [domId]: merged } });
  });
}

// ---- 侧栏工作流任务清单(TaskDock 式,替代工作流下的会话列表;设计稿 docs/workflow-task-model.md) ----
// 存储同 TaskDock:domains/<id>/TASKS.md,行级双校验;409 冲突以文件为准,重拉即可。
// 写回走 app-core 的 taskWrite(四个入口共用一份),这里只保留本视图的刷新。
// 登记进 hook 之后,别处(侧栏清单、TaskPaper、会话输入框)改完这一页也会跟着重绘。
taskViewRegister(async (domId) => {
  try { await _fetchTasks(domId); } catch {}
  refreshFoldCounts();
});
function _wfTasksPost(domId, bodyObj) { return taskWrite(domId, bodyObj); }
// 任务行的「新建会话」:会话仍按工作流展示,不恢复已下线的 s: 单归属或按任务分组；
// 但这次明确从某条任务发起,应在多对多任务账本里 claim。否则 recap 看不到稳定任务 id，
// 即使回合给出明确完成证据也没有权限关闭它。普通「新建会话」仍不关联任何任务。
async function _wfTaskNewSession(t, domId) {
  const s = await newTerminal(curEngine, domId);
  const sid = s && (s.id || s.sessionID);
  if (!sid || !t.t || t.state === "done") return s;
  const linked = await taskWrite(domId, {
    op: "ledger",
    change: { intent: "claim", task_id: t.t, title: t.title },
    session: { id: sid, engine: (s && s.engine) || curEngine, title: t.title },
    revision: t.ledger && t.ledger.revision,
  });
  if (!linked.ok) alert("会话已创建，但关联任务失败；请刷新任务后重试。");
  return s;
}
// 任务勾选框:形状按 unchecked 那枚空框(圆角方框,留白偏大),视口收成 16 让方框填满整个盒子——
// 盒子 13.5px,比任务名(14.5px)收一档:与字身等高时方框会压过标题,略小一点才退到辅助位。
// 圆角 3(边长的五分之一):圆到不硌眼,又还看得出是个方框——再大就往圆点那边靠,
// 会跟会话行首的圆点撞在一起。晕跟着外扩 0.75(半个描边宽),两枚外缘的曲率才一致。
// 进行中照会话圆点那套做:一圈淡晕紧包着实心块,中间不留缝——留缝就成了「框里放着一个小方块」,
// 读起来是两个东西;贴满才是「这一格被填上了」。内块的 x/y 取 3.5 = 外框起点 1 加半个晕宽(5)。会话行首是圆点,任务这一侧用方框——两类东西并排时,
// 形状不同才好一眼分开。三态只换框内的内容,外框始终在:
// 待办空框、进行中框内一枚小实心方块、已完成打勾。
// 内方块只占边长的三分之一:再大就读成「回」字,框与内容分不出主次。
// 内方块的几何仍与外框分层，颜色由 style.css 的 .wt-cb-in 使用主题感知的品牌紫。
function taskCbSvg(state) {
  // 三态共用同一个外框(描边方框,x=1 y=1 width=14 height=14 rx=3)。原来「进行中」
  // 单独画一块填充满格的色块当外缘,不走描边——那一版的外缘跟「未开始」的描边框长得
  // 不一样,读成了两种控件(2026-08-25 用户实见,要求外框对齐「未开始」)。
  // 内部标记几轮来回:9.5(顶到边)→5.5(嫌小)→6.5(「再略大一点」)→6.0(「改小一点」,
  // 6.5 又大了)→6.25(两端都被打回,取中)→5.5(2026-08-26 同一天又要求「改成 5.5
  // 试试」,回到当初嫌小的那一档再看一遍)。中心仍钉在 viewBox 中点(8,8):
  // x/y = 8 - 边长/2。
  //
  // 圆角 1.2 → 0.45:那圈缝原来不等宽,直边 2.71 CSS px、四角 3.51 px,角上宽 29%(实测;
  // 缝内底色两处都是 43,不是明暗渐变的错觉,是纯几何)。内块的圆角占自身半宽 44%,比外框
  // 内缘的 36% 更圆——一圈缝要等宽,内圈得是外圈等距内缩的形状,而缝宽 3.5 已经超过外框
  // 内缘的圆角半径 2.25,内缩到那个距离时圆角早收没了,等宽解恰好是个直角方块。留 1.2 的
  // 圆角等于在四条对角线上又削掉一块,缝于是在角上张开;一条沿直边平行、到转角张开的带子
  // 正是倒角的画法,内块因此看着像浮在一个台子上(2026-08-26 用户实见「缝隙有凸起的感觉」)。
  // 圆角先收到 0.45(四角多余量 10.0%→3.7%),2026-08-26 用户要求索性试直角:去掉 rx,
  // 缝就真的等宽了(0%)——直角方块正是那道等宽解本身,见上。
  const box = '<rect class="wt-cb-box" x="1" y="1" width="14" height="14" rx="3"/>';
  const inner = state === "done" ? '<polyline points="4.8 8.2 7.1 10.4 11.2 5.9"/>'
              : state === "doing" ? '<rect class="wt-cb-in" x="5.25" y="5.25" width="5.5" height="5.5" stroke="none"/>'
              : "";
  return '<svg viewBox="0 0 16 16" width="12.4" height="12.4" fill="none" stroke="currentColor"'
       + ' stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' + box + inner + '</svg>';
}

// 有工作流归属或引用的会话只在工作流下显示。
// 这里只筛选展示集合，不修改会话原始归属；临时 shell 终端归终端应用。
function looseSessions() {
  const items = sessionBuckets().loose;
  return applyOrder(items, _pinSessionOrder(DESKTOP, items), termOrderId);
}
function renderLooseSessions(container) {
  if (_termsList === null) { refreshTerms().then(() => { if (container.isConnected) renderLooseSessions(container); }); return; }
  container.querySelectorAll(".term-item, .group-empty").forEach((x) => x.remove());
  const list = looseSessions();
  if (!list.length) return;   // 固定「聊天」是唯一新建入口；列表只展示已经发送过的会话
  for (const s of list) container.appendChild(termRow(s, s.domain || DESKTOP));
  makeReorderable(container, ".term-item", (el) => el.dataset.orderId || el.title, (order) => {
    wbState.sessionOrder = wbState.sessionOrder || {};
    const merged = mergeOrderedSubset(wbState.sessionOrder[DESKTOP], order);
    wbState.sessionOrder[DESKTOP] = merged;
    saveState({ sessionOrder: { [DESKTOP]: merged } });
  });
}

// 会话行(项目树/无项目区共用):状态点 + 标题 + 分支 + 引擎小字 + 关闭。
// 交互与原 vibing 列一致:点=附着(休眠自动 resume),×=关闭(活 keep/休眠删),右键=重命名。
// 会话所在目录的短标:无归属聊天不标；工作流列表标来源。
// 落在工作流目录里的用工作流名(目录名是内部 id,给人看没意义),引用进来的用其目录名。
function termPathHTML(s, domId) {
  const cwd = s.cwd || "";
  if (!cwd || !domId || domId === DESKTOP) return "";
  let base;
  if (cwd.endsWith("/domains/" + domId)) {
    const d = (typeof domains !== "undefined" ? domains || [] : []).find((x) => x.id === domId);
    base = (d && d.name) || domId;
  } else {
    base = cwd.replace(/\/+$/, "").split("/").pop() || cwd;
  }
  return `<span class="ti-path" title="${escapeHtml(cwd)}">${escapeHtml(base)}</span>`;
}
function termRow(s, domId) {
  const r = document.createElement("div");
  const dormant = !s.alive && !s.busy;   // 回合跑着就不算未运行,哪怕没有常驻进程
  r.className = "session-item term-item" + (termMatchesId(s, activeTermId) ? " active" : "") + (dormant ? " dormant" : "") + (s.unread && !s.busy ? " unread" : "");
  // 标题后标出会话所在目录:项目实体退役后,来源靠路径分辨,不再靠分组
  r.innerHTML = `${termStatHTML(s)}<span class="ti-main"><span class="ti-label">${escapeHtml(stripTitleGlyph(s.title))}</span>${termPathHTML(s, domId)}${termBranchHTML(s)}${termRecordHTML(s)}</span>`;
  bindTermRowIdentity(r, s);   // 主 id 供轮询/排序，另外两种 id 供会话页选中匹配
  // 不挂 title:悬停已有卡片,卡片第一行就是标题,系统气泡是同一句话再说一遍,
  // 而且它比卡片晚出、位置又不受我们控制,两个浮层叠在一起。
  r.onclick = () => {
    // 临时终端不再进「聊天」组(见 looseSessions),正常路径下这里走不到;留作兜底:
    // 万一别处又渲染出 shell 行,点它要回到终端应用里它那张标签,而不是拿 "shell" 去 resume。
    if (s.engine === "shell") {
      openShellTab(s.id, stripTitleGlyph(s.title) || "终端");
      return;
    }
    if (typeof clearTermCompletion === "function") clearTermCompletion(s);
    // cwd 同样随页带过去：链接点击不应依赖侧栏清单此刻是否已经刷新完成。
    attachTerminal(s.id, s.engine, { domain: domId, cwd: s.cwd || "" });
    setTermHead(s);
    navPush({ kind: "sess", id: s.id, domain: domId });
    document.querySelectorAll(".term-item").forEach((x) => x.classList.toggle("active", termRowMatchesId(x, s.id)));
    // 侧栏的工作流高亮跟着走:点开一段会话之后,选中的应当是它所属的那个工作流,
    // 而不是上一次点过的那个。散会话(聊天组)没有归属,把高亮清掉。
    _sideHighlight(domId && domId !== DESKTOP ? domId : null);
    r.classList.remove("dormant"); r.querySelector(".ti-dot")?.remove();   // 休眠点开即拉活
  };
  r.oncontextmenu = (ev) => { ev.preventDefault(); ev.stopPropagation(); openTermCtxMenu(ev.clientX, ev.clientY, s, r, domId); };
  return r;
}
// dormant 参数保留旧签名:true=永久删除记录(即使有进程),false=只关闭进程、保留历史。
async function closeTermSession(s, dormant) {
  const msg = dormant ? `永久删除会话「${s.title || s.id}」？会物理删除该会话的历史和相关记录，不可恢复。`
                      : `关闭会话「${s.title || s.id}」的进程？会话历史保留，之后可重新打开。`;
  if (!confirm(msg)) return;
  const keep = dormant ? "" : "?keep=1";
  try {
    // 永久删除由后端同时清适配器索引、终端 meta 与引擎原生记录；前端不能拆成
    // 两个可各自失败的请求。失败时保留原行，用户可直接重试。
    await api(`/workbench/terminals/${s.id}${keep}`, { method: "DELETE" });
  } catch (e) {
    alertLike(`${dormant ? "删除" : "关闭"}失败：${e.message || "请稍后重试"}`);
    return;
  }
  disposeTermPane(s.id);
  // 临时终端在终端应用里还占着一张标签:会话都结束了,标签不该留着(删除已经发过,不再重复发)
  if (typeof termAppDropTab === "function") termAppDropTab(s.id);
  await refreshTerms();
  _rerenderSessionsHost();
}

// 主区终端头:当前附着会话的标题 + 实时 cwd 徽标(/workbench/term/cwd,2s 轮询)
let _termHeadSid = null;

// 主区没有会话在显示时收掉头部。整条藏起来而不是只清标题:标题旁边那几枚按钮
// (网页视图、文件、分享)都是对着某一段会话的,留着等于给一段不在显示的会话开着入口。
function clearTermHead() {
  const head = $("#term-head");
  if (head) head.hidden = true;
  const minimize = $("#term-min");
  if (minimize) minimize.hidden = true;
  const t = $("#term-title");
  if (t) t.textContent = "";
  _termHeadSid = null;
}
function setTermHead(s) {
  // 会话名 + 打开项目目录钮(开面文件夹,点开文件浏览页)
  const head = $("#term-head"); if (!head) return;
  head.hidden = false;
  const minimize = $("#term-min");
  if (minimize) minimize.hidden = false;
  document.getElementById("terminal")?.classList.remove("welcome-on");
  const back = head.querySelector("#term-back");
  if (back) back.remove();
  $("#term-title").textContent = stripTitleGlyph(s.title) || s.id;
  _termHeadSid = s.id;
  // 网页视图。能搬迁的引擎把「这一段」原样搬过去,不能的以网页视图新开一段——
  // 都不给的话,切了引擎就再没有进 GUI 的门。
  // 判据取引擎自报的能力位,不按引擎名写死:claude 读 transcript 拿历史,
  // codex 的 rollout 还没写,它自然落到新开那一档。
  const th = $("#term-tohtml");
  if (th) {
    th.hidden = false;
    th.title = "网页视图";
    th.onclick = async () => {
      let adoptable = false;
      try {
        const cf = await api(`/config?engine=${encodeURIComponent(s.engine || "")}`);
        adoptable = !!((cf || {}).caps || {}).adopt;
      } catch {}
      if (!adoptable) return newHtmlSession({ pref: true, domain: s.domain });
      // Codex 新终端先只有 tN 形式的 PTY id，首条消息前没有 rollout/thread id 可导入。
      // 点击时刷新一次；若已经捕获就搬同一段，否则空 TUI 没有历史可搬，关掉后直接建 GUI。
      let target = s.resume_id || s.id;
      if (engineIsCodexLike(s.engine) && !s.resume_id && /^t\d+$/.test(s.id || "")) {
        const list = await refreshTerms({ maxAge: 0 });
        const fresh = (list || []).find((x) => x.id === s.id);
        target = fresh && fresh.resume_id;
        if (!target) {
          try { await api(`/workbench/terminals/${encodeURIComponent(s.id)}`, { method: "DELETE" }); } catch {}
          disposeTermPane(s.id);
          await refreshTerms({ maxAge: 0 });
          renderSessionPanel();
          return newHtmlSession({ pref: true, domain: s.domain });
        }
      }
      return termToHtml(target, { engine: s.engine });
    };
  }
  try { renderTermToc(s); } catch {}   // 左缘会话刻度条跟着当前会话走
  let fb = head.querySelector("#term-files");
  if (!fb) {
    fb = document.createElement("button");
    fb.id = "term-files"; fb.type = "button"; fb.title = "打开工作目录";
    fb.innerHTML = '<span class="ico-share"></span>';
    head.appendChild(fb);
  }
  // 目录钮开的是**会话自己的工作目录**(归属只做分组、工作目录跟会话走,见 0ac33df)。
  // 能落到域 id 的就落,不要一律用 abs:——文件页按 id 建标签并持久化浏览态,同一目录走两种 id
  // 会开出两个标签、各存一份状态(左栏「打开工作流文件」用的就是域 id)。四档依次退让:
  //   1) cwd 命中某条域记录的 dir(项目实体若回归时生效);
  //   2) cwd 形如 .../domains/<域 id> → 它就是该工作流自己的目录,用域 id;
  //   3) 否则按绝对路径开(abs: 是 /workbench/fs 支持的 root 形式,与"浏览目录"同一条路);
  //   4) 会话没带 cwd(旧会话)→ 回退到归属目录,domains 未加载时用 domain 本身兜底,
  //      免得按钮因"查不到"整个消失(查得到但开不了更好排查)。
  const list = (typeof domains !== "undefined" ? domains || [] : []);
  const trim = (p) => String(p || "").replace(/\/+$/, "");
  const named = (id) => ({ id, name: (list.find((x) => x.id === id) || {}).name || id });
  const cwd = trim(s.cwd);
  let target = null;
  if (cwd) {
    const byDir = list.find((x) => trim(x.dir) && trim(x.dir) === cwd);
    const m = /\/domains\/([\w:-]+)$/.exec(cwd);
    if (byDir) target = named(byDir.id);
    else if (m && list.some((x) => x.id === m[1])) target = named(m[1]);
    else target = { id: "abs:" + cwd, name: cwd.split("/").pop() || cwd };
  } else {
    const dom = s.domain || DESKTOP;
    const d = list.find((x) => x.id === dom) || (dom !== DESKTOP ? { id: dom, name: dom } : null);
    if (d && d.id !== DESKTOP) target = named(d.id);
  }
  fb.hidden = !target;
  fb.onclick = target ? () => openFilesPage(target) : null;
  // 原「收回工作流页内嵌」钮(#term-shrink)随内嵌展开一并下线;收起改用右上角常驻的 #term-min。
  // 旧节点可能还挂在复用的 head 上,遇到就摘掉,免得和 #term-min 在右上角叠成两个图标。
  head.querySelector("#term-shrink")?.remove();
}

function workspaceRow(d) {
  const row = document.createElement("div");
  row.className = "asset-item ws-row";
  const glyph = ICONS[d.icon] || "";
  row.innerHTML = `<span class="ws-glyph">${glyph}<button class="ws-hide" title="从 desktop 移除（可恢复，不删官方定义）">${ICONS.hide}</button></span><div class="ws-text"><div class="ai-title">${escapeHtml(d.name)}</div>${d.desc ? `<div class="ai-sub">${escapeHtml(d.desc)}</div>` : ""}</div><button class="ws-enter" title="进入工作区">${ICONS.enter}</button>`;
  row.onclick = () => _revealWorkflowInSidebar(d.id);
  row.querySelector(".ws-enter").onclick = (ev) => { ev.stopPropagation(); selectNav("domain", d.id); };   // 箭头=进入工作区
  row.querySelector(".ws-hide").onclick = (ev) => { ev.stopPropagation(); hideDomain(d.id); };   // 名字后的眼睛=隐藏
  wireDomainCtx(row, d);   // 管理器不改名;职能工作区右键重命名
  return row;
}
async function hideDomain(id) {
  try { await api("/workbench/hidden", { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ id, hidden: true }) }); } catch (e) { alert("移除失败：" + e.message); return; }
  await reloadDomains();
  if (nav.type === "domain" && nav.id === id) selectNav("domain", DESKTOP);   // 正看着它就退回 desktop
  else renderSessionPanel();
}
async function unhideDomain(id) {
  try { await api("/workbench/hidden", { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ id, hidden: false }) }); } catch (e) { alert("恢复失败：" + e.message); return; }
  await reloadDomains();
  renderSessionPanel();
}
function hiddenWorkspaces(list) {
  // 已隐藏工作区:折叠,逐条可恢复。展开态跨重渲染保留(恢复一条不收起)
  const det = document.createElement("details");
  det.className = "hidden-ws";
  det.innerHTML = `<summary>已隐藏（${list.length}）</summary>`;
  det.open = hiddenFoldOpen;
  det.addEventListener("toggle", () => { hiddenFoldOpen = det.open; });
  for (const dom of list) {
    const r = document.createElement("div");
    r.className = "asset-item ws-row hidden-row";
    r.innerHTML = `<span class="ws-glyph">${ICONS[dom.icon] || ""}</span><div class="ws-text"><div class="ai-title">${escapeHtml(dom.name)}</div></div><button class="ws-enter" title="恢复到 desktop">${ICONS.resume}</button>`;
    r.querySelector(".ws-enter").onclick = (ev) => { ev.stopPropagation(); unhideDomain(dom.id); };
    det.appendChild(r);
  }
  return det;
}
