// 由 app-panel.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 5/5 片
let activeTermId = null;
let _termsList = null;   // /workbench/terminals 缓存:渲染同步进行,避免"清空后 await 回填"造成会话列表闪烁;由轮询 + 变更动作保鲜
let _sessionBucketsCache = null;
function invalidateSessionBuckets() { _sessionBucketsCache = null; }
function sessionBuckets() {
  const mode = `${curEngine || ""}:${shepherdMode ? 1 : 0}`;
  if (_sessionBucketsCache && _sessionBucketsCache.list === _termsList
      && _sessionBucketsCache.mode === mode) return _sessionBucketsCache;
  const byDomain = new Map(), ownedCount = new Map(), loose = [];
  for (const t of _termsList || []) {
    if (!sessionVisibleInCurrentMode(t)) continue;
    const own = t.domain || DESKTOP;
    ownedCount.set(own, (ownedCount.get(own) || 0) + 1);
    const refs = [...new Set((t.refs || []).filter(Boolean))];
    if (own === DESKTOP && !refs.length) loose.push(t);
    for (const dom of new Set([own, ...refs])) {
      if (!byDomain.has(dom)) byDomain.set(dom, []);
      byDomain.get(dom).push(t);
    }
  }
  _sessionBucketsCache = { list: _termsList, mode, byDomain, ownedCount, loose };
  return _sessionBucketsCache;
}
// 同一段 GUI 会话同时有两种稳定身份：侧栏落盘行的引擎锚点(id / resume_id)，以及
// HTML 会话页使用的 Desk 号(session_id, adp_)。选中态、页头与历史回放统一走这组助手，
// 不再让各调用点各自猜哪一种 id 才是当前值。
function termIdentityIds(t) {
  return [...new Set([t && t.id, t && t.resume_id, t && t.session_id]
    .filter(Boolean).map(String))];
}
// 运行中的 Codex 会话可能临时以 tN 暴露，结束后又恢复为持久 resume_id。
// 排序只认持久身份，否则同一会话会被误判为新会话并跳到列表顶部。
function termOrderId(t) {
  return String((t && (t.resume_id || t.id)) || "");
}
function termMatchesId(t, sid) {
  const key = String(sid || "");
  return !!key && termIdentityIds(t).includes(key);
}
function termRowMatchesId(row, sid) {
  const key = String(sid || "");
  if (!key || !row) return false;
  return [row.dataset.sid, row.dataset.resumeId, row.dataset.sessionId, row.title]
    .filter(Boolean).map(String).includes(key);
}
function bindTermRowIdentity(row, t) {
  row.dataset.sid = String(t.id || "");
  row.dataset.orderId = termOrderId(t);
  if (t.resume_id) row.dataset.resumeId = String(t.resume_id);
  if (t.session_id) row.dataset.sessionId = String(t.session_id);
  if (typeof paintTermCompletion === "function") paintTermCompletion(row, t);
}
// 保存读到的回合，而不是等待下一次 busy/idle 清掉一个临时标志。
const _termReadPending = new Map();
function termCompletionKeys(t) {
  return termIdentityIds(t).map(id => JSON.stringify([t.engine || '', id]));
}
function termCompletionRead(t, generation = Number(t.completed_generation ?? t.completion_generation ?? 0)) {
  return termCompletionKeys(t).some(key => _termReadPending.has(key)
    && _termReadPending.get(key) >= generation);
}
function termCompletionUnread(t) {
  return !!t.unread && !t.busy && !t.agents && !t.list_stale && !termCompletionRead(t);
}
function paintTermCompletion(row, t) {
  row._completionSession = t;
  // 只给无代际契约的旧后端保留跳变兼容；新回合有自己的 generation。
  if (!t.completion_generation && !t.list_stale && t.busy)
    termCompletionKeys(t).forEach(key => _termReadPending.delete(key));
  const unread = termCompletionUnread(t);
  row.classList.toggle('unread', unread);
  const stat = row.querySelector('.ti-stat');
  if (stat) { stat.className = 'ti-stat st-' + termState(t); stat.title = termStatTip(t); }
  let marker = row.querySelector('.ti-completed-unread');
  if (!unread) { marker?.remove(); return; }
  if (!marker) {
    marker = document.createElement('span'); marker.className = 'ti-completed-unread';
    marker.setAttribute('role', 'img'); marker.setAttribute('aria-label', '刚完成，未读');
    marker.title = '刚完成，未读';
    row.appendChild(marker);
  }
}
function clearTermCompletion(t, generation) {
  if (generation === undefined)
    generation = Number(t.completed_generation ?? Math.max(0, Number(t.completion_generation || 0) - (t.busy ? 1 : 0)));
  termCompletionKeys(t).forEach(key => _termReadPending.set(key,
    Math.max(_termReadPending.get(key) || 0, generation)));
  document.querySelectorAll('.term-item').forEach(row => {
    const item = row._completionSession;
    if (item && item.engine === t.engine && termIdentityIds(item).some(id => termMatchesId(t, id))) {
      paintTermCompletion(row, item);
    }
  });
}
function termById(sid) {
  return (_termsList || []).find((t) => termMatchesId(t, sid));
}
// 会话标题以侧栏清单为权威源。适配器自己的标题可能为空或仍是默认值「对话」；
// 左栏已经叠加了自动派生标题与用户改名，页头应当复用同一结果。
function _termListedTitle(sid) {
  const t = termById(sid);
  return t ? stripTitleGlyph(t.title || "") : "";
}
function _syncOpenChatTitles() {
  if (typeof pages === "undefined") return;
  for (const t of _termsList || []) {
    const title = stripTitleGlyph(t.title || "");
    if (!title) continue;
    const ids = new Set(termIdentityIds(t));
    const p = pages.find((x) => String(x.url || "").startsWith("chat:")
      && ids.has(String(x.url).slice(5)));
    if (!p || p.title === title) continue;
    p.title = title;
    if (typeof pActive !== "undefined" && pActive === p && typeof setOverlayTitle === "function")
      setOverlayTitle(p);
  }
}
function _applyTermsList(list) {
  _termsList = list || [];
  invalidateSessionBuckets();
  _syncOpenChatTitles();
  const runs = wbState.taskRuns || {};
  let changed = false;
  for (const t of _termsList) {
    if (!t.resume_id || t.resume_id === t.id || !runs[t.id]) continue;
    runs[t.resume_id] = { ...(runs[t.resume_id] || runs[t.id]), resume_id: t.resume_id, live_id: t.id };
    delete runs[t.id];
    changed = true;
  }
  if (changed) {
    wbState.taskRuns = runs;
    saveState({ taskRuns: runs });
  }
}
// 会话清单:同一拍里的并发调用共用一次请求,新鲜的直接复用。
// 启动时这份清单被四个互不相识的函数各取一遍(openResumePicker / renderSessionPanel /
// resumeSession / enterTerminalMode),实测冷启动一次连发 4 条、合计三百多毫秒,而且服务端
// 那一趟要扫三家引擎的落盘(冷扫 1.5 秒)。要新的就传 maxAge: 0。
let _termsFetch = null, _termsAt = 0;
let _termsScopeKey = null, _termsSource = "", _termsVerified = false;
const TERMS_FRESH_MS = 1200;

function _termsScope() {
  return (document.cookie.match(/(?:^|;\s*)cd_backend=([^;]*)/) || [])[1] || "self";
}

function _termsEnsureScope() {
  const scope = _termsScope();
  if (_termsScopeKey !== scope) {
    if (_termsScopeKey !== null) _termReadPending.clear();
    _termsScopeKey = scope; _termsSource = ""; _termsVerified = false;
    _termsList = null; invalidateSessionBuckets(); _termsAt = 0; _termsFetch = null;
  }
  return scope;
}

function _termsNotice(message) {
  const list = document.querySelector("#panel-list");
  if (!list?.parentNode) return;
  let notice = document.querySelector("#session-list-notice");
  if (!notice && message) {
    notice = document.createElement("div"); notice.id = "session-list-notice";
    notice.style.cssText = "padding:8px 16px;font-size:13px;line-height:1.5;color:var(--text-weak);flex-shrink:0";
    notice.innerHTML = '<span role="status"></span> <button type="button">重试</button>';
    notice.querySelector("button").onclick = async () => {
      await refreshTerms({ maxAge: 0 }); _rerenderSessionsHost();
    };
    list.parentNode.insertBefore(notice, list);
  }
  if (notice) {
    notice.hidden = !message;
    const text = notice.querySelector("span");
    if (text.textContent !== message) text.textContent = message;
  }
}

// pre:上次轮询落盘的会话列表。先画旧值,后台预热与正常轮询再对账；冷扫描不挡首页。
function seedTerms(payload, opts = {}) {
  const scope = _termsEnsureScope();
  if (opts.cached) {
    try {
      const cached = payload;
      if (!cached || cached.scope !== scope || !Array.isArray(cached.sessions)) {
        _termsList = []; invalidateSessionBuckets(); return;
      }
      _termsSource = cached.source || "";
      _applyTermsList(cached.sessions.filter(row => row && typeof row.id === "string" && typeof row.engine === "string")
        .map(row => ({ ...row, list_stale: true })));
      _termsAt = 0; // 缓存只能垫底，不能阻止首轮读取。
      // 权威列表仍在后台核验，但已有缓存可正常使用，不把正常读取过程伪装成需处理的错误。
      // 只有请求失败或服务端明确返回不完整列表时，才显示说明与“重试”。
      _termsNotice("");
    } catch { _termsList = []; invalidateSessionBuckets(); }
    return;
  }
  const legacy = Array.isArray(payload);
  const list = legacy ? payload : payload?.sessions;
  if (!Array.isArray(list) || list.some(row => !row || typeof row.id !== "string" || typeof row.engine !== "string")
      || (!legacy && (typeof payload.complete !== "boolean" || typeof payload.source !== "string" || !payload.source)))
    throw new Error("会话列表格式异常");
  if (legacy && _termsVerified) throw new Error("会话列表来源待确认");
  const source = legacy ? "" : payload.source;
  const complete = legacy || payload.complete;
  // 只有同一设备、同一数据目录的缓存才能参与合并。
  const previous = source === _termsSource ? (_termsList || []) : [];
  const rows = list.map(row => ({ ...row }));
  if (!complete) {
    for (const old of previous) {
      const ids = termIdentityIds(old);
      if (!rows.some(row => row.engine === old.engine && termIdentityIds(row).some(id => ids.includes(id))))
        rows.push({ ...old, list_stale: true });
    }
  }
  _termsSource = source;
  _termsVerified = !legacy;
  _applyTermsList(rows);
  _termsAt = Date.now();
  _termsNotice(complete ? "" : "会话列表暂未读取完整，已保留可确认来源的记录。");
  if (!complete) return; // 不完整列表绝不能覆盖上一份完整持久缓存。
  try {
    // 来源与内容单键提交，避免容量不足/中途关闭留下错配的两份键。
    const snapshot = JSON.stringify({ scope, source, sessions: list });
    if (localStorage.getItem("cd_terms_snapshot") !== snapshot) localStorage.setItem("cd_terms_snapshot", snapshot);
    const raw = JSON.stringify(list);
    if (localStorage.getItem("cd_terms") !== raw) localStorage.setItem("cd_terms", raw);
  } catch {}
}

async function refreshTerms(opts) {
  const scope = _termsEnsureScope();
  const maxAge = (opts && opts.maxAge != null) ? opts.maxAge : TERMS_FRESH_MS;
  if (_termsList !== null && Date.now() - _termsAt < maxAge) return _termsList;
  if (_termsFetch) return _termsFetch;          // 已经有一趟在飞,搭它的车
  const pending = Promise.resolve().then(async () => {
    try {
      const result = await api("/workbench/terminals?integrity=1&scope=" + encodeURIComponent(scope));
      if (scope === _termsScope() && _termsFetch === pending) seedTerms(result);
    } catch (error) {
      // 请求失败与返回格式异常都会走到这里；提示文案不区分，原因必须进日志。
      if (typeof deskLog === "function")
        deskLog("[terms] 会话列表刷新失败:" + (error && (error.name + " " + error.message) || error));
      if (scope === _termsScope() && _termsFetch === pending) {
        _applyTermsList((_termsList || []).map(row => ({ ...row, list_stale: true })));
        _termsNotice("暂时无法更新会话列表，已保留上次记录。请检查连接后重试。");
      }
    }
    return _termsList || [];
  });
  _termsFetch = pending;
  try { return await pending; }
  finally { if (_termsFetch === pending) _termsFetch = null; }
}
function taskRunTermId(id, tr) {
  const liveId = tr && tr.live_id;
  if (liveId && (_termsList || []).some((t) => t.id === liveId && t.alive)) return liveId;
  return (tr && tr.resume_id) || id;
}
function watchTaskRunResumeId(id) {
  const started = Date.now();
  const tick = async () => {
    if (!(wbState.taskRuns || {})[id]) return;
    await refreshTerms();
    if (!(wbState.taskRuns || {})[id]) return;   // 已迁移到 resume_id
    if (Date.now() - started < 120000) setTimeout(tick, 1000);
  };
  setTimeout(tick, 1000);
}
// 折叠分组(vibing / coding 项目)的会话数:含休眠会话(可 resume,也算"没丢")。折叠时在标题旁显示,免得以为会话丢了。
function domSessionCount(domId) {
  return sessionBuckets().ownedCount.get(domId) || 0;
}
// 数量紧贴标题，折叠态显隐由 CSS 决定；空组不显示，不另建可点击控件。
function setSidebarFoldCount(head, count, labelSelector, unit) {
  if (!head) return;
  let badge = head.querySelector(":scope > .sidebar-fold-count");
  if (!count) { badge?.remove(); return; }
  if (!badge) {
    const label = head.querySelector(labelSelector);
    if (!label) return;
    badge = document.createElement("span");
    badge.className = "sidebar-fold-count";
    label.after(badge);
  }
  badge.textContent = String(count);
  badge.title = `${count} 个${unit}`;
}

// 数量与展开后的列表同口径：工作流会话含引用，聊天排除有工作流关联的会话。
function refreshFoldCounts() {
  const buckets = sessionBuckets();
  setSidebarFoldCount(document.querySelector("#proj-head"), sideProjects().length, ":scope > span", "工作流");
  setSidebarFoldCount(document.querySelector(".loose-head"), buckets.loose.length, ":scope > span", "会话");
  document.querySelectorAll("details.pfold[data-count-dom]").forEach((det) => {
    const summary = det.querySelector(":scope > summary"); if (!summary) return;
    if (det.classList.contains("pf-work")) {
      summary.querySelector(":scope > .pf-count")?.remove();
      setSidebarFoldCount(summary, (buckets.byDomain.get(det.dataset.countDom) || []).length, ":scope > .pf-name", "会话");
      return;
    }
    const n = domSessionCount(det.dataset.countDom);
    let badge = summary.querySelector(":scope > .pf-count");
    if (n > 0) {
      if (!badge) { badge = document.createElement("span"); badge.className = "pf-count"; summary.insertBefore(badge, summary.querySelector(".pf-btns")); }
      badge.textContent = n;
    } else if (badge) badge.remove();
  });
}
// 任务清单或轻量账本变了(服务端每秒 stat 后推 task.updated,见 task_watch)就重画开着的
// 页面，并原位替换详情里的进展区。原来这些位置各自轮询，详情打开后更是完全不刷。
// 改成事件驱动之后,别的会话里改完约一秒就到,而且不必每一页都定时往服务端问一趟
// ——那一趟经隧道要 0.23~1.9 秒。
function onTasksChanged(did) {
  if (document.hidden) return;   // 看不见就不画:回到前台时 visibilitychange 那条会补上
  if (typeof wfRefreshTaskSheet === "function") wfRefreshTaskSheet(did);
  const body = $("#asset-body");
  if (!body) return;
  if (body.classList.contains("tasks-page")) openTasksPage(true);
}

// 刷新之后把侧栏那一行的高亮找回来。
//
// activeTermId 原来由 restoreTerminal 按 cd_term 恢复,那个函数随 TUI 退场一起删了;
// 后来会话页的恢复补了回来(restorePagesAndBrowser 按 cd_pages 的 open/active),
// 选中态却没人接手——于是刷新之后主区开着某段会话,侧栏里却没有一行是亮的
// (2026-08-25 用户实见)。而且不是只闪一下:侧栏每一轮重渲都按 activeTermId 重建 class,
// 直接给行加 active 会被下一拍冲掉,得让这个变量本身对。
//
// cd_term 由 attachTerminal 一直在写,取回来即可。只在主区确实恢复出会话页时才认:
// 上次看的是工坊或某个应用的话,不该有哪一行亮着。
function restoreActiveTerm() {
  if (typeof overlayOpen !== "undefined" && !overlayOpen) return;   // pActive 只是最近页；收起后它仍有值
  const cur = typeof pActive !== "undefined" && pActive ? String(pActive.url || "") : "";
  if (!cur.startsWith("chat:")) return;
  // 主区当前真正显示的页是唯一权威态。旧实现另读 cd_term；两者一旦漂移，便会把
  // A 页和 B 行同时呈现。按 pActive 对齐，并顺手修正两个会话键。
  const sid = cur.slice(5);
  if (!sid) return;
  activeTermId = sid;
  try {
    localStorage.setItem("cd_term", sid);
    localStorage.setItem("cd_term:" + curEngine, sid);
  } catch {}
  // 侧栏这会儿可能已经画好了(开机那份内联数据种得早),也可能还没。已画的当场上色,
  // 没画的由它自己那一遍渲染按 activeTermId 上色,两条路都覆盖到。
  document.querySelectorAll(".term-item").forEach((x) =>
    x.classList.toggle("active", termRowMatchesId(x, sid)));
}

function refreshFoldCountsEnsure() {   // 冷启动 _termsList 可能还没拉:拉到再刷
  if (_termsList === null) refreshTerms().then(refreshFoldCounts);
  else refreshFoldCounts();
  // 工作流行的任务数徽标:后台补拉各工作流的任务统计,拉齐后再刷一遍
  const wfs = sideProjects().map((p) => p.id);
  if (wfs.some((id) => !_taskStats[id])) fetchTasksBundle(wfs).then(refreshFoldCounts);
}
// 会话状态(左栏圆点):working=工作中(呼吸) / agents=主线闲着但子代理还在跑(同一条呼吸) /
// unread=已完成有未读(实心亮) / idle=已读(实心压暗) / dormant=未运行(最暗)。
// 先看 busy 再看 alive:GUI 模式下进程按回合起,回合跑着的时候 alive 仍是 false,
// 而 busy 是 true。原来 alive 排在前面,这类会话一律判成未运行,行首那颗点从不闪烁——
// 恰恰是最该动的那一档从来没出现过(2026-08-10 对着 /workbench/terminals 的实际返回核的:
// 正在跑的会话是 alive=false、busy=true)。
// agents 排在 alive 之前同理:异步派出的子代理活过派它出来的那个回合,主线答完之后
// busy 转假、GUI 会话的 alive 本来就是假,而后台确实还有活在跑,点该继续闪(接口的
// agents 字段给的是在跑的个数,见 term._term_agents_annotate)。
function termState(s) {
  if (s.list_stale) return "unknown";
  if (s.busy && !(s.completion_generation && termCompletionRead(s, Number(s.completion_generation)))) return "working";
  if (s.agents) return "agents";
  if (termCompletionUnread(s)) return "unread";
  if (!s.alive) return "dormant";
  return "idle";
}
// 圆点的悬浮说明。五档读起来是一套:说的都是「它此刻在什么状态」。
function termStatTip(s) {
  const st = termState(s);
  if (st === "unknown") return "状态待确认，保留上次记录";
  if (st === "working") return "工作中";
  if (st === "agents") return s.agents > 1 ? `子代理工作中，${s.agents} 个` : "子代理工作中";
  if (st === "unread") return "已完成，有未读";
  if (st === "dormant") return "未运行，点开恢复";
  return "空闲";
}
// 会话行首的圆点。原来这里是引擎图标(每引擎一枚),换成统一的圆点:
// 侧栏里一列会话,引擎是哪个由行尾的路径与分支交代得更清楚,行首那个位置该说的是「它在不在动」。
// 三枚形状各异的品牌字形并排时,墨迹轻重不一,反而把状态这层信息盖住了。
// 工作中复用会话视图状态行同一条呼吸(见 style.css 的 ti-blink),两处读起来是同一件事。
function termStatHTML(s) {
  return `<span class="ti-stat st-${termState(s)}" title="${escapeHtml(termStatTip(s))}"><i class="ti-pt"></i></span>`;
}
// claude OSC 标题带前缀状态符号(·/✳ 及 spinner 帧),状态已由图标承载,显示前剥掉。
// 前端也剥一遍(后端落库剥是另一道),保证不依赖后端重启即生效。
// 剥开头"符号+空格"前缀(claude 工作时的 spinner 字形帧逐帧不同,按码位枚举漏;改按类别:
// 1–2 个非字母非数字字符 + 空格,可重复)。要求跟空格,保护 (WIP)/C++ 这类正常开头。
function stripTitleGlyph(t) { return (t || "").replace(/^(?:[^\p{L}\p{N}\s]{1,2}\s+)+/u, "") || t; }
function compactBranchName(b) { const s = (b || "").trim(); return s.includes("/") ? s.split("/").pop() : s; }
function termBranchHTML(t) { return t && t.branch ? `<span class="ti-branch" title="${escapeHtml(t.branch)}">${escapeHtml(compactBranchName(t.branch))}</span>` : ""; }
function termRecordHTML(t) {
  return t.record_status === "unavailable"
    ? '<span class="ti-path ti-record-warning" title="暂时无法读取原生记录，会话入口已保留；记录恢复后此提示自动消失">记录暂不可读</span>' : "";
}
// 就地更新一行的标题与状态(轮询用,避免整列重渲)
function updateTermRow(row, t) {
  bindTermRowIdentity(row, t);   // 活跃临时 id 变化只更新身份，不为此替换整行
  const lab = row.querySelector(".ti-label");
  const title = stripTitleGlyph(t.title);
  if (lab && lab.textContent !== title) lab.textContent = title;
  const warning = row.querySelector(".ti-record-warning");
  if (t.record_status === "unavailable") {
    if (!warning) row.querySelector(".ti-main")?.insertAdjacentHTML("beforeend", termRecordHTML(t));
  } else if (warning) warning.remove();
  const br = row.querySelector(".ti-branch");
  const shortBranch = compactBranchName(t.branch);
  if (shortBranch) {
    if (br) { br.textContent = shortBranch; br.title = t.branch; }
    else row.querySelector(".ti-main")?.insertAdjacentHTML("beforeend", termBranchHTML(t));
  } else if (br) br.remove();
  const stEl = row.querySelector(".ti-stat");
  if (stEl) {
    const cls = "ti-stat st-" + termState(t);   // 含 dormant:圆点要区分「未运行」与「空闲」,口径与首渲一致
    if (stEl.className !== cls) stEl.className = cls;
    const tip = termStatTip(t);   // 说明也随轮询更新:子代理那一档带着个数,个数会变
    if (stEl.title !== tip) stEl.title = tip;
  }
}
async function renderTermSessions(container, gen, domainId, noPoll, aliasDom) {
  // 会话=终端:列出指定工作区(默认当前)的常驻活终端,点击切换,管理操作保留在右键菜单。
  const dom = domainId || curDomainId();
  if (_termsList === null) {   // 仅冷启动拉一次;之后同步从缓存渲染,无 await 空窗
    await refreshTerms();
    if (gen !== undefined && gen !== _panelGen) return;   // 已有更新的渲染覆盖,丢弃这次(避免重复 append)
  }
  // aliasDom:编码子区兼容尚未重启、内存域仍是老式「容器:子区」的活会话
  const _items = _termsList.filter((t) => sessionVisibleInCurrentMode(t) && ((t.domain || DESKTOP) === dom || (aliasDom && (t.domain || "") === aliasDom)));
  const list = applyOrder(_items, _pinSessionOrder(dom, _items), termOrderId);   // 同 visibleSessionsFor:次序钉住,不随活动重排
  // 清空重渲:本函数会被同一个 container 反复调用(子区折叠反复展开/轮询集合变化触发),不清则旧行后面追加越来越多。
  // 只摘会话行与空态,不能 innerHTML=""——面板折叠传进来的 container 是 <details> 本身,整清会把 <summary> 一起抹掉,
  // 浏览器回退显示默认的「Details」标题。
  container.querySelectorAll(".term-item, .group-empty").forEach((x) => x.remove());
  if (!list.length) {
    const e = document.createElement("div"); e.className = "group-empty";
    e.textContent = "还没有会话，点 + 开一个"; container.appendChild(e); return;
  }
  for (const s of list) {
    const r = document.createElement("div");
    const dormant = !s.alive;   // 已落盘但无活进程:点开即 --resume 拉活
    r.className = "session-item term-item" + (termMatchesId(s, activeTermId) ? " active" : "") + (dormant ? " dormant" : "") + (s.unread && !s.busy ? " unread" : "");
    // 标题后标出会话所在目录:项目实体退役后,来源靠路径分辨,不再靠分组
  r.innerHTML = `${termStatHTML(s)}<span class="ti-main"><span class="ti-label">${escapeHtml(stripTitleGlyph(s.title))}</span>${termPathHTML(s, domId)}${termBranchHTML(s)}${termRecordHTML(s)}</span>`;
    r.title = s.id;
    bindTermRowIdentity(r, s);
    r.onclick = () => {   // 活=附着,休眠=恢复(WS 端按 uuid 自动 --resume)
      clearTermCompletion(s);
      attachTerminal(s.id, s.engine, { domain: dom, cwd: s.cwd || "" });
      // 只切高亮,不调 renderSessionPanel(它会清空整列再异步重建,导致会话闪烁消失再出现)
      document.querySelectorAll(".term-item").forEach((x) => x.classList.toggle("active", termRowMatchesId(x, s.id)));
      r.classList.remove("dormant"); r.querySelector(".ti-dot")?.remove();   // 休眠会话点开即拉活,去掉休眠标记
    };
    r.oncontextmenu = (ev) => {   // 右键=改标题菜单(引擎无关,自定义名优先于自动派生)
      ev.preventDefault(); ev.stopPropagation();
      openTermCtxMenu(ev.clientX, ev.clientY, s, r);
    };
    container.appendChild(r);
  }
  makeReorderable(container, ".term-item", (el) => el.dataset.orderId || el.title, (order) => {   // 长按拖拽调会话次序:coding 项目内 / vibing 工作区内,逻辑一致(order 存 sessionOrder[dom])
    wbState.sessionOrder = wbState.sessionOrder || {};
    const merged = mergeOrderedSubset(wbState.sessionOrder[dom], order);
    wbState.sessionOrder[dom] = merged;
    saveState({ sessionOrder: { [dom]: merged } });
  });
  if (!noPoll) startTermPoll();   // 标题随 claude/codex 改名刷新
}
// 会话增删改后重渲"当前会话宿主":子区工作面时会话列在主区 asset-view(sf-sess),要重渲工作面;
// 否则(desktop/职能工作区/coding)会话列在面板,重渲面板。renderTermSessions 的改名/删除都走它。
function _rerenderSessionsHost() {
  renderSessionPanel();   // 重构后会话只列在侧栏项目树,重渲面板即可(子区工作面已退役)
}
// 会话行右键菜单:重命名 / 移动到其他工作区·项目 / 关闭
// 会话信息块:标题与时间一行,归属目录与分支各一行。原来这是悬停卡,单独浮在行侧——
// 与右键菜单是两个浮层、两套触发方式,而它们讲的是同一段会话。合成一个:信息在上,
// 一条发丝线之下是能做的事。目录那一行仍可点,点开在标签页里打开它。
function termInfoNode(s, domId) {
  const box = document.createElement("div");
  box.className = "rp-info";
  const dirBox = document.createElement("div");
  dirBox.innerHTML = termPathHTML(s, domId);
  // termPathHTML 在「聊天」这一组(归属是桌面)时按设计不出目录:那一列里每行的 cwd 都一样,
  // 标出来是重复的。但这一块讲的正是「这段会话落在哪个目录」,那时退回 cwd 的末段。
  const dir = (dirBox.textContent || "").trim()
    || ((s.cwd || "").replace(/\/+$/, "").split("/").pop() || "");
  const branch = compactBranchName(s.branch);
  // 不再重复会话名:右键的那一行就在眼前,菜单里再写一遍是同一句话说两遍。
  // 留下的是这一行看不出来的两件事——它落在哪个目录、在哪条分支上。
  box.innerHTML =
    (dir ? `<div class="tc-r tc-dir" title="在标签页里打开这个目录"><span class="ico-folder-open"></span>`
           + `<span>${escapeHtml(dir)}</span></div>` : "")
    + (branch ? `<div class="tc-r">${ICONS.branch}<span>${escapeHtml(branch)}</span></div>` : "");
  const d = box.querySelector(".tc-dir");
  if (d && s.cwd) {
    d.onclick = (ev) => {
      ev.stopPropagation();
      document.querySelector(".rp-ctx")?.remove();
      openFilesPage({ id: "abs:" + s.cwd, name: s.cwd.split("/").pop() || s.cwd });
    };
  }
  return box;
}

const RP_RENAME_ICO = '<span class="menu-icon-mask is-rename" aria-hidden="true"></span>';
const RP_TRASH_ICO = '<span class="menu-icon-mask is-delete" aria-hidden="true"></span>';
const RP_MOVE_ICO = '<span class="menu-icon-mask is-move" aria-hidden="true"></span>';

function openTermCtxMenu(x, y, s, row, domId) {
  const items = [{ node: termInfoNode(s, domId || s.domain || DESKTOP) },
                 { label: DeskI18n.t("menu.rename"), ico: RP_RENAME_ICO,
                   keepDrawer: true, fn: () => renameTermSession(s, row) }];
  if (s.engine !== "shell" && !s.task) items.push({ label: DeskI18n.t("menu.moveTo"), ico: RP_MOVE_ICO, fn: () => {
    const curDom = s.domain || DESKTOP;
    const opts = [];
    for (const d of domains) {   // 只列普通工作流 + 项目(容器/管理器/专项不收会话)
      if (d.id === curDom || d.kind || d.id === DESKTOP) continue;
      opts.push({ label: DeskI18n.t("menu.moveWorkflow", { name: d.name }), fn: () => moveTermSession(s, d.id) });
    }
    if (curDom !== DESKTOP) opts.push({ label: DeskI18n.t("menu.moveChat"), fn: () => moveTermSession(s, DESKTOP) });
    if (opts.length) openCtxMenu(x, y, opts);
  } });
  // 「有没有进程」与「要不要删记录」是两件事,原来挤在同一项里,于是同样在用的会话,
  // 带 PTY 的显示「关闭」、GUI 那些(进程按回合起,平时没有常驻 PTY)显示「删除」,
  // 看起来像按引擎给了不同的菜单。拆成两项:有进程才多一项「关闭进程」,
  // 「删除会话」始终在,做的事到哪儿都一样。
  // 「关闭进程」两种来源:终端会话有 PTY(alive),GUI 会话握着一条跨回合的常驻引擎进程
  // (resident)。两者点下去都是放掉进程、记录不动,只是走的通道不同。
  if (s.alive) items.push({ label: DeskI18n.t("menu.closeProcess"), ico: ICONS.close,
                            fn: () => closeTermSession(s, false) });
  else if (s.resident) items.push({ label: DeskI18n.t("menu.closeProcess"), ico: ICONS.close,
                                    fn: () => releaseTermResident(s) });
  items.push({ label: DeskI18n.t("menu.deleteConversation"), danger: true, ico: RP_TRASH_ICO,
               fn: () => closeTermSession(s, true) });
  openCtxMenu(x, y, items);
}
// 放掉 GUI 会话的常驻引擎进程:记录一个字节都不动,下一回合冷起一次。
// 用处是让引擎从干净状态重来,或者内存吃紧时手动放几条。
async function releaseTermResident(s) {
  const sid = s.resume_id || s.id;
  try {
    await api(`/workbench/terminals/${encodeURIComponent(sid)}/release`, { method: "POST" });
  } catch (e) { alert("关闭失败:" + e.message); return; }
  await refreshTerms();
  _rerenderSessionsHost();
}

// 移动会话:meta 换域 + pinned(后端),活 claude 空闲时后端顺手注入 /cd;其他引擎下次重开时 cwd 才切
async function moveTermSession(s, domId) {
  const sid = s.resume_id || s.id;
  let r = null;
  try {
    r = await api(`/workbench/terminals/${encodeURIComponent(sid)}/domain`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ domain: domId }) });
  } catch (e) { alert("移动失败:" + e.message); return; }
  if (r && r.error) { alert("移动失败:" + r.error); return; }
  await refreshTerms();
  _rerenderSessionsHost();
}
// 光标处右键菜单骨架:items=[{label, fn, danger, checked, keepDrawer}]。点外/再次右键关闭;
// 会话行、工作区行、专项行共用。keepDrawer 给手机抽屉里的就地编辑动作使用：菜单虽然挂在
// body，点下去也不能被当成“点了抽屉外部”。
// checked 用来标"当前选中的那一档":勾单独占一个元素排在行尾,不拼进 label——拼前缀会把选中项
// 的名字推右两个字符,和其它项对不齐。勾的形态与配色照抄引擎菜单的 .fm-check(#30d158)。
// 不传 checked 的调用方(绝大多数)渲染结果与从前完全一致。
// 勾用描边的 SVG,不用 ✓ 字形:字形随字体走(粗细、倾斜、基线各不相同),
// 而这里要的是系统菜单那种细笔画的对勾。颜色取系统强调蓝(截图采样 #2a79d7)。
const RP_CHEV = '<span class="rp-chev" aria-hidden="true"></span>';
// 选中态用一枚实心圆点,不用勾。勾是「做完了/对了」的意思,而这里表达的是「当前在这一档」;
// 一列灰字里那道蓝勾也过于抢眼,圆点安静得多。
const RP_CHECK = '<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor"><circle cx="12" cy="12" r="5.6"/></svg>';
// cls:给弹层加一个附加类。右键菜单贴着内容宽就好,输入框那几个(模式/模型/思考强度)
// 要定宽——它们是同一处反复开关的一组,宽度跟着最长那一行走的话,来回切会一跳一跳。
function openCtxMenu(x, y, items, cls) {
  if (openCtxMenu.close) openCtxMenu.close();
  document.querySelector(".rp-ctx")?.remove();
  const m = document.createElement("div");
  m.className = "rp-ctx" + (cls ? " " + cls : "");
  const modelList = m.classList.contains("rp-model") ? document.createElement("div") : null;
  if (modelList) {
    modelList.className = "rp-model-list";
    m.appendChild(modelList);
  }
  // 有勾选项时,每一行都留出勾的位置(未选的留空)。只给选中那行加一个 span 的话,
  // 菜单宽度由「最长文字 + 勾」决定,那一行正好占满、没有余量,勾就贴在文字后面而不是右缘。
  const hasCheck = items.some((x) => x.checked !== undefined);
  const labelNodes = new Map();
  // 上一行是不是分组标题。标题本身就把这一组开了口,紧跟它的第一条动作不再画分隔线——
  // 那条线画在标题与它所标注的内容之间,把两者切开,读起来像标题属于上一组。
  let afterHeader = false;
  let modelGroup = null;
  let selectedModelRow = null;
  for (const it of items) {
    if (it.node) { m.appendChild(it.node); afterHeader = false; continue; }   // 整块自定义内容(如会话信息)
    // 普通标题只读；模型组标题可折叠，并用独立容器限定吸顶范围，不能盖住下一组。
    if (it.header) {
      const h = document.createElement("div");
      h.className = "rp-row rp-ctx-head" + (it.sep ? " rp-sep" : "");
      h.innerHTML = `<span class="rp-title">${escapeHtml(it.header)}</span>`
        + (it.value ? `<span class="rp-value">${escapeHtml(it.value)}</span>` : "");
      if (modelList && it.modelRow && it.modelGroup) {
        const group = document.createElement("div");
        group.className = "rp-model-group";
        modelList.appendChild(group);
        modelGroup = group;
        group.appendChild(h);
        h.classList.add("rp-model-group-toggle");
        h.setAttribute("role", "button");
        h.tabIndex = 0;
        let collapsed = !!it.collapsed;
        group.classList.toggle("is-collapsed", collapsed);
        h.setAttribute("aria-expanded", String(!collapsed));
        const toggle = () => {
          const top = h.getBoundingClientRect().top;
          collapsed = !collapsed;
          group.classList.toggle("is-collapsed", collapsed);
          h.setAttribute("aria-expanded", String(!collapsed));
          it.onCollapse?.(collapsed);
          reflow();
          // 折叠吸顶中的长组后，尽量让刚点击的标题仍留在原位置。
          modelList.scrollTop += h.getBoundingClientRect().top - top;
        };
        h.onclick = toggle;
        h.onkeydown = (e) => {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
        };
      } else {
        (modelList && it.modelRow ? modelList : m).appendChild(h);
        modelGroup = null;
      }
      afterHeader = true;
      continue;
    }
    const r = document.createElement("div");
    r.className = "rp-row" + (it.danger ? " rp-ctx-del" : "");
    // disabled:列出来但不可选(如未安装的引擎)。此前调用方传了这个字段,这里却没读——
    // 行看着能点,点了没反应,全靠 fn 自己守门。现在置灰、不吃 hover、不触发 fn。
    if (it.disabled) { r.classList.add("rp-disabled"); r.setAttribute("aria-disabled", "true"); }
    if (it.keepDrawer) r.classList.add("rp-drawer-keep");
    // value:右侧的当前取值(灰);chev:进二级菜单的记号;sep:上方一条发丝线分组。
    // 三者都可选,老调用方不传就还是原来的两列。
    if (it.sep && !afterHeader) r.classList.add("rp-sep");
    afterHeader = false;
    if (it.chev) r.classList.add("rp-chevrow");   // 记号那一格改成贴着字形,见 style.css
    if (it.ico) r.classList.add("rp-hasico");
    r.innerHTML = (it.ico ? `<span class="rp-ico">${it.ico}</span>` : "")
      + `<span class="rp-title">${escapeHtml(it.label)}</span>`
      + (it.value ? `<span class="rp-value">${escapeHtml(it.value)}</span>` : "")
      // 有勾选列时,进二级菜单的记号占勾的那一格,不再另起一列:另起一列的话,取值前面会
      // 空出一个没人用的勾位,而记号本身又跑到勾的右边,和上面那些勾对不齐。
      + (hasCheck ? `<span class="rp-check">${it.chev ? RP_CHEV : (it.checked ? RP_CHECK : "")}</span>` : "")
      + (!hasCheck && it.chev ? RP_CHEV : "");
    if (it.id != null) labelNodes.set(it.id, r.querySelector(".rp-title"));
    r.onclick = () => { if (it.disabled) return; drop(); it.fn(); };
    (modelList && it.modelRow ? (modelGroup || modelList) : m).appendChild(r);
    if (it.modelRow && it.checked) selectedModelRow = r;
  }
  document.body.appendChild(m);
  // 先限制尺寸再测量定位。只夹坐标不能容纳超长模型列表；键盘打开时按可视视口计算。
  const viewport = window.visualViewport;
  const bounds = () => ({
    left: (viewport?.offsetLeft || 0) + 8,
    top: (viewport?.offsetTop || 0) + 8,
    width: Math.max(0, (viewport?.width || window.innerWidth) - 16),
    height: Math.max(0, (viewport?.height || window.innerHeight) - 16),
  });
  const fit = () => {
    const b = bounds();
    m.style.maxWidth = b.width + "px";
    // 先给固定参数区留空间，再将模型滚动区限制为最多十行。
    if (modelList) {
      const style = getComputedStyle(m);
      const chrome = [style.paddingTop, style.paddingBottom, style.borderTopWidth,
        style.borderBottomWidth].reduce((sum, value) => sum + (parseFloat(value) || 0), 0);
      const heightOf = (row) => row.getBoundingClientRect().height
        + (parseFloat(getComputedStyle(row).marginTop) || 0)
        + (parseFloat(getComputedStyle(row).marginBottom) || 0);
      const fixed = Array.from(m.children).filter((row) => row !== modelList)
        .reduce((sum, row) => sum + heightOf(row), chrome);
      const rowsHeight = Array.from(modelList.querySelectorAll(".rp-row"))
        .filter((row) => row.getClientRects().length).slice(0, 10)
        .reduce((sum, row) => sum + heightOf(row), 0);
      modelList.style.maxHeight = Math.max(0, Math.min(rowsHeight, b.height - fixed)) + "px";
    }
    m.style.maxHeight = b.height + "px";
    return b;
  };
  const place = (px, py) => {
    const b = fit();
    m.style.top = Math.max(b.top, Math.min(py, b.top + b.height - m.offsetHeight)) + "px";
    m.style.left = Math.max(b.left, Math.min(px, b.left + b.width - m.offsetWidth)) + "px";
  };
  place(x, y);
  // 手机上还要再摆一次。触发这些菜单的按钮多半在输入框那一行,点它时键盘正开着;
  // 菜单一冒出来焦点离开输入框、键盘收起,可视高度陡增几百像素——按弹出那一刻的坐标
  // 算出来的位置,收完键盘就落在屏幕中部,离触发它的按钮很远(2026-08-17 手机实见)。
  // 所以记下锚点元素,等视口稳定后按它的实际位置重摆:菜单永远贴着按钮出现。
  const anchor = (typeof x === "object" && x) ? null : (openCtxMenu._anchor || null);
  openCtxMenu._anchor = null;
  const reflow = () => {
    if (!m.isConnected) return;
    const b = fit();
    if (!anchor?.isConnected) {
      place(x, y);
    } else {
      const r = anchor.getBoundingClientRect();
      // 优先摆在按钮上方(输入区在屏幕底部,往下没有空间);上方放不下再摆下方。
      const top = r.top - m.offsetHeight - 6;
      place(r.left, top >= b.top ? top : r.bottom + 6);
    }
    modelList?.dispatchEvent(new CustomEvent("desk-scrollbar-refresh", { bubbles: true }));
  };
  reflow();
  if (modelList && selectedModelRow) {
    // 已折叠的当前模型定位到其标题；展开组则保留当前模型居中行为。
    const group = selectedModelRow.closest(".rp-model-group");
    const selected = group?.classList.contains("is-collapsed") ? group.firstElementChild : selectedModelRow;
    modelList.scrollTop = selected.getBoundingClientRect().top
      - modelList.getBoundingClientRect().top - modelList.clientHeight / 2 + selected.offsetHeight / 2;
  }
  if (anchor && anchor.isConnected) {
    setTimeout(reflow, 60);      // 键盘收起有动画,一次不够
    setTimeout(reflow, 260);
  }
  window.addEventListener("resize", reflow);
  viewport?.addEventListener("resize", reflow);
  viewport?.addEventListener("scroll", reflow);
  const drop = () => {
    window.removeEventListener("keydown", menuKey, true);
    window.removeEventListener("resize", reflow);
    viewport?.removeEventListener("resize", reflow);
    viewport?.removeEventListener("scroll", reflow);
    m.remove();
    document.removeEventListener("mousedown", close);
    document.removeEventListener("pointerdown", close);
    document.removeEventListener("contextmenu", close);
  };
  // pointerdown 一并监听:触屏上不发 mousedown(点在菜单外只会收到 touch/pointer 事件),
  // 于是菜单弹出来就再也关不掉——手机上这几个档位菜单正是这样(2026-08-17 实见)。
  const close = (e) => {
    // 覆盖式滑块挂在 body，拖动本菜单的滑块不应被当成点击菜单外部。
    const thumb = e.target.closest?.(".sb-thumb");
    if (!m.contains(e.target) && !(thumb?._key && m.contains(thumb._key))) drop();
  };
  // 在 window 捕获阶段接管 Escape，早于 document 上的会话中断监听。
  const menuKey = (e) => {
    if (e.key !== "Escape" || !m.isConnected) return;
    e.preventDefault();
    e.stopPropagation();
    const restoreFocus = m.contains(document.activeElement);
    drop();
    if (restoreFocus && anchor?.isConnected) anchor.focus({ preventScroll: true });
  };
  window.addEventListener("keydown", menuKey, true);
  setTimeout(() => {
    if (!m.isConnected) return;
    document.addEventListener("mousedown", close);
    document.addEventListener("pointerdown", close);
    document.addEventListener("contextmenu", close);
  }, 0);
  // 长按弹出的菜单,手指没抬就被 bindLongPressMenu 判定成想拖拽:那边没有 m/drop 的
  // 引用,只能靠这个口子主动收掉当前菜单。每次 openCtxMenu 重新指向最新这次的 drop。
  openCtxMenu.close = drop;
  // 异步元数据只更新所属菜单的标签，保留节点、选中项和用户滚动位置。
  return {
    isOpen: () => m.isConnected,
    setLabels: (labels) => {
      if (!m.isConnected) return;
      let changed = false;
      for (const [id, label] of labels) {
        const node = labelNodes.get(id);
        if (node && node.textContent !== label) { node.textContent = label; changed = true; }
      }
      if (changed) reflow();
    },
  };
}
// 给下一次 openCtxMenu 指定锚点元素(手机上按它重摆位置,见上)。
// 单独一个入口而不是加参数:调用点有二十多处,只有档位那几处需要它。
openCtxMenu.anchorNext = (el) => { openCtxMenu._anchor = el || null; };
// 重命名一个域(职能工作区/专项容器)。容器走 saveContainer(整条回写,顺带重生成说明标题);
// 工作区走 /workbench/domains/<id>/rename(运行态改 name;分发版工作区落最小覆盖条目,只盖显示名)。
async function renameDomain(d) {
  let v = ""; try { v = (window.prompt(`重命名「${d.name}」`, d.name) || "").trim(); } catch {}
  if (!v || v === d.name) return;
  {
    try {
      await api(`/workbench/domains/${encodeURIComponent(d.id)}/rename`, { method: "POST",
        headers: { "content-type": "application/json" }, body: JSON.stringify({ name: v }) });
    } catch (e) { alert("重命名失败：" + e.message); return; }
    await reloadDomains(); renderSessionPanel();
  }
  // 正位于该域(或其子区)时刷新面板标题;desktop 工作区列表由上面的重渲覆盖
  if (nav.type === "domain" && (nav.id === d.id || String(nav.id).startsWith(d.id + ":"))) selectNav("domain", nav.id);
}
// 给一行挂"右键→重命名域"。d 可以是域对象,或延迟取值的函数(rail 按钮建行时 domains 可能还没就绪)。
function wireDomainCtx(el, d) {
  el.oncontextmenu = (ev) => {
    ev.preventDefault(); ev.stopPropagation();
    const dom = typeof d === "function" ? d() : d;
    if (dom) openCtxMenu(ev.clientX, ev.clientY, [{ ico: ICONS.pencil, label: DeskI18n.t("menu.rename"), fn: () => renameDomain(dom) }]);
  };
}
// 就地编辑会话标题:Enter / 失焦保存,Esc 取消;清空=回落自动派生标题。key 用稳定引擎 uuid(resume_id)兜底内部 id。
async function renameTermSession(s, row) {
  const label = row.querySelector(".ti-label");
  if (!label || row.querySelector(".ti-rename")) return;
  const old = stripTitleGlyph(s.title);
  const input = document.createElement("input");
  input.className = "ti-rename"; input.value = old;
  input.onclick = (e) => e.stopPropagation();
  label.replaceWith(input);
  input.focus(); input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return; done = true;
    const val = input.value.trim();
    if (!save || val === old) {   // 取消或没改:原样还原标签,不打 API
      const lab = document.createElement("span"); lab.className = "ti-label";
      lab.textContent = old; input.replaceWith(lab); return;
    }
    try {
      const result = await api(`/workbench/terminals/${encodeURIComponent(s.resume_id || s.id)}/title`,
        { method: "POST", body: JSON.stringify({ title: val }) });
      if (result?.error) throw new Error(result.error);
    } catch (e) { alert("重命名失败：" + e.message); }
    await refreshTerms(); _rerenderSessionsHost();   // 重渲取后端权威标题(自定义名 / 清空后回落);子区工作面重渲主区、否则面板
  };
  input.onkeydown = (e) => {
    if (e.isComposing || e.keyCode === 229) return;   // 输入法合成态的回车=选字,不是提交
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  };
  input.onblur = () => finish(true);
}
let _termPoll = null;
function startTermPoll() {
  // 重构后全局单模式:侧栏项目树常驻,轮询遍历「展开的项目折叠 + 无项目区」,
  // 每块独立比较「应显示集」(visibleSessionsFor/looseSessions,与渲染同口径),
  // 变了只重渲该块,否则逐行 updateTermRow 就地刷新——不再按 nav 分派两套模式。
  if (_termPoll) return;
  const tick = async () => {
    if (document.hidden) return;   // 页面在后台:看不见的东西不必保鲜,而经隧道时每一轮都是真往返
    if (document.body.classList.contains("reorder-active")
        || document.body.classList.contains("reorder-pending")) return;   // 从长按候选起就不重渲
    if (document.querySelector(".ti-rename")) return;   // 就地改标题中:暂停,免冲掉输入框
    let full;
    full = await refreshTerms();
    // 请求发出后才按下也要拦：不能让已经在途的轮询越过上面的起点检查。
    if (document.hidden || document.querySelector(".ti-rename")
        || document.body.classList.contains("reorder-active")
        || document.body.classList.contains("reorder-pending")) return;
    _applyTermsList(full);   // 保鲜缓存 + task 执行记录从 live id 迁移到真实 resume id
    invalidateSessionBuckets(); // 测试与旧调用方可能覆写 _applyTermsList；轮询边界仍显式换代
    refreshFoldCounts();     // 项目行会话数徽标保鲜(含折叠的项目)
    const byOrderId = new Map(full.map((t) => [termOrderId(t), t]));
    // 侧栏每个展开的工作流:会话块同口径保鲜,集合变了重渲,否则逐行就地刷新
    for (const host of document.querySelectorAll("details.pf-work[open] .pf-sess")) {
      const domId = host.closest("details.pf-work").dataset.pid;
      const rows = [...host.querySelectorAll(".term-item")];
      const want = visibleSessionsFor(domId).shown.map(termOrderId).sort().join();
      const have = rows.map((r) => r.dataset.orderId || r.dataset.sid || r.title).sort().join();
      if (want !== have) renderProjSessions(host, domId);
      else for (const r of rows) { const t = byOrderId.get(r.dataset.orderId || r.dataset.sid || r.title); if (t) updateTermRow(r, t); }
    }
    for (const loose of document.querySelectorAll("#panel-list .loose-sessions")) {
      const rows = [...loose.querySelectorAll(".term-item")];
      const want = looseSessions().map(termOrderId).sort().join();
      const have = rows.map((r) => r.dataset.orderId || r.dataset.sid || r.title).sort().join();
      if (want !== have) renderLooseSessions(loose);
      else for (const r of rows) { const t = byOrderId.get(r.dataset.orderId || r.dataset.sid || r.title); if (t) updateTermRow(r, t); }
    }
    // 主区终端头:未初始化(newTerminal/恢复路径不经行点击)则补建,已建则标题保鲜。
    // 欢迎页在场时不补建:切引擎重载后,恢复流程先把上一引擎的会话页建出来(activeTermId 随之指向它),
    // 再按本引擎快照落回欢迎页;这里若补建,会把那段会话的标题挂到欢迎页上,并经 setTermHead
    // 摘掉 welcome-on,欢迎页退回 1040px 栏宽,两侧露出深一档的底色(2026-11-02 用户实见:
    // Shepherd 切到 Pi 后欢迎页两边灰边)。
    const welcomeShown = typeof _isWelcomeVisible === "function" && _isWelcomeVisible();
    if (activeTermId && !welcomeShown) {
      const t = termById(activeTermId);
      if (t) {
        if (_termHeadSid !== t.id) setTermHead(t);
        else {
          const tt = $("#term-title");
          const v = stripTitleGlyph(t.title) || t.id;
          if (tt && tt.textContent !== v) tt.textContent = v;
          try { renderTermToc(t); } catch {}   // 刻度条自带签名比对,内容没变不会重建
        }
      }
    }
    applySideFilter();   // 搜索态下新渲染的行重放过滤
  };
  // 首屏缓存的状态待确认，不能等首个 1.5/5 秒定时点才恢复工作中闪烁。
  // 立即取数并在返回时就地更新行；refreshTerms 会复用其他启动请求，不重复扫描。
  // 后续仍保持原兜底节律，避免手机经隧道持续请求过密。
  _termPoll = setInterval(tick, isLocalView() ? 1500 : 5000);
  void tick();
}

// 应用陈列（单行，极简）：名字 + 极简类型标记（闪电=agentic 跑 agent；方块=纯页面）。
// 描述移到 hover。只分 agentic / 非，不预设"观测/工具"等类型（asset-model 不预设产出形状）。
// 应用使用次数（被依赖证据，反馈机制第二层）：agentic 取真实执行记录数，非 agentic 取打开计数
// 使用次数=被依赖证据，只对"有效使用动作"计数。agentic 的有效动作=执行（宿主可见，
// 取 taskRuns）；non-agentic 页面的真实使用发生在 iframe 内（填参/计算/拉数），宿主观测不到，
// 打开≠用过，故暂不计数。待 make-app 规范页面经 postMessage 自报"used"后再计（设计见 docs）。
function appUseCount(a, domainId) {
  if (!a.agentic) return null;
  const tr = wbState.taskRuns || {};
  // 持久计数:执行次数是 app 自身的被依赖证据,不随当前引擎会话列表起伏。
  // 直接数 taskRuns(删除会话时已同步清对应记录,见删除路径),故跨引擎切换不归零。
  return Object.values(tr).filter((r) => r.domain === domainId && r.taskId === a.id).length;
}
// 老式显示器:交互与托管统一的那枚图标。应用列表里绝大多数是自造的小工具,统一形制比各显
// 神通好读——名字才是区分它们的东西。
const APP_MARK = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="3.5" width="19" height="14" rx="2"/><rect x="5.5" y="6.5" width="13" height="8" rx="1"/><path d="M9 20.5h6"/><path d="M12 17.5v3"/></svg>';

// 例外留给有既定形象的那几个:icon 字段填成站内图片路径时改用图片。终端是第一个——它不是
// 谁写的小工具,而是一样人人认得的东西,一眼认出来比风格统一更有用。
// 只认站内绝对路径,不认外链:图标随 payload 发行,不该在启动时依赖网络。
function appMark(a) {
  const ic = String((a && a.icon) || "");
  return /^\/[\w./-]+\.(png|jpe?g|svg|webp)$/i.test(ic)
    ? `<img class="ai-img" src="${escapeHtml(ic)}" alt="">` : APP_MARK;
}

function appRow(a, domainId, projectDomain) {
  const d = document.createElement("div");
  d.className = "app-item";
  d.title = (a.desc ? `${a.label} — ${a.desc}` : a.label) + (a.uid ? `\nuid: ${a.uid}` : "");
  const mark = appMark(a);
  const n = appUseCount(a, domainId);
  d.innerHTML = `<span class="ai-mark">${mark}</span><div class="ai-text"><span class="ai-label">${escapeHtml(a.label)}</span>`
    + (a.desc ? `<span class="ai-desc">${escapeHtml(a.desc)}</span>` : "") + `</div>`
    + (n ? `<span class="ai-count" title="执行 ${n} 次">${n}</span>` : "");
  // 终端:后端起一个真的 PTY,前端用 xterm 挂上去。它不是一张 HTML 页,所以不走 iframe 那条路。
  // 做成应用是为了给它一个入口——「+」改成直接开地址行之后,原来那个菜单里的「打开终端」没了着落。
  if (a.page === "shell")
    d.onclick = () => openTerminalApp();   // 多个 shell 是这个应用的内部标签(app-terminal.js),开着就切回去
  else if (!a.agentic)
    d.onclick = () => openPage(pageUrl(a, domainId, projectDomain), a.label, { app: true, appid: a.uid || a.id });   // 打开≠用过，不计数
  else d.onclick = () => openAgenticApp(a, domainId);   // agentic 计数=执行记录数，开详情页不计
  return d;
}

// agentic 应用详情页：不直接执行，先展示描述/执行记录/执行钮；点执行才发起，运行过程在本页可折叠展示
function openAgenticApp(a, domainId) {
  const body = $("#overlay-body");
  pActive = null;                       // 详情页不是 iframe 页
  pages.forEach((x) => { x.el.style.display = "none"; });
  document.querySelectorAll("#overlay-body > .app-detail").forEach((x) => x.remove());
  $("#overlay-title").innerHTML = escapeHtml(a.label);   // uid 不上页头,同 setOverlayTitle
  $("#ov-external").hidden = true;   // 应用页本地 iframe,无需外部浏览器出口
  overlayOpen = true; $("#overlay").classList.add("open");
  $("#overlay").classList.add("app-page");   // agentic 详情页同样给 收起/关闭

  // 两种声明：5W 声明式（who/what/how/when/where + params 表单）或旧式单句 prompt（兼容）
  const isDecl = !!(a.who || a.how);
  const when = a.when || a.trigger || "manual";
  const det = document.createElement("div");
  det.className = "app-detail";
  let middle;
  if (isDecl) {
    const w5 = ["who", "where", "how", "what"]
      .filter((k) => a[k])
      .map((k) => `<div class="w5-h">${k}</div><div class="w5-p">${escapeHtml(a[k])}</div>`)
      .join("");
    const form = (a.params || []).map((p) => {
      const req = p.required ? '<span class="ad-req">*</span>' : "";
      let input;
      if (Array.isArray(p.enum)) {
        input = `<select class="sm-input ad-p" data-p="${escapeHtml(p.name)}">` +
          p.enum.map((v) => `<option value="${escapeHtml(v)}"${v === p.default ? " selected" : ""}>${escapeHtml(v)}</option>`).join("") +
          `</select>`;
      } else {
        input = `<input class="sm-input ad-p" data-p="${escapeHtml(p.name)}" value="${escapeHtml(p.default || "")}" placeholder="${escapeHtml(p.placeholder || "")}">`;
      }
      return `<div class="ad-field"><label>${escapeHtml(p.label || p.name)}${req}</label>${input}</div>`;
    }).join("");
    middle =
      `<div class="ad-5w">${w5}</div>` +
      (form ? `<div class="ad-sub">参数</div><div class="ad-form">${form}</div>` : "");
  } else {
    middle = `<div class="ad-prompt"><div class="ad-sub">将执行</div><pre>${escapeHtml(a.prompt || "")}</pre></div>`;
  }
  det.innerHTML =
    `<div class="ad-h1">${escapeHtml(a.label)}</div>` +
    `<div class="ad-desc">${escapeHtml(a.desc || "")}</div>` +
    middle +
    `<div class="ad-actions"><button class="ad-run">执行</button>` +
    (when !== "manual" ? `<span class="ad-trig">触发：${escapeHtml(TRIGGER_LABEL[when] || when)}</span>` : "") +
    `</div><div class="ad-runs-head"><span class="ad-sub">执行记录</span><button class="ad-clear" title="清空执行记录" hidden></button></div><div class="ad-runs"></div>`;
  body.appendChild(det);

  const runsBox = det.querySelector(".ad-runs");
  const clearBtn = det.querySelector(".ad-clear");
  clearBtn.innerHTML = ICONS.trash;
  const myRunEntries = () => {   // 本应用的执行记录(两引擎统一从 taskRuns 取),按时间倒序
    const taskRuns = wbState.taskRuns || {};
    const sessTime = (id) => (allSessions.find((s) => s.id === id)?.time?.updated) || 0;
    return Object.entries(taskRuns)
      .filter(([, tr]) => tr.domain === domainId && tr.taskId === a.id)
      .sort(([ix, ax], [iy, ay]) => ((ay.at || sessTime(iy)) - (ax.at || sessTime(ix))));
  };
  const renderRuns = () => {
    const entries = myRunEntries();
    runsBox.innerHTML = "";
    clearBtn.hidden = !entries.length;
    if (!entries.length) { runsBox.innerHTML = '<div class="ad-empty">还没有执行记录</div>'; return entries; }
    for (const [id, tr] of entries)
      runsBox.appendChild(termRunRecord(id, tr, a.label, false));   // 三引擎执行记录都是嵌入式 TUI
    return entries;
  };
  renderRuns();
  clearBtn.onclick = async () => {
    const entries = myRunEntries();
    if (!entries.length || !confirm(`清空「${a.label}」的 ${entries.length} 条执行记录？会删除这些会话。`)) return;
    clearBtn.disabled = true;
    for (const [id, tr] of entries) {
      // 执行记录一律是 GUI 会话(TUI 退场后不再有终端形态的那一档),删走 /session。
      try { await api(`/session/${encodeURIComponent(id)}`, { method: "DELETE", timeout: 8000 }); } catch {}
      delete wbState.taskRuns[id];
    }
    await saveState({ taskRuns: wbState.taskRuns });
    renderRuns();
    clearBtn.disabled = false;
  };

  det.querySelector(".ad-run").onclick = async (ev) => {
    // 组装执行文本：5W 声明式=params 填占位→四段拼接；旧式=原 prompt
    let text;
    if (isDecl) {
      const vals = {};
      let missing = null;
      det.querySelectorAll(".ad-p").forEach((inp) => { vals[inp.dataset.p] = inp.value.trim(); });
      for (const p of a.params || [])
        if (p.required && !vals[p.name]) missing = p.label || p.name;
      if (missing) { alert(`请填写必填参数：${missing}`); return; }
      const subst = (str) => (str || "").replace(/\$\{(\w+)\}/g, (_, k) => vals[k] ?? "");
      text = [["角色", subst(a.who)], ["范围", subst(a.where)], ["步骤", subst(a.how)], ["产出", subst(a.what)]]
        .filter(([, v]) => v)
        .map(([k, v]) => `【${k}】${v}`)
        .join("\n") + "\n\n开始执行。";
    } else {
      text = a.prompt;
    }
    ev.target.disabled = true;
    // 执行记录是一段带 task 标的 GUI 会话:不进侧栏 Vibing 列,只挂在这条任务行下面。
    // TUI 退场前这里起的是一个嵌入式终端(prompt 作首条消息喂进 TUI),现在两者的语义一致——
    // 建会话、把执行文本作为首句发出去,区别只在它跑在哪种视图里。
    let r;
    try {
      r = await newHtmlSessionWithPrompt({ domain: domainId, task: true }, text);
    } catch { ev.target.disabled = false; return; }
    ev.target.disabled = false;
    if (!r || !r.id) return;
    wbState.taskRuns = wbState.taskRuns || {};
    wbState.taskRuns[r.id] = { domain: domainId, taskId: a.id, trigger: when, engine: curEngine, at: Date.now(), label: a.label };
    await saveState({ taskRuns: wbState.taskRuns });
    watchTaskRunResumeId(r.id);
    if (runsBox.querySelector(".ad-empty")) runsBox.innerHTML = "";
    runsBox.prepend(termRunRecord(r.id, wbState.taskRuns[r.id], a.label, true));
  };
}
// 终端引擎(claude/codex)的执行记录：记录体里嵌真实 TUI。live=刚 spawn;历史=展开时 --resume 拉活
function termRunRecord(id, tr, label, live) {
  const det = document.createElement("details");
  det.className = "run-record term-run";
  det.open = !!live;
  const when = tr.at ? new Date(tr.at).toLocaleString("zh-CN", { hour12: false }) : "";
  det.innerHTML = `<summary><span class="rr-title">${escapeHtml(label || tr.label || "执行")}</span>` +
    `<span class="rr-when">${escapeHtml(when)}</span></summary><div class="rr-body rr-term"></div>`;
  const bodyEl = det.querySelector(".rr-body");
  let handle = null;
  const mount = async () => { if (!handle) handle = await embedTerminal(bodyEl, taskRunTermId(id, tr), tr.engine); };
  if (live) mount();
  else det.addEventListener("toggle", () => {
    if (det.open) mount();
    else if (handle) { handle.dispose(); handle = null; bodyEl.innerHTML = ""; }   // 收起即脱离(进程保活)
  });
  return det;
}
