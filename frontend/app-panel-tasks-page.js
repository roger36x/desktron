// 由 app-panel.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 3/5 片
// ---- 全局任务页(侧栏「任务」入口):全局看板 + 分工作流统计 + 所有待办 ----
// 数据逐工作流拉 /workbench/domains/<id>/tasks 再汇总:统计口径与工作流页一致(同一份 stats),
// 详情统一由任务浮层承载，工作流本身不再另设详情页。
const WF_SEC_ICONS = {
  分工作流: ["violet", '<rect x="3" y="4" width="7" height="7" rx="1.5"/><rect x="14" y="4" width="7" height="7" rx="1.5"/><rect x="3" y="15" width="7" height="5" rx="1.5"/><rect x="14" y="15" width="7" height="5" rx="1.5"/>'],
  所有待办: ["blue", '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>'],
};
function wfSecIcons(root) {
  root.querySelectorAll(".wf-sec-head").forEach((h) => {
    if (h.querySelector(".wf-sec-ic")) return;
    const title = h.querySelector(".wf-sec-title");
    const hit = title && WF_SEC_ICONS[title.textContent.trim()];
    if (!hit) return;
    const el = document.createElement("span");
    el.className = "wf-sec-ic ic-" + hit[0];
    el.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
      + 'stroke-linecap="round" stroke-linejoin="round">' + hit[1] + "</svg>";
    h.insertBefore(el, title);
  });
}

// flow 注记两代格式:v1=整串 URL 编码的线性链;v2=原生「|」分阶段、「+」分并行支路。
function _wfFlowStages(t) {
  const raw = String(t.flow || "");
  if (!raw) return [];
  try {
    if (raw.includes("|") || raw.includes("+"))
      return raw.split("|").map((seg) => seg.split("+").map((x) => decodeURIComponent(x).trim()).filter(Boolean)).filter((st) => st.length);
    return decodeURIComponent(raw).split("|").map((x) => [x.trim()]).filter((st) => st[0]);
  } catch { return []; }
}
function _wfFlowToggle(stages, doneSet, si, ni) {
  const id = `${si}.${ni}`;
  const next = new Set(doneSet);
  if (next.has(id)) next.delete(id);
  else {
    next.add(id);
    for (let s2 = 0; s2 < si; s2++) stages[s2].forEach((_, n2) => next.add(`${s2}.${n2}`));
  }
  return [...next].sort().join(",");
}
function _wfFlowSerialize(stages) {
  return stages.map((st) => st.map(encodeURIComponent).join("+")).join("|");
}
function _wfFlowDone(t, stages) {
  const set = new Set();
  const raw = String(t.fdone || "").trim();
  if (raw) { raw.split(",").forEach((x) => x && set.add(x)); return set; }
  const cur = parseInt(t.fcur || "0", 10) || 0;
  stages.forEach((st, si) => { if (si < cur) st.forEach((_, ni) => set.add(`${si}.${ni}`)); });
  return set;
}
function _tpAgg(rows) {
  const z = { today_touched: 0, today_new: 0, week_touched: 0, week_done: 0, prev_week_done: 0, trend_total: 0 };
  const trend = new Array(14).fill(0);
  let open = 0, done = 0, spanSum = 0, spanN = 0;
  for (const r of rows) {
    const s = r.data.stats || {};
    for (const k of Object.keys(z)) z[k] += s[k] || 0;
    (s.trend14 || []).forEach((v, i) => { trend[i] += v || 0; });
    open += r.open.length; done += r.done.length;
    if (s.avg_hours != null) { spanSum += s.avg_hours * (s.week_done || 1); spanN += (s.week_done || 1); }
  }
  return { ...z, trend14: trend, open, done, avg_hours: spanN ? Math.round(spanSum / spanN * 10) / 10 : null };
}
// 14 天完成趋势:按工作流分色堆叠。色系统一在绿—青这一段(完成=绿的既有语义),
// 只在色相与明度上错开,避免多色拼盘;超出色板则循环复用。
// 任务页的统计配色。绿在这套界面里另有职责——「成功 / 在线」那一类状态用的就是它,
// 拿来给任务按工作流分色,每根柱子读起来都像在报健康度。改成与「任务」标识同一族的淡紫,
// 六档按明度与冷暖岔开,堆叠时仍分得清是哪个工作流。
const _TP_COLORS = ["#b1b9f9", "#8b8fe0", "#cbb9f5", "#6f77c9", "#a08fe8", "#7fa0ec"];
function _tpTrendStack(rows) {
  // 堆叠柱:一天一根,各工作流按当日占比切段。折线试过一版又换回来——十四天里多数是零,
  // 折线把零值连成贴地的长横线,读起来像「一直在做但产出为零」,而事实是那些天没有闭环。
  //
  // 柱子放细:外层 svg 是 preserveAspectRatio="none" 横向拉伸的,视口宽度大约是 viewBox 的
  // 两倍,所以这里的 8 个单位落到屏幕上是十几像素。原来 34 个单位铺满了整格,一眼看去是
  // 一片色块而不是一根柱。
  const day = 14, W = 700, H = 56, slot = W / day, bw = Math.min(8, slot - 6);
  const per = rows.map((r, i) => ({ name: r.d.name, color: _TP_COLORS[i % _TP_COLORS.length],
                                    tr: (r.data.stats || {}).trend14 || [] }));
  const totals = Array.from({ length: day }, (_, i) => per.reduce((a, p) => a + (p.tr[i] || 0), 0));
  const max = Math.max(1, ...totals);
  let out = "";
  for (let i = 0; i < day; i++) {
    if (!totals[i]) continue;
    const full = Math.max(6, Math.round(totals[i] / max * 50));   // 当日总高,再按各工作流占比切段
    const x = (slot * i + (slot - bw) / 2).toFixed(1);
    let acc = 0;
    per.forEach((p) => {
      const v = p.tr[i] || 0;
      if (!v) return;
      const h = Math.max(2, full * v / totals[i]);
      out += `<rect x="${x}" y="${(H - 4 - acc - h).toFixed(1)}" width="${bw}" height="${h.toFixed(1)}"` +
        ` fill="${p.color}"><title>${escapeHtml(p.name)}:${v}</title></rect>`;
      acc += h;
    });
  }
  return out;
}
// 横轴日期:trend14 的下标 i 对应 today-(13-i) 天(见 tasks.py 的 trend 构造),两边口径必须一致
function _tpTrendDays(day = 14) {
  const base = new Date(); base.setHours(0, 0, 0, 0);
  return Array.from({ length: day }, (_, i) => {
    const d = new Date(base.getTime() - (day - 1 - i) * 86400000);
    return { label: `${d.getMonth() + 1}/${d.getDate()}`, i };
  });
}
// 悬停某一天:浮层给当日总数 + 分工作流拆解(值为 0 的工作流不列)。浮层复用折线图那套 .wf-chart-tip
function _tpWireTrend(body, rows) {
  const hit = body.querySelector(".tp-hit");
  if (!hit) return;
  const days = _tpTrendDays();
  const per = rows.map((r, i) => ({ name: r.d.name, color: _TP_COLORS[i % _TP_COLORS.length],
                                    tr: (r.data.stats || {}).trend14 || [] }));
  let tip = null;
  const place = (ev) => {
    const vw = window.innerWidth, vh = window.innerHeight;
    let lx = ev.clientX + 14, ly = ev.clientY + 14;
    if (lx + tip.offsetWidth > vw - 8) lx = Math.max(8, vw - tip.offsetWidth - 8);
    if (ly + tip.offsetHeight > vh - 8) ly = Math.max(8, ev.clientY - tip.offsetHeight - 10);
    tip.style.left = lx + "px"; tip.style.top = ly + "px";
  };
  hit.querySelectorAll("i").forEach((cell) => {
    const idx = Number(cell.dataset.i);
    const show = (ev) => {
      const list = per.map((p) => ({ ...p, v: p.tr[idx] || 0 })).filter((p) => p.v > 0);
      const total = list.reduce((a, p) => a + p.v, 0);
      if (!tip) { tip = document.createElement("div"); tip.className = "wf-chart-tip"; document.body.appendChild(tip); }
      tip.innerHTML = `<div class="tt-h">${days[idx].label} · 闭环 ${total}</div>` +
        (list.length ? list.map((p) =>
          `<div class="tt-r"><i style="background:${p.color}"></i><span class="tt-n">${escapeHtml(p.name)}</span><b>${p.v}</b></div>`).join("")
          : `<div class="tt-r"><span class="tt-n">当日无闭环</span></div>`);
      tip.style.display = "block";
      place(ev);
    };
    cell.onmouseenter = show;
    cell.onmousemove = (ev) => { if (tip && tip.style.display === "block") place(ev); };
    cell.onmouseleave = () => { if (tip) tip.style.display = "none"; };
  });
}
function _tpTrendLegend(rows) {
  return rows.filter((r) => ((r.data.stats || {}).trend14 || []).some((v) => v))
    .map((r, i, arr) => {
      const idx = rows.indexOf(r);
      return `<span class="tp-lg"><i style="background:${_TP_COLORS[idx % _TP_COLORS.length]}"></i>` +
        `${escapeHtml(r.d.name)}</span>`;
    }).join("");
}
// 各工作流的任务清单。任务页与设置页里的「任务统计」都要这份,取数只写一处。
//
// 走 _fetchTasks,不自己打。它自称是任务数据的唯一入口,而这里原来绕开它另用 api() 取了
// 一遍:同一份数据两处各取各的,去重也就无从谈起。挡重复的那道闸做在它那里,侧栏折叠计数、
// 任务页、设置页统计这几路一并受益(2026-08-25 实测:同一时刻的 11 个 tasks 收到 5 个)。
//
// 一次取全,不再一个工作流一个请求。中间试过两版都不对:先只在这里加去重,实测 tasks 仍是
// 11 个(侧栏折叠计数与容器聚合走的是 _fetchTasks,绕不到这道闸);再改成 Promise.all 并行,
// 实测多开 3 条连接、每个请求 1.06~1.84 秒。经隧道的开销几乎全在建连(同日实测:复用连接
// 20 次零尖峰、中位 242ms,新建连接 20 次里一半超 600ms、最慢 1540ms),而服务端这头一个
// 只要 1~3 毫秒。所以既不是「并行还是串行」的问题,也不是「去重与否」——是请求数本身。
async function tpTaskRows() {
  const ds = sideProjects();
  const bundle = await fetchTasksBundle(ds.map((d) => d.id));
  const rows = [];
  for (const d of ds) {
    const data = bundle[d.id];
    if (!data) continue;   // 单个工作流读失败不连累整页
    const items = data.items || [];
    rows.push({ d, data, open: items.filter((t) => t.state !== "done"),
                done: items.filter((t) => t.state === "done") });
  }
  return rows;
}

// 统计块:四张数字卡 + 最近 14 天的完成情况。原来长在任务页顶上,2026-08-16 挪进设置页
// 末尾的一节——任务页要回答的是「现在有什么待办」,这几个数回答的是「最近干得怎么样」,
// 后者是偶尔回看一次的东西,压在待办前面每次都要先翻过去。
// 只产 HTML,不接线:柱状图的悬停由 _tpWireTrend 单独挂(它需要拿到 byOpen 的次序)。
function tpStatsHtml(byOpen, g) {
  const diff = g.week_done - g.prev_week_done;
  return `<div class="wf-stats">` +
      `<div class="wf-stat"><div class="k">今日任务</div><div class="v">${g.today_touched}</div><div class="d">新建 ${g.today_new}</div></div>` +
      `<div class="wf-stat"><div class="k">本周任务</div><div class="v">${g.week_touched}</div><div class="d">待办 ${g.open}</div></div>` +
      `<div class="wf-stat"><div class="k">本周完成</div><div class="v">${g.week_done}</div>` +
        `<div class="d${diff > 0 ? " up" : ""}">较上周 ${diff >= 0 ? "+" : ""}${diff}</div></div>` +
      `<div class="wf-stat"><div class="k">平均完成时间</div><div class="v">${g.avg_hours != null ? g.avg_hours : 0}<small>小时</small></div>` +
        `<div class="d">${g.avg_hours != null ? "近期闭环均值" : "待闭环数据"}</div></div>` +
    `</div>` +
    `<div class="wf-trend"><div class="k"><span>最近 14 天完成情况</span><span>共 ${g.trend_total} 项</span></div>` +
      `<div class="tp-chart">` +
        `<svg viewBox="0 0 700 56" preserveAspectRatio="none">${_tpTrendStack(byOpen)}</svg>` +
        // 命中层:14 个等宽格盖在图上,整列可悬停(只对着柱子会很难点中矮柱/空日)
        `<div class="tp-hit">${_tpTrendDays().map((d) => `<i data-i="${d.i}"></i>`).join("")}</div>` +
      `</div>` +
      `<div class="tp-xaxis">${_tpTrendDays().map((d) => `<span>${d.label}</span>`).join("")}</div>` +
      `<div class="tp-legend">${_tpTrendLegend(byOpen)}</div></div>`;
}

// repaint=true 是「这一页已经开着,只把内容换掉」:不推导航栈、不动滚动位置、不闪「加载中」。
// 服务端推 task.updated 过来时走的就是这一支(见 onTasksChanged);当初这一页打开之后
// 就再也不刷了,别处改完任务它一直停在旧数据上。
async function openTasksPage(repaint) {
  const body0 = $("#asset-body");
  if (repaint && !(body0 && body0.classList.contains("tasks-page"))) return;   // 不在这一页就别画
  if (!repaint) {
    navPush({ kind: "tasks" });
    showView("asset");
    $("#conv-title").textContent = "任务";
    body0.className = "md wf-page tasks-page";
    body0.innerHTML = `<h1 class="wf-h1">任务</h1><div class="wf-sub">加载中…</div>`;
    $("#asset-view").scrollTop = 0;
  }
  const body = body0;
  const rows = await tpTaskRows();
  if (!body.isConnected || !body.classList.contains("tasks-page")) return;
  const g = _tpAgg(rows);
  const byOpen = rows.slice().sort((a, b) => b.open.length - a.open.length);
  body.innerHTML =
    `<h1 class="wf-h1">任务</h1><div class="wf-sub">待办：${g.open} | 已完成：${g.done} · 共 ${rows.length} 个工作流</div>` +
    `<div class="wf-sec-head"><span class="wf-sec-title">分工作流</span>` +
      `<span class="wf-sec-sub">点击在侧栏中定位</span></div>` +
    `<div class="tp-wfs">${byOpen.map((r) =>
      `<div class="tp-wf" data-dom="${escapeHtml(r.d.id)}">` +
        `<span class="tp-wf-n">${escapeHtml(r.d.name)}</span>` +
        `<span class="tp-num" title="待办">${r.open.length}</span>` +
        `<span class="tp-wf-go">${_TP_CHEV}</span></div>`
    ).join("") || '<div class="wf-empty">还没有工作流。</div>'}</div>` +

    `<div class="wf-sec-head"><span class="wf-sec-title">所有待办</span>` +
      `<span class="tp-count">${g.open}</span></div>` +
    `<div class="tp-tasks">${byOpen.flatMap((r) => r.open.map((t) =>
      `<div class="tp-task" data-dom="${escapeHtml(r.d.id)}" data-line="${t.line}" data-title="${escapeHtml(t.title)}">` +
        `<span class="wt-cb${t.state === "doing" ? " doing" : ""}">${taskCbSvg(t.state)}</span>` +
        `<span class="tp-task-n">${escapeHtml(t.title)}</span></div>`)).join("")
      || '<div class="wf-empty">没有待办任务。</div>'}</div>`;
  wfSecIcons(body);
  body.querySelectorAll(".tp-wf").forEach((el) => {
    el.onclick = () => _revealWorkflowInSidebar(el.dataset.dom);
  });
  body.querySelectorAll(".tp-task").forEach((el) => {
    el.onclick = () => {
      const row = rows.find((r) => r.d.id === el.dataset.dom);
      const task = row && row.open.find((t) => Number(t.line) === Number(el.dataset.line)
        && t.title === el.dataset.title);
      if (task) wfOpenTaskSheet(task, el.dataset.dom);
    };
  });
}
const _TP_CHEV = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>';

// ---- 工作流项目引用:展示与操作收进工作流标题菜单，后端接口保持不变 ----
// 导入会话的选择弹层:列出该目录下扫到的会话,默认全选未归属的;已归属别处的置灰不可选。
// 不做无声全量导入——同一个仓可能服务过多件事,一次性全收会让工作流会话列表被历史淹没,
// 而清理只能逐条右键挪回去。
async function _wfRepoImportPicker(it, domId, host) {
  document.querySelector(".nw-modal")?.remove();
  const m = document.createElement("div");
  m.className = "nw-modal";
  m.innerHTML = '<div class="nw-backdrop"></div><div class="nw-box ri-box">'
    + `<div class="nw-title">从 ${escapeHtml(it.name)} 关联会话</div>`
    + '<div class="ri-hint">读取中…</div>'
    + '<div class="ri-list"><div class="wf-empty">加载中…</div></div>'
    + '<div class="nw-actions"><button class="nw-cancel">取消</button>'
    + '<button class="nw-ok" disabled>关联</button></div></div>';
  document.body.appendChild(m);
  const close = () => m.remove();
  m.querySelector(".nw-backdrop").onclick = close;
  m.querySelector(".nw-cancel").onclick = close;
  const list = m.querySelector(".ri-list");
  const hint = m.querySelector(".ri-hint");
  const ok = m.querySelector(".nw-ok");

  let rows = [];
  try {
    const r = await api(`/workbench/domains/${encodeURIComponent(domId)}/repos`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ op: "list-sessions", path: it.path, engine: curEngine }), timeout: 30000 });
    rows = (r && r.sessions) || [];
  } catch (e) {
    const stale = /未知 op|404/.test(e.message || "");
    hint.textContent = "";
    list.innerHTML = `<div class="wf-empty">${stale
      ? "后端还是旧版本(会话清单接口未加载),重启 Desk 后再试。"
      : "读取失败:" + escapeHtml(e.message)}</div>`;
    return;
  }
  if (!m.isConnected) return;
  if (!rows.length) {
    hint.textContent = "";
    list.innerHTML = '<div class="wf-empty">这个目录下没有扫到会话。</div>';
    return;
  }
  const nameOf = (id) => (domains.find((x) => x.id === id) || {}).name || id;
  const linked = rows.filter((s) => s.linked).length;
  // 关联不搬家:会话仍留在它自己的目录与原有分组里,这里只是让本工作流也能看到它
  hint.textContent = `这个目录下有 ${rows.length} 个会话` +
    (linked ? `,其中 ${linked} 个已关联到本工作流` : "") +
    "。关联不会移动会话,原来的位置照旧。";
  list.innerHTML = rows.map((s, i) => {
    const when = s.time ? new Date(s.time).toLocaleString("zh-CN", { hour12: false }).slice(5, 16) : "";
    return `<label class="ri-row${s.linked ? " taken" : ""}">` +
      `<input type="checkbox" data-i="${i}"${s.linked ? " disabled" : " checked"}>` +
      `<span class="ri-t">${escapeHtml(s.title)}</span>` +
      `<span class="ri-m">${s.linked ? "已关联" : escapeHtml(when)}</span></label>`;
  }).join("");
  const boxes = [...list.querySelectorAll("input")];
  const pick = boxes.filter((b) => !b.disabled);
  // 全选钮:超过三条才给,少量时逐个点更快
  if (pick.length > 3) {
    const all = document.createElement("button");
    all.className = "ri-all";
    hint.after(all);
    var syncAll = () => {
      const on = pick.every((b) => b.checked);
      all.textContent = on ? "全不选" : `全选(${pick.length})`;
      all.onclick = () => { pick.forEach((b) => { b.checked = !on; }); sync(); };
    };
  }
  const sync = () => {
    const n = boxes.filter((b) => b.checked).length;
    ok.disabled = !n;
    ok.textContent = n ? `关联所选 ${n} 个会话` : "关联";
    if (typeof syncAll === "function") syncAll();
  };
  boxes.forEach((b) => { b.onchange = sync; });
  sync();
  ok.onclick = async () => {
    const ids = boxes.filter((b) => b.checked).map((b) => rows[Number(b.dataset.i)].id);
    ok.disabled = true;
    let r;
    try {
      r = await api(`/workbench/domains/${encodeURIComponent(domId)}/repos`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ op: "import-sessions", path: it.path, ids, engine: curEngine }), timeout: 60000 });
    } catch (e) { alert("关联失败:" + e.message); ok.disabled = false; return; }
    close();
    if (r && !r.ok) { alert("关联失败:" + (r.error || "未知")); return; }
    // 会话列表读的是 _termsList 缓存,renderProjSessions 只在缓存为空时才拉;
    // 导入改的是后端 meta,不刷新缓存的话新会话不会出现在「会话」节。
    await refreshTerms();
    renderSessionPanel();   // 侧栏同步(工作流页的「会话」节已撤,只剩侧栏这一处)
  };
}

async function _wfRepoPick(host, domId) {
  // 「+」直接开系统目录选择器(壳内走 Finder,浏览器态回落输入框,同「打开代码仓」)。
  // 选中即加入引用清单;不问分支——cwd 恒为工作流目录,开不开分支由 agent 在任务里判断。
  const path = await pickFolder();
  if (!path) return false;
  let r;
  try {
    r = await api(`/workbench/domains/${encodeURIComponent(domId)}/repos`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ op: "add", path }), timeout: 60000 });
  } catch (e) { alert("添加失败:" + e.message); return false; }
  if (!(r && r.ok)) { alert("添加失败:" + ((r && r.error) || "未知")); return false; }
  return true;
}

async function _wfRepoRemove(domId, project) {
  if (!confirm(`从工作流移除「${project.path}」？只移除引用，不删除磁盘目录。`)) return false;
  let r;
  try {
    r = await api(`/workbench/domains/${encodeURIComponent(domId)}/repos`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ op: "remove", path: project.path }), timeout: 60000 });
  } catch (e) { alert("移除失败:" + e.message); return false; }
  if (!(r && r.ok)) { alert("移除失败:" + ((r && r.error) || "未知")); return false; }
  return true;
}

// 聊天框构件(仿 ChatGPT/Claude):Enter 发送=在本域开会话跑 prompt,onCreated 可挂后置动作(如归入任务)
function _wfChatComposer(domId, ph, onCreated) {
  const chat = document.createElement("div");
  chat.className = "wf-chat";
  chat.onclick = (ev) => ev.stopPropagation();
  const ta = document.createElement("textarea");
  ta.className = "wf-chat-in";
  ta.rows = 1;
  ta.placeholder = ph;
  ta.spellcheck = false;
  const btn = document.createElement("button");
  btn.className = "wf-chat-send";
  btn.title = "发送并开会话";
  btn.disabled = true;
  btn.innerHTML = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="m5 12 7-7 7 7"/></svg>';
  let busy = false;
  const send = async () => {
    const v = ta.value.trim();
    if (!v || busy) return;
    busy = true; btn.disabled = true; ta.disabled = true;
    let r = null;
    try {
      // TUI 退场后这里不再起终端:建一段 GUI 会话,首句走它自己的发送路径(见 newHtmlSessionWithPrompt)。
      r = await newHtmlSessionWithPrompt({ domain: domId }, v);
    } catch { r = null; }
    if (!r || !r.id) { busy = false; ta.disabled = false; btn.disabled = !ta.value.trim(); return; }
    if (onCreated) { try { await onCreated(r); } catch {} }
    setTermHead({ id: r.id, title: v.slice(0, 40), domain: domId, engine: curEngine });   // 创建响应只有 id:域名补上,收回/返回钮才认得出工作流会话
    navPush({ kind: "sess", id: r.id, domain: domId });
    await refreshTerms();
    renderSessionPanel();
  };
  ta.oninput = () => {
    btn.disabled = !ta.value.trim() || busy;
    ta.style.height = "auto";
    ta.style.height = Math.min(140, ta.scrollHeight) + "px";
  };
  ta.onkeydown = (ev) => {
    if (ev.isComposing || ev.keyCode === 229) return;
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); send(); }
  };
  btn.onclick = (ev) => { ev.stopPropagation(); send(); };
  chat.appendChild(ta);
  chat.appendChild(btn);
  return chat;
}
