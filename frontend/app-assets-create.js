// 由 app-assets.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 6/6 片
// ---- 包安装(旧路径:手填 git 地址 / 单包审查) ----
async function installPackage(addr) {
  addr = (addr || prompt("git-url[@ref][#subpath]") || "").trim();
  if (!addr) return;
  let rev;
  try {
    rev = await api("/workbench/packages/plan", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ address: addr }), timeout: 60000,
    });
  } catch (e) { alert("解析失败:" + e.message); return; }
  showPackageReview(addr, rev || {});
}

function pkgModalShell(title, bodyHtml, okLabel) {
  return '<div class="pkg-backdrop"></div><div class="pkg-box">'
    + `<div class="pkg-title">${escapeHtml(title)}</div>`
    + `<div class="pkg-body">${bodyHtml}</div>`
    + '<div class="pkg-actions"><button class="pkg-cancel">取消</button>'
    + (okLabel ? `<button class="pkg-ok">${escapeHtml(okLabel)}</button>` : "")
    + "</div></div>";
}

function showPackageReview(addr, rev) {
  document.querySelector(".pkg-modal")?.remove();
  const m = document.createElement("div"); m.className = "pkg-modal";
  const onEsc = (e) => { if (e.key === "Escape") close(); };
  const close = () => { m.remove(); document.removeEventListener("keydown", onEsc); };
  const join = (a) => (a || []).join("、");
  const row = (k, v) => v ? `<div class="pkg-row"><span class="pkg-k">${escapeHtml(k)}</span><span class="pkg-v">${escapeHtml(v)}</span></div>` : "";

  if (rev.kind === "catalog") {
    const rows = (rev._rows || []).map((r) => {
      const on = r.on ? " checked" : "";
      const dis = r.offline ? " disabled" : "";
      return `<label class="pkg-row pkg-pick${r.offline ? " pkg-offline" : ""}"><input type="checkbox" class="pkg-cb"`
        + ` data-addr="${escapeHtml(r.addr || "")}" data-name="${escapeHtml(r.name || "")}"${on}${dis}>`
        + `<span class="pkg-k">${escapeHtml(r.name || "")}</span>`
        + `<span class="pkg-v">${escapeHtml(r.source || "")}${r.ext ? " · 其他来源" : ""}${r.offline ? " · 待联网" : ""}</span></label>`;
    }).join("");
    const fallbackNote = rev._fallback ? `<div class="pkg-muted" style="margin-bottom:6px">离线模式:联网后自动拉取内容仓,届时可安装</div>` : "";
    const body = fallbackNote
      + `<div class="pkg-sec">包${rev.name ? "（官方目录:" + escapeHtml(rev.name) + "）" : ""}`
      + `<span class="pkg-muted">（勾选=安装,取消=卸载）</span></div>`
      + (rows || '<div class="pkg-muted">(暂无,可填其他 git 地址)</div>')
      + `<div class="pkg-otherrow"><button class="pkg-inst pkg-other">填其他 git 地址</button></div>`;
    m.innerHTML = pkgModalShell("导入", body, "完成");
    document.body.appendChild(m);
    m.querySelectorAll(".pkg-cb").forEach((cb) => cb.onchange = async () => {
      cb.disabled = true;
      try {
        if (cb.checked) {
          const r = await api("/workbench/packages/install", { method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ address: cb.dataset.addr, confirm: true, target: rev._target || undefined }), timeout: 180000 });
          if (r && r.ok === false) { alert("安装失败:" + ((r.errors || []).join("; ") || "")); cb.checked = false; }
        } else {
          await api("/workbench/packages/" + encodeURIComponent(cb.dataset.name), { method: "DELETE" });
        }
      } catch (e) { alert("操作失败:" + e.message); cb.checked = !cb.checked; }
      cb.disabled = false;
      await reloadDomains(); renderSessionPanel();   // 装好的工作区自动归位 / 卸载即移除
    });
    m.querySelector(".pkg-other").onclick = () => { close(); installPackage(); };   // 官方目录外:手填任意 git 地址
    m.querySelector(".pkg-ok").onclick = close;   // 完成:勾选即时生效,此处只关闭
  } else {
    const errs = rev.errors || [];
    const prov = rev.provides || {};
    const req = rev.requires || {};
    const empty = !join(prov.workspaces) && !join(prov.apps) && !join(prov.kb);
    const body =
      row("来源", addr)
      + row("作者", rev.author || "")
      + row("版本", rev.version || "")
      + row("提交", (rev.resolved_commit || "").slice(0, 10))
      + '<div class="pkg-sec">提供</div>'
      + row("工作区", join(prov.workspaces))
      + row("应用", join(prov.apps))
      + row("KB", join(prov.kb))
      + (empty ? '<div class="pkg-muted">(无可注册内容)</div>' : "")
      + (rev.write_apps && rev.write_apps.length ? `<div class="pkg-warn">写类 / agentic 应用：${escapeHtml(join(rev.write_apps))}(可调写工具,装后逐项留意)</div>` : "")
      + (rev.html_sources && rev.html_sources.length ? row("HTML 页", join(rev.html_sources)) : "")
      + ((req.skills && req.skills.length) ? row("依赖 skill", join(req.skills)) : "")
      + (rev.unmet_skills && rev.unmet_skills.length ? `<div class="pkg-warn">本机缺 skill：${escapeHtml(join(rev.unmet_skills))}(装上但相关 app 不可用,标待补)</div>` : "")
      + (rev.app_refs_unresolved && rev.app_refs_unresolved.length ? `<div class="pkg-warn">未解析的 app 引用：${escapeHtml(join(rev.app_refs_unresolved))}(不在本包内,v0 不跨包解析)</div>` : "")
      + (errs.length ? `<div class="pkg-err">不可装：${escapeHtml(errs.join("; "))}</div>` : "");
    const canInstall = rev.ok && rev.kind === "package";
    m.innerHTML = pkgModalShell("安装审查", body, canInstall ? "确认安装" : null);
    document.body.appendChild(m);
    if (canInstall) {
      m.querySelector(".pkg-ok").onclick = async () => {
        const btn = m.querySelector(".pkg-ok"); btn.disabled = true; btn.textContent = "安装中…";
        let r;
        try {
          r = await api("/workbench/packages/install", {
            method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ address: addr, confirm: true }), timeout: 180000,
          });
        } catch (e) { alert("安装失败:" + e.message); btn.disabled = false; btn.textContent = "确认安装"; return; }
        close();
        if (r && r.ok === false) alert("安装失败:" + ((r.errors || []).join("; ") || ""));
        await reloadDomains(); renderSessionPanel();   // 装好的工作区自动归位 desktop + 左栏
      };
    }
  }
  m.querySelector(".pkg-backdrop").onclick = close;
  m.querySelector(".pkg-cancel").onclick = close;
  document.addEventListener("keydown", onEsc);
}

// 工作流创建只有这一条底层路径:侧栏弹框与首启引导都调用它,避免两处 slug/冲突规则漂移。
async function createWorkspaceByName(rawName) {
  const name = String(rawName || "").trim();
  if (!name) throw new Error("请输入工作流名称");
  // id:名称本身是合法 slug 就直接用,否则时间戳槽位;与既有 id 撞则加后缀。
  // POST 对同 id 是合并语义,而这里始终要新建,所以不能直接复用已存在的 id。
  let id = /^[a-z0-9_-]{2,}$/.test(name) ? name : "ws-" + Date.now().toString(36);
  while ((domains || []).some((d) => d.id === id)) id += "-2";
  let r;
  try {
    r = await api("/workbench/domains", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ id, name }) });
  } catch (e) { throw new Error("新建失败:" + e.message); }
  if (!(r && r.ok)) throw new Error("新建失败:" + ((r && r.error) || "未知错误"));
  return { id, name };
}

// 新建工作流:小弹框收名称 → 直接建目录+注册(POST /workbench/domains),与「打开代码仓」对称;
// 说明后续在工作流落地页里编辑,不再经 vibing/make-workspace 引导
function openNewWorkspace(anchor) {
  // 与侧栏动作菜单共用紧凑壳;只创建工作流,不枚举或重新注册磁盘目录。
  anchor = anchor || $("#proj-add");
  const existing = document.querySelector(".resume-pop");
  if (existing) {
    if (existing._close) existing._close(); else existing.remove();
    if (existing._anchor === anchor) return;   // 再点同一个入口 = 收起,不重开
  }
  if (openCtxMenu.close) openCtxMenu.close();
  const pop = document.createElement("div");
  pop.className = "resume-pop rp-ctx sidebar-action-menu nw-pop";
  pop._anchor = anchor;
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "新建工作流");
  pop.innerHTML = '<input class="nw-input" aria-label="工作流名称" placeholder="新工作流名称" spellcheck="false" autocomplete="off">';
  document.body.appendChild(pop);
  const inp = pop.querySelector(".nw-input");
  const close = () => {
    pop.remove();
    document.removeEventListener("pointerdown", onOutside);
    window.removeEventListener("keydown", onKey, true);
    window.removeEventListener("cd-drawer-closed", close);
    window.removeEventListener("resize", place);
    window.visualViewport?.removeEventListener("resize", place);
  };
  pop._close = close;
  openCtxMenu.close = close;   // 打开另一张动作菜单时也清理本层的事件监听
  const onOutside = (e) => { if (!pop.contains(e.target) && !anchor.contains(e.target)) close(); };
  const onKey = (e) => {
    if (e.key !== "Escape") return;
    e.preventDefault(); e.stopPropagation(); close();
    if (anchor.isConnected) anchor.focus({ preventScroll: true });
  };
  setTimeout(() => { if (pop.isConnected) document.addEventListener("pointerdown", onOutside); }, 0);
  window.addEventListener("keydown", onKey, true);
  window.addEventListener("cd-drawer-closed", close);
  const place = () => {
    const rect = anchor.getBoundingClientRect();
    placePop(pop, rect);
  };
  place();
  window.addEventListener("resize", place);
  window.visualViewport?.addEventListener("resize", place);
  let submitting = false;
  const submit = async () => {
    const name = inp.value.trim();
    if (!name || submitting) { if (!submitting) inp.focus(); return; }
    submitting = true; inp.disabled = true;
    let created;
    try { created = await createWorkspaceByName(name); }
    catch (e) {
      submitting = false; inp.disabled = false; alert(e.message);
      if (pop.isConnected) inp.focus();
      return;
    }
    close();
    await reloadDomains();
    selectNav("domain", created.id);   // 落到新工作流的落地页
  };
  inp.onkeydown = (e) => {
    if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); submit(); }
  };
  setTimeout(() => { if (pop.isConnected) inp.focus(); }, 0);
}

// 新建应用:小弹框收一句话用途 → 在本工作区开一段 vibing,首条 prompt 指引 agent 用 make-app 创建
function openNewApp(domainId) {
  document.querySelector(".nw-modal")?.remove();
  const m = document.createElement("div"); m.className = "nw-modal";
  m.innerHTML = '<div class="nw-backdrop"></div><div class="nw-box">'
    + '<div class="nw-title">新建应用</div>'
    + '<input class="nw-input" placeholder="应用要做什么(一句话)" spellcheck="false" autocomplete="off">'
    + '<div class="nw-actions"><button class="nw-cancel">取消</button><button class="nw-ok">确认</button></div>'
    + '</div>';
  document.body.appendChild(m);
  const inp = m.querySelector(".nw-input");
  const close = () => m.remove();
  const submit = async () => {
    const desc = inp.value.trim();
    if (!desc) { inp.focus(); return; }
    close();
    await createAppViaVibing(domainId, desc);
  };
  m.querySelector(".nw-backdrop").onclick = close;
  m.querySelector(".nw-cancel").onclick = close;
  m.querySelector(".nw-ok").onclick = submit;
  inp.onkeydown = (e) => { if (e.key === "Enter") submit(); else if (e.key === "Escape") close(); };
  setTimeout(() => inp.focus(), 0);
}

async function createAppViaVibing(domainId, desc) {
  const dom = domainId || curDomainId();
  const seed =
    `帮我在本工作区做一个应用:${desc}。\n` +
    `请用 make-app skill 完成,别直接手写注册文件:\n` +
    `1) 想清楚是 agentic(调模型,5W 声明 + 参数表单)还是 non-agentic(本地 html 页);\n` +
    `2) 必须明确选择 agent_operability:none/L1/L2/L3/L4。none 写原因;L1-L4 按 Agent-Operable App 协议提供 agent_control;\n` +
    `3) 按 make-app 规范产出(agentic 写 who/what/how/when/where/params;non-agentic 写 .workbench/pages/<id>.html);\n` +
    `4) 跑 bash .claude/skills/make-app/scripts/register_app.sh ${dom} <app.json> 注册到本工作区;\n` +
    `(没加载 skill 的引擎先读 .claude/skills/make-app/SKILL.md。)完成后告诉我结果。`;
  let r;
  try {
    r = await newHtmlSessionWithPrompt({ domain: dom }, seed);
  } catch (e) { alert("创建失败:" + e.message); return; }
  if (r && r.id) { await refreshTerms(); renderSessionPanel(); }
}

function openResumeChoice(s, domId) {
  document.querySelector(".resume-pop")?.remove();
  const canHandoff = canHandoffSource(s.engine);
  const sourceSid = s.resume_id || s.id;
  const pop = document.createElement("div");
  pop.className = "resume-pop resume-choice-pop";
  pop.innerHTML = '<div class="rp-choice-title">接续历史会话</div>'
    + '<div class="rp-choice-sub">' + escapeHtml(s.title || s.id) + '</div>'
    + '<button class="rp-choice-primary" ' + (canHandoff ? '' : 'disabled title="暂不支持读取该引擎的历史"') + '>' + (canHandoff ? '无角色接续（新会话）' : '暂不支持无角色接续') + '</button>'
    + '<button class="rp-choice-secondary">恢复原会话</button>'
    + '<div class="rp-choice-hint">' + (canHandoff ? '读取旧会话并携带摘要创建新的无角色会话；不会 fork 或 resume 原线程。' : '该引擎的历史记录暂不可解析；只能恢复原会话。') + '</div>';
  document.body.appendChild(pop);
  if (canHandoff) pop.querySelector('.rp-choice-primary').onclick = () => { pop.remove(); continueSessionInCurrentEngine(s.engine, sourceSid, domId, s.title || s.id); };
  pop.querySelector('.rp-choice-secondary').onclick = () => { pop.remove(); resumeSession(sourceSid, s.sourceDomain || domId, s.engine); };
  pop.style.left = Math.max(12, Math.round((window.innerWidth - pop.offsetWidth) / 2)) + 'px';
  pop.style.top = Math.max(48, Math.round((window.innerHeight - pop.offsetHeight) / 3)) + 'px';
  const close = (e) => { if (!pop.contains(e.target)) { pop.remove(); document.removeEventListener("mousedown", close); } };
  setTimeout(() => document.addEventListener("mousedown", close), 0);
}

function cancelHandoffProgress(run) {
  if (run && run !== _handoffRun) return;
  _handoffRun++;
  _handoffDomain = null;
  const prev = _handoffPreviousTerm;
  _handoffPreviousTerm = null;
  if (prev && _termPanes[prev]) attachTerminal(prev, undefined, { handoff: true });
  else clearTerminalView();
}

function showHandoffProgress(sourceEngine, targetEngine, title, activeStep, run) {
  if (_handoffDomain && curDomainId() !== _handoffDomain) return;
  showView("terminal");
  document.getElementById("terminal")?.classList.remove("welcome-on");
  const host = $("#term-host"); if (!host) return;
  _hideAllPanes();
  if (!_termWatermark) { _termWatermark = document.createElement("div"); _termWatermark.className = "term-pane term-watermark"; host.appendChild(_termWatermark); }
  const steps = ["读取旧会话", "整理旧会话摘要", "创建无角色会话"];
  _termWatermark.className = "term-pane term-watermark handoff-progress";
  _termWatermark.innerHTML = '<div class="handoff-box" role="status" aria-live="polite"><div class="handoff-title">无角色接续</div>'
    + '<button class="handoff-cancel" title="留在当前会话">' + (ICONS.close || '×') + '</button>'
    + '<div class="handoff-sub">' + escapeHtml(termEngineLabel(sourceEngine)) + '<span class="ui-arrow-icon" aria-hidden="true"></span>' + escapeHtml(termEngineLabel(targetEngine)) + '</div>'
    + '<div class="handoff-name">' + escapeHtml(title || '') + '</div>'
    + '<div class="handoff-steps">' + steps.map((step, i) => '<div class="handoff-step ' + (i < activeStep ? 'done' : i === activeStep ? 'active' : '') + '"><span></span>' + escapeHtml(step) + '</div>').join('') + '</div>'
    + '<div class="handoff-note">创建出新会话后会自动进入；也可以先留在当前会话。</div></div>';
  _termWatermark.querySelector(".handoff-cancel").onclick = () => cancelHandoffProgress(run);
  _termWatermark.style.display = "block";
}

function showHandoffError(sourceEngine, targetEngine, title, message, run) {
  showHandoffProgress(sourceEngine, targetEngine, title, 0, run);
  if (!_termWatermark) return;
  const box = _termWatermark.querySelector(".handoff-box");
  if (box) box.insertAdjacentHTML("beforeend", '<div class="handoff-error">' + escapeHtml(message) + '</div>');
}

async function continueSessionInCurrentEngine(sourceEngine, sid, domainId, title) {
  document.querySelector(".resume-pop")?.remove();
  const run = ++_handoffRun;
  const startActive = activeTermId;
  _handoffDomain = domainId;
  _handoffPreviousTerm = startActive;
  const displayTitle = title || sid;
  showHandoffProgress(sourceEngine, curEngine, displayTitle, 0, run);
  let handoff;
  try {
    handoff = await api("/workbench/session-handoff", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ source_engine: sourceEngine, session_id: sid, target_engine: curEngine }), timeout: 45000 });
  } catch (e) { if (run !== _handoffRun) return; showHandoffError(sourceEngine, curEngine, displayTitle, "交接失败:" + e.message, run); return; }
  if (run !== _handoffRun) return;
  if (!handoff || !handoff.prompt) { showHandoffError(sourceEngine, curEngine, displayTitle, "交接失败:没有可用内容", run); return; }
  showHandoffProgress(sourceEngine, curEngine, displayTitle, 1, run);
  if (run !== _handoffRun) return;
  showHandoffProgress(sourceEngine, curEngine, displayTitle, 2, run);
  let r;
  try {
    // 无角色接续落在新的 GUI 会话上:整理后的 seed prompt 作首句发出去,与别处的
    // 「建会话 + 发首句」同一条路；desk_role 显式空值阻断工作流角色继承。
    r = await newHtmlSessionWithPrompt({ domain: domainId, desk_role: "" }, handoff.prompt);
  } catch (e) { if (run !== _handoffRun) return; showHandoffError(sourceEngine, curEngine, displayTitle, "创建失败:" + e.message, run); return; }
  if (r && r.id) {
    const newTitle = ('无角色接续：' + stripTitleGlyph(displayTitle)).slice(0, 80);
    try { await api('/workbench/terminals/' + encodeURIComponent(r.id) + '/title', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ title: newTitle }) }); } catch {}
    await refreshTerms();
    if (domainId) { try { expandedProjects.add(domainId); persistExpanded(); } catch {} }
    renderSessionPanel();
    if (run !== _handoffRun) return;
    if (activeTermId === startActive && curDomainId() === domainId && !($("#terminal")?.hidden)) await attachTerminal(r.id, curEngine, { handoff: true, domain: domainId });
    _handoffPreviousTerm = null;
    if (run === _handoffRun) _handoffDomain = null;
  }
}

async function resumeSession(sid, domainId, engine) {
  // 恢复一个引擎原生会话:已在跑就直接附着,否则在该工作区 cwd 下 resume 拉活再附着
  document.querySelector(".resume-pop")?.remove();
  try {
    await api(`/workbench/terminals/${encodeURIComponent(sid)}/reopen`, { method: "POST" });
  } catch (e) {
    alertLike(`恢复失败：${e.message || "请稍后重试"}`);
    return;
  }
  // adp_ 开头的是 Desk 自己的会话号,不是引擎侧的锚点。把它当 resume 目标送过去,引擎会起来、
  // 却在自己的记录里找不到这个 id,停在 resume 选择器上报「No sessions match」(2026-08-17
  // 用户实见)。这类会话是建了没跑过首轮的空壳——它在 GUI 里本来就是一段空会话,直接打开即可。
  if (String(sid || "").startsWith("adp_")) {
    if (typeof openHtmlSession === "function") { openHtmlSession(sid, ""); return; }
    alert("这段会话还没跑过回合,没有引擎侧的记录可恢复。");
    return;
  }
  // TUI 退场后不再有「先把进程 resume 起来再附着」这一步:引擎侧的锚点(claude 的 uuid /
  // codex 家族的 thread id)直接认领成工作台会话,记录读得到就打得开,进程按回合起。
  await attachTerminal(sid, engine || curEngine, { domain: domainId, auto: false });
  await refreshTerms();   // 先刷新缓存再重渲,使同步渲染即反映新活会话,不闪
  if (domainId) { try { expandedProjects.add(domainId); persistExpanded(); } catch {} }   // 恢复的会话落在该 domain 的折叠里(coding 项目 / 子区):展开它,否则藏在收起的折叠中看不到
  renderSessionPanel();
}

async function renderProjectSessions(container, project) {
  // 项目展开 = vibing 同款:列本项目打开的会话(desk 终端),× 关闭(可 resume 找回);不开全局轮询(避免与 coding 多项目冲突)
  container.innerHTML = "";
  await renderTermSessions(container, undefined, project.id, true);
}
async function enterTerminalMode(engine) {   // 进终端模式(懒开):有活终端附着最近一个,否则只显示空态,不自动新建
  // 窄屏没有终端这一路(见 app-chat.js 的 termUsable)。这里先亮视图再去拉列表,
  // 中间那段空窗会把上一次残留的 pane 显出来——手机上打开 Desk 时闪一下终端、还写着
  // 「终端已退出」,就是这么来的(2026-08-17 手机实见)。窄屏直接不进这一路。
  if (typeof termUsable === "function" && !termUsable()) {
    // 窄屏没有终端这一路,但空态(「开始新会话」+ 引擎卡)仍要露出来——原来直接 return,
    // #terminal 保持 hidden,手机上打开就是一片空白,连个入口都看不到(2026-08-18 实见)。
    // 顺序是先清后亮:clearTerminalView 会先藏掉所有 pane 再摆水印,这样也不会闪出上次的残留。
    showView("terminal");
    clearTerminalView();
    return;
  }
  showView("terminal");
  let list = [];
  list = await refreshTerms();
  const mine = list.filter((t) => t.engine === engine && t.alive && !t.task && (t.domain || DESKTOP) === curDomainId());
  if (mine.length) attachTerminal(mine[mine.length - 1].id, engine, { domain: curDomainId() });
  else clearTerminalView();   // 启动不自动拉 claude 进程;用户点 + 或点会话再开
}
async function restoreTerminal(engine) {   // 刷新后:优先挂回上次附着的会话(仍活着),否则回落 enterTerminalMode
  // 同 enterTerminalMode:窄屏下不先亮终端视图。要恢复的那段会话仍会按 GUI 打开——
  // attachTerminal 里那道视图偏好的闸会分流过去(hsViewPref 在窄屏恒为 html),
  // 这里只是不让终端容器在判断出结果之前先露一面。
  const mo = typeof termUsable === "function" && !termUsable();
  if (!mo) showView("terminal");
  let list = [];
  list = await refreshTerms();
  let saved = null;
  try { saved = localStorage.getItem("cd_term:" + engine) || localStorage.getItem("cd_term"); } catch {}
  // GUI 偏好下不能按「还活着」去找:那段会话没有 PTY(它的进程是按回合起的,跑完就退),
  // 条件必然落空,于是退回「最近的活终端」——表现就是刷新之后跳到别的会话去了。
  // 休眠会话也在这张表里,认它即可;认领走的是记录,不需要进程还在。
  if (saved && typeof hsViewPref === "function" && hsViewPref() === "html") {
    const any = list.find((t) => t.id === saved && t.engine === engine && !t.task);
    if (any) { attachTerminal(any.id, any.engine, { domain: curDomainId() }); renderSessionPanel(); return; }
  }
  const hit = saved && list.find((t) => t.id === saved && t.engine === engine && t.alive && !t.task);
  if (hit) { attachTerminal(hit.id, hit.engine, { domain: curDomainId() }); renderSessionPanel(); return; }
  // GUI 会话没有 PTY(进程按回合起,跑完就退),alive 必然为假。按它筛会把 GUI 那批全滤掉,
  // 切到一个近期没跑过回合的引擎时一条都匹配不上,直接落到清空——表现就是回到起始页。
  const guiPref = typeof hsViewPref === "function" && hsViewPref() === "html";
  const mine = list.filter((t) => t.engine === engine && (guiPref || t.alive) && !t.task
                                  && (t.domain || DESKTOP) === curDomainId());
  if (mine.length) attachTerminal(mine[mine.length - 1].id, engine, { domain: curDomainId() });
  // 没什么可挂:亮空态(「开始新会话」+ 引擎卡)。窄屏原来是「停在原视图」,可首屏根本没有
  // 原视图可停——#terminal 一直是 hidden,手机上打开 Desk 主区就是一片空白(2026-08-18 实见)。
  // 先清后亮:clearTerminalView 会先藏 pane 再摆水印,不会闪出上次的残留。
  else { showView("terminal"); clearTerminalView(); }
}
async function syncTermView() {   // 切工作区:主区跟随当前工作区——优先恢复上次在该区看的会话,否则挂最近活终端
  let list = [];
  list = await refreshTerms();
  // 先看上次在本区看的会话还在不在(对 coding 尤其关键:它的会话 domain 是 proj-*,域过滤匹配不到)
  const want = _lastTermByNav[curDomainId()];
  const wantSess = want && list.find((t) => t.id === want && t.alive && sessionVisibleInCurrentMode(t));
  if (wantSess) {
    if (wantSess.id !== activeTermId) { attachTerminal(wantSess.id, wantSess.engine, { domain: curDomainId() }); renderSessionPanel(); }
    return;
  }
  // GUI 会话没有 PTY(进程按回合起,跑完就退),alive 必然为假。按它筛会把 GUI 那批全滤掉,
  // 切到一个近期没跑过回合的引擎时一条都匹配不上,直接落到清空——表现就是回到起始页。
  const guiPref = typeof hsViewPref === "function" && hsViewPref() === "html";
  const mine = list.filter((t) => sessionVisibleInCurrentMode(t) && (guiPref || t.alive)
                                  && (t.domain || DESKTOP) === curDomainId());
  if (mine.length) {
    const top = mine[mine.length - 1];
    if (top.id !== activeTermId) { attachTerminal(top.id, top.engine, { domain: curDomainId() }); renderSessionPanel(); }   // 已是当前就不重连
  } else if (activeTermId || _isHandoffProgressVisible()) {
    clearTerminalView(); renderSessionPanel();   // 该区无活终端,而主区还挂着别区的:清掉,避免显示与工作区不符
  }
}

async function openAsset(title, path) {
  navPush({ kind: "asset", title, path });
  activeAssetPath = path;
  document.querySelectorAll(".asset-item").forEach((x) => x.classList.toggle("active", x.dataset.path === path));
  showView("asset");
  // 单文件详情页从技能/知识/文件浏览等多处进入,自己不带任何返回入口,顶部补一个退回上一站的箭头
  const ct = $("#conv-title");
  ct.innerHTML = `<button class="cv-back" title="返回" type="button">${ICONS.back}</button><span class="cv-back-label"></span>`;
  ct.querySelector(".cv-back-label").textContent = title;
  ct.querySelector(".cv-back").onclick = () => navGo(-1);
  const body = $("#asset-body");
  body.innerHTML = '<div class="empty">加载中…</div>';
  try {
    let txt;
    if (path.startsWith("memory:")) {
      txt = await api(`/workbench/memory?name=${encodeURIComponent(path.slice(7))}`);
    } else if (path.startsWith("gskill:")) {
      txt = await api(`/workbench/gskill?name=${encodeURIComponent(path.slice(7))}`);
    } else if (path.startsWith("fs:")) {
      const fsp = path.slice(3); const ci = fsp.indexOf(":");
      txt = await api(`/workbench/fs?root=${encodeURIComponent(fsp.slice(0, ci))}&path=${encodeURIComponent(fsp.slice(ci + 1))}&file=1`);
    } else {
      txt = await api(`/workbench/file?path=${encodeURIComponent(path)}`);
    }
    if (path.endsWith(".md") || path.startsWith("gskill:")) {   // 全局 skill(gskill:<name>)内容是 SKILL.md,也按 markdown 渲染
      // frontmatter 拆出来按 yaml 代码块渲染，不混进正文
      let fm = "";
      if (txt.startsWith("---")) {
        const end = txt.indexOf("\n---", 3);
        if (end > 0) { fm = txt.slice(3, end).trim(); txt = txt.slice(end + 4); }
      }
      body.innerHTML = (fm ? md.render("```yaml\n" + fm + "\n```") : "") + md.render(txt);
    } else {
      body.innerHTML = renderCode(txt, path);   // 按扩展名语法高亮(命中语言且不超限),否则纯文本
    }
    $("#asset-view").scrollTop = 0;
  } catch (e) {
    body.innerHTML = `<div class="empty">读取失败：${escapeHtml(e.message)}</div>`;
  }
}
