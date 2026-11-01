// 由 app-chat.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 11/13 片
// ---- 目标与子代理:输入框附近的会话状态 -------------------------------------
// 两者共用输入框上下这条窄带。目标是「这一段一直生效的约束」,摆在上沿;子代理是
// 「此刻还有谁在替我干活」,摆在下沿。排队消息与它们都不同——那是「等着发」,仍在原位。

function hsGoalPaint(v, g) {
  const box = v.el.querySelector(".hs-goal");
  if (!box) return;
  const on = g && g.condition && !g.met;
  box.hidden = !on;
  if (!on) return;
  box.querySelector(".hs-goal-t").textContent = g.condition;
  box.title = g.condition;
}

// 开发态同步前端时后端进程不能重启(会打断正在跑的 agent),旧进程也就暂时没有 Codex
// goal()。若历史里最后一轮正是 /goal 且仍在运行,先从这条用户消息恢复待确认条；只认运行中
// 的同一轮,不会把很久以前已经结束的 /goal 当成当前目标。下次正常启动后由后端回执接管。
function hsGoalSeed(v, msgs) {
  if (v.goalPending || v.goalSig) return;
  const list = msgs || [];
  for (let i = list.length - 1; i >= 0; i--) {
    const info = (list[i] || {}).info || {};
    if (info.role !== "user" || info.goal || info.compact || info.aside) continue;
    const text = ((list[i].parts || []).filter((q) => q.type === "text")
                  .map((q) => q.text || "").join("\n")).trim();
    const match = /^\/goal\s+([\s\S]+)$/.exec(text);
    if (!match) return;   // 最近一次真人发言不是 /goal,不从更老的回合推断
    const running = list.slice(i + 1).some((m) => {
      const x = (m || {}).info || {};
      return x.role === "assistant" && x.running;
    });
    if (!running) return;
    v.goalPending = { condition: match[1].trim(), met: false, before: "",
                      expires: Date.now() + 120000 };
    hsGoalPaint(v, v.goalPending);
    return;
  }
}

// 目标:Claude 从 goal_status attachment 恢复,Codex 从 create_goal/update_goal 成功回执恢复。
// 展示层只认统一的 {condition, met};每轮收尾与 /goal 后的短轮询负责同步。
async function hsGoalSync(v) {
  let g = null;
  try { g = await api(`/session/${encodeURIComponent(v.sid)}/goal`); } catch { return; }
  const pending = v.goalPending;
  if (pending) {
    const remoteSig = (g && g.condition) ? g.condition + "|" + (g.met ? "1" : "0") : "";
    // 新的 active 回执说明工具已经真正登记成功；旧目标或空结果不能把乐观条提前抹掉。
    if (g && g.condition && !g.met && (!pending.before || remoteSig !== pending.before)) {
      v.goalPending = null;
    } else if (Date.now() < pending.expires) {
      g = pending;
    } else {
      v.goalPending = null;
    }
  }
  hsGoalPaint(v, g);
  // 达成那一刻把分割线补进上一回合末尾。它平时是从记录里画的(hsPour 见到 info.goal),而记录要等
  // 对账重建之后才读得到——于是「目标已达成」得刷新一次才出现(2026-08-20 用户实见)。
  // 只在这一次同步里看到「由未达成变成达成」时补:首次同步不补(记录里本来就有,补了是重复),
  // 之后的重建以记录为准重画,这一条会被换掉,同样不重复。
  const sig = (g && g.condition) ? g.condition + "|" + (g.met ? "1" : "0") : "";
  const prev = v.goalSig;
  v.goalSig = sig;
  if (prev !== undefined && prev !== sig && g && g.met && g.condition) hsGoalRow(v, g, null);
}

// ---- 任务栏 ----------------------------------------------------------------
// 会话不再归属某条任务(2026-08-10 `s:` 注记下线),本工作流的全部任务备忘对每段会话都可见。
// 那就得有个地方让人看见「这个工作流现在在办什么」——贴在输入框上沿,与目标条同一位置语言:
// 折叠时一行,展开列在办的任务,点一条打开工作流页同款任务详情。
const _hsTasksOpen = new Set();   // 展开态按会话记(内存级):有人边聊边对着任务看,换页回来该还开着

// 点别处收起。收起原本只挂在任务行上,而摊开之后清单会把那一行顶出视野——手机上一屏
// 装不下,那一行连同输入框一起被推到屏幕外,于是清单开着就再也关不掉(2026-08-17 手机实见)。
// 判据是「落点不在任务区里」:清单自己的行、就地添加的输入框、以及备忘卡片都算在内,
// 点它们是在用这份清单,不是要离开它。
// 挂 document 的捕获阶段:清单里的行各有各的 click 处理并且会 stopPropagation,
// 冒泡阶段收不到它们,而捕获阶段一定先经过这里。
const _hsTasksAway = new WeakMap();
function hsTasksOutside(v, open) {
  const prev = _hsTasksAway.get(v);
  if (prev) { document.removeEventListener("pointerdown", prev, true); _hsTasksAway.delete(v); }
  if (!open) return;
  const away = (ev) => {
    if (!v.el.isConnected) { document.removeEventListener("pointerdown", away, true); return; }
    const t = ev.target;
    // 共用覆盖条挂在 body；任务、安排和状态详情的滑块仍属于本会话的上下文卡片。
    const thumb = t.closest?.(".sb-thumb");
    if (thumb?._key && v.el.querySelector(".hs-context")?.contains(thumb._key)) return;
    if (t.closest && (t.closest(".hs-context") || t.closest(".wf-sheet") || t.closest(".hs-peek") || t.closest(".arr-modal"))) return;
    hsContextClose(v, "");
    document.removeEventListener("pointerdown", away, true);
    _hsTasksAway.delete(v);
  };
  _hsTasksAway.set(v, away);
  document.addEventListener("pointerdown", away, true);
}

// 一行会话记录属于哪个工作流。口径同侧栏:归属(domain)优先,归属是聊天时退到第一个
// 引用它的工作流(refs);都没有 = 这段会话不属于任何工作流。
function hsRowDomain(row) {
  if (!row) return "";
  const own = row.domain || DESKTOP;
  return own !== DESKTOP ? own : ((row.refs || [])[0] || "");
}

// 归属直接问会话记录自己(/session 的 domain 与 refs,后端已把会话表登记的那份补齐),
// 不再绕道 engine_sid 去会话表反查:引擎侧 id 要跑过一个回合才有,不落引擎侧 id 的
// 适配器一直没有,那条路对它恒不通。
async function hsSessionDomain(v) {
  // 侧栏那份会话清单(/workbench/terminals,app-panel-terms 维护)每行已带 domain 与 refs,
  // 与 /session 同一口径(term._term_persisted_scan 与 ui_contract._session_belonging 都读会话表登记)。
  // 在里面找到就不再整份拉 /session:那是 12KB、经隧道两个往返,只为查一行的归属
  // (2026-09-03 手机链路盘点)。没找到(清单未到、刚建的会话)才回落到网络。
  const cached = (typeof _termsList !== "undefined" && Array.isArray(_termsList))
    ? _termsList.find((x) => x && (x.session_id === v.sid || x.id === v.sid)) : null;
  // 清单行的 id 是引擎侧锚点(uuid),GUI 会话自己的 adp_ id 在 session_id 里(见 term._term_persisted_scan)。
  // 原来只比 id,对 GUI 会话恒不中,这条捷径等于没接上(2026-09-04 调用栈实证)。
  if (cached) return hsRowDomain(cached);
  let list = [];
  try { list = await api("/session"); } catch { return null; }   // null=没取到,与「确认无归属」("")分开
  return hsRowDomain((list || []).find((x) => x.id === v.sid));
}

// 只有确认不属于工作流时才隐藏。属于工作流的会话里这一栏是任务的固定入口，
// 即便当前没有在办任务也保留；展开后仍可就地写下第一条。
function hsTasksHide(v, box) {
  box.hidden = true;
}

function hsTaskBelongsToSession(task, sid) {
  return ((task && task.ledger && task.ledger.sessions) || []).some((session) => session && session.id === sid);
}

// 取数失败≠没有任务。原来任何一步失败都直接藏栏,又只在回合收尾时再同步——一次瞬时
// 超时(经隧道 P90 有过 2.2s,api 上限 8s,拥挤时够得着)就是「栏子消失到下次对话为止」
// (2026-08-26 手机实见,间歇出现)。失败时保住已显示的那份,记一行日志,5 秒后重试一次;
// 「确认无归属」照旧隐藏——那不是失败,是答案。
function hsTasksBail(v, box, why) {
  deskLog(`[hs] 任务栏取数失败(${why}):` + (box.hidden ? "本就没显示,5s 后重试" : "保留上一份,5s 后重试"));
  if (v._tasksRetryT) return;
  v._tasksRetryT = setTimeout(() => {
    v._tasksRetryT = 0;
    if (v.el.isConnected) hsTasksSync(v);
  }, 5000);
}

async function hsTasksSync(v) {
  const box = v.el.querySelector(".hs-tasks");
  if (!box) return;
  const dom = await hsSessionDomain(v);
  if (dom === null) { hsTasksBail(v, box, "session"); return; }
  if (!dom) { hsTasksHide(v, box); return; }
  let data = null;
  try { data = await _fetchTasks(dom); } catch { hsTasksBail(v, box, "tasks:" + dom); return; }
  const items = (data.items || []).filter((t) => t.state !== "done");   // 只列在办的:
  // 已完成的备忘写的是「当时怎么做」,任务闭环后这层指导会把过时做法带回来,看它去工作流页的归档区。
  const sessionCurrent = items.filter((t) => t.state === "doing" && hsTaskBelongsToSession(t, v.sid));
  const current = sessionCurrent[0]
    || items.find((t) => t.state === "doing") || items[0] || null;
  const currentItems = sessionCurrent;
  const backlogItems = items.filter((t) => !hsTaskBelongsToSession(t, v.sid));
  box.hidden = false;
  v.taskDom = dom;
  v.currentTask = current;
  const currentLabel = box.querySelector(".hs-task-current-label");
  if (current) DeskI18n.plain(currentLabel, current.title); else DeskI18n.bind(currentLabel, "ctx.task");
  DeskI18n.bind(box.querySelector(".hs-tasks-sum-title"), "ctx.allTodos", { count: backlogItems.length });
  const currentSection = box.querySelector(".hs-current-task-section");
  currentSection.hidden = !currentItems.length;
  const currentList = box.querySelector(".hs-current-tasks");
  currentList.innerHTML = "";
  for (const task of currentItems) {
    const row = document.createElement("button"); row.type = "button"; row.className = "hs-current-task";
    row.innerHTML = '<span class="hs-current-task-title"></span><span class="hs-current-task-meta"></span>';
    row.querySelector(".hs-current-task-title").textContent = task.title;
    row.querySelector(".hs-current-task-meta").textContent =
      `${task.t || DeskI18n.t("task.noId")} · ${DeskI18n.t(task.state === "doing" ? "task.doing" : "task.todo")}`;
    row.onclick = (ev) => { ev.stopPropagation(); wfOpenTaskSheet(task, dom); };
    currentList.appendChild(row);
  }
  const list = box.querySelector(".hs-tasks-list");
  const sig = dom + "|" + backlogItems.map((t) => `${t.t}:${t.line}:${t.state}:${t.title}`).join("\n");
  if (list.dataset.sig !== sig) {
    list.dataset.sig = sig;
    list.innerHTML = "";
    for (const t of backlogItems) {
      const row = document.createElement("div");
      row.className = "hs-task" + (t.state === "doing" ? " doing" : "");
      row.innerHTML = '<span class="hs-task-id"></span><span class="hs-task-t"></span>';
      const taskID = String(t.t || "").trim();
      const id = row.querySelector(".hs-task-id");
      id.textContent = taskID;
      id.classList.toggle("empty", !taskID);
      row.querySelector(".hs-task-t").textContent = t.title;
      row.title = taskID ? `${taskID} ${t.title}` : t.title;
      // 任务栏与工作流页只保留一个详情实现：同一壳、同一流程图、同一备忘编辑能力。
      row.onclick = (ev) => { ev.stopPropagation(); wfOpenTaskSheet(t, dom); };
      // 右键与任务详情使用同一份菜单。编辑任务流时直接打开详情浮层里的画布。
      // 改完记录不会自己回流到这一栏,菜单里的动作跑完补一次同步。
      row.oncontextmenu = (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        wfCloseTaskSheet();
        const go = () => {
          const sheet = wfOpenTaskSheet(t, dom);
          _wfFlowBuilder(sheet.querySelector(".wf-sheet"), t, dom, _wfFlowStages(t) || []);
        };
        const items = wfTaskMenuItems(t, dom, _wfFlowStages(t) || [], { flow: go })
          .map((it) => (it.fn ? { ...it, fn: (...a) => {
            const r = it.fn(...a);
            setTimeout(() => hsTasksSync(v), 400);
            return r;
          } } : it));
        openCtxMenu(ev.clientX, ev.clientY, items);
      };
      list.appendChild(row);
    }
  }
  const open = _hsTasksOpen.has(v.sid);
  hsTasksPaint(v, open);
  hsTasksOutside(v, open);   // 切回一段本来就摊开的会话:那条「点别处收起」也要跟着挂回来
}

function hsTasksPaint(v, open) {
  const box = v.el.querySelector(".hs-tasks");
  if (!box) return;
  box.classList.toggle("open", open);
  box.querySelector(".hs-tasks-popover").hidden = !open;
  box.querySelector(".hs-tasks-bar").setAttribute("aria-expanded", String(open));
  for (const list of box.querySelectorAll(".hs-current-tasks, .hs-tasks-list"))
    list.dispatchEvent(new CustomEvent("desk-scrollbar-refresh", { bubbles: true }));
}

// ---- 会话安排与状态 --------------------------------------------------------
const _hsArrangementOpen = new Set();
const _hsStatusOpen = new Set();

function hsContextClose(v, except) {
  if (except !== "tasks") { _hsTasksOpen.delete(v.sid); hsTasksPaint(v, false); }
  if (except !== "arrangements") {
    _hsArrangementOpen.delete(v.sid);
    const pop = v.el.querySelector(".hs-arrangements-popover");
    if (pop) pop.hidden = true;
    v.el.querySelector(".hs-arrangements-bar")?.setAttribute("aria-expanded", "false");
  }
  if (except !== "status") {
    _hsStatusOpen.delete(v.sid);
    const pop = v.el.querySelector(".hs-context-status-popover");
    if (pop) pop.hidden = true;
    v.el.querySelector(".hs-context-status-bar")?.setAttribute("aria-expanded", "false");
  }
}

async function hsArrangementsSync(v) {
  let data = null;
  try { data = await api("/workbench/arrangements", { timeout: 20000 }); } catch { return; }
  const items = (data.items || []).filter((item) => item.session_id === v.sid);
  v.sessionArrangements = items;
  const enabled = items.filter((item) => item.enabled);
  const next = enabled.map((item) => Number(item.next_at || 0)).filter(Boolean).sort((a, b) => a - b)[0];
  const meta = v.el.querySelector(".hs-arrangements-meta");
  if (meta && next) DeskI18n.plain(meta, _arrNext(next));
  else if (meta) DeskI18n.bind(meta, items.length ? "ctx.arrangementCount" : "ctx.noArrangements", { count: items.length });
  const list = v.el.querySelector(".hs-arrangements-list");
  if (!list) return;
  list.innerHTML = items.length ? "" : '<div class="hs-arrangements-empty"><span class="hs-arrangements-empty-icon" aria-hidden="true"></span><span class="hs-arrangements-empty-copy"><strong>当前会话暂无安排</strong><span>可以创建定时或手动运行的安排</span></span></div>';
  if (!items.length) {
    DeskI18n.bind(list.querySelector(".hs-arrangements-empty-copy strong"), "ctx.arrEmptyTitle");
    DeskI18n.bind(list.querySelector(".hs-arrangements-empty-copy span"), "ctx.arrEmptyHint");
  }
  for (const item of items) {
    const row = document.createElement("button"); row.type = "button"; row.className = "hs-arrangement-row";
    row.innerHTML = '<span></span><small></small>';
    row.querySelector("span").textContent = item.name || item.instruction || DeskI18n.t("arr.unnamed");
    row.querySelector("small").textContent = item.enabled ? _arrNext(item.next_at) : DeskI18n.t("arr.paused");
    row.onclick = (ev) => { ev.stopPropagation(); _arrEditSheet(item, { sessionID: v.sid, onSaved: () => hsArrangementsSync(v) }); };
    list.appendChild(row);
  }
}

function hsArrangementsToggle(v, ev) {
  ev?.stopPropagation();
  const open = !_hsArrangementOpen.has(v.sid);
  hsContextClose(v, open ? "arrangements" : "");
  if (open) _hsArrangementOpen.add(v.sid); else _hsArrangementOpen.delete(v.sid);
  v.el.querySelector(".hs-arrangements-popover").hidden = !open;
  v.el.querySelector(".hs-arrangements-bar").setAttribute("aria-expanded", String(open));
  if (open) { hsArrangementsSync(v); hsTasksOutside(v, true); }
}

function hsArrangementsManage(v) {
  hsContextClose(v, "");
  const modal = _arrSheet(DeskI18n.t("ctx.manage"), DeskI18n.t("arr.sessionScope"));
  const body = modal.querySelector(".arr-sheet-body");
  const paint = () => {
    const items = v.sessionArrangements || [];
    body.innerHTML = '<div class="hs-arrangement-manager"></div><div class="arr-form-actions"><button class="arr-primary" type="button" data-new></button></div>';
    body.querySelector("[data-new]").textContent = DeskI18n.t("arr.new");
    const list = body.querySelector(".hs-arrangement-manager");
    list.innerHTML = items.length ? "" : '<div class="wf-empty"></div>';
    if (!items.length) list.firstChild.textContent = DeskI18n.t("arr.noneInSession");
    for (const item of items) {
      const row = document.createElement("div"); row.className = "hs-arrangement-manage-row";
      row.innerHTML = '<button type="button" data-edit><span></span><small></small></button><button type="button" data-toggle></button>';
      row.querySelector("span").textContent = item.name || item.instruction || DeskI18n.t("arr.unnamed");
      row.querySelector("small").textContent = _arrTrigger(item.trigger);
      row.querySelector("[data-toggle]").textContent = DeskI18n.t(item.enabled ? "arr.pause" : "arr.resume");
      row.querySelector("[data-edit]").onclick = () => _arrEditSheet(item, { sessionID: v.sid, onSaved: async () => { await hsArrangementsSync(v); hsArrangementsManage(v); } });
      row.querySelector("[data-toggle]").onclick = async () => {
        try { await api(`/workbench/arrangements/${encodeURIComponent(item.id)}/toggle${_arrAt(item.backend)}`, { method: "POST" }); await hsArrangementsSync(v); paint(); }
        catch (error) { _deskNotice(error.message || DeskI18n.t("common.failed")); }
      };
      list.appendChild(row);
    }
    body.querySelector("[data-new]").onclick = () => _arrEditSheet(null, { sessionID: v.sid, onSaved: () => hsArrangementsSync(v) });
  };
  paint();
}

function hsContextStatusSync(v) {
  const host = v.el.querySelector(".hs-context-status");
  if (!host) return;
  const current = v.envCurrent || {};
  // 升级回执留在可见浮层中；普通 config 轮询不能抹掉进度/结果，登录和其他故障仍优先。
  const update = v.engineUpdateNotice;
  const p = update && update.engine === v.engine
    && (!current.stage || current.stage === "version" || !["warning", "error"].includes(current.state) && current.stage !== "auth")
    ? update : current;
  const state = p.stage === "auth" ? "login" : (["error", "warning", "working", "update"].includes(p.state) ? p.state : "ready");
  // Backend labels stay as sent; only the fixed fallbacks follow the interface language.
  const titleKey = p.label ? (state === "ready" && p.stage !== "version" ? "ctx.ready" : "")
    : (state === "ready" ? "ctx.ready" : (state === "login" ? "ctx.needLogin" : "ctx.needAction"));
  const title = titleKey ? DeskI18n.t(titleKey) : String(p.label);
  host.className = `hs-context-status ${state}`;
  const meta = host.querySelector(".hs-context-status-meta");
  if (titleKey) DeskI18n.bind(meta, titleKey); else DeskI18n.plain(meta, title);
  const details = [...new Set((p.details || []).map(x => String(x || "").trim()).filter(Boolean))];
  // 摘要和详情来自同一错误时只展示一份；被截断的摘要在浮层中恢复完整文字。
  const full = hsEnvFull(title, details);
  const heading = host.querySelector(".hs-context-status-title");
  if (titleKey && !details.length) DeskI18n.bind(heading, titleKey); else DeskI18n.plain(heading, full || title);
  const detail = host.querySelector(".hs-context-status-detail");
  if (state === "ready" && p.stage !== "version") DeskI18n.bind(detail, "ctx.readyDetail", { engine: v.engine || "Desk" });
  else DeskI18n.plain(detail, full ? "" : details.filter(x => x !== title).join("\n"));
  detail.hidden = !detail.textContent;
  const action = host.querySelector(".hs-context-status-action");
  action.hidden = !(p.action && p.action.label);
  action.dataset.kind = p.action?.kind || "";
  action.querySelector(".hs-context-status-action-label").textContent = p.action?.label || "";
  const pending = p === update && update.state === "working";
  action.disabled = pending;
  action.setAttribute("aria-busy", String(pending));
}

function hsContextStatusToggle(v, ev) {
  ev?.stopPropagation();
  const open = !_hsStatusOpen.has(v.sid);
  hsContextClose(v, open ? "status" : "");
  if (open) _hsStatusOpen.add(v.sid); else _hsStatusOpen.delete(v.sid);
  v.el.querySelector(".hs-context-status-popover").hidden = !open;
  v.el.querySelector(".hs-context-status-bar").setAttribute("aria-expanded", String(open));
  hsContextStatusSync(v);
  if (open) hsTasksOutside(v, true);
}

function hsContextStatusAction(v, ev) {
  ev.stopPropagation();
  const action = v.envCurrent?.action;
  if (action?.kind === "engine-update") {
    return hsEngineUpdate(v, action.engine, ev.currentTarget);
  }
  const legacy = v.envEl?.querySelector(".hs-env-a");
  if (legacy && !legacy.hidden) legacy.click();
}

// 子代理:每个有独立记录,边跑边写。这里只列名字与最新一步,点开才去取它的正文。
// 主会话自己也占一条,和 TUI 一致——「谁在干活」要连主线一起看才成立。
async function hsAgentsSync(v) {
  hsAgentArm(v);   // 先记下「刚派出去了一个」,再去问名单——那一刻名单多半还是空的
  // 首次请求本来就可能早于子代理记录落盘，也可能暂时失败。先启动有界 watcher，不能把
  // 是否重试系在这一次请求成功上；progress 帧本身无需反复触发同步。
  if (Date.now() < (v.agentWatchUntil || 0)) hsAgentsWatch(v, true);
  let list = [];
  try { list = await api(`/session/${encodeURIComponent(v.sid)}/agents`); } catch { return; }
  const btn = v.el.querySelector(".hs-agent");
  if (!btn) return;
  // 名单分两拨。在跑的一律留下,不问它是哪一轮派出去的:异步派出的代理会活过派它出来的
  // 那个回合,刷新页面之后 statusT0 也会重算,按回合切会把它们全滤掉——入口于是「跑着跑着
  // 就没了」「刷新一下就没了」。已经交回的那些才按本回合切:subagents 目录只增不减,
  // 这段会话攒了四十多个,不切的话一打开就是一份历史名单。
  // 已交回的按「这一轮派出去的」切,判据取 started(何时派出)。用 updated 不成立:那是它
  // 最后写记录的时刻,上一轮的代理只要交回得晚一点就会算进这一轮。判不出归属时(statusT0
  // 为 0,比如刚刷完页面还没开始新回合)一条都不列——那时「本回合」并不存在,放行等于把
  // 整段会话的历史名单摊开,刷新后菜单里冒出八条旧代理正是这么来的。
  const since = v.statusT0 || 0;
  const live = (list || []).filter((a) => hsAgentLive(v, a));
  const past = since ? (list || []).filter((a) => !hsAgentLive(v, a)
                                    && ((a.started || a.updated || 0) >= since - 5000)) : [];
  const room = Math.max(0, 8 - live.length);
  v.agents = (room ? past.slice(-room) : []).concat(live)
    .sort((x, y) => (x.started || 0) - (y.started || 0));
  const busyAg = live.length > 0;
  hsAgentsWatch(v, busyAg || !!v.agentOpen || Date.now() < (v.agentWatchUntil || 0));
  // 例外是你正开着某个子代理的面板:那时先留着,免得看到一半入口自己没了。
  // 父回合还在跑的时候也留着——这一轮派出去的代理即使都交回了,那几份报告仍然是这一轮的事。
  btn.hidden = !v.agents.length || !(busyAg || v.agentOpen || (v.busy && since));
  if (btn.hidden) return;
  btn.classList.toggle("run", !!busyAg);
  // 钮上只报「有几个在跑」。摆一个具体的任务名等于在两三个字里塞一件正在发生的事,
  // 名字一长就截断,还会随着谁最后写了一行而跳来跳去。谁在做什么,点开看。
  btn.querySelector(".hs-agent-t").textContent =
    busyAg ? DeskI18n.t("agent.runningCount", { count: live.length })
      : (v.agentOpen ? DeskI18n.t("agent.view") : DeskI18n.t("agent.count", { count: v.agents.length }));
  btn.onclick = (ev) => hsAgentPick(v, ev.currentTarget);
}

// 后台命令(Bash run_in_background)此刻有几条在跑。
// 与子代理那枚并排、同一套形制,但不带箭头也点不开:它只报一个数。命令的产出去它自己的
// 输出文件里看,菜单里摆一行「某某命令」既点不出内容,也会让人以为点得开。
// 一条命令活得比回合长(2026-08-20 实测有跑满 120 秒的),所以这枚钮不随回合收起,
// 只看还有没有在跑的。
function hsBashPaint(v) {
  const el = v.el && v.el.querySelector(".hs-bash");
  if (!el) return;
  const n = (v.bgtasks && v.bgtasks.running) || 0;
  el.hidden = !n;
  hsBashWatch(v, !!n);
  if (!n) return;
  el.querySelector(".hs-bash-t").textContent = DeskI18n.t("agent.scriptsRunning", { count: n });
}

function hsBashWatch(v, on) {
  if (!on) { clearInterval(v.bashTick); v.bashTick = 0; return; }
  if (v.bashTick) return;
  v.bashTick = setInterval(() => {
    if (!v.el.isConnected) { hsBashWatch(v, false); return; }
    if (hsHidden(v)) return;
    hsEngineSync(v);  // /config 会让后端按 output 文件终止标记对账
  }, 3000);
}

// 有代理在跑的时候盯着这份名单。「派出去」在主线上只有一帧,而那一刻新代理的记录文件
// 常常还没落地,只在那一帧同步,名单就停在「只有一个」,要等主线下一次动工具才补上。
// 实测两个代理相差 6 秒启动,界面上却隔了几分钟才见到第二个。
function hsAgentsWatch(v, on) {
  if (!on) { clearInterval(v.agentsTick); v.agentsTick = 0; return; }
  if (v.agentsTick) return;
  v.agentsTick = setInterval(() => {
    if (!v.el.isConnected) { hsAgentsWatch(v, false); return; }
    if (hsHidden(v)) return;   // 露面时会主动同步,藏着不反复扫描 rollout
    hsAgentsSync(v);
  }, 3000);
}

// 开关不能只由「名单里有代理在跑」决定:刚派出去那一刻记录文件还没落地,名单是空的,
// 这一拍判为不用轮询,之后就再也没人去查,入口自始至终不出现。所以父回合里每多出一张
// Agent 工具卡,就先盯一段时间,等它落地——落地之后由准确状态接手。
const HS_AG_ARM = 60000;

function hsAgentArm(v) {
  const t = v.turns[v.turns.length - 1];
  let n = 0;
  if (t) for (const q of t.parts.values()) {
    if (q.type === "tool" && /^(task|agent|subagent)$/.test(String(q.tool || "").toLowerCase())) n++;
  }
  if (n && n !== v.agentToolN) v.agentWatchUntil = Date.now() + HS_AG_ARM;
  v.agentToolN = n;
}

// 菜单与模式、模型同一套:主线一条(等于收起子代理视图),其余每个子代理一条,
// 右侧压暗的一格报它此刻的状态。选中态用勾,和那两个菜单一致。
// 一个子代理跑了多久。还在跑的算到此刻;已收场的算到收场那一刻。收场时刻缺失(判不出
// 怎么收的那一档)退回记录最后一次改动的时间——它是这份记录停笔的地方,比算到此刻实在。
function hsAgentSpan(a) {
  const end = a.ended || (a.status === "running" ? Date.now() : (a.updated || 0));
  return Math.max(0, end - (a.started || 0));
}

// 在跑的那一档报形态,不报具体动作。「调用 Bash」这种具体到工具的话在主线的过程区里有用
// ——那里逐步都在,一步一行;摆进这份名单只是一格随时在变的噪音,名字长了还会截断。
// 后端给的 last 只分得出两样:一句正文,或者「调用 <工具名>」,据此给两个词。
function hsAgentDoing(a) {
  return /^调用 /.test(String(a.last || "")) ? "处理中" : (a.last ? "正在输出" : "处理中");
}

// 右侧那一格:形态加时长,空格分开。形态在跑时是「处理中 / 正在输出」,收场后是状态本身
// (已交回、无响应、进程已退出)。时长走主线那套时钟(hsClock,`1m34s`),两处读起来是
// 同一种东西;没有它,一个刚派出去的和一个跑了二十分钟的在这份名单里长得一模一样。
function hsAgentValue(v, a) {
  const st = hsAgentState(v, a);
  return [st.note || hsAgentDoing(a), hsClock(hsAgentSpan(a))].filter(Boolean).join(" ");
}

// 菜单是一次性构造的 DOM,构造完就不再动,于是那个秒数停在打开的那一刻——要看它走,
// 得把菜单关掉再打开一次。这里在菜单开着的时候每秒重绘一次那几格。
// 按 id 回 v.agents 取当时最新的一条,而不是用构造时捕获的旧对象:那三秒一次的轮询会
// 把状态换掉,一个在你看着的时候交回的代理,应当当场从「处理中」变成「已交回」。
function hsAgentTick(v) {
  const menu = document.querySelector(".rp-ctx");
  if (!menu) return;
  const rows = [...menu.querySelectorAll(".rp-row")].slice(1);   // 第一行是主线
  const ids = (v.agents || []).map((a) => a.id);
  const tick = setInterval(() => {
    if (!menu.isConnected) { clearInterval(tick); return; }
    ids.forEach((id, i) => {
      const a = (v.agents || []).find((x) => x.id === id);
      const cell = rows[i] && rows[i].querySelector(".rp-value");
      if (a && cell) cell.textContent = hsAgentValue(v, a);
    });
  }, 1000);
}

function hsAgentPick(v, anchor) {
  const items = [{ dot: v.busy ? "run" : "done", label: DeskI18n.t("ctx.main"), value: v.busy ? DeskI18n.t("task.doing") : "",
                   checked: !v.agentOpen, fn: () => hsAgentClose(v) }];
  for (const a of v.agents || []) {
    const st = hsAgentState(v, a);
    items.push({ dot: st.dot, label: hsAgentName(a), value: hsAgentValue(v, a),
                 checked: v.agentOpen === a.id, fn: () => hsAgentOpen(v, a) });
  }
  const r = anchor.getBoundingClientRect();
  openCtxMenu(r.left, r.top - 8 - items.length * 34, items, "rp-wide");
  hsAgentTick(v);
}

function hsAgentViewHidden(v) {
  return document.hidden || (typeof hsHidden === "function" && hsHidden(v));
}

function hsAgentRefreshOpen(v) {
  if (!v || !v.agentOpen || hsAgentViewHidden(v)) return;
  const id = v.agentOpen;
  const known = (v.agents || []).find((x) => x.id === id)
    || (v.agentCurrent && v.agentCurrent.id === id ? v.agentCurrent : null);
  if (known) hsAgentOpen(v, known);
}

// 一个子代理此刻在不在干活。后端给的 status 是准确判据:它取自引擎自己写下的事实——
// 同步派出的那一路看父回合那次 Agent 调用有没有返回,异步派出的那一路看跑完时注入的
// task-notification。两者都没有就是还在跑。这里不再按「记录多久没动」猜:子代理的记录
// 写得断续,一次长工具调用期间可以安静好几分钟,时间窗在那里必然误判。
// 后端给不出状态时(旧版本,或派它出来的那一轮已经不在记录里)才退回原来的时间窗。
const HS_AG_FRESH = 90000;

function hsAgentLive(v, a) {
  if (!a) return false;
  // 后端会同时核对受管监督进程和当前工具调用；明确的 running 是生命周期事实。
  // 长模型等待或长工具调用可以超过半小时没有记录，前端只把静默显示成「无响应」，
  // 不能再擅自改写成「已停止」。历史悬挂记录也由后端统一收敛为 gone。
  if (a.status === "running") return true;
  if (a.status === "done") return false;
  return !!v.busy && (hsAgentToolBusy(v) || Date.now() - (a.updated || 0) < HS_AG_FRESH);
}

// 父回合里还挂着没返回的 Agent 工具。只在后端给不出状态时用得上。
function hsAgentToolBusy(v) {
  const t = v.turns[v.turns.length - 1];
  if (!t) return false;
  for (const q of t.parts.values()) {
    if (q.type !== "tool") continue;
    if (!/^(task|agent|subagent)$/.test(String(q.tool || "").toLowerCase())) continue;
    if ((q.state || {}).status === "running") return true;
  }
  return false;
}

// 一个子代理此刻的状态,分三档:在跑;在跑但记录很久没动,多半卡住了,得说出来——
// 那时它的「最新一步」是几分钟前的旧话,不点破的话人会以为还在推进;已经收场的报收场方式。
// 「很久」分两档:有准确状态时放宽到五分钟,一次大扫描安静两分钟是常事,按两分钟报
// 「无响应」会天天误报;只能靠时间窗猜的时候仍用两分钟,那一路本来就没有别的信号。
const HS_AG_STALE = 120000;

const HS_AG_QUIET = 300000;

// 一个子代理是怎么收的场。菜单、面板头、正文流里那条分割线共用这一份文案:同一件事
// 在三处出现,措辞不一致就得让人自己去对。
// gone:派它出来的那个引擎进程实例已经换过一轮,它随旧实例一起没了,谈不上交回。
// failed 单列一档——引擎在通知里就是这么报的,并进「已交回」等于把一次失败说成正常结束。
function hsAgentOutcomeText(outcome) {
  if (outcome === "gone") return "进程已退出";
  if (outcome === "stopped" || outcome === "killed") return "已中止";
  if (outcome === "failed") return "已失败";
  return "执行完成";
}

function hsAgentState(v, a) {
  if (!hsAgentLive(v, a)) return { dot: "done", note: hsAgentOutcomeText(a.outcome) };
  if (a.status === "running" && a.phase === "capacity") return { dot: "run", note: DeskI18n.t("agent.queued") };
  const idle = Date.now() - (a.updated || 0);
  if (idle > (a.status ? HS_AG_QUIET : HS_AG_STALE)) {
    return { dot: "stale", note: DeskI18n.t("agent.idle", { n: Math.round(idle / 60000) }) };
  }
  return { dot: "run", note: "" };
}

// 面板头上的状态。开着这层看的时候,人最想知道的是「它还在动吗」——原来只有一个任务名,
// 在跑、卡住、已交回三种情形长得一模一样。状态本来就算出来了,只是没往这儿放。
// 在跑的那档再带一句「最后一步多久之前」:没有它,一个安静了四十秒的代理和刚写完一行的
// 代理看上去还是一样的。
function hsAgentHead(v, a) {
  const host = v.el.querySelector(".hs-agview");
  if (!host) return;
  const st = hsAgentState(v, a);
  const dot = host.querySelector(".rp-dot");
  if (dot) dot.className = "rp-dot rp-dot-" + st.dot;
  const el = host.querySelector(".hs-agv-st");
  if (!el) return;
  hsSetText(el, st.dot === "run" ? (st.note || DeskI18n.t("agent.running")) + " · " + hsAgo(a.updated)
                                  : (st.note || DeskI18n.t("agent.returned")));
}

// 「多久之前」。子代理这层只有秒到分的量级,再粗就没意义了。
function hsAgo(ts) {
  const s = Math.max(0, Math.round((Date.now() - (ts || 0)) / 1000));
  if (s < 5) return DeskI18n.t("time.justNow");
  if (s < 60) return DeskI18n.t("time.seconds", { n: s });
  return DeskI18n.t("time.minutes", { n: Math.round(s / 60) });
}

// 子代理任务常带一层 `Task:` 委派信封。界面只展示真正交给子代理的任务文本，
// 原始信封仍保留在日志记录里，展开工具详情时可审计。
function hsAgentTaskText(value) {
  const raw = String(value || "").trim();
  const marks = [...raw.matchAll(/(?:^|\n)Task\s*[:：]\s*/gi)];
  if (!marks.length) return raw;
  const mark = marks[marks.length - 1];
  return raw.slice((mark.index || 0) + mark[0].length).trim();
}

function hsAgentName(a) {
  return hsEllipsis(hsAgentTaskText(a.task || a.kind || "agent").replace(/\s+/g, " "), 24);
}

// 子代理记录沿用主线的回合骨架：用户输入仍是气泡，助手正文仍是 .hs-text，思考和工具
// 仍收进一条 .hs-proc。此前这里只借用了工具行，其他内容另画一套，结果 markdown 标记
// 会直接露出来，几十步工具也会全部摊开；同一段会话切进子代理后像换了一个产品。
function hsAgentTurn(body) {
  const el = document.createElement("section");
  el.className = "hs-agv-turn";
  body.appendChild(el);
  return { el, assistant: null, process: null };
}

// 子代理接口有些 provider 会把初始任务附在记录末尾。界面按语义而不是存储顺序展示：
// 初始 prompt 固定放在顶部；后续 user 消息仍留在真实发生的位置。优先认 Task: 信封，
// 兼容没有信封的 provider 时才退到第一条 user 文本。
function hsAgentPrompt(msgs, agent) {
  let first = null;
  for (const [mi, m] of (msgs || []).entries()) {
    if ((m.info || {}).role !== "user") continue;
    for (const [pi, q] of (m.parts || []).entries()) {
      if (q.type !== "text" || !(q.text || "").trim()) continue;
      const item = { mi, pi, text: hsAgentTaskText(q.text), raw: q.text };
      if (!first) first = item;
      if (/^\s*Task\s*[:：]/i.test(q.text)) return item;
    }
  }
  if ((agent && agent.task || "").trim()) return { mi: -1, pi: -1, text: hsAgentTaskText(agent.task), raw: agent.task };
  return first;
}

function hsAgentPromptEcho(text, prompt) {
  if (!prompt || !(prompt.text || "").trim()) return false;
  const got = hsAgentTaskText(text).replace(/\s+/g, " ").trim();
  const want = String(prompt.text || "").replace(/\s+/g, " ").trim();
  return !!got && got === want;
}

function hsAgentPromptTurn(body, text) {
  const turn = hsAgentTurn(body);
  const u = document.createElement("div");
  u.className = "hs-u hs-u-peer hs-agv-prompt";
  u.innerHTML = '<div class="hs-bubble"><div class="hs-u-body md"></div></div>';
  u.querySelector(".hs-u-body").innerHTML = md.render(hsAgentTaskText(text)).trim();
  turn.el.appendChild(u);
}

function hsAgentNotice(body, text) {
  const turn = hsAgentTurn(body);
  const d = document.createElement("div");
  d.className = "hs-agv-note";
  d.textContent = text;
  turn.el.appendChild(d);
}

function hsAgentAssistant(turn) {
  if (turn.assistant) return turn.assistant;
  turn.assistant = document.createElement("div");
  turn.assistant.className = "hs-a";
  turn.el.appendChild(turn.assistant);
  return turn.assistant;
}

function hsAgentProcess(v, turn, key) {
  if (turn.process) return turn.process;
  const el = document.createElement("div");
  el.className = "hs-proc hs-agv-proc has-body has-steps";
  el.innerHTML = '<div class="hs-proc-bar"><span class="hs-proc-slot">'
    + '<span class="hs-status st-done"><i class="hs-dot"></i><span class="hs-st-t"></span></span>'
    + '</span></div><div class="hs-proc-body"></div><div class="hs-rule hs-agv-result-rule" hidden></div>'
    + '<div class="hs-agv-result" hidden></div>';
  const openKey = "process:" + key;
  if (v.agentExpanded.has(openKey)) el.classList.add("open");
  el.querySelector(".hs-proc-bar").onclick = () => {
    el.classList.toggle("open");
    if (el.classList.contains("open")) v.agentExpanded.add(openKey);
    else v.agentExpanded.delete(openKey);
  };
  const assistant = hsAgentAssistant(turn);
  assistant.insertBefore(el, assistant.firstChild);
  turn.process = { el, body: el.querySelector(".hs-proc-body"),
    result: el.querySelector(".hs-agv-result"), rule: el.querySelector(".hs-agv-result-rule"), count: 0 };
  return turn.process;
}

function hsAgentResult(proc, node) {
  proc.rule.hidden = false;
  proc.result.hidden = false;
  proc.result.appendChild(node);
}

// 打开一个子代理的输出:盖在正文区上的一层只读视图。不能在这里发言,但执行摘要可以
// 像主会话一样展开,查看命令、参数、输出与差异;要接手仍回主线说话。
async function hsAgentOpen(v, a) {
  const same = v.agentOpen === a.id;
  const requestSeq = (v.agentLogSeq || 0) + 1;
  v.agentLogSeq = requestSeq;
  if (!same || !(v.agentExpanded instanceof Set)) v.agentExpanded = new Set();
  v.agentOpen = a.id;
  v.agentCurrent = a;
  let host = v.el.querySelector(".hs-agview");
  if (!host) {
    host = document.createElement("div");
    host.className = "hs-agview";
    // 头里再套一层:这一层要和正文收在同一列宽里(见 style.css 的 .hs-agv-hin),
    // 而分隔线仍横贯整幅——线是页头与正文的分界,不是那一列的边框。
    host.innerHTML = '<div class="hs-agv-h"><div class="hs-agv-hin"><i class="rp-dot"></i>'
      + '<span class="hs-agv-t"></span><span class="hs-agv-st"></span>'
      // 这枚钮只是收起这一层、回到主线,不会停掉子代理(hsAgentClose 没有任何停止调用,
      // 它清轮询、摘掉浮层、重算入口就完了)。原来画的是一个 ×,读起来像「终止」——
      // 同一个位置在别处(提问面板、审阅侧栏)确实是关掉一件事,这里却不是
      // (2026-08-24 用户问到时发现)。保留「主线」文字,但方向提示用图标而不是文字箭头。
      + '<button class="hs-agv-x" title="' + hsEsc(DeskI18n.t("agent.backHint")) + '" aria-label="'
        + hsEsc(DeskI18n.t("agent.backToMain")) + '">'
        + '<span class="ui-arrow-icon is-left" aria-hidden="true"></span><span>' + hsEsc(DeskI18n.t("ctx.main"))
        + '</span></button></div></div>'
      + '<div class="hs-agv-b"></div>';
    host.querySelector(".hs-agv-x").onclick = () => hsAgentClose(v);
    v.el.querySelector(".hs-wrap").appendChild(host);
  }
  host.querySelector(".hs-agv-t").textContent = hsAgentName(a);
  hsAgentHead(v, a);
  const body = host.querySelector(".hs-agv-b");
  // 首次打开才给加载态。轮询重取时保留旧内容,否则每四秒整页闪一次；同时记住人是否正在
  // 跟随末尾——已经往上翻去看某一步详情时,新记录不该把视口强拉回底部。
  const oldTop = body.scrollTop;
  const follow = !same || body.scrollHeight - body.scrollTop - body.clientHeight < 36;
  if (!same) body.textContent = DeskI18n.t("common.reading");
  hsAgentsSync(v);
  // 看的是还在跑的那个,内容要跟着长。记录只增不减,按大小变化重取即可;
  // 面板关掉或换了一个就停。
  clearInterval(v.agentTick);
  v.agentTick = setInterval(() => {
    if (v.agentOpen !== a.id || !v.el.isConnected) { clearInterval(v.agentTick); v.agentTick = 0; return; }
    if (hsAgentViewHidden(v)) { v.agentLogOwed = true; return; }
    hsAgentHead(v, a);   // 状态每拍都刷:「多久没动静」是随时间自己走的,不等记录变大
    api(`/session/${encodeURIComponent(v.sid)}/agents`).then((l) => {
      const now = (l || []).find((x) => x.id === a.id);
      if (!now) return;
      // 状态也要跟着更新:面板开着的时候它多半正好跑完,不更新就一直停在「运行中」。
      a.updated = now.updated; a.last = now.last;
      a.phase = now.phase;
      a.status = now.status; a.outcome = now.outcome; a.ended = now.ended;
      if (now.size !== a.size) { a.size = now.size; hsAgentOpen(v, a); }
      else hsAgentHead(v, a);
    }).catch(() => {});
  }, 4000);
  if (hsAgentViewHidden(v)) { v.agentLogOwed = true; return; }
  let msgs = [];
  const mobileLog = typeof isMobileView === "function" && isMobileView();
  try {
    const mobileTail = mobileLog ? "?tail_bytes=262144&limit=60" : "";
    msgs = await api(`/session/${encodeURIComponent(v.sid)}/agents/${encodeURIComponent(a.id)}${mobileTail}`);
  } catch {
    // 轮询偶发失败时保留上一帧；只有首次打开没有任何可看的内容时才显示错误。
    if (v.agentLogSeq !== requestSeq) return;
    if (!same) body.textContent = DeskI18n.t("agent.unreadable");
    return;
  }
  // 同一子代理可能因轮询并发发出两代请求；旧响应不能覆盖更新的一帧。
  if (v.agentOpen !== a.id || v.agentLogSeq !== requestSeq) return;
  v.agentLogOwed = false;
  body.innerHTML = "";
  const prompt = hsAgentPrompt(msgs, a);
  if (prompt) hsAgentPromptTurn(body, prompt.text);
  let totalProcessParts = 0;
  if (mobileLog) for (const m of (msgs || [])) for (const q of (m.parts || [])) {
    if ((q.type === "reasoning" || q.type === "tool") && !hsAgentPromptEcho(q.text || "", prompt)) totalProcessParts++;
  }
  const processSkip = Math.max(0, totalProcessParts - 60);
  if (processSkip) hsAgentNotice(body, `移动端仅展示最近 60 条处理过程，已省略较早 ${processSkip} 条。`);
  let processSeen = 0;
  let turn = null, turnNo = -1;
  const processes = [];
  for (const [mi, m] of (msgs || []).entries()) {
    const role = (m.info || {}).role;
    const parts = (m.parts || []).map((q, pi) => [pi, q]).filter(([pi, q]) => {
      if (prompt && prompt.mi === mi && prompt.pi === pi) return false;
      if ((q.type === "text" || q.type === "reasoning") && hsAgentPromptEcho(q.text || "", prompt)) return false;
      return true;
    });
    if (!parts.length) continue;
    if (role === "user" || !turn) { turn = hsAgentTurn(body); turnNo++; }
    for (const [pi, q] of parts) {
      if (q.type === "text" && (q.text || "").trim()) {
        if (role === "user") {
          const u = document.createElement("div");
          u.className = "hs-u";
          u.innerHTML = '<div class="hs-bubble"><div class="hs-u-body md"></div></div>';
          u.querySelector(".hs-u-body").innerHTML = md.render(q.text).trim();
          turn.el.appendChild(u);
        } else {
          const proc = hsAgentProcess(v, turn, turnNo);
          const d = document.createElement("div");
          d.className = "hs-text md hs-agv-p";
          d.innerHTML = md.render(q.text).trim();
          hsAgentResult(proc, d);
          if (!processes.includes(proc)) processes.push(proc);
        }
      } else if (q.type === "reasoning" && (q.text || "").trim()) {
        if (mobileLog && processSeen++ < processSkip) continue;
        const proc = hsAgentProcess(v, turn, turnNo);
        const d = document.createElement("div");
        d.className = "hs-think hs-agv-r";
        d.innerHTML = '<div class="hs-think-body md"></div>';
        d.firstChild.innerHTML = md.render(q.text).trim();
        proc.body.appendChild(d);
        proc.count++;
        if (!processes.includes(proc)) processes.push(proc);
      } else if (q.type === "tool") {
        if (mobileLog && processSeen++ < processSkip) continue;
        // 子代理接口返回的就是主会话同款 part。复用工具渲染器,不要在这里再把它压扁成
        // 一句摘要；副本避免把 el/sig 之类的前端状态写回接口对象。
        const proc = hsAgentProcess(v, turn, turnNo);
        const part = Object.assign({}, q, { el: null, sig: "" });
        hsPaintTool(v, part, proc.body);
        part.el.classList.add("hs-agv-s");
        const key = String(q.id || `${mi}:${pi}`);
        const head = part.el.querySelector(".hs-step-h");
        const toggle = head.onclick;
        head.onclick = () => {
          toggle();
          if (part.el.classList.contains("open")) v.agentExpanded.add(key);
          else v.agentExpanded.delete(key);
        };
        if (v.agentExpanded.has(key) && hsStepDetailMount(part, v)) part.el.classList.add("open");
        proc.count++;
        if (!processes.includes(proc)) processes.push(proc);
      }
    }
  }
  const running = hsAgentState(v, a).dot === "run";
  processes.forEach((proc, i) => {
    const live = running && i === processes.length - 1;
    const status = proc.el.querySelector(".hs-status");
    status.className = "hs-status " + (live ? "st-working" : "st-done");
    hsStTxt(status.querySelector(".hs-st-t"), live ? "处理中" : "已完成",
            proc.count ? " · " + DeskI18n.t("status.steps", { count: proc.count }, `${proc.count} 步`) : "");
  });
  if (follow) hsSetScrollBottom(body);
  else body.scrollTop = oldTop;
}

function hsAgentClose(v) {
  clearInterval(v.agentTick); v.agentTick = 0;
  v.agentOpen = "";
  v.agentLogSeq = (v.agentLogSeq || 0) + 1;
  v.agentCurrent = null;
  v.agentLogOwed = false;
  const host = v.el.querySelector(".hs-agview");
  if (host) host.remove();
  hsAgentsSync(v);
}
