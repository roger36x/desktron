// 应用操控审批是会话里的阻塞交互，不是模型的思考或工具日志。
//
// 待审批同时出现在发起它的回合过程与全局提醒里：过程保留动作上下文，右侧信息卡或
// 右上通知负责跨会话发现。两个入口共用一份账本与同一条决定路径；决定后的过程卡原位
// 变成回执，刷新、切引擎与跨端处理都不消失。
//
// 不新写状态机:查 /workbench/app-control/approvals,批走 .../approvals/decide,
// 后端仍复用 runtime.approve。数据只有 _apprItems 一份，决定只有 apprDecide 一条路。

const _apprCards = new Map();   // operation_id → 会话过程里的审批卡/回执卡
const _apprItems = new Map();   // operation_id → 后端审批账本里的最新状态
const _apprReplying = new Set();// 正在提交决定,避免轮询期间重复点击
const _apprNodes = new Map();   // operation_id → 信息卡里的审批条目
const _apprSeen = new Set();    // 见过的待批 id:新来的那条自动成为当前项
const _apprTickets = new Map(); // operation_id → 飞书深链的一次性票据；只驻留页面内存
let _apprOpenId = null;         // null=尚未选择，""=用户明确收起全部，id=当前展开项
let _apprHadPending = false;    // 上一帧还有待批:清空那一刻通知才显示「已处理」回执
let _apprDoneTimer = 0;
let _apprRaf = 0;
let _apprTick = 0;
let _apprRO = null;
let _apprROTarget = null;
let _apprNativeSig = "";        // 原生浏览器需要避让的通知矩形；不变时不重复发 IPC
let _apprNativeSeq = 0;
let _apprLoaded = false;
let _apprLoadError = "";
let _apprPolling = false;
let _apprPollPromise = null;
const _apprNativePending = new Map();
window.__approvalNativeResult = (message) => {
  const pending = _apprNativePending.get(String(message?.requestId || ""));
  if (!pending) return;
  _apprNativePending.delete(String(message.requestId));
  if (message.error) pending.reject(new Error(String(message.error)));
  else pending.resolve(message.result);
};
function _apprNative(action, body = {}) {
  const bridge = window.webkit?.messageHandlers?.approvalControl;
  if (!bridge) return Promise.reject(new Error("trusted approval UI unavailable"));
  const requestId = "approval-" + (++_apprNativeSeq) + "-" + Date.now();
  return new Promise((resolve, reject) => {
    _apprNativePending.set(requestId, { resolve, reject });
    bridge.postMessage({ requestId, action, ...body });
    setTimeout(() => {
      if (!_apprNativePending.has(requestId)) return;
      _apprNativePending.delete(requestId);
      reject(new Error("approval request timeout"));
    }, 12000);
  });
}

function _apprHasNative() {
  return !!window.webkit?.messageHandlers?.approvalControl;
}

function _apprBrowser(action, body = {}) {
  const headers = { "X-Desk-Approval-UI": "1" };
  if (action === "list") return api("/workbench/app-control/approvals?include_resolved=1&limit=200",
    { headers, credentials: "same-origin", timeout: 10000 });
  if (action !== "clear" && action !== "decide") return Promise.reject(new Error("不支持的审批操作"));
  headers["content-type"] = "application/json";
  return api("/workbench/app-control/approvals/" + action, {
    method: "POST", headers, credentials: "same-origin", timeout: 10000, body: JSON.stringify(body),
  });
}

// 动作说明:条目第一行要写清同意的是什么。
const _APPR_WHAT = {
  open: (o) => `打开网页${(o.input && o.input.url) ? "：" + o.input.url : ""}`,
  navigate: (o) => `跳转到${(o.input && o.input.url) ? " " + o.input.url : "网页"}`,
  click: () => "点击页面元素",
  coordinate_click: () => "按坐标点击页面",
  press: (o) => `在页面上按键${(o.input && o.input.key) ? " " + o.input.key : ""}`,
  type: () => "在页面上输入文字",
  fill: () => "填写表单",
  select: () => "选择下拉项",
  hover: () => "悬停在页面元素上",
  scroll: () => "滚动页面",
  wait: () => "等待页面就绪",
  back: () => "后退到上一页",
  forward: () => "前进到下一页",
  reload: () => "重新加载页面",
  activate: () => "切换到这个标签",
  handoff: () => "显示并接管原标签",
  close: () => "关闭这个标签",
  screenshot: () => "对当前页面截图",
};

// 条目、分区头、通知与小条统一用现有审批回执的锁形图标,不用状态圆点(2026-09-04 设计稿)。
// HS_ICONS 在 app-chat-tools.js 里，同一包内先加载；直接复用锁与解锁图稿。
const _APPR_CHEV = '<svg class="appr-chev" viewBox="0 0 32 32" data-desk-scattered="23" aria-hidden="true"><g fill="none" stroke="var(--ink-42)" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="m6.8 11.8 8.3 8.8a1.25 1.25 0 0 0 1.8 0l8.3-8.8"/></g></svg>';
// 通知和会话过程必须是同一张审批卡。共用内部结构，避免两套 DOM 靠 CSS 追着模仿。
function _apprApprovalInner() {
  const lock = HS_ICONS.lock;
  return '<div class="hs-ico">' + lock + '</div>'
    + '<div class="hs-card-t"><div class="hs-t1"></div><div class="hs-t2"></div></div>'
    + '<div class="hs-acts"><button type="button" class="hs-lnk">拒绝</button>'
    + '<button type="button" class="hs-pill">允许</button></div>';
}

function _apprDesc(op) {
  const f = _APPR_WHAT[op.name];
  return (f ? f(op) : `${op.kind} · ${op.name}`);
}

function _apprTitle(op) {
  const intent = String((op && op.intent) || "").trim();
  return intent || _apprDesc(op);
}

function _apprEsc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function _apprSessionName(sid) {
  if (!sid) return "";
  // 侧栏清单已把引擎 id / resume_id / Desk session_id 归并到同一条会话，
  // 且叠加了自动命名与用户改名；审批卡应复用这份用户实际看到的名称。
  try {
    const title = typeof _termListedTitle === "function" ? _termListedTitle(sid) : "";
    if (title) return String(title);
  } catch {}
  try {
    const p = (typeof pages !== "undefined" ? pages : []).find((x) => x.url === "chat:" + sid);
    if (p && p.title) return String(p.title);
  } catch {}
  try {
    if (typeof termRowMatchesId === "function") {
      const row = [...document.querySelectorAll(".term-item")].find((x) => termRowMatchesId(x, sid));
      const label = row && row.querySelector(".ti-label");
      if (label && label.textContent.trim()) return label.textContent.trim();
    }
  } catch {}
  return sid.slice(0, 8);
}

// 标题已经说明要做什么；副行只补充审批判断所需的业务上下文。
// 页面标题是操作范围，会话名称是这次操作服务的任务；地址与执行器名不进入卡片。
function _apprContext(op) {
  const scope = String((op && op.page_title) || "").trim();
  const purpose = _apprSessionName(op && op.session_id);
  return [scope && "范围：" + scope, purpose && "用途：" + purpose].filter(Boolean).join(" · ");
}

// 展开详情只平铺动作参数，不预设它一定是浏览器页面；表单值只保留短摘要。
function _apprInputText(op) {
  const i = op.input || {};
  const bits = [];
  if (String(op.intent || "").trim()) bits.push(_apprDesc(op));
  if (i.url) bits.push(i.url);
  if (i.selector) bits.push("选择器 " + i.selector);
  else if (i.node_id) bits.push("节点 " + i.node_id);
  if (i.key) bits.push("按键 " + i.key);
  if (typeof i.x === "number" && typeof i.y === "number") bits.push(`坐标 (${i.x}, ${i.y})`);
  if (typeof i.value === "string" && i.value) bits.push("值 " + (i.value.length > 80 ? i.value.slice(0, 80) + "…" : i.value));
  if (typeof i.text === "string" && i.text) bits.push("文字 " + (i.text.length > 80 ? i.text.slice(0, 80) + "…" : i.text));
  if (op.name === "screenshot") bits.push("截图可能包含页面业务信息");
  if (op.effect === "external_write") bits.push("这会写入外部系统");
  return bits.join(" · ") || _apprDesc(op);
}

// 从飞书通知点进来的:fragment 带 operation id 与一次性票据。立刻查一次,不等下一轮轮询——
// 人是特意点进来处理它的,再等五秒就成了「点了没反应」。
// 参数用完即清:留着的话刷新一次又会跳一遍,而那条多半已经决定过了。
async function apprFromLink() {
  let id = "", sid = "", ticket = "";
  try {
    const q = new URLSearchParams(String(location.hash || "").replace(/^#/, ""));
    id = q.get("approve") || "";
    sid = q.get("session") || "";
    ticket = q.get("approval_ticket") || "";
  } catch { return; }
  if (!id) return;
  try {
    const u = new URL(location.href);
    u.hash = "";
    history.replaceState(null, "", u.pathname + u.search);
  } catch {}
  // 先切到发起它的那段会话:回执落在那一页,人点进来看到的应当是这段对话的上下文
  // (2026-09-03 用户实见切错页像是没出现)。待批本身不依赖这一页开着。
  if (sid) {
    try {
      let p = (typeof pages !== "undefined" ? pages : []).find((x) => x.url === "chat:" + sid);
      if (!p && typeof openHtmlSession === "function") {
        await openHtmlSession(sid, "对话", false);
        p = (typeof pages !== "undefined" ? pages : []).find((x) => x.url === "chat:" + sid);
      }
      if (p && typeof showOverlay === "function") showOverlay(p);
    } catch {}
  }
  // 点进来时那条可能已经不在待批里了(超时落为失败、或已在别处决定)。
  // 什么都不说的话人会以为页面坏了——链接是我们发出去的,过期也该有个交代。
  let list = [];
  try {
    if (!ticket) throw new Error("missing approval ticket");
    const r = await api("/workbench/app-control/approvals/ticket", {
      method: "POST", headers: { "content-type": "application/json" }, timeout: 10000,
      body: JSON.stringify({ operation_id: id, ticket }),
    });
    list = r && r.approval ? [r.approval] : [];
  } catch { return; }
  if (!list.some((o) => o.id === id)) {
    alert("这条确认已经结束了：可能已超时，或已在别处处理。");
    return;
  }
  _apprTickets.set(id, ticket);
  list.forEach((op) => { if (op && op.id) _apprItems.set(op.id, op); });
  apprSync();
  apprFocus(id);            // 条目就位后把它展开并短暂高亮,别让人在多项里自己找
}

// 从飞书点进来的那一张:在它所在的宿主里展开并高亮。不再开居中遮罩——
// 待批已经常驻在信息卡或右上通知里,遮罩只是多一层要关的东西(2026-09-04 设计稿)。
function apprFocus(id) {
  const op = _apprItems.get(id);
  if (!op || !_apprPending(op)) return false;   // 已结束的不弹:那只是打扰
  _apprOpenId = id;
  apprRehost(true);
  const noticeCard = document.querySelector("#appr-notice .appr-notice-card");
  const el = noticeCard && noticeCard.dataset.op === id ? noticeCard : _apprNodes.get(id);
  if (!el) return false;
  el.classList.add("appr-hi");
  setTimeout(() => el.classList.remove("appr-hi"), 1600);
  try { el.scrollIntoView({ block: "nearest" }); } catch {}
  return true;
}

async function apprPoll() {
  if (_apprPollPromise) return _apprPollPromise;
  _apprPolling = true;
  _apprPollPromise = (async () => {
    let list = [];
    try {
      const r = await (_apprHasNative() ? _apprNative("list") : _apprBrowser("list"));
      list = (r && (r.approvals || r.pending)) || [];
      if (!Array.isArray(list) || !r || (!r.approvals && !r.pending)) throw new Error("审批返回格式异常");
      _apprLoaded = true;
      _apprLoadError = "";
    } catch (e) {
      _apprLoadError = e?.message || "连接失败";
      apprRehost(true); // 保留上次记录，不能把请求失败画成“没有历史”。
      return;
    }
    _apprItems.clear();
    list.forEach((op) => { if (op && op.id) _apprItems.set(op.id, op); });
    globalThis.hsVpdNoteAll?.("appr", { ph: 0, n: list.length });   // 视口取证:五秒轮询的落点
    // 页面重建会把旧节点整棵摘掉。Map 若继续指着旧节点，下一轮会误以为“已经画过”。
    for (const [id, el] of _apprCards) {
      if (!el.isConnected) _apprCards.delete(id);
      else if (!_apprItems.has(id)) { el.remove(); _apprCards.delete(id); }
    }
    apprSync();
  })();
  try { return await _apprPollPromise; }
  finally { _apprPolling = false; _apprPollPromise = null; }
}

function _apprPending(op) {
  return op && op.approval_state === "pending" && op.status === "waiting_user";
}

function _apprStateText(op) {
  const s = String((op && op.approval_state) || "");
  if (s.startsWith("approved:")) return "已允许";
  if (s.startsWith("rejected:")) return "已拒绝";
  if (s === "timeout" || (op && op.error === "approval_timeout")) return "已超时";
  if (s === "cancelled" || (op && op.status === "cancelled")) return "已取消";
  return _apprPending(op) ? "需要你的确认" : "已结束";
}

// 把账本同步到两处:全局待批提醒与会话过程卡。过程卡在决定后原位变成回执。
function apprSync() {
  const ops = [..._apprItems.values()];
  // 信息卡保留账本中所有未清空的记录；过程回执不受清空影响。
  for (const op of ops) {
    if (_apprPending(op)) {
      if (!_apprSeen.has(op.id)) {
        _apprSeen.add(op.id);
        _apprOpenId = op.id;
        document.querySelectorAll(".hs-sit-approvals").forEach((sec) => {
          sec.classList.remove("collapsed");
          const head = sec.querySelector(".hs-sit-approval-head");
          if (head) head.setAttribute("aria-expanded", "true");
        });
      }
      _apprNode(op);
      apprOpen(op);
    } else {
      if (!op.approval_cleared_at) _apprNode(op);
      apprOpen(op);
    }
  }
  // 只移除账本已淘汰或被用户清空的已结束条目，待处理项始终保留。
  for (const id of [..._apprNodes.keys()]) {
    const op = _apprItems.get(id);
    if (!op || (!_apprPending(op) && op.approval_cleared_at)) {
      const el = _apprNodes.get(id);
      _apprNodes.delete(id);
      if (el) el.remove();
    }
  }
  apprRehost();
}

function _apprNode(op) {
  let el = _apprNodes.get(op.id);
  if (!el) {
    el = document.createElement("article");
    el.className = "appr-item";
    el.dataset.op = op.id;
    el.innerHTML = '<div class="appr-sum" aria-expanded="false">'
      + '<span class="appr-glyph">' + HS_ICONS.lock + '</span>'
      + '<span class="appr-copy"><span class="appr-heading"><b class="appr-title"></b>'
      + '<span class="appr-age"></span><button type="button" class="appr-toggle" aria-label="展开审批详情">' + _APPR_CHEV + '</button></span>'
      + '<span class="appr-secondary"><small class="appr-sub"></small><span class="appr-meta"><span class="appr-meta-line">'
      + '<span class="appr-state" hidden></span>'
      + '<span class="appr-actions"><button type="button" class="appr-reject">拒绝</button>'
      + '<button type="button" class="appr-allow">允许</button></span></span></span></span></span></div>'
      + '<div class="appr-detail"><div class="appr-what"></div></div>';
    const toggle = () => {
      _apprOpenId = _apprOpenId === op.id ? "" : op.id;
      apprRehost(true);
    };
    el.querySelector(".appr-sum").onclick = (event) => {
      if (event.target.closest(".appr-actions, .appr-toggle")) return;
      toggle();
    };
    el.querySelector(".appr-toggle").onclick = toggle;
    el.querySelector(".appr-reject").onclick = (event) => { event.stopPropagation(); apprDecide(op.id, false); };
    el.querySelector(".appr-allow").onclick = (event) => { event.stopPropagation(); apprDecide(op.id, true); };
    _apprNodes.set(op.id, el);
  }
  _apprNodePaint(el, op);
  return el;
}

async function apprClearHistory() {
  const ended = [..._apprItems.values()].filter((op) => !_apprPending(op) && !op.approval_cleared_at);
  if (!ended.length) return;
  if (!confirm("从信息卡清空已结束的审批？会话过程中的审批记录会保留。")) return;
  const before = Math.max(...ended.map((op) => Number(op.updated_at || op.finished_at || op.created_at || 0)));
  try {
    await (_apprHasNative() ? _apprNative("clear", { before }) : _apprBrowser("clear", { before }));
    await apprPoll();
  } catch (e) {
    alert("清空失败：" + (e && e.message ? e.message : e));
  }
}

function _apprNodePaint(el, op) {
  const approved = String(op.approval_state || "").startsWith("approved:");
  const glyphs = HS_ICONS;
  el.querySelector(".appr-glyph").innerHTML = approved ? glyphs.unlock : glyphs.lock;
  el.querySelector(".appr-title").textContent = _apprTitle(op);
  el.querySelector(".appr-sub").textContent = _apprContext(op);
  el.querySelector(".appr-age").textContent = typeof hsSituationAgo === "function"
    ? hsSituationAgo(Number(op.created_at || 0) * 1000) : "";
  el.querySelector(".appr-what").textContent = _apprInputText(op);
  const pending = _apprPending(op);
  const busy = _apprReplying.has(op.id);
  const acts = el.querySelector(".appr-actions");
  const state = el.querySelector(".appr-state");
  state.title = "";
  acts.hidden = !pending || busy;
  acts.querySelectorAll("button").forEach((b) => { b.disabled = busy; });
  el.classList.toggle("busy", busy);
  el.classList.toggle("decided", !pending);
  if (pending) {
    state.hidden = !busy;
    state.textContent = busy ? "处理中…" : "";
  } else {
    state.hidden = false;
    state.textContent = _apprStateText(op);
  }
}

// 决定只从这一条路出去。宿主是哪一个、那段会话开没开着都无所谓:锚点后端在入队时
// 已经记下,这里只在那一页开着时把当下的回合坐标一并带上,补旧记录的缺。
async function apprDecide(id, allow) {
  const op = _apprItems.get(id);
  const el = _apprNodes.get(id);
  if (!op || !_apprPending(op)) return;
  if (_apprReplying.has(id)) return;
  _apprReplying.add(id);
  if (el) _apprNodePaint(el, op);
  const card = _apprCards.get(id);
  if (card) _apprPaint(card, op);
  const before = Object.assign({}, op);
  let anchor;
  try {
    const v = (typeof _hsViews !== "undefined") && _hsViews.get(op.session_id || "");
    const host = v && v.el && v.el.isConnected ? _apprTurn(v, op) : null;
    if (host) {
      anchor = { turn_id: host.msgId || "", turn_seq: host.seq || 0,
                 after_part_id: op.after_part_id || "",
                 after_part_index: op.after_part_index || host.parts.size };
    }
  } catch {}
  try {
    const ticket = _apprTickets.get(id) || "";
    const saved = ticket
      ? await api("/workbench/app-control/approvals/decide", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify(Object.assign(
            { operation_id: id, approved: allow, ticket }, anchor ? { anchor } : {})),
        })
      : await (_apprHasNative()
        ? _apprNative("decide", Object.assign(
          { operationId: id, approved: allow }, anchor ? { anchor } : {}))
        : _apprBrowser("decide", Object.assign(
          { operation_id: id, approved: allow }, anchor ? { anchor } : {})));
    _apprTickets.delete(id);
    _apprReplying.delete(id);
    if (saved) {
      _apprItems.set(id, saved);
    } else {
      Object.assign(op, { approval_state: allow ? "approved:ui_user" : "rejected:ui_user",
                          status: allow ? "queued" : "rejected" });
    }
    apprSync();
  } catch (e) {
    _apprReplying.delete(id);
    Object.assign(op, before);
    if (el) {
      _apprNodePaint(el, op);
      const state = el.querySelector(".appr-state");
      state.hidden = false;
      state.textContent = "提交失败";
      state.title = "提交失败：" + (e && e.message ? e.message : e);
    }
    if (card) {
      _apprPaint(card, op);
      card.querySelector(".hs-t2").textContent = "提交失败：" + (e && e.message ? e.message : e);
    }
  } finally {
    _apprReplying.delete(id);
  }
}

// ---- 宿主 ----------------------------------------------------------------
// 不按窗口宽度猜。信息卡本身有一整套显隐规则(容器 1260、视口 1180/620、触屏、审阅态),
// 这里只看结果:它此刻真实可见且不窄于 280px,就用它;否则右上通知。
function _apprCardHost() {
  if (typeof _hsViews === "undefined") return null;
  for (const v of _hsViews.values()) {
    if (!v || !v.el || !v.el.isConnected) continue;
    const sit = v.el.querySelector(".hs-sit");
    if (!sit || sit.offsetParent === null) continue;
    if (sit.getBoundingClientRect().width < 280) continue;
    return sit.querySelector(".hs-sit-approval-host");
  }
  return null;
}

function _apprNoticeHost() {
  let host = document.getElementById("appr-notice");
  if (host) return host;
  host = document.createElement("div");
  host.id = "appr-notice";
  host.className = "appr-notice-host";
  host.hidden = true;
  host.innerHTML = '<section class="appr-notice hs-page" role="region" aria-label="待审批">'
    + '<div class="appr-notice-body">'
    + '<div class="appr-notice-card hs-card hs-ask hs-approval">'
    + _apprApprovalInner() + '</div>'
    + '<button type="button" class="appr-notice-next" hidden></button>'
    + '</div>'
    + '<div class="appr-notice-done" hidden><b>审批已处理</b><span>浏览器可以继续执行</span></div>'
    + '</section>';
  const card = host.querySelector(".appr-notice-card");
  card.querySelector(".hs-lnk").onclick = () => apprDecide(card.dataset.op, false);
  card.querySelector(".hs-pill").onclick = () => apprDecide(card.dataset.op, true);
  host.querySelector(".appr-notice-next").onclick = () => {
    const ids = _apprSortedIds().filter((id) => _apprPending(_apprItems.get(id)));
    const at = ids.indexOf(_apprOpenId);
    _apprOpenId = ids[(at + 1) % Math.max(1, ids.length)] || "";
    apprRehost(true);
  };
  document.body.appendChild(host);
  return host;
}

function apprRehost(immediate) {
  if (immediate) { if (_apprRaf) { cancelAnimationFrame(_apprRaf); _apprRaf = 0; } _apprRehostNow(); return; }
  if (_apprRaf) return;
  _apprRaf = requestAnimationFrame(() => { _apprRaf = 0; _apprRehostNow(); });
}

// 原生浏览器正文不在当前 DOM 层叠上下文里。通知的显隐或尺寸变化时，让它重新读取
// browserRect；同一份矩形不重复同步，审批的一秒巡检不会持续刷原生桥。
function _apprSyncNativeSurface(notice) {
  requestAnimationFrame(() => {
    const r = notice && !notice.hidden ? notice.getBoundingClientRect() : null;
    const sig = r && r.width > 0 && r.height > 0
      ? [r.left, r.top, r.width, r.height].map((x) => Math.round(x)).join(":") : "";
    if (sig === _apprNativeSig) return;
    _apprNativeSig = sig;
    globalThis.hsVpdNoteAll?.("appr", { ph: 4 });
    if (typeof browserSyncNative === "function") browserSyncNative();
  });
}

function _apprPositionNotice(notice) {
  // 标题栏随会话、设置、应用面板变化，不能把通知固定在页面顶端。
  let bottom = 0;
  document.querySelectorAll("#top-nav, #conv-head, #overlay-head, #term-head, #app-side-head, #app-side-head-2, .sm-modal-toolbar").forEach((head) => {
    if (!head.getClientRects().length || getComputedStyle(head).visibility === "hidden") return;
    const rect = head.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight) {
      bottom = Math.max(bottom, rect.bottom);
    }
  });
  notice.style.setProperty("--appr-title-bottom", Math.ceil(bottom) + "px");
}

function _apprSortedIds() {
  return [..._apprNodes.keys()].sort((a, b) => {
    const left = _apprItems.get(a), right = _apprItems.get(b);
    const lp = _apprPending(left), rp = _apprPending(right);
    if (lp !== rp) return lp ? -1 : 1;
    // 待批先到先处理；历史按最后更新时间倒序。
    if (lp) return Number(left.created_at || 0) - Number(right.created_at || 0);
    const time = (op) => Number(op.updated_at || op.finished_at || op.created_at || 0);
    return time(right) - time(left);
  });
}

function _apprSectionState(sec, hasPending) {
  // 初次显示及待办全部结束时折叠历史；相同状态的轮询不覆盖手动展开/收起。
  if (sec._apprHasPending === hasPending) return;
  sec._apprHasPending = hasPending;
  sec.classList.toggle("collapsed", !hasPending);
  const head = sec.querySelector(".hs-sit-approval-head");
  if (head) head.setAttribute("aria-expanded", String(hasPending));
}

function _apprRehostNow() {
  globalThis.hsVpdNoteAll?.("appr", { ph: 3 });
  const ids = _apprSortedIds();
  const pendingIds = ids.filter((id) => _apprPending(_apprItems.get(id)));
  const card = _apprCardHost();
  const notice = _apprNoticeHost();
  const host = card;
  // 信息卡显示未清空的审批列表；右上通知使用会话审批卡结构单独绘制当前待办。
  const hostedIds = card ? ids : [];
  ids.forEach((id) => {
    const el = _apprNodes.get(id);
    if (host && hostedIds.includes(id)) {
      if (el.parentNode !== host) host.appendChild(el);
    } else if (el.parentNode) el.remove();
  });
  // 节点可能已在宿主中；状态变化后仍需按新顺序排列，不重建节点及其事件。
  hostedIds.forEach((id, index) => {
    const el = _apprNodes.get(id);
    if (host.children[index] !== el) host.insertBefore(el, host.children[index] || null);
  });
  // 信息卡允许人把唯一一项也收起。空字符串是明确选择，不能被一秒一次的宿主巡检
  // 改回第一项；通知没有折叠详情这一层，仍必须选中一项供人处理。
  const selectableIds = card ? hostedIds : pendingIds;
  if (_apprOpenId === null || (_apprOpenId && !selectableIds.includes(_apprOpenId))
      || (!card && !_apprOpenId && selectableIds.length)) {
    _apprOpenId = selectableIds[0] || "";
  }
  ids.forEach((id) => {
    const el = _apprNodes.get(id);
    const open = card ? id === _apprOpenId : true;
    el.classList.toggle("open", open);
    el.classList.toggle("cur", id === _apprOpenId);
    el.querySelector(".appr-sum").setAttribute("aria-expanded", String(open));
    el.querySelector(".appr-toggle").setAttribute("aria-expanded", String(open));
    el.querySelector(".appr-toggle").setAttribute("aria-label", open ? "收起审批详情" : "展开审批详情");
  });
  // 信息卡分区:当前可见的信息卡始终保留审批区；没有待批时只归零计数，
  // 分区标题与总计数对应同一份列表，折叠后仍显示统计。
  document.querySelectorAll(".hs-sit-approvals").forEach((sec) => {
    _apprSectionState(sec, pendingIds.length > 0);
    const mine = !!card && sec.contains(card);
    sec.hidden = !mine;
    if (!mine) return;
    const count = sec.querySelector(".hs-sit-approval-count");
    count.textContent = String(ids.length);
    count.hidden = !ids.length;
    count.title = `待处理 ${pendingIds.length}，已结束 ${ids.length - pendingIds.length}`;
    count.setAttribute("aria-label", count.title);
    DeskI18n.bind(sec.querySelector("h2"),
      pendingIds.length && pendingIds.length === ids.length ? "sit.approvals.pending" : "sit.approvals.recent");
    let loadState = sec.querySelector(".appr-load-state");
    if (!loadState) {
      loadState = document.createElement("button");
      loadState.type = "button";
      loadState.className = "appr-load-state";
      loadState.setAttribute("aria-live", "polite");
      loadState.onclick = () => apprPoll();
      sec.appendChild(loadState);
    }
    loadState.hidden = !_apprLoadError && ids.length > 0;
    loadState.disabled = !_apprLoadError;
    const message = _apprLoadError ? "审批加载失败，点击重试" : (_apprLoaded ? "暂无审批记录" : "正在加载审批…");
    if (loadState.textContent !== message) loadState.textContent = message;
    loadState.title = _apprLoadError;
    const clear = sec.querySelector(".hs-sit-approval-clear");
    if (clear) {
      clear.hidden = !ids.some((id) => {
        const op = _apprItems.get(id);
        return !_apprPending(op) && !op.approval_cleared_at;
      });
      if (!clear.onclick) clear.onclick = apprClearHistory;
    }
    const head = sec.querySelector(".hs-sit-approval-head");
    if (head && !head.onclick) {
      head.onclick = () => {
        const collapsed = sec.classList.toggle("collapsed");
        head.setAttribute("aria-expanded", String(!collapsed));
      };
    }
  });
  // 右上通知
  const box = notice.querySelector(".appr-notice");
  const noticeCard = notice.querySelector(".appr-notice-card");
  const done = notice.querySelector(".appr-notice-done");
  const body = notice.querySelector(".appr-notice-body");
  if (card || !pendingIds.length) {
    // 全部处理完:通知模式下显示完成回执 1.6s 再退出
    if (!card && _apprHadPending && !pendingIds.length) {
      notice.hidden = false; box.hidden = false;
      body.hidden = true; done.hidden = false;
      clearTimeout(_apprDoneTimer);
      _apprDoneTimer = setTimeout(() => { done.hidden = true; body.hidden = false; notice.hidden = true; }, 1600);
    } else if (done.hidden) {
      notice.hidden = true;
    }
  } else {
    clearTimeout(_apprDoneTimer); done.hidden = true; body.hidden = false;
    notice.hidden = false;
    box.hidden = false;
    const next = notice.querySelector(".appr-notice-next");
    const at = Math.max(0, pendingIds.indexOf(_apprOpenId));
    const op = _apprItems.get(pendingIds[at]);
    if (op) {
      noticeCard.dataset.op = op.id;
      _apprPaint(noticeCard, op);
    }
    next.hidden = pendingIds.length < 2;
    next.textContent = `查看下一项 · ${at + 1}/${pendingIds.length}`;
  }
  _apprHadPending = pendingIds.length > 0;
  if (!notice.hidden) _apprPositionNotice(notice);
  _apprSyncNativeSurface(notice);
  _apprWatch(pendingIds.length > 0 || (!!card && ids.length > 0), card);
}

// 宿主会变:信息卡随容器宽度、审阅态、程序坞显隐;人也会切页。变化时在下一帧迁移。
function _apprWatch(on, card) {
  if (!on) {
    if (_apprTick) { clearInterval(_apprTick); _apprTick = 0; }
    if (_apprRO) { _apprRO.disconnect(); _apprRO = null; _apprROTarget = null; }
    return;
  }
  if (!_apprTick) {
    _apprTick = setInterval(() => apprRehost(), 1000);
    window.addEventListener("resize", () => apprRehost());
  }
  const target = card ? card.closest(".hs-page") : null;
  if (target !== _apprROTarget && typeof ResizeObserver === "function") {
    if (_apprRO) _apprRO.disconnect();
    _apprRO = target ? new ResizeObserver(() => { globalThis.hsVpdNoteAll?.("appr", { ph: 5 }); apprRehost(); }) : null;
    if (_apprRO) _apprRO.observe(target);
    _apprROTarget = target;
  }
}

// ---- 回执:决定之后仍落回原会话锚点 ----------------------------------------
function _apprTurn(v, op) {
  // message id 在引擎记录对账后可能变化，回合 seq 才是第二把稳定钥匙。
  const exact = v.turns.find((t) => op.turn_id && t.msgId === op.turn_id)
    || v.turns.find((t) => op.turn_seq && t.seq === op.turn_seq);
  if (exact) return exact;
  // Codex rollout 在收尾对账时会按原生记录的时间重新生成 user/assistant message id，
  // 且原生记录没有 Desk 的 seq；实时锚点因此会在刷新后双双失效。created_at 是动作进入
  // 同一会话账本的服务端时刻，取「它发生前最后一条用户消息」可稳定还原所属回合。
  // 若审批所在回合还没翻进当前 DOM，所有 uat 都晚于动作，此处自然不猜；hsPrepend 在
  // 用户向上翻到那一段时会再次调用 apprRenderView。
  const at = Number(op.created_at || 0) * 1000;
  if (!at) return null;
  let timed = null;
  for (const t of v.turns) {
    const started = Number(t.uat || 0);
    if (started && started <= at && (!timed || started > Number(timed.uat || 0))) timed = t;
  }
  return timed;
}

function _apprPlace(t, el, op) {
  const parts = [...t.parts.values()];
  let at = parts.findIndex((p) => op.after_part_id && p.id === op.after_part_id) + 1;
  if (!at && op.after_part_index) at = Math.min(Number(op.after_part_index), parts.length);
  // 审批是执行过程中的决策边界，按保存的 part 锚点与思考、工具混排。旧实现虽然计算了
  // part 位置，却只寻找正文节点，过程节点全被跳过；连续审批因而集中堆到正文里。
  hsProcPlace(t, el, at);
}

function _apprPaint(el, op) {
  const pending = _apprPending(op);
  const busy = _apprReplying.has(op.id);
  const approved = String((op && op.approval_state) || "").startsWith("approved:");
  const title = _apprTitle(op);
  const sub = pending ? (busy ? "处理中…" : _apprInputText(op)) : _apprStateText(op);
  // 五秒一轮的账本同步会把每张过程卡都重画一遍:换图标节点、换文本节点、重设 hidden。
  // 内容没变时这些都是空转,却让 WebKit 桌面端的滚动容器每轮往上跳一段再被贴底逻辑拉回
  // (2026-09-07 视口取证:上跳与轮询同相位,前一批 DOM 变动正是这几处;手机端没有原生桥,
  // 轮询在第一步就退出,所以不跳)。同一状态只画一次,变化才动 DOM。
  const sig = [op.id, pending ? 1 : 0, busy ? 1 : 0, approved ? 1 : 0, title, sub].join("\u0001");
  if (el._apprSig === sig) return;
  el._apprSig = sig;
  el.classList.toggle("resolved", !pending);
  el.classList.toggle("busy", busy);
  el.querySelector(".hs-ico").innerHTML = approved ? HS_ICONS.unlock : HS_ICONS.lock;
  el.querySelector(".hs-t1").textContent = title;
  el.querySelector(".hs-t2").textContent = sub;
  const acts = el.querySelector(".hs-acts");
  if (acts) {
    acts.hidden = !pending;
    acts.querySelectorAll("button, .hs-lnk").forEach((b) => {
      b.disabled = busy;
      b.setAttribute("aria-disabled", String(busy));
    });
  }
}

// 会话过程里的审批卡与回执。待批时可直接决定；结束后同一节点原位收成回执。
function apprOpen(op) {
  const v = (typeof _hsViews !== "undefined") && _hsViews.get(op.session_id || "");
  if (!v || !v.el || !v.el.isConnected) return;      // 那一页没开着就不画,下一轮再来
  const old = _apprCards.get(op.id);
  const t = _apprTurn(v, op);
  // 回执必须有权威回合锚点。旧版留下的记录没有锚点时不把它猜到最后一轮;
  // 落错会话位置比不显示更糟。
  if (!t) return;                                     // 历史尚未铺到该回合,下一轮再画
  if (old && old.isConnected) {
    _apprPaint(old, op);
    _apprPlace(t, old, op);
    if (_apprPending(op)) hsProcReveal(t);
    globalThis.hsVpdNote?.(v, "appr", { ph: 1 });
    return;
  }
  const el = document.createElement("div");
  el.className = "hs-card hs-ask hs-approval";
  el._host = hsProcHost(t);
  el.innerHTML = _apprApprovalInner();
  el.querySelector(".hs-lnk").onclick = () => apprDecide(op.id, false);
  el.querySelector(".hs-pill").onclick = () => apprDecide(op.id, true);
  _apprCards.set(op.id, el);
  _apprPaint(el, op);
  _apprPlace(t, el, op);
  if (_apprPending(op)) hsProcReveal(t);
  globalThis.hsVpdNote?.(v, "appr", { ph: 2 });
}

// 页面重建后先用最近一次轮询的账本立刻复原，不必再空等五秒。
function apprRenderView(v) {
  if (!v) return;
  for (const op of _apprItems.values()) {
    if (op.session_id === v.sid) apprOpen(op);
  }
  apprRehost();
}
