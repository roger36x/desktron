// 由 app-chat.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 13/13 片
// ---- 页面搭建 --------------------------------------------------------------

// 回车语义按设备而不是窗口宽度分：窄桌面窗口仍是 PC，Enter 应发送；手机即便接了
// 硬件键盘仍是移动端，Enter 应换行。UAData 优先，Safari 再用 UA 与触屏短边兜底。
function hsPhoneInput() {
  if (navigator.userAgentData && navigator.userAgentData.mobile === true) return true;
  if (/Android|iPhone|iPad|iPod/i.test(navigator.userAgent || "")) return true;
  const coarse = !!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
  const touch = Number(navigator.maxTouchPoints || 0) > 0;
  const shortEdge = Math.min(Number(screen.width || innerWidth), Number(screen.height || innerHeight));
  return coarse && touch && shortEdge <= 820;
}

// Fixed labels of the context bar and composer; rows inside the popovers are translated when painted.
function hsComposerI18n(el) {
  const bind = (selector, key, attr = "text") => DeskI18n.bind(el.querySelector(selector), key, {}, attr);
  bind(".hs-current-tasks-title > span:last-child", "ctx.currentTasks");
  bind(".hs-tasks-add", "ctx.addTask", "placeholder");
  bind(".hs-arrangements-bar .hs-context-label", "nav.arrangements");
  bind(".hs-context-manage > span", "ctx.manage");
  bind(".hs-context-status-bar .hs-context-label", "ctx.status");
  // Defaults until the first sync; the sync functions rebind or replace them.
  bind(".hs-arrangements-meta", "ctx.noArrangements");
  bind(".hs-context-status-meta", "ctx.ready");
  bind(".hs-context-status-title", "ctx.ready");
  bind(".hs-inp", "composer.placeholder", "data-ph");
  bind(".hs-clip", "composer.attach", "data-tip");
  bind(".hs-send", "composer.send", "data-tip");
}

function hsBuild(el, sid, title) {
  const buildOpts = el._hsBuildOpts || null;
  delete el._hsBuildOpts;
  el.classList.add("hs-page");
  el.classList.toggle("hs-sit-disabled", !hsSituationEnabled());
  el.innerHTML =
    // 刻度列挂在页上,不挂在 .hs-wrap 里:wrap 是 flex:1,输入框一长它就被挤矮,
    // 那一列的 top:50% 跟着上移(2026-08-20 用户实见)。它量的是整段会话的位置,
    // 与输入框多高无关,所以定位参照物该是整页。
    '<div class="hs-toc"></div>' +
    '<div class="hs-wrap">' +
      // 查找条浮在正文之上、贴着顶。放在 .hs-wrap 里不放在页上:它要跟着正文列走,
      // 刻度列与输入区不在这一列里。
      '<div class="hs-find" hidden>'
        + '<span class="hs-find-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg></span>'
        + '<input class="hs-find-q" placeholder="在已加载的内容里找" spellcheck="false" aria-label="查找">'
        + '<span class="hs-find-n"></span>'
        + '<button type="button" class="hs-find-prev" aria-label="上一个">' + HS_ICONS.chev + "</button>"
        + '<button type="button" class="hs-find-next" aria-label="下一个">' + HS_ICONS.chev + "</button>"
        + '<button type="button" class="hs-find-x" aria-label="关闭查找">' + HS_ICONS.qclose + "</button>"
      + "</div>" +
      '<div class="hs-history-status" role="status" aria-live="polite" hidden>正在载入会话…</div>' +
      '<div class="hs-body"><div class="hs-stream hs-col"></div></div>' +
    "</div>" +
    '<div class="hs-rv-dv" hidden></div>' +
    '<div class="hs-rv" hidden>'
      + '<div class="hs-rv-h"><button type="button" class="hs-rv-back" aria-label="返回会话">'
        + HS_ICONS.qprev + '<span>返回会话</span></button>'
        + '<span class="hs-rv-t">本轮改动</span><span class="hs-rv-s"></span>'
        // 图标取应用面板里 Monaco IDE 的那一枚(_appIconHTML 按名字查同一张表),不另画:
        // 交接的去处和面板上那个应用是同一个,标记不一致等于让人再认一次。文件查看页那枚
        // 「在 … 中打开」(见 hsOpenFile)走的是同一份图标与同一句文案。
        // 取不到时退回文字,免得读成「在 中打开」。
        + '<button type="button" class="hs-rv-ide" hidden><span>在</span>'
          + ((typeof _appIconHTML === "function"
              ? _appIconHTML("Monaco IDE", "ide", "", "", "") : "") || "<span>IDE</span>")
          + '<span>中打开</span></button>'
        + '<span class="hs-rv-sp"></span>'
        + '<button type="button" class="hs-rv-undo" hidden>撤销本轮</button>'
        + '<button type="button" class="hs-rv-x" aria-label="关闭">×</button></div>'
      + '<div class="hs-rv-files"><input class="hs-rv-filter" placeholder="筛选文件…" spellcheck="false">'
        + '<div class="hs-rv-list"></div></div>'
      + '<div class="hs-rv-body"></div>'
    + "</div>" +
    '<div class="hs-lr-dv" hidden></div>' +
    '<aside class="hs-lr" hidden aria-label="飞书消息引用">'
      + '<div class="hs-lr-h"><button type="button" class="hs-lr-back" aria-label="返回会话">'
        + HS_ICONS.qprev + '<span>返回会话</span></button>'
        + '<div class="hs-lr-heading"><span class="hs-lr-t">飞书消息</span>'
          + '<span class="hs-lr-chat"></span></div>'
        + '<span class="hs-lr-sp"></span>'
        + '<div class="hs-lr-modes" role="tablist" aria-label="飞书消息显示方式">'
          + '<button type="button" class="hs-lr-mode on" data-mode="preview" role="tab" aria-selected="true">预览</button>'
          + '<button type="button" class="hs-lr-mode" data-mode="original" role="tab" aria-selected="false">原页</button></div>'
        + '<button type="button" class="hs-lr-open">外部打开</button>'
        + '<button type="button" class="hs-lr-x" aria-label="关闭">×</button></div>'
      + '<div class="hs-lr-contextbar" hidden><span>模型默认不可见</span>'
        + '<button type="button" class="hs-lr-context-add">加入下一条消息</button></div>'
      + '<div class="hs-lr-body" aria-live="polite"></div>'
      + '<div class="hs-lr-web" hidden><div class="hs-lr-web-fallback">'
        + '<strong>原页需要 Desk 桌面端</strong><span>当前环境无法承载飞书网页，可使用外部打开。</span></div></div>'
    + "</aside>" +
    '<div class="hs-composer">'
    + '<div class="hs-goal hs-col" hidden><span class="hs-goal-k">目标</span><span class="hs-goal-t"></span></div>'
    + '<div class="hs-peer hs-col" hidden role="status" aria-live="polite">'
      + '<span class="hs-peer-dot" aria-hidden="true"></span><span class="hs-peer-label">跨会话问询</span>'
      + '<span class="hs-peer-main"></span><span class="hs-peer-phase"></span></div>'
    + '<div class="hs-context hs-col"><div class="hs-context-rail">'
      + '<div class="hs-tasks" hidden>'
        + '<button type="button" class="hs-context-item hs-tasks-bar" aria-expanded="false">'
          + '<span class="hs-context-icon hs-context-task-icon" aria-hidden="true"></span>'
          + '<span class="hs-context-label hs-task-current-label">任务</span></button>'
        + '<div class="hs-context-popover hs-tasks-popover" hidden>'
          + '<div class="hs-current-task-section" hidden>'
            + '<div class="hs-current-tasks-title"><span class="hs-current-tasks-title-icon" aria-hidden="true"></span><span>当前会话的任务</span></div>'
            + '<div class="hs-current-tasks"></div>'
          + '</div>'
          + '<div class="hs-tasks-sum"><span class="hs-tasks-sum-icon" aria-hidden="true">' + HS_ICONS.check + '</span><span class="hs-tasks-sum-title"></span></div><div class="hs-tasks-list"></div>'
          + '<input class="hs-tasks-add" placeholder="输入回车创建新任务" spellcheck="false"></div></div>'
      + '<div class="hs-arrangements">'
        + '<button type="button" class="hs-context-item hs-arrangements-bar" aria-expanded="false">'
          + '<span class="hs-context-icon hs-context-arrangements-icon" aria-hidden="true"></span>'
          + '<span class="hs-context-label">安排</span><span class="hs-context-meta hs-arrangements-meta">暂无安排</span></button>'
        + '<div class="hs-context-popover hs-arrangements-popover" hidden><div class="hs-arrangements-list"></div>'
          + '<button type="button" class="hs-context-manage"><svg viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M9 4.2v9.6M4.2 9h9.6"/></svg><span>管理安排</span></button></div></div>'
      + '<div class="hs-context-status">'
        + '<button type="button" class="hs-context-item hs-context-status-bar" aria-expanded="false">'
          + '<span class="hs-context-icon hs-context-status-icon" aria-hidden="true"></span>'
          + '<span class="hs-context-label">状态</span><span class="hs-context-meta hs-context-status-meta">引擎已就绪</span></button>'
        + '<div class="hs-context-popover hs-context-status-popover" role="status" hidden><div class="hs-context-status-summary">'
          + '<span class="hs-context-status-popover-icon" aria-hidden="true"><span class="hs-context-status-icon"></span></span>'
          + '<div class="hs-context-status-copy"><strong class="hs-context-status-title">引擎已就绪</strong>'
          + '<div class="hs-context-status-detail"></div></div></div><button type="button" class="hs-context-status-action" hidden>'
          + '<span class="hs-context-status-action-icon" aria-hidden="true"></span><span class="hs-context-status-action-label"></span></button></div></div>'
    + '</div></div>'
    + '<div class="hs-env hs-col" hidden role="status" aria-live="polite" aria-expanded="false">'
      + '<div class="hs-env-bar"><span class="hs-env-k"></span><span class="hs-env-t"></span>'
        + '<button type="button" class="hs-env-chev" aria-label="展开详情">' + HS_ICONS.chev + "</button>"
        + '<button type="button" class="hs-env-a" hidden></button>'
        + '<button type="button" class="hs-env-x" aria-label="关闭">×</button></div>'
      + '<div class="hs-env-d" hidden></div></div>'
    + '<div class="hs-box hs-col">' +
      '<div class="hs-queue" hidden></div>' +
      '<div class="hs-inp" contenteditable="true" data-ph="写点什么…"></div>' +
      '<div class="hs-atts" hidden></div>' +
      '<div class="hs-row">' +
        '<button class="hs-clip" data-tip="附件">' + HS_ICONS.clip + "</button>" +
        '<input class="hs-file" type="file" multiple hidden>' +
        '<span class="hs-mode"><span class="hs-composer-icon is-lock" aria-hidden="true"></span><span class="hs-mode-t">…</span></span>' +
        '<span class="hs-sandbox" hidden><span class="hs-composer-icon is-lock" aria-hidden="true"></span><span class="hs-sandbox-t">…</span></span>' +
        '<span class="hs-agent" hidden><i class="hs-ag-d"></i><span class="hs-agent-t"></span>'
          + HS_ICONS.chev2 + "</span>" +
        '<span class="hs-bash" hidden><i class="hs-bash-d"></i><span class="hs-bash-t"></span></span>' +
        '<span class="hs-sp"></span>' +
        '<span class="hs-model"><span class="hs-m1">…</span><span class="hs-m2"></span></span>' +
        '<button class="hs-send" data-tip="发送"><svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="6"/><polyline points="6 12 12 6 18 12"/></svg></button>' +
      "</div>" +
    "</div>" +
    "</div>" +
    hsSituationMarkup();

  const v = {
    sid, el,
    ephemeral: !!(buildOpts && buildOpts.ephemeral),
    draftDomain: (buildOpts && buildOpts.domain) || DESKTOP,
    // 相对文件链接以会话启动目录为准。侧栏清单通常也有这份值，但页面不能把正确性
    // 建立在异步清单已经返回这一前提上。
    fileCwd: (buildOpts && buildOpts.cwd) || "",
    body: el.querySelector(".hs-body"),
    stream: el.querySelector(".hs-stream"),
    toc: el.querySelector(".hs-toc"),
    inp: el.querySelector(".hs-inp"),
    peerEl: el.querySelector(".hs-peer"), peerAsk: null, peerEpoch: "", peerRevision: 0,
    envEl: el.querySelector(".hs-env"), envTimer: 0, envHideTimer: 0,
    turns: [], busy: false, turnReady: false, turnGeneration: 0, historyReady: false,
    readyGeneration: 0, settledGeneration: 0, roles: new Map(), queue: [],
    stick: true, restickTimer: 0, restickNear: false, bottomIntent: false,
    lastEventAt: 0, tkOn: null,
    statusEl: null, statusState: null, statusLabel: "", statusTail: "", statusT0: 0, statusTick: 0, recTimer: 0,
    rv: null, larkRef: null,
    findHits: [], findAt: 0, findQ: "",
    startAnchor: buildOpts && buildOpts.anchor,
  };
  el._hsView = v;
  hsSetStick(v, true);
  hsObserveLayout(v);
  hsRvWire(v);
  hsLarkRefWire(v);
  hsSituationWire(v);
  hsComposerI18n(el);
  hsFindWire(v);
  _hsViews.set(sid, v);
  hsEnsureSSE();
  if (!v.ephemeral) {
    hsDraftLoad(v);
    v.queueRecoveryReady = hsRecoverServerQueue(v);
  }
  hsTocBind(v);   // 刻度的命中判定挂在整列上,刻度自身每次 hsIndex 都重建,挂不住

  const envToggle = (ev) => {
    if (ev) ev.stopPropagation();
    hsEnvironmentToggle(v);
  };
  const tasksToggle = (ev) => {
    if (ev) ev.stopPropagation();
    const open = !_hsTasksOpen.has(v.sid);
    hsContextClose(v, open ? "tasks" : "");
    if (open) _hsTasksOpen.add(v.sid);
    else _hsTasksOpen.delete(v.sid);
    hsTasksPaint(v, open);
    hsTasksOutside(v, open);
  };
  el.querySelector(".hs-tasks-bar").onclick = tasksToggle;
  el.querySelector(".hs-arrangements-bar").onclick = (ev) => hsArrangementsToggle(v, ev);
  el.querySelector(".hs-context-status-bar").onclick = (ev) => hsContextStatusToggle(v, ev);
  el.querySelector(".hs-context-manage").onclick = (ev) => { ev.stopPropagation(); hsArrangementsManage(v); };
  el.querySelector(".hs-context-status-action").onclick = (ev) => hsContextStatusAction(v, ev);
  // 就地加任务:清单摊开的时候人正对着「还有什么没做」,想到一条就写下来,不必绕去工作流页。
  // 写回的是同一份 domains/<id>/TASKS.md,走共用的 taskAdd(app-core),侧栏与工作流页
  // 由它统一广播刷新——四个入口同一条路,不各写各的。
  const tasksAdd = el.querySelector(".hs-tasks-add");
  tasksAdd.onclick = (ev) => ev.stopPropagation();
  tasksAdd.onkeydown = async (ev) => {
    if (ev.isComposing || ev.keyCode === 229) return;   // 输入法合成态的回车=选字,不是提交
    if (ev.key !== "Enter") return;
    const title = tasksAdd.value.trim();
    if (!title || !v.taskDom) return;
    tasksAdd.disabled = true;
    try { await taskAdd(v.taskDom, title); } catch {}
    tasksAdd.value = "";
    tasksAdd.disabled = false;
    tasksAdd.focus();   // 连着记几条是常态,不把焦点还回去就得再点一次
    hsTasksSync(v);
  };

  // 整条都可点开,不只是那几个字:这一行本身就是一枚披露控件(同任务条点整条 .hs-tasks-bar)。
  v.envEl.querySelector(".hs-env-bar").onclick = envToggle;
  v.envEl.querySelector(".hs-env-x").onclick = (ev) => {
    ev.stopPropagation();
    // 关掉的是「更新」那一条的话记下版本号,本次会话不再提(见 hsUpdateNotice)
    if (v.envEl.classList.contains("update") && v.updateKey) _hsUpdateMuted.add(v.updateKey);
    if (v.envCurrent?.issueID) {
      try { sessionStorage.setItem('desk-error-muted:' + v.sid + ':' + v.envCurrent.issueID, '1'); } catch {}
    }
    hsEnvironmentHide(v, true);
  };

  el.querySelector(".hs-send").onclick = () => hsSend(v);
  v.inp.addEventListener("keydown", (ev) => {
    // 手机 Enter 换行；PC Enter 发送、Shift+Enter 换行。输入法确认候选永远不发送。
    const enterKey = ev.key === "Enter" && !ev.isComposing && ev.keyCode !== 229;
    const phoneInput = hsPhoneInput();
    const newlineKey = enterKey && (phoneInput || ev.shiftKey);
    const sendKey = enterKey && !phoneInput && !ev.shiftKey;
    // 明确要求换行时，命令补全也不能抢走这次 Enter。
    if (v.slashEl && !sendKey && !newlineKey
        && ["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(ev.key)) {
      if (hsSlashKey(v, ev)) { ev.preventDefault(); return; }
    }
    if (ev.key === "Escape" && !v.slashEl) {
      hsEscape(v, ev);
      return;
    }
    if (hsEditKey(v, ev)) { ev.preventDefault(); return; }
    if (sendKey) {
      ev.preventDefault();
      hsSlashHide(v);
      hsSend(v);
    }
  });
  v.inp.addEventListener("input", () => {
    v.histIdx = -1; hsSlashSync(v); hsCmdPaint(v); hsDraftSave(v);
  });
  // Esc 是会话级动作,不应该依赖输入框焦点。查看回复、点过队列或展开处理中之后,
  // 焦点可能已经离开输入框;仍应能停掉当前回合,让排队消息继续。
  v.docKey = (ev) => {
    if (ev.key !== "Escape" || ev.defaultPrevented) return;
    if (!v.el.isConnected) { document.removeEventListener("keydown", v.docKey, true); return; }
    if (!(pActive && pActive.el === v.el) || v.el.style.display === "none") return;
    if (v.inp.contains(ev.target)) return;   // 输入框自己的 keydown 已处理,避免重复发 abort
    // 终端与会话可以并排存在，而且 xterm 的隐藏 textarea 也在同一份 document 里。
    // 这个监听处于捕获阶段，会早于 xterm 收到 Esc；不按来源放行就会一边把 Esc
    // 送给终端，一边中断聊天回合。所有 xterm（应用、登录向导、运行记录）都应自管按键。
    if (ev.target && ev.target.closest && ev.target.closest(".xterm")) return;
    // 查找条里的 Esc 是「关查找」,它自己会 stopPropagation;这里再挡一道,
    // 因为这个监听在捕获阶段,跑在它前面(2026-08-27 加查找时补)。
    if (ev.target && ev.target.closest && ev.target.closest(".hs-find")) return;
    // 弹窗的 Esc 用于关闭弹窗；捕获监听不能抢先中断背后的会话。
    if (ev.target && ev.target.closest && ev.target.closest('[role="dialog"], .auth-tui-back')) return;
    hsEscape(v, ev);
  };
  document.addEventListener("keydown", v.docKey, true);
  // 页面被藏起来或关掉时把欠着的那一次补上:防抖留了 300ms 的窗口,
  // 正好在这段里切走的话,最后敲的几个字会丢。
  const flush = () => hsDraftSave(v, true);
  v.flushDraft = flush;
  document.addEventListener("visibilitychange", flush);
  window.addEventListener("pagehide", flush);
  // 恢复的草稿要把光标放到末尾,不然接着敲会插在最前面。等聚焦那一刻再放,
  // 此时元素已经在文档里,选区才立得住。
  v.inp.addEventListener("focus", () => {
    if (!v.draftFresh) return;
    v.draftFresh = false;
    const sel = getSelection(), r = document.createRange();
    r.selectNodeContents(v.inp); r.collapse(false);
    sel.removeAllRanges(); sel.addRange(r);
  });
  // 富文本粘贴一律降纯文本:contenteditable 默认会把外部样式整块带进来
  v.inp.addEventListener("paste", (ev) => {
    const files = [...((ev.clipboardData && ev.clipboardData.files) || [])];
    if (files.length) { ev.preventDefault(); hsAttAdd(v, files); return; }   // 截图直接粘进来
    ev.preventDefault();
    const t = (ev.clipboardData && ev.clipboardData.getData("text/plain")) || "";
    if (t) document.execCommand("insertText", false, t);
  });
  const fin = el.querySelector(".hs-file");
  el.querySelector(".hs-clip").onclick = () => fin.click();
  fin.onchange = () => { hsAttAdd(v, fin.files); fin.value = ""; };
  const box = el.querySelector(".hs-box");
  box.addEventListener("dragover", (ev) => {
    if ([...((ev.dataTransfer && ev.dataTransfer.items) || [])].some((x) => x.kind === "file")) {
      ev.preventDefault(); ev.dataTransfer.dropEffect = "copy"; box.classList.add("drop");
    }
  });
  box.addEventListener("dragleave", () => box.classList.remove("drop"));
  box.addEventListener("drop", (ev) => {
    const files = [...((ev.dataTransfer && ev.dataTransfer.files) || [])];
    box.classList.remove("drop");
    if (!files.length) return;
    ev.preventDefault(); ev.stopPropagation();
    hsAttAdd(v, files);
  });
  const down = document.createElement("button");
  down.className = "hs-down";
  down.title = "回到最新";
  down.innerHTML = HS_ICONS.down;
  v.down = down;
  down.onclick = async () => {
    if (v.historyNewer > 0) {
      await hsLoad(v);
      hsSetStick(v, true); hsPin(v, 600);
      return;
    }
    hsSetStick(v, true);
    hsScrollCommit(v);
  };
  const downRail = document.createElement("div");
  downRail.className = "hs-down-rail";
  const downTrack = document.createElement("div");
  downTrack.className = "hs-down-track hs-col";
  downTrack.appendChild(down);
  downRail.appendChild(downTrack);
  el.querySelector(".hs-wrap").appendChild(downRail);
  // 操作行悬停显隐不在这里做:.hov 由 app-rail.js 的全局悬停管理器统一管辖(它把 .hov
  // 当独占类,会摘掉不在 _HOV_SEL 里的),`.hs-turn` 已补进那张表。交给它还白捡了它为
  // WKWebView 写的三条清态兜底:指针移出窗口、窗口失焦、文档级 mouseleave。
  // 往更早翻由手势直接发起,不看 scroll 事件。
  //
  // 原来的判据是「人滚过 && scrollTop < 120」,而那个「滚过」是一次性置真、终生有效的
  // 标记。于是这一页只要被人滚过一次,此后任何一次程序性的滚动只要路过顶端都会翻页:
  // 点刻度(或它的预览卡)跳转走的是 scrollIntoView({behavior:"smooth"}),平滑滚动一路
  // 发 scroll 事件,跳到靠前的回合时必然落进 120px 以内;更糟的是翻页会往上插三十个回合,
  // 而那次平滑滚动仍朝着原来那个数值目标走,插完之后目标又落回顶端,于是再翻一次——
  // 一次点击能连着灌进好几段历史。用户看到的就是「什么都没做,刻度自己长出一大截」
  // (2026-08-17 用户实见)。
  //
  // 手势自己带着方向,不必从位置反推:滚轮向上、上翻键、手指往下拖,都是明确的
  // 「我要看更早的」。程序性的滚动不产生这些事件,天然不会触发。
  const pageBack = () => { if (!v.historyLoading && v.body.scrollTop < 120) hsMore(v); };
  v.body.addEventListener("scroll", () => {
    // 取证先于一切分支:浏览器自己夹小 scrollTop 不经过 JS 写入点,只在这里露面。
    // 下面几行本来就要读这几个几何值,这里提前读不多付布局。sinceW/echo 用来分辨
    // 这条事件是自家写入的回声,还是浏览器另起的移动(见 hsVpdCheck 上方说明)。
    globalThis.hsVpdScroll?.(v);
    // 对账重建正在按语义锚点恢复视口。replaceChildren 中间的空态会把 scrollTop 临时
    // 压成 0，这些程序性事件不能反过来把 stick 判成 true，也不能触发向上翻页。
    if (v.restoringScroll) { hsTocSchedule(v); return; }
    // 拖滚动条也是人在翻,但它不发手势事件,只能从 scroll 认:按住不放的期间才算。
    const top = v.body.scrollTop;
    const gap = v.body.scrollHeight - top - v.body.clientHeight;
    const scrollUp = v.lastTop !== undefined && top < v.lastTop;
    const scrollDown = v.lastTop !== undefined && top > v.lastTop;
    const dragUp = v.dragging && scrollUp;
    if (v.dragging && scrollDown) v.bottomIntent = true;
    if (dragUp) { hsUnstick(v); pageBack(); }
    // 位置判据只回贴,不脱钩。WKWebView 在内容首次溢出容器时会发一个位置没动过的
    // scroll 事件(scrollTop 还是 0,gap 恰好越过阈值),按位置反推就把贴底关了,
    // 整段输出从此不再跟随,下一次 pin 才猛拽回底——表现为「不像 tail -f,一顿一跳」
    // (2026-09-01 WebKit 探针实证;Chromium 不发这种事件,所以桌面壳里才看得到)。
    // 脱钩只认明确手势:滚轮向上、按住往上拖,各自的监听器已经在管。
    //
    // 回贴分两档。无手势只认「向下到达」真正的底(4px,含触底回弹):脱钩之后这一页
    // 归人,上滚下滚都不该被抓走,流式期间内容增长发出的 scroll 事件更不能替人做主。
    // 向上离开时哪怕还在 4px 内也不回贴——滚轮上翻的第一批 scroll 事件只挪一两像素,
    // 若按纯位置判,刚脱的钩当场被贴回去,跟随环再把视口拽回底,表现为「上翻偶尔挣不脱」
    // (2026-09-01 探针复现:同一操作七次里失手一次,取决于第一帧挪了几像素)。
    // 带明确向下手势进入 80px 区就立即算「我要追上」——包括手指松开后的惯性段，
    // 不等回弹停稳。流式期间底部是移动靶，只认 4px 的话手动追永远差一口气。
    // 无手势的精确触底仍有一道静默窗:脱钩后 300ms 内不做位置回贴。跟随环写 scrollTop 产生的
    // scroll 事件是异步派发的,用户脱钩的下一两帧还会有「自家钉底」的事件迟到进来——
    // 内容长高后的钉底是向下移动,方向判据挡不住,当场又把钩挂上(探针抓到 14ms 内
    // 脱了又贴)。事件流里程序性滚动与用户滚动无法区分,事件又会合并,计数对不上账,
    // 只有时间窗切得干净;真人上翻之后 300ms 内又滚回底部,物理上做不到。
    else if (v.bottomIntent && scrollDown && gap < 80) {
      hsSetStick(v, true); hsScrollCommit(v);
    } else if (!v.stick && gap < 4 && !scrollUp
               && performance.now() - (v.unstickAt || 0) > 300) hsSetStick(v, true);
    // 空闲时没有逐帧跟随环；若浏览器在重排后改了 scrollTop，但最终高度未变，
    // ResizeObserver 不会再通知。贴底模式下这不是用户上翻，必须在这次 scroll
    // 提交中复位，否则会永久停在较早的正文。真实向上手势已在前面解除 stick。
    if (v.stick && !v.busy && gap > 1) hsScrollCommit(v, true);
    if (!v.stick) {
      hsHoldCapture(v);   // 人挪了视口,自持锚点跟着换到新位置
      // 不在这一帧猜滚动是否真的结束。iOS 到底后的弹性回弹会让最后一帧方向变成向上，
      // 但最终位置仍在底部；等整段滚动停稳后只按实际位置收起“回到最新”。
      hsRestickAfterScroll(v, v.bottomIntent && scrollDown);
    }
    hsTocSchedule(v);
    v.lastTop = v.body.scrollTop;
  }, { passive: true });
  // pointerdown 本身不是滚动意图。正文选字、窗口失焦或高度收缩不能被当作向上拖动。
  v.body.addEventListener("pointerdown", (e) => { v.pointerY = e.clientY; v.dragging = false; }, { passive: true });
  // 鼠标按住移动是划选文字，正文本身不会因此滚动。只记下拖动状态，真正向上滚动
  // (选区自动滚屏)时由 scroll 监听脱钩；否则一次划选就会脱钩并挂回已摘下的旧回合，
  // 正文在选字途中整体重排。触屏与触控笔的拖动才是滚动手势，仍立即脱钩。
  v.body.addEventListener("pointermove", (e) => {
    if (e.buttons && v.pointerY !== undefined && Math.abs(e.clientY - v.pointerY) > 3 && !v.dragging) {
      v.dragging = true;
      if (e.pointerType === "mouse") return;
      globalThis.hsVpdInput?.(v,"drag"); hsUnstick(v);
    }
  }, { passive: true });
  v.body.addEventListener("desk-scroll-start", () => globalThis.hsVpdInput?.(v, "sbar"));   // 取证:先记来源
  v.body.addEventListener("desk-scroll-start", () => hsUnstick(v));
  const dragEnd = () => { v.dragging = false; v.pointerY = undefined; };
  window.addEventListener("pointerup", dragEnd, { passive: true });
  window.addEventListener("pointercancel", dragEnd, { passive: true });
  window.addEventListener("blur", dragEnd);
  // 滚轮向上即脱钩,不等位置阈值:位置判定要挪出 80px 才生效,而流式期间每帧钉一次底,
  // 用户的向上滚动在一帧之内就被砸回去,永远攒不出 80px。意图比位置先到。
  v.body.addEventListener("wheel", (e) => {
    globalThis.hsVpdInput?.(v,e.deltaY < 0 ? "wheel-up" : e.deltaY > 0 ? "wheel-down" : "wheel-x");
    if (e.deltaY < 0) { hsUnstick(v); pageBack(); }
    // 向下滚进 80px 区是明确的「追上最新」:回贴并立即钉一次,把最后几十像素补掉。
    // 只认手势不认位置漂移,所以不会在被动阅读时抓人。
    else if (e.deltaY > 0 && !v.stick) {
      v.bottomIntent = true;
      if (v.body.scrollHeight - v.body.scrollTop - v.body.clientHeight < 80) {
        hsSetStick(v, true); hsScrollCommit(v);
      }
    }
  }, { passive: true });
  v.body.addEventListener("touchstart", () => { v.touchTop = v.body.scrollTop; },
                          { passive: true });
  v.body.addEventListener("touchmove", () => {
    globalThis.hsVpdInput?.(v,"touch");
    const top = v.body.scrollTop;
    if (v.touchTop !== undefined && top < v.touchTop) { hsUnstick(v); pageBack(); }
    else if (v.touchTop !== undefined && top > v.touchTop && !v.stick) {
      v.bottomIntent = true;
      if (v.body.scrollHeight - top - v.body.clientHeight < 80) {
        hsSetStick(v, true); hsScrollCommit(v);
      }
    }
    v.touchTop = v.body.scrollTop;
  }, { passive: true });
  v.body.addEventListener("keydown", (e) => {
    globalThis.hsVpdInput?.(v, "key");   // 取证:任何按键都是明确的人在操作,只记来源不记键名
    if (/^(PageUp|Home|ArrowUp)$/.test(e.key)) { hsUnstick(v); pageBack(); }
    else if (e.key === "End") { hsSetStick(v, true); hsPin(v, 300); }
  });

  // 能力与历史并行取。操作行先按「未知」隐藏，能力回来后 hsCapsPaint 会统一更新已建的
  // 按钮；不必让 Codex 冷启动时可能较慢的 model/list 挡住会话正文。
  // 恢复标签时会一次建出所有页；隐藏页若也立刻取 /config，二十个旧标签会占满连接池，
  // 当前页的 /message 反而排在后面。与历史、目标同样延到真正露面时再取。
  if (!v.ephemeral && hsHidden(v)) v.needConfig = true;
  else v.configReady = hsEngineSync(v);
  el.querySelector(".hs-mode").onclick = (ev) => hsPickMode(v, ev.currentTarget);
  el.querySelector(".hs-sandbox").onclick = (ev) => hsPickSandbox(v, ev.currentTarget);
  el.querySelector(".hs-model").onclick = (ev) => hsPickModel(v, ev.currentTarget);

  // 正文、目标、任务、环境和输入框共用 .hs-col 内容轨。尺寸与内容高度变化统一由
  // hsObserveLayout 处理，不再为窗口、侧栏和每种卡片分别补一次滚动。

  // 子代理名单会扫描 Codex rollout,不能与正文历史争抢主请求路径。先让历史完成并交给
  // 浏览器一帧,再在后台取名单；它本来也要等历史里的本回合起点才能正确切归属。
  const agentsAfterLoad = () => requestAnimationFrame(() => {
    if (v.el.isConnected) hsAgentsSync(v);
  });
  v.agentsAfterLoad = agentsAfterLoad;
  // 历史灌完再取:那时才有本回合的起点,已经交回的那几个要靠它切归属(见 hsAgentsSync)。
  // 但只给「这一刻要看的那一页」灌。刷新时 restorePages 会把留着的会话页一次全建出来,
  // 每一页都灌完整段历史——实测 50 页、23 万个 DOM 节点(2026-08-15 探针),此后任何一次
  // 全局的样式失效或几何读取都要在这 23 万个节点上走一遍,整页对点击没有反应。
  // 盖住的那些先留空壳,轮到它露面时再灌(hsShown)。
  if (v.ephemeral) {
    // 临时空会话只需要输入能力。没有后端 id，不请求历史、目标、任务或跟随状态。
    v.historyReady = true;
    v.turnReady = true;
  } else if (hsHidden(v)) {
    // 盖住的页连目标、任务、环境三条元数据也一并延后,与历史同一口径。原来只延后了历史,
    // 这三条照发:刷新时 restorePages 把留着的会话页一次全建出来,九页就是二十七条请求,
    // 再加对账又一轮——2026-09-05 实测开机 20 秒内 goal 与 situation 各 61 次,合计 40 秒
    // 服务端时间。更要紧的是 WebKit 对同一主机只开六条连接,这一串排在前面,当前页真正
    // 要的 /message 在 init 完成 5 秒后才发得出去。露面时再取(hsShown),晚的只是右卡与
    // 目标条,几十毫秒。
    v.needLoad = true;
    v.needMeta = true;
  } else {
    if (v.startAnchor) hsLoadAnchor(v, v.startAnchor).then(agentsAfterLoad, () => {});
    else hsLoad(v).then(agentsAfterLoad, () => {});
    hsGoalSync(v);
    hsTasksSync(v);
    hsArrangementsSync(v);
  }
  return v;
}

// 同时留在 DOM 里的会话页上限(含当前这一页)。超出的按「最久没看」把正文清空,
// 壳与视图对象留着,再次露面时重灌——会话的权威记录在后端,重灌是一次取数加一次渲染。
// 工具详情按需构建后,一段真实 Codex 会话约 3,700 个节点。桌面留 5 页仍远低于旧实现曾
// 出现的 23 万节点。手机 WebKit 的 WebContent 内存与 tile 预算小得多，隐藏页也会保留整棵
// DOM 和长页面后备存储；手机保留最近三页的 25 轮窗口，超出后由权威记录重灌。
const HS_WARM_MAX = 5;
const HS_PHONE_WARM_MAX = 3;
let _hsWarmClock = 0;

// 手机冷页仍分帧构建，避免 25 轮 Markdown 在一个长任务里堵住点击；但构建中的中间态
// 不直接给用户看。visibility:hidden 保留最终几何，最后一个批次完成后在同一绘制帧里
// 测量、钉底并揭示，避免“空页 → 4 轮 → 8 轮……”反复闪烁和跳动。
function hsHistoryLoading(v, on) {
  if (!v || !v.el) return;
  if (on && !(typeof hsPhoneInput === "function" && hsPhoneInput())) return;
  v.historyLoading = !!on;
  v.el.classList.toggle("hs-history-loading", !!on);
  const status = v.el.querySelector(".hs-history-status");
  if (status) status.hidden = !on;
  if (v.body) {
    if (on) v.body.setAttribute("aria-busy", "true");
    else v.body.removeAttribute("aria-busy");
  }
}

function hsHistoryReveal(v) {
  if (!v || !v.historyLoading || v.pendRest || !v.historyReady) return;
  // 最终批次、折叠测量、刻度与贴底都在同一任务内完成；浏览器只会绘制结束状态。
  // 不依赖 rAF：离屏或刚恢复的 WKWebView 可能冻结动画帧，反而让骨架一直不消失。
  hsMeasureBubbles(v);
  hsIndex(v);
  v.historyRevealing = true;
  try { hsSetStick(v, true); }  // 内含唯一一次最终 hsScrollCommit
  finally { v.historyRevealing = false; }
  hsHistoryLoading(v, false);
}

function hsShown(v) {
  if (!v) return;
  if (typeof hsLarkRefNativeSync === "function") hsLarkRefNativeSync(v, true);
  hsSeen(v);   // 缓存页重新打开也算已读，不依赖重新取历史。
  v.warmAt = ++_hsWarmClock;
  hsSituationSync(v);
  if (v.needConfig) { v.needConfig = false; v.configReady = hsEngineSync(v); }
  // 盖着的时候建出来的气泡没能测折叠(那时量不出高度)。露面即补测,并走布局事务,
  // 免得折叠改变高度之后阅读位置跟着跳。
  hsViewportChange(v, () => {}, false, "fold-shown");
  if (v.needMeta) {   // 建页时因为盖着而没取的目标与任务,此刻补上(见 hsBuild)
    v.needMeta = false;
    hsGoalSync(v);
    hsTasksSync(v);
    hsArrangementsSync(v);
  }
  if (v.needLoad) {
    v.needLoad = false;
    const load = v.startAnchor ? hsLoadAnchor(v, v.startAnchor) : hsLoad(v);
    load.then(v.agentsAfterLoad || (() => hsAgentsSync(v)), () => {});
  }
  if (v.agentLogOwed && typeof hsAgentRefreshOpen === "function") hsAgentRefreshOpen(v);
  // 桌面裁剪只摘 DOM，回合对象仍在 v.turns 里；露面时可本地挂回，否则刻度会指向
  // 游离节点。手机后台裁剪则已释放旧回合，hsRemount 会走 hsMore 从后端再取 30 轮。
  // 重新点开会话不是“向上翻页”的用户意图，不能在这里无提示扩窗；等滚轮、上翻键、
  // 触摸上翻或刻度点击进入 hsUnstick 后，再按正常分页路径补回。
  if (v.domTrimmed && !v.mobileTrimmed && !v.stick) hsRemount(v);
  if (v.pendRest) hsPourRestSoon(v);
  hsEvictColdSoon(v);
}

// 清冷页会拆几千个节点,不应卡在点击切页的同步调用栈里。只保留一份待办,
// 快速连点时以最后露出的页面为准。
let _hsEvictJob = 0, _hsEvictKeep = null;
function hsEvictColdSoon(keep) {
  _hsEvictKeep = keep;
  if (_hsEvictJob) return;
  const run = () => {
    _hsEvictJob = 0;
    const current = _hsEvictKeep;
    _hsEvictKeep = null;
    hsEvictCold(current);
  };
  if (window.requestIdleCallback) _hsEvictJob = requestIdleCallback(run, { timeout: 800 });
  else _hsEvictJob = setTimeout(run, 80);
}

// 冷页只清正文,不销毁视图:输入框里的草稿、队列、引擎档位都挂在视图上,销毁等于丢掉它们。
function hsEvictCold(keep) {
  const warm = [..._hsViews.values()].filter((v) => !v.ephemeral && v.el.isConnected && !v.needLoad);
  const phone = typeof hsPhoneInput === "function" && hsPhoneInput();
  const max = phone ? HS_PHONE_WARM_MAX : HS_WARM_MAX;
  warm.sort((a, b) => (b.warmAt || 0) - (a.warmAt || 0));
  // 三个手机热页只保留“可立即打开”的最近窗口，不保留三份无限展开的长历史。
  // 只裁真正要保留的后台页；排在预算外的页面马上会被完整回收，不多做一轮布局。
  if (phone) for (const v of warm.slice(0, max)) if (v !== keep && hsHidden(v)) hsTrimDom(v);
  if (warm.length <= max) return;
  for (const v of warm.slice(max)) {
    if (v === keep || !hsHidden(v)) continue;
    // 在跑的那一页不整个清:v.turns 一空,正在收的那一轮就没有落点了。但也不能像原来那样
    // 整段跳过——密集并行时每一段都在跑,这道闸于是一段都收不掉(2026-08-24 实测六段全 busy)。
    // 退一档:裁到 HS_DOM_TURNS。裁的是旧回合,正在流的那一轮永远在保留的那一头。
    if (v.busy) { hsTrimDom(v); continue; }
    if (v.followTimer) { clearInterval(v.followTimer); v.followTimer = 0; }
    if (v.statusTick) { clearInterval(v.statusTick); v.statusTick = 0; }
    if (v.agentTick) { clearInterval(v.agentTick); v.agentTick = 0; v.agentLogOwed = !!v.agentOpen; }
    if (v.restTimer) { clearTimeout(v.restTimer); v.restTimer = 0; }
    hsFlush(v);
    v.historyLoadEpoch = (v.historyLoadEpoch || 0) + 1;  // 让回收前发出的历史响应失效
    hsHistoryLoading(v, false);
    v.stream.innerHTML = "";
    v.turns = []; v.domTrimmed = false; v.mobileTrimmed = false;
    v.phoneHistoryOpen = false;
    v.roles = new Map(); v.asideOf = new Map(); v.mobileDroppedMids = new Set();
    v.statusEl = null;
    v.pendRest = null;
    v.paged = false;
    v.moreDone = false;
    v.pulse = "";
    if (v.toc) v.toc.innerHTML = "";
    v.tkOn = null;
    v.needLoad = true;
    v.historyReady = false;
  }
}

// 把视口钉在底部。内容高度不是一次到位的:字体替换、图片解码、markdown 里的表格都会在随后
// 现代端的后续高度变化统一交给 ResizeObserver；旧 WebView 才短暂逐帧兜底。
// 页面藏着时(标签恢复)高度为零,贴底无意义,等它显示出来再钉。
function hsPin(v, ms = 1200) {
  if (!v.stick) return;
  hsScrollCommit(v, true);
  // 没有尺寸观察器时仍使用同一队列，不再创建另一条 pin 循环。
  if (!v.layoutObserver) {
    const s = hsViewport(v);
    s.until = Math.max(s.until, performance.now() + ms);
    hsViewportSchedule(v);
  }
}

// 往上翻页:存储里只留最近那一段(一次性铺几百个回合会把界面卡死),更早的从记录里现取。
// 更早的回合先在一个离屏容器里铺好,再整体插到最前——直接往 v.stream 上灌会追加到末尾。
// 插完把滚动位置按高度差补回去,否则内容在手底下往下窜。
// 向上翻页与首屏的分批铺设共用这一段。
// 文档里挂多少个回合。页面开着不关的话,回合只增不减——后端给的 25 个窗口只管首屏,
// 之后每一轮都往 DOM 里加,没有任何一处裁剪。2026-08-22 用户实见 Desk 涨到 2~3GB
// (同一台机器上还有一个停在 Desk 页面上没退的无头浏览器涨到 18.5GB,那是同一件事的
// 极端版本:进程不退,页面一直收,DOM 一直长)。
//
// 裁的是 DOM 不是数据:回合对象连同它的 parts 都留在 v.turns 里,只是节点从文档里摘出来。
// 刻度那一列照旧按 v.turns 画,所以摘掉的回合在尺子上仍然看得见、点得到。
const HS_DOM_TURNS = 40;
const HS_PHONE_DOM_TURNS = 25;
const HS_PHONE_HIDDEN_DOM_TURNS = 25;

// 贴着底时裁:视线在最新那一轮,摘掉上面的节点看不出来,随后还会重新钉底;
// 人一旦往上走就不裁了——他正在读的就是那些内容。
//
// 但「正在读」的前提是这一页露着。盖住的那一页没有阅读位置要护,照样该裁。
// 2026-08-24 并行负载实测:六段会话同时在跑,人停在其中一段,另外五段各自往上翻过
// (v.stick 已经是 false),于是这道闸对它们全部落空,DOM 只增不减——界面进程的物理足迹
// 涨到 5.79GB,其中 5533MB 是滚动容器的 tile 后备存储(1703 个区域,694 个尺寸同为 4144K)。
// 注意这个量在 ps 的 RSS 里看不见:5.4GB 已被换出,RSS 只显示 113MB。
function hsTrimDom(v) {
  if (v.pouring) return;
  if (!v.stick && !hsHidden(v)) return;
  const live = v.turns.filter((t) => t.el && t.el.parentNode);
  const phone = typeof hsPhoneInput === "function" && hsPhoneInput();
  const hiddenPhone = phone && hsHidden(v);
  // 阅读手势展开的历史保持原有布局，回底及后续流式更新不再反复摘挂正文。
  // 离开页面才回收；25 是默认窗口，不是阅读期间的强制上限。
  if (hiddenPhone) v.phoneHistoryOpen = false;
  else if (phone && v.phoneHistoryOpen) return;
  const max = phone ? (hiddenPhone ? HS_PHONE_HIDDEN_DOM_TURNS : HS_PHONE_DOM_TURNS) : HS_DOM_TURNS;
  // 当前手机页只摘正文，保留回合与刻度；上下翻动由 hsRemount 本地恢复，避免反复取数重建。
  // 后台页仍释放内存，且必须包含前台已摘下的节点，不能只清文档中还挂着的回合。
  const candidates = hiddenPhone ? v.turns : live;
  if (candidates.length <= max) return;
  const old = candidates.slice(0, candidates.length - max);
  if (hiddenPhone) {
    // 隐藏手机页释放旧回合的内存投影；权威记录仍在后端，回到页面后按需分页重建。
    const dropped = new Set(old), findQ = v.findQ || "";
    // Range 与 CSS Highlight 会反向持有文本节点；先清掉旧命中，否则 detached 子树仍不能回收。
    if (v.findHits && typeof hsFindPaint === "function") {
      v.findHits = []; v.findAt = 0; hsFindPaint(v);
    }
    hsViewportChange(v, () => {
      v.mobileDroppedMids ||= new Set();
      for (const t of old) {
        t.el.remove();
        for (const id of [t.uid, t.msgId, ...(t.executionMids || [])]) if (id) v.mobileDroppedMids.add(id);
        for (const part of (t.parts || new Map()).values()) {
          for (const id of [part.mid, part.messageID]) if (id) v.mobileDroppedMids.add(id);
          part.el = part.streamRoot = part.streamMark = null;
          part.streamHeadHtml = part.streamTailHtml = part.mdHtml = null;
        }
        if (t.parts) t.parts.clear();
        t.el = t.holder = t.procEl = t.footEl = null;
      }
      v.turns = v.turns.filter((t) => !dropped.has(t));
      if (v.asideOf) for (const [id, t] of v.asideOf) if (dropped.has(t)) v.asideOf.delete(id);
      v.mobileTrimmed = true;
      v.moreDone = false;   // 已知还有刚释放的历史，向上滚必须允许重新取
      v.paged = false;
    }, false, "trim-mobile");
    hsIndex(v);
    if (findQ && typeof hsFindRun === "function") hsFindRun(v, findQ, false);
    return;
  }
  hsViewportChange(v, () => {
    for (const t of old) t.el.remove();
    v.domTrimmed = true;
  }, false, "trim");
}

// 挂回来。挂在 hsUnstick 上——往上翻、点刻度、点「回到某一轮」走的都是它,一处接住全部
// 上行入口,不必在每条路上各记一次。滚动位置照 hsPrepend 那套补偿:内容加在视口上方,
// scrollTop 要跟着涨同样的高度,否则视线会往下跳。
function hsRemount(v) {
  if (typeof hsPhoneInput === "function" && hsPhoneInput() && !hsHidden(v)) v.phoneHistoryOpen = true;
  if (v.mobileTrimmed) {
    // 手机旧回合已经真正释放，没有 detached DOM 可挂；按正常分页路径从权威记录补回。
    v.moreDone = false; hsMore(v);
    return;
  }
  if (!v.domTrimmed) return;
  v.domTrimmed = false;
  const first = v.turns.find((t) => t.el && t.el.parentNode);
  const ref = first ? first.el : null;
  const frag = document.createDocumentFragment();
  for (const t of v.turns) {
    if (!t.el || t.el.parentNode) continue;
    frag.appendChild(t.el);
  }
  if (!frag.childNodes.length) return;
  hsViewportChange(v, () => v.stream.insertBefore(frag, ref), false, "remount");
}

function hsPrepend(v, msgs, index = true) {
  const prepend = () => {
    globalThis.hsVpdNote?.(v, "src", { why: "prepend", n: (msgs || []).length });
    const real = v.stream, kept = v.turns, stick = v.stick;
    const scratch = document.createElement("div");
    v.stream = scratch; v.turns = []; v.stick = false;   // 临时换成草稿,不是清空视图,domTrimmed 不动
    hsPour(v, msgs);
    const older = v.turns;
    v.stream = real; v.turns = older.concat(kept); v.stick = stick;
    const frag = document.createDocumentFragment();
    while (scratch.firstChild) frag.appendChild(scratch.firstChild);
    real.insertBefore(frag, real.firstChild);
    if (index) hsIndex(v);
    // 审批回执是历史回合的投影,也会占正文高度。必须在计算高度差之前挂回:
    // 原来先补 scrollTop、再挂回回执,手机首屏分批恢复时三张历史卡晚一拍长出来,
    // 漏掉的高度会让视口一次向上回退约三张卡片(2026-09-05 录屏实证)。
    // 把同一批历史及其投影放进一个布局事务,最终只按完整高度补偿一次。
    if (typeof apprRenderView === "function") apprRenderView(v);
    // 首屏旧历史通过 scratch 分批插入，hsPour 期间 v.pouring 会挡住实时裁剪；插完补一次，
    // 手机打开长会话时无需等到下一轮新增才从 25 回合降到预算内。放在同一布局事务里，
    // 审批回执与裁剪后的最终高度只补偿一次。
    hsTrimDom(v);
  };
  // 冷页正文不可见且最终一定贴底，无需每批读取 scrollHeight 再补偿滚动位置。
  if (v.historyLoading) { prepend(); return; }
  return hsViewportChange(v, prepend);
}

async function hsMore(v) {
  if (typeof hsPhoneInput === "function" && hsPhoneInput() && !hsHidden(v)) v.phoneHistoryOpen = true;
  if (v.pendRest) hsPourRest(v, true);   // 用户已主动往上看,先把首屏欠着的全部补齐
  const restoringMobile = !!v.mobileTrimmed;
  if (v.moreBusy || v.moreDone || (v.busy && !restoringMobile)) return;
  v.moreBusy = true;
  let msgs = null;
  try {
    // have 按用户回合计数；当前 running 的用户消息已落在历史里，live assistant 才是额外快照。
    // 因此即使在 busy 恢复手机窗口，也要按 v.turns 全数跳过，不能少算当前回合。
    const have = (v.historyNewer || 0) + v.turns.length;
    msgs = await api(`/session/${encodeURIComponent(v.sid)}/history?have=${have}&limit=30`,
                     { timeout: 30000 });
  } catch { v.moreBusy = false; return; }
  // 请求失败时必须保留 mobileTrimmed，让刻度或上翻手势可以重试；只有后端成功回答后，
  // 才能确认已释放窗口已经补回，或确实没有更早历史。
  if (restoringMobile) v.mobileTrimmed = false;
  if (!msgs || !msgs.length) { v.moreDone = true; v.moreBusy = false; hsTocFade(v); return; }
  hsPrepend(v, msgs);
  v.paged = true;
  v.moreBusy = false;
}

// 跟进:你在终端那侧继续干活时,这一页不会自己动。空闲且页面可见时看一眼记录变没变,
// 变了就以记录为准重建一次。判据只取文件大小与修改时刻——重读整份记录要 200ms,不能拿来轮询。
function hsFollow(v) {
  if (v.followTimer) clearInterval(v.followTimer);
  // 回到前台立刻查一次。你在 macOS 终端里跟同一段会话聊天时,Desk 多半在后台,
  // 轮询整个不跑(document.hidden);切回来还要再等最多十秒才轮到下一拍——
  // 而「聊完切回 GUI 看」正是这条路最常见的用法(2026-08-10)。
  if (!v.visWired) {
    v.visWired = true;
    const onVis = () => {
      if (document.hidden || !v.el.isConnected) return;
      hsSeen(v);
      hsFollowTick(v);
    };
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("focus", onVis);
  }
  v.followTimer = setInterval(() => hsFollowTick(v), 10000);
}

// 查一拍:记录变了就重建。轮询与「回前台」共用这一段,判据只写一处。
// 会话页被浮层盖住(开了应用、网页标签、文件浏览)时,它在 DOM 里但不参与布局。
// 这种时候不该跟进:跟进发现记录变了就整棵重建,而重建一段长会话是几秒的同步活,
// 界面在那几秒里完全不动——点开一个应用恰好会让焦点跳一次,重建就落在点击那一刻,
// 于是「打开任何应用都卡」。等它重新露出来再补。
function hsHidden(v) {
  return !v.el.isConnected || v.el.offsetParent === null;
}

async function hsFollowTick(v, queueOnly = false) {
  if (!v.el.isConnected) { if (v.followTimer) { clearInterval(v.followTimer); v.followTimer = 0; } return; }
  if (v.queueRecoveryReady) await v.queueRecoveryReady;
  if (v.queueRecoveryBlocked) return;
  if (hsHidden(v) && !queueOnly) { v.followLater = true; return; }
  if (!queueOnly) hsSeen(v);   // 重新露面或上次回执失败时补报，已确认的回合不重复请求。
  if (v.followLater) { v.followLater = false; v.pulse = ""; }   // 盖住期间欠下的那次,露出来补上
  // 上一句没收到回执,先把它判明白再谈别的。「可能已经发出去」不是一个能一直停在那儿
  // 的状态:它要么已经在会话里(那就别再动界面),要么根本没到(那就得把话还给人)。
  if (v.pendingSend) {
    const pending = v.pendingSend;
    const { text, atts } = pending;
    if (v.receiptChecking) return;
    const evidence = {};
    v.receiptChecking = true;
    let got;
    try { got = await hsSentOrNot(v, text, evidence); }
    finally { v.receiptChecking = false; }
    if (v.pendingSend !== pending) return;
    if (got === "unknown") {
      // 刷新后仍在核对回执时，沿用过程状态；不覆盖已经开始的引擎工作状态。
      if (pending.requestID && !v.busy) hsStatus(v, "working", "发送中");
      return;
    }
    v.pendingSend = null; v.sending = null;
    if (got === "sent") { v.turnReady = false; hsQPaint(v); hsDraftSave(v, true); hsStateSync(v); return; }
    if (evidence.retryable && pending.autoRetry !== false && !pending.missingAttachments) {
      const attempt = v.promptDispatch;
      if (attempt && attempt.id === pending.requestID) {
        attempt.rejected = true;
        if (!attempt.turn.uid) {
          attempt.turn.el.remove(); v.turns = v.turns.filter((t) => t !== attempt.turn); hsIndex(v);
          v.turnGeneration = attempt.previousGeneration;
        }
        v.promptDispatch = null;
      }
      (v.queue = v.queue || []).unshift({ text, atts });
      v.turnReady = false; hsBusy(v, false);
      hsQRetry(v);
      return;
    }
    hsQIssue(v, "action", "消息未发送，已保留草稿，请检查后重试");
    if (v.shepherdMode) {
      if (v.promptDispatch && v.promptDispatch.id === pending.requestID) {
        hsPromptRejected(v, {requestID:pending.requestID,
          error:{code:"prompt_not_started", message:"请求未进入引擎，已保留草稿"}});
        return;
      }
    }
    hsStatusClear(v);
    hsErrorNotice(v, "上一句没有发出去，已保留草稿", "delivery");
    hsRestoreUnsent(v, text, atts);
    return;
  }
  if (queueOnly) {
    if (v.queuePaused || !(v.queue || []).length) return;
    // 不复用页面旧 ready。先核对回执/执行状态，断线失败时保持原队列。
    const state = await hsStateSync(v);
    if (state && state.state === "ready" && !state.compacting && !(state.compact || {}).running)
      await hsQFlush(v);
    return;
  }
  // 排队消息的出口本来只有回合边界(收场帧那一路,加 hsBusyVerify 的兜底)。那一下一段会话
  // 只发生一次,而队列是会跨越它活下来的:刷新之后它由草稿恢复(hsDraftLoad 读 d.q),
  // 那时回合早已收尾,再没有任何东西会去发它,消息就一直停在清单里
  // (2026-08-17 实见:AI 回完了,排队那条还在原位;后端查过,那段会话确实空闲)。
  // 这里是唯一还在跑的、能确认「此刻不忙」的地方,给它补一个出口。
  if (!v.busy && (v.queue || []).length) {
    if (v.turnReady) hsQFlush(v);
    else await hsStateSync(v);
    return;
  }
  if (document.hidden) return;
  // busy 时不重建,但要核对一次。回合的收尾只经 SSE 发一遍,那两条(session.error /
  // session.idle)一旦落在断线窗口里就永远收不到,页面停在「处理中」——而这里原本
  // 「busy 就直接返回」,等于把唯一能纠正它的通道也关掉,只能靠人刷新
  // (2026-08-10 实见:引擎以「会话正在别处使用」拒了这一回合,后端 17:15:03 已收尾,
  //  界面还在转)。安静得反常时才核对,平时不打扰。
  //
  // 这一步要排在下面那道 paged 之前。paged 拦的是「重建」,而核对只问一句控制面的状态,
  // 不动 DOM;两件事原来写在同一个 return 里,于是往上翻过一次历史之后,这一页就再也
  // 纠正不了自己的「处理中」——手机上尤其容易踩到,那块屏幕本来就要翻
  // (2026-08-25 用户实见:电脑那侧早已跑完,手机上还转着,而侧栏的点已经不闪了
  //  ——那一路走的是终端清单轮询,与这一页各走各的)。
  if (v.busy) { hsBusyVerify(v); return; }
  // 翻过页就不再自动重建:重建以存储为准,而存储只有最近那一段,会把你翻出来的历史抹掉
  if (v.paged) return;
  let st = null;
  try { st = await api(`/session/${encodeURIComponent(v.sid)}/pulse`); } catch { return; }
  if (!st || !st.size) return;
  const sig = st.size + ":" + st.mtime;
  if (!v.pulse) { v.pulse = sig; return; }
  if (v.pulse === sig) return;
  v.pulse = sig;
  hsReconcile(v);
}

// 与后端对一次账:它那边这一回合还在跑吗?只在「显示为在跑、但很久没有任何事件」时才问,
// 免得把一个不轻的请求变成常态轮询。不在跑就地收尾,让界面回到可用状态。
const HS_BUSY_QUIET_MS = 60000;
const HS_INTERRUPT_VERIFY_MS = 250;

function hsScheduleBusyVerify(v, delay) {
  if (!v || !v.busy) return;
  if (v.busyVerifyTimer) clearTimeout(v.busyVerifyTimer);
  v.busyVerifyTimer = setTimeout(() => {
    v.busyVerifyTimer = 0;
    hsBusyVerify(v, { force: true });
  }, delay || HS_INTERRUPT_VERIFY_MS);
}

async function hsBusyVerify(v, opts) {
  const interrupted = v.interrupted || v.interruptDrop;
  const wait = interrupted ? HS_INTERRUPT_VERIFY_MS : HS_BUSY_QUIET_MS;
  const base = interrupted && v.interruptAt ? v.interruptAt : (v.lastEventAt || v.statusT0 || 0);
  const quiet = Date.now() - base;
  if (!opts || !opts.force) {
    if (quiet < wait) return;
    if (v.busyCheckAt && Date.now() - v.busyCheckAt < wait) return;
  }
  v.busyCheckAt = Date.now();
  // 新契约直接问控制面：消息是否带 running 只是历史投影，不能代表 writer 已经释放。
  let state = null;
  try { state = await api(`/session/${encodeURIComponent(v.sid)}/state`); } catch {}
  if (state && state.state) {
    hsApply(v, "session.state", state);
    if (state.state !== "ready" && interrupted) {
      hsScheduleBusyVerify(v, HS_INTERRUPT_VERIFY_MS);
    }
    return;
  }
  // 兼容尚未重启的旧后端；升级完成后正常不会走到这里。
  let msgs = null;
  try { msgs = await api(`/session/${encodeURIComponent(v.sid)}/message`); } catch { return; }
  if (!v.busy || !msgs) return;
  if (msgs.some((m) => (m.info || {}).running)) {
    if (interrupted) hsScheduleBusyVerify(v, HS_INTERRUPT_VERIFY_MS);
    return;   // 确实还在跑,不动它
  }
  hsFlush(v);
  hsFinalize(v);
  if (interrupted) { v.interrupted = false; v.interruptDrop = false; hsStatus(v, "done", "已中断"); }
  else hsStatus(v, "done", "已结束");
  hsBusy(v, false);
  v.turnReady = true;        // 旧后端没有 ready 事件，只能由消息投影兜底解锁
  hsIndex(v);
  // 只有这一支会真的重建。上面把「核对」放到了 paged 之前,那道守卫就得在这里补回来:
  // 重建以存储为准,而存储只有最近那一段,翻出来的历史会被抹掉。
  if (!v.paged) hsReconcile(v);
  if (v.noDrain) v.noDrain = false;
  else hsQFlush(v);
  hsSeen(v);
  v.pulse = "";            // 逼下一拍重建一次,把后端那边的收尾内容接回来
}

// 取历史。这一步失败过去是静默返回的:不重试、不报错、不留痕,那一页就永远空着,
// 直到有别的事件逼它重取(2026-08-10 实见:sync home 触发重载,重载把飞行中的请求掐断,
// 之后会话内容一直是空的,直到外部一次 session.updated 才回来)。
// 一次没成不代表取不到,所以退避重试;真的取不到就把话说出来,并留一个「重试」——
// 一片空白既讲不出发生了什么,也没有出路。
async function hsLoad(v) {
  v.phoneHistoryOpen = false;
  const loadEpoch = v.historyLoadEpoch = (v.historyLoadEpoch || 0) + 1;
  hsHistoryLoading(v, true);
  if (v.historyLoading) v.stick = false;  // 隐藏构建期间不做每批贴底；揭示时只提交最终位置
  v.recToken = (v.recToken || 0) + 1;
  v.startAnchor = null;
  v.historyNewer = 0;
  v.paged = false;
  v.moreDone = false;
  v.historyReady = false;
  // 与 /message 并行:它不依赖消息,只是应用要等正文铺好(见 hsStateSync 的 pre)。
  // 先发出去,失败也不能在这里抛——那会把整段加载带走,而它只是个状态同步。
  const statePre = api(`/session/${encodeURIComponent(v.sid)}/state`).catch(() => null);
  let msgs = null;
  for (let i = 0; i < 3 && v.el.isConnected; i++) {
    try { msgs = await api(`/session/${encodeURIComponent(v.sid)}/message`); break; }
    catch (e) {
      v.loadErr = (e && e.message) || String(e);
      if (i < 2) await new Promise((r) => setTimeout(r, 400 * (i + 1)));
    }
  }
  if (loadEpoch !== v.historyLoadEpoch || !v.el.isConnected) return;
  if (!msgs) { hsLoadFailed(v); return; }
  hsLoadFailedClear(v);
  // 这一页刚(重)灌,方向判据要跟着清零:留着上一份的位置会让第一次 scroll 读成
  // 「正在往上滚」。翻页本身由手势发起,与这里无关(见 hsBuild 里的 pageBack)。
  v.lastTop = undefined;
  v.touchTop = undefined;
  v.dragging = false;
  // 灌历史之前先把这期间抢先落进来的东西清掉。
  //
  // 页面一建好就注册进 _hsViews 开始收 SSE,而历史要等一次接口往返才回来。回合正在流式
  // 输出时,增量帧会先到:那一刻 v.turns 还是空的,hsTurnFor 只好新建一个【没有用户气泡】
  // 的回合把这条消息认领下来;随后历史整段 append 在它下面,于是正在生成的这段回答孤零零
  // 排在最上方、上面没有提问,真正的提问排在最下方、答复区空着(2026-08-18 用 jsdom
  // 加载真源码复现)。sync home 会无条件 reload,所以这条在开发态是常态路径,不是窄窗口。
  //
  // hsRebuild 那侧本来就是先清后灌(2934 行起),这里对齐它。清掉的增量不会丢:msgs 里
  // 带着 live 快照那一份,后续帧照旧按 messageID 落回同一块。
  if (v.turns.length) {
    v.turns = []; v.domTrimmed = false;
    v.roles = new Map();
    v.asideOf = new Map();
    if (v.stream) v.stream.textContent = "";
    hsStatusClear(v);
  }
  hsPourFast(v, msgs);
  hsGoalSeed(v, msgs);
  // 首屏分两批铺(见 hsPourFast)。刻度这一列是位置尺,先按前几个回合画一遍、再跳成整段,
  // 读起来就是「不知从哪儿冒出来一截更早的」。等第二批落地由 hsPrepend 一次画全。
  if (!v.pendRest) hsIndex(v);
  if (!v.historyLoading) { hsSetStick(v, true); hsPin(v); }
  hsSeen(v);
  hsFollow(v);
  await hsStateSync(v, statePre);
  if (loadEpoch !== v.historyLoadEpoch || !v.el.isConnected) return;
  // 首次审批账本尚未完成时先等待同一份单飞请求；否则卡片会在正文揭示后插入并再次改高度。
  if (typeof apprPoll === "function" && typeof _apprLoaded !== "undefined" && !_apprLoaded) await apprPoll();
  if (loadEpoch !== v.historyLoadEpoch || !v.el.isConnected) return;
  v.historyReady = true;
  hsReconcileAfterLoad(v);
  if (typeof apprRenderView === "function") apprRenderView(v);
  hsHistoryReveal(v);
}

// 全文搜索命中只铺目标附近的小窗。更早内容仍沿用向上翻页；窗口之后若还有新回合，
// 原有的「回到最新」钮会重灌末尾窗口，不把中间几百轮一次塞进 DOM。
async function hsLoadAnchor(v, anchor) {
  v.phoneHistoryOpen = false;
  v.recToken = (v.recToken || 0) + 1;
  anchor = anchor || {};
  const token = (v.anchorToken || 0) + 1;
  v.anchorToken = token;
  v.startAnchor = anchor;
  v.historyReady = false;
  const statePre = api(`/session/${encodeURIComponent(v.sid)}/state`).catch(() => null);
  let result = null;
  try {
    const q = `message=${encodeURIComponent(anchor.message_id || "")}`
      + `&turn_from_end=${encodeURIComponent(anchor.turn_from_end || 0)}`
      + `&engine=${encodeURIComponent(anchor.engine || v.engine || "")}&before=6&after=6`;
    result = await api(`/session/${encodeURIComponent(v.sid)}/around?${q}`, { timeout: 30000 });
  } catch (e) {
    v.loadErr = (e && e.message) || String(e); hsLoadFailed(v); return;
  }
  if (token !== v.anchorToken || !v.el.isConnected) return;
  const msgs = (result && result.messages) || [];
  if (!msgs.length) { hsLoad(v); return; }
  hsLoadFailedClear(v);
  if (v.restTimer) { clearTimeout(v.restTimer); v.restTimer = 0; }
  v.pendRest = null;
  v.turns = []; v.domTrimmed = false;
  v.roles = new Map(); v.asideOf = new Map();
  v.stream.textContent = ""; hsStatusClear(v);
  hsSetStick(v, false);
  const viewportEpoch = hsViewport(v).epoch;
  v.historyNewer = Math.max(0, Number(result.newer || 0));
  v.paged = true;
  v.moreDone = false;
  hsPour(v, msgs);
  hsIndex(v); hsSeen(v); hsFollow(v);
  await hsStateSync(v, statePre);
  if (token !== v.anchorToken || !v.el.isConnected) return;
  v.historyReady = true;
  hsReconcileAfterLoad(v);
  if (typeof apprRenderView === "function") apprRenderView(v);
  if (v.down) { v.down.classList.add("on"); v.down.title = "回到最新"; }
  if (viewportEpoch === hsViewport(v).epoch) {
    hsMeasureBubbles(v);
    const mid = String(anchor.message_id || "");
    const turn = v.turns.find((t) => t.uid === mid || t.msgId === mid
      || [...(t.parts || new Map()).values()].some((p) => p.messageID === mid));
    if (!turn || !turn.el) return;
    v.stream.querySelectorAll(".hs-search-target").forEach((el) => el.classList.remove("hs-search-target"));
    turn.el.classList.add("hs-search-target");
    hsViewportReveal(v, turn.el, "center");
    setTimeout(() => turn.el && turn.el.classList.remove("hs-search-target"), 2400);
  }
}

// 取不到历史时的落地:说清楚发生了什么,并留一条出路。
function hsLoadFailed(v) {
  if (!v.el.isConnected) return;
  hsHistoryLoading(v, false);
  hsLoadFailedClear(v);
  const el = document.createElement("div");
  el.className = "hs-loadfail";
  el.innerHTML = '<span class="hs-lf-t"></span><button class="hs-lf-b" type="button">重试</button>';
  el.querySelector(".hs-lf-t").textContent =
    "没能取到这段会话的历史" + (v.loadErr ? "(" + String(v.loadErr).slice(0, 60) + ")" : "");
  el.querySelector(".hs-lf-b").onclick = () => { hsLoadFailedClear(v); hsLoad(v); };
  v.stream.appendChild(el);
  v.loadFailEl = el;
}

function hsLoadFailedClear(v) {
  if (v.loadFailEl) { v.loadFailEl.remove(); v.loadFailEl = null; }
}

// 报一声「我在看这段」,把侧栏的未读清掉。GUI 没有终端那样的常连附着通道,
// 所以是打开时报一次、每回合收尾时再报一次,不长期占着「正在看」的名分。
function hsSeen(v) {
  // 后台会话仍保留 DOM，也会收到回合收尾；仅挂载并不代表用户正在看。
  // 对齐旧终端 attached 的语义：真正露面的会话才可清除完成未读。
  if (document.hidden || hsHidden(v)) return;
  const listed = typeof termById === "function" ? termById(v.sid) : null;
  const generation = v.turnGeneration
    ? Math.max(0, Number(v.turnGeneration) - (v.busy ? 1 : 0))
    : Number(listed?.completed_generation ?? Math.max(0, Number(listed?.completion_generation || 0) - (listed?.busy ? 1 : 0)));
  const identity = listed || { id: v.sid, engine: v.engine || '' };
  if (typeof clearTermCompletion === "function") clearTermCompletion(identity, generation);
  if (v.seenAck >= generation || v.seenPending === generation) return;
  v.seenPending = generation;
  api(`/session/${encodeURIComponent(v.sid)}/seen`, {
    method: "POST", body: JSON.stringify({ generation })
  }).then(result => {
    v.seenAck = Math.max(v.seenAck ?? -1, Number(result?.seen_generation ?? generation));
  }).catch(() => {}).finally(() => {
    if (v.seenPending === generation) v.seenPending = null;
  });
}

// ---- 对外入口 --------------------------------------------------------------

// 视图切换:同一段会话换一种看法。两条路最终都是 `claude --resume <同一个 uuid>`,
// 历史落在同一份 transcript 里,所以切换不搬数据——HTML 这侧重读一遍记录,终端那侧直接
// 拿 uuid 开 PTY。跑动中不切:一段会话被 PTY 与 `claude -p` 同时写会把记录交错写坏。
//
// 会话只有一种视图。TUI 那条路 2026-08-20 整体退场:引擎不再以嵌入式终端的形态跑,
// 所有会话都是 GUI 会话(POST /session,进程由适配器按回合起)。这两个函数留着是因为
// 调用点分散在四个文件里,一处一处改的收益不抵风险;它们现在恒真/恒 html,
// 分流点因此全部落在 GUI 那一侧。
function termUsable() { return true; }
function hsViewPref() { return "html"; }

// 引擎登录/退出:开一个内嵌 shell 跑该引擎自己的命令。与设置页那条同一套(openLoginTUI),
// 不另做一套 UI。命令按引擎分，避免假设各 CLI 的认证子命令相同。
const HS_AUTH_CMD = {
  claude: { login: "claude auth login", logout: "claude auth logout" },
  codex: { login: "codex login", logout: "codex logout" },
  pi: { login: "pi", logout: "pi" },
};
function hsLoginShell(v, kind = "login", source = {}) {
  const eng = source.engine || (v && v.engine) || (typeof curEngine === "string" ? curEngine : "claude");
  const provider = source.provider || (eng === "pi" ? String(v?.sessionOpts?.model || "").split("/")[0] : eng);
  if (!HS_AUTH_CMD[eng]) { alert("未找到该引擎的登录入口"); return; }
  const cmd = HS_AUTH_CMD[eng][kind === "logout" ? "logout" : "login"];
  if (typeof openLoginTUI !== "function") { alert("打不开登录终端"); return; }
  openLoginTUI(cmd, `${eng} ${kind === "logout" ? "退出登录" : "登录"} · ${cmd}`, { engine: eng, provider });
}

async function hsEngineSid(v) {
  // 侧栏清单每行的 id 就是引擎侧锚点,session_id 是我们这侧的 adp_ id;在手头就不再整份拉 /session
  // (12KB、经隧道两个往返,对账那条路每次都要先问它一次)。
  const row = (typeof _termsList !== "undefined" && Array.isArray(_termsList))
    ? _termsList.find((x) => x && x.session_id === v.sid && x.id && x.id !== v.sid) : null;
  if (row) return row.id;
  let list = [];
  try { list = await api("/session"); } catch {}
  const me = (list || []).find((x) => x.id === v.sid);
  if (me && me.engine_sid) return me.engine_sid;
  const msgs = await api(`/session/${encodeURIComponent(v.sid)}/message`).catch(() => []);
  for (let i = (msgs || []).length - 1; i >= 0; i--) {
    const e = (msgs[i].info || {}).engine_sid;
    if (e) return e;
  }
  return "";
}

// 终端那一侧的入口:拿当前终端会话的 id(claude 的会话 id 就是引擎 uuid)去认领,
// 后端把 transcript 翻成契约消息,然后用 HTML 打开同一段会话。
// auto = 由视图偏好自动分流过来的,不是用户点的切换钮。这种情况下认领失败要静默退回终端:
// 刚建的会话还没有记录可读,弹一个「打不开」再把人晾在空页上,比直接给他终端糟。
// 切换序号:adopt 是一次网络往返,快速在两段会话之间点会并发两个请求,谁先回来不保证。
// 后点的先回、先点的后回时,最终渲染的是先点那段——表现为「切不过去」。响应回来先认领号,
// 不是最后一次点击的目标就整个丢弃(包括失败回退,否则会退回已经切走的那段终端)。
let _t2hSeq = 0;
async function termToHtml(sid, opts) {
  if (!sid) return;
  const seq = ++_t2hSeq;
  const stale = () => seq !== _t2hSeq;
  const auto = opts && opts.auto;
  // 侧栏清单已合并自动派生标题与用户改名，优先级高于 adopt 的适配器回退标题。
  const listedTitle = String((opts && opts.title) || "").trim();
  // 被点的那段会话属于哪个引擎,得跟着请求一起送:搬迁在后端按引擎选适配器,
  // 而当前偏好的引擎未必就是它——在 claude 偏好下点一段 codex 会话,不说明就会
  // 拿 claude 的读法去找一份不存在的记录。
  const engine = (opts && opts.engine) || "";
  // 侧栏已提供 Desk 会话别名时直接打开，查看历史无需重新导入原生记录。
  // 原生记录可能缺少启动失败的回合；导入保护应继续阻止覆盖，但不能阻止查看。
  const known = (typeof _termsList !== "undefined" && Array.isArray(_termsList))
    ? _termsList.find(t => (!engine || t.engine === engine)
      && [t.id, t.resume_id, t.session_id].filter(Boolean).map(String).includes(String(sid))
      && /^adp_/.test(String(t.session_id || ""))) : null;
  if (known) {
    return openHtmlSession(known.session_id, listedTitle || known.title || "对话", true,
                           { domain: known.domain || "", cwd: known.cwd || "" });
  }
  // 退回终端也要按这段会话自己的引擎,不能用当前偏好:两者不一致时(codex 偏好下点开
  // 一段 claude 会话),拿 codex 去 resume 一个 claude uuid,终端同样是空的。
  const bail = (m) => {
    if (stale()) return;
    // TUI 退场后没有可退回去的另一种视图了。显式点开的那一路要挡住说清楚;自动分流那一路
    // (刷新后恢复、切工作区跟随)不弹模态——那时人未必在看这一块,一个挡住整屏的框比问题
    // 本身更烦——但也不能全无声音,否则点了没反应。降级成一条飘一下的提示。
    if (!auto) alert("打不开:" + m);
    else if (typeof _toast === "function") _toast("打不开:" + m);
  };
  // 连接级失败要退避重试,不能一次不成就弹窗。经隧道那条路上「Load failed」是常态:
  // 隧道自己重连的那十几秒里,所有在飞的请求都会被断掉,而切会话恰好是人最常按的动作
  // (2026-08-25 用户实见:手机上切会话偶发「打不开:load failed」)。
  // 只重试连接级失败:HTTP 层已经答话的(4xx/5xx,api 会把响应体里的原因带出来)重试没有
  // 意义,那是后端明确拒绝,再问一遍还是同一个答案。
  let r = null, lastErr = "";
  for (const wait of [0, 600, 1800]) {
    if (wait) await new Promise((ok) => setTimeout(ok, wait));
    if (stale()) return;
    try {
      r = await api("/session/adopt", { method: "POST", headers: { "content-type": "application/json" },
                                        body: JSON.stringify({ engine_sid: sid, engine }),
                                        timeout: 30000 });
      lastErr = "";
      break;
    } catch (e) {
      lastErr = (e && e.message) || String(e);
      if (/→\s*\d{3}/.test(lastErr)) break;   // 后端答过话了,是它不肯,不是路不通
    }
  }
  if (lastErr) { bail(lastErr); return; }
  if (stale()) return;                       // 期间又点了别的会话:这次的结果作废
  if (!r || r.error || !r.id) { bail((r && r.error) || "读不到这段会话的记录"); return; }
  openHtmlSession(r.id, listedTitle || r.title || "对话", true,
                  { domain: r.domain || (opts && opts.domain) || "",
                    cwd: r.cwd || (opts && opts.cwd) || "" });
}

async function openHtmlSession(sid, title, show = true, opts) {
  const navigationEpoch = show
    ? ((opts && opts.navigationEpoch) || claimMainNavigation()) : null;
  // app:true = 不进标签条(页头自带收起/关闭)。会话不是标签:终端视图也不占标签,
  // HTML 视图是同一段会话的另一个看法,两者切换应该是原地换视图,而不是又开一页。
  const existing = _hsViews.get(sid);
  const page = openContentPage(HS_PAGE(sid), title || "对话", "◇", (el) => {
    el._hsBuildOpts = opts || null;
    hsBuild(el, sid, title);
  }, false, { app: true });
  if (show) showOverlay(page, navigationEpoch);
  const v = _hsViews.get(sid);
  if (existing && opts && opts.anchor) hsLoadAnchor(v, opts.anchor);
  // 点开一段会话就是要看最新的那几句,所以先把钉底重新打开再钉。
  // hsPin 自己带一道 `if (!v.stick) return`——那是给跑动期间准备的:人往上翻之后不该被拽回底部。
  // 但这一页要是之前被翻上去过,stick 就一直是假,再打开它时那道守卫把这次钉底也一并挡掉,
  // 于是停在上次读到的地方(2026-08-20 用户实见:点开会话默认不在底部)。
  // 只在明确「打开这一页」时重置:刷新恢复走的是 show=false,不经过这里,位置照旧。
  if (show && v) {
    setTimeout(() => v.inp && v.inp.focus(), 60);
    if (!(opts && opts.anchor)) {
      hsSetStick(v, true);
      hsPin(v, 600);
    }
  }
}

// 固定「聊天」入口使用一张不落后端、不进页面恢复记录的临时页。它与工作流的
// 「新建会话」不同：后者已经明确了归属与创建意图，可以立即 POST /session。
const HS_DRAFT_SID = "__chat_draft__";
const HS_DRAFT_PAGE = "chat-draft";

function openDraftChat() {
  let p = pages.find((x) => x.url === HS_DRAFT_PAGE);
  if (!p) {
    const draftTitle = DeskI18n.t("chat.newDraft");
    p = openContentPage(HS_DRAFT_PAGE, draftTitle, "◇", (el) => {
      el._hsBuildOpts = { ephemeral: true, domain: DESKTOP };
      hsBuild(el, HS_DRAFT_SID, draftTitle);
    }, true, { app: true });
  } else showOverlay(p);
  // 它现在就是聊天主区该回去的页面，但还不是一段可选中的真实会话。
  lastChatPage = p;
  activeTermId = "";
  document.querySelectorAll(".term-item").forEach((x) => x.classList.remove("active"));
  const v = _hsViews.get(HS_DRAFT_SID);
  setTimeout(() => { if (v && v.inp) v.inp.focus(); }, 60);
  return p;
}

async function hsMaterializeDraft(v) {
  if (!v || !v.ephemeral) return true;
  if (v.materializing) return false;
  v.materializing = true;
  let s = null;
  try {
    const body = { domain: v.draftDomain || DESKTOP, engine: v.engine || hsNewSessionEngine(
      typeof shepherdMode !== "undefined" && shepherdMode) };
    if (typeof shepherdMode !== "undefined" && shepherdMode) body.shepherd_mode = true;
    if (body.shepherd_mode && typeof shepherdTrial !== "undefined" && shepherdTrial)
      body.shepherd_trial = true;
    // 会话的提示语言随界面语言，建立后固定
    if (typeof DeskI18n !== "undefined") body.prompt_locale = DeskI18n.locale;
    s = await api("/session", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    hsErrorNotice(v, "新建会话失败：" + ((e && e.message) || e), "delivery");
    v.materializing = false;
    return false;
  }
  const sid = s && (s.id || s.sessionID);
  if (!sid) {
    hsErrorNotice(v, "新建会话失败：后端没有返回会话 id", "delivery");
    v.materializing = false;
    return false;
  }

  const oldSid = v.sid;
  _hsViews.delete(oldSid);
  v.sid = sid;
  v.ephemeral = false;
  v.materializing = false;
  _hsViews.set(sid, v);
  const p = pages.find((x) => x.el === v.el);
  if (p) {
    p.url = HS_PAGE(sid);
    p.title = "对话";
    if (pActive === p) {
      activeTermId = sid;
      lastChatPage = p;
      try {
        localStorage.setItem("cd_term", sid);
        localStorage.setItem("cd_term:" + curEngine, sid);
      } catch {}
      setOverlayTitle(p);
    }
  }
  document.querySelectorAll(".term-item").forEach((x) =>
    x.classList.toggle("active", termRowMatchesId(x, sid)));
  persistPages();
  // 第一条消息必须等会话级 Shepherd/引擎配置确认；否则创建后紧接着发送会漏掉 engine。
  v.configReady = hsEngineSync(v);
  await v.configReady;
  refreshTerms({ maxAge: 0 }).then(() => {
    const host = document.querySelector("#panel-list .loose-sessions");
    if (host) renderLooseSessions(host);   // 首条消息发出后立即出现在「聊天」，不等下一轮轮询
  }).catch(() => {});
  return true;
}

function hsDiscardDraft(p) {
  if (!p || p.url !== HS_DRAFT_PAGE) return;
  const v = _hsViews.get(HS_DRAFT_SID);
  if (!v || v.el !== p.el) return;
  _hsViews.delete(HS_DRAFT_SID);
  if (v.docKey) document.removeEventListener("keydown", v.docKey, true);
  if (v.flushDraft) {
    document.removeEventListener("visibilitychange", v.flushDraft);
    window.removeEventListener("pagehide", v.flushDraft);
  }
}

// 「终端视图」那枚钮随 TUI 一起撤了(2026-08-20)。函数留一个空壳:页头切换会逐页调它,
// 而调用点在 app-pages.js,删函数要连着改两个文件的加载顺序假设。恒藏即可。
function hsSyncToTerm(p) {
  const b = document.getElementById("ov-toterm");
  if (b) { b.hidden = true; b.onclick = null; }
}

// 新建一段 HTML 会话:走契约的 POST /session,拿到 id 后开页。
// opts.pref=true:同时把「用哪种视图看会话」的偏好定到 GUI——从终端那枚钮点进来时,
// 用户表达的是「以后就这么看」,不只是这一次。
// 新建一段会话并把首句发出去。工作流页的聊天框、agentic app 的执行记录都走它——
// 这两处原来各自 POST /workbench/terminals 起一个 TUI 会话把 prompt 喂进去,TUI 退场后
// 统一到这里。opts.task=true 的那一档不进侧栏 Vibing 列(记录在任务行里)。
async function newHtmlSessionWithPrompt(opts, text) {
  const s = await newHtmlSession(opts);
  const sid = s && (s.id || s.sessionID);
  if (!sid) return null;
  const t = String(text || "").trim();
  if (!t) return s;
  const post = () => api(`/session/${encodeURIComponent(sid)}/prompt_async`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ parts: [{ type: "text", text: t }] }), timeout: 60000 });
  // 执行记录不开页(它挂在任务行下面),没有视图可等,直接投递；失败必须交给
  // 调用方，不能把只有空会话、没有首条请求的记录当成创建成功。
  if (opts && opts.task) { await post(); return s; }
  // 视图是异步建起来的(openHtmlSession → hsBuild),等它就位再走正常的发送路径:
  // 首句与后续消息因此同一条路,回合、状态、草稿都一致。等不到就直接投递——消息不丢,
  // 只是这一轮的进度要等页面自己刷出来。
  for (let i = 0; i < 40; i++) {
    const v = _hsViews.get(sid);
    if (v && v.inp) {
      if (!await hsDispatch(v, t, [])) throw new Error("首条消息未发送，请重试");
      return s;
    }
    await new Promise((r) => setTimeout(r, 50));
  }
  await post();
  return s;
}

async function newHtmlSession(opts) {
  // 请求发出前认领主区；已经在路上的旧恢复即使稍后结束，也失去展示权。
  // 如果等待期间用户又点了别处，更新的用户动作会取得下一代，仍由它胜出。
  const navigationEpoch = !(opts && opts.task) ? claimMainNavigation() : null;
  let s = null, sessionDomain = "";
  try {
    // 带上当前工作流:新会话据此在侧栏归位,不然它只存在于这个视图自己的菜单里
    const body = { domain: (opts && opts.domain)
      || (typeof curDomainId === "function" ? curDomainId() : "") };
    sessionDomain = body.domain || "";
    // 多侧栏各自选择引擎；随这次创建请求传递，不修改全局偏好。
    // 欢迎页可逐次选择 Shepherd；未指定时沿用全局模式。
    const wantShepherd = opts && typeof opts.shepherd_mode === "boolean"
      ? opts.shepherd_mode : (typeof shepherdMode !== "undefined" && !!shepherdMode);
    body.engine = (opts && opts.engine) || hsNewSessionEngine(wantShepherd);
    if (wantShepherd) body.shepherd_mode = true;
    if (body.shepherd_mode && typeof shepherdTrial !== "undefined" && shepherdTrial) body.shepherd_trial = true;
    if (opts && opts.task) body.task = true;
    // desk_role 是可选且允许显式为空的；无角色接续必须把空串带到新会话索引里,
    // 才不会重新继承目标工作流可能配置的业务角色。
    if (opts && Object.prototype.hasOwnProperty.call(opts, "desk_role"))
      body.desk_role = String(opts.desk_role || "");
    // 会话的提示语言随界面语言，建立后固定
    if (typeof DeskI18n !== "undefined") body.prompt_locale = DeskI18n.locale;
    s = await api("/session", { method: "POST", headers: { "content-type": "application/json" },
                                body: JSON.stringify(body) });
  } catch (e) {
    alert("新建会话失败:" + (e && e.message ? e.message : e));
    return null;
  }
  const sid = s && (s.id || s.sessionID);
  if (!sid) { alert("新建会话失败:后端没有返回会话 id"); return null; }
  if (!(opts && opts.task)) {
    await openHtmlSession(sid, "对话", true,
                          { navigationEpoch, domain: sessionDomain, cwd: s.cwd || "" });
  }
  return s;
}
