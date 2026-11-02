// 由 app-assets.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 4/6 片
function termEngineLabel(engine) {
  return (engineOpts[engine] && engineOpts[engine].label) || engine;
}
function canHandoffSource(engine) {
  // 能作为无角色接续来源 = 后端能把它的记录读成对话(term.py 的 _session_handoff_prompt)。
  return engine === "claude" || engineIsCodexLike(engine);
}
let _resumePickerRun = 0;

// 锚定弹层的落位。原来只写「贴在锚点下方」,锚点靠近窗口下缘时,弹层就直接长到窗外去了
// ——CSS 里的 max-height 是 320px,够不到窗口边缘的那一截内容既滚不到也看不见。
// 这里按锚点上下两侧的余量决定朝哪边展开,并把 max-height 压到那一侧的余量,由弹层自己滚。
function placePop(pop, rect, gap = 4, edge = 8) {
  const below = window.innerHeight - rect.bottom - gap - edge;
  const above = rect.top - gap - edge;
  const want = pop.scrollHeight;
  const up = below < Math.min(want, 200) && above > below;   // 下方摆不开、且上方更宽才翻上去
  const room = Math.max(120, up ? above : below);
  pop.style.maxHeight = Math.min(320, room) + "px";
  const h = pop.offsetHeight;
  pop.style.top = (up ? Math.max(edge, rect.top - gap - h)
                      : Math.min(rect.bottom + gap, window.innerHeight - h - edge)) + "px";
  pop.style.left = Math.max(edge, Math.min(rect.right - pop.offsetWidth,
                                           window.innerWidth - pop.offsetWidth - edge)) + "px";
}

async function openResumePicker(anchor, domainId, sourceDomainIds) {
  // 历史会话:只列当前引擎,可恢复原线程或携带摘要新建无角色线程。
  const run = ++_resumePickerRun;
  const existing = document.querySelector(".resume-pop");
  if (existing) { if (existing._close) existing._close(); else existing.remove(); }
  // 列表要等两个后端请求。手机上若等待期间已经收起抽屉，结果回来后不能再把菜单
  // 孤零零挂到正文上；第二次打开也要让第一次的迟到结果失效。
  let drawerClosed = false;
  const pending = document.createElement("span");
  pending.className = "resume-pop";
  pending.hidden = true;
  pending._anchor = anchor;
  const cancelForDrawer = () => {
    drawerClosed = true;
    pending.remove();
    window.removeEventListener("cd-drawer-closed", cancelForDrawer);
  };
  pending._close = cancelForDrawer;
  document.body.appendChild(pending);
  const openedFromDrawer = document.body.classList.contains("mo-drawer");
  if (openedFromDrawer) window.addEventListener("cd-drawer-closed", cancelForDrawer, { once: true });
  const domId = domainId || curDomainId();
  const extraDomains = Array.isArray(sourceDomainIds) ? sourceDomainIds : (sourceDomainIds ? [sourceDomainIds] : []);
  const scanDomains = [...new Set([domId, ...extraDomains].filter(Boolean))];
  const engines = [curEngine];   // 只列当前引擎的会话(跨引擎交接场景退役,列表混引擎徒增认知)
  let list = [], live = [], scanErrors = [], fallbackTerms = [];
  const chatExcluded = new Set();
  try {
    const all = await Promise.all(scanDomains.flatMap((scanDomain) => engines.map(async (eng) => {
      try {
        const r = await api('/workbench/sessions?domain=' + encodeURIComponent(scanDomain) + '&engine=' + encodeURIComponent(eng), { timeout: 30000 });
        return (r.sessions || []).map((s) => ({ ...s, engine: eng, sourceDomain: scanDomain }));
      } catch (e) { scanErrors.push({ engine: eng, domain: scanDomain, message: e.message || String(e) }); return []; }
    })));
    list = all.flat();
  } catch {}
  try {
    const terms = await api("/workbench/terminals", { timeout: 12000 });
    const liveTerms = (terms || []).filter((t) => t.alive);
    live = liveTerms.flatMap((t) => [t.id, t.resume_id]).filter(Boolean);
    if (domId === DESKTOP) {
      for (const t of (terms || [])) {
        const belongsElsewhere = t.task || (t.refs || []).length || (t.domain || DESKTOP) !== DESKTOP;
        if (!belongsElsewhere || !t.engine) continue;
        for (const id of [t.id, t.resume_id]) {
          if (id) chatExcluded.add(t.engine + ':' + id);
        }
      }
    }
    fallbackTerms = (terms || []).filter((t) => t.engine && engines.includes(t.engine) && !t.task
      && !(t.refs || []).length && scanDomains.includes(t.domain || DESKTOP))
      .map((t) => ({ id: t.id, resume_id: t.resume_id, title: t.title || t.id, time: t.time || 0, engine: t.engine, source: 'terminals', sourceDomain: t.domain || DESKTOP }));
  } catch (e) { scanErrors.push({ engine: "desk", domain: domId, message: e.message || String(e) }); }
  if (chatExcluded.size) {
    list = list.filter((s) => ![s.id, s.resume_id]
      .some((id) => id && chatExcluded.has(s.engine + ':' + id)));
  }
  const seen = new Map();
  list = list.filter((s) => {
    const keys = [s.engine + ':' + s.id, s.resume_id ? s.engine + ':' + s.resume_id : ''];
    if (keys.some((k) => k && seen.has(k))) return false;
    keys.forEach((k) => { if (k) seen.set(k, s); });
    return true;
  });
  for (const s of fallbackTerms) {
    const keys = [s.engine + ':' + s.id, s.resume_id ? s.engine + ':' + s.resume_id : ''];
    const hit = keys.find((k) => k && seen.has(k));
    if (hit) {
      const existing = seen.get(hit);
      if (s.title) existing.title = s.title;
      if (s.resume_id) existing.resume_id = s.resume_id;
      if (s.sourceDomain) existing.sourceDomain = s.sourceDomain;
      existing.source = s.source;
      keys.forEach((k) => { if (k) seen.set(k, existing); });
      continue;
    }
    keys.forEach((k) => { if (k) seen.set(k, s); });
    list.push(s);
  }
  list.sort((a, b) => (b.time || 0) - (a.time || 0));
  if (openedFromDrawer) window.removeEventListener("cd-drawer-closed", cancelForDrawer);
  if (run !== _resumePickerRun || drawerClosed || !anchor.isConnected || !pending.isConnected) return;
  const pop = document.createElement("div");
  pop.className = "resume-pop";
  if (!list.length) {
    pop.innerHTML = '<div class="rp-empty">没有可恢复的会话</div>';
  } else {
    for (const s of list) {
      const isLive = live.includes(s.id) || live.includes(s.resume_id);
      const r = document.createElement("div");
      r.className = "rp-row" + (isLive ? "" : " dormant");
      const fromOtherDomain = s.sourceDomain && s.sourceDomain !== domId;
      const label = fromOtherDomain ? '原代码库' : '';   // 列表已只含当前引擎,引擎名列退役
      r.innerHTML = '<span class="rp-dot' + (isLive ? ' on' : '') + '" title="' + (isLive ? '运行中' : '未运行，点开恢复') + '"></span>'
        + '<span class="rp-title">' + escapeHtml(stripTitleGlyph(s.title) || s.id) + '</span>'
        + '<span class="rp-engine">' + escapeHtml(label) + '</span>';
      r.onclick = () => {
        close();
        // 运行中的当前会话直接打开；历史会话先给「无角色接续」与「恢复原线程」两种
        // 明确语义。前者走 session-handoff 新建线程,不会 fork/resume 旧线程。
        if (isLive && s.engine === curEngine) resumeSession(s.id, domId, s.engine);
        else openResumeChoice(s, domId);
      };
      r.oncontextmenu = (ev) => {   // 右键=删除该会话记录(左键=恢复),弹小菜单
        ev.preventDefault(); ev.stopPropagation();
        openRpCtxMenu(ev.clientX, ev.clientY, s, isLive, r, pop);
      };
      pop.appendChild(r);
    }
  }
  if (scanErrors.length) {
    const warn = document.createElement("div");
    warn.className = "rp-warn";
    warn.textContent = "扫描失败: " + scanErrors.map((e) => termEngineLabel(e.engine)).join("、");
    warn.title = scanErrors.map((e) => e.engine + ": " + e.message).join("\n");
    pop.appendChild(warn);
  }
  pop.style.visibility = "hidden";
  pending.replaceWith(pop);
  const rect = anchor.getBoundingClientRect();
  placePop(pop, rect);
  pop.style.visibility = "";
  const close = (e) => {
    const target = e && e.target;
    // cd-drawer-closed 从 window 派发，target 不是 DOM Node，不能传给 contains()。
    if (target && target.nodeType && (pop.contains(target) || target === anchor)) return;
    pop.remove();
    document.removeEventListener("mousedown", close);
    document.removeEventListener("pointerdown", close);
    window.removeEventListener("cd-drawer-closed", close);
  };
  pop._close = close;
  // 菜单挂在 body 下，不会随 #panel 一起退场；抽屉关闭时显式清掉。触屏浏览器又可能
  // 不发 mousedown，所以与侧栏「+」菜单一样同时监听 pointerdown。
  setTimeout(() => {
    document.addEventListener("mousedown", close);
    document.addEventListener("pointerdown", close);
    window.addEventListener("cd-drawer-closed", close);
  }, 0);
}
// resume 列表行的右键菜单:永久删除该会话记录(claude 删 transcript / codex 删 rollout)。
// 走与会话列表 × 相同的 DELETE 端点,不带 keep → 连记录一并删,不可恢复。删完就地从列表抹去该行,空了显空态。
function openRpCtxMenu(x, y, s, isLive, row, pop) {
  document.querySelector(".rp-ctx")?.remove();
  const m = document.createElement("div");
  m.className = "rp-ctx";
  const del = document.createElement("div");
  del.className = "rp-row rp-ctx-del";
  del.innerHTML = '<span class="rp-title">删除会话</span>';
  del.onclick = async () => {
    m.remove();
    const msg = isLive
      ? `删除会话「${s.title || s.id}」？该会话运行中，删除会结束进程并清除记录，不可恢复。`
      : `删除会话「${s.title || s.id}」？会清除其记录，不可恢复。`;
    if (!confirm(msg)) return;
    try {
      // 后端统一清适配器索引、终端 meta 与引擎记录；失败时不移除行，保留重试入口。
      await api(`/workbench/terminals/${s.id}`, { method: "DELETE" });
    } catch (e) {
      alertLike(`删除失败：${e.message || "请稍后重试"}`);
      return;
    }
    disposeTermPane(s.id);   // 回收该会话 pane(活动则清主区+水印)
    row.remove();
    if (!pop.querySelector(".rp-row")) pop.innerHTML = '<div class="rp-empty">没有可恢复的会话</div>';
    await refreshTerms(); renderSessionPanel();   // 同步会话列表缓存与左栏
  };
  m.appendChild(del);
  document.body.appendChild(m);
  m.style.top = Math.min(y, window.innerHeight - m.offsetHeight - 8) + "px";
  m.style.left = Math.min(x, window.innerWidth - m.offsetWidth - 8) + "px";
  const close = (e) => { if (!m.contains(e.target)) { m.remove(); document.removeEventListener("mousedown", close); document.removeEventListener("contextmenu", close); } };
  setTimeout(() => { document.addEventListener("mousedown", close); document.addEventListener("contextmenu", close); }, 0);
}
// 终端全屏:隐藏 rail+panel,main 占满;再点恢复。refit 让 xterm 适应新尺寸。
(function wireTermFullscreen() {
  const btn = document.getElementById("term-fullscreen");
  if (!btn) return;
  btn.onclick = () => {
    document.body.classList.toggle("term-fs");
    const pane = activeTermId && _termPanes[activeTermId];
    if (pane) setTimeout(() => { _paneFit(pane); if (pane.sendResize) pane.sendResize(); }, 80);
  };
})();
// 收起:退出全屏态并回上一个视图。pane 不销毁、WS 不断,进程照跑(同 #ov-min 之于应用)。
// 右上角按钮与刻度条里点「当前会话」共用它,两处语义一致。
function termMinimize() {
  document.body.classList.remove("term-fs");
  // 连开几段会话就在栈里压了几条 sess,navGo(-1) 只退一格 = 退到上一段会话,要点同样多次才出得来。
  // 跳过栈顶连续的 sess,一次退到最近的非会话站。
  let i = _navHist.idx - 1;
  while (i >= 0 && _navHist.stack[i] && _navHist.stack[i].kind === "sess") i--;
  if (i >= 0) navGo(i - _navHist.idx);
  else _restoreEngineWelcome();   // 栈里全是会话:回欢迎页
}
(function wireTermMin() {
  const btn = document.getElementById("term-min");
  if (btn) btn.onclick = termMinimize;
})();

// 会话刻度条:列当前会话所属容器(工作流/项目/聊天)的会话,不回侧栏就能换一段。
// 观感复用 .wf-toc.ticks——平时是一列细横杠,悬停整条展开成带标题的列表。
function renderTermToc(cur) {
  const toc = document.getElementById("term-toc");
  if (!toc) return;
  // 终端视图的刻度已下线:换会话走侧栏,这一条在正文左侧常驻,收益不抵占位。
  // 构建逻辑先留着——HTML 视图的刻度是另一套(按发言索引),两者别混。
  toc.hidden = true;
  return;
  // 冷启动时会话表还没拉回来,visibleSessionsFor 会拿到空数组 → 判成「不足两条」而隐藏,
  // 要等下一轮保鲜轮询才补上,表现为刷新后刻度要过一会儿才出现。这里拉到再画(同 renderProjSessions)。
  if (_termsList === null) { refreshTerms().then(() => renderTermToc(cur)); return; }
  // 分组口径与侧栏逐字一致:归入任务的按同任务收拢,余下的工作流会话按 visibleSessionsFor,
  // 聊天历史按 looseSessions。自己写一套 filter 会走偏，例如把别的引擎或任务执行记录带进来。
  const own = cur.domain || DESKTOP;
  const dom = own !== DESKTOP ? own : ((cur.refs || [])[0] || DESKTOP);
  let list = [];
  try {
    list = dom === DESKTOP ? looseSessions() : visibleSessionsFor(dom).all;
  } catch {}
  if (!list.some((t) => t.id === cur.id)) list = [cur, ...list];   // 归了任务的当前会话不在那份清单里,补进来
  if (!list.length) { toc.hidden = true; return; }   // 一条也没有才藏;只有当前会话这一条时照常显示,
                                                    // 否则刻度条会随会话数增减忽隐忽现,左缘位置跟着跳
  toc.hidden = false;
  const sig = list.map((t) => `${t.id}|${stripTitleGlyph(t.title)}|${t.busy ? 1 : 0}`).join("\n") + "\n@" + cur.id;
  if (toc._sig === sig) return;   // 轮询每秒都来,内容没变不重建(重建会打断悬停展开)
  toc._sig = sig;
  toc.innerHTML = "";
  // 顶部「返回工作流」:只在展开态出现(CSS 里 .tt-back 默认隐藏)。刻度态是「我在第几段会话」的位置尺,
  // 混进一条不是会话的横杠会让人数错位置。聊天区(DESKTOP)的会话不归属任何工作流,这一行不出现。
  if (dom !== DESKTOP) {
    const back = document.createElement("div");
    back.className = "tt-back";
    // 图标用侧栏工作流行的同一枚十字星:和列表里的引擎字形同为实心紧凑形,细线箭头是另一套语言;
    // 语义上点它是「去所属工作流」,不是「往左」,用目标物的图标比方向箭头准。
    back.innerHTML = `<span class="tt-ic"><svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor" stroke="none"><path d="M12 2c.6 5.5 4.5 9.4 10 10-5.5.6-9.4 4.5-10 10-.6-5.5-4.5-9.4-10-10 5.5-.6 9.4-4.5 10-10z"/></svg></span><span class="tt-nm"></span>`;
    back.querySelector(".tt-nm").textContent = "返回工作流";
    back.onclick = () => { _revealWorkflowInSidebar(dom); _restoreEngineWelcome(); };
    toc.appendChild(back);
  }
  for (const t of list) {
    const it = document.createElement("div");
    const nm = stripTitleGlyph(t.title) || t.id;
    // 展开态对齐侧栏会话行:引擎图标 + 标题;刻度态两者都不显示(CSS 里收成一条横杠)
    it.innerHTML = `<span class="tt-ic">${BRAND_ICONS[t.engine] || ""}</span><span class="tt-nm"></span>`;
    it.querySelector(".tt-nm").textContent = nm;
    it.title = nm;
    if (t.busy) it.classList.add("tt-busy");
    if (t.id === cur.id) it.classList.add("on");
    it.onclick = () => {
      if (t.id === cur.id) { termMinimize(); return; }   // 再点当前会话 = 最小化,同右上角那颗
      attachTerminal(t.id, t.engine, { domain: dom });
      setTermHead(t);
      navPush({ kind: "sess", id: t.id, domain: dom });
      renderTermToc(t);
    };
    toc.appendChild(it);
  }
}

// 手机端附加键栏:软键盘上没有的那几个键,按下直接把转义序列写进当前会话的 PTY。
// 只做单键,不做粘滞 Ctrl —— agent 的 TUI 实际用到的组合键就是中断与结束输入这两个,
// 做成独立按钮比让用户先点 Ctrl 再点字母更省一步,也不必去改 term.onData 的输入链路。
const TERM_KEYS = [
  ["Esc", "\x1b"], ["Tab", "\t"],
  ["↑", "\x1b[A"], ["↓", "\x1b[B"], ["←", "\x1b[D"], ["→", "\x1b[C"],
  ["^C", "\x03"], ["^D", "\x04"],
];
function termSendKey(seq) {
  const pane = _termPanes[activeTermId];
  if (!pane || !pane.ws || pane.ws.readyState !== 1) return;
  try {
    pane.ws.send((pane.enc || new TextEncoder()).encode(seq));
    pane.term.focus();   // 点按钮会把焦点从 xterm 拿走,补回来才能接着打字
  } catch {}
}
(function initTermKeys() {
  const bar = document.getElementById("term-keys");
  if (!bar || bar.childElementCount) return;
  for (const [label, seq] of TERM_KEYS) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.onmousedown = (ev) => ev.preventDefault();   // 别让按钮抢焦点,否则手机上软键盘会收起
    b.onclick = () => termSendKey(seq);
    bar.appendChild(b);
  }
})();

// 手机端长按取词:触摸屏上 xterm 的拖拽选区几乎点不中,长按整行反而准。
// 600ms 不移动即选中手指所在那一行并复制;http 下 navigator.clipboard 不可用(非安全上下文),
// 回落到隐藏 textarea + execCommand。选中态保留一会儿,让人看清选了什么。
function _termCopyText(text) {
  if (!text) return false;
  try {
    if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(text); return true; }
  } catch {}
  try {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch { return false; }
}
function wireTermTouchSelect(pane) {
  const el = pane.host;
  if (!el || el._touchSel) return;
  el._touchSel = true;
  let timer = null, sx = 0, sy = 0;
  const clear = () => { clearTimeout(timer); timer = null; };
  el.addEventListener("touchstart", (ev) => {
    if (!isMobileView() || ev.touches.length !== 1) return;
    const t = ev.touches[0];
    sx = t.clientX; sy = t.clientY;
    clear();
    timer = setTimeout(() => {
      const term = pane.term, rect = el.getBoundingClientRect();
      if (!term || !term.rows) return;
      const row = Math.floor((sy - rect.top) / (rect.height / term.rows));
      if (row < 0 || row >= term.rows) return;
      try {
        term.selectLines(row, row);
        const txt = (term.getSelection() || "").replace(/[ \t]+$/g, "");
        if (_termCopyText(txt)) _toast("已复制这一行");
      } catch {}
    }, 600);
  }, { passive: true });
  // 竖向滑动 = 翻历史。三引擎的 TUI 都开着鼠标上报,xterm 把触摸转成鼠标事件交给应用,
  // 手势被 TUI 吃掉,.xterm-viewport 的原生滚动根本触发不了。这里自己按行距换算成 scrollLines。
  let lastY = 0, scrolling = false;
  el.addEventListener("touchstart", (ev) => {
    if (ev.touches.length === 1) { lastY = ev.touches[0].clientY; scrolling = false; }
  }, { passive: true });
  el.addEventListener("touchmove", (ev) => {
    const t = ev.touches[0];
    if (!t) return;
    if (Math.abs(t.clientX - sx) > 8 || Math.abs(t.clientY - sy) > 8) clear();   // 手指移动=滚动,不是长按
    if (!isMobileView() || ev.touches.length !== 1) return;
    const term = pane.term;
    const cell = term && term._core && term._core._renderService
      && term._core._renderService.dimensions.css.cell.height;
    if (!term || !cell) return;
    const dy = t.clientY - lastY;
    if (!scrolling && Math.abs(dy) < 6) return;   // 小抖动不算滑动,免得点按被判成滚
    scrolling = true;
    const lines = Math.trunc(dy / cell);
    if (!lines) return;
    lastY = t.clientY;
    // 会话跑在备用屏(实测 buffer.type === 'alternate'、baseY 恒为 0),没有回滚缓冲,
    // scrollLines 是空操作;而鼠标上报开着,滚动得以滚轮事件的形式交给 TUI 自己处理。
    // 所以合成一个 wheel 派发给 xterm 的屏幕层,由它翻译成引擎认识的滚轮序列。
    const alt = term.buffer.active.type === "alternate";
    try {
      if (alt) {
        const screen = term.element && term.element.querySelector(".xterm-screen");
        (screen || term.element).dispatchEvent(new WheelEvent("wheel", {
          deltaY: -lines * cell, deltaMode: 0, bubbles: true, cancelable: true,
          clientX: t.clientX, clientY: t.clientY,
        }));
      } else {
        term.scrollLines(-lines);   // 普通缓冲(有回滚)直接滚视口
      }
    } catch {}
    if (ev.cancelable) ev.preventDefault();   // 拦掉原生手势,免得页面跟着一起滚
  }, { passive: false });
  el.addEventListener("touchend", clear, { passive: true });
  el.addEventListener("touchcancel", clear, { passive: true });
}
function _toast(msg) {
  let t = document.getElementById("mo-toast");
  if (!t) { t = document.createElement("div"); t.id = "mo-toast"; document.body.appendChild(t); }
  t.textContent = msg;
  t.classList.add("on");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("on"), 1600);
}

// 手机端软键盘:键盘弹出压缩的是可视视口,布局视口不动,于是输入行被键盘盖住、xterm 的行数也不对。
// 跟着 visualViewport 定高,并给活动 pane 补一次 fit 与尺寸上报;顺带把被 iOS 顶上去的页面拉回原位。
(function mobileViewportFit() {
  const vv = window.visualViewport;
  if (!vv) return;
  let timer = null;
  const apply = () => {
    const root = document.documentElement;
    if (!isMobileView()) { root.style.removeProperty("--vvh"); return; }
    root.style.setProperty("--vvh", Math.round(vv.height) + "px");
    if (window.scrollY) window.scrollTo(0, 0);
    clearTimeout(timer);
    timer = setTimeout(() => {   // 防抖:键盘动画期间会连发十几次 resize
      const pane = _termPanes[activeTermId];
      if (!pane) return;
      _paneFit(pane);
      if (pane.sendResize) pane.sendResize();
    }, 140);
  };
  vv.addEventListener("resize", apply);
  vv.addEventListener("scroll", apply);
  window.addEventListener("orientationchange", () => setTimeout(apply, 300));
  apply();
})();

// 保活模型:每会话一个 pane(独立 xterm + WS),都挂在 #term-host 下,只有活动的 display:block。
// 切会话不销毁、不重放——隐藏的 pane 的 WS 仍在收实时输出写进 buffer,切回来直接显示当前屏,消掉"切换→空白"那类污染。
function _paneFit(pane) {   // 隐藏(0 尺寸)时不 fit,免得把 cols/rows 算成垃圾
  if (!pane || !pane.host || pane.host.offsetWidth === 0 || pane.host.offsetHeight === 0) return;
  try { fitSafe(pane.fit); } catch {}
}
function _termScrollBottom(term) {
  // xterm 的大段 replay/write 与 fit/refresh 不是同一时序;连续补几次,确保最终停在 scrollback 尾部。
  if (!term) return;
  const go = () => { try { term.scrollToBottom(); } catch {} };
  go();
  requestAnimationFrame(go);
  setTimeout(go, 80);
  setTimeout(go, 260);
}
// 主区 pane 的保活、LRU 回收、唤醒态、静默重连、codex 重绘请求,随 TUI 一起撤
// (2026-08-20):这些都挂在「一个会话一个常驻 xterm + 一条 WS」那套模型上,而引擎
// 不再以终端形态跑,没有人再建 pane。shell 终端有自己的一份(app-terminal.js + term-embed.js),
// 与这里无关。

function _hideAllPanes() {
  for (const id in _termPanes) _termPanes[id].host.style.display = "none";
  if (_termWatermark) _termWatermark.style.display = "none";
}
// 欢迎页是否正摆在主区(浮层压在上面时也算:收起浮层回到的就是它)。
function _isWelcomeVisible() {
  return !!(_termWatermark && _termWatermark.style.display !== "none" && !_termWatermark.classList.contains("handoff-progress"));
}
function _isHandoffProgressVisible() {
  return !!(_termWatermark && _termWatermark.style.display !== "none" && _termWatermark.classList.contains("handoff-progress"));
}
function _showWatermark() {
  const host = $("#term-host"); if (!host) return;
  document.getElementById("terminal")?.classList.add("welcome-on");
  // 欢迎页不属于任何一段会话:头部那条标题与按钮要一起收掉。
  // 不收的话,从一段会话打开应用页再关掉、落回欢迎页时,标题还挂着刚才那段会话的名字,
  // 头部的「网页视图」之类也还指着一段已经不在显示的会话。
  if (typeof clearTermHead === "function") clearTermHead();
  _hideAllPanes();
  if (!_termWatermark) { _termWatermark = document.createElement("div"); host.appendChild(_termWatermark); }
  _termWatermark.className = "term-pane term-watermark";
  _termWatermark.innerHTML = vibeWorkingHTML();
  _wireWelcome(_termWatermark);
  _termWatermark.style.display = "block";
}
// 关闭一段会话时把主区的残留收干净。TUI 退场后 _termPanes 恒空(没有人再建 pane),
// 这里实际只剩「当前看的就是它 → 清掉记忆的会话号、摆回空态」那一段;pane 的回收留着,
// 免得哪天又有别的东西往里放。
function disposeTermPane(sessionId) {
  const pane = _termPanes[sessionId];
  if (pane) {
    if (pane.ws) { pane.ws._intentional = true; try { pane.ws.close(); } catch {} }
    if (pane.ro) { try { pane.ro.disconnect(); } catch {} }
    try { pane.term.dispose(); } catch {}
    try { pane.host.remove(); } catch {}
    delete _termPanes[sessionId];
  }
  if (activeTermId === sessionId) {
    activeTermId = null; _term = null; _termWs = null; _termRo = null;
    try { localStorage.removeItem("cd_term"); localStorage.removeItem("cd_term:" + curEngine); } catch {}
    _showWatermark();
  }
}
// 「打开某段会话」的必经之路。TUI 2026-08-20 退场后这里只剩一条去处:GUI 视图。
// 保留这个函数名是因为调用点分散在四个文件、二十来处,改名的收益不抵风险。
// sessionId 有两种形态:adp_* 是工作台自己的会话号(直接开),其余是引擎侧锚点
// (claude 的 uuid / codex 家族的 thread id),要先认领成工作台会话再开——
// 后者正是「把命令行里跑过的那段会话搬进来」的入口。
async function attachTerminal(sessionId, engine, opts) {
  const sid = String(sessionId || "");
  if (!sid) return;
  document.getElementById("terminal")?.classList.remove("welcome-on");
  const eng = engine || curEngine;
  const listedTitle = String((opts && opts.title) || "").trim()
    || (typeof _termListedTitle === "function" ? _termListedTitle(sid) : "");
  activeTermId = sid;
  _lastTermByNav[curDomainId()] = sid;
  try {
    localStorage.setItem("cd_term", sid);
    localStorage.setItem("cd_term:" + eng, sid);
  } catch {}
  if (/^adp_/.test(sid)) {
    openHtmlSession(sid, listedTitle, true, opts || {});
    return;
  }
  // 引擎要跟着一起送:被点的这一段未必属于当前偏好的引擎,后端按它选适配器。
  // 丢掉的话会拿错引擎的读法去找一份不存在的记录,认领失败、页面一片空白。
  termToHtml(sid, { auto: !!(opts && opts.auto !== false), engine: eng, title: listedTitle,
                    domain: (opts && opts.domain) || "", cwd: (opts && opts.cwd) || "" });
}

function cycleTerminal(dir) {
  // 工作流快捷入口与「聊天」完整历史会重复渲染同一段会话；快捷切换按稳定身份去重，
  // 否则 ⌘⇧↑/↓ 会在同一段会话上停两次。
  const seen = new Set();
  const rows = [...document.querySelectorAll("#panel-list .term-item")].filter((r) => {
    const key = r.dataset.sessionId || r.dataset.resumeId || r.dataset.sid || r.title;
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  if (rows.length < 2) return;
  let idx = rows.findIndex((r) => termRowMatchesId(r, activeTermId));
  if (idx < 0) idx = dir > 0 ? -1 : 0;   // 当前不在列表:↓从第一条起,↑从最后一条起
  const nextRow = rows[(idx + dir + rows.length) % rows.length];
  nextRow.click();
  nextRow.scrollIntoView({ block: "nearest" });
}
window.addEventListener("keydown", (e) => {   // capture:先于 xterm 的 textarea 监听
  const pane = activeTermId && _termPanes[activeTermId];
  // ⌘↓ 回到底部:codex 家族的 TUI 在普通缓冲区里出字、有回滚,才需要这一手
  const codexBottom = pane && engineIsCodexLike(pane.engine) && !$("#terminal")?.hidden
    && e.metaKey && !e.shiftKey && !e.altKey && !e.ctrlKey && e.key === "ArrowDown";
  if (codexBottom) {
    e.preventDefault(); e.stopImmediatePropagation();
    _termScrollBottom(pane.term);
    pane.term.focus();
    return;
  }
  if (!e.metaKey || !e.shiftKey || e.altKey || e.ctrlKey) return;
  if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
  if (!document.querySelector("#panel-list .term-item")) return;   // 没有主侧栏会话列表时不接管
  e.preventDefault(); e.stopPropagation();
  cycleTerminal(e.key === "ArrowUp" ? -1 : 1);
}, true);
