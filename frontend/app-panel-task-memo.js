// 由 app-panel.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 4/5 片
// 任务行的操作菜单。工作流页的「…」、工作流页的右键、会话页任务栏的右键共用这一份——
// 三处的入口不同,能做的事应当一致。
// opts.flow:「编辑 / 创建任务流」的落点。工作流页给行内编辑器;别处没有那块画布,
// 给一个跳去工作流页的动作,由调用方提供。
function wfTaskMenuItems(t, domId, stages, opts = {}) {
  const editFlow = opts.flow || (() => {});
  const items = [
    { ico: ICONS.plus, label: DeskI18n.t("wf.newConversation"), fn: () => _wfTaskNewSession(t, domId) },
    // 任务流是一张节点图,用分叉那一枚;编辑与创建是同一个落点,图标也不分两枚。
    { ico: ICONS.branch, label: DeskI18n.t(stages.length ? "task.editFlow" : "task.createFlow"), fn: editFlow },
    // 改名走共用的那一份(app-core 的 taskRename),提示语与别处同一句
    { ico: ICONS.pencil, label: DeskI18n.t("menu.rename"), fn: () => taskRename(domId, t) },
  ];
  // 状态三档各占一枚:退回待办用回环箭头(从完成退、从进行中退是同一件事,同一枚),
  // 进行中用脉冲线,完成用勾。
  if (t.state === "done") items.push({ ico: ICONS.resume, label: DeskI18n.t("task.restore"), fn: () => taskSetState(domId, t, "todo") });
  else items.push(
    { ico: t.state === "doing" ? ICONS.resume : ICONS.activity,
      label: DeskI18n.t(t.state === "doing" ? "task.backToTodo" : "task.markDoing"),
      fn: () => taskSetState(domId, t, t.state === "doing" ? "todo" : "doing") },
    { ico: ICONS.check, label: DeskI18n.t("task.markDone"), fn: () => taskSetState(domId, t, "done") });
  if (stages.length) items.push({ ico: ICONS.trash, label: DeskI18n.t("task.deleteFlow"), danger: true, fn: () => {
    _wfTasksPost(domId, { op: "set", line: t.line, expect: t.title, flow: "" });
  } });
  items.push({ ico: ICONS.trash, label: DeskI18n.t("task.delete"), danger: true, fn: async () => {
    // 确认句在共用的 taskDelete 里(四个入口同一句);备忘不跟着删,它留在备忘录里。
    await taskDelete(domId, t);
  } });
  return items;
}

// 任务详情浮层。原来是就地展开:一条任务展开之后,流程图加整篇备忘把列表顶出去半屏,
// 上下两条任务被推得很远,想对着看两条就得来回滚(2026-08-16 按用户要求改)。
// 现在点一行弹一张浮层,内容与原展开区相同(流程胶囊 + 备忘),列表本身一动不动。
//
// 复用而不是另写一套:流程图仍是 _wfFlowSvg,备忘仍是 _wfNoteMount——那两处各自带着
// 保存、右键关联、图片粘贴等一整套行为,重写必然漏。
let _wfSheet = null;   // 同一时刻只开一张

function wfCloseTaskSheet() {
  if (!_wfSheet) return;
  const el = _wfSheet;
  _wfSheet = null;
  document.removeEventListener("keydown", el._esc, true);
  document.removeEventListener("mousedown", el._out, true);
  window.removeEventListener("resize", el._fit);
  el.remove();
  if (el._onClose) { try { el._onClose(); } catch {} }
}

// 浮层只盖主区,不盖整窗:它是工作流页里的一张卡,压住侧栏读起来像是「整个应用被挡住了」,
// 而且侧栏此刻仍然可点(2026-08-16 用户实见)。遮罩仍按 #main 的矩形量；会话里详情卡
// 单独锚到正文列中心，不能把右侧实况卡或应用侧栏也算进视觉中心。非会话页退回主区中心。
// 用 fixed 而不是往 #asset-view 里塞 absolute:那个容器是滚动的,absolute 会跟着内容滚走。
function _wfSheetFit(back) {
  const host = document.getElementById("main") || document.body;
  const r = host.getBoundingClientRect();
  back.style.left = Math.round(r.left) + "px";
  back.style.top = Math.round(r.top) + "px";
  back.style.width = Math.round(r.width) + "px";
  back.style.height = Math.round(r.height) + "px";

  let center = r.left + r.width / 2;
  const page = (typeof pActive !== "undefined" && pActive && pActive.el &&
    pActive.el.classList.contains("hs-page") && pActive.el.isConnected) ? pActive.el : null;
  const lane = page && page.querySelector(".hs-stream");
  if (lane) {
    const lr = lane.getBoundingClientRect();
    if (lr.width > 0) center = lr.left + lr.width / 2;
  }
  const sheet = back.querySelector(".wf-sheet");
  const width = sheet ? sheet.getBoundingClientRect().width : 0;
  if (width > 0) {
    const edge = Math.min(32, Math.max(10, r.width / 20));
    center = Math.max(r.left + width / 2 + edge,
      Math.min(center, r.right - width / 2 - edge));
  }
  back.style.setProperty("--wf-sheet-shift-x",
    Math.round(center - (r.left + r.width / 2)) + "px");
}

// 通用浮层壳:任务详情与记忆卡共用。opts.lead 是标题左边那一枚(复选框之类),
// opts.titleAct 紧跟标题(例如记忆的编辑钮),opts.act 靠右、位于关闭钮之前;
// opts.onClose 在关掉时调一次(记忆卡靠它把借走的正文节点还回原处)。
function openFloatSheet(opts) {
  wfCloseTaskSheet();
  const back = document.createElement("div");
  back.className = "wf-sheet-back";
  const sheet = document.createElement("div");
  sheet.className = "wf-sheet";
  sheet.innerHTML = `<div class="wf-sheet-head">` +
      (opts.lead || "") +
      `<span class="wf-sheet-title-group"><span class="wf-sheet-title"></span>` +
        (opts.titleAct || "") +
      `</span>` +
      (opts.act || "") +
      `<button class="wf-sheet-x" title="关闭">${_WF_XMARK}</button>` +
    `</div><div class="wf-sheet-body"></div>`;
  sheet.querySelector(".wf-sheet-title").textContent = opts.title || "";
  const body = sheet.querySelector(".wf-sheet-body");
  sheet.querySelector(".wf-sheet-x").onclick = wfCloseTaskSheet;
  back._out = (ev) => {
    // Menus and the selected image's handle are portalled to document.body.
    if (ev.target.closest && ev.target.closest(".rp-ctx")) return;
    const thumb = ev.target.closest?.(".sb-thumb");
    if (thumb?._key && sheet.contains(thumb._key)) return;
    if (_wfImgSel && sheet.contains(_wfImgSel.img) && _wfImgSel.h.contains(ev.target)) return;
    if (!sheet.contains(ev.target)) wfCloseTaskSheet();
  };
  back._esc = (ev) => { if (ev.key === "Escape") { ev.stopPropagation(); wfCloseTaskSheet(); } };
  back._fit = () => _wfSheetFit(back);
  back._onClose = opts.onClose || null;
  document.addEventListener("mousedown", back._out, true);
  document.addEventListener("keydown", back._esc, true);
  window.addEventListener("resize", back._fit);
  back.appendChild(sheet);
  document.body.appendChild(back);
  _wfSheetFit(back);
  _wfSheet = back;
  return { back, sheet, body };
}

function _wfTaskMdValue(value) {
  return String(value || "").trim().replace(/\n/g, "\n  ");
}

function _wfTaskLedgerMarkdown(t) {
  const ledger = (t && t.ledger) || {};
  const lines = ["## 当前进展", ""];
  const fields = [["已完成", ledger.done], ["下一步", ledger.next], ["阻碍", ledger.blocker]];
  for (const [label, value] of fields) {
    const text = _wfTaskMdValue(value);
    if (text) lines.push(`- **${label}** ${text}`);
  }
  const criteria = ((ledger.contract || {}).criteria || []).filter((x) => x && x.summary);
  if (criteria.length) {
    const status = ledger.criterion_status || {};
    lines.push("- **验收** " + criteria.map((item) =>
      (status[item.id] === "passed" ? "✓ " : "") + _wfTaskMdValue(item.summary)).join("；"));
  }
  const sessions = (ledger.sessions || []).filter((x) => x && x.id).slice(0, 3);
  if (sessions.length) {
    const links = sessions.map((session, index) => {
      const label = String(session.title || session.engine || "会话").replace(/[\\\[\]]/g, "\\$&");
      return `[${label}](#wf-task-session-${index})`;
    });
    lines.push("- **关联会话** " + links.join("、"));
  }
  if (lines.length === 2) lines.push("还没有进展记录。");
  return { markdown: lines.join("\n"), sessions };
}

function _wfTaskMarkdownHtml(text, domId) {
  const source = String(text || "").replace(WF_IMG_RE, (_, alt, rel) =>
    `![${alt}](${_wfAssetSrc(domId, rel)})`);
  return md.render(source);
}

// 任务详情只有一块阅读面：账本快照与人工补充都按 Markdown 排版，不再拆成字段卡、标签和
// 第二张“补充资料”卡。当前保持纯阅读，修改仍由 Agent 的任务合同与备忘录流程完成。
async function _wfTaskMarkdownMount(host, domId, t) {
  host.replaceChildren();
  const box = document.createElement("section");
  box.className = "wf-task-markdown";
  const ledgerView = document.createElement("div");
  ledgerView.className = "wf-task-md-ledger md";
  const noteView = document.createElement("div");
  noteView.className = "wf-task-md-note md";
  box.append(ledgerView, noteView);
  host.appendChild(box);

  let noteBody = "";
  const paint = () => {
    const detail = _wfTaskLedgerMarkdown(t);
    ledgerView.innerHTML = md.render(detail.markdown);
    ledgerView.querySelectorAll('a[href^="#wf-task-session-"]').forEach((link) => {
      const index = Number((link.getAttribute("href") || "").split("-").pop());
      const session = detail.sessions[index];
      if (!session) return;
      link.title = "打开关联会话";
      link.onclick = (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        wfCloseTaskSheet();
        attachTerminal(session.id, session.engine || curEngine, { title: session.title || "" });
      };
    });
    noteView.innerHTML = noteBody.trim() ? _wfTaskMarkdownHtml(noteBody, domId) : "";
    noteView.hidden = !noteBody.trim();
  };
  host._wfTaskMarkdownRefresh = paint;
  paint();

  if (!t.note) return;
  try {
    const result = await _wfMemoPost(domId, t, "memo-get");
    if (!result || result.error || typeof result.text !== "string") throw new Error("invalid memo response");
    noteBody = _wfMemoBody(result.text);
    if (host.isConnected) paint();
  } catch {
    if (!host.isConnected) return;
    const error = document.createElement("div");
    error.className = "wf-note-err";
    error.textContent = "补充说明读取失败，进展信息仍可查看。";
    box.appendChild(error);
  }
}

function _wfTaskFlowMount(area, t, domId) {
  area.querySelector(":scope > .wf-flow")?.remove();
  const stages = _wfFlowStages(t);
  if (!stages.length) return;
  const doneSet = _wfFlowDone(t, stages);
  const capsule = document.createElement("div");
  capsule.className = "wf-flow";
  const links = _wfNodeLinks(t);
  const titleOf = (tid) => {
    const item = (_taskItems[domId] || []).find((x) => x.t === tid);
    return tid ? ((item && item.title) || "已关联") : "";
  };
  capsule.appendChild(_wfFlowSvg(stages, doneSet,
    (list) => _wfTasksPost(domId, { op: "set", line: t.line, expect: t.title, fdone: list }),
    (ev, si, ni) => _wfNodeLinkMenu(ev, domId, t, `${si}.${ni}`, () => {}),
    (si, ni) => titleOf(links.get(`${si}.${ni}`))));
  area.insertBefore(capsule, area.querySelector(":scope > .wf-task-note-section"));
}

async function wfRefreshTaskSheet(did) {
  const back = _wfSheet;
  const ref = back && back._taskRef;
  if (!ref || (did && did !== "*" && did !== ref.domId)) return false;
  let data;
  try { data = await _fetchTasks(ref.domId); } catch { return false; }
  if (_wfSheet !== back || !back.isConnected) return false;
  const current = (data.items || []).find((x) => (ref.t && x.t === ref.t)
    || (!ref.t && x.line === ref.line && x.title === ref.title));
  if (!current) { wfCloseTaskSheet(); return false; }

  const task = ref.task;
  // 保留同一个对象：菜单、流程回调与正在编辑的备忘都引用它。原位更新可让后续保存使用
  // 最新 line/title，避免任务被改名或前面插入新行后仍拿旧 expect 写回。
  for (const key of Object.keys(task)) if (!Object.prototype.hasOwnProperty.call(current, key)) delete task[key];
  Object.assign(task, current);
  const title = back.querySelector(".wf-sheet-title");
  if (title) title.textContent = task.title;
  const checkbox = back.querySelector(".wf-sheet-head > .wt-cb");
  if (checkbox) {
    checkbox.className = "wt-cb wf-cb-static" + (task.state === "done" ? " on" : (task.state === "doing" ? " doing" : ""));
    checkbox.innerHTML = taskCbSvg(task.state);
  }
  const area = back.querySelector(".wf-tflow");
  if (!area || typeof area._wfTaskMarkdownRefresh !== "function") return false;
  area._wfTaskMarkdownRefresh();
  back._taskRef = { domId: ref.domId, t: task.t || "", line: task.line,
                    title: task.title, task };
  return true;
}

if (!window._wfTaskSheetVisibility) {
  window._wfTaskSheetVisibility = true;
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && _wfSheet && _wfSheet._taskRef) wfRefreshTaskSheet("");
  });
}

function wfOpenTaskSheet(t, domId) {
  const { sheet, body } = openFloatSheet({
    title: t.title,
    lead: `<span class="wt-cb wf-cb-static${t.state === "done" ? " on" : (t.state === "doing" ? " doing" : "")}">${taskCbSvg(t.state)}</span>`,
    act: `<button class="wf-task-menu" title="任务操作">${_WF_DOTS}</button>`,
  });
  const back = _wfSheet;
  back._taskRef = { domId, t: t.t || "", line: t.line, title: t.title, task: t };

  const area = document.createElement("div");
  area.className = "wf-tflow";
  body.appendChild(area);
  _wfTaskMarkdownMount(area, domId, t);

  const menu = (ev) => {
    ev.preventDefault(); ev.stopPropagation();
    // 菜单每次按当前任务重算阶段；实时刷新会原位更新 t，不能继续捕获打开时的旧 stages。
    const stages = _wfFlowStages(t);
    openCtxMenu(ev.clientX, ev.clientY,
      wfTaskMenuItems(t, domId, stages, { flow: () => _wfFlowBuilder(sheet, t, domId, stages) }));
  };
  sheet.querySelector(".wf-task-menu").onclick = menu;
  return back;
}

const _WF_DOTS = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" stroke="none"><circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/></svg>';
const _WF_XMARK = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

// 任务流 chip 编辑器:节点=可删的小胶囊,输入框回车逐个添加(空回车=保存),Backspace 删末节点,
// Esc 取消;保存把节点串写进任务行 flow 注记(全删=移除任务流)。创建与编辑同一入口。
// ---- 任务备忘(Desk 备忘录应用 notepad):每工作流一份 notepad.json,任务备忘 = 「任务」文件夹一篇,
// note: 注记绑定。编辑即预览:虚线框是常驻可编辑文本(contenteditable),输入防抖自动保存,无按钮无模式。
// 任务带流程时按节点组织成树(总记 + 每节点一节,「## 节点名」为存储分隔,备忘录/agent 里同样可读)。 ----
async function _wfMemoPost(domId, t, op, extra) {
  const wfName = ((typeof domains !== "undefined" && domains.find((x) => x.id === domId)) || {}).name || domId;
  return api(`/workbench/domains/${encodeURIComponent(domId)}/tasks`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify(Object.assign({ op, line: t.line, expect: t.title, wfName }, extra || {})) });
}
function _wfMemoBody(text) { return String(text || "").split("\n").slice(1).join("\n").replace(/^\n+/, ""); }
async function _wfNoteMount(host, domId, t) {
  let text = "";
  if (t.note) {
    try {
      const r = await _wfMemoPost(domId, t, "memo-get");
      if (!r || r.error || typeof r.text !== "string") throw new Error("invalid memo response");
      text = r.text;
    } catch {
      if (!host.isConnected) return false;
      host.replaceChildren();
      const error = document.createElement("div");
      error.className = "wf-note-err";
      error.textContent = "备忘读取失败，原文未改动。";
      const retry = document.createElement("button");
      retry.type = "button";
      retry.textContent = "重试";
      retry.onclick = async () => {
        retry.disabled = true;
        await _wfNoteMount(host, domId, t);
      };
      host.append(error, retry);
      return false;
    }
  }
  if (!host.isConnected) return false;
  _wfNoteRender(host, domId, t, _wfMemoBody(text));
  return true;
}
function _wfMemoSaver(domId, t, host) {   // 防抖自动保存;失败在备忘区显性提示,不静默丢
  let timer = null;
  const flag = (msg) => {
    let e = host.querySelector(".wf-note-err");
    if (!msg) { if (e) e.remove(); return; }
    if (!e) { e = document.createElement("div"); e.className = "wf-note-err"; host.appendChild(e); }
    e.textContent = msg;
  };
  const push = (bodyText) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(async () => {
      timer = null;
      const content = t.title + "\n\n" + bodyText.replace(/\s+$/, "") + "\n";
      let r = null;
      try { r = await _wfMemoPost(domId, t, "memo-set", { text: content }); }
      catch (e2) { flag("自动保存失败:" + e2.message); return; }
      if (!r || r.error) { flag("自动保存失败:" + ((r && r.error) || "后端版本过旧,重启 Desk 后恢复")); return; }
      flag("");
      if (r.note && !t.note) t.note = r.note;
    }, 600);
  };
  return push;
}
const WF_IMG_RE = /!\[([^\]]*)\]\(((?:\.workbench\/canvases|(?:domains\/[\w-]+(?:\/[\w-]+)?\/)?(?:tasknotes\/assets|context\/assets|context))\/[^)\s]+)\)/g;   // alt 可带 w=宽度;任务备忘图在 tasknotes/assets,Context 贴图在 context/assets,画布导出图在 .workbench/canvases;domains/ 前缀全局可解析
function _wfAssetSrc(domId, rel) {
  // 工作台根下的路径(画布导出图 .workbench/canvases/*.svg)原样用;旧短格式按当前域补全
  const full = (rel.startsWith("domains/") || rel.startsWith(".workbench/")) ? rel : `domains/${domId.replace(":", "/")}/` + rel;
  return "/workbench/view?raw=1&path=" + encodeURIComponent(full);
}
const WF_LINK_RE = /\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g;   // [标题](url):备忘里的超链接标记(图片标记带 ! 前缀,先于本式消费)
function _wfEdLink(title, url) {
  const a = document.createElement("a");
  a.className = "wf-note-link";
  a.textContent = title || url;
  a.href = url;
  a.dataset.url = url;
  a.title = url;
  a.contentEditable = "false";   // 原子节点:整体删除,点击不进编辑态
  return a;
}
function _wfEdWireLinks(ed) {
  // 点击打开由 app-core 的全局捕获层统一接管(进顶部标签页);这里只保证锚点是原子节点
  ed.querySelectorAll("a.wf-note-link").forEach((el) => { el.contentEditable = "false"; });
}
function _wfEdAppendText(ed, text) {   // 文本段二次切分:链接标记 → <a>,其余原样文本
  const parts = String(text).split(WF_LINK_RE);
  for (let i = 0; i < parts.length; i += 3) {
    if (parts[i]) ed.appendChild(document.createTextNode(parts[i]));
    if (parts[i + 2] !== undefined) ed.appendChild(_wfEdLink(parts[i + 1], parts[i + 2]));
  }
}
function _wfEdPasteLark(ed, url) {   // 贴 URL:直接落普通超链接(不解析文档名)
  const a = _wfEdLink(url, url);
  ed.focus();
  try { document.execCommand("insertHTML", false, a.outerHTML + "&nbsp;"); } catch { ed.appendChild(a); }
  _wfEdWireLinks(ed);
  ed.dispatchEvent(new Event("input"));
}
function _wfMemoIndent(text, start, end, outdent) {
  text = String(text || "");
  start = Math.max(0, Math.min(text.length, Number(start) || 0));
  end = Math.max(start, Math.min(text.length, Number(end) || 0));
  const first = start > 0 ? text.lastIndexOf("\n", start - 1) + 1 : 0;
  const endProbe = end > start && text[end - 1] === "\n" ? end - 1 : end;
  const last = endProbe > 0 ? text.lastIndexOf("\n", endProbe - 1) + 1 : 0;
  const edits = [];
  for (let at = first; at <= last;) {
    if (outdent) {
      const m = text.slice(at).match(/^(?:\t| {1,2})/);
      if (m) edits.push({ at, del: m[0].length, ins: "" });
    } else {
      edits.push({ at, del: 0, ins: "  " });
    }
    const nl = text.indexOf("\n", at);
    if (nl < 0 || nl + 1 > last) break;
    at = nl + 1;
  }
  const remap = (pos) => {
    let delta = 0;
    for (const e of edits) {
      if (pos < e.at) break;
      if (pos <= e.at + e.del) return e.at + delta + e.ins.length;
      delta += e.ins.length - e.del;
    }
    return pos + delta;
  };
  let next = text;
  for (let i = edits.length - 1; i >= 0; i--) {
    const e = edits[i];
    next = next.slice(0, e.at) + e.ins + next.slice(e.at + e.del);
  }
  return { text: next, start: remap(start), end: remap(end) };
}
function _wfEdOffset(ed, node, offset) {
  try {
    const range = document.createRange();
    range.setStart(ed, 0);
    range.setEnd(node, offset);
    const tmp = document.createElement("div");
    tmp.appendChild(range.cloneContents());
    tmp.appendChild(document.createTextNode("\ue000"));   // 防 _wfNoteRead 裁掉前缀末尾换行
    return Math.max(0, _wfNoteRead(tmp).length - 1);
  } catch { return 0; }
}
function _wfEdPointAt(ed, target, endBias) {
  target = Math.max(0, Number(target) || 0);
  let pos = 0;
  for (let i = 0; i < ed.childNodes.length; i++) {
    const node = ed.childNodes[i];
    if (node.nodeType === 3) {
      const len = node.textContent.length;
      if (target <= pos + len) return { node, offset: Math.max(0, target - pos) };
      pos += len;
      continue;
    }
    let token = "";
    if (node.nodeName === "IMG") {
      const w = parseInt(node.style.width, 10);
      token = `![${w ? "w=" + w : "图"}](${node.dataset.rel || ""})`;
    } else if (node.nodeName === "A") {
      const url = node.dataset.url || node.getAttribute("href") || "";
      token = `[${(node.textContent || url).replace(/[\[\]]/g, "")}](${url})`;
    } else {
      token = node.textContent || "";
    }
    if (target <= pos) return { node: ed, offset: i };
    if (target < pos + token.length) return { node: ed, offset: endBias ? i + 1 : i };
    pos += token.length;
  }
  return { node: ed, offset: ed.childNodes.length };
}
function _wfEdIndentSelection(ed, domId, outdent) {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return false;
  const range = sel.getRangeAt(0);
  if (!ed.contains(range.startContainer) || !ed.contains(range.endContainer)) return false;
  const start = _wfEdOffset(ed, range.startContainer, range.startOffset);
  const end = _wfEdOffset(ed, range.endContainer, range.endOffset);
  const changed = _wfMemoIndent(_wfNoteRead(ed), start, end, outdent);
  if (changed.text === _wfNoteRead(ed)) return true;
  _wfEdSet(ed, changed.text, domId);   // 重建时会把 markdown 链接/图片还原成原子节点
  const a = _wfEdPointAt(ed, changed.start, false);
  const b = _wfEdPointAt(ed, changed.end, true);
  const next = document.createRange();
  next.setStart(a.node, a.offset);
  next.setEnd(b.node, b.offset);
  sel.removeAllRanges();
  sel.addRange(next);
  ed.dispatchEvent(new Event("input"));
  return true;
}
function _wfNoteEd(domId, t, initText, ph) {
  const ed = document.createElement("div");
  ed.className = "wf-note-ed";
  ed.contentEditable = "true";
  ed.dataset.ph = ph;
  _wfEdSet(ed, initText || "", domId);
  ed.onclick = (ev) => ev.stopPropagation();
  ed.addEventListener("keydown", (ev) => {
    if (ev.isComposing || ev.keyCode === 229 || ev.key !== "Tab") return;
    ev.preventDefault();
    ev.stopPropagation();
    _wfEdIndentSelection(ed, domId, ev.shiftKey);
  });
  ed.addEventListener("paste", (ev) => {   // 粘贴:图片上传后内联;富文本一律降纯文本
    ev.preventDefault();
    const items = [...((ev.clipboardData && ev.clipboardData.items) || [])];
    const img = items.find((x) => x.type && x.type.startsWith("image/"));
    if (img) { _wfEdPasteImage(ed, domId, t, img.getAsFile()); return; }
    const txt = ev.clipboardData.getData("text/plain");
    if (!txt) return;
    const one = txt.trim();
    if (/^https?:\/\/\S+$/.test(one)) { _wfEdPasteLark(ed, one); return; }
    document.execCommand("insertText", false, txt);
  });
  ed.addEventListener("dragover", (ev) => {   // 拖图进备忘:文件拖拽放行(Finder/桌面),其余走默认
    if ([...((ev.dataTransfer && ev.dataTransfer.items) || [])].some((x) => x.kind === "file")) {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "copy";
    }
  });
  ed.addEventListener("drop", (ev) => {
    const files = [...((ev.dataTransfer && ev.dataTransfer.files) || [])].filter((f) => f.type && f.type.startsWith("image/"));
    if (!files.length) return;
    ev.preventDefault(); ev.stopPropagation();
    try {   // 光标落到投放点,图片插在拖到的位置
      const r = document.caretRangeFromPoint(ev.clientX, ev.clientY);
      if (r && ed.contains(r.startContainer)) { const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); }
    } catch {}
    for (const f of files) _wfEdPasteImage(ed, domId, t, f);
  });
  return ed;
}
function _wfEdSet(ed, text, domId) {
  ed.innerHTML = "";
  const parts = String(text).split(WF_IMG_RE);   // 捕获组切分:[文本, alt, 图路径, 文本, alt, 路径...]
  for (let i = 0; i < parts.length; i += 3) {
    if (parts[i]) _wfEdAppendText(ed, parts[i]);
    if (parts[i + 2] !== undefined) {
      const img = document.createElement("img");
      img.src = _wfAssetSrc(domId, parts[i + 2]);
      img.dataset.rel = parts[i + 2];
      const wm = String(parts[i + 1] || "").match(/w=(\d+)/);
      if (wm) img.style.width = wm[1] + "px";
      _wfImgWire(img, ed);
      ed.appendChild(img);
    }
  }
  _wfEdWireLinks(ed);
}
// ---- 图片选中与拖角缩放:点图出右下角拖柄,拖动改宽(等比),宽度序列化进标记 alt(w=NNN) ----
let _wfImgSel = null;
function _wfImgWire(img, ed) {
  img.onclick = (ev) => { ev.stopPropagation(); ev.preventDefault(); _wfImgSelect(img, ed); };
  img.draggable = false;
}
function _wfImgDeselect() {
  if (!_wfImgSel) return;
  _wfImgSel.img.classList.remove("wf-img-sel");
  _wfImgSel.h.remove();
  _wfImgSel = null;
}
function _wfImgSelect(img, ed) {
  _wfImgDeselect();
  img.classList.add("wf-img-sel");
  const h = document.createElement("div");
  h.className = "wf-img-handle";
  h.title = "拖动调整大小";
  document.body.appendChild(h);
  const place = () => { const r = img.getBoundingClientRect(); h.style.left = (r.right - 7) + "px"; h.style.top = (r.bottom - 7) + "px"; };
  place();
  _wfImgSel = { img, h };
  h.onmousedown = (e) => {
    e.preventDefault(); e.stopPropagation();
    const w0 = img.getBoundingClientRect().width, x0 = e.clientX;
    const move = (ev) => { img.style.width = Math.max(80, Math.round(w0 + ev.clientX - x0)) + "px"; place(); };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      ed.dispatchEvent(new Event("input"));   // 落盘新宽度
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  };
}
if (!window._wfImgGlobal) {   // 全局一次:点空白/滚动即取消选中
  window._wfImgGlobal = true;
  document.addEventListener("click", (ev) => { if (!ev.target.closest || (!ev.target.closest(".wf-img-handle") && ev.target.tagName !== "IMG")) _wfImgDeselect(); });
  document.addEventListener("scroll", () => _wfImgDeselect(), true);
}
function _wfNoteRead(ed) {   // DOM → 纯文本(+图片标记):文本/换行/img 之外的标签只取结构
  let out = "";
  const walk = (node) => {
    for (const n of node.childNodes) {
      if (n.nodeType === 3) out += n.textContent;
      else if (n.nodeName === "BR") out += "\n";
      else if (n.nodeName === "IMG") {
        const w = parseInt(n.style.width, 10);
        out += `![${w ? "w=" + w : "图"}](${n.dataset.rel || ""})`;
      }
      else if (n.nodeName === "A") {
        const u = n.dataset.url || n.getAttribute("href") || "";
        out += `[${(n.textContent || u).replace(/[\[\]]/g, "")}](${u})`;
      }
      else if (n.nodeType === 1) {
        if (out && !out.endsWith("\n") && /^(DIV|P)$/.test(n.nodeName)) out += "\n";
        walk(n);
      }
    }
  };
  walk(ed);
  return out.replace(/\n+$/, "");
}
async function _wfEdPasteImage(ed, domId, t, file) {
  if (!file) return;
  const rd = new FileReader();
  rd.onload = async () => {
    const b64 = String(rd.result).split(",")[1] || "";
    let r = null;
    try {
      r = await api(`/workbench/domains/${encodeURIComponent(domId)}/tasks`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ op: "note-asset", line: t.line, expect: t.title, name: file.name || "贴图.png", data_b64: b64 }), timeout: 60000 });
    } catch { return; }
    if (!r || r.error || !r.rel) return;
    const rel = `domains/${domId.replace(":", "/")}/tasknotes/` + r.rel;   // 存全路径:全局备忘录里也能解析显示
    const img = document.createElement("img");
    img.src = r.src || _wfAssetSrc(domId, rel);
    img.dataset.rel = rel;
    ed.focus();
    try { document.execCommand("insertHTML", false, img.outerHTML); } catch { ed.appendChild(img); }
    ed.querySelectorAll("img").forEach((im) => { if (!im.onclick) _wfImgWire(im, ed); });   // insertHTML 造的节点补挂缩放
    ed.dispatchEvent(new Event("input"));   // 走同一条防抖自动保存
  };
  rd.readAsDataURL(file);
}
// 「## 节点名」切分正文 → {_: 总记, 节点名: 文本};未知标题并入所在节
function _wfMemoSections(body, names) {
  const sec = { _: [] };
  let cur = "_";
  for (const line of String(body || "").split("\n")) {
    const m = line.match(/^##\s+(.+?)\s*$/);
    if (m && names.includes(m[1])) { cur = m[1]; sec[cur] = sec[cur] || []; continue; }
    (sec[cur] = sec[cur] || []).push(line);
  }
  const out = {};
  for (const k of Object.keys(sec)) out[k] = sec[k].join("\n").replace(/^\n+|\s+$/g, "");
  return out;
}
// 节点↔任务关联(nl 注记:「阶段.节点=任务 id」逗号分隔,坐标与 fdone 同一套)。
// 关联指向任务的稳定 id(t: 注记),不是行号也不是标题——行会漂、标题会改,只有它稳。
function _wfNodeLinks(t) {
  const m = new Map();
  for (const kv of String(t.nl || "").split(",")) {
    const [k, v] = kv.split("=");
    if (k && v) m.set(k, v);
  }
  return m;
}
function _wfNodeLinkSer(m) {
  return [...m.entries()].map(([k, v]) => `${k}=${v}`).join(",");
}
async function _wfNodeLinkMenu(ev, domId, t, coord, onDone) {
  ev.preventDefault(); ev.stopPropagation();
  let data;
  try { data = await _fetchTasks(domId); } catch { data = { items: [] }; }
  const links = _wfNodeLinks(t);
  const cur = links.get(coord);
  const items = [];
  if (cur) {
    const now = (data.items || []).find((x) => x.t === cur);
    // 这份菜单余下的行是可选的任务本身(数据,不是动作),不配图标;取消关联是动作,
    // 与「移除引用」同一枚叉。两类行的文字左缘由 .rp-ctx 那条补位规则对齐。
    items.push({ ico: ICONS.close, label: now ? DeskI18n.t("task.unlinkNamed", { title: now.title }) : DeskI18n.t("task.unlink"), fn: async () => {
      links.delete(coord);
      await _wfTasksPost(domId, { op: "set", line: t.line, expect: t.title, nl: _wfNodeLinkSer(links) || " " });
      if (onDone) onDone();
    } });
  }
  for (const x of (data.items || [])) {
    if (x.state === "done" || x.line === t.line || (x.t && x.t === cur)) continue;
    items.push({ label: x.title, fn: async () => {
      let tid = x.t;
      if (!tid) {   // 目标任务还没有稳定 id:现取(后端缺则生成并写回该行)
        try {
          const r = await api(`/workbench/domains/${encodeURIComponent(domId)}/tasks`, {
            method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ op: "task-id", line: x.line, expect: x.title }) });
          tid = r && r.id;
        } catch {}
      }
      if (!tid) return;
      links.set(coord, tid);
      await _wfTasksPost(domId, { op: "set", line: t.line, expect: t.title, nl: _wfNodeLinkSer(links) });
      if (onDone) onDone();
    } });
  }
  if (!items.length) return;
  openCtxMenu(ev.clientX, ev.clientY, items);
}
function _wfNoteRender(host, domId, t, body) {
  host.innerHTML = "";
  host._wfRead = null;
  const stages = _wfFlowStages(t);
  const save = _wfMemoSaver(domId, t, host);
  if (!stages.length) {   // 无任务流:一整块实况文本
    const ed = _wfNoteEd(domId, t, body, "补充资料（可选）");
    host._wfRead = () => _wfNoteRead(ed);
    ed.oninput = () => save(host._wfRead());
    host.appendChild(ed);
    return;
  }
  const names = [];   // 节点顺序展平,重名只留首个(节名即分节键)
  for (const st of stages) for (const n of st) if (!names.includes(n)) names.push(n);
  const sec = _wfMemoSections(body, names);
  const eds = {};
  const assemble = () => {
    const parts = [];
    const head = (sec._ || "").trim();   // 历史总记内容原样保留(无编辑入口,不丢数据)
    if (head) parts.push(head);
    for (const n of names) {
      const v = eds[n] ? _wfNoteRead(eds[n]).trim() : (sec[n] || "").trim();
      if (v) parts.push("## " + n + "\n" + v);
    }
    return parts.join("\n\n");
  };
  const tree = document.createElement("div");
  tree.className = "wf-note-tree";
  // 树形:单节点阶段 = 一级;并行阶段 = 缩进挂在前一个一级节点下(文件树式竖线);
  // 折叠随父级走,子级有内容时父级默认展开。行首圆点:有内容实心,空则空心。
  const links = _wfNodeLinks(t);
  const byTid = new Map();   // 关联目标的标题:同一份任务清单里查,查不到(已删)就只显示已关联
  try { (_taskItems[domId] || []).forEach((x) => { if (x.t) byTid.set(x.t, x.title); }); } catch {}
  const doneSet = _wfFlowDone(t, stages);
  const mkItem = (label, coord) => {
    const item = document.createElement("div");
    item.className = "wf-node";
    const row = document.createElement("div");
    row.className = "wf-node-row";
    const tid = links.get(coord);
    const tag = tid ? `<span class="wf-node-link" title="已关联任务">${escapeHtml(byTid.get(tid) || "已关联")}</span>` : "";
    // 行首圆点 = 节点完成态,与上方流程图同义同形(点它即勾/取消,同图上点节点);
    // 「备忘里有没有内容」挪到节点名之后的小点上——两种信息共用一个圆点会互相冒充。
    row.innerHTML = `<span class="wf-node-dot${doneSet.has(coord) ? " done" : ""}" title="标记完成/取消"></span>`
      + `<span>${escapeHtml(label)}</span><span class="wf-node-memo" title="已有备忘"></span>${tag}`;
    row.oncontextmenu = (ev) => _wfNodeLinkMenu(ev, domId, t, coord, () => {});   // 右键=关联/取消关联到已有任务
    const child = document.createElement("div");
    child.className = "wf-node-child";
    const ed = _wfNoteEd(domId, t, sec[label] || "", "记录进展");
    eds[label] = ed;
    const dot = row.querySelector(".wf-node-dot");
    const memoDot = row.querySelector(".wf-node-memo");
    const paintDot = () => memoDot.classList.toggle("has", !!_wfNoteRead(ed).trim());
    paintDot();
    dot.onclick = (ev) => {
      ev.stopPropagation();   // 点圆点=改进度,不连带展开
      const [si, ni] = coord.split(".").map(Number);
      _wfTasksPost(domId, { op: "set", line: t.line, expect: t.title,
                            fdone: _wfFlowToggle(stages, doneSet, si, ni) });
    };
    ed.oninput = () => { save(assemble()); paintDot(); };
    child.appendChild(ed);
    item.appendChild(row); item.appendChild(child);
    row.onclick = (ev) => { ev.stopPropagation(); item.classList.toggle("open"); if (item.classList.contains("open")) ed.focus(); };
    if ((sec[label] || "").trim()) item.classList.add("open");
    return item;
  };
  let lastTop = null;
  stages.forEach((st, si) => {
    if (st.length === 1) {
      lastTop = mkItem(st[0], `${si}.0`);
      tree.appendChild(lastTop);
      return;
    }
    const nested = !!lastTop;   // 开局就分叉的极端情况:挂树根
    let kids = null;
    if (nested) {
      kids = lastTop.querySelector(":scope > .wf-node-kids");
      if (!kids) {
        kids = document.createElement("div");
        kids.className = "wf-node-kids";
        lastTop.appendChild(kids);
      }
    }
    let anyKid = false;
    st.forEach((n, ni) => {
      const it = mkItem(n, `${si}.${ni}`);
      if ((sec[n] || "").trim()) anyKid = true;
      (nested ? kids : tree).appendChild(it);
    });
    if (nested && anyKid) lastTop.classList.add("open");   // 子级有内容,父级跟着展开
  });
  host._wfRead = assemble;
  host.appendChild(tree);
}
function _wfFlowBuilder(wrap, t, domId, stages0) {
  wrap.classList.add("open");
  let area = wrap.querySelector(".wf-tflow");
  if (!area) { area = document.createElement("div"); area.className = "wf-tflow"; wrap.appendChild(area); }
  if (area.querySelector(".wf-fbuild")) { area.querySelector(".wf-fbuild input")?.focus(); return; }
  const bld = document.createElement("div");
  bld.className = "wf-fbuild";
  const stages = stages0.map((st) => [...st]);
  const commit = () => {
    _wfTasksPost(domId, { op: "set", line: t.line, expect: t.title,
      flow: stages.length ? _wfFlowSerialize(stages) : "" });
  };
  const paint = () => {
    bld.innerHTML = "";
    stages.forEach((st, i) => {
      const pill = document.createElement("span");
      pill.className = "wf-fpill";
      pill.innerHTML = `<span class="wf-fpt" title="点击编辑">${escapeHtml(st.join(" + "))}</span><b title="移除">×</b>`;
      pill.querySelector("b").onclick = () => { stages.splice(i, 1); paint(); };
      pill.querySelector(".wf-fpt").onclick = () => {   // 点文本=就地改这一阶段(并行仍用 + 分隔)
        const ed = document.createElement("input");
        ed.className = "wf-fpedit";
        ed.value = st.join(" + ");
        ed.spellcheck = false;
        pill.replaceWith(ed);
        ed.focus(); ed.select();
        let fin = false;
        const commit = (save) => {
          if (fin) return; fin = true;
          if (save) {
            const names = ed.value.split(/[+＋]/).map((x) => x.trim()).filter(Boolean);
            if (names.length) stages[i] = names; else stages.splice(i, 1);
          }
          paint();
        };
        ed.onkeydown = (ev) => {
          if (ev.isComposing || ev.keyCode === 229) return;
          if (ev.key === "Enter") { ev.preventDefault(); commit(true); }
          else if (ev.key === "Escape") commit(false);
        };
        ed.onblur = () => commit(true);
      };
      bld.appendChild(pill);
    });
    const inp = document.createElement("input");
    inp.placeholder = stages.length ? "下一节点,回车添加;并行用 +;空行回车保存" : "节点名,回车添加(并行节点用 + 连,如「演练 + 预案」)";
    inp.spellcheck = false;
    inp.onkeydown = (ev) => {
      if (ev.isComposing || ev.keyCode === 229) return;
      if (ev.key === "Enter") {
        ev.preventDefault();
        const st = inp.value.split(/[+＋]/).map((x) => x.trim()).filter(Boolean);   // 全角＋(中文输入法)也认
        if (st.length) { stages.push(st); paint(); }
        else commit();
      } else if (ev.key === "Backspace" && !inp.value && stages.length) {
        stages.pop(); paint();
      } else if (ev.key === "Escape") {
        bld.remove();
      }
    };
    bld.appendChild(inp);
    const ok = document.createElement("button");
    ok.className = "wf-fok";
    ok.textContent = "保存";
    ok.onclick = commit;
    bld.appendChild(ok);
    inp.focus();
  };
  area.prepend(bld);
  paint();
}
// 任务流 stepper(SVG,阶段化):亮 = 已完成,点谁亮谁(白实心),未完成一律空心;
// 并行支路各自独立。点未完成节点=完成(前序阶段顺手补齐),点已完成=撤销。不再自动渲染「当前阶段」。
// onCtx:节点右键回调(si, ni)——关联任务走它;linkOf:取该节点已关联任务的标题,用于悬停提示
function _wfFlowSvg(stages, doneSet, onChange, onCtx, linkOf) {
  const NS = "http://www.w3.org/2000/svg";
  const ROW = 26, R = 3;
  const isDone = (si, ni) => doneSet.has(`${si}.${ni}`);
  const maxK = Math.max(1, ...stages.map((st) => st.length));
  const H = Math.max(30, maxK * ROW + 4);
  const midY = H / 2;
  const svg = document.createElementNS(NS, "svg");
  let x = 12;
  const labelW = (n) => [...n].reduce((a, ch) => a + (ch.charCodeAt(0) > 255 ? 12 : 6.5), 0);
  const cols = stages.map((st) => {
    const w = Math.max(...st.map(labelW));
    const k = st.length;
    const col = { dotX: x, txX: x + 10,
      ys: st.map((_, i) => (k === 1 ? midY : midY + (i - (k - 1) / 2) * ROW)),
      exitX: x + 10 + w + 8 };
    x = col.exitX + 30;
    return col;
  });
  const W = x - 30 + 4;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("width", W); svg.setAttribute("height", H); svg.setAttribute("font-size", "12");
  const path = (d) => { const el = document.createElementNS(NS, "path"); el.setAttribute("d", d); el.setAttribute("fill", "none"); el.setAttribute("stroke", "#3f3f3f"); el.setAttribute("stroke-width", "1.2"); svg.appendChild(el); };
  const route = (xa, ya, xb, yb) => {   // 水平→圆角转弯→垂直→圆角转弯→水平
    if (ya === yb) { path(`M${xa} ${ya} H${xb}`); return; }
    const xc = (xa + xb) / 2, dir = yb > ya ? 1 : -1;
    path(`M${xa} ${ya} H${xc - R} Q${xc} ${ya} ${xc} ${ya + dir * R} V${yb - dir * R} Q${xc} ${yb} ${xc + R} ${yb} H${xb}`);
  };
  for (let j = 1; j < cols.length; j++) {
    const a = cols[j - 1], b = cols[j];
    const x0 = a.exitX, x1 = b.dotX - 6;
    if (a.ys.length === 1 && b.ys.length === 1) route(x0, a.ys[0], x1, b.ys[0]);
    else {   // 经中点合流再分叉:任意支路数通用
      const xm = (x0 + x1) / 2;
      for (const y of a.ys) route(x0, y, xm, midY);
      for (const y of b.ys) route(xm, midY, x1, y);
    }
  }
  const clickNode = (si, ni) => onChange(_wfFlowToggle(stages, doneSet, si, ni));
  stages.forEach((st, si) => {
    const col = cols[si];
    st.forEach((name, ni) => {
      const y = col.ys[ni];
      const done = isDone(si, ni);
      const g = document.createElementNS(NS, "g");
      const c = document.createElementNS(NS, "circle");
      c.setAttribute("cx", col.dotX); c.setAttribute("cy", y); c.setAttribute("r", done ? 4.5 : 4);
      if (done) c.setAttribute("fill", "#e8e8e8");
      else { c.setAttribute("fill", "none"); c.setAttribute("stroke", "#6f6f6f"); c.setAttribute("stroke-width", "1.5"); }
      const tx = document.createElementNS(NS, "text");
      tx.setAttribute("x", col.txX); tx.setAttribute("y", y + 4);
      tx.setAttribute("fill", done ? "#e8e8e8" : "#8f8f8f");
      tx.textContent = name;
      g.appendChild(c); g.appendChild(tx);
      const lk = linkOf ? linkOf(si, ni) : "";
      if (lk) {   // 已关联:文字后缀一个链接符 + 悬停给出目标任务名(SVG 里不做胶囊,免得撑歪布局)
        tx.textContent = name + " ⌁";
        const ti = document.createElementNS(NS, "title");
        ti.textContent = "已关联任务:" + lk;
        g.appendChild(ti);
      }
      g.style.cursor = "pointer";
      g.addEventListener("click", (ev) => { ev.stopPropagation(); clickNode(si, ni); });
      if (onCtx) g.addEventListener("contextmenu", (ev) => onCtx(ev, si, ni));
      svg.appendChild(g);
    });
  });
  return svg;
}
