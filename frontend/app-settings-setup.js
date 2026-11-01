// 首次设置就绪中心。状态只来自 /workbench/setup；本文件只负责信息结构与真实动作入口。
// 首启自动打开一次，之后从设置页进入。进度严格采用后端 required，可选项不计入。

let _setupBack = null;
let _setupResumeButton = null;

const _SETUP_CARD_IDS = ["runtime", "engines", "tools"];
const _SETUP_NEXT_CARD = {
  runtime: "runtime", engines: "engines", engine_login: "engines",
};

function _setupIcon(name) {
  return (typeof ICONS !== "undefined" && ICONS[name]) || "";
}

function _setupEngineSummary(st) {
  const all = (((st.groups || {}).engines || {}).detail || []);
  const installed = all.filter((e) => e.installed);
  const ready = installed.filter((e) => e.logged_in).length;
  const unknown = installed.filter((e) => e.unknown).length;
  return { all, installed, ready, unknown };
}

function _setupCardData(st) {
  const groups = st.groups || {};
  const rt = groups.runtime || {};
  const eng = _setupEngineSummary(st);
  const login = groups.engine_login || {};
  const tools = groups.intranet || {};
  return [
    {
      id: "runtime", icon: "laptop", title: "本机环境",
      caption: rt.ok ? "Python · Node · Git" : "存在缺失或版本过低",
      done: !!rt.ok, pending: !!rt.pending, optional: false,
    },
    {
      id: "engines", icon: "code", title: "AI 引擎",
      caption: `${eng.installed.length} 个已安装 · ${eng.ready} 个可用`,
      done: !!((groups.engines || {}).ok && login.ok), pending: eng.unknown > 0, optional: false,
    },
    {
      id: "tools", icon: "lock", title: "工作工具",
      caption: tools.pending ? "正在确认登录状态" : "lark-cli",
      done: !!tools.ok && !tools.pending, pending: !!tools.pending, optional: true,
    },
  ];
}

function _setupDefaultCard(st) {
  const next = _SETUP_NEXT_CARD[st.next];
  return next || "runtime";
}

function _setupIntro(st) {
  if (st.complete) {
    return {
      title: "Desk 已经可以开始工作",
      lead: "本机环境与 AI 引擎均已就绪。工作工具是可选项，需要时再连接。",
    };
  }
  const copy = {
    runtime: ["先补齐这台电脑的运行环境", "Python、Node 或 Git 未达到运行要求。补齐后重新检查即可。"],
    engines: ["安装一个 AI 引擎", "选择你常用的引擎。安装脚本会自动执行，完成后再登录。"],
    engine_login: ["连接一个 AI 引擎", "尚未安装时先选择一家；授权页面会自动打开。"],
  };
  const [title, lead] = copy[st.next] || ["完成 Desk 的运行准备", "依次处理下方尚未就绪的项目。"];
  return { title, lead };
}

function _setupAction(label, action, kind = "secondary", extra = {}) {
  const attrs = Object.entries(extra).map(([key, value]) => key === "disabled" && value
    ? " disabled" : ` data-${key}="${escapeHtml(encodeURIComponent(String(value)))}"`).join("");
  return `<button class="setup-button ${kind}" data-setup-action="${action}" type="button"${attrs}>${DeskI18n.txh(label)}</button>`;
}

function _setupEngineLogin(key) {
  return ({ claude: "claude auth login", codex: "codex login", pi: "pi" })[key] || "";
}

function _setupDetailRow(icon, name, note, status, tone = "") {
  return `<div class="setup-detail-row">
    <div class="setup-detail-main"><span class="setup-detail-icon">${_setupIcon(icon)}</span>
      <span class="setup-detail-name">${DeskI18n.txh(name)}${note ? `<small>${DeskI18n.txh(note)}</small>` : ""}</span></div>
    <span class="setup-detail-status ${tone}">${DeskI18n.txh(status)}</span>
  </div>`;
}

function _setupRuntimePanel(st) {
  const g = (st.groups || {}).runtime || {};
  const all = ((g.detail || {}).rows || []);
  const bad = all.filter((r) => !r.ok);
  const rows = all.map((r) => {
    // 缺了的那一条把 hint 显示出来:预填的命令只有一条,而 hint 里写着备选
    // (node 那条的 nvm)。命令跑不通时人得有下一步可看,不然就停在这儿了。
    const minimum = r.min ? DeskI18n.tx("最低 {version}", { version: r.min }) : "运行依赖";
    const need = r.ok ? minimum : (r.hint || minimum);
    const status = r.ok ? (r.version || "已安装") : (r.version ? DeskI18n.tx("{version} · 版本过低", { version: r.version }) : "未安装");
    return _setupDetailRow("terminal", r.name, need, status, r.ok ? "ready" : "attention");
  }).join("");
  // 缺依赖是死路,不是提示:Desk 起不来、引擎也装不上。原来这里只有「重新检查」,
  // 等于让人自己去别处找装法(2026-09-02 复盘:node 在新 Mac 上最常缺)。
  const actions = bad.filter((r) => r.install).map((r) =>
    _setupAction(DeskI18n.tx("安装 {name}", { name: r.name }), "command", "primary",
      { command: r.install, head: DeskI18n.tx("安装 {name}", { name: r.name }) + " · " + r.install }));
  actions.push(_setupAction("重新检查本机", "refresh", actions.length ? "secondary" : "secondary"));
  return `<div class="setup-focus-meta"><span>${_setupIcon("laptop")}</span><b>${DeskI18n.txh("本机环境")}</b></div>
    <h2>${g.ok ? DeskI18n.txh("这台电脑可以运行 Desk") : DeskI18n.txh("运行环境还未就绪")}</h2>
    <div class="setup-detail-list">${rows || _setupDetailRow("terminal", "运行环境", "manifest", "暂时无法读取")}</div>
    <div class="setup-focus-actions">${actions.join("")}</div>`;
}

function _setupEnginesPanel(st) {
  const groups = st.groups || {};
  const eng = _setupEngineSummary(st);
  const rows = eng.all.map((e) => {
    let status = "未安装", tone = "";
    if (e.installed && e.unknown) { status = "状态未知"; tone = "muted"; }
    else if (e.installed && e.logged_in) { status = "已登录"; tone = "ready"; }
    else if (e.installed) { status = "未登录"; tone = "attention"; }
    return _setupDetailRow("code", e.label, e.installed ? "已安装" : "可选引擎", status, tone);
  }).join("");
  const actions = [];
  const missing = eng.all.filter((e) => e.installed && !e.logged_in && !e.unknown);
  missing.forEach((e) => {
    const cmd = _setupEngineLogin(e.key);
    if (cmd) actions.push(_setupAction(DeskI18n.tx("登录 {name}", { name: e.label }), "command", "primary",
      { command: cmd, head: e.key === "pi" ? DeskI18n.tx("Pi 登录 · 进入后输入 /login")
        : DeskI18n.tx("{name} 登录", { name: e.label }) + " · " + cmd }));
  });
  const uninstalled = eng.all.filter((e) => !e.installed && e.install);
  uninstalled.forEach((e, index) => actions.push(_setupAction(DeskI18n.tx("安装 {name}", { name: e.label }), "command",
    !eng.installed.length && index === 0 ? "primary" : "secondary",
    { command: e.install, head: DeskI18n.tx("安装 {name}", { name: e.label }) + " · " + e.install })));
  actions.push(_setupAction("重新检查", "refresh", "secondary"));
  return `<div class="setup-focus-meta"><span>${_setupIcon("code")}</span><b>${DeskI18n.txh("AI 引擎")}</b></div>
    <h2>${groups.engine_login?.ok ? DeskI18n.txh("AI 引擎已经连接") : (groups.engines?.ok ? DeskI18n.txh("登录一个已安装的引擎") : DeskI18n.txh("选择并安装一个引擎"))}</h2>
    <p>${DeskI18n.txh("选择一家即可。脚本负责安装，密码、扫码与网页授权由你确认。")}</p>
    <div class="setup-detail-list">${rows}</div>
    <div class="setup-focus-actions">${actions.join("")}</div>`;
}

function _setupToolsPanel(st) {
  const g = (st.groups || {}).intranet || {};
  const rows = (g.detail || []);
  const body = rows.map((r) => {
    let status = "未安装", tone = "";
    if (r.unknown) { status = g.pending ? "查询中" : "状态未知"; tone = "muted"; }
    else if (r.keychain) { status = "钥匙串受阻"; tone = "attention"; }
    else if (r.logged_in) { status = "已登录"; tone = "ready"; }
    else if (r.installed) { status = "未登录"; tone = "attention"; }
    return _setupDetailRow("lock", r.label, r.hint, status, tone);
  }).join("");
  const actions = [];
  for (const r of rows) {
    // 钥匙串这一档不是没登录,给的是 lark-cli 自己的解法;登录按钮在这里帮不上忙。
    if (r.installed && r.keychain) {
      const cmd = r.fix || "lark-cli config keychain-downgrade";
      actions.push(_setupAction(DeskI18n.tx("修复 {name} 钥匙串", { name: r.label }), "command", "primary",
        { command: cmd, head: DeskI18n.tx("解除钥匙串限制 · {command}", { command: cmd }) }));
      continue;
    }
    if (r.installed && !r.logged_in && !r.unknown) {
      // lark-cli 的 broad setup 必须明确授权域；不带参数会把权限选择重新留给用户。
      const cmd = "lark-cli auth login --domain all";
      actions.push(_setupAction(DeskI18n.tx("登录 {name}", { name: r.label }), "command", "primary",
        { command: cmd, head: DeskI18n.tx("{name} 登录", { name: r.label }) + " · " + cmd }));
    }
  }
  actions.push(_setupAction(g.pending ? "更新状态" : "重新检查", "refresh", "secondary"));
  return `<div class="setup-focus-meta"><span>${_setupIcon("lock")}</span><b>${DeskI18n.txh("工作工具")}</b></div>
    <h2>${g.pending ? DeskI18n.txh("正在确认登录状态") : (g.ok ? DeskI18n.txh("工作工具已经连接") : DeskI18n.txh("连接工作工具（可选）"))}</h2>
    <p>${DeskI18n.txh("lark-cli 让 Agent 读写飞书文档、消息与日历。不使用飞书可以跳过。")}${rows.some((r) => !r.installed)
      ? " " + DeskI18n.txh("尚未安装时，请按 https://github.com/larksuite/cli 的说明安装后重新检查。") : ""}</p>
    <div class="setup-detail-list">${body}</div>
    <div class="setup-focus-actions">${actions.join("")}</div>`;
}

function _setupFocusPanel(st, selected, back) {
  return ({ runtime: _setupRuntimePanel, engines: _setupEnginesPanel,
    tools: _setupToolsPanel }[selected] || _setupRuntimePanel)(st);
}

// 切换语言时只按已读到的状态重绘，不重新检查环境或登录。
globalThis.addEventListener?.("desk-locale", () => {
  if (!_setupBack || !_setupBack.isConnected || !_setupBack._setupState) return;
  DeskI18n.localize(_setupBack);
  _setupPaint(_setupBack, _setupBack._setupState);
});

async function openSetupGuide(opts) {
  if (_setupBack) return;
  let st;
  try { st = await api("/workbench/setup", { timeout: 20000 }); } catch (e) {
    if (opts && opts.auto) return;
    alert(DeskI18n.tx("读取设置状态失败：{error}", { error: e.message })); return;
  }
  if (opts && opts.auto && st.seen) return;

  const back = document.createElement("div");
  back.className = "auth-tui-back setup-back";
  back._setupState = st;
  back._setupSelected = _setupDefaultCard(st);
  back.innerHTML = `<div class="auth-tui-panel setup-panel" role="dialog" aria-modal="true" aria-label="设置 Desktron">
    <header class="setup-head">
      <div class="setup-brand"><span>${_setupIcon("laptop")}</span><b>Desktron</b></div>
      <div class="setup-head-actions"><button data-setup-action="resume-operation" type="button" hidden>查看安装进度</button>
        <button class="setup-rescan" data-setup-action="refresh" type="button">重新检查</button>
        <button class="auth-tui-x" data-setup-action="close" aria-label="关闭">×</button></div>
    </header>
    <div class="setup-workspace"><div class="setup-page">
      <section class="setup-intro"><div><div class="setup-eyebrow">首次设置 · 本机</div><h1></h1><p></p></div>
        <div class="setup-score"><strong></strong><span>必要项已就绪</span></div></section>
      <nav class="setup-readiness" aria-label="Desk 就绪状态"></nav>
      <section class="setup-content-grid"><article class="setup-focus-panel"><div class="setup-focus-body"></div></article></section>
      <footer class="setup-footer"><span class="setup-checked-at"></span><div>
        <button class="setup-button secondary setup-skip" data-setup-action="close" type="button">稍后继续</button>
        <button class="setup-button primary setup-enter" data-setup-action="close" type="button">开始使用 Desk</button>
      </div></footer>
    </div></div>
    <div class="setup-operation" hidden><section class="setup-operation-panel" role="dialog" aria-modal="true" aria-label="操作助手">
      <header class="setup-operation-head"><div><small>操作助手</small><b class="setup-termtt">准备操作</b></div>
        <button data-setup-action="hide-terminal" type="button" aria-label="返回设置">×</button></header>
      <div class="setup-operation-guide"><span class="setup-operation-state"><i></i><b>正在处理</b></span>
        <p>操作已经自动开始，无需输入命令。只有终端明确要求确认、验证码或密码时才需要输入。</p>
        <p class="setup-operation-progress" role="status" hidden></p></div>
      <div class="setup-termhost" translate="no"></div>
      <footer class="setup-operation-actions"><button class="setup-open-auth" data-setup-action="open-auth" type="button" hidden>打开授权页面</button>
        <button class="setup-retry-auth" data-setup-action="retry-auth" type="button" hidden>重新获取授权链接</button>
        <span></span><button data-setup-action="hide-terminal" type="button">返回设置</button>
        <button class="primary" data-setup-action="finish-terminal" type="button">完成后检查状态</button></footer>
    </section></div></div>`;
  DeskI18n.localize(back);   // 模板里的固定文字；动态部分由 _setupPaint 与各反馈函数生成
  back.querySelector('.setup-head-actions').prepend(DeskI18n.picker());
  for (const [selector, key, attr] of [
    ['.setup-eyebrow', 'setup.title'], ['.setup-rescan', 'setup.check'],
    ['.auth-tui-x', 'setup.close', 'aria-label'],
    ['[data-setup-action="resume-operation"]', 'setup.progress'],
  ]) DeskI18n.bind(back.querySelector(selector), key, {}, attr || 'text');
  document.body.appendChild(back);
  _setupBack = back;

  const close = () => {
    if (!_setupBack) return;
    if (back._setupPoll) clearTimeout(back._setupPoll);
    if (back._setupTaskPoll) clearTimeout(back._setupTaskPoll);
    if (back._setupTerm) { try { back._setupTerm.dispose(); } catch {} }
    _setupBack = null; back.remove(); _smCache = null;
    if (typeof renderSettings === "function" && document.getElementById("sm-host")) renderSettings();
  };
  back._setupClose = close;
  back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
  back.addEventListener("click", (e) => _setupHandleClick(back, e));

  if (!st.seen) {
    try { api("/workbench/setup", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ seen: true }) }); } catch {}
  }
  _setupPaint(back, st);
  _setupPollPending(back);
  await _setupFindTask(back);
}

async function _setupFindTask(back) {
  const button = back.querySelector('[data-setup-action="resume-operation"]');
  try {
    const result = await api("/workbench/setup-tasks");
    if (!back.isConnected) return;
    const rows = result.tasks || [];
    back._setupSavedTask = rows.find((r) => r.attachable) || rows[0] || null;
    if (button) { button.hidden = !back._setupSavedTask; button.textContent = DeskI18n.t("setup.progress"); }
  } catch {
    if (button && back.isConnected) { button.hidden = false; button.textContent = DeskI18n.tx("重新读取安装进度"); }
    return null;
  }
  return back._setupSavedTask;
}

async function _setupResumeTask(back) {
  const task = await _setupFindTask(back);
  if (!task || !back.isConnected) return;
  await _setupRunInPanel(back, "", task.title, { restoreTask: task });
}

async function _setupWatchTask(back) {
  if (back._setupTaskPoll) clearTimeout(back._setupTaskPoll);
  const generation = back._setupTaskWatchGeneration = (back._setupTaskWatchGeneration || 0) + 1;
  const id = back._setupSavedTask?.id;
  if (!id || !back.isConnected) return;
  let task = back._setupSavedTask, retry = false;
  const line = back.querySelector(".setup-operation-progress");
  try {
    const result = await api("/workbench/setup-tasks");
    if (!back.isConnected || generation !== back._setupTaskWatchGeneration || back._setupSavedTask?.id !== id) return;
    const found = result.tasks?.find(row => row.id === id);
    if (!found) throw new Error("任务记录尚未找到");
    task = back._setupSavedTask = found;
    if (line) line.hidden = true;
  } catch {
    if (!back.isConnected || generation !== back._setupTaskWatchGeneration || back._setupSavedTask?.id !== id) return;
    retry = true;
    if (line) { line.hidden = false; line.textContent = DeskI18n.tx("暂时读不到最新进度，原任务可能仍在运行。请勿重复执行。"); }
  }
  if (back.isConnected && (retry || ["starting", "waiting_terminal", "running"].includes(task.state)))
    back._setupTaskPoll = setTimeout(() => _setupWatchTask(back), 5000);
}

// 首启浮层只自动出现一次，但“设置未完成”不能跟着消失。关闭浮层、切页或重启后，
// 底栏仍保留一个轻量入口，回到同一份实时检查清单继续。
async function setupResumeEntry(state) {
  if (typeof isLocalView === "function" && !isLocalView()) return;
  let st = state;
  if (!st) {
    try { st = await api("/workbench/setup", { timeout: 20000 }); } catch { return; }
  }
  if (st.complete && !st.operation_pending) {
    _setupResumeButton?.remove();
    _setupResumeButton = null;
    return;
  }
  if (!_setupResumeButton?.isConnected) {
    const settings = document.getElementById("settings-btn");
    if (!settings?.parentNode) return;
    const button = document.createElement("button");
    button.id = "foot-setup";
    button.className = "foot-be";
    button.type = "button";
    button.onclick = () => openSetupGuide({});
    settings.parentNode.insertBefore(button, settings);
    _setupResumeButton = button;
  }
  DeskI18n.bind(_setupResumeButton, st.complete ? "setup.progress" : "setup.resume");
  DeskI18n.bind(_setupResumeButton, st.complete ? "setup.progressHint" : "setup.resumeHint", {}, "title");
}

function _setupPaint(back, st) {
  if (!back.isConnected) return;
  back._setupState = st;
  setupResumeEntry(st);
  if (!_SETUP_CARD_IDS.includes(back._setupSelected)) back._setupSelected = _setupDefaultCard(st);
  const intro = _setupIntro(st);
  back.querySelector(".setup-intro h1").textContent = intro.title;
  back.querySelector(".setup-intro p").textContent = intro.lead;
  const introKey = st.complete ? 'complete' : (['runtime', 'engines', 'engine_login'].includes(st.next) ? st.next : 'default');
  DeskI18n.bind(back.querySelector('.setup-intro h1'), 'setup.' + introKey + '.title', {}, 'text', intro.title);
  DeskI18n.bind(back.querySelector('.setup-intro p'), 'setup.' + introKey + '.lead', {}, 'text', intro.lead);
  const done = st.done || 0;
  const total = st.total || 0;
  const remaining = Math.max(0, total - done);
  const score = back.querySelector(".setup-score");
  score.classList.toggle("ready", !!st.complete);
  score.classList.toggle("attention", !st.complete);
  score.querySelector("strong").textContent = st.complete ? "已就绪" : `还需完成 ${remaining} 项`;
  score.querySelector("span").textContent = st.complete ? `${done}/${total} 项必要能力可用` : "处理下方“当前步骤”后即可开始";
  const scoreLabel = score.querySelector('strong'), scoreNote = score.querySelector('span');
  DeskI18n.bind(scoreLabel, st.complete ? 'setup.ready' : 'setup.remaining', {count: remaining}, 'text', scoreLabel.textContent);
  DeskI18n.bind(scoreNote, st.complete ? 'setup.scoreComplete' : 'setup.scoreNext', {done, total}, 'text', scoreNote.textContent);

  const nextCard = _SETUP_NEXT_CARD[st.next] || "";
  back.querySelector(".setup-readiness").innerHTML = _setupCardData(st).map((card) => {
    const label = card.pending ? "查询中" : (card.done ? "已就绪" : (card.optional ? "可选" : (card.id === nextCard ? "现在处理" : "稍后处理")));
    const cls = [card.done ? "done" : "", card.pending ? "pending" : "", card.id === nextCard ? "current" : "",
      card.id === back._setupSelected ? "selected" : ""].filter(Boolean).join(" ");
    return `<button class="setup-system-node ${cls}" data-setup-select="${card.id}" type="button" aria-pressed="${card.id === back._setupSelected}"${card.id === nextCard ? ' aria-current="step"' : ""}>
      <span class="setup-node-top"><span class="setup-node-icon">${_setupIcon(card.icon)}</span><span class="setup-node-state">${label}</span></span>
      <b>${escapeHtml(card.title)}</b><small>${escapeHtml(card.caption)}</small></button>`;
  }).join("");
  const cards = _setupCardData(st);
  back.querySelectorAll('.setup-system-node').forEach((node, index) => {
    const card = cards[index];
    const stateNode = node.querySelector('.setup-node-state');
    DeskI18n.bind(node.querySelector('b'), 'setup.' + card.id, {}, 'text', card.title);
    DeskI18n.bind(stateNode, 'setup.' + (card.pending ? 'pending' : card.done ? 'ready' : card.optional ? 'optional' : card.id === nextCard ? 'current' : 'later'), {}, 'text', stateNode.textContent);
    const eng = _setupEngineSummary(st);
    const captionKey = card.id === 'runtime' ? (st.groups?.runtime?.ok ? 'runtimeOK' : 'runtimeMissing') :
      card.id === 'engines' ? 'engineCount' : (st.groups?.intranet?.pending ? 'toolsChecking' : 'toolsNames');
    DeskI18n.bind(node.querySelector('small'), 'setup.' + captionKey, {installed: eng.installed.length, ready: eng.ready}, 'text', card.caption);
  });
  back.querySelector(".setup-focus-body").innerHTML = _setupFocusPanel(st, back._setupSelected, back);
  const focus = back.querySelector(".setup-focus-panel");
  const isCurrent = !st.complete && back._setupSelected === nextCard;
  focus.classList.toggle("is-current-step", isCurrent);
  const primary = focus.querySelector(".setup-focus-actions .primary");
  if (isCurrent && primary) {
    primary.classList.add("setup-next-action");
    const prompt = document.createElement("div");
    prompt.className = "setup-action-prompt";
    prompt.innerHTML = `<span>${DeskI18n.txh("当前步骤")}</span><b></b>`;
    prompt.querySelector("b").textContent = DeskI18n.tx("点击“{action}”继续", { action: primary.textContent.trim() });
    primary.closest(".setup-focus-actions").before(prompt);
  }
  const enter = back.querySelector(".setup-enter");
  const skip = back.querySelector(".setup-skip");
  enter.textContent = st.complete ? "开始使用 Desk" : "暂不处理，进入 Desk";
  enter.dataset.setupAction = "close";
  skip.textContent = "稍后继续";
  skip.hidden = !!st.complete;
  DeskI18n.bind(enter, st.complete ? 'setup.enter' : 'setup.enterAnyway', {}, 'text', enter.textContent);
  DeskI18n.bind(skip, 'setup.skip', {}, 'text', skip.textContent);
  back.querySelector(".setup-checked-at").textContent = "刚刚检查 · 状态保存在这台电脑";
  const checkedAt = back.querySelector('.setup-checked-at');
  DeskI18n.bind(checkedAt, 'setup.checked', {}, 'text', checkedAt.textContent);
}

async function _setupHandleClick(back, event) {
  const select = event.target.closest("[data-setup-select]");
  if (select) {
    back._setupSelected = select.dataset.setupSelect;
    _setupPaint(back, back._setupState);
    return;
  }
  const button = event.target.closest("[data-setup-action]");
  if (!button) return;
  const action = button.dataset.setupAction;
  if (action === "resume-operation") { await _setupResumeTask(back); return; }
  if (action === "close") { back._setupClose(); return; }
  if (action === "hide-terminal") { back.querySelector(".setup-operation").hidden = true; return; }
  if (action === "finish-terminal") {
    back.querySelector(".setup-operation").hidden = true;
    await _setupRefresh(back, true, button); return;
  }
  if (action === "open-auth") {
    if (back._setupAuthUrl) await _setupOpenExternal(back._setupAuthUrl);
    return;
  }
  if (action === "retry-auth") {
    const retry = back._setupAuthCommand;
    if (retry?.cmd) {
      if (back._setupTermId) {
        try { await api(`/workbench/terminals/${encodeURIComponent(back._setupTermId)}`, { method: "DELETE" }); } catch {}
        back._setupTermId = "";
      }
      await _setupRunInPanel(back, retry.cmd, retry.head);
    }
    return;
  }
  if (action === "refresh") { await _setupRefresh(back, true, button); return; }
  if (action === "command") {
    const cmd = decodeURIComponent(button.dataset.command || "");
    const head = decodeURIComponent(button.dataset.head || DeskI18n.tx("操作记录"));
    if (cmd) await _setupRunInPanel(back, cmd, head);
  }
}

async function _setupRefresh(back, force, button) {
  if (!back.isConnected || back._setupRefreshing) return;
  back._setupRefreshing = true;
  const old = button && button.textContent;
  if (button) { button.disabled = true; button.textContent = DeskI18n.tx("检查中…"); }
  try {
    const options = force ? { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ refresh: true }), timeout: 25000 } : { timeout: 20000 };
    const st = await api("/workbench/setup", options);
    _setupPaint(back, st);
    if (force) back._setupPollCount = 0;
    _setupPollPending(back);
    return st;
  } catch (e) {
    const checked = back.querySelector(".setup-checked-at");
    if (checked) DeskI18n.plain(checked, DeskI18n.tx("检查失败：{error}", { error: e.message }));
  } finally {
    back._setupRefreshing = false;
    if (button && button.isConnected) { button.disabled = false; button.textContent = old; }
  }
}

function _setupPollPending(back) {
  if (back._setupPoll) clearTimeout(back._setupPoll);
  const pending = !!back._setupState?.groups?.intranet?.pending;
  if (!pending || !back.isConnected) return;
  back._setupPollCount = (back._setupPollCount || 0) + 1;
  if (back._setupPollCount > 12) return;
  back._setupPoll = setTimeout(() => _setupRefresh(back, false), 5000);
}

async function _setupRunInPanel(back, cmd, head, options = {}) {
  const wrap = back.querySelector(".setup-operation");
  const host = back.querySelector(".setup-termhost");
  const tt = back.querySelector(".setup-termtt");
  const authButton = back.querySelector(".setup-open-auth");
  const retryButton = back.querySelector(".setup-retry-auth");
  let watchesAuth = /(?:\bauth\s+login\b|\blogin\b|^pi$)/.test(cmd);
  wrap.classList.remove("background-operation");
  // 返回设置后再次点击当前动作，只恢复正在运行的操作，不创建第二个终端。
  if (back._setupTerm && back._setupTermId && !options.restoreTask) { wrap.hidden = false; return true; }
  if (options.restoreTask && back._setupTerm) {
    try { back._setupTerm.dispose(); } catch {}
    back._setupTerm = null; back._setupTermId = "";
  }
  const generation = (back._setupTaskGeneration || 0) + 1;
  back._setupTaskGeneration = generation;
  back._setupAuthUrl = "";
  if (authButton) authButton.hidden = true;
  if (retryButton) retryButton.hidden = true;
  wrap.hidden = false;
  host.hidden = false; host.innerHTML = "";
  tt.textContent = String(head || DeskI18n.tx("正在处理")).split(" · ")[0];
  _setupTaskFeedback(back, "正在准备操作", "正在创建或找回任务，请稍候。无需重复点击。");
  const requestId = "setup_" + (globalThis.crypto?.randomUUID?.() || Date.now() + "_" + Math.random().toString(36).slice(2));
  let r;
  try {
    r = options.restoreTask ? { id: options.restoreTask.terminal_id, task: options.restoreTask }
      : await api("/workbench/shell", { method: "POST", headers: { "content-type": "application/json" },
      // 固定命令作为进程参数执行；PTY 只承接密码、授权等运行时交互。
      // 不模拟输入到登录 shell，避免启动插件的询问吞掉命令。
      body: JSON.stringify({ command: cmd, setup_request_id: requestId }) });
  } catch (e) {
    host.hidden = true;
    _setupTaskFeedback(back, "任务状态暂时无法确认", "请先查看安装进度，确认结果前不要重复执行。", true);
    if (typeof _setupFindTask === "function") await _setupFindTask(back);
    return false;
  }
  if (r?.task) {
    // 服务端可能返回另一个按钮已启动的任务；展示和复查必须跟随原任务。
    const resumed = !!options.restoreTask || r.task.id !== requestId;
    options = { ...options, onClose: resumed ? null : options.onClose };
    if (resumed) {
      // 仅用于登录链接域名校验；恢复不会重新执行这条已登记的命令。
      cmd = r.task.auth_command || "";
      watchesAuth = !!cmd;
    }
    back._setupSavedTask = r.task;
    if (typeof _setupWatchTask === "function") _setupWatchTask(back);
    const resume = back.querySelector('[data-setup-action="resume-operation"]');
    if (resume) resume.hidden = false;
    tt.textContent = r.task.title;
    if (!r.task.attachable) {
      host.replaceChildren();
      const output = document.createElement("pre");
      output.style.cssText = "white-space:pre-wrap;overflow-wrap:anywhere;padding:16px";
      output.textContent = r.task.output || DeskI18n.tx("没有可恢复的终端输出。请重新检查配置。");
      host.appendChild(output);
      const states = { succeeded: "命令已完成", failed: "操作未完成", interrupted: "操作已中断",
        starting: "正在确认任务", waiting_terminal: "正在确认终端", running: "正在确认任务" };
      _setupTaskFeedback(back, states[r.task.state] || "状态待确认",
        r.task.state === "succeeded" ? "请检查配置是否已就绪。" : "原任务不会自动重跑。请检查状态和记录后再决定是否重试。",
        r.task.state !== "succeeded");
      return true;
    }
  }
  if (!r || !r.id) {
    host.hidden = true;
    _setupTaskFeedback(back, "任务状态暂时无法确认", "请先查看安装进度，确认结果前不要重复执行。", true);
    return false;
  }
  back._setupTermId = r.id;
  _setupTaskFeedback(back, r.task?.state === "running" ? "任务进行中" : "正在连接操作终端", watchesAuth
    ? "无需输入命令。请按终端提示完成登录；授权页会自动打开，完成后回到这里。"
    : "终端接通后会继续操作。只有明确要求确认、验证码或密码时才需要输入，完成后会自动复查。");
  if (back._setupTerm) { try { back._setupTerm.dispose(); } catch {} }
  back._setupAuthCommand = watchesAuth ? { cmd, head } : null;
  const termOpts = { onSessionClose: (event) => setTimeout(async () => {
    if (!back.isConnected || generation !== back._setupTaskGeneration) return;
    if (!event?.reason) {
      back._setupTerm = null;
      _setupTaskFeedback(back, "终端连接已断开", "任务可能仍在运行。返回设置并重新检查状态，确认结果前不要重复执行。", true);
      return;
    }
    back._setupTermId = ""; back._setupTerm = null;
    let reason = event.reason;
    if (r.task?.id) {
      try {
        const result = await api("/workbench/setup-tasks");
        if (!back.isConnected || generation !== back._setupTaskGeneration) return;
        const saved = result.tasks?.find(row => row.id === r.task.id);
        if (!["succeeded", "failed", "interrupted"].includes(saved?.state)) throw new Error("结果未落盘");
        back._setupSavedTask = saved;
        reason = saved.state === "succeeded" ? "终端已退出"
          : saved.state === "interrupted" ? "任务中断，退出状态未确认"
            : DeskI18n.tx("任务失败（退出码 {code}）", { code: saved.exit_code ?? DeskI18n.tx("未知") });
      } catch {
        if (!back.isConnected || generation !== back._setupTaskGeneration) return;
        _setupTaskFeedback(back, "退出结果待确认", "请查看安装进度并重新检查配置，暂勿重复执行。", true);
        return;
      }
    }
    if (reason !== "终端已退出") {
      _setupTaskFeedback(back, "操作未完成", DeskI18n.tx("{reason}。请查看终端记录，处理后重新尝试。", { reason: DeskI18n.tx(reason) }), true);
      return;
    }
    _setupTaskFeedback(back, "操作已结束", "正在重新检查配置，请稍候。");
    if (typeof options.onClose === "function") await options.onClose();
    else await _setupRefresh(back, true, null);
    if (back.isConnected && generation === back._setupTaskGeneration) wrap.hidden = true;
  }, 700) };
  if (watchesAuth) termOpts.onOutputLink = (url) => _setupReceiveAuthLink(back, cmd, url);
  try {
    back._setupTerm = await embedTerminal(host, r.id, "shell", termOpts) || null;
  } catch (e) {
    host.hidden = true;
    _setupTaskFeedback(back, "无法显示操作过程", "任务可能已经开始，但过程窗口暂时不可用。返回设置后重新检查状态。", true);
    back._setupTerm = null;
  }
  return !!back._setupTerm;
}

function _setupTaskFeedback(back, state, help, error = false) {
  const line = back.querySelector(".setup-operation-state");
  const text = back.querySelector(".setup-operation-guide p");
  if (line) {
    line.classList.toggle("error", error);
    const label = line.querySelector("b");
    if (label) label.textContent = DeskI18n.tx(state);
  }
  if (text) text.textContent = DeskI18n.tx(help);
}

function _setupTrustedAuthLink(cmd, raw) {
  let u;
  try { u = new URL(raw); } catch { return ""; }
  if (u.protocol !== "https:") return "";
  const h = u.hostname.toLowerCase();
  const suffix = (domain) => h === domain || h.endsWith("." + domain);
  // 只自动打开固定登录命令可能使用的身份域。即便 shell rc 或命令输出里混入其他 URL，
  // 也不会被自动送进浏览器；终端原有的手动链接仍然可用。
  if (cmd.startsWith("lark-cli ")) return suffix("feishu.cn") || suffix("larksuite.com");
  if (cmd === "claude auth login") return suffix("anthropic.com") || suffix("claude.ai");
  if (cmd === "codex login") return suffix("openai.com") || suffix("chatgpt.com");
  return "";
}

async function _setupOpenExternal(url) {
  // 经远端访问时，授权应开在当前浏览器所在的设备，而不是 Desk 主机。
  if (typeof isLocalView === "function" && !isLocalView()) {
    try { return !!window.open(url, "_blank", "noopener"); } catch { return false; }
  }
  try {
    await api("/workbench/open-external", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ url, profile: "system" }) });
    return true;
  } catch { return false; }
}

async function _setupReceiveAuthLink(back, cmd, raw) {
  if (!back.isConnected || back._setupAuthUrl) return;
  const url = _setupTrustedAuthLink(cmd, raw);
  if (!url) return;
  back._setupAuthUrl = url;
  const button = back.querySelector(".setup-open-auth");
  if (button) button.hidden = false;
  const retry = back.querySelector(".setup-retry-auth");
  if (retry) retry.hidden = false;
  const opened = await _setupOpenExternal(url);
  _setupTaskFeedback(back, opened ? "等待完成授权" : "需要打开授权页面", opened
    ? "授权页面已经打开。完成授权后回到这里，终端会继续执行。"
    : "浏览器没有自动打开。请点击“打开授权页面”继续。", !opened);
}
