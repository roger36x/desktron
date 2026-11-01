// 由 app-chat.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 12/13 片
// ---- 本轮改动:审阅侧栏 ----------------------------------------------------
// 过程区的编辑卡已经把改了什么摆出来了,但一次回合改了五个文件时,它们散在一串步骤里,
// 要回答「这一轮总共动了什么」得自己往回翻。侧栏把同一回合的编辑按文件收拢:左边一份
// 文件清单带各自的增删,右边是选中那个文件的完整差异。不做轮次选择器——要看更早的,
// 回到那一轮的卡片上点进来,入口本来就在那儿。
const HS_RV_CTX = 3;          // 折叠时每处改动上下留几行参照(与卡片同口径)
const HS_RV_MINW = 340, HS_RV_MAXW = 900, HS_RV_DEFW = 520;

function hsRvIsEdit(p) {
  const t = String(p.tool || "").toLowerCase();
  return (t === "edit" || t === "multiedit" || t === "write") && (p.state || {}).status !== "running";
}

// 一个编辑 part 摊成「一个文件一份差异」。codex 的一次 apply_patch 可能同时动几个文件
// (input.changes 是个数组),那时按 changes 拆开,否则整个 part 就是一份。
function hsRvPartFiles(p) {
  const inp = (p.state || {}).input || {};
  const ch = Array.isArray(inp.changes) ? inp.changes.filter((c) => c && c.file_path) : null;
  if (ch && ch.length > 1) {
    return ch.map((c) => hsDiffRows("edit", { file_path: c.file_path, diff: c.diff }, true)).filter(Boolean);
  }
  const r = hsDiffRows(p.tool, inp, true);
  return r ? [r] : [];
}

// 这一回合的改动按文件合并。同一个文件被改了不止一次时不硬拼成一份「净差异」——那要重放
// 补丁才算得准,算错了比不合并更坏。改成把几段按发生顺序接起来,段间标一句第几次编辑。
function hsRvCollect(t) {
  const by = new Map();
  for (const p of t.parts.values()) {
    if (!hsRvIsEdit(p)) continue;
    for (const r of hsRvPartFiles(p)) {
      if (!r.rows.length) continue;
      const path = r.path || "(未命名)";
      let f = by.get(path);
      if (!f) { f = { path, rows: [], add: 0, del: 0, edits: 0 }; by.set(path, f); }
      f.edits += 1;
      if (f.rows.length) f.rows.push({ kind: "meta", text: `— 第 ${f.edits} 次编辑 —` });
      for (const row of r.rows) {
        if (row.kind === "file") continue;   // 已经按文件分组,补丁里的文件名行不再重复
        f.rows.push(row);
        if (row.kind === "add") f.add += 1;
        else if (row.kind === "del") f.del += 1;
      }
    }
  }
  return [...by.values()];
}

// 折叠成片未改的行,并把折掉的那些留在 gap 上——点一下就地展开,不必回文件里再找一遍。
function hsRvFold(rows) {
  const keep = new Array(rows.length).fill(false);
  rows.forEach((r, i) => {
    if (r.kind === "ctx") return;
    for (let k = Math.max(0, i - HS_RV_CTX); k <= Math.min(rows.length - 1, i + HS_RV_CTX); k++) keep[k] = true;
  });
  const out = [];
  let hid = null;
  rows.forEach((r, i) => {
    if (keep[i]) { hid = null; out.push(r); return; }
    if (!hid) { hid = { kind: "gap", hidden: [] }; out.push(hid); }
    hid.hidden.push(r);
  });
  return out;
}

// 行号取自 unified diff 的 hunk 头(codex 那路本来就有)。claude 的 Edit 入参里没有位置,
// 号码留空——编一个不如不写,这与卡片上的口径一致。
function hsRvNumber(rows) {
  let n = 0;
  for (const r of rows) {
    if (r.kind === "meta") {
      const m = /@@\s*-\d+(?:,\d+)?\s*\+(\d+)/.exec(r.text || "");
      if (m) n = parseInt(m[1], 10);
      r.n = "";
    } else if (r.kind === "gap") {
      r.n = "";
      if (n) n += (r.hidden || []).length;
    } else if (r.kind === "del") {
      r.n = "";                       // 删掉的行在新文件里不存在,不给号
    } else {
      r.n = n ? String(n++) : "";
    }
  }
  return rows;
}

function hsRvRowHtml(r) {
  if (r.kind === "gap") {
    const k = (r.hidden || []).length;
    return `<div class="hs-rv-gap">${k ? k + " 行未改动 · 点开展开" : "⋯"}</div>`;
  }
  const mark = r.kind === "add" ? "+" : r.kind === "del" ? "-" : " ";
  return `<div class="hs-dl ${r.kind}"><span class="hs-dn">${r.n || ""}</span>`
    + `<span class="hs-dm">${r.kind === "meta" ? " " : mark}</span>`
    + `<span class="hs-dt">${hsEsc(r.text || "")}</span></div>`;
}

function hsRvPaintFiles(v) {
  const rv = v.rv, host = v.el.querySelector(".hs-rv-list");
  if (!host) return;
  const kw = (v.el.querySelector(".hs-rv-filter").value || "").trim().toLowerCase();
  host.innerHTML = "";
  rv.files.forEach((f, i) => {
    if (kw && !f.path.toLowerCase().includes(kw)) return;
    const d = document.createElement("div");
    d.className = "hs-rv-f" + (i === rv.cur ? " on" : "");
    d.innerHTML = `<span class="hs-rv-fp">${hsEsc(f.path)}</span>`
      + (f.edits > 1 ? `<span class="hs-rv-fn">${f.edits} 次编辑</span>` : "")
      + `<span class="hs-rv-fs"><span class="hs-cadd">${f.add ? "+" + f.add : ""}</span>`
      + `<span class="hs-cdel">${f.del ? "-" + f.del : ""}</span></span>`;
    d.title = f.path;
    d.onclick = () => { rv.cur = i; hsRvPaintFiles(v); hsRvPaintBody(v); };
    host.appendChild(d);
  });
}

function hsRvPaintBody(v) {
  const rv = v.rv, box = v.el.querySelector(".hs-rv-body");
  const f = rv.files[rv.cur];
  if (!box) return;
  if (!f) { box.innerHTML = '<div class="hs-rv-empty">这一轮没有文件改动</div>'; return; }
  const rows = hsRvNumber(hsRvFold(f.rows));
  box.innerHTML = `<div class="hs-dfile">${hsEsc(f.path)}</div>`
    + rows.map(hsRvRowHtml).join("");
  box.scrollTop = 0;
  // 折叠条就地展开:把留着的那几行铺回原位,不重画整篇(重画会把滚动位置带走)
  [...box.querySelectorAll(".hs-rv-gap")].forEach((g, i) => {
    const gap = rows.filter((r) => r.kind === "gap")[i];
    if (!gap || !(gap.hidden || []).length) return;
    g.onclick = () => {
      const html = hsRvNumber(gap.hidden.map((r) => ({ ...r }))).map(hsRvRowHtml).join("");
      g.outerHTML = html;
    };
  });
}

function hsRvOpen(v, t, path) {
  const files = hsRvCollect(t);
  if (!files.length) return;
  if (v.larkRef) hsLarkRefClose(v);
  const idx = Math.max(0, files.findIndex((f) => f.path === path));
  const opts = (t && t._rvOpts) || {};
  v.rv = { turn: opts.session ? null : t, files, cur: idx };
  const el = v.el.querySelector(".hs-rv");
  el.hidden = false;
  v.el.querySelector(".hs-rv-dv").hidden = false;
  v.el.classList.add("rv-on");
  const add = files.reduce((n, f) => n + f.add, 0), del = files.reduce((n, f) => n + f.del, 0);
  el.querySelector(".hs-rv-t").textContent = opts.title || "本轮改动";
  el.querySelector(".hs-rv-s").innerHTML =
    `<span class="hs-cadd">+${add}</span><span class="hs-cdel">-${del}</span>`;
  // 撤销就是现成的回退,按能力位显隐:codex 那路没有文件检查点,给了也点不动
  const un = el.querySelector(".hs-rv-undo");
  un.hidden = !!opts.session || !(v.caps && v.caps.rewind);
  un.onclick = () => hsRewind(v, t);
  // 「在 IDE 打开」开的是此刻正在看的那一份,不是整个仓库:这一栏本来就是逐个文件读 diff,
  // 读到想动手的那份直接接上去。所以 onclick 里现取 v.rv,不在这里把路径闭包进去——
  // 面板不重开、只在左边换一份文件的情况比重开常见得多。
  const ide = el.querySelector(".hs-rv-ide");
  ide.hidden = typeof openFileInIde !== "function";
  ide.onclick = async () => {
    const f = (v.rv && v.rv.files[v.rv.cur]) || null;
    if (!f) return;
    // 路径是绝对的,hsResolveFile 会顺带换成仓库根 + 仓内相对路径(见 hsRepoRoot):
    // 编辑器那一页按 root 建标签,给它文件所在目录的话,同一个仓每换一层就多开一页。
    let loc = null;
    try { loc = await hsResolveFile(f.path, v.el); } catch {}
    if (loc && loc.root && loc.rel) openFileInIde(loc.root, loc.rel);
  };
  el.querySelector(".hs-rv-filter").value = "";
  hsRvPaintFiles(v);
  hsRvPaintBody(v);
  hsRvMark(v);
}

function hsRvClose(v) {
  const el = v.el.querySelector(".hs-rv");
  if (el) el.hidden = true;
  const dv = v.el.querySelector(".hs-rv-dv");
  if (dv) dv.hidden = true;
  v.el.classList.remove("rv-on");
  v.rv = null;
  hsRvMark(v);
}

// ---- 飞书消息引用侧栏 ------------------------------------------------------
// 与审阅同属会话的辅助视图，但不借应用页或浏览器页：点击回答里的消息引用时，
// 留在当前会话中读取原消息和邻近上下文。只有明确指向消息位置的深链才接管，普通群链接
// 仍按原规则打开，避免把「进群」误解成「查引用」。
function hsLarkRefParse(href) {
  let u;
  try { u = new URL(String(href || ""), location.href); } catch { return null; }
  const host = u.hostname.toLowerCase();
  if (!["feishu.cn", "larksuite.com"].some((d) => host === d || host.endsWith("." + d))) return null;
  const path = u.pathname.replace(/\/+$/, "");
  if (!["/client/chat/open", "/client/thread/open", "/client/message/link/open"].includes(path)) return null;
  const q = {};
  u.searchParams.forEach((value, key) => { q[key.toLowerCase()] = value; });
  const mid = q.messageid || q.message_id || ((String(href).match(/(?:^|[^A-Za-z0-9])(om_[A-Za-z0-9_-]+)/) || [])[1] || "");
  const pos = q.position || q.message_position || "";
  const thread = q.open_thread_id || q.openthreadid || "";
  if (path === "/client/chat/open" && !mid && !pos) return null;
  if (path === "/client/thread/open" && !thread) return null;
  if (path === "/client/message/link/open" && !q.token) return null;
  return { url: u.href };
}

function hsLarkRefOpenFromLink(href, source) {
  const ref = hsLarkRefParse(href);
  if (!ref || !source || !source.closest) return false;
  const page = source.closest(".hs-page");
  const v = page && page._hsView;
  if (!v) return false;
  hsLarkRefOpen(v, ref.url);
  return true;
}

const _hsLarkContextWait = new Map();
window.__larkRefContextResult = (payload) => {
  const key = String((payload || {}).requestId || "");
  const pending = _hsLarkContextWait.get(key);
  if (!pending) return;
  _hsLarkContextWait.delete(key);
  clearTimeout(pending.timer);
  if (payload && payload.ok && payload.data) pending.resolve(payload.data);
  else pending.reject(new Error(String((payload || {}).error || "未能读取当前飞书聊天")));
};

function hsLarkRefNativeId(v) { return "lark-ref:" + String(v.sid || "draft"); }

function hsLarkRefNativeHost(id) {
  if (typeof _hsViews === "undefined") return null;
  for (const v of _hsViews.values()) {
    if (v && v.larkRef && hsLarkRefNativeId(v) === id) return v.el.querySelector(".hs-lr-web");
  }
  return null;
}

function hsLarkRefNativeRect(host) {
  if (!host) return null;
  const r = host.getBoundingClientRect();
  return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width),
    h: Math.round(r.height), passThroughLeft: 7 };
}

function hsLarkRefNativeSync(v, reopen) {
  if (typeof NATIVE_BROWSER === "undefined" || !NATIVE_BROWSER || typeof nb !== "function") return false;
  const state = v && v.larkRef;
  const host = v && v.el.querySelector(".hs-lr-web");
  const id = state ? hsLarkRefNativeId(v) : "";
  const visible = !!(state && state.mode === "original" && host && !host.hidden
    && host.offsetParent !== null && state.url);
  if (!visible) {
    if (state && state.nativeId) nb({ action: "hideOne", id: state.nativeId });
    return false;
  }
  const rect = hsLarkRefNativeRect(host);
  if (!rect || rect.w <= 0 || rect.h <= 0) return false;
  if (state.nativeId && state.nativeId !== id) nb({ action: "close", id: state.nativeId });
  if (reopen || state.nativeId !== id || state.nativeUrl !== state.url) {
    if (state.nativeId === id && state.nativeUrl && state.nativeUrl !== state.url) {
      nb({ action: "close", id });
    }
    state.nativeId = id;
    state.nativeUrl = state.url;
    nb({ action: "open", id, url: state.url, rect, nativeMode: "lark-message-reference" });
  } else nb({ action: "rect", id, rect });
  return true;
}

function hsLarkRefNativeClose(v) {
  const state = v && v.larkRef;
  if (state && state.nativeId && typeof nb === "function") nb({ action: "close", id: state.nativeId });
  if (state) { state.nativeId = ""; state.nativeUrl = ""; }
}

function hsLarkRefMode(v, mode) {
  const state = v && v.larkRef;
  if (!state) return;
  const original = mode === "original";
  state.mode = original ? "original" : "preview";
  v.el.querySelectorAll(".hs-lr-mode").forEach((b) => {
    const on = b.dataset.mode === state.mode;
    b.classList.toggle("on", on); b.setAttribute("aria-selected", String(on));
  });
  const body = v.el.querySelector(".hs-lr-body");
  const web = v.el.querySelector(".hs-lr-web");
  const bar = v.el.querySelector(".hs-lr-contextbar");
  body.hidden = original;
  web.hidden = !original;
  bar.hidden = !original;
  const native = typeof NATIVE_BROWSER !== "undefined" && NATIVE_BROWSER;
  web.classList.toggle("native", native);
  const add = v.el.querySelector(".hs-lr-context-add");
  if (add) add.disabled = !native;
  if (original && native) requestAnimationFrame(() => hsLarkRefNativeSync(v, true));
  else if (state.nativeId && typeof nb === "function") nb({ action: "hideOne", id: state.nativeId });
  hsLarkRefContextState(v);
}

function hsLarkRefCapture(v) {
  const state = v && v.larkRef;
  if (!state || state.mode !== "original" || !state.nativeId || typeof nb !== "function") {
    return Promise.reject(new Error("请先打开飞书原页"));
  }
  const requestId = "lark_ctx_" + Date.now().toString(36) + Math.random().toString(36).slice(2);
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      _hsLarkContextWait.delete(requestId);
      reject(new Error("读取飞书聊天超时，请等待原页加载完成后重试"));
    }, 12000);
    _hsLarkContextWait.set(requestId, { resolve, reject, timer });
    nb({ action: "larkContext", id: state.nativeId, requestId });
  });
}

function hsLarkRefTextB64(text) {
  const bytes = new TextEncoder().encode(String(text || ""));
  let binary = "";
  for (let i = 0; i < bytes.length; i += 8192) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  }
  return btoa(binary);
}

function hsLarkRefContextState(v) {
  const b = v && v.el.querySelector(".hs-lr-context-add");
  if (!b || b.dataset.busy === "1") return;
  const attached = (v.atts || []).some((a) => a && a.type === "context" && a.context_kind === "lark_web");
  b.textContent = attached ? "已加入下一条消息" : "加入下一条消息";
  b.classList.toggle("on", attached);
}

async function hsLarkRefAddContext(v) {
  const b = v.el.querySelector(".hs-lr-context-add");
  if (!b || b.disabled || b.dataset.busy === "1") return;
  b.dataset.busy = "1"; b.disabled = true; b.textContent = "正在读取…";
  try {
    const data = await hsLarkRefCapture(v);
    const text = String(data.text || "").trim();
    if (!text) throw new Error("当前飞书窗口没有可读取的聊天正文");
    const title = String(data.title || v.el.querySelector(".hs-lr-chat").textContent || "飞书聊天").trim();
    const captured = new Date().toLocaleString();
    const content = `飞书聊天窗口：${title}\n读取时间：${captured}\n\n${text}`;
    const attachment = { type: "context", context_kind: "lark_web",
      name: "飞书聊天上下文.txt", media_type: "text/plain", data_b64: hsLarkRefTextB64(content) };
    v.atts = (v.atts || []).filter((a) => !(a && a.type === "context" && a.context_kind === "lark_web"));
    v.atts.push(attachment);
    if (typeof hsAttPaint === "function") hsAttPaint(v);
  } catch (error) {
    hsEnvironment(v, { state: "warning", stage: "lark-context",
      label: (error && error.message) || "未能读取当前飞书聊天" });
  } finally {
    b.dataset.busy = "0"; b.disabled = false; hsLarkRefContextState(v);
  }
}

function hsLarkRefLoading(v) {
  const body = v.el.querySelector(".hs-lr-body");
  v.el.querySelector(".hs-lr-chat").textContent = "正在读取原消息";
  body.innerHTML = '<div class="hs-lr-loading" aria-label="正在读取飞书消息">'
    + '<span></span><i></i><span></span><i></i><span></span><i></i></div>';
}

const HS_LARK_REACTION_EMOJI = {
  OK: "👌", THUMBSUP: "👍", THANKS: "🙏", MUSCLE: "💪", FINGERHEART: "🫰",
  APPLAUSE: "👏", FISTBUMP: "🤜", JIAYI: "➕", DONE: "✅", SMILE: "🙂",
  BLUSH: "😊", LAUGH: "🤣", LOL: "😆", FACEPALM: "🤦", LOVE: "❤️",
  WINK: "😉", THINKING: "🤔", SOB: "😭", CRY: "😢", WOW: "😮",
  PROUD: "😎", GLANCE: "👀", KISS: "😘", COMFORT: "🫂", FIRE: "🔥",
  PARTY: "🎉", HUNDRED: "💯", ROSE: "🌹", HEART: "❤️"
};

function hsLarkRefReactionItems(reactions) {
  if (!reactions || typeof reactions !== "object") return [];
  const counts = Array.isArray(reactions.counts) ? reactions.counts
    : (Array.isArray(reactions.reaction_count) ? reactions.reaction_count : []);
  const details = Array.isArray(reactions.details) ? reactions.details
    : (Array.isArray(reactions.message_reaction_items) ? reactions.message_reaction_items : []);
  const byType = new Map();
  counts.forEach((entry) => {
    if (!entry || typeof entry !== "object") return;
    const type = String(entry.reaction_type || entry.emoji_type || entry.type || "").toUpperCase();
    if (!type) return;
    byType.set(type, { type, count: Number(entry.count || entry.total || 0), names: [] });
  });
  details.forEach((entry) => {
    if (!entry || typeof entry !== "object") return;
    const type = String(entry.emoji_type || (entry.reaction_type || {}).emoji_type || "").toUpperCase();
    if (!type) return;
    const value = byType.get(type) || { type, count: 0, names: [] };
    value.count = Math.max(value.count, value.names.length + 1);
    const operator = entry.operator && typeof entry.operator === "object" ? entry.operator : {};
    const name = String(operator.name || operator.display_name || entry.operator_name || "");
    if (name && !value.names.includes(name)) value.names.push(name);
    byType.set(type, value);
  });
  if (!counts.length && reactions.counts && typeof reactions.counts === "object") {
    Object.entries(reactions.counts).forEach(([type, count]) => {
      byType.set(String(type).toUpperCase(), { type: String(type).toUpperCase(), count: Number(count || 0), names: [] });
    });
  }
  return Array.from(byType.values()).filter((item) => item.count > 0);
}

function hsLarkRefReactionsHtml(item) {
  const rows = hsLarkRefReactionItems(item.reactions);
  if (!rows.length) return "";
  return '<div class="hs-lr-reactions" aria-label="消息表情回应">' + rows.map((reaction) => {
    const emoji = HS_LARK_REACTION_EMOJI[reaction.type];
    const title = reaction.names.length ? reaction.names.join("、") : reaction.type;
    return `<span class="hs-lr-reaction" title="${hsEsc(title)}">`
      + `<b class="${emoji ? "" : "label"}" aria-hidden="true">${emoji || hsEsc(reaction.type)}</b>`
      + `<span>${reaction.count}</span></span>`;
  }).join("") + "</div>";
}

function hsLarkRefSameGroup(item, previous) {
  if (!previous || item.target || previous.target) return false;
  if (String(item.sender_id || item.sender || "") !== String(previous.sender_id || previous.sender || "")) return false;
  const current = Date.parse(String(item.create_time || "").replace(" ", "T"));
  const before = Date.parse(String(previous.create_time || "").replace(" ", "T"));
  return Number.isFinite(current) && Number.isFinite(before) && current - before >= 0 && current - before < 300000;
}

function hsLarkRefMessageHtml(item, previous) {
  const sender = String(item.sender || "未知成员");
  const initial = Array.from(sender.trim())[0] || "?";
  const when = String(item.create_time || "").replace(/^\d{4}-/, "");
  const meta = [when, item.updated ? "已编辑" : ""].filter(Boolean).join(" ");
  const grouped = hsLarkRefSameGroup(item, previous);
  const content = item.deleted ? "这条消息已撤回" : String(item.content || "");
  let rendered = "";
  try { rendered = md.render(content); } catch { rendered = `<p>${hsEsc(content)}</p>`; }
  const avatar = /^https?:\/\//.test(String(item.avatar || ""))
    ? `<img src="${hsEsc(item.avatar)}" alt="">` : hsEsc(initial);
  return `<article class="hs-lr-msg${item.target ? " target" : ""}${item.is_self ? " self" : ""}${grouped ? " grouped" : ""}">`
    + `<div class="hs-lr-avatar" aria-hidden="true">${grouped ? "" : avatar}</div>`
    + '<div class="hs-lr-msgmain">' + (grouped ? "" : '<div class="hs-lr-meta">'
    + `<strong>${hsEsc(sender)}</strong><span>${hsEsc(meta)}</span>`
    + (item.target ? '<em>引用位置</em>' : "") + "</div>"
    ) + `<div class="hs-lr-bubble"><div class="hs-lr-content md">${rendered}</div>`
    + `${hsLarkRefReactionsHtml(item)}</div></div></article>`;
}

function hsLarkRefPaint(v, data) {
  const body = v.el.querySelector(".hs-lr-body");
  const rows = Array.isArray(data.messages) ? data.messages : [];
  v.el.querySelector(".hs-lr-chat").textContent = data.chat_name || "飞书会话";
  if (!rows.length) {
    body.innerHTML = '<div class="hs-lr-empty"><strong>没有可显示的消息</strong>'
      + '<span>可以在飞书中打开原链接。</span></div>';
    return;
  }
  body.innerHTML = '<div class="hs-lr-context"><span>引用上下文</span></div>'
    + rows.map((item, index) => hsLarkRefMessageHtml(item, rows[index - 1])).join("");
  const target = body.querySelector(".hs-lr-msg.target");
  if (target) requestAnimationFrame(() => target.scrollIntoView({ block: "center" }));
}

function hsLarkRefError(v, error) {
  const body = v.el.querySelector(".hs-lr-body");
  const detail = String((error && (error.detail || error.message)) || "暂时无法读取这条消息")
    .replace(/^.*?→\s*\d+：?/, "");
  v.el.querySelector(".hs-lr-chat").textContent = "无法预览";
  body.innerHTML = '<div class="hs-lr-empty"><strong>没有取到原消息</strong>'
    + `<span>${hsEsc(detail)}</span><button type="button">在飞书中打开</button></div>`;
  const b = body.querySelector("button");
  if (b) b.onclick = () => openExternal(v.larkRef && v.larkRef.url);
}

async function hsLarkRefOpen(v, url) {
  if (v.rv) hsRvClose(v);
  if (v.larkRef) hsLarkRefNativeClose(v);
  const token = {};
  v.larkRef = { url, token, mode: "preview", nativeId: "", nativeUrl: "" };
  const panel = v.el.querySelector(".hs-lr");
  panel.hidden = false;
  v.el.querySelector(".hs-lr-dv").hidden = false;
  v.el.classList.add("lr-on");
  hsLarkRefMode(v, "preview");
  hsLarkRefLoading(v);
  try {
    const data = await api(`/workbench/lark-message?url=${encodeURIComponent(url)}`, { timeout: 45000 });
    if (!v.larkRef || v.larkRef.token !== token) return;
    v.larkRef.url = data.open_url || url;
    v.larkRef.chatName = data.chat_name || "";
    hsLarkRefPaint(v, data);
  } catch (error) {
    if (v.larkRef && v.larkRef.token === token) hsLarkRefError(v, error);
  }
}

function hsLarkRefClose(v) {
  hsLarkRefNativeClose(v);
  const panel = v.el.querySelector(".hs-lr");
  const divider = v.el.querySelector(".hs-lr-dv");
  if (panel) panel.hidden = true;
  if (divider) divider.hidden = true;
  v.el.classList.remove("lr-on");
  v.larkRef = null;
}

function hsLarkRefWire(v) {
  const el = v.el, divider = el.querySelector(".hs-lr-dv");
  let w = parseInt(localStorage.getItem("cd_lr_w") || "", 10);
  if (!(w >= HS_RV_MINW && w <= HS_RV_MAXW)) w = HS_RV_DEFW;
  el.style.setProperty("--lr-w", w + "px");
  el.querySelectorAll(".hs-lr-x, .hs-lr-back").forEach((b) => { b.onclick = () => hsLarkRefClose(v); });
  el.querySelector(".hs-lr-open").onclick = () => openExternal(v.larkRef && v.larkRef.url);
  el.querySelectorAll(".hs-lr-mode").forEach((b) => { b.onclick = () => hsLarkRefMode(v, b.dataset.mode); });
  el.querySelector(".hs-lr-context-add").onclick = () => hsLarkRefAddContext(v);
  let x0 = 0, w0 = 0;
  const move = (e) => {
    const next = Math.min(Math.max(w0 + (x0 - e.clientX), HS_RV_MINW), HS_RV_MAXW);
    el.style.setProperty("--lr-w", next + "px");
    if (v.larkRef && v.larkRef.mode === "original") hsLarkRefNativeSync(v, false);
  };
  const up = () => {
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
    document.body.classList.remove("fb-resizing");
    try { localStorage.setItem("cd_lr_w", parseInt(el.style.getPropertyValue("--lr-w"), 10)); } catch {}
  };
  divider.onmousedown = (e) => {
    e.preventDefault(); x0 = e.clientX; w0 = parseInt(el.style.getPropertyValue("--lr-w"), 10) || HS_RV_DEFW;
    document.body.classList.add("fb-resizing");
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  };
  if (typeof ResizeObserver !== "undefined") {
    v.larkRefResizeObserver = new ResizeObserver(() => {
      if (v.larkRef && v.larkRef.mode === "original") hsLarkRefNativeSync(v, false);
    });
    v.larkRefResizeObserver.observe(el.querySelector(".hs-lr-web"));
  }
}

// 哪张卡是当前正在看的:选中态只留一处,免得几张卡同时亮着
function hsRvMark(v) {
  const on = v.rv && v.rv.turn;
  v.el.querySelectorAll(".hs-step-r").forEach((b) => {
    b.classList.toggle("on", !!on && b._turn === on && b._path === (v.rv.files[v.rv.cur] || {}).path);
  });
}

// 中缝拖宽:宽度按会话页记一次,下次打开还是这个宽度
function hsRvWire(v) {
  const el = v.el, dv = el.querySelector(".hs-rv-dv");
  let w = parseInt(localStorage.getItem("cd_rv_w") || "", 10);
  if (!(w >= HS_RV_MINW && w <= HS_RV_MAXW)) w = HS_RV_DEFW;
  el.style.setProperty("--rv-w", w + "px");
  el.querySelectorAll(".hs-rv-x, .hs-rv-back").forEach((b) => {
    b.onclick = () => hsRvClose(v);
  });
  el.querySelector(".hs-rv-filter").oninput = () => hsRvPaintFiles(v);
  let x0 = 0, w0 = 0;
  const mv = (e) => {
    const nw = Math.min(Math.max(w0 + (x0 - e.clientX), HS_RV_MINW), HS_RV_MAXW);
    el.style.setProperty("--rv-w", nw + "px");
  };
  const up = () => {
    document.removeEventListener("mousemove", mv);
    document.removeEventListener("mouseup", up);
    document.body.classList.remove("fb-resizing");
    try { localStorage.setItem("cd_rv_w", parseInt(el.style.getPropertyValue("--rv-w"), 10)); } catch (e) {}
  };
  dv.onmousedown = (e) => {
    e.preventDefault(); x0 = e.clientX; w0 = parseInt(el.style.getPropertyValue("--rv-w"), 10) || HS_RV_DEFW;
    document.body.classList.add("fb-resizing");
    document.addEventListener("mousemove", mv);
    document.addEventListener("mouseup", up);
  };
}

// ---- 页内查找(⌘F)-----------------------------------------------------------
//
// 为什么不让浏览器自带的查找干这件事:它只看得见当前文档里的东西,而这一页有三层遮挡——
// 贴底时 hsTrimDom 会把靠上的回合从文档里摘走(节点还在 v.turns 上),处理中区、思考正文、
// 压缩摘要默认 display:none,更早的历史压根没铺(要往上翻页现取)。所以自己做一套,
// 至少能覆盖前两层:开查找时先 hsUnstick 把摘走的挂回来,命中落在折叠区就把祖先展开。
//
// 覆盖不到的那一层要说清楚:工具的命令全文、输出、diff 与子代理日志是展开那一刻才建节点的
// (见 hsStepDetailMount / hsStepFull),没展开就不在文档里,这里搜不到;更早的历史同理。
// 计数旁边因此标一句「已加载」,不让人以为搜的是整段会话。
//
// 高亮走 CSS 自定义高亮接口,与输入框那处命令着色同一套(见 hsCmdPaint):它只标区间、
// 不动 DOM,不会把正文的节点结构改掉,也不会碰到 markdown 渲染出来的那些标签。
// 老内核没有这个接口时静默降级成「只跳转不着色」,功能仍然可用。
const HS_FIND_HL = "hs-find";        // 全部命中
const HS_FIND_CUR = "hs-find-cur";   // 当前这一个

// 不进搜索范围的区域:刻度列、输入区、查找条自己,以及右侧的改动面板(它是另一套视图)。
const HS_FIND_SKIP = ".hs-toc, .hs-composer, .hs-find, .hs-rv, .hs-rv-dv, .hs-lr, .hs-lr-dv";

function hsFindHl(name) {
  if (!window.CSS || !CSS.highlights || typeof Highlight === "undefined") return null;
  let hl = CSS.highlights.get(name);
  if (!hl) { hl = new Highlight(); CSS.highlights.set(name, hl); }
  return hl;
}

// 收集命中。按文本节点逐个找,跨节点的匹配不认——正文被 markdown 拆成一堆行内标签,
// 跨节点拼接要维护一份偏移表,而搜到的关键词多半落在一个节点里,不值这个复杂度。
function hsFindScan(v, q) {
  const hits = [];
  if (!q) return hits;
  const needle = q.toLowerCase();
  // reasoning 折叠时会主动释放正文 DOM，避免手机为不可见长思考保留数万个节点。
  // 查找是明确的全文读取动作：只把确实包含关键词的思考块展开并重建，再交给统一的
  // TreeWalker/Range 高亮路径；未命中的长块仍不物化。
  for (const turn of v.turns || []) {
    for (const part of (turn.parts || new Map()).values()) {
      if (part.type !== "reasoning" || !part.el || part.el.classList.contains("open")) continue;
      if (!String(part.text || "").toLowerCase().includes(needle)) continue;
      part.el.classList.add("open");
      hsPaint(v, part);
    }
  }
  const walk = document.createTreeWalker(v.stream, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
      const el = node.parentElement;
      if (!el) return NodeFilter.FILTER_REJECT;
      // 折叠起来的内容照收:命中之后由 hsFindGo 把祖先展开。真正要跳过的只有
      // 那几块不属于会话正文的区域。
      if (el.closest(HS_FIND_SKIP)) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    const hay = n.nodeValue.toLowerCase();
    let at = hay.indexOf(needle);
    while (at >= 0) {
      const r = document.createRange();
      r.setStart(n, at);
      r.setEnd(n, at + needle.length);
      hits.push(r);
      at = hay.indexOf(needle, at + Math.max(1, needle.length));
      if (hits.length >= 2000) return hits;   // 上限:两千个之后再多也没人一个个翻
    }
  }
  return hits;
}

function hsFindPaint(v) {
  const all = hsFindHl(HS_FIND_HL), cur = hsFindHl(HS_FIND_CUR);
  if (all) { all.clear(); for (const r of v.findHits || []) all.add(r); }
  if (cur) {
    cur.clear();
    const r = (v.findHits || [])[v.findAt];
    if (r) cur.add(r);
  }
  const n = (v.findHits || []).length;
  const box = v.el.querySelector(".hs-find-n");
  if (box) box.textContent = n ? `${v.findAt + 1}/${n}` : (v.findQ ? "无匹配" : "");
  for (const sel of [".hs-find-prev", ".hs-find-next"]) {
    const b = v.el.querySelector(sel);
    if (b) b.disabled = n < 2;
  }
}

// 跳到第 i 个命中。折叠区要先展开——range 落在 display:none 里量不出位置,
// 直接滚等于滚到 0。三类折叠各有各的开关类名,统一在这里放开。
function hsFindGo(v, i) {
  const hits = v.findHits || [];
  if (!hits.length) return;
  v.findAt = ((i % hits.length) + hits.length) % hits.length;
  const r = hits[v.findAt];
  const host = r.startContainer.parentElement;
  if (host) {
    for (const sel of [".hs-proc", ".hs-think", ".hs-cmp"]) {
      const box = host.closest(sel);
      if (box) box.classList.add("open");
    }
  }
  hsFindPaint(v);
  const rect = r.getBoundingClientRect();
  const view = v.body.getBoundingClientRect();
  if (rect.height || rect.width) {
    // 摆到视口三分之一高的位置,不是正中:命中往往在一段话中间,上面留少一点、
    // 下面留多一点,接着往下读不用马上再滚。
    hsViewportReveal(v, r, "third");
  }
}

function hsFindRun(v, q, keepAt) {
  v.findQ = q;
  const before = (v.findHits || [])[v.findAt] || null;
  v.findHits = hsFindScan(v, q);
  if (!v.findHits.length) { v.findAt = 0; hsFindPaint(v); return; }
  // 重新搜之后尽量停在原地:改的往往是关键词末尾一两个字,位置跳回开头很扰人。
  let at = 0;
  if (keepAt && before) {
    const i = v.findHits.findIndex((r) => r.startContainer === before.startContainer
                                          && r.startOffset >= before.startOffset);
    if (i >= 0) at = i;
  }
  hsFindGo(v, at);
}

function hsFindOpen(v) {
  const bar = v.el.querySelector(".hs-find");
  if (!bar) return;
  hsUnstick(v);   // 把 hsTrimDom 摘走的回合挂回来,顺带停掉贴底(跳转时别被拽回底部)
  bar.hidden = false;
  const q = bar.querySelector(".hs-find-q");
  // 选中已有的词:连着按两次 ⌘F 是「换个词再搜」,不是「接着上次那个词打字」。
  q.focus(); q.select();
  if (q.value) hsFindRun(v, q.value, false);
}

function hsFindClose(v) {
  const bar = v.el.querySelector(".hs-find");
  if (!bar || bar.hidden) return;
  bar.hidden = true;
  v.findHits = []; v.findAt = 0; v.findQ = "";
  hsFindPaint(v);
  if (v.inp) v.inp.focus();
}

function hsFindWire(v) {
  const bar = v.el.querySelector(".hs-find");
  if (!bar) return;
  const q = bar.querySelector(".hs-find-q");
  let t = 0;
  q.addEventListener("input", () => {
    clearTimeout(t);
    // 防抖:整页扫一遍在长会话上要几十毫秒,每敲一个字符扫一次会顿。
    t = setTimeout(() => hsFindRun(v, q.value, true), 120);
  });
  q.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      // 回车走下一个;按住 shift 回上一个。没搜过就先搜一次(防抖还没到点时也能立刻走)
      clearTimeout(t);
      if (q.value !== v.findQ) { hsFindRun(v, q.value, false); return; }
      hsFindGo(v, v.findAt + (ev.shiftKey ? -1 : 1));
      return;
    }
    if (ev.key === "Escape") {
      // 必须拦住:文档上那个 Esc 监听是捕获阶段的会话中止(见 v.docKey),
      // 不拦的话关个查找条会顺手把正在跑的回合停掉。
      ev.preventDefault(); ev.stopPropagation();
      hsFindClose(v);
    }
  });
  bar.querySelector(".hs-find-prev").onclick = () => hsFindGo(v, v.findAt - 1);
  bar.querySelector(".hs-find-next").onclick = () => hsFindGo(v, v.findAt + 1);
  bar.querySelector(".hs-find-x").onclick = () => hsFindClose(v);

  // ⌘F 开查找。Ctrl+F 在输入框里是那套 emacs 键位的「光标右移一格」(见 hsEditKey),
  // 抢过来会破坏手感,所以焦点在输入框时只认 ⌘;在别处两个都认(Windows/Linux 没有 ⌘)。
  v.findKey = (ev) => {
    if (ev.key !== "f" && ev.key !== "F") return;
    if (!ev.metaKey && !ev.ctrlKey) return;
    if (!v.el.isConnected) { document.removeEventListener("keydown", v.findKey, true); return; }
    if (!(pActive && pActive.el === v.el) || v.el.style.display === "none") return;
    if (!ev.metaKey && v.inp && v.inp.contains(ev.target)) return;   // 输入框里的 Ctrl+F 留给 readline
    ev.preventDefault();
    hsFindOpen(v);
  };
  document.addEventListener("keydown", v.findKey, true);
}
