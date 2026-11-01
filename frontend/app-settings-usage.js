// Token 用量：只读本机标量账本。工作流与标题在后端按当前会话索引关联，正文不进入统计。
function renderTokenUsageSettings(host) {
  const state = { data: null, range: 90, engine: "all", date: "", workflow: "", sort: "tokens" };
  let heatRangeAtRight = 0;
  host.classList.add("tu-host");
  host.innerHTML = '<div class="tu-loading">' + DeskI18n.txh("读取本机用量…") + '</div>';

  const fmt = (value) => {
    const n = Number(value || 0);
    if (n >= 1e9) return (n / 1e9).toFixed(n >= 1e10 ? 1 : 2).replace(/\.0$/, "") + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 1 : 2).replace(/\.0$/, "") + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 1 : 2).replace(/\.0$/, "") + "K";
    return Math.round(n).toLocaleString(DeskI18n.tag);
  };
  const dateText = (iso, opts = { month: "short", day: "numeric" }) =>
    new Date(iso + "T12:00:00").toLocaleDateString(DeskI18n.tag, opts);
  const cutoff = () => {
    const d = new Date(state.data.range.end + "T12:00:00");
    d.setDate(d.getDate() - state.range + 1);
    return d.toISOString().slice(0, 10);
  };
  const add = (to, row) => {
    for (const key of ["total", "input", "output", "cached", "cache_write", "turns", "detail_turns",
      "historical_total", "historical_input", "historical_output", "historical_cached", "historical_turns",
      "historical_detail_turns", "recorded_total", "recorded_turns", "anomalous_total", "anomalous_turns"])
      to[key] = (to[key] || 0) + Number(row[key] || 0);
    return to;
  };
  const blank = () => ({ total: 0, input: 0, output: 0, cached: 0, cache_write: 0, turns: 0,
    detail_turns: 0, historical_total: 0, historical_input: 0, historical_output: 0,
    historical_cached: 0, historical_turns: 0, historical_detail_turns: 0,
    recorded_total: 0, recorded_turns: 0, anomalous_total: 0, anomalous_turns: 0 });
  const rate = (row) => {
    // Use the all-row total: input detail coverage can be partial for retained history.
    const base = row.total;
    return base ? row.cached / base : null;
  };
  const sessionValue = (session) => {
    const value = blank();
    for (const [day, item] of Object.entries(session.daily || {})) {
      if (day >= cutoff() && (!state.date || day === state.date)) add(value, item);
    }
    return { ...session, ...value, cache_rate: rate(value) };
  };
  const sessions = () => state.data.sessions.map(sessionValue)
    .filter((row) => row.recorded_total > 0 && (state.engine === "all" || row.engine === state.engine));
  const visibleRows = () => {
    const rows = sessions();
    if (state.workflow) return rows.filter((row) => row.domain === state.workflow);
    const groups = new Map();
    for (const row of rows) {
      const group = groups.get(row.domain) || { id: row.domain, name: row.workflow, sessions: 0, last_at: 0, ...blank() };
      add(group, row); group.sessions += 1; group.last_at = Math.max(group.last_at, row.last_at || 0);
      groups.set(row.domain, group);
    }
    return [...groups.values()].map((row) => ({ ...row, cache_rate: rate(row) }));
  };
  const sortedRows = () => visibleRows().sort((a, b) => state.sort === "cache"
    ? (a.cache_rate ?? 2) - (b.cache_rate ?? 2) : b.recorded_total - a.recorded_total);

  function shell() {
    host.innerHTML = '<div class="tu-toolbar"><div class="tu-range" role="group" aria-label="时间范围">'
      + '<button data-days="30">30 天</button><button data-days="90">90 天</button><button data-days="365">一年</button></div></div>'
      + '<div class="tu-period"></div><div class="tu-summary" aria-label="Token 用量概览"></div>'
      + '<section class="tu-card tu-heat-card"><header><div><h3>每日用量</h3><p>点击日期，筛选下方归因列表。</p></div></header>'
      + '<div class="tu-heat-scroll"><div class="tu-heat-grid"><div class="tu-months"></div><div class="tu-heat-body"><div class="tu-weekdays"><span></span><span>周一</span><span></span><span>周三</span><span></span><span>周五</span><span></span></div><div class="tu-heatmap"></div></div></div></div>'
      + '<footer><span class="tu-heat-meta"><span class="tu-active-days"></span><span class="tu-date-chip" hidden>已筛选 <b></b><button type="button" aria-label="清除日期筛选">×</button></span></span><span class="tu-legend">少 <i></i><i></i><i></i><i></i><i></i> 多</span></footer></section>'
      + '<section class="tu-card tu-breakdown"><header><div class="tu-crumbs"><button type="button" hidden><span class="ui-arrow-icon is-left" aria-hidden="true"></span><span>工作流</span></button><b>按工作流归因</b></div>'
      + '<div class="tu-controls"><select class="tu-engine" aria-label="筛选引擎"><option value="all">全部引擎</option></select>'
      + '<select class="tu-sort" aria-label="排序方式"><option value="tokens">Token 最多</option><option value="cache">缓存占比最低</option></select></div></header>'
      + '<div class="tu-columns"><span>工作流</span><span>口径</span><span>Token</span><span></span></div><div class="tu-rows"></div>'
      + '<div class="tu-empty" hidden><b>没有匹配的用量</b><span>请调整日期或引擎筛选。</span></div></section>'
      + '<p class="tu-method">可信统计只包含新版逐回合 Provider usage；旧记录作为历史参考单列，异常跳变会隔离且不计入总量。查询读取本机增量账本，不扫描聊天正文。</p>'
      + '<div class="tu-drawer" hidden><button class="tu-drawer-backdrop" type="button" aria-label="关闭详情"></button><aside role="dialog" aria-modal="true" aria-label="会话用量详情"><header><div><span>会话用量</span><h3></h3><p></p></div><button class="tu-drawer-close" type="button" aria-label="关闭详情">×</button></header><div class="tu-detail-total"><span>总 Token</span><strong></strong></div><div class="tu-detail-grid"></div><dl></dl><p class="tu-detail-note">不同引擎与套餐的计费口径不同，因此这里不把 Token 直接换算为金额。</p></aside></div>';
    DeskI18n.localize(host);   // 模板里的固定文字；数据在其后填入
    const engines = [...new Set(state.data.sessions.map((row) => row.engine).filter(Boolean))].sort();
    host.querySelector(".tu-engine").insertAdjacentHTML("beforeend", engines.map((engine) =>
      `<option value="${escapeHtml(engine)}">${escapeHtml(engine)}</option>`).join(""));
    host.querySelectorAll("[data-days]").forEach((button) => button.onclick = () => {
      state.range = Number(button.dataset.days); state.date = ""; state.workflow = ""; paint();
    });
    host.querySelector(".tu-engine").onchange = (event) => { state.engine = event.target.value; state.workflow = ""; paint(); };
    host.querySelector(".tu-sort").onchange = (event) => { state.sort = event.target.value; paintRows(); };
    host.querySelector(".tu-crumbs button").onclick = () => { state.workflow = ""; paintRows(); };
    host.querySelector(".tu-date-chip button").onclick = () => { state.date = ""; paint(); };
    host.querySelector(".tu-drawer-backdrop").onclick = closeDetail;
    host.querySelector(".tu-drawer-close").onclick = closeDetail;
  }

  function paintSummary() {
    const total = sessions().reduce(add, blank());
    const start = state.data.coverage?.start;
    const recordedDays = new Set();
    for (const session of sessions()) for (const [day, item] of Object.entries(session.daily || {}))
      if (day >= cutoff() && Number(item.recorded_total || 0) > 0) recordedDays.add(day);
    const anomaly = total.anomalous_turns ? " · " + DeskI18n.txh("已隔离 {count} 个异常回合", { count: total.anomalous_turns }) : "";
    host.querySelector(".tu-summary").innerHTML = '<div><span>' + DeskI18n.txh("可信 Token") + '</span><strong>' + fmt(total.total) + '</strong><small>'
      + DeskI18n.txh("{count} 个逐回合记录", { count: total.turns }) + ' · '
      + DeskI18n.txh("{count} 个会话", { count: sessions().length }) + '</small></div>'
      + '<div><span>' + DeskI18n.txh("历史参考") + '</span><strong>' + fmt(total.historical_total) + '</strong><small>'
      + DeskI18n.txh("{count} 个旧记录", { count: total.historical_turns }) + anomaly + '</small></div>'
      + '<div><span>' + DeskI18n.txh("数据覆盖") + '</span><strong>' + DeskI18n.txh("{count} 天", { count: recordedDays.size }) + '</strong><small>'
      + (start ? DeskI18n.txh("始于 {date}", { date: dateText(start, { year: "numeric", month: "short", day: "numeric" }) })
        : DeskI18n.txh("尚无可用记录"))
      + '</small></div>';
  }

  function paintHeat() {
    // 图表始终保留接口返回的完整一年骨架；30/90 天只是统计与交互窗口，不能把
    // 较早月份裁掉后将几周方格孤零零推到右侧。
    const chartStart = state.data.range.start;
    const selectedStart = cutoff();
    const map = new Map();
    for (const session of state.data.sessions) {
      if (state.engine !== "all" && session.engine !== state.engine) continue;
      for (const [day, item] of Object.entries(session.daily || {})) {
        if (day >= chartStart) map.set(day, (map.get(day) || 0) + Number(item.recorded_total || 0));
      }
    }
    const start = new Date(chartStart + "T12:00:00");
    const end = new Date(state.data.range.end + "T12:00:00");
    const padded = new Date(start); padded.setDate(start.getDate() - start.getDay());
    const dates = [];
    for (const cursor = new Date(padded); cursor <= end; cursor.setDate(cursor.getDate() + 1))
      dates.push(cursor.toISOString().slice(0, 10));
    while (dates.length % 7) dates.push("");
    const coverageStart = state.data.coverage?.start || "9999-12-31";
    const selectedValues = [...map].filter(([day]) => day >= selectedStart && day >= coverageStart).map(([, value]) => value);
    const max = Math.max(0, ...selectedValues);
    // 照 GitHub 贡献图分档：0 为空，其余按所选周期内非零日的四分位切成 1–4 档。
    const nonzero = selectedValues.filter(Boolean).sort((a, b) => a - b);
    const quart = (q) => nonzero.length ? nonzero[Math.min(nonzero.length - 1, Math.floor(q * nonzero.length))] : 0;
    const cuts = [quart(.25), quart(.5), quart(.75)];
    const level = (value) => !value || !max ? 0 : 1 + cuts.filter((cut) => value > cut).length;
    const heat = host.querySelector(".tu-heatmap");
    heat.innerHTML = dates.map((day) => {
      if (!day || day < chartStart) return '<i class="tu-day blank"></i>';
      const value = map.get(day) || 0;
      if (day < coverageStart)
        return `<i class="tu-day unavailable" title="${dateText(day)} · ${DeskI18n.txh("尚未记录")}"></i>`;
      if (day < selectedStart)
        return `<i class="tu-day history" data-level="${level(value)}" title="${dateText(day)} · ${DeskI18n.txh("所选周期外")}"></i>`;
      return `<button type="button" class="tu-day${state.date === day ? " selected" : ""}" data-date="${day}" data-level="${level(value)}" aria-label="${day}，${fmt(value)} Token" title="${dateText(day)} · ${fmt(value)} Token"></button>`;
    }).join("");
    heat.querySelectorAll("button").forEach((button) => button.onclick = () => {
      state.date = state.date === button.dataset.date ? "" : button.dataset.date; paint();
    });
    const weeks = dates.length / 7;
    const months = [], seenMonths = new Set(); let lastMonthColumn = -2;
    dates.forEach((day, index) => {
      if (!day || day < chartStart) return;
      const month = day.slice(0, 7), column = Math.floor(index / 7) + 1;
      if (seenMonths.has(month)) return;
      seenMonths.add(month);
      // 标签相邻时保留后一个完整月，去掉开头只露出几天的那个月。
      if (column <= lastMonthColumn + 1) months.pop();
      lastMonthColumn = column;
      months.push(`<span style="grid-column:${column}">${dateText(day, { month: "short" })}</span>`);
    });
    const grid = host.querySelector(".tu-heat-grid");
    grid.style.setProperty("--weeks", weeks); grid.dataset.range = state.range;
    host.querySelector(".tu-months").innerHTML = months.join("");
    // 全年骨架始终保留；首次打开或切换周期时只改变视口位置，让最近日期优先可见。
    // 点击日期、切换引擎等同周期重绘不重置位置，用户仍可停留在手动拖到的月份。
    if (heatRangeAtRight !== state.range) {
      heatRangeAtRight = state.range;
      const scroll = host.querySelector(".tu-heat-scroll");
      requestAnimationFrame(() => {
        if (!host.isConnected || !scroll.isConnected) return;
        scroll.scrollLeft = Math.max(0, scroll.scrollWidth - scroll.clientWidth);
      });
    }
    host.querySelector(".tu-active-days").textContent = state.data.coverage?.start
      ? DeskI18n.tx("已记录 {count} 天 · 数据始于 {date}", { count: selectedValues.filter(Boolean).length, date: dateText(state.data.coverage.start) })
      : DeskI18n.tx("尚无 Token 用量记录");
    const chip = host.querySelector(".tu-date-chip"); chip.hidden = !state.date;
    chip.querySelector("b").textContent = state.date ? dateText(state.date) : "";
  }

  function paintRows() {
    const rows = sortedRows();
    const max = Math.max(1, ...rows.map((row) => row.recorded_total));
    const isSession = Boolean(state.workflow);
    const workflow = state.data.workflows.find((row) => row.id === state.workflow)
      || state.data.sessions.find((row) => row.domain === state.workflow);
    host.querySelector(".tu-crumbs b").textContent = workflow?.name || DeskI18n.tx("按工作流归因");
    host.querySelector(".tu-crumbs button").hidden = !isSession;
    host.querySelector(".tu-columns span").textContent = isSession ? DeskI18n.tx("会话") : DeskI18n.tx("工作流");
    const list = host.querySelector(".tu-rows");
    list.innerHTML = rows.map((row) => {
      const name = isSession ? row.title : row.name;
      const meta = isSession ? `${row.engine}${row.model ? " · " + row.model : ""}${row.archived ? " · " + DeskI18n.tx("匿名保留") : ""}` : DeskI18n.tx("{count} 个会话", { count: row.sessions });
      const quality = row.total > 0 ? (row.historical_total > 0 ? DeskI18n.txh("可信 + 历史") : DeskI18n.txh("可信")) : DeskI18n.txh("历史参考");
      return `<button type="button" class="tu-row" data-id="${escapeHtml(row.id)}"><span class="tu-row-copy"><b>${escapeHtml(name)}</b><small>${escapeHtml(meta)}</small></span><span class="tu-bar-cell"><i><b style="width:${Math.max(4, row.recorded_total / max * 100)}%"></b></i><small>${quality}</small></span><span class="tu-tokens">${fmt(row.recorded_total)}<small>${state.date ? DeskI18n.txh("当日") : DeskI18n.txh("所选周期")}</small></span><span class="tu-arrow">${isSession ? "⋯" : '<i class="ui-arrow-icon" aria-hidden="true"></i>'}</span></button>`;
    }).join("");
    list.querySelectorAll(".tu-row").forEach((button) => button.onclick = () => {
      if (!isSession) { state.workflow = button.dataset.id; paintRows(); return; }
      const row = rows.find((item) => item.id === button.dataset.id); if (row) openDetail(row);
    });
    host.querySelector(".tu-empty").hidden = rows.length > 0;
  }

  function paint() {
    host.querySelectorAll("[data-days]").forEach((button) => button.classList.toggle("on", Number(button.dataset.days) === state.range));
    host.querySelector(".tu-period").textContent = `${dateText(cutoff(), { year: "numeric", month: "short", day: "numeric" })} – ${dateText(state.data.range.end, { year: "numeric", month: "short", day: "numeric" })}`;
    paintSummary(); paintHeat(); paintRows();
  }

  function openDetail(row) {
    const drawer = host.querySelector(".tu-drawer");
    drawer.querySelector("h3").textContent = row.title;
    drawer.querySelector("header p").textContent = `${row.engine}${row.model ? " · " + row.model : ""}`;
    drawer.querySelector(".tu-detail-total strong").textContent = fmt(row.recorded_total);
    drawer.querySelector(".tu-detail-grid").innerHTML = `<div><span>${DeskI18n.txh("可信统计")}</span><b>${fmt(row.total)}</b></div><div><span>${DeskI18n.txh("历史参考")}</span><b>${fmt(row.historical_total)}</b></div><div><span>${DeskI18n.txh("缓存读取")}</span><b>${fmt(row.cached + row.historical_cached)}</b></div><div><span>${DeskI18n.txh("异常隔离")}</span><b>${DeskI18n.txh("{count} 回合", { count: row.anomalous_turns })}</b></div>`;
    drawer.querySelector("dl").innerHTML = `<div><dt>${DeskI18n.txh("工作流")}</dt><dd>${escapeHtml(row.workflow)}</dd></div><div><dt>${DeskI18n.txh("已记录回合")}</dt><dd>${row.recorded_turns}</dd></div><div><dt>${DeskI18n.txh("最近活跃")}</dt><dd>${new Date(row.last_at).toLocaleString(DeskI18n.tag, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</dd></div>`;
    drawer.hidden = false; drawer.querySelector(".tu-drawer-close").focus();
  }
  function closeDetail() { host.querySelector(".tu-drawer").hidden = true; }

  api("/workbench/usage?days=365", { timeout: 20000 }).then((data) => {
    if (!host.isConnected) return; state.data = data; shell(); paint();
  }).catch((error) => { if (host.isConnected) host.innerHTML = `<div class="tu-loading">${DeskI18n.txh("读取失败：{error}", { error: error.message || error })}</div>`; });
}
