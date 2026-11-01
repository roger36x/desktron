// 由 app-chat.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 10/13 片
// ---- 斜杠命令 --------------------------------------------------------------
// 命令表由后端按当前会话解析。Desk 动作在界面执行，适配器发现的原生命令只在普通
// 原生会话里交给该引擎；Shepherd 不把一家引擎的私有命令带进另一家。打 / 弹窗，
// 继续打字过滤，上下键选择，回车执行。

// ---- 输入框键位(对齐 TUI) ------------------------------------------------
// TUI 的输入行走 readline 那套,肌肉记忆是跨引擎通用的;GUI 里不支持就会一直误触。
// 光标移动与按单位删除用 Selection.modify(WebKit/Blink 都有),它按渲染后的行与词计算,
// 比自己数字符靠谱——中文、软换行、emoji 都不用特判。
// 历史用本会话已发出的用户消息,Ctrl+P 往前、Ctrl+N 往回,任何输入都重置游标。
function hsSelMod(alter, dir, gran) {
  const sel = getSelection();
  if (sel && sel.modify) { try { sel.modify(alter, dir, gran); return true; } catch {} }
  return false;
}
function hsKill(dir, gran) {
  if (!hsSelMod("extend", dir, gran)) return false;
  const sel = getSelection();
  if (sel && !sel.isCollapsed) document.execCommand("delete");
  return true;
}
function hsHistory(v, step) {
  const list = v.turns.map((t) => t.user).filter(Boolean);
  if (!list.length) return true;
  // 头一次翻进历史时,先把手上正在写的收起来:翻到底(next === list.length)是回到它,
  // 不是清空。原来一律填空串,于是按一下 ↑ 再按一下 ↓,写了一半的那段就没了,
  // 而 ↑ 本身不给任何提示说它会吃掉当前输入(2026-08-21 用户实报)。
  // 取值走 hsInpText 而不是 textContent:contenteditable 的换行有三种形态,
  // textContent 只认其中一种,多行草稿存出来会连成一整行(同 hsInpText 的说明)。
  if (v.histIdx === undefined || v.histIdx < 0) {
    v.histIdx = list.length;
    v.histDraft = hsInpText(v.inp);
  }
  const next = v.histIdx + step;
  if (next < 0 || next > list.length) return true;
  v.histIdx = next;
  v.inp.textContent = next === list.length ? (v.histDraft || "") : list[next];
  hsCmdPaint(v);
  const sel = getSelection(), r = document.createRange();
  r.selectNodeContents(v.inp); r.collapse(false);
  sel.removeAllRanges(); sel.addRange(r);
  hsSlashHide(v);
  return true;
}
// 光标是否在最开头 / 最末尾:据此决定上下键是移动光标还是翻历史。
// 多行输入时中间按上下仍然只是移动光标,不会误触历史。
// 光标是不是已经在第一行 / 最后一行。上下键翻历史靠它决定要不要接管。
//
// 判据按几何算,不按「光标前面还有没有文字」:Range.toString() 不把 <br> 与空的块算成换行,
// 于是上方全是空行时它读出 0——人想按 ↑ 回到上面那个空行,却翻出了上一条指令,正在写的
// 这条被覆盖掉(2026-08-20 用户实见)。几何还顺带管住软折行:一句话折成两行,在第二行
// 按 ↑ 该回到第一行,而那时光标前面明明有文字,老判据同样答不对。
function hsAtEdge(el, back) {
  const sel = getSelection();
  if (!sel || !sel.isCollapsed || !sel.anchorNode || !el.contains(sel.anchorNode)) return false;
  const cr = sel.getRangeAt(0).getBoundingClientRect();
  const box = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  const line = parseFloat(cs.lineHeight) || 20;
  // 空输入框里量不到光标矩形(没有任何行盒),那本来就是边界
  if (!cr || (!cr.height && !cr.top)) return true;
  const half = line * 0.5;   // 半行的余量:行内还有 padding 与基线偏移,严格相等靠不住
  if (back) return cr.top - (box.top + (parseFloat(cs.paddingTop) || 0)) < half;
  return (box.bottom - (parseFloat(cs.paddingBottom) || 0)) - cr.bottom < half;
}

function hsEditKey(v, ev) {
  // 上下键翻历史:只在光标已经顶到头 / 顶到尾时才接管,和终端里的习惯一致
  if (!ev.isComposing && !ev.ctrlKey && !ev.metaKey && !ev.altKey && !ev.shiftKey) {
    if (ev.key === "ArrowUp" && hsAtEdge(v.inp, true)) return hsQPop(v) || hsHistory(v, -1);
    if (ev.key === "ArrowDown" && hsAtEdge(v.inp, false)) return hsHistory(v, 1);
  }
  if (!ev.ctrlKey || ev.metaKey || ev.altKey || ev.isComposing) return false;
  switch (ev.key.toLowerCase()) {
    case "a": return hsSelMod("move", "backward", "lineboundary");
    case "e": return hsSelMod("move", "forward", "lineboundary");
    case "b": return hsSelMod("move", "backward", "character");
    case "f": return hsSelMod("move", "forward", "character");
    case "k": return hsKill("forward", "lineboundary");
    case "u": return hsKill("backward", "lineboundary");
    case "w": return hsKill("backward", "word");
    case "d": return hsKill("forward", "character");
    case "h": return hsKill("backward", "character");
    case "t": {   // 交换光标前的两个字符,readline 的老习惯
      const sel = getSelection();
      if (!sel || !sel.isCollapsed) return false;
      hsSelMod("extend", "backward", "character");
      const one = String(sel.toString());
      if (!one) return true;
      document.execCommand("delete");
      hsSelMod("move", "backward", "character");
      document.execCommand("insertText", false, one);
      hsSelMod("move", "forward", "character");
      return true;
    }
    case "p": return hsQPop(v) || hsHistory(v, -1);
    case "n": return hsHistory(v, 1);
    case "l": case "c": v.inp.textContent = ""; hsSlashHide(v); return true;   // 清空输入框
    default: return false;
  }
}

function hsLastReplyText(v) {
  const t = [...v.turns].reverse().find((x) => x.parts.size);
  if (!t) return "";
  return [...t.parts.values()].filter((x) => x.type === "text")
    .map((x) => x.text || "").join("\n\n");
}

async function hsCompactPoll(v) {
  if (!v.manualCompact || !v.manualCompactID) return;
  try {
    const path = `/session/${encodeURIComponent(v.sid)}/compact/${encodeURIComponent(v.manualCompactID)}`;
    let row = await api(path);
    if (row.result === "not_found") {
      row = await api(path + "/cancel-unaccepted", { method: "POST" });
    }
    if (!row.running && row.result !== "not_found") hsCompactTerminal(v, row);
  } catch {}
  if (v.manualCompact) v.compactPollTimer = setTimeout(() => hsCompactPoll(v), 5000);
}

async function hsCompact(v) {
  if (v.busy || !v.turnReady) { hsErrorNotice(v, "当前回合结束后再压缩", "compact"); return; }
  if (!(v.caps || {}).compact) { hsErrorNotice(v, "当前引擎不支持 GUI 压缩", "compact"); return; }
  if (!v.turns.length) hsRename(v, "/compact");
  hsStatusClear(v);
  v.stats = null;
  v.todoNow = v.todoNext = "";
  const turn = hsNewTurn(v, "/compact", "", [], Date.now());
  v.manualCompactTurn = turn;
  // Unified Shepherd records compaction as a real archive turn. Claim the
  // authoritative user id instead of rendering its SSE echo as a second turn.
  if (v.shepherdMode) turn.mine = true;
  hsBusy(v, true);
  v.turnReady = false;
  hsSetStick(v, true);
  hsTouch(v);
  hsStatus(v, "working", "正在压缩上下文");
  hsScroll(v);
  hsCompactOn(v, Date.now());
  v.cmpLive = true;
  // 手动产品操作由外层 Shepherd 完成态收口。原生引擎在内部仍可能上报一次自动压缩，
  // 那只是实现细节，不能抢先消费这次手动操作的分隔线。
  v.manualCompact = true;
  v.manualCompactID = v.shepherdMode
    ? "compact_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2) : "";
  let waitsForTerminal = false;
  try {
    const result = await api(`/session/${encodeURIComponent(v.sid)}/compact`, {
      method: "POST", headers: { "content-type": "application/json" },
      ...(v.shepherdMode ? { body: JSON.stringify({ operation_id: v.manualCompactID }) } : {}),
      timeout: 300000 });
    // Shepherd only acknowledges admission here. The operation may run for
    // minutes; session.compact and /state own its durable terminal projection.
    if (v.shepherdMode && result.accepted) {
      waitsForTerminal = true;
      await hsCompactPoll(v);
      return;
    }
    hsCompactTerminal(v, result);
  } catch (e) {
    if (v.shepherdMode && !e?.status) {
      // A phone/proxy can drop the HTTP request while the accepted compaction
      // keeps running. Never translate transport loss into a model failure.
      waitsForTerminal = true;
      hsStatus(v, "working", "压缩连接中断，正在核对结果");
      v.compactPollTimer = setTimeout(() => hsCompactPoll(v), 0);
      return;
    }
    hsCompactOff(v);
    hsErrorNotice(v, "压缩失败：" + (e && e.message ? e.message : e), "compact");
    hsStatus(v, "error", "已停止");
  } finally {
    if (!waitsForTerminal) {
      v.manualCompact = false;
      v.manualCompactID = "";
      if (v.compactPollTimer) clearTimeout(v.compactPollTimer);
      v.compactPollTimer = 0;
      hsBusy(v, false);
      v.turnReady = true;
      hsQFlush(v);
    }
  }
}

const HS_COMMAND_FALLBACK = [
  ["/new", "新建一段对话", "ui"], ["/copy", "复制上一条回复", "ui"],
  ["/task", "记一条任务到当前工作流", "endpoint"],
  ["/interrupt", "打断当前回合", "endpoint", true],
  ["/unsend", "撤回刚发出的那句", "endpoint", true],
].map(([name, description, transport, control]) => ({
  name, description, control: !!control, owner: "desk", transport, history: "none",
}));

function hsCommandHandler(name) {
  return {
    "/new": () => newHtmlSession(),
    "/fork": (v) => { const t = v.turns[v.turns.length - 1]; if (t) hsFork(v, t); },
    "/copy": (v) => { const s = hsLastReplyText(v); if (s) hsCopy(s); },
    "/task": (v, args) => hsTaskQuickAdd(v, args),
    "/interrupt": (v) => hsInterrupt(v),
    "/unsend": (v) => hsUnsend(v),
    "/mode": (v) => hsPickMode(v, v.el.querySelector(".hs-mode")),
    "/plan": (v) => hsSetOpt(v, { collaboration_mode: "plan" }),
    "/permissions": (v) => hsPickSandbox(v, v.el.querySelector(".hs-sandbox")),
    "/model": (v) => hsPickModel(v, v.el.querySelector(".hs-model")),
    "/effort": (v) => hsPickEffort(v, v.el.querySelector(".hs-model")),
    "/compact": (v) => hsCompact(v),
    "/clear": (v) => { if (v.engine === "claude") v.inp.textContent = ""; else newHtmlSession(); },
    "/login": (v) => hsLoginShell(v, "login"),
    "/logout": (v) => hsLoginShell(v, "logout"),
  }[name];
}

function hsCommandRows(v) {
  const manifest = v && v.commandManifest;
  return manifest && manifest.version === 1 && Array.isArray(manifest.commands)
    ? manifest.commands : HS_COMMAND_FALLBACK;
}

// 撤回:把刚发出去的那句话拿回输入框重编辑。
//
// 只在这一轮还没产出任何东西时才办。已经开始产出就不办,也不退成打断——后端那侧
// 「末尾不是真人发言就不摘」(见 _pop_last_user),硬撤的话它不会摘那条消息,而界面这边
// 已经把它从流里删掉并填回了输入框:记录里还留着一份,框里又有一份,下一次对账就重了。
// 与其做一半,不如说清楚这一步现在办不了,让人自己选打断。
function hsUnsend(v) {
  if (!v.busy) { hsErrorNotice(v, "没有在跑的回合，没什么可撤", "command"); return; }
  const t = v.turns[v.turns.length - 1];
  const fresh = !!t && !t.parts.size && !(v.queue || []).length;
  if (!fresh) {
    hsErrorNotice(v, "这一轮已经开始产出，收不回来了；要停用 /interrupt", "command");
    return;
  }
  return hsInterrupt(v);   // fresh 那一支走的就是撤回
}

async function hsTaskQuickAdd(v, args) {
  const title = String(args || "").trim();
  if (!title) { hsErrorNotice(v, "用法：/task 任务标题", "command"); return; }
  const dom = v.taskDom || (await hsSessionDomain(v));
  if (!dom) { hsErrorNotice(v, "这段会话不属于任何工作流，记不了任务", "command"); return; }
  try { await taskAdd(dom, title); } catch (e) {
    hsErrorNotice(v, "记任务失败：" + ((e && e.message) || e), "command");
    return;
  }
  hsStatus(v, "done", "已记下:" + hsEllipsis(title, 30));
  hsTasksSync(v);   // 立即把新任务同步进已经常驻的栏子
}

function hsLocalSlash(v) {
  return hsCommandRows(v).filter((row) => row.owner === "desk").map((row) => ({
    n: row.name, d: row.description || "", ctl: !!row.control, fn: hsCommandHandler(row.name),
  })).filter((row) => row.fn);
}

function hsSlashLookup(v, text) {
  const m = String(text || "").trim().match(/^(\/[\w:-]+)(?:\s+([\s\S]*))?$/);
  if (!m) return null;
  const name = m[1].toLowerCase();
  const cmd = hsSlashAll(v).find((c) => !c.eng && c.n.toLowerCase() === name);
  return cmd ? { cmd, args: m[2] || "" } : null;
}

function hsRunLocalSlash(v, c, args) {
  if (!c) return;
  const out = c.fn(v, args || "");
  if (out && typeof out.catch === "function") {
    out.catch((e) => hsErrorNotice(v, String(e && e.message ? e.message : e), "command"));
  }
}

// 后端的 command_manifest 已经按当前会话解析命令归属、传输方式和能力位。Shepherd
// 只接 Desk 自己实现的命令；普通原生模式才附加该引擎确认过的 native_prompt 命令。
function hsSlashAll(v) {
  const rows = hsCommandRows(v);
  const eng = rows.filter((row) => row.owner !== "desk").map((row) => ({
    n: row.name, d: row.description || "", eng: true,
  }));
  // 忙闲与刚加载的模型目录属于当前视图的瞬时状态；命令归属和传输方式只认 Manifest。
  const local = hsLocalSlash(v).filter((c) => {
    if (c.n === "/model") return !!(hsModels(v).length || hsEfforts(v).length);
    if (c.n === "/effort") return !!hsEfforts(v).length;
    // 控制类只在有东西可打断时出现。空闲时列出来,点了什么也不会发生——
    // 与上面那几条按能力位过滤是同一个道理。
    if (c.ctl) return !!v.busy;
    return true;
  });
  const own = new Set(local.map((c) => c.n));
  // 同名时本地赢——打断这种事必须赢。但不能赢得无声无息:引擎那条会从补全里整个消失,
  // 而 claude 那份清单就是装了的 skill,会长。撞上了记一笔,至少查得到。
  const hidden = eng.filter((c) => own.has(c.n));
  if (hidden.length && v && !v._slashShadowSaid) {
    v._slashShadowSaid = true;
    if (typeof deskLog === "function") {
      deskLog("[hs] 同名的 Desk 命令盖住了引擎命令:" + hidden.map((c) => c.n).join(" "));
    }
  }
  return local.concat(eng.filter((c) => !own.has(c.n)));
}

// 输入框里打对了的命令换色。用 CSS 自定义高亮接口按区间着色,不动 DOM——
// 输入框是 contenteditable,重写 innerHTML 要自己还原光标,中文输入法组字期间还会被打断;
// 高亮接口只标区间,这两样都不会发生。接口不存在的老内核静默跳过,只是没有颜色。
const HS_CMD_HL = "hs-cmd";

// 发送钮的可发态。判据与 hsSend 的入口一致:有字或有附件都算,只挂了张图同样发得出去。
// 挂在 hsCmdPaint / hsDraftSave / hsAttPaint 三处的开头,是因为「内容变了」这件事没有
// 单一入口——打字、粘贴、翻历史、取回排队消息、发完清空、增删附件各走各的路,
// 这三个函数合起来盖住了全部,漏一条就会出现「写了字但钮还是灰的」。
function hsSendSync(v) {
  const btn = v && v.el && v.el.querySelector(".hs-send");
  if (!btn || !v.inp) return;
  btn.classList.toggle("on", !!(hsInpText(v.inp).trim() || (v.atts || []).length));
}
function hsCmdPaint(v) {
  hsSendSync(v);   // 放在早退之前:没有 CSS Highlight 的浏览器上,这一档照样要跟着变
  if (!window.CSS || !CSS.highlights || typeof Highlight === "undefined") return;
  let hl = CSS.highlights.get(HS_CMD_HL);
  if (!hl) { hl = new Highlight(); CSS.highlights.set(HS_CMD_HL, hl); }
  hl.clear();
  const node = v.inp.firstChild;
  if (!node || node.nodeType !== 3) return;
  // 只认开头那一段,且要在命令表里真有这一条:打到一半的 /go 不着色,
  // 着了色就等于说「这条能用」,而它还不能。
  const m = (v.inp.textContent || "").match(/^\/[\w:-]+/);
  if (!m) return;
  if (!hsSlashAll(v).some((c) => c.n.toLowerCase() === m[0].toLowerCase())) return;
  const r = document.createRange();
  r.setStart(node, 0);
  r.setEnd(node, Math.min(m[0].length, node.textContent.length));
  hl.add(r);
}

function hsSlashSync(v) {
  const txt = v.inp.textContent || "";
  const m = txt.match(/^\/([\w-]*)$/);
  if (!m) { hsSlashHide(v); return; }
  const kw = m[1].toLowerCase();
  const hits = hsSlashAll(v).filter((c) => c.n.slice(1).toLowerCase().startsWith(kw)).slice(0, 40);
  if (!hits.length) { hsSlashHide(v); return; }
  if (!v.slashEl) {
    v.slashEl = document.createElement("div");
    v.slashEl.className = "hs-slash";
    v.el.querySelector(".hs-box").appendChild(v.slashEl);
  }
  v.slashHits = hits;
  v.slashIdx = Math.min(v.slashIdx || 0, hits.length - 1);
  v.slashEl.innerHTML = "";
  hits.forEach((c, i) => {
    const row = document.createElement("div");
    row.className = "hs-sl" + (i === v.slashIdx ? " on" : "");
    row.innerHTML = '<span class="hs-sl-n"></span><span class="hs-sl-d"></span>';
    row.querySelector(".hs-sl-n").textContent = c.n;
    row.querySelector(".hs-sl-d").textContent = c.d || "";   // 一句话说明,一行放不下就截断(见 .hs-sl-d)
    row.onmousedown = (ev) => { ev.preventDefault(); hsSlashRun(v, i); };
    v.slashEl.appendChild(row);
  });
}

function hsSlashHide(v) {
  if (v.slashEl) { v.slashEl.remove(); v.slashEl = null; }
  v.slashHits = null; v.slashIdx = 0;
}

function hsSlashRun(v, i) {
  const c = (v.slashHits || [])[i];
  hsSlashHide(v);
  if (!c) { v.inp.textContent = ""; return; }
  // 引擎命令不在这里执行:补全成「/goal 」等着补参数,回车照常发出去交给引擎。
  if (c.eng) { hsFill(v, c.n + " "); return; }
  v.inp.textContent = "";
  hsRunLocalSlash(v, c, "");
}

function hsSlashKey(v, ev) {
  const n = (v.slashHits || []).length;
  if (!n) return false;
  if (ev.key === "Escape") { hsSlashHide(v); return true; }
  if (ev.key === "ArrowDown") { v.slashIdx = (v.slashIdx + 1) % n; hsSlashSync(v); return true; }
  if (ev.key === "ArrowUp") { v.slashIdx = (v.slashIdx - 1 + n) % n; hsSlashSync(v); return true; }
  if (ev.key === "Enter" || ev.key === "Tab") { hsSlashRun(v, v.slashIdx); return true; }
  return false;
}
