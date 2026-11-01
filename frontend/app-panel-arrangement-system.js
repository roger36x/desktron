// “安排”中的系统任务组件：记忆引擎的真实运行态。
// 这些是跟随 Desk 常驻运行的系统安排，不属于已下线的代理资源管理页。
const ARR_SYS_ICONS = {
  memory: '<svg viewBox="0 0 32 32" data-desk-scattered="14" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M27 7.5c0 2.5-4.9 4.5-11 4.5S5 10 5 7.5 9.9 3 16 3s11 2 11 4.5ZM5 7.5v17c0 2.5 4.9 4.5 11 4.5s11-2 11-4.5v-17M5 16c0 2.5 4.9 4.5 11 4.5s11-2 11-4.5"/></g></svg>',
};
const ARR_SYS_BACK_ICON = '<svg viewBox="0 0 32 32" data-desk-scattered="16" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><g transform="translate(32 0) scale(-1 1)"><g transform="translate(-1.65323 -1.08592) scale(1.06787)"><path d="m11 3.5 10.2 10.6c1.1 1.1 1.1 2.7 0 3.8L11 28.5" stroke-width="2.6"/></g></g></g></svg>';
const _arrSysLogs = new Set();     // 打开日志框的卡

function _arrSysRel(ts) {
  if (!ts) return "";
  const d = Math.max(0, Date.now() / 1000 - ts);
  if (d < 90) return DeskI18n.tx("刚刚");
  if (d < 3600) return DeskI18n.tx("{count} 分钟前", { count: Math.round(d / 60) });
  if (d < 86400) return DeskI18n.tx("{count} 小时前", { count: Math.round(d / 3600) });
  return DeskI18n.tx("{count} 天前", { count: Math.round(d / 86400) });
}
// short=true 时不带“下次”前缀，用在已有“下一次”标题的位置。
function _arrSysNext(ts, short = false) {
  if (!ts) return "";
  const d = ts - Date.now() / 1000;
  if (d <= 60) return DeskI18n.tx("即将运行");
  const amount = d < 3600 ? DeskI18n.tx("{count} 分钟后", { count: Math.round(d / 60) })
    : DeskI18n.tx("{count} 小时后", { count: Math.round(d / 3600) });
  return DeskI18n.tx(short ? "约 {time}" : "下次约 {time}", { time: amount });
}
function _arrSysCard({ kind, name, desc, running, state, stateClass, last, notice, lead, facts,
                    activityTitle, activity, jobsTitle, jobs, detailNote, actions, logs }) {
  const chev = '<svg viewBox="0 0 32 32" data-desk-scattered="17" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><g transform="translate(-1.65323 -1.08592) scale(1.06787)"><path d="m11 3.5 10.2 10.6c1.1 1.1 1.1 2.7 0 3.8L11 28.5" stroke-width="2.6"/></g></g></svg>';
  return `<article class="arr-sys-agent" data-kind="${kind}">
    <button class="arr-sys-head" type="button">
      <span class="arr-sys-icon">${ARR_SYS_ICONS[kind] || ARR_SYS_ICONS[kind.split(":")[0]] || ""}</span>
      <span class="arr-sys-copy"><span class="arr-sys-name">${name}</span><span class="arr-sys-desc">${desc}</span></span>
      <span class="arr-sys-time"><strong class="arr-sys-state ${stateClass || ""}"><i></i>${DeskI18n.txh(state)}</strong><span>${last || ""}</span></span>
      <span class="arr-sys-chev">${chev}</span>
    </button>
  </article>`;
}

// 列表只回答「它是谁、现在怎样」；运行结果、内部指标和动作进入独立资源页。
// 这样手机端不再把一张长卡塞回列表，桌面端也不会因展开一项把后续资源推到屏外。
function _arrSysRenderResource(body, card, back, reload, options = {}) {
  const icon = ARR_SYS_ICONS[card.kind] || "";
  const actionHtml = (card.actions || []).map((a, i) =>
    `<button type="button" class="arr-sys-act${i === 0 ? " primary" : ""}" data-act="${a.act}">${a.icon || ""}${a.label}</button>`).join("");
  const factHtml = (card.facts || []).map((f) =>
    `<div class="arr-sys-resource-fact"><span>${f[0]}</span><strong>${f[1]}</strong></div>`).join("");
  const outcomeHtml = (card.activity || []).map((r) =>
    `<div class="arr-sys-outcome"><span>${r[0]}</span><strong class="${r[2] ? "warn" : ""}">${r[1]}</strong></div>`).join("");
  const jobHtml = (card.jobs || []).map((j) =>
    `<div class="arr-sys-job"><span class="arr-sys-job-name">${j[0]}</span><span class="arr-sys-job-trigger">${j[1]}</span><span class="arr-sys-job-result${j[3] ? " warn" : ""}">${j[2]}</span></div>`).join("");
  const logHtml = (card.logs || []).map((l) => `<div>${escapeHtml(l)}</div>`).join("");
  body.className = options.bodyClass || "md arrangements-page arr-sys-resource-page";
  body.innerHTML = `<button type="button" class="arr-sys-resource-back">${ARR_SYS_BACK_ICON}<span>${escapeHtml(options.backLabel || DeskI18n.tx("系统安排"))}</span></button>
    <header class="arr-sys-resource-head">
      <span class="arr-sys-resource-icon" data-kind="${card.kind}">${icon}</span>
      <span class="arr-sys-resource-copy"><h1>${card.name}</h1><p>${card.desc}</p></span>
      <span class="arr-sys-resource-state"><strong class="arr-sys-state ${card.stateClass || ""}"><i></i>${DeskI18n.txh(card.state)}</strong><span>${card.last || ""}</span></span>
    </header>
    <nav class="arr-sys-resource-tabs"><button type="button" class="on">${DeskI18n.txh("运行状态")}</button></nav>
    <section class="arr-sys-resource-pane">
      <div class="arr-sys-status-intro"><div><h2>${card.state === "正常" ? DeskI18n.txh("运行正常") : DeskI18n.txh(card.state)}</h2><p>${card.lead || ""}</p></div><div class="arr-sys-resource-actions">${actionHtml}</div></div>
      ${card.notice ? `<div class="arr-sys-notice"><svg viewBox="0 0 32 32" data-desk-scattered="18" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><g transform="translate(0 0) scale(1)"><circle cx="16" cy="16" r="13.7"/><path d="M16 11v8"/><circle cx="16" cy="23.8" r="1.2" fill="currentColor" stroke="none"/></g></g></svg><span>${card.notice}</span></div>` : ""}
      <div class="arr-sys-status-grid"><div><div class="arr-sys-block-title">${card.activityTitle || DeskI18n.txh("最近结果")}</div><div class="arr-sys-outcomes">${outcomeHtml || '<div class="arr-sys-empty">' + DeskI18n.txh("还没有运行记录") + '</div>'}</div></div><div><div class="arr-sys-block-title">${DeskI18n.txh("当前状态")}</div><div class="arr-sys-resource-facts">${factHtml}</div></div></div>
      <details class="arr-sys-run-details"${_arrSysLogs.has(card.kind) ? " open" : ""}><summary>${DeskI18n.txh("运行详情")}</summary><div class="arr-sys-run-inner">
        ${card.detailNote ? `<div class="arr-sys-detail-note">${card.detailNote}</div>` : ""}
        ${jobHtml ? `<div class="arr-sys-detail-title">${card.jobsTitle || DeskI18n.txh("内部自动化")}</div><div class="arr-sys-jobs">${jobHtml}</div>` : ""}
        <div class="arr-sys-logs${_arrSysLogs.has(card.kind) ? " show" : ""}">${logHtml}</div>
      </div></details>
    </section>`;
  body.querySelector(".arr-sys-resource-back").onclick = back;
  body.querySelectorAll(".arr-sys-resource-actions .arr-sys-act").forEach((b) => {
    b.onclick = () => _arrSysAction(b.dataset.act, () => reload());
  });
}
function _arrSysCards(st) {
  // 卡片字段直接进 HTML：固定文字用 DeskI18n.txh，参数在其中转义。state 保留中文原值供比较，
  // 显示时经 _arrSysCard / _arrSysRenderResource 翻译。
  const T = (zh, params) => DeskI18n.txh(zh, params);
  const cards = [];
  if (st.memory) {
    const m = st.memory;
    const pm = m.project_memory || {};
    const recap = m.recap || {};
    const recapBad = recap.failed_engines || [];
    const jerr = pm.last_error || {};
    const sep = DeskI18n.tx("、");
    // 停机通知按「先报真错、再报环境噪声」排:host-slept 类失败不推停机线,只作提示
    const notices = [];
    if (pm.stopped) notices.push(T("记忆引擎连续 {count} 轮失败已停机{reason}，清 engine-state.json 的 fails 恢复。",
      { count: pm.fails, reason: jerr.reason ? `(${jerr.reason})` : "" }));
    else if (jerr.reason && String(jerr.reason).startsWith("host-slept")) notices.push(T("记忆引擎上轮因宿主睡眠中断，未计停机线，醒后自动重试。"));
    // 带上各引擎最近一次的失败原因:只有引擎名的话,限流、断网、会话找不着看起来一个样。
    const recapWhy = recapBad.map((n) => {
      const d = ((recap.engines || {})[n] || {}).last_error || {};
      return d.detail ? `${n}(${d.detail})` : n;
    });
    if (recapBad.length) notices.push(T("会话 recap 异常：{list}。", { list: recapWhy.join(sep) }));
    const jStatus = pm.stopped ? T("连败停机 {count}/3", { count: pm.fails })
      : (jerr.reason ? T("上轮 {reason}", { reason: String(jerr.reason).split("/").pop() })
                     : (m.project_memory_pending ? T("待刷新 {count} 区", { count: m.project_memory_pending }) : T("已是最新")));
    // SOT 合入提名:四层里唯一要人审的一层。提名此前只落在报告文件里,人不会主动去翻,
    // sot 因此长期只有一条。摆到卡片正面,提名有就催一次。
    const noms = m.sot_nominations || [];
    if (noms.length) notices.push(T("SOT 合入提名 {count} 条待人审：{list}{more}。",
      { count: noms.length, list: noms.slice(0, 3).join(sep), more: noms.length > 3 ? DeskI18n.tx(" 等") : "" }));
    const memoryWarn = pm.stopped || recapBad.length;
    const memLead = pm.stopped
      ? T("记忆沉淀已停止，需要处理后才能继续。")
      : (recapBad.length ? T("会话捕获在 {list} 上失败，项目记忆仍会消费已有流水。", { list: recapBad.join(sep) })
      : (noms.length ? T("记忆引擎正常运行，有 {count} 条 SOT 合入提名等待确认。", { count: noms.length })
                     : (m.candidates ? T("最近一轮已完成，有 {count} 项候选等待查阅。", { count: m.candidates })
                                     : T("最近一轮已完成，没有需要你处理的问题。"))));
    const recapStatus = recapBad.length ? T("{count} 个引擎失败", { count: recapBad.length })
      : (recap.last_ok ? (recap.last_write ? T("{time}写入", { time: _arrSysRel(recap.last_write) }) : T("已运行，无实质内容"))
                       : T("等待首个完成回合"));
    const next = _arrSysNext(m.next_run, true);
    cards.push({
      kind: "memory", name: T("记忆引擎"), running: true,
      desc: T("捕获会话、蒸馏判断、汇总工作流记忆"),
      state: memoryWarn ? "需处理" : "正常", stateClass: memoryWarn ? "warn" : "on",
      last: m.last_run ? T("{time}完成", { time: _arrSysRel(m.last_run) }) : T("尚未运行"),
      notice: notices.join(" "),
      lead: memLead,
      facts: [[T("最近完成"), escapeHtml(_arrSysRel(m.last_run) || "—")], [T("待查阅确认"), T("{count} 项", { count: m.candidates ?? 0 })],
              [T("下一次"), next ? escapeHtml(next) : T("随 Desk 常驻")]],
      activityTitle: T("本轮结果"),
      activity: [
        [T("会话与候选"), m.candidates ? T("{count} 项待查阅", { count: m.candidates }) : T("无待处理"), false],
        [T("项目记忆"), m.project_memory_pending ? T("{count} 区待刷新", { count: m.project_memory_pending }) : T("已是最新"), false],
        [T("SOT 合入提名"), noms.length ? T("{count} 条待人审", { count: noms.length }) : T("无"), false],
      ],
      jobsTitle: T("内部自动化"),
      jobs: [
        [T("会话 recap"), T("回合结束后"), recapStatus, !!recapBad.length],
        [T("项目记忆生成"), T("每 {count} 分钟", { count: Math.round((m.interval_s || 1800) / 60) }), jStatus, !!pm.stopped],
        [T("候选与复验分析"), T("随引擎轮"), T("候选 {count}", { count: m.candidates ?? 0 }), false],
      ],
      actions: [
        { act: "mem-run", label: T("立即运行"), icon: '<svg viewBox="0 0 32 32" data-desk-scattered="19" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M7.2 8.8a11.4 11.4 0 1 1-2 12M5 3.5v7.3h7.3"/></g></svg>' },
        { act: "mem-logs", label: T("查看报告") },
      ],
      detailNote: T("项目记忆 {time} · 已覆盖 {count} 个工作区",
        { time: pm.last_ok ? _arrSysRel(pm.last_ok) : DeskI18n.tx("未生成过"), count: pm.ws_done || 0 }),
      logs: m.report_tail || [],
    });
  }
  return cards;
}
async function _arrSysAction(act, rerender) {
  if (act === "mem-run") { try { await api("/workbench/agents/run-once", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ agent: "memory" }) }); } catch {} setTimeout(rerender, 2500); return; }
  if (act === "mem-logs") { _arrSysLogs.has("memory") ? _arrSysLogs.delete("memory") : _arrSysLogs.add("memory"); rerender(true); return; }
}
