// desktron 前端(app.js 拆分):tasks:TaskDock——左栏底部固定任务清单(TASKS.md,跨会话/跨引擎常驻)
// 经典脚本按序加载共享全局作用域,加载顺序见 index.html。勿单独重排。
//
// 存储是 domains/<工作区>/TASKS.md 纯文本(- [ ]/[~]/[x]),会话 cwd 里 agent 直接编辑,
// dock 与它读写同一份文件——任务管理不依赖 agent 在场,纯 UI 勾选/添加即用。
// 展示贴 CC 自带 task 面板:doing 实心块高亮、todo 空心块、done 收成一行计数(点开可勾回),
// 固定钉在 #panel-list 与引擎状态条之间,始终可见,清单超高块内自己滚。
// 行级更新带「行号+现标题」校验,后端 409 = agent 并行改过,重拉全量,不盲改。

const _taskStats = {};   // 任务源 key(工作区 id 或 容器:子区) → {done,doing,total}:面板容器行角标的缓存

const _taskItems = {};   // 任务源 key → 条目数组(_fetchTasks 是任务数据的唯一入口,勿单独刷新)

// 同一个 key 在飞的那一次共用。这里自称是任务数据的唯一入口,可原来只缓存结果、不挡请求:
// 页面刚起来那阵,侧栏折叠计数、任务页、设置页统计各自遍历一遍全部工作流,同一时刻就发出去
// 十来个请求(2026-08-25 实测 11 个,五个工作流各取了两遍多)。本机看不出来,经隧道时每个
// 0.23~1.9 秒,还都在跟真正要发的请求抢连接。
//
// 只挡「同时在飞」,不做时间缓存:任务由 agent 直接改 TASKS.md,过期的数比多一次请求麻烦。
const _tasksFlight = {};
const _tasksGeneration = {};  // task.updated 使旧请求失效；迟到响应不得覆盖新账本快照
function invalidateTasks(key) {
  if (!key) return;
  _tasksGeneration[key] = (_tasksGeneration[key] || 0) + 1;
  delete _taskStats[key];
  delete _taskItems[key];
  delete _tasksFlight[key];   // 已发请求仍可结束，但下面的 generation 闸会丢弃其结果
  const pre = (typeof window !== "undefined" && window.__BOOT__) || null;
  const snap = pre && pre["/workbench/tasks"];
  if (snap) delete snap[key];  // 事件晚于开机快照时不能再消费那份旧数据
}
function invalidateAllTasks() {
  const pre = (typeof window !== "undefined" && window.__BOOT__) || null;
  const snap = (pre && pre["/workbench/tasks"]) || {};
  const keys = new Set([...Object.keys(_taskStats), ...Object.keys(_taskItems),
                        ...Object.keys(_tasksFlight), ...Object.keys(_tasksGeneration),
                        ...Object.keys(snap)]);
  for (const key of keys) invalidateTasks(key);
}
// 开机快照:首页内联的开机数据里带了全部工作流 + _desktop 的任务(ui_boot._boot_data)。
// 每个 key 只消费一次,用过即删——这不是时间缓存(任务由 agent 直接改文件,过期的数比多一次
// 请求麻烦,见上),只是把「开机第一眼」那份顺路带过来;之后的刷新照旧走网络,
// 变更仍由 task.updated 推送失效(hsDispatchGlobal)。
function _bootTasksTake(key) {
  const pre = (typeof window !== "undefined" && window.__BOOT__) || null;
  const snap = pre && pre["/workbench/tasks"];
  if (!snap || !Object.prototype.hasOwnProperty.call(snap, key)) return null;
  const r = snap[key];
  delete snap[key];
  return r && typeof r === "object" ? r : null;
}
function _takeTasks(key, r) {
  _taskStats[key] = { done: r.done || 0, doing: r.doing || 0, total: r.total || 0 };
  _taskItems[key] = r.items || [];
  return r;
}
function _fetchTasks(key) {
  if (_tasksFlight[key]) return _tasksFlight[key];
  const generation = _tasksGeneration[key] || 0;
  const pre = _bootTasksTake(key);
  if (pre) return Promise.resolve(_takeTasks(key, pre));
  let p;
  const refresh = generation ? `?task_generation=${generation}` : "";
  p = api(`/workbench/domains/${encodeURIComponent(key)}/tasks${refresh}`).then((r) => {
    // 请求在途时收到 task.updated：不能让旧响应重新写回缓存，也不能把旧响应交给重绘方。
    if ((_tasksGeneration[key] || 0) !== generation) return _fetchTasks(key);
    return _takeTasks(key, r);   // 整份条目缓存:节点关联按稳定 id 反查任务标题
  });
  // 无论成败都要撤掉自己的记号；若失效后已有新请求，不能把新请求的句柄一起删掉。
  _tasksFlight[key] = p;
  p.catch(() => {}).then(() => { if (_tasksFlight[key] === p) delete _tasksFlight[key]; });
  return p;
}
async function fetchTaskStats(key) {
  try { await _fetchTasks(key); } catch {}
  return _taskStats[key];
}
// 几个工作流一次取回。凡是遍历一批 key 的地方都该走这条,而不是 keys.map(fetchTaskStats):
// 后者一个 key 一个请求,服务端这头只要 1~3 毫秒,贵的全在路上——经隧道时握手中位 384ms,
// 四分之一的新连接还要多付整整一秒(2026-08-25 实测)。填的是同一份缓存,单个取那条路照旧。
// 返回 {key: 那一份原始响应},与 _fetchTasks 的返回同形,调用方两条路都能用同一套读法。
let _bundleOff = false;   // 这一台的后端没有那条路,别再一次次去撞
async function fetchTasksBundle(keys) {
  const want = [...new Set(keys)].filter(Boolean);
  if (!want.length) return {};
  const generations = Object.fromEntries(want.map((key) => [key, _tasksGeneration[key] || 0]));
  // 老后端没有这条路(404)。新前端不该挑后端版本:退回一个一个取,慢但对。
  // 撞过一次就记住:三处调用各撞一次的话,老后端下反而比改之前多三个请求
  // (2026-08-25 实测:前端已换、后端还没重启的那阵,bundle 3 个全 404,逐个 11 个照旧)。
  const oneByOne = async () => {
    const out = {};
    for (const k of want) { try { out[k] = await _fetchTasks(k); } catch {} }
    return out;
  };
  if (_bundleOff) return oneByOne();
  // 开机快照覆盖了这一批就直接用,连合并那一趟也省掉;缺任何一个 key 就整批走网络,
  // 不做半份快照半份请求——那会让同一批数据来自两个时刻。
  {
    const pre = (typeof window !== "undefined" && window.__BOOT__) || null;
    const snap = pre && pre["/workbench/tasks"];
    if (snap && want.every((k) => Object.prototype.hasOwnProperty.call(snap, k) && snap[k])) {
      const out = {};
      for (const k of want) out[k] = _takeTasks(k, _bootTasksTake(k));
      return out;
    }
  }
  let r;
  try {
    // ids 排序后再拼:同一批工作流被不同调用方以不同顺序传进来,URL 就不同,
    // api 那层的同路径合并对它一律落空——2026-09-02 实测启动窗里这一条打了两趟,
    // 内容完全相同,只是 ids 顺序不一样。排序让相同集合得到相同 URL。
    const ids = [...want].sort();
    const generationKey = ids.map((key) => generations[key]).join(",");
    const refresh = ids.some((key) => generations[key])
      ? `&task_generation=${encodeURIComponent(generationKey)}` : "";
    r = await api(`/workbench/tasks?ids=${ids.map(encodeURIComponent).join(",")}${refresh}`);
  } catch {
    _bundleOff = true;
    return oneByOne();
  }
  const out = { ...(r || {}) };
  for (const k of want) {
    if ((_tasksGeneration[k] || 0) !== generations[k]) {
      // 合并请求在途时收到 task.updated；这一项改走带 generation 闸的单项新请求。
      try { out[k] = await _fetchTasks(k); } catch { delete out[k]; }
      continue;
    }
    const one = out[k];
    if (!one) continue;   // 单个工作流读失败不连累其余
    _takeTasks(k, one);
  }
  return out;
}
// 容器行的子区进度聚合:各子区任务源(ref 工作区或 容器:子区)完成度合计,替代已退役的手填 %
function containerTaskSub(c, subEl) {
  const keys = (c.subareas || []).map((s) => s.ref || `${c.id}:${s.id}`);
  const put = () => {
    if (!subEl.isConnected) return;
    let done = 0, total = 0;
    for (const k of keys) { const s = _taskStats[k]; if (s) { done += s.done; total += s.total; } }
    subEl.textContent = `${keys.length} 个子区` + (total ? ` · 任务 ${done}/${total}` : "");
  };
  put();
  fetchTasksBundle(keys).then(put);
}

// ---- TaskDock ----
const _dock = { gen: 0, timer: null, sig: "", doneOpen: false, srcSig: "" };

// 全局任务账本：读取所有工作流的 TASKS.md 聚合结果；每条仍写回自己的工作流。
function _dockSource() {
  return { mode: "global", key: "*" };
}

// 写回走 app-core 的 taskWrite(四个入口共用一份);这里只登记「写完之后本视图怎么刷」。
// 登记而不是各写各的,是为了让别处的改动也能带动这一份重绘——原先在工作流页勾一条,
// 侧栏这份清单要等下一次轮询才跟上。
taskViewRegister((key, r) => {
  if (r && r.ok) _taskStats[key] = { done: r.done, doing: r.doing, total: r.total };
  renderTaskDock(true);
});

function _dockRow(t, key) {
  const row = document.createElement("div");
  row.className = "td-row st-" + t.state;
  const ledger = t.ledger || {}, primary = (ledger.sessions || []).find((s) => s.role === "primary") || (ledger.sessions || [])[0];
  const provenance = [key && key !== DESKTOP ? key : "", primary && (primary.title || primary.engine)].filter(Boolean).join(" · ");
  row.innerHTML = `<span class="td-mark" title="${t.state === "done" ? "标回待办" : "标记完成"}"></span><span class="td-title" title="点击编辑">${escapeHtml(t.title)}${provenance ? `<small>${escapeHtml(provenance)}</small>` : ""}</span>`;
  row.querySelector(".td-mark").onclick = (ev) => {
    ev.stopPropagation();
    taskSetState(key, t, t.state === "done" ? "todo" : "done");
  };
  const source = row.querySelector("small");
  if (source && primary && primary.id) {
    source.title = "打开关联会话";
    source.onclick = (ev) => {
      ev.stopPropagation();
      attachTerminal(primary.id, primary.engine || curEngine, { title: primary.title || "" });
    };
  }
  // 点标题就地编辑:Enter/失焦保存(带行号+原标题校验),Esc 取消
  row.querySelector(".td-title").onclick = (ev) => {
    ev.stopPropagation();
    if (row.querySelector(".td-edit")) return;
    const inp = document.createElement("input");
    inp.className = "td-edit"; inp.value = t.title; inp.spellcheck = false;
    row.querySelector(".td-title").replaceWith(inp);
    inp.focus(); inp.select();
    let done = false;
    const finish = (save) => {
      if (done) return; done = true;
      const v = inp.value.trim();
      if (save && v && v !== t.title) taskWrite(key, { op: "set", line: t.line, expect: t.title, title: v });
      else { _dock.sig = ""; renderTaskDock(true); }   // 取消/没改:清签名强制重绘还原(否则内容没变会被去重跳过)
    };
    inp.onkeydown = (e) => {
      if (e.isComposing || e.keyCode === 229) return;   // 输入法合成态的回车=选字,不是提交
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
    };
    inp.onblur = () => finish(true);
  };
  row.oncontextmenu = (ev) => {
    ev.preventDefault(); ev.stopPropagation();
    openCtxMenu(ev.clientX, ev.clientY, [
      { label: DeskI18n.t(t.state === "doing" ? "task.backToTodo" : "task.markDoing"),
        fn: () => taskSetState(key, t, t.state === "doing" ? "todo" : "doing") },
      { label: DeskI18n.t("menu.rename"), fn: () => taskRename(key, t) },
      { label: DeskI18n.t("menu.delete"), fn: () => taskDelete(key, t) },
    ]);
  };
  return row;
}

function _dockPaint(host, src, data) {
  host.innerHTML = "";
  const openOf = (items) => items.filter((t) => t.state !== "done")
    .sort((a, b) => (a.state === "doing" ? 0 : 1) - (b.state === "doing" ? 0 : 1));   // doing 置顶,其余保持文件序
  if (src.mode === "single" || src.mode === "global") {
    const items = (data && data.items) || [];
    const open = openOf(items);
    const cnt = document.getElementById("task-cnt");   // 底部「任务」计数徽标
    if (cnt) { cnt.textContent = open.length ? String(open.length) : ""; cnt.hidden = !open.length; }
    for (const t of open) host.appendChild(_dockRow(t, t.domain || src.key));
    const done = items.filter((t) => t.state === "done");
    if (done.length) {
      const dl = document.createElement("div"); dl.className = "td-done-line";
      dl.textContent = `已完成 ${done.length}`;
      dl.title = _dock.doneOpen ? "收起已完成" : "展开已完成(可勾回)";
      dl.onclick = () => { _dock.doneOpen = !_dock.doneOpen; renderTaskDock(true); };
      host.appendChild(dl);
      if (_dock.doneOpen) for (const t of done) host.appendChild(_dockRow(t, t.domain || src.key));
    }
    return;   // 弹层不提供添加输入:加任务走侧栏工作流行或工作流页的行内输入
  }
  // 聚合(统一任务中心):按工作流分组,只显示有未完成任务的组;组头点击在侧栏定位。
  // 添加输入挂当前选中项目组(无选中项目则提示去项目里加)。
  let any = false, openTotal = 0;
  const curId = nav.type === "domain" && nav.id !== DESKTOP ? nav.id : null;
  for (const g of data) {
    const open = openOf(g.items || []);
    openTotal += open.length;
    if (!open.length && g.key !== curId) continue;
    any = true;
    const hd = document.createElement("div"); hd.className = "td-grp";
    hd.textContent = g.label; hd.title = `在侧栏定位「${g.label}」`;
    hd.onclick = () => _revealWorkflowInSidebar(g.key);
    host.appendChild(hd);
    for (const t of open) host.appendChild(_dockRow(t, g.key));
  }
  if (!any) {   // 弹层模式下不整体藏,显示空态(hidden 属性会被 .open 的显示规则覆盖,不再依赖)
    const e = document.createElement("div"); e.className = "group-empty";
    e.textContent = "没有未完成任务";
    host.appendChild(e);
  }
  const cnt = document.getElementById("task-cnt");   // 底部「任务中心」计数徽标
  if (cnt) { cnt.textContent = openTotal ? String(openTotal) : ""; cnt.hidden = !openTotal; }
}

// 重渲/轮询入口。immediate=true(nav 切换、操作后)立即取数重绘;否则按节律。
// 输入框聚焦或右键菜单开着时跳过重绘(不冲掉正在输入的文字/菜单),数据没变也跳过(不闪烁)。
async function renderTaskDock(immediate) {
  const dock = document.getElementById("task-dock");
  if (!dock) return;
  const src = _dockSource();
  const srcSig = src ? JSON.stringify(src) : "";
  if (srcSig !== _dock.srcSig) { _dock.srcSig = srcSig; _dock.sig = ""; _dock.doneOpen = false; immediate = true; }
  else if (!immediate && _dock.timer) return;   // 面板重渲高频调用:源没变且轮询在跑,不额外取数
  if (!src) { dock.hidden = true; clearTimeout(_dock.timer); _dock.timer = null; return; }
  // 后台标签页(document.hidden)不发这次请求,只重排下一拍——浏览器本来就会把不可见页的
  // setTimeout 越拖越长,再叠一层真实网络请求没有意义。切回来那一下由下面的 visibilitychange
  // 补(force),不必等这层被拖慢的定时器自己转到。同一处理见 app-rail.js:523。
  if (document.hidden && !immediate) { _dock.timer = setTimeout(() => renderTaskDock(false), 1000); return; }
  const gen = ++_dock.gen;
  clearTimeout(_dock.timer);
  // 这一层现在只是兜底。清单的变化由服务端每秒 stat 出来推 task.updated(见 task_watch),
  // 到得比这快得多;留着是因为 SSE 断线期间的事件服务端不重放,断在哪一拍就漏哪一拍。
  // 远端放到 30 秒:那一侧每问一趟是 0.23~1.9 秒的跨区往返,而漏掉的那一拍另有人补——
  // SSE 一重连就会刷一次(见 app-chat 的 hsRecoverLiveViews)。本机直连 0~1ms,维持原样。
  const schedule = () => {
    const ms = src.mode === "agg" ? 30000 : (isLocalView() ? 5000 : 30000);
    _dock.timer = setTimeout(() => renderTaskDock(false), ms);
  };
  let data;
  try {
    if (src.mode === "single") data = await _fetchTasks(src.key);
    else if (src.mode === "global") data = await api("/workbench/tasks?scope=all");
    else data = (await Promise.all(src.keys.map(async (k) => {
      try { return { ...k, items: (await _fetchTasks(k.key)).items || [] }; } catch { return { ...k, items: [] }; }
    })));
  } catch { if (gen === _dock.gen) schedule(); return; }
  if (gen !== _dock.gen) return;   // 期间又切换过,丢弃过期结果
  const busy = dock.contains(document.activeElement) || document.querySelector(".rp-ctx");
  // sig 计入当前选中项目:添加输入挂在选中项目组(见 _dockPaint),数据没变但选中变了也要重绘
  const sig = JSON.stringify(data) + (_dock.doneOpen ? "+d" : "") + "|" + (nav.type === "domain" ? nav.id : "");
  if ((!busy || immediate) && sig !== _dock.sig) {
    _dock.sig = sig;
    // 弹层已下线,容器始终隐藏;仍走一遍 _dockPaint 是因为侧栏「任务」的计数徽标(#task-cnt)在里面算
    dock.hidden = true;
    _dockPaint(dock, src, data);
  }
  schedule();
}
// 切回本页立即补一拍:别的窗口这期间写过 TASKS.md,本窗口的定时器刚才一直被浏览器
// 限流拖着(见上面 document.hidden 分支),不强制的话要等它自己转到才会看见,可能是几十秒后。
document.addEventListener("visibilitychange", () => { if (!document.hidden) renderTaskDock(true); });
