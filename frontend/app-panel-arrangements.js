// “安排”是 Desk 定时调度的统一入口。它不聚合工作流 TASKS.md，也不镜像飞书消息触发：
// 已有飞书 Bot 可作为执行配置绑定；结果仍回到安排。系统任务展示记忆引擎的运行状态和详情。
const _ARR_ICONS = {
  down: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"12\" height=\"12\" class=\"desk-reviewed-icon\" data-desk-icon=\"_ARR_ICONS.down\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"m6.8 11.8 8.3 8.8a1.25 1.25 0 0 0 1.8 0l8.3-8.8\" /></g></svg>",
  search: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"_ARR_ICONS.search\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><circle cx=\"13.7\" cy=\"13.7\" r=\"8.7\" /><path d=\"m20.1 20.1 6.7 6.7\" /></g></svg>",
  calendar: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"_ARR_ICONS.calendar\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"4.9\" y=\"6.8\" width=\"22.2\" height=\"20.2\" rx=\"4.3\" /><path d=\"M10.1 4.2v5.4M21.9 4.2v5.4M5.3 13.1h21.4M10.4 18.4h3.1M18.5 18.4h3.1M10.4 22.8h3.1\" /></g></svg>",
  chev: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"_ARR_ICONS.chev\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"m11.8 6.8 8.8 8.3a1.25 1.25 0 0 1 0 1.8l-8.8 8.3\" /></g></svg>",
  close: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"_ARR_ICONS.close\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"m7.2 7.2 17.6 17.6M24.8 7.2 7.2 24.8\" /></g></svg>",
  who: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"_ARR_ICONS.who\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><circle cx=\"16\" cy=\"10.1\" r=\"5.2\" /><path d=\"M5.7 27v-1.2a8 8 0 0 1 8-8h4.6a8 8 0 0 1 8 8V27\" /></g></svg>",
};
const _ARR_DEFAULT_AGENT = "_desk";
const _ARR_CREATE_PROMPT = "帮我创建一个安排。请使用 make-arrangement skill：先通过对话确认每次要做什么、何时执行；信息不足时继续问我，确认后再创建。默认由 Desk 执行，不需要先创建 Agent。";
let _arrCache = { items: [], peers: [], agents: null, system: null, backend: "desktop", error: "" };
let _arrFilter = "all", _arrQuery = "", _arrSeq = 0, _arrTimer = 0, _arrSystemKind = "";
const _arrOpen = new Set();
const _ARR_ENVIRONMENTS = {
  local: { label: "本地", backend: "desktop", description: "在本地电脑执行的安排。", empty: "还没有本地安排" },
};
let _arrSection = "local";
const _arrSections = Object.fromEntries(["local", "system"].map((key) => [key, { query: "", filter: "all", top: 0 }]));
function _arrEnvironmentAvailable() {
  const env = _ARR_ENVIRONMENTS[_arrSection];
  return !!env && !_arrCache.error && (env.backend === _arrCache.backend
    || (_arrCache.peers || []).some((peer) => peer.name === env.backend && peer.ok));
}
function _arrSelectSection(section) {
  if (!_arrSections[section] || (section === _arrSection && !_arrSystemKind)) return;
  const scroll = _resourceScroll("arrangements");
  _arrSections[_arrSection] = { query: _arrQuery, filter: _arrFilter, top: scroll.scrollTop };
  _arrSection = section; _arrQuery = _arrSections[section].query; _arrFilter = _arrSections[section].filter; _arrSystemKind = "";
  _arrPaint(); scroll.scrollTop = _arrSections[section].top;
  _resourcePaintNav();
}

function _arrAt(backend) {
  return backend ? `?backend=${encodeURIComponent(backend)}` : "";
}
function _arrAgo(ts) {
  if (!ts) return "还没有运行";
  const d = Math.max(0, Date.now() / 1000 - Number(ts));
  if (d < 90) return "刚刚";
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  if (d < 172800) return "昨天";
  return `${Math.floor(d / 86400)} 天前`;
}
function _arrNext(ts) {
  if (!ts) return DeskI18n.t("arr.manualOnly");
  const d = Number(ts) * 1000 - Date.now();
  if (d <= 60000) return DeskI18n.t("arr.soon");
  if (d < 3600000) return DeskI18n.t("arr.inMinutes", { n: Math.ceil(d / 60000) });
  if (d < 86400000) return DeskI18n.t("arr.inHours", { n: Math.ceil(d / 3600000) });
  const x = new Date(Number(ts) * 1000);
  const time = `${String(x.getHours()).padStart(2, "0")}:${String(x.getMinutes()).padStart(2, "0")}`;
  // zh-CN 的 short 月份即「10月15日」，与原先手写格式一致；其他语言按各自习惯。
  return x.toLocaleDateString(DeskI18n.tag, { month: "short", day: "numeric" }) + " " + time;
}
function _arrTrigger(t) {
  t = t || { kind: "manual" };
  if (t.kind === "custom") {
    const unit = {day: "天", week: "周", month: "个月"}[t.unit] || "天";
    const days = t.unit === "week" ? `的${(t.weekdays || []).map((d) => "周" + "一二三四五六日"[d]).join("、")}` : "";
    const monthly = t.unit === "month" ? `（${Number((t.start_date || "").slice(-2))} 日，无此日期则在月末）` : "";
    return `每 ${t.every} ${unit}${days}${monthly} ${t.time} · ${t.start_date} 起 · ${t.end_date ? "截至 " + t.end_date + "（含当天）" : "永不截止"}`;
  }
  if (t.kind === "once") return `${t.date} ${t.time} · 仅一次`;
  if (t.kind === "interval") return `每隔 ${t.minutes} 分钟`;
  if (t.kind === "daily") return `每天 ${t.time}`;
  if (t.kind === "weekdays") return `工作日 ${t.time}`;
  if (t.kind === "weekly") return `每周${"一二三四五六日"[Number(t.weekday) || 0]} ${t.time}`;
  return "仅手动";
}
function _arrUsed(used) {
  const parts = [];
  for (const [k, v] of Object.entries(used || {})) {
    if (Array.isArray(v) && v.length) parts.push(`${k} ${v.length} 条`);
    else if (typeof v === "number" && v) parts.push(`${k} ${v} 条`);
  }
  return parts.length ? `读取 ${parts.join("、")}` : "未记录知识读取";
}
function _arrRows() {
  const b = _arrCache.agents;
  const names = {}, avatars = {};
  for (const a of ((b && b.items) || [])) {
    const key = `${a.backend || "desktop"}/${a.id}`;
    names[key] = a.name || a.id;
    avatars[key] = a.avatar || "";
  }
  const schedules = (_arrCache.items || []).map((x) => ({ ...x, row_kind: "schedule",
    agent_name: (x.agent_id || _ARR_DEFAULT_AGENT) === _ARR_DEFAULT_AGENT
      ? "Desktron" : (names[`${x.backend || "desktop"}/${x.agent_id}`] || x.agent_id),
    agent_avatar: avatars[`${x.backend || "desktop"}/${x.agent_id}`] || "" }));
  return schedules;
}
function _arrIconHtml(x) {
  const fallback = _ARR_ICONS.calendar;
  const avatar = x.agent_avatar;
  if (!avatar) return `<span class="arr-icon">${fallback}</span>`;
  return `<span class="arr-icon has-avatar"><img src="${escapeHtml(avatar)}" alt="" loading="lazy" onerror="this.parentElement.classList.remove('has-avatar');this.remove()">${fallback}</span>`;
}
function _arrRowHtml(x) {
  const key = `arr:${x.backend || "desktop"}:${x.id}`;
  const open = _arrOpen.has(key);
  const last = x.last_run || {};
  const failed = last.status === "failed";
  const paused = !x.enabled;
  const attention = failed;
  const lead = paused ? "已暂停" : (failed ? "需处理"
    : (["once", "custom"].includes(x.trigger?.kind) && !x.next_at ? "定时已结束" : _arrNext(x.next_at)));
  const sub = `${x.agent_name || x.agent_id} · ${_arrTrigger(x.trigger)}`;
  const status = last.status === "running" || last.status === "queued" ? "正在运行"
    : last.status ? `${last.status === "succeeded" ? "上次成功" : "上次失败"} · ${_arrAgo(last.finished_at || last.started_at)}` : "等待首次运行";
  const search = `${x.name || x.id} ${x.agent_id || ""} ${sub}`.toLowerCase();
  const hidden = (_arrQuery && !search.includes(_arrQuery))
    || (_arrFilter === "attention" && !attention)
    || (_arrFilter === "paused" && !paused);
  const detail = `<div class="arr-detail-grid"><div><span>每次运行</span><b>${escapeHtml(x.instruction || "")}</b></div><div><span>运行位置</span><b>${escapeHtml(x.backend || "desktop")} · ${escapeHtml(x.agent_id || "")}</b></div><div><span>触发</span><b>${escapeHtml(_arrTrigger(x.trigger))}</b></div><div><span>结果</span><b>回到安排，不自动外发</b></div></div>${last.id ? `<div class="arr-result"><span>最近一次</span><b>${escapeHtml(last.error || last.output || _arrUsed(last.used))}</b></div>` : ""}<div class="arr-actions"><button data-act="run">立即运行</button><button data-act="toggle">${paused ? "恢复" : "暂停"}</button><button data-act="edit">配置</button>${last.id ? '<button data-act="result">查看结果</button>' : ""}<button class="danger" data-act="delete">删除</button></div>`;
  return `<article class="arr-row${open ? " open" : ""}${attention ? " attention" : ""}" data-key="${escapeHtml(key)}" data-id="${escapeHtml(x.id)}" data-backend="${escapeHtml(x.backend || "")}"${hidden ? " hidden" : ""}><div class="arr-main" role="button" tabindex="0" aria-expanded="${open}">${_arrIconHtml(x)}<span class="arr-copy"><b>${escapeHtml(x.name)}</b><span>${escapeHtml(sub)}</span></span><span class="arr-when"><b>${escapeHtml(lead)}</b><span>${escapeHtml(status)}</span></span><span class="arr-chev">${_ARR_ICONS.chev}</span></div><div class="arr-detail">${detail}</div></article>`;
}
function _arrPaint() {
  const body = _resourceBody("arrangements");
  if (!body || !body.classList.contains("arrangements-page")) return;
  body._resourceBack = null;
  const system = _arrSection === "system";
  const env = _ARR_ENVIRONMENTS[_arrSection];
  const allRows = _arrRows();
  const rows = env ? allRows.filter((x) => (x.backend || "desktop") === env.backend) : [];
  const available = _arrEnvironmentAvailable();
  const peer = env && (_arrCache.peers || []).find((p) => p.name === env.backend);
  const environmentError = env && !available ? (peer ? `${env.label}连接失败` : `尚未连接${env.label}`) : "";
  const systemCards = _arrSysCards(_arrCache.system || {}).filter((c) => c.kind === "memory");
  if (_arrSystemKind) {
    const card = systemCards.find((c) => c.kind === _arrSystemKind);
    if (card) {
      body._resourceBack = () => { _arrSystemKind = ""; _arrPaint(); };
      _arrSysRenderResource(body, card, body._resourceBack, _arrReloadSystem, {
        bodyClass: "md wf-page arrangements-page arr-sys-resource-page", backLabel: "系统安排",
      });
      _resourceSyncSearch();
      return;
    }
    _arrSystemKind = "";
  }
  body.className = "md wf-page arrangements-page";
  const visible = rows.filter((x) => {
    const last = x.last_run || {};
    const attention = last.status === "failed";
    const paused = !x.enabled;
    const search = `${x.name || x.id} ${x.agent_id || ""} ${_arrTrigger(x.trigger)}`.toLowerCase();
    return (!_arrQuery || search.includes(_arrQuery)) && (_arrFilter === "all" || (_arrFilter === "attention" && attention) || (_arrFilter === "paused" && paused));
  });
  const nexts = rows.filter((x) => x.enabled && x.next_at).map((x) => x.next_at);
  const summary = _arrCache.error ? `读取失败：${_arrCache.error}`
    : environmentError ? environmentError
    : `${rows.length} 个安排${nexts.length ? ` · 下次运行 ${_arrNext(Math.min(...nexts))}` : ""}`;
  const running = systemCards.filter((c) => c.running).length;
  const matchingSystem = systemCards.filter((c) => (!_arrQuery || JSON.stringify(c).toLowerCase().includes(_arrQuery))
    && (_arrFilter === "all" || (_arrFilter === "attention" && c.stateClass === "warn") || (_arrFilter === "paused" && !c.running)));
  const systemHtml = systemCards.length ? matchingSystem.map(_arrSysCard).join("") || '<div class="wf-empty">没有匹配的系统安排</div>'
    : '<div class="wf-empty">状态接口不可用：重启 Desk 后这里会显示记忆引擎</div>';
  const create = '<button class="arr-primary" data-new><span>新建安排</span>' + _ARR_ICONS.down + '</button>';
  const search = `<label class="arr-search">${_ARR_ICONS.search}<input placeholder="${system ? "搜索系统安排" : `搜索${env.label}安排`}" spellcheck="false" value="${escapeHtml(_arrQuery)}"></label>`;
  const filters = [["all", "全部"], ["attention", "需处理"], ["paused", system ? "已停止" : "已暂停"]];
  const toolbar = `<div class="arr-toolbar">${search}<div class="arr-filters">${filters.map(([key, label]) =>
    `<button type="button" data-filter="${key}" class="${_arrFilter === key ? "on" : ""}" aria-pressed="${_arrFilter === key}">${label}</button>`).join("")}</div></div>`;
  body.innerHTML = _resourceHeader(system ? "系统安排" : env.label,
    system ? "查看跟随 Desk 运行的任务及其执行状态。" : env.description, available ? create : "")
    + toolbar
    + (system ? `<section class="arr-system-agents"><div class="arr-list-head"><b>随 Desk 运行</b><span>${running} 运行 · ${systemCards.length - running} 停止</span></div><div id="arr-system-agents">${systemHtml}</div></section>`
      : `<div class="arr-list-head"><b>定时与手动安排</b><span>${escapeHtml(summary)}</span></div><div class="arr-list">${rows.length ? rows.map(_arrRowHtml).join("") : `<div class="arr-empty">${escapeHtml(environmentError || env.empty)}</div>`}</div>${rows.length && !visible.length ? '<div class="arr-no-match">没有匹配的安排。</div>' : ""}`);
  const newBtn = body.querySelector("[data-new]");
  if (newBtn) newBtn.onclick = () => openAddMenu(newBtn, [
    { label: "用 Desk 创建", desc: "在聊天中说明需求，由 Desk 帮你设置", fn: _arrCreateWithDesk },
    { label: "手动设置", desc: "直接填写执行内容与时间", fn: () => _arrEditSheet() },
  ]);
  const inp = body.querySelector(".arr-search input");
  let composing = false;
  const commitQuery = () => { _arrQuery = inp.value.trim().toLowerCase(); _arrPaint(); const n = body.querySelector(".arr-search input"); if (n?.getClientRects().length) { n.focus(); n.setSelectionRange(n.value.length, n.value.length); } };
  inp.oncompositionstart = () => { composing = true; };
  inp.oncompositionend = () => { composing = false; commitQuery(); };
  inp.oninput = (e) => { if (!composing && !e.isComposing) commitQuery(); };
  body.querySelectorAll("[data-filter]").forEach((b) => { b.onclick = () => { _arrFilter = b.dataset.filter; _arrPaint(); }; });
  body.querySelectorAll("#arr-system-agents .arr-sys-agent").forEach((el) => {
    el.querySelector(".arr-sys-head").onclick = () => { _arrSystemKind = el.dataset.kind; _arrPaint(); _resourceScroll("arrangements").scrollTop = 0; };
  });
  body.querySelectorAll(".arr-row").forEach((row) => {
    const main = row.querySelector(".arr-main");
    const toggle = () => { _arrOpen.has(row.dataset.key) ? _arrOpen.delete(row.dataset.key) : _arrOpen.add(row.dataset.key); _arrPaint(); };
    main.onclick = toggle;
    main.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } };
    row.querySelectorAll("[data-act]").forEach((b) => { b.onclick = (e) => { e.stopPropagation(); _arrAction(b.dataset.act, row); }; });
  });
  _resourceSyncSearch();
}
async function _arrReloadSystem() {
  try { _arrCache.system = await api("/workbench/agents/status", { timeout: 20000 }); } catch {}
  _arrPaint();
}
async function _arrLoad(seq) {
  const results = await Promise.allSettled([
    api("/workbench/arrangements", { timeout: 20000 }),
    api("/workbench/lark-bot/agents", { timeout: 20000 }),
    api("/workbench/agents/status", { timeout: 20000 }),
  ]);
  if (seq !== _arrSeq) return;
  _arrCache.error = "";
  if (results[0].status === "fulfilled") {
    _arrCache.items = results[0].value.items || [];
    _arrCache.backend = results[0].value.backend || "desktop";
    _arrCache.peers = results[0].value.peers || [];
  }
  else _arrCache.error = results[0].reason.message || "取不到安排";
  if (results[1].status === "fulfilled") {
    const data = results[1].value;
    _arrCache.agents = data;
  }
  if (results[2].status === "fulfilled") {
    _arrCache.system = results[2].value;
  }
  _arrPaint();
}
function openArrangementsPage(repaint) {
  if (repaint && !_resourceIsOpen("arrangements")) return;
  if (!repaint) {
    const { body } = openResourcePage("arrangements", "安排");
    body.className = "md wf-page arrangements-page";
  }
  _arrPaint();
  const seq = ++_arrSeq;
  _arrLoad(seq);
  clearTimeout(_arrTimer);
  _arrTimer = setTimeout(() => {
    if (_resourceIsOpen("arrangements")) openArrangementsPage(true);
  }, 10000);
}
async function _arrAction(act, row) {
  const item = (_arrCache.items || []).find((x) => x.id === row.dataset.id && (x.backend || "") === row.dataset.backend);
  if (!item) return;
  const at = _arrAt(item.backend);
  try {
    if (act === "edit") { _arrEditSheet(item); return; }
    if (act === "delete") {
      if (!confirm(`删除“${item.name}”？运行记录会保留。`)) return;
      await api(`/workbench/arrangements/${encodeURIComponent(item.id)}${at}`, { method: "DELETE" });
      _deskNotice("安排已删除");
    } else if (act === "toggle") {
      await api(`/workbench/arrangements/${encodeURIComponent(item.id)}/toggle${at}`, { method: "POST" });
      _deskNotice(item.enabled ? "安排已暂停" : "安排已恢复");
    } else if (act === "run") {
      const r = await api(`/workbench/arrangements/${encodeURIComponent(item.id)}/run${at}`, { method: "POST", timeout: 20000 });
      _deskNotice("已开始运行，结果会回到这里");
      if (r.run) _arrWatchRun(r.run.id, item.backend, true);
    } else if (act === "result" && item.last_run) {
      _arrResultSheet(item.last_run.id, item.backend, item.name);
      return;
    }
    openArrangementsPage(true);
  } catch (e) { _deskNotice(e.message || "操作失败"); }
}
function _arrCloseSheet(sheet) {
  if (!sheet) return;
  document.removeEventListener("keydown", sheet._esc);
  sheet.remove();
}
function _arrSheet(title, sub) {
  document.querySelector(".arr-modal")?.remove();
  const m = document.createElement("div");
  m.className = "arr-modal";
  m.innerHTML = `<div class="arr-backdrop"></div><section class="arr-sheet" role="dialog" aria-modal="true"><div class="arr-sheet-head"><div><h2>${escapeHtml(title)}</h2>${sub ? `<p>${escapeHtml(sub)}</p>` : ""}</div><button class="arr-close" aria-label="关闭">${_ARR_ICONS.close}</button></div><div class="arr-sheet-body"></div></section>`;
  m._esc = (e) => { if (e.key === "Escape") _arrCloseSheet(m); };
  document.addEventListener("keydown", m._esc);
  m.querySelector(".arr-backdrop").onclick = () => _arrCloseSheet(m);
  m.querySelector(".arr-close").onclick = () => _arrCloseSheet(m);
  document.body.appendChild(m);
  return m;
}
async function _arrCreateWithDesk() {
  if (!_arrEnvironmentAvailable()) return;
  const env = _ARR_ENVIRONMENTS[_arrSection];
  try {
    const made = await newHtmlSessionWithPrompt({ domain: DESKTOP }, `${_ARR_CREATE_PROMPT}\n执行环境为${env.label}，目标运行端为 ${env.backend}，请在该端创建安排。`);
    if (!made) _deskNotice("没有创建会话");
  } catch (e) { _deskNotice(e.message || "创建会话失败"); }
}
function _arrAgentOptions(selected, onlyBackend) {
  const source = _arrCache.agents || {};
  const backends = new Set([onlyBackend || source.backend || "desktop"]);
  if (!onlyBackend) {
    for (const peer of (source.peers || [])) if (peer.ok && peer.name) backends.add(peer.name);
    for (const item of (source.items || [])) backends.add(item.backend || "desktop");
    for (const item of (_arrCache.items || [])) backends.add(item.backend || "desktop");
  }
  const defaults = [...backends].map((backend) => {
    const key = `${backend}::${_ARR_DEFAULT_AGENT}`;
    return `<option value="${escapeHtml(key)}"${key === selected ? " selected" : ""}>Desktron · ${escapeHtml(backend)}</option>`;
  }).join("");
  const items = (source.items || [])
    .filter((a) => !onlyBackend || (a.backend || "desktop") === onlyBackend);
  const options = (rows) => rows.map((a) => {
    const key = `${a.backend || "desktop"}::${a.id}`;
    return `<option value="${escapeHtml(key)}"${key === selected ? " selected" : ""}${a.enabled === false ? " disabled" : ""}>${escapeHtml(a.name || a.id)} · ${escapeHtml(a.backend || "desktop")}${a.enabled === false ? "（已停用）" : ""}</option>`;
  }).join("");
  const agents = options(items.filter((a) => a.kind === "agent"));
  const bots = options(items.filter((a) => a.kind !== "agent"));
  const missing = selected && ![...backends].some((b) => `${b}::${_ARR_DEFAULT_AGENT}` === selected)
    && !items.some((a) => `${a.backend || "desktop"}::${a.id}` === selected);
  return `<optgroup label="Desk（默认）">${defaults}</optgroup>`
    + (agents ? `<optgroup label="我的 Agent（可选）">${agents}</optgroup>` : "")
    + (bots ? `<optgroup label="已有飞书 Bot">${bots}</optgroup>` : "")
    + (missing ? `<option value="${escapeHtml(selected)}" selected disabled>原执行者不可用 · ${escapeHtml(selected)}</option>` : "");
}
function _arrFormTrigger(form) {
  const kind = form.elements.kind.value;
  const trigger = { kind };
  if (!["manual", "interval"].includes(kind)) trigger.time = form.elements.time.value;
  if (kind === "once") trigger.date = form.elements.date.value;
  if (kind === "interval") trigger.minutes = Number(form.elements.minutes.value);
  if (kind === "weekly") trigger.weekday = Number(form.elements.weekday.value);
  if (kind === "custom") {
    trigger.unit = form.elements.repeat_unit.value;
    trigger.every = Number(form.elements.repeat_every.value);
    trigger.start_date = form.elements.start_date.value;
    if (trigger.unit === "week") trigger.weekdays = [...form.querySelectorAll('[name="repeat_day"]:checked')].map((el) => Number(el.value)).sort((a, b) => a - b);
    if (form.elements.ending.value === "date") trigger.end_date = form.elements.end_date.value;
  }
  return trigger;
}

function _arrToday() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function _arrEditSheet(item, options) {
  options = options || {};
  // 执行者复用已有配置；调度条件单独呈现，试运行不作为保存前置。
  const editing = !!item;
  if (!editing && !_arrEnvironmentAvailable()) return;
  const defaultBackend = _ARR_ENVIRONMENTS[_arrSection]?.backend || _arrCache.backend || "desktop";
  const selected = editing
    ? `${item.backend || "desktop"}::${item.agent_id || _ARR_DEFAULT_AGENT}`
    : `${defaultBackend}::${_ARR_DEFAULT_AGENT}`;
  const trig = (item && item.trigger) || { kind: "manual" };
  const m = _arrSheet(editing ? "配置安排" : "新建安排");
  const host = m.querySelector(".arr-sheet-body");
  host.innerHTML = `<form class="arr-form">`
    + `<div class="arr-field full"><label for="arr-instruction">要做什么</label>`
    + `<div class="arr-compose"><textarea id="arr-instruction" name="instruction" required placeholder="例如：汇总我的日程与未读消息，标出需要处理的。">${escapeHtml((item && item.instruction) || "")}</textarea>`
    + `<div class="arr-compose-foot"><label class="arr-chip" title="谁来做">${_ARR_ICONS.who}<select name="agent" aria-label="谁来做">${_arrAgentOptions(selected, editing ? (item.backend || "desktop") : defaultBackend)}</select></label></div></div></div>`
    + `<div class="arr-freq"><label for="arr-kind">频率</label><select id="arr-kind" name="kind"><option value="manual">仅手动</option><option value="once">指定日期，仅一次</option><option value="daily">每天</option><option value="weekdays">工作日</option><option value="weekly">每周</option><option value="interval">每隔一段时间</option><option value="custom">自定义重复</option></select></div>`
    + `<div class="arr-freq arr-timing"><span>执行时间</span>`
    + `<input name="date" type="date" aria-label="执行日期" value="${escapeHtml(trig.date || "")}">`
    + `<select name="weekday" aria-label="星期">${"一二三四五六日".split("").map((x, i) => `<option value="${i}"${Number(trig.weekday) === i ? " selected" : ""}>星期${x}</option>`).join("")}</select>`
    + `<input name="time" type="time" aria-label="执行时间" step="60" value="${escapeHtml(trig.time || "09:00")}">`
    + `<input name="minutes" type="number" aria-label="间隔分钟数" min="1" max="10080" step="1" value="${escapeHtml(String(trig.minutes || 60))}"><span class="arr-interval-unit">分钟</span></div>`
    + `<fieldset class="arr-repeat" hidden><legend>自定义重复</legend>`
    + `<div class="arr-freq"><label for="arr-repeat-every">重复频率</label><span class="arr-repeat-rate">每 <input id="arr-repeat-every" name="repeat_every" type="number" min="1" max="999" step="1" value="${escapeHtml(String(trig.every || 1))}"><select name="repeat_unit" aria-label="重复单位"><option value="day">天</option><option value="week">周</option><option value="month">月</option></select></span></div>`
    + `<div class="arr-repeat-days" role="group" aria-label="每周执行日期">${[6, 0, 1, 2, 3, 4, 5].map((d) => `<label><input type="checkbox" name="repeat_day" value="${d}"${(trig.weekdays || [new Date().getDay() === 0 ? 6 : new Date().getDay() - 1]).includes(d) ? " checked" : ""}><span>${"一二三四五六日"[d]}</span></label>`).join("")}</div>`
    + `<div class="arr-freq"><label for="arr-start-date">起始日期</label><input id="arr-start-date" name="start_date" type="date" value="${escapeHtml(trig.start_date || _arrToday())}"></div>`
    + `<div class="arr-freq arr-repeat-ending"><span>截止时间</span><label><input type="radio" name="ending" value="never"${trig.end_date ? "" : " checked"}>永不截止</label><label><input type="radio" name="ending" value="date"${trig.end_date ? " checked" : ""}>截止日期</label><input name="end_date" type="date" aria-label="截止日期（含当天）" value="${escapeHtml(trig.end_date || "")}"></div>`
    + `<p class="arr-repeat-summary" aria-live="polite"></p></fieldset>`
    + `<p class="arr-schedule-hint"></p><p class="arr-agent-hint" hidden>复用此 Bot 的引擎、工作目录与上下文配置。</p>`
    + `<div class="arr-test" hidden><b></b><span></span></div>`
    + `<div class="arr-form-actions"><small>结果回到安排页，不会自动外发。</small><button type="button" data-test>试运行</button><button type="button" data-cancel>取消</button><button class="arr-primary" type="submit">保存</button></div></form>`;
  const form = host.querySelector("form"), save = form.querySelector("[type=submit]"), test = form.querySelector("[data-test]"), card = form.querySelector(".arr-test");
  form.elements.kind.value = trig.kind || "manual";
  form.elements.repeat_unit.value = trig.unit || "week";
  const reveal = () => {
    const kind = form.elements.kind.value;
    const custom = kind === "custom";
    const weekly = form.elements.repeat_unit.value === "week";
    const repeat = form.querySelector(".arr-repeat");
    repeat.hidden = !custom; repeat.disabled = !custom;
    form.querySelector(".arr-repeat-days").hidden = !weekly;
    const days = [...form.querySelectorAll('[name="repeat_day"]')];
    days.forEach((el) => { el.disabled = !custom || !weekly; });
    days[0].setCustomValidity(custom && weekly && !days.some((el) => el.checked) ? "请至少选择一个星期" : "");
    for (const name of ["repeat_every", "start_date"]) form.elements[name].required = custom;
    const until = form.elements.end_date;
    until.hidden = form.elements.ending.value !== "date";
    until.disabled = !custom || until.hidden; until.required = !until.disabled;
    until.min = form.elements.start_date.value;
    form.querySelector(".arr-repeat-summary").textContent = custom ? _arrTrigger(_arrFormTrigger(form)) : "";
    const fields = { date: kind === "once", weekday: kind === "weekly",
      time: !["manual", "interval"].includes(kind), minutes: kind === "interval" };
    form.querySelector(".arr-timing").hidden = kind === "manual";
    for (const [name, visible] of Object.entries(fields)) {
      const field = form.elements[name];
      field.hidden = !visible; field.disabled = !visible; field.required = visible;
    }
    form.querySelector(".arr-interval-unit").hidden = kind !== "interval";
    const [backend, id] = form.elements.agent.value.split("::");
    const agent = (_arrCache.agents?.items || []).find((a) => a.id === id && (a.backend || "desktop") === backend);
    form.querySelector(".arr-agent-hint").hidden = !agent || agent.kind === "agent";
    form.querySelector(".arr-schedule-hint").textContent = kind === "manual" ? "保存后，点击「立即运行」时执行。"
      : kind === "interval" ? "从保存或恢复起计时；修改执行内容不会重置倒计时。"
      : `按 ${backend || "desktop"} 的本地时间执行${kind === "once" ? "，仅触发一次" : ""}。`;
  };
  const payload = () => {
    const [backend, agent_id] = form.elements.agent.value.split("::");
    return { backend, agent_id, instruction: form.elements.instruction.value.trim(),
      session_id: (item && item.session_id) || options.sessionID || "",
      trigger: _arrFormTrigger(form), enabled: editing ? item.enabled : true };
  };
  const sig = () => JSON.stringify(payload());
  let testedSig = "";
  const say = (cls, head, body) => { card.hidden = false; card.className = "arr-test " + cls; card.querySelector("b").textContent = head; card.querySelector("span").textContent = body; };
  // 改动之后上一次试运行的结果就不作数了,收起来;保存不受影响
  const dirty = () => { if (testedSig && testedSig !== sig()) { testedSig = ""; card.hidden = true; } reveal(); };
  form.oninput = dirty; form.onchange = dirty; reveal();
  form.querySelector("[data-cancel]").onclick = () => _arrCloseSheet(m);
  test.onclick = async () => {
    const p = payload();
    if (!p.instruction) { form.elements.instruction.focus(); _deskNotice("先写清楚每次要做什么"); return; }
    if (!form.elements.agent.selectedOptions[0] || form.elements.agent.selectedOptions[0].disabled) { _deskNotice("请选择可用的执行者"); return; }
    test.disabled = true; say("testing", "正在试运行", "使用所选执行者运行一次，结果只回到这里。");
    try {
      const r = await api(`/workbench/arrangements/test${_arrAt(p.backend)}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(p), timeout: 20000 });
      const run = await _arrWaitRun(r.run.id, p.backend);
      if (run.status !== "succeeded") throw new Error(run.error || "试运行没有完成");
      testedSig = sig(); say("passed", "试运行通过", (run.output || _arrUsed(run.used)).slice(0, 160));
    } catch (e) { say("failed", "试运行未通过", e.message || "运行失败"); }
    test.disabled = false;
  };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const p = payload();
    if (!p.instruction) { form.elements.instruction.focus(); _deskNotice("先写清楚每次要做什么"); return; }
    if (!form.elements.agent.selectedOptions[0] || form.elements.agent.selectedOptions[0].disabled) { _deskNotice("请选择可用的执行者"); return; }
    if (!form.reportValidity()) return;
    const at = _arrAt(p.backend);
    const path = editing ? `/workbench/arrangements/${encodeURIComponent(item.id)}${at}` : `/workbench/arrangements${at}`;
    save.disabled = true;
    try {
      await api(path, { method: editing ? "PUT" : "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(p), timeout: 20000 });
      _arrCloseSheet(m); _deskNotice(editing ? "安排已更新" : "安排已保存");
      if (typeof options.onSaved === "function") options.onSaved();
      else openArrangementsPage(true);
    } catch (err) { _deskNotice(err.message || "保存失败"); save.disabled = false; }
  };
  setTimeout(() => form.elements.instruction.focus(), 50);
}
async function _arrWaitRun(id, backend) {
  for (let i = 0; i < 120; i++) {
    const r = await api(`/workbench/arrangements/runs/${encodeURIComponent(id)}${_arrAt(backend)}`, { timeout: 20000 });
    if (r.run && !["queued", "running"].includes(r.run.status)) return r.run;
    await new Promise((ok) => setTimeout(ok, 1500));
  }
  throw new Error("运行超时，可稍后在安排页查看结果");
}
async function _arrWatchRun(id, backend, toast) {
  try { const run = await _arrWaitRun(id, backend); if (toast) _deskNotice(run.status === "succeeded" ? "运行完成" : (run.error || "运行失败")); openArrangementsPage(true); } catch (e) { if (toast) _deskNotice(e.message); }
}
async function _arrResultSheet(id, backend, title) {
  const m = _arrSheet(title || "运行结果", "最近一次运行");
  const host = m.querySelector(".arr-sheet-body");
  host.innerHTML = '<div class="arr-result-full">读取中…</div>';
  try {
    const r = await api(`/workbench/arrangements/runs/${encodeURIComponent(id)}${_arrAt(backend)}`, { timeout: 20000 });
    const run = r.run || {};
    host.innerHTML = `<div class="arr-run-meta"><span>${escapeHtml(run.status === "succeeded" ? "运行成功" : run.status === "failed" ? "运行失败" : "正在运行")}</span><span>${escapeHtml(_arrAgo(run.finished_at || run.started_at))}</span><span>${escapeHtml(_arrUsed(run.used))}</span></div>${run.error ? `<div class="arr-run-error">${escapeHtml(run.error)}</div>` : ""}<pre class="arr-result-full">${escapeHtml(run.output || "暂无输出")}</pre>`;
  } catch (e) { host.innerHTML = `<div class="arr-run-error">${escapeHtml(e.message || "读取失败")}</div>`; }
}
