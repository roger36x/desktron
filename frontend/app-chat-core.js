// 由 app-chat.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 1/13 片
// HTML 会话视图(claude 版)。
//
// 与终端会话是两条并行的路径,互不影响:终端跑真实 TUI(PTY + xterm),这里跑引擎的 headless
// 结构化通道(engine_adapters.py 的契约七端点 + SSE)。做它的起因是终端跑在备用屏、没有回滚
// 缓冲,历史归 TUI 自己管,前端做不到「跳到第 N 条发言」;HTML 里那只是普通锚点。
//
// 契约(后端 ui_server.py 的 _adapter_route):
//   GET/POST /session                   列表 / 新建
//   DELETE   /session/{id}              删除
//   GET      /session/{id}/message      历史(info + parts)
//   POST     /session/{id}/prompt_async 发消息(204,结果走 SSE)
//   POST     /session/{id}/permission   权限回帧 {id, allow}
//   GET      /event                     SSE 事件流
//
// part 类型:text(正文)、reasoning(思考)、tool(工具,state.status/input/output)。
// text part 流式创建走 message.part.delta,完成时另发 message.part.updated 全文快照。
// 两条事件都要能「按 id 建块」:前者负责实时出字,后者负责断线丢帧后的权威收口。
//
// 可靠性上的几处做法沿用 2026-06 那版结构化聊天(git show b74dea8^:frontend/app-chat.js):
// 贴底状态缓存、不在流式里读布局、回合末拿引擎权威转录对账自愈、工作态看门狗、
// 流式纯文本收尾升 markdown、工具签名去重。那些是踩出来的,不重新踩一遍。

const HS_PAGE = (sid) => "chat:" + sid;
let _hsSSE = null;            // 契约 SSE 只开一条,按 sessionID 分发到各视图
let _hsSSEAt = Date.now();    // 最近一次真收到字节；readyState=OPEN 不能证明 iOS 的连接还活着
const _hsViews = new Map();   // sid → view 对象
let _hsRecoveryWired = false;
let _hsQueueTimer = 0;
let _hsSSEOpened = false;     // 本页是否已经连上过事件流:第一次连上不是重连,见 onopen
let _hsRuntime = (() => {
  const boot = typeof window !== "undefined" && window.__BOOT__;
  const value = boot && boot["/workbench/runtime"];
  return value && typeof value === "object" ? value : null;
})();
let _hsRuntimeReloading = false;

// SSE 的首帧就是服务实例握手。boot_id 只变说明后端进程重启:现有连接已经是新连接,
// 对账会话与侧栏即可；frontend_rev 变化才说明内存里的 JS/CSS 已旧，整页重载一次。
function hsRuntimeConnected(runtime) {
  if (!runtime || typeof runtime !== "object") return;
  const prev = _hsRuntime;
  _hsRuntime = runtime;
  if (!prev) return;
  if (prev.boot_id && runtime.boot_id && prev.boot_id !== runtime.boot_id) {
    for (const v of _hsViews.values()) {
      if ((v.queue || []).length || v.pendingSend || v.sending) {
        v.turnReady = false;
        if (!v.queuePaused) hsQIssue(v, "state", "正在恢复连接");
      }
    }
  }
  if (prev.frontend_rev && runtime.frontend_rev && prev.frontend_rev !== runtime.frontend_rev) {
    if (!_hsRuntimeReloading) {
      _hsRuntimeReloading = true;
      // 冷启时这里偶发会白跑一整轮:第一份页面加载完随即整页重载,前一轮的取数全作废。
      // 不留痕就分不清是这条(前端版本变了)还是切引擎那条,也看不出它发生在第几秒。
      if (typeof deskStartupMark === "function") {
        deskStartupMark("reload", `frontend_rev ${prev.frontend_rev}->${runtime.frontend_rev}`);
      }
      location.reload();
    }
    return;
  }
  if (prev.boot_id && runtime.boot_id && prev.boot_id !== runtime.boot_id) {
    hsRecoverLiveViews();
    try { if (typeof refreshTerms === "function") refreshTerms(); } catch {}
  }
}

// ---- 事件总线接入 ----------------------------------------------------------

// opts.busyOnly:整页刚加载、事件流第一次连上时用。那一刻没有「断线窗口」可补——
// 历史是几百毫秒前经 /message 灌的,任务清单是随首页内联来的;照全量核对走一遍等于
// 每次打开会话都再拉一次 /session、/session/adopt、整份 /message、/config、/state,
// 经隧道要两三秒,还会把刚灌好的正文整页重建一次(2026-09-04 切引擎盘点,调用栈实证)。
// 只有回合正在跑的会话例外:它在「历史取回」与「连接建立」之间的几百毫秒里可能漏帧,照常核对。
function hsRecoverLiveViews(opts) {
  if (document.hidden) return;
  hsQueuePump();
  const busyOnly = !!(opts && opts.busyOnly);
  // 任务清单也核一遍。断线窗口里的 task.updated 服务端不重放,而客户端那层轮询已经
  // 放到 30 秒(远端),不补的话断一次就可能盯着旧清单看半分钟。缓存一并清掉,
  // 否则重画取到的还是本地那份。
  // 这一段各自兜错:它只是顺路的核对,哪一步抛了都不该把下面真正要紧的
  // 会话恢复一起带走——带走的话一次异常就是「回来之后页面永远不动」。
  if (!busyOnly) try {
    if (typeof invalidateAllTasks === "function") invalidateAllTasks();
    if (typeof renderTaskDock === "function") renderTaskDock(true);
    if (typeof onTasksChanged === "function") onTasksChanged("");
  } catch {}
  for (const v of _hsViews.values()) {
    if (v.ephemeral) continue;   // 尚未发送的前端空会话没有后端记录，无需恢复或对账
    if (!v.historyReady || hsHidden(v)) continue;
    if (busyOnly && !v.busy) continue;
    // 失败重试一次:手机从后台回来的头几秒,蜂窝/VPN 往往还没重新挂上,这一趟
    // 十有八九失败;原来静默吞掉,恢复就悬在半空,只能等十秒轮询兜底。
    hsStateSync(v).then((st) => {
      if (st) { if (!v.busy) hsReconcile(v); return; }
      setTimeout(() => { if (v.el && v.el.isConnected && !document.hidden) hsStateSync(v).then(() => { if (!v.busy) hsReconcile(v); }, () => {}); }, 2500);
    }, () => {});
    // 档位也核一遍:session.settings 只在连着的时候送得到,断线窗口里那一帧是补不回来的
    // (服务端不重放)。不核的话页面会一直显示断线前的旧档位。
    hsEngineSync(v);
  }
}

// 从后台真正回来(不是普通的窗口焦点变化)。iOS 上有两件桌面上不存在的事:
// 一、standalone(主屏)形态下恢复时 visibilitychange 有不触发的已知场景,多听一个
//     focus 兜底;二、EventSource 挂起后会变成僵尸——readyState 还是 OPEN,却再也
//     收不到帧,也永远不报错。恢复那一刻不重建它的话,这一次的状态核对做完之后,
//     后续所有更新照样到不了:页面看着就是「停在最小化时的样子,再也不动」
//     (2026-08-26 用户实见;Chrome 里冻结/解冻复现不出,是 iOS 独有的形态)。
// 只在「确实藏起来超过 5 秒」时重建:桌面上窗口焦点每天变几十次,不该次次重连。
let _hsHiddenAt = 0;
let _hsWakeAt = Date.now();
let _hsWakeTimer = 0;
function hsResume(e) {
  // 首次导航也会发一次 pageshow(persisted=false)。那不是「回来」,页面此刻正在开机,
  // 没有任何东西需要恢复;照走会把随首页内联的任务清单清掉再拉一遍。
  // 从 bfcache 回来的 pageshow 带 persisted=true,照常恢复。
  if (e && e.type === "pageshow" && !e.persisted) return;
  const now = Date.now();
  _hsWakeAt = now;       // 生命周期事件先到时，别让后面的漂移探针重复重连一次
  if (document.hidden) { _hsHiddenAt = now; return; }
  const away = _hsHiddenAt ? now - _hsHiddenAt : 0;
  _hsHiddenAt = 0;
  if (away > 5000 && _hsSSE) {
    try { _hsSSE.close(); } catch {}
    _hsSSE = null;
    hsEnsureSSE();
  }
  // 无论走不走重连,这一趟都要对账。原来重连那一路直接 return,把恢复整个交给新连接的
  // onopen——而建连要一个往返(手机经隧道一两秒),那段窗口里界面停在断线前;更糟的是
  // 蜂窝刚恢复、VPN 还没挂上时建连会失败,onopen 不触发,恢复就再也不发生,只能退出重开
  // (2026-09-03 用户实见:后台收起一会儿回来,输出不再更新)。
  // 对账本身不依赖 SSE:它走 /state 与转录,是独立的一趟 HTTP。新连接的 onopen 之后
  // 还会再对一次,重复一次的代价远小于漏掉。
  hsRecoverLiveViews();
}

// iOS 主屏 Web App 被系统冻结后，visibilitychange / pageshow / focus 都可能没有补发。
// 定时器本身在后台也不会持续跑；但页面解冻后，欠下的这一拍会回来。用真实时间的跳变
// 认出“刚从冻结中醒来”，再走与生命周期事件完全相同的重连与状态对账路径。
// 这不是后台刷新：后台仍然零请求；它只保证回到前台后能自愈，不必退出重开。
function hsWakeProbe() {
  const now = Date.now();
  if (document.hidden) {
    if (!_hsHiddenAt) _hsHiddenAt = now;
    return;              // 隐藏期间不推进 _hsWakeAt，保留最后一次可见时刻
  }
  const gap = now - _hsWakeAt;
  _hsWakeAt = now;
  if (gap <= 5000) {
    // 生命周期事件没有发生、时钟也没有跳时，仍用服务端已有的 SSE keepalive 判断连接。
    // iOS 的僵尸 EventSource 会保持 OPEN，却收不到心跳；45 秒没有任何字节就换一条。
    if (_hsSSE && now - _hsSSEAt > 45000) {
      try { _hsSSE.close(); } catch {}
      _hsSSE = null;
      _hsSSEAt = now;         // 新连接留一个完整宽限窗；离线时不每两秒重拨
      hsEnsureSSE();
    }
    return;
  }
  if (!_hsHiddenAt) _hsHiddenAt = now - gap;
  hsResume();
}

// 队列的调度归整个页面；冷页可以释放正文和自己的轮询，待发消息仍会续发。
// 只查有待办的会话，不加载隐藏会话的 /message、任务和目标。
async function hsQueuePump() {
  if (document.hidden) return;
  await Promise.allSettled([..._hsViews.values()].map(async (v) => {
    if (v.ephemeral || !v.el.isConnected || v.queuePolling || v.qSending ||
        (!v.queueRecoveryBlocked && !v.pendingSend && (!(v.queue || []).length || v.queuePaused))) return;
    v.queuePolling = true;
    try {
      if (v.queueRecoveryBlocked) {
        v.queueRecoveryReady = hsRecoverServerQueue(v);
        if (!(await v.queueRecoveryReady)) return;
      }
      await hsFollowTick(v, true);
    }
    finally { v.queuePolling = false; }
  }));
}

function hsEnsureSSE() {
  if (_hsSSE) return;
  try {
    _hsSSE = new EventSource("/event");
  } catch { return; }
  _hsSSEAt = Date.now();
  _hsSSE.onmessage = (e) => {
    _hsSSEAt = Date.now();
    let ev = null;
    try { ev = JSON.parse(e.data); } catch { return; }
    const p = ev.properties || {};
    // 不挂在某一段会话上的事件(任务清单变了之类)先分派出去。这条通道原来只按 sessionID
    // 找视图,找不到就整条丢掉——于是服务端就算推了也没人接。
    if (hsDispatchGlobal(ev.type, p)) return;
    const sid = p.sessionID || (p.info && p.info.sessionID) || (p.part && p.part.sessionID) || "";
    const v = _hsViews.get(sid);
    if (v) hsApply(v, ev.type, p);
  };
  _hsSSE.addEventListener("ping", (e) => {
    _hsSSEAt = Date.now();
    // 心跳同时带页面宿主当前的前端版本。这样 CSS/JS 在服务进程不重启时被 sync-home
    // 换掉，已经打开的手机页面也会在下一拍自行换版。兼容旧后端的空 ping：主动取一次
    // runtime；等后端升级后直接消费心跳负载，不再增加请求。
    let runtime = null;
    try { runtime = (JSON.parse(e.data || "{}") || {}).runtime || null; } catch {}
    if (runtime) hsRuntimeConnected(runtime);
    else api("/workbench/runtime").then(hsRuntimeConnected).catch(() => {});
  });
  _hsSSE.onopen = () => {
    _hsSSEAt = Date.now();
    // EventSource 会自动重连,但服务端不重放断线期间的事件。手机切后台、蜂窝与 Wi-Fi
    // 切换时尤其常见:过程已画出,最终正文与 idle 恰好落在断线窗口,页面就永久少一截。
    // 历史已经灌好的可见会话在重连后主动核对控制态与权威消息;冷页轮到显示时由 hsLoad 处理。
    // 本页生命周期里的第一次连上不算重连:之前没有连接,就没有断线窗口。只核对正在跑的会话
    // (它在取历史与建连之间可能漏帧),空闲的不动——否则每次打开会话都要多付一整轮取数与重建。
    const first = !_hsSSEOpened;
    _hsSSEOpened = true;
    if (first) { hsRecoverLiveViews({ busyOnly: true }); return; }
    hsRecoverLiveViews();
  };
  _hsSSE.onerror = () => {
    // EventSource 自带重连;这里只在彻底关闭时重建(刷新页面/服务重启)
    if (_hsSSE && _hsSSE.readyState === 2) { _hsSSE = null; setTimeout(hsEnsureSSE, 2000); }
  };
  if (!_hsRecoveryWired) {
    _hsRecoveryWired = true;
    // iOS 从后台回来时连接有时仍显示 OPEN,不会再触发 onopen。生命周期与网络入口
    // 统一走 hsResume:藏够久的重建 SSE(僵尸连接只有换一条才能救),其余情形照旧
    // 只做状态核对。focus 也听:standalone 形态下 visibilitychange 有不触发的场景。
    document.addEventListener("visibilitychange", hsResume);
    window.addEventListener("pageshow", hsResume);
    window.addEventListener("online", hsResume);
    window.addEventListener("focus", hsResume);
    if (!_hsWakeTimer) _hsWakeTimer = setInterval(hsWakeProbe, 2000);
    if (!_hsQueueTimer) _hsQueueTimer = setInterval(hsQueuePump, 10000);
  }
}

// 不属于任何一段会话的事件。认下来返回 true,调用方就不再往会话视图里找。
//
// 目前只有一条:task.updated——TASKS.md 或 task-ledger.json 变了,由服务端每秒 stat
// 出来推(见 task_watch)。标题/状态和 Agent 轻量进展位于不同文件，两者都可能由后端外的
// 进程写入；因此不能只在当前 API 响应后重画。收到之后清掉缓存再重画,
// 不清的话 _taskStats/_taskItems 里还是旧的,画出来的仍是上一份。
function hsDispatchGlobal(type, p) {
  if (type === "server.connected") {
    hsRuntimeConnected(p.runtime || {});
    return true;
  }
  if (type === "task.updated") {
    const did = p.domain || "";
    if (did && typeof invalidateTasks === "function") invalidateTasks(did);
    if (typeof renderTaskDock === "function") renderTaskDock(true);
    if (typeof onTasksChanged === "function") onTasksChanged(did);
    return true;
  }
  // 注册表变了(agent 在对话里部署了 app、reconcile 补了默认依赖)。同样由服务端 stat 出来推,
  // 客户端那条每 2.5 秒的轮询因此退成兜底(见 app-init 的 appRevPoll)。
  if (type === "apps.updated") {
    if (typeof onAppsChanged === "function") onAppsChanged(p.rev);
    return true;
  }
  return false;
}

// 用户消息的 part 后端也会广播(_run 里发过一遍),不过滤就会把用户自己的话当助手正文再画一次。
// 角色以 message.updated 记下的为准,记不到时退到 id 前缀(适配器建 id 用 msg_u_ / msg_a_)。
function hsIsUser(v, msgId) {
  const r = v.roles.get(msgId);
  return r ? r === "user" : String(msgId || "").startsWith("msg_u_");
}

// 无可见内容的 part:步骤标记与压缩标记。渲染它们只会多出空容器。
// 一张图从哪儿取。新记录里存的是 `/session/<id>/asset/<name>` 引用(见 engine_adapters
// 的 ASSET_SEG 一段):浏览器自己去取、自己缓存,正文里因此不再背着 base64。
// data_b64 那一支留给改动之前落盘的记录——它们不会被回填,少了这一支就再也画不出来。
function hsImgSrc(a) {
  if (!a) return "";
  if (a.url) return a.url;
  return a.data_b64 ? `data:${a.media_type || "image/png"};base64,${a.data_b64}` : "";
}

function hsSkipPart(q) {
  const t = q && q.type;
  // 与后端 is_async_result 一致：旧服务/缓存中的同步工具结果不能再画成返回气泡。
  // 以通知来源和任务标识为依据，不根据命令内容或完成状态猜测异步执行。
  if (t === "execution-result") {
    if (q.delivery) return !["task_notification", "agent_message"].includes(q.delivery);
    const taskId = String(q.task_id || "");
    return !taskId || /:[0-9a-f]{16}$/.test(taskId);
  }
  return t === "step-start" || t === "step-finish" || t === "compaction";
}

// 状态条开头那个词。working / ready 说的是引擎在准备或已恢复,那不是异常,用类别名;
// warning / error 直接把严重程度摆出来,与「任务」并排时一眼分得出这一行要不要管。
const HS_ENV_KIND = { working: "引擎", ready: "引擎", warning: "警告", error: "错误",
                      update: "更新" };
function hsEnvironmentKind(p, state) {
  return state === "ready" && p.stage === "context_degraded" ? "提醒"
    : (HS_ENV_KIND[state] || "引擎");
}
function hsEnvironmentReadyDuration(p) {
  return p.stage === "context_degraded" ? 5000 : 800;
}

// 详情是不是「摘要那句话的完整版」。引擎报错走的就是这条:后端把首行截到 120 字当摘要,
// 整段原文当详情(见 engine_adapters._notice_head)。是的话展开就地摊开这一行,
// 不在下面再摆一遍同一句;不是的话(MCP 那种一次十几条)照旧列在下面。
function hsEnvFull(label, details) {
  const stem = String(label || "").replace(/…+$/, "").trim();
  if (!stem || (details || []).length !== 1) return "";
  return details[0].startsWith(stem) ? details[0] : "";
}

function hsEnvironmentHide(v, immediate) {
  const el = v.envEl;
  if (!el) return;
  clearTimeout(v.envTimer); v.envTimer = 0;
  clearTimeout(v.envHideTimer); v.envHideTimer = 0;
  const reset = () => {
    el.hidden = true;
    el.className = "hs-env hs-col";
    el.dataset.short = ""; el.dataset.full = "";   // 摊开态留着,下一条会顶着上一条的正文出现
    hsContextStatusSync(v);
  };
  if (immediate) { reset(); return; }
  el.classList.add("out");
  v.envHideTimer = setTimeout(reset, 180);
}

// 错误与引擎状态统一放在输入框上方,不把诊断正文混进某一轮的过程状态。
// 成功只是瞬时反馈;警告与失败留着,点开才展开完整内容。
// 帧的生产者有两处:codex 常驻进程的启动态走 SSE,引擎自身的状态(如 claude 未登录)
// 由 /config 带回来——后者在第一个回合之前就该出现,等不到 SSE。
// 引擎有没有新版本。走的是同一条窄条,类别词写「更新」。
//
// 只在这条窄条空着的时候画:引擎报错、未登录比「有个新版本」要紧得多,不能被它顶掉。
// 所以调用点在 cf.environment 的 else 分支上,不是并列。
//
// 关掉之后本次会话不再提:版本这件事以天计,一轮回合里反复弹是噪音。× 一按就记下,
// 换一段会话或下次打开照旧提醒。
const _hsUpdateMuted = new Set();
const _hsRecallOpened = new Set();
const _hsRecallOpening = new Set();
function hsUpdateNotice(v, up) {
  if (!up || !up.hasUpdate || !up.latest) return;
  const key = up.engine + "@" + up.latest;
  if (_hsUpdateMuted.has(key)) return;
  if (v.engineUpdateNotice && v.engineUpdateNotice.updateKey !== key
      && v.engineUpdateNotice.state !== "working" && !v.engineUpdateNotice.unknown) {
    v.engineUpdateNotice = null;
  }
  v.updateKey = key;
  hsEnvironment(v, {
    state: "update", stage: "version",
    label: `${up.engine} 有新版本：当前 ${up.have || "?"}，最新 ${up.latest}`,
    // 更新命令仍进详情,点开就能看清它到底要跑什么——一键执行不该是个盲盒。
    details: up.how ? [up.how] : [],
    // 那条命令由后端从 manifest 取(与 `desktron install` 同一份声明),这里只报「更新哪家」,
    // 不把命令行传回去:传回去等于开一个任意执行的口子。
    action: up.how ? { label: "更新", kind: "engine-update", engine: up.engine } : null,
  });
}

// 一键更新。跑的是 manifest 里那条安装命令,由后端执行——它几十秒才回来,期间钮变成
// 「更新中…」并禁掉,免得人连点几次装三遍。
// 成了就把这一条换成「已更新到 x.y.z,重开引擎生效」:新版本要下一次起进程才用上,
// 不说这一句,人会以为点完就换了。败了把引擎的原话摊在详情里,不吞。
async function hsShepherdReconcile(v, attemptID, btn) {
  if (!attemptID || btn.disabled) return;
  const confirmed = confirm("隔离这轮无法验证的 Shepherd 内容？\n\n私有来源和审计证据会完整保留；系统不会重跑本轮。后续上下文会加入一条明确的未知边界，不会静默回退到上一轮。");
  if (!confirmed) return;
  const back = btn.textContent;
  btn.disabled = true; btn.textContent = "处理中…";
  try {
    await api(`/session/${encodeURIComponent(v.sid)}/reconcile`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ action: "omit_and_continue", attempt_id: attemptID }),
      timeout: 120000 });
    hsEnvironment(v, { state: "ready", stage: "recovery",
      label: "已隔离无法验证的内容，并从未知边界继续" });
    await hsEngineSync(v);
    await hsStateSync(v);
    if (!v.busy) hsReconcile(v);
  } catch (error) {
    hsEnvironment(v, { state: "error", stage: "recovery",
      label: "Shepherd 本轮仍保持封锁",
      details: [error?.message || "无法确认旧原生进程已停止，请稍后重试"] ,
      action: { kind: "shepherd-reconcile", label: back, attemptID } });
  } finally {
    btn.disabled = false; btn.textContent = back;
  }
}

async function hsDeliveryAcceptUnknown(v, requestID, btn) {
  if (!requestID || btn.disabled) return;
  const confirmed = confirm("上一轮可能已经执行，但原生终态无法确认。\n\n继续后系统不会重发上一轮；其工具副作用也不会被撤销。是否保留未知边界并继续？");
  if (!confirmed) return;
  const back = btn.textContent;
  btn.disabled = true; btn.textContent = "处理中…";
  try {
    await api(`/session/${encodeURIComponent(v.sid)}/request/${encodeURIComponent(requestID)}/accept-unknown`,
      { method: "POST", timeout: 30000 });
    v.pendingSend = null; v.sending = null; hsQIssue(v, null);
    hsDraftSave(v, true); hsQPaint(v);
    hsEnvironment(v, { state: "ready", stage: "delivery",
      label: "已保留上一轮未知边界，可以继续" });
    await hsStateSync(v);
  } catch (error) {
    hsEnvironment(v, { state: "error", stage: "delivery",
      label: "上一轮仍保持封锁", details: [error?.message || "无法确认请求状态"],
      action: { kind: "delivery-accept-unknown", label: back, requestID } });
  } finally {
    btn.disabled = false; btn.textContent = back;
  }
}

async function hsEngineUpdate(v, engine, btn) {
  if (!engine || btn?.disabled || v.engineUpdateNotice?.state === "working"
      || v.engineUpdateNotice?.unknown) return;
  const notice = { engine, updateKey: v.updateKey || "", state: "working", stage: "version",
    label: DeskI18n.t("ctx.updatingTitle", { engine }),
    details: [DeskI18n.t("ctx.updatingHint")],
    action: { kind: "engine-update", engine, label: DeskI18n.t("ctx.updating") } };
  v.engineUpdateNotice = notice;
  // 更新的是屏幕上真正可见的按钮，不再只改已被 CSS 隐藏的旧状态条。
  hsContextStatusSync(v);
  let r = null;
  let unknown = false;
  try {
    r = await api("/workbench/engine-update", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ engine }), timeout: 620000 });
    unknown = !r || typeof r.ok !== "boolean";
  } catch (e) {
    const msg = (e && e.message) || String(e);
    // 404 表示旧后端缺少入口；其他连接/超时错误不能证明安装没有执行，不自动重试。
    const missing = e?.status === 404 || /\b404\b/.test(msg);
    // 后端还在跑旧代码时，保留明确诊断，不把接口缺失误报为安装失败。
    r = { ok: false, out: missing ? DeskI18n.t("ctx.updateEndpointMissing") : msg };
    unknown = !missing;
  }
  const ok = r?.ok === true && !unknown;
  Object.assign(notice, { state: ok ? "ready" : (unknown ? "warning" : "error"), unknown,
    label: DeskI18n.t(ok ? "ctx.updateSuccess" : (unknown ? "ctx.updateUnknown" : "ctx.updateFailed"), { engine }),
    details: [DeskI18n.t(ok ? "ctx.updateSuccessHint" : (unknown ? "ctx.updateUnknownHint" : "ctx.updateFailedHint")),
      r?.out || (!ok ? DeskI18n.t("ctx.updateNoOutput") : "")].filter(Boolean),
    action: null });
  if (ok) _hsUpdateMuted.add(notice.updateKey);
  hsContextStatusSync(v);
}

function hsEnvironmentBegin(v, sig, now, state) {
  // Duplicate SSE/config frames must not cancel, extend or resurrect a ready
  // notice. Other states keep the existing four-second noise suppression.
  if (sig === v.envSig && (state === "ready" || now - (v.envSigAt || 0) < 4000)) return false;
  v.envSig = sig; v.envSigAt = now;
  clearTimeout(v.envTimer); v.envTimer = 0;
  clearTimeout(v.envHideTimer); v.envHideTimer = 0;
  return true;
}

function hsEnvironmentRelevant(v, p) {
  if (p.engine && v.engine && p.engine !== v.engine) return false;
  const model = String(v.sessionOpts?.model || '');
  const provider = v.engine === 'pi' ? (model.includes('/') ? model.split('/')[0] : '') : v.engine;
  if (p.stage === 'auth' && p.provider && provider && p.provider !== provider) return false;
  return !(p.stage === 'auth' && !p.engine && v.shepherdMode);
}
function hsEnvironmentSnapshot(v, p, revision) {
  if (revision && revision < (v.envRevision || 0)) return;
  if (p) { hsEnvironment(v, p); return; }
  if (revision) v.envRevision = revision;
  if (v.envCurrent && (v.envCurrent.issueRevision || ['auth', 'recap'].includes(v.envCurrent.stage))) {
    v.envCurrent = null; v.envSig = ''; hsEnvironmentHide(v, true);
  }
}

function hsEnvironment(v, p) {
  const el = v.envEl;
  if (!el || !p) return;
  if (p.scope === 'turn' && p.generation && Number(p.generation) < (v.turnGeneration || 0)) return;
  if (p.issueRevision && p.issueRevision < (v.envRevision || 0)) return;
  if (p.issueRevision) v.envRevision = p.issueRevision;
  if (!hsEnvironmentRelevant(v, p)) {
    if (v.envCurrent && !hsEnvironmentRelevant(v, v.envCurrent)) {
      v.envCurrent = null; v.envSig = ''; hsEnvironmentHide(v, true);
    }
    return;
  }
  if (p.clear) {
    v.envCurrent = null; v.envSig = ''; hsEnvironmentHide(v, true); hsContextStatusSync(v); return;
  }
  const previous = v.envCurrent;
  if (!p.issueRevision && previous && ['warning', 'error'].includes(previous.state)
      && p.state === 'ready' && p.stage !== previous.stage) return;
  v.envCurrent = p;
  hsContextStatusSync(v);
  if (p.issueID) {
    try { if (sessionStorage.getItem('desk-error-muted:' + v.sid + ':' + p.issueID)) return; } catch {}
  }
  const state = ["working", "ready", "warning", "error", "update"].includes(p.state)
    ? p.state : "working";
  const details = (p.details || []).map((x) => String(x || "").trim()).filter(Boolean);
  let label = String(p.label || "正在准备引擎");
  if (p.stage === 'auth' && p.engine) {
    const source = p.provider && p.provider !== p.engine ? `${p.engine} / ${p.provider}` : p.engine;
    label = `${source}：${label}`;
  }
  // 持久型问题过去不带 details 时，has-detail 与箭头会一起消失，看起来像展开功能被移除。
  // warning / error 都保留一份完整详情；短句展开后内容相同，但披露控件与交互契约稳定。
  if (["warning", "error"].includes(state) && !details.length) details.push(label);
  const mcp = p.mcp || {};
  if (p.stage === "mcp" && state === "working" && mcp.total && !/\d/.test(label)) {
    label += ` · ${(mcp.ready || 0) + (mcp.failed || 0)}／${mcp.total}`;
  }
  const sig = JSON.stringify([state, p.stage || "", label, details,
    mcp.ready || 0, mcp.failed || 0, mcp.total || 0, p.action || null,
    p.issueRevision || 0]);
  const now = Date.now();
  if (!hsEnvironmentBegin(v, sig, now, state)) return;
  v.envStage = p.stage || "";
  if (v.envStage !== "recall") {
    const panel = el.querySelector(".hs-recall-actions");
    if (panel) panel.remove();
  }
  el.hidden = false;
  el.className = "hs-env hs-col " + state + (details.length ? " has-detail" : "");
  // 类别词占任务条「任务」那个位置,严重程度由它的颜色带。摘要里不再重复说一遍「警告」,
  // 后端那几条标签也随之去掉了结尾的「存在警告」。
  el.querySelector(".hs-env-k").textContent = hsEnvironmentKind(p, state);
  el.querySelector(".hs-env-t").textContent = label;
  // 动作钮:只有帧里带 action 才出。登录这类事要在真终端里交互,条上光有文字等于让人自己去找。
  // TUI 退场后没有「切回终端视图」这一说了,登录改为开一个内嵌 shell 跑引擎自己的登录命令。
  const act = el.querySelector(".hs-env-a");
  const a = p.action && p.action.label ? p.action : null;
  act.hidden = !a;
  if (a) {
    act.textContent = a.label;
    act.onclick = (ev) => {
      ev.stopPropagation();
      if (a.kind === "terminal") hsLoginShell(v, "login", p);
      else if (a.kind === "engine-update") hsEngineUpdate(v, a.engine, act);
      else if (a.kind === "recall") hsRecallPanel(v, true);
      else if (a.kind === "recall-open") hsRecallOpen(v, a.sessionID);
      else if (a.kind === "shepherd-reconcile") hsShepherdReconcile(v, a.attemptID, act);
      else if (a.kind === "delivery-accept-unknown") hsDeliveryAcceptUnknown(v, a.requestID, act);
    };
  }
  // 详情只是同一句话的完整版时(引擎报错常是这样:摘要就是它截断的头部),展开不另起一块,
  // 而是把这一行本身摊开。上一版两处都画,于是同一句英文报错在屏幕上摆了两遍。
  const detail = el.querySelector(".hs-env-d");
  const full = hsEnvFull(label, details);
  el.dataset.short = label;
  el.dataset.full = full;
  detail.textContent = full ? "" : details.join("\n");
  detail.hidden = true;
  el.classList.remove("open");
  el.title = details.join("\n") || label;
  el.tabIndex = -1;
  el.setAttribute("aria-expanded", "false");
  requestAnimationFrame(() => el.classList.add("on"));
  if (state === "ready")
    v.envTimer = setTimeout(() => hsEnvironmentHide(v), hsEnvironmentReadyDuration(p));
}

// 输入框上方提示条承载仍需处理的环境问题与恢复动作。失败回合本身的结构化错误同时写进
// 该回合的可展开过程区；状态顶行仍只保留“已停止”，不把原因塞进终态标签。
function hsErrorNotice(v, message, stage, details, identity) {
  message = String(message || "会话出错").trim() || "会话出错";
  const first = (message.split("\n")[0] || message).trim();
  const label = first.length > 120 ? first.slice(0, 119).trimEnd() + "…" : first;
  hsEnvironment(v, { ...identity, state: "error", stage: stage || "ui", label,
    details: (details || [message]).filter(Boolean) });
}

// 后端已在 error_notice 中选择当前提示；同一错误的两种通知共用这份投影，
// 前端不再用“已有警告”猜测优先级。旧后端缺少投影时仍展示原始错误。
function hsTurnErrorNotice(v, p) {
  if (p.environment) { hsEnvironment(v, p.environment); return; }
  if (p.engine && !hsEnvironmentRelevant(v, { ...p,
      stage: p.error?.code === 'AUTH' ? 'auth' : 'turn' })) return;
  const message = String((p.error || {}).message || "会话出错").trim() || "会话出错";
  const current = v.envCurrent || null;
  const action = (current || {}).action || null;
  if (current && current.stage === "delivery" && action && action.kind === "delivery-accept-unknown") {
    const details = [...(current.details || []), message].map((x) => String(x || "").trim()).filter(Boolean);
    hsEnvironment(v, { ...current, details: [...new Set(details)] });
    return;
  }
  const retry = p.retry ? hsRetrySummary(p.retry) : "";
  hsErrorNotice(v, message, "turn", [retry, message], {scope:'turn', generation:p.generation,
    issueID:p.issueID || (p.generation ? 'turn:' + v.sid + ':' + p.generation : undefined)});
}

function hsEnvironmentToggle(v) {
  const el = v.envEl;
  if (!el || !el.classList.contains("has-detail")) return false;
  const open = el.classList.toggle("open");
  // 详情是同一句话的完整版时摊开这一行自己；否则在下面列出详情。
  const full = el.dataset.full || "";
  if (full) el.querySelector(".hs-env-t").textContent = open ? full : (el.dataset.short || "");
  else el.querySelector(".hs-env-d").hidden = !open;
  el.setAttribute("aria-expanded", String(open));
  el.querySelector(".hs-env-chev").setAttribute("aria-label", open ? "收起详情" : "展开详情");
  return open;
}

function hsPeerSync(v, peer) {
  if (!v || !v.peerEl || peer === undefined) return;
  const epoch = String((peer || {}).epoch || "");
  if (epoch && epoch !== String(v.peerEpoch || "")) {
    v.peerEpoch = epoch;
    v.peerRevision = 0;
  }
  const revision = Math.max(0, Number((peer || {}).revision || 0));
  if (revision < Number(v.peerRevision || 0)) return;
  v.peerRevision = revision;
  if (!peer || peer.active === false) {
    v.peerAsk = null;
    v.peerEl.hidden = true;
    return;
  }
  v.peerAsk = { ...peer, active: true };
  const source = String(peer.source || "另一个会话").trim().replace(/[:：]\s*$/, "");
  const phase = peer.phase === "queued" ? "等待处理" : "正在处理";
  const main = v.peerEl.querySelector(".hs-peer-main");
  main.textContent = source;
  main.title = source;
  v.peerEl.querySelector(".hs-peer-phase").textContent = phase;
  v.peerEl.hidden = false;
}

function hsStateGeneration(p) {
  const n = Number((p || {}).generation || 0);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

// generation 是回合栅栏：旧回合迟到的 idle/ready 不能解锁已经开始的新回合。
function hsStateTake(v, p) {
  const gen = hsStateGeneration(p);
  if (gen && v.turnGeneration && gen < v.turnGeneration) return false;
  if (gen) v.turnGeneration = gen;
  return true;
}

function hsTurnSettled(v, p) {
  const gen = hsStateGeneration(p);
  // 同一代重复到达平时不必再收一次尾。但界面仍显示为忙,说明上一次没收干净,这一次要收:
  // 去重是为了别把收尾的装饰画两遍,不是为了守着一个错的忙态不动
  // (2026-09-05 用户实见:摁了 esc 已经中断,状态一直停在「处理中」,只能刷新)。
  if (gen && v.settledGeneration === gen && !v.busy) return;
  // 撤回那一轮的收尾另走一条路:界面在 hsInterrupt 里已经归位,不该再画一次。
  // 认这一代,不认个数——原来是个只增不减的计数器,而撤回的那一轮未必等得到 ready
  // (按得够快时后端那一轮已经收尾,abort 找不到 live ctx,一帧状态都不发)。留下来的
  // 那个 1 会把后面某一轮真正的收尾吞掉,那一轮就永远停在「处理中」。
  if (v.recallIdle && (!gen || !v.recallGen || gen === v.recallGen)) {
    v.recallIdle = Math.max(0, (v.recallIdle || 0) - 1);
    v.recallGen = 0;
    v.noDrain = false;
    v.interrupted = false;
    v.interruptDrop = false;
    v.awaitAsk = false;
    hsAskClose(v);
    hsSeen(v);
    if (gen) v.settledGeneration = gen;
    return;
  }
  // 卡片还等着人回应时不收尾。收尾会给这一回合挂上时间戳与复制/分支那一排,那是
  // 「已经结束」的记号;而人一批准,回合还会继续吐字,于是界面自相矛盾:底下写着结束时间,
  // 上面又在「处理中」(2026-09-02 手机端实见)。后端那侧已经不在等人时收口了,这里再守
  // 一道:两处独立成立,任一处漏了另一处还兜得住。
  if (v.awaitAsk) {
    // 这一代还没真收尾,所以不能记进 settledGeneration。原来记了,等人批准、回合真结束时
    // 收尾帧带着同一个 generation 到达,被开头那道去重闸挡掉——状态就永远停在「处理中」,
    // 卡片明明已经点过「已允许」(2026-09-03 用户实见)。
    // 记账的语义是「这一代已经收过尾」,而这里恰恰是没收。
    return;
  }
  const wasInterrupted = v.interrupted || v.interruptDrop || !!p.interrupted;
  const wasFailed = !!p.failed;
  const attempt = v.promptDispatch;
  // 前端只能把下一代预测为「上一代 + 1」，后端为了跨重启唯一会同时取当前毫秒作下界，
  // 因此本轮真实 generation 常常大于预测值。只要终态没有被 hsStateTake 判为旧帧，
  // 大于等于本地预测的 generation 就属于当前请求；严格相等会让已完成请求的草稿存根残留。
  if (attempt && (!gen || attempt.generation <= gen)) {
    if (v.sending === attempt.sending) v.sending = null;
    if ((v.pendingSend || {}).requestID === attempt.id) v.pendingSend = null;
    v.promptDispatch = null;
    hsQPaint(v); hsDraftSave(v, true);
  }
  v.awaitAsk = false;
  hsAskClose(v);
  hsFlush(v);
  hsFinalize(v);
  // 收尾帧带着成文时刻与生成引擎。实时回合的消息 id 里只有起点时间,不补上的话回复底下
  // 显示的就是提问那一刻。操作行可能已经建好(报错、打断会先于收尾帧定稿),所以就地改写。
  // 此刻持有回合锁的只有正在收尾的这一轮,下一轮要等 ready 才开始,末轮就是它。
  const tail = (v.turns || [])[(v.turns || []).length - 1];
  if (tail && tail.msgId) {
    if (p.at) {
      tail.at = p.at;
      const tm = tail.footEl && tail.footEl.querySelector(".hs-time");
      if (tm) tm.textContent = hsWhen(p.at);
    }
    if (p.engine) tail.engine = p.engine;
    if (p.model) tail.model = p.model;
    if (p.usage) tail.usage = p.usage;
    if (p.engine || p.model || p.usage) hsGenPaint(v, tail);
  }
  // A product-owned manual compaction has a separate durable terminal. Its
  // internal model turn may emit idle first; that must not consume the progress
  // bar or release the composer before checkpoint verification completes.
  if (!v.manualCompact && (v.compactT0 || v.compactEl)) {
    hsCompactOff(v); v.cmpLive = false;
  }
  if (wasInterrupted) { v.interrupted = false; v.interruptDrop = false; hsStatus(v, "done", "已中断"); }
  else if (wasFailed) {
    hsProcFailure(tail, p.error);
    hsTurnErrorNotice(v, p);
    hsStatus(v, "error", "已停止");
  }
  else hsStatus(v, "done", "已完成");
  hsBusy(v, false);
  hsIndex(v);
  // 欠着的那次重建在这里还:回合跑着时被挡下的对账(见 hsReconcileNow 的 recOwed),
  // 补的正是断线窗口里丢掉的那截内容。不还的话只能退出重开才看得到
  // (2026-09-03 用户实见:显示已完成但正文只有几行)。
  // adopt 一并做:欠账多半来自恢复那条路,它要的就是从引擎记录重建一次。
  const owed = v.recOwed;
  v.recOwed = false;
  hsReconcile(v);
  if (owed) {
    // 防抖那 600ms 里若又忙起来,这一次同样会被挡;再挂一次兜底,间隔取得比防抖长。
    setTimeout(() => {
      if (v.el && v.el.isConnected && !v.busy) hsReconcileNow(v).catch(() => {});
    }, 1500);
  }
  hsSeen(v);
  hsGoalSync(v); hsAgentsSync(v); hsTasksSync(v);
  // 档位也核一次:/model 换模型改的是引擎自己的会话状态,Desk 这边的 opts 一个字没动,
  // 也不会发 session.settings。不核的话档位钮停在旧值,直到人切一次页才更新——
  // 而换模型的人恰恰会盯着那一格看有没有生效(2026-09-02 用户指出)。
  // 回合收尾是最早能知道真实模型的时刻:引擎在这一轮的消息里报过 model(见 last_model)。
  hsEngineSync(v);
  if (gen) v.settledGeneration = gen;
}

function hsTurnReady(v, p) {
  if (!hsStateTake(v, p)) return;
  const gen = hsStateGeneration(p);
  // 同上:重复的 ready 只在界面已经归位时才当成冗余丢掉。会话空闲时 /state 报的
  // generation 是上一轮的(见 turn_state 的 _turn_seq 那一支),自愈探测拿回来的正是
  // 这样一份「重复」的 ready——原来在这里就被挡掉,等于把最后一条纠正通道也关了。
  if (gen && v.readyGeneration === gen && !v.busy && v.turnReady) return;
  // ready 本身也是可靠收尾信号；即使前一帧 idle 落在 SSE 断线窗口里，界面仍能归位。
  if (v.busy || v.interrupted || v.interruptDrop) hsTurnSettled(v, p);
  v.turnReady = true;
  if (gen) v.readyGeneration = gen;
  if (v.noDrain) v.noDrain = false;
  else hsQFlush(v);
}

// pre:已经发出去、还在飞的那个请求。取数可以提前,应用不行——它要落在正文铺好之后
// (hsTurnReady 那一路认的是已经存在的回合)。所以把「发」和「等」拆开:hsLoad 一进门
// 就把这一趟发出去,与 /message 并行跑,到原来的位置再收。经隧道时一个往返一两秒,
// 两趟串起来就是双份(2026-08-25 实测开一次会话要打四个请求)。
async function hsStateSync(v, pre) {
  let st = null;
  try { st = await (pre || api(`/session/${encodeURIComponent(v.sid)}/state`)); } catch { return null; }
  if (st && Object.prototype.hasOwnProperty.call(st, "peer")) hsPeerSync(v, st.peer);
  if (st && !hsStateTake(v, st)) return null;
  if (st && Object.prototype.hasOwnProperty.call(st, "execution_review"))
    hsExecutionReviewSync(v, st.execution_review);
  if (st) hsCompactStateSync(v, st);
  // Manual compaction is a control operation, not an ordinary model turn.
  // Between admission and worker start /state can still say ready; compacting
  // is the stronger state and must keep the composer fenced.
  if (st && st.state && !(st.compacting && st.state === "ready"))
    hsApply(v, "session.state", st);
  if (st) hsAskStateSync(v, st);
  if (st) hsRecallStateSync(v, st);
  return st;
}

function hsRecallStateSync(v, st) {
  v.recallState = st.recall || null;
  const pending = v.recallState && v.recallState.phase !== "done";
  if (!pending) {
    const panel = v.envEl && v.envEl.querySelector(".hs-recall-actions");
    if (panel) panel.remove();
    if (v.recallShown && v.envStage === "recall") hsEnvironmentHide(v, true);
    v.recallShown = false;
    return;
  }
  v.recallShown = true;
  const recovered = v.recallState.session_id;
  const failed = !!v.recallState.auto_failed;
  hsEnvironment(v, { state: recovered ? "ready" : failed ? "warning" : "working", stage: "recall",
    label: recovered ? "已恢复到安全副本" : failed ? "自动恢复未完成" : "正在自动恢复撤回",
    details: failed ? [v.recallState.error || "可以再次尝试，原会话不会被覆盖"] : [],
    action: recovered ? { kind: "recall-open", label: "打开副本", sessionID: recovered }
      : failed ? { kind: "recall", label: "再次尝试" } : null });
  if (recovered) hsRecallOpen(v, recovered);
}

async function hsRecallOpen(v, sessionID, draft, atts) {
  if (!sessionID || hsHidden(v) || _hsRecallOpened.has(sessionID) ||
      _hsRecallOpening.has(sessionID)) return false;
  _hsRecallOpening.add(sessionID);
  try {
    await openHtmlSession(sessionID, "恢复副本");
    _hsRecallOpened.add(sessionID);
    const restored = _hsViews.get(sessionID);
    if (restored && !hsInpText(restored.inp) && !(restored.atts || []).length) {
      hsFill(restored, draft || ""); restored.atts = atts || []; hsAttPaint(restored);
      hsDraftSave(restored, true);
    }
    return true;
  } catch (err) {
    hsStatus(v, "warn", "恢复副本已就绪", "打开失败，可以稍后再试");
    hsEnvironment(v, { state: "warning", stage: "recall", label: "恢复副本已就绪",
      details: ["自动打开失败，原会话仍然保留"],
      action: { kind: "recall-open", label: "打开副本", sessionID } });
    return false;
  } finally {
    _hsRecallOpening.delete(sessionID);
  }
}

async function hsRecallPanel(v, force) {
  if (v.recallResolving) return;
  v.recallResolving = true;
  const draft = hsInpText(v.inp), atts = [...(v.atts || [])];
  let st = null;
  try { st = await hsStateSync(v); } catch {}
  const op = st && st.recall;
  if (!op || op.phase === "done" || op.session_id || st.state !== "ready" ||
      (op.auto_failed && !force)) {
    v.recallResolving = false;
    if (op && op.session_id) hsRecallOpen(v, op.session_id, draft, atts);
    return;
  }
  try {
    const result = await api(`/session/${encodeURIComponent(v.sid)}/recall`, {
      method: "POST", headers: { "content-type": "application/json" }, timeout: 150000,
      body: JSON.stringify({ operation_id: op.id, action: "auto" }) });
    if (result.session_id) await hsRecallOpen(v, result.session_id, draft, atts);
    else { await hsStateSync(v); await hsReconcile(v); }
  } catch (err) {
    // 请求可能已经完成但响应丢失；只相信持久状态，不把恢复再执行一遍。
    const current = await hsStateSync(v);
    if (current && current.recall && current.recall.phase === "done") hsReconcile(v);
    else if (current && current.recall && current.recall.session_id)
      await hsRecallOpen(v, current.recall.session_id, draft, atts);
    else hsStatus(v, "warn", "自动恢复未完成", String(err.message || err));
  } finally {
    v.recallResolving = false;
    await hsStateSync(v);
  }
}

// 权限卡与压缩同理:permission.asked 只在发生那一刻推一次,服务端不重放。手机切后台、
// 锁屏、蜂窝与 Wi-Fi 切换都会断开 SSE,那一帧就此丢失——卡片再也不出现,而回合还在等人答
// (2026-09-03 用户实见:选项卡在手机上没收到)。回前台时用 /state 里的 asks 补齐。
// 已经画着的不重画:hsAsk 那一路自己会先 hsAskClose,重画会把人选了一半的选项清掉。
function hsAskStateSync(v, st) {
  if (!Array.isArray(st.asks)) return; // 旧后端及非快照事件不推断审批已结束。
  const asks = st.asks;
  if (v.ask && asks.some(p => p.id === v.ask.id)) return;
  hsAskClose(v);
  v.awaitAsk = false;
  if (!asks.length) {
    if (HS_WAIT_LABELS.includes(v.statusLabel)) hsStatus(v, "working", "处理中");
    return;
  }
  const p = asks[0];
  if (!p || !p.id) return;
  v.awaitAsk = true;
  hsStatus(v, "working", p.tool === "AskUserQuestion" ? "等待你回答…" : "等待你批准…");
  hsAsk(v, p);
}

// 压缩的开始/结束平时走 SSE，但手机切后台时那条连接会被挂起，
// 收尾帧不会重放。回前台时用 /state 的可查压缩态对账：还在压就续着画，
// 已结束就撤掉 95% 的估算条。这里不用回合 generation：手动 /compact 不是普通回合，
// 它的 generation 不会变，那道去重闸正是旧页面无法自愈的原因。
function hsCompactTerminal(v, compact) {
  compact ||= {};
  if (v.manualCompactID && compact.operation_id !== v.manualCompactID) return false;
  if (compact.result === "not_started" && v.manualCompactTurn) {
    v.manualCompactTurn.el?.remove();
    v.turns = (v.turns || []).filter((turn) => turn !== v.manualCompactTurn);
    if (typeof hsIndex === "function") hsIndex(v);
  }
  v.manualCompactTurn = null;
  hsCompactOff(v);
  v.cmpLive = false;
  v.cmpAnchor = undefined;
  v.manualCompact = false;
  v.manualCompactID = "";
  if (v.compactPollTimer) clearTimeout(v.compactPollTimer);
  v.compactPollTimer = 0;
  hsBusy(v, false);
  v.turnReady = true;
  if (compact.result === "not_started") {
    hsStatus(v, "warn", "压缩未开始，请重试");
  } else if (compact.result === "failed" || compact.error) {
    hsErrorNotice(v, "压缩失败：" + (compact.error || "引擎未说明原因"), "compact");
    hsStatus(v, "error", "已停止");
  } else {
    hsStatus(v, "done", compact.result === "cancelled" ? "压缩已取消"
      : compact.result === "noop" ? "无需压缩" : "已压缩");
  }
  hsReconcile(v);
  hsQFlush(v);
  return true;
}

function hsCompactStateSync(v, st) {
  const compact = st.compact || {};
  if (v.manualCompactID && compact.operation_id !== v.manualCompactID) return;
  const started = Number(st.compacting || 0);
  if (started > 0) {
    if (compact.operation_id) {
      v.manualCompact = true;
      v.manualCompactID = compact.operation_id;
    }
    hsBusy(v, true);
    v.turnReady = false;
    if (!v.compactT0) {
      hsFlush(v); v.cmpAnchor = hsCompactAnchor(v);
      hsStatus(v, "working", "正在压缩上下文");
      hsCompactOn(v, started);
      v.cmpLive = true;
    }
    return;
  }
  if (!v.compactT0) return;
  if (st.state === "running") {
    hsStatus(v, "working", "处理中");
    return;
  }
  if (st.state !== "ready") return;
  hsCompactTerminal(v, compact);
}

function hsToolProgressMerge(part, q, priorStatus) {
  if (!part || !q || !q.progressOnly) return false;
  // SSE 有序，但页面切换与恢复可能让旧帧晚到；终态一旦可见，预览绝不能降回 running。
  if (priorStatus && priorStatus !== "running") return false;
  const next = q.state || {}, old = part.state || {};
  part.state = { ...old, status: "running", output: String(next.output || ""),
    outputMore: Number(next.outputMore || 0),
    progressRevision: Number(next.progressRevision || 0),
    progressTotal: Number(next.progressTotal || 0) };
  part.progress = { revision: part.state.progressRevision, total: part.state.progressTotal,
                    preview: part.state.output };
  return true;
}

function hsToolProgressQuiet(q, part, priorStatus) {
  return !!(q && q.progressOnly && part && part.el
    && !part.el.classList.contains("open")
    && priorStatus === "running" && String((q.state || {}).status || "") === "running");
}

function hsDeliveryBlocked(v, delivery) {
  if (!delivery) return;
  hsQIssue(v, "state", "正在核对上一轮执行结果");
  hsBusy(v, false);
  const cleanup = delivery.status === "cleanup_required";
  hsEnvironment(v, { state: "warning", stage: "delivery",
    label: cleanup ? "上一轮尚未派发，正在等待清理消息投影" : "上一轮可能已经执行，已暂停后续消息",
    details: [cleanup ? "系统会重试精确清理该用户消息，不会发送到模型。"
                      : "系统不会自动重发上一轮。请先核对历史和外部副作用，再决定是否继续。"],
    action: { kind: "delivery-accept-unknown",
              label: cleanup ? "重试清理" : "保留未知边界并继续",
              requestID: delivery.request_id } });
}

function hsExecutionReviewSync(v, review) {
  v.executionReview = review && typeof review === "object" ? review : null;
  hsStatusPaint(v);
}

function hsApply(v, type, p) {
  if (type === "session.environment") { hsEnvironment(v, p); return; }
  if (type === "session.execution_review") {
    if (!hsStateTake(v, p)) return;
    hsExecutionReviewSync(v, p.review);
    return;
  }
  if (type === "session.peer") { hsPeerSync(v, p); return; }
  if (type === "session.state") {
    if (Object.prototype.hasOwnProperty.call(p || {}, "peer")) hsPeerSync(v, p.peer);
    if (!hsStateTake(v, p)) return;
    if (Object.prototype.hasOwnProperty.call(p || {}, "execution_review"))
      hsExecutionReviewSync(v, p.execution_review);
    hsAskStateSync(v, p);
    if (p.retry && p.state !== "ready") hsRetrySync(v, p.retry);
    else if (p.activity && v.retry) hsRetrySync(v, {active: false, generation: p.generation});
    if (p.activity) hsActivitySync(v, p.activity);
    if (p.recall) hsRecallStateSync(v, p);
    // The unified /compact turn publishes its ordinary ready before the outer
    // product command has verified and projected the checkpoint terminal.
    if (p.state === "ready" && v.manualCompact) return;
    if (p.state === "ready") { hsTurnReady(v, p); return; }
    v.turnReady = false;
    if (p.state === "blocked" && p.delivery) {
      hsDeliveryBlocked(v, p.delivery);
      return;
    }
    if (p.state === "running") {
      if (!v.busy) { hsBusy(v, true); hsStatus(v, "working", "处理中"); }
      else if (v.statusLabel === "发送中") hsStatus(v, "working", "处理中");
    } else if (p.state === "cancelling") {
      v.interrupted = true;
      v.interruptDrop = true;
    }
    return;
  }
  if (type === "message.updated") {
    const info = p.info || {};
    if (v.needLoad) return;   // 冷页只留空壳；露面后从权威历史一次重建
    if (info.id && v.mobileDroppedMids && v.mobileDroppedMids.has(info.id)) return;
    if (v.interruptDrop && info.role === "assistant") return;
    if (info.id) v.roles.set(info.id, info.role || "assistant");
    if (info.role === "user") {
      const t = v.turns[v.turns.length - 1];
      // 序号由后端在回合开始时给(见 _seq_next),随这条消息一起下来。自续轮的落位靠它
      // 对齐——界面自己数不出顺序:排队的消息与子代理交回可以撞在同一刻。
      // 只有自己刚发出、还等着后端给 id 的那一轮才认领这个 id(t.mine 由 hsDispatch 打)。
      // 原来只看「末尾那一轮有没有 uid」:本地发出到自己的 message.updated 回来之间有一个
      // 往返的空窗,这期间若别处(手机)那条先到,它的 id 就被盖到本地这一轮头上——外来那句
      // 因为本地这轮已有正文而被跳过、永不显示,本地自己的 id 随后走下面那支又建一个回合,
      // 同一句话出现两次,助手回复还可能按 uid 挂错宿主。两端同时发正是这个功能的场景。
      if (t && !t.uid && t.mine) { t.uid = info.id; t.seq = info.seq || 0; t.mine = false; }
      // 别处发的那一句:本地没有对应的回合,得在这里补一个,否则它的气泡永远不出现,
      // 只有刷新之后从记录里重建才看得到(2026-08-18 实见:手机上排队发出,桌面这侧
      // 只见回复不见提问)。正文这会儿还没到,随后的 part.updated 会填进来。
      // 判据是「这个 id 我没见过」而不是「末尾那一轮有没有 uid」:本地自己发的那一路
      // 在 POST 之前就建好了回合并挂上 id,走不到这里。
      else if (info.id && !v.turns.some((x) => x.uid === info.id)) {
        // 外来消息真正开跑后才会广播到这里；同一会话的回合锁保证上一轮此刻已经结束。
        // 本地发送会在 hsDispatch 里先释放旧状态节点，外来消息此前漏了这一步：下一枚
        // running 状态会把同一个节点从上一轮搬到新轮，上一轮的「已完成」因此消失，
        // 直到刷新按历史重建才回来。这里补齐同一条回合边界，并清掉上一轮的附属摘要。
        hsStatusClear(v);
        v.stats = null;
        v.todoNow = v.todoNext = "";
        const nt = hsNewTurn(v, "", info.id, [], info.at || 0);
        nt.seq = info.seq || 0;
        hsScroll(v);
      }
    }
    if (info.role === "assistant" && !(v.mobileDroppedMids && v.mobileDroppedMids.has(info.id))) {
      hsTurnFor(v, info.id);
    }
    hsTouch(v);
    return;
  }
  if (type === "message.part.delta") {
    if (v.needLoad) return;
    if (hsIsUser(v, p.messageID)) return;
    if (v.interruptDrop) return;
    // 手机为硬内存预算释放了旧回合；迟到帧已由后端写入权威历史，不能在底部另造孤立回合。
    if (v.mobileDroppedMids && v.mobileDroppedMids.has(p.messageID)) return;
    hsTouch(v);
    const field = p.field === "reasoning" ? "reasoning" : "text";
    if (!hsIsAside(v, p.messageID)) {
      // 思考流过来时,把最后一句留作副行:顶行只报阶段,具体在想什么放下面一行。
      if (field === "reasoning") hsLogThink(v, p.delta || "");
      // 顶行报此刻的形态。间隙里(工具跑完、正文还没开始)人要知道的是「还在想」还是
      // 「在出字」,而不是这一轮累计做过什么——后者是不变量,占着这个位置等于什么都没说。
      const phase = field === "reasoning" ? "正在思考" : "正在输出";
      // Pi 的 reasoning 常以几十毫秒一帧到达。阶段没变时重写 class、重挂同一个节点并
      // 重画文字没有新信息,却会让呼吸动画在 WebView 里显得急促甚至近似常亮。
      // 秒表有自己的 1s tick,这里只在「思考 / 输出」真正切换时更新阶段。
      if (v.statusState !== "working" || v.statusLabel !== phase) hsStatus(v, "working", phase);
    }
    // 新的文字块意味着上一段文字已经稳定；先按完整 markdown 定稿，再接活动尾部。
    // 同一个 part id 会被跳过，不影响正常逐字流式。
    const part = hsPart(v, p.messageID, p.partID, field);
    hsSettleNarrative(v, part.turn, p.messageID, p.partID);
    hsQueue(v, part, p.delta || "");
    return;
  }
  if (type === "message.part.updated") {
    const q = p.part || {};
    if (v.needLoad) return;
    if (v.mobileDroppedMids && v.mobileDroppedMids.has(q.messageID)) return;
    // 用户消息的 part 原本一律丢掉(气泡由本地发送那一路自己画)。但别处发来的那一句,
    // 上面刚补了一个空回合,正文正是靠这一帧填进去的——只认那种「还没有正文」的回合,
    // 本地已经画好的不受影响。
    if (hsIsUser(v, q.messageID)) {
      const t = v.turns.find((x) => x.uid === q.messageID);
      if (t && q.type === "text" && q.text && !t.user) {
        hsSetTurnUser(v, t, q.text, q.peer_head); hsTouch(v); hsScroll(v);
      }
      // 截图那一半原来整个丢掉:desktop 发的图,手机上那条消息只有文字,要刷新重建
      // 才看得到(2026-08-26 用户实见)。本地发的回合不收——它建回合时就画了 base64
      // 那份,再收 SSE 带来的 /session 引用会同一张图出现两遍(src 不同,去重认不出)。
      if (t && q.type === "image" && (q.url || q.data_b64)
          && !(t.atts || []).some((a) => a.type === "image" && a.data_b64)) {
        const src = q.url || "";
        if (!(t.atts || []).some((a) => a.type === "image" && a.url === src)) {
          t.atts = t.atts || [];
          t.atts.push({ type: "image", media_type: q.media_type, url: q.url, data_b64: q.data_b64 });
          hsTurnImgs(v, t);
          hsTouch(v); hsScroll(v);
        }
      }
      if (t && q.type === "file" && q.url
          && !(t.atts || []).some((a) => a.type === "file" && a.url === q.url)) {
        t.atts = t.atts || [];
        t.atts.push({ type: "file", name: q.name, context_kind: q.context_kind,
                      media_type: q.media_type, url: q.url });
        const bub = t.el && t.el.querySelector(".hs-u .hs-bubble");
        if (bub) {
          const ln = document.createElement("div"); ln.className = "hs-u-file";
          ln.textContent = q.name || "文件"; bub.appendChild(ln);
        }
        hsTouch(v); hsScroll(v);
      }
      return;
    }
    if (hsSkipPart(q)) return;
    if (v.interruptDrop) return;
    if (q.type === "text" && isInjected(q.text)) return;   // 引擎插件注入的系统提示,不是模型输出
    hsTouch(v);
    // 工具真正开始，或引擎切到新的文字 part，都是上一段 reasoning/text 的可靠边界。
    // tool 的完成帧不重复触发：它可能晚到下一段 reasoning 之后，不能反向把新段定稿。
    const part = hsPart(v, q.messageID, q.id, q.type || "text");
    const priorToolStatus = q.type === "tool" ? String((part.state || {}).status || "") : "";
    if (["text", "reasoning"].includes(q.type)
        || (q.type === "tool" && (q.state || {}).status === "running")) {
      hsSettleNarrative(v, part.turn, q.messageID, q.id);
    }
    if (q.type === "tool" && q.progressOnly) {
      if (!hsToolProgressMerge(part, q, priorToolStatus)) return;
    } else hsMergePart(part, q);
    if (q.type === "tool" && hsToolProgressQuiet(q, part, priorToolStatus)) {
      // 后端明确标成纯 output progress，且本客户端没有展开：只在内存中接住最新预览，
      // 完全跳过状态行、任务栏、工具 DOM、布局事务与滚动。其他工具更新绝不走这条捷径。
      return;
    }
    if (q.type === "tool") {
      const st = q.state || {};
      const o = hsToolObj(q);
      // 只说在跑哪个工具,不带实参:参数常常是一整条 shell 命令,摆在状态行上又长又噪,
      // 而展开区里本来就逐条列着,要看细节去那儿看。
      // 有具体动作就报具体动作(「执行 python3」),没有才回到「处理中」——
      // 「处理中 Bash」是两句话叠在一起,前半句什么也没说。
      if (hsIsAside(v, q.messageID)) {
        // 自续轮的步骤不写进消息流那盏灯:它有自己那盏(在宿主回合上)。见 hsIsAside。
      } else if (st.status === "running") {
        // 有明确任务名(模型自己写的那句 description)就直接用它:「执行中」讲的是动作类别,
        // 任务名已经把这一步在干什么说清楚了,两者并排是同一件事说两遍。
        // 只有拿不到任务名时才退回动词。
        // 顶行只报形态,副行报此刻具体在做什么——有模型自己写的那句任务说明就用它,
        // 没有才退回「动词 + 对象」。归并计数不放这里:它是不变量,而这一行的价值
        // 恰恰在于它一直在变。
        hsStatus(v, "working", "执行中");
        hsLogStep(v, !o.code && o.t ? o.t
                                    : hsToolVerb(q.tool, true) + (o.t ? " " + o.t : ""));
      } else {
        // 这一步跑完了。别写「正在思考」——那时我们并不知道它在想还是接着要调下一个工具,
        // 真在思考会有 reasoning 增量过来,那一路自己会把状态改过去。这里只说「处理中」。
        hsStatus(v, "working", "处理中");
        hsLogStep(v, hsToolOutcome(q, false) + (o.t ? " " + o.t : ""));
      }
      hsTodoSync(v, q);
      // 派出子代理的那一刻就刷一次:等整轮收尾才出现的话,最该看它的那段时间反而没有。
      // 工具实测叫 Agent 不叫 Task(2026-08-08 对着真实回合核的),两个名字都认。
      if (/^(task|agent|subagent)$/.test(String(q.tool || "").toLowerCase())
          && priorToolStatus !== String(st.status || "")) hsAgentsSync(v);
    }
    hsPaintLive(v, part);
    hsScroll(v);
    return;
  }
  if (type === "session.settings") {
    // 档位在别处被改了(另一台设备,或同一台的另一个页面)。原来后端不广播,改完只有
    // 发起那一端知道,别处停在打开页面时读到的值:mac 上调成 max,手机仍显示 high,
    // 除非把会话页关掉重开(2026-08-25 用户实见)。
    if (p && p.opts && typeof p.opts === "object") {
      const eventEngine = p.engine || p.selected_engine || v.engine;
      if (v.shepherdMode) {
        v.shepherdEngineState = v.shepherdEngineState || {};
        const slot = v.shepherdEngineState[eventEngine] || {};
        v.shepherdEngineState[eventEngine] = Object.assign({}, slot, {
          opts: p.opts, catalog: p.options || slot.catalog || {} });
      }
      if (!v.shepherdMode || eventEngine === v.engine) v.sessionOpts = p.opts;
      if (v.shepherdMode && p.selected_engine) {
        v.producerEngine = p.producer_engine || v.producerEngine || p.selected_engine;
        // A late event from an older click must not overwrite the newest local
        // selection. Its persisted result is still safe and will be superseded.
        if (!v.shepherdDesiredEngine && !v.shepherdSwitching
            && p.selected_engine !== v.engine) {
          v.engine = p.selected_engine;
          v.catalog = p.options || v.catalog || {};
          if (typeof hsCatalogRemember === "function") hsCatalogRemember(v.engine, p.options);
          v.modelPick = ""; v.modelPickBase = ""; v.realModel = "";
        }
      }
      // 后端随事件带当前模型；仅引擎名表示尚未自报具体模型，带“·”则取尾部实值。
      if ((!v.shepherdMode || eventEngine === v.engine) && typeof p.model === "string") {
        const pretty = (typeof hsPrettyModel === "function") ? hsPrettyModel(p.model) : "";
        const tail = p.model.includes(" · ") ? p.model.split(" · ").pop() : "";
        const base = pretty || tail || "";
        v.realModel = (base && base !== p.model) ? base : "";
      }
      if (!v.shepherdMode || eventEngine === v.engine) hsOptsPaint(v);
    }
    return;
  }
  if (type === "session.bgtasks") { v.bgtasks = p; hsBashPaint(v); return; }
  if (type === "session.stats") { v.stats = p; hsStatusPaint(v); return; }
  if (type === "session.compact") {
    if (p.generation && !hsStateTake(v, p)) return;
    // Unified Shepherd 的手动 /compact 外层会在检查点已提交后给出权威收尾；内部原生
    // contextCompaction 帧带 messageID/generation，只用于执行，不得把手动线误标成自动，
    // 更不能在检查点尚未验证时提前画成成功。
    if (v.shepherdMode && v.manualCompact && p.messageID) return;
    if (v.manualCompactID && p.operation_id !== v.manualCompactID) return;
    const manualTerminal = (!!p.operation_id && p.trigger === "manual" && !p.running);
    // The outer operation terminal is authoritative and must not depend on
    // cmpLive: an ordinary idle can arrive first or the running SSE can be lost.
    if (manualTerminal) {
      hsTouch(v);
      if (!(p.result === "failed" || p.error || p.result === "cancelled" || p.result === "noop")) {
        hsCompactRow(v, "", p, v.cmpAnchor); hsScroll(v);
      }
      hsCompactTerminal(v, p);
      return;
    }
    hsTouch(v);
    // 这条事件到没到过前端,原来在界面上看不出来:压缩期间引擎一帧内容都不发,收不到
    // 与「收到了但没画」长得一样。留一行日志,下次压缩从日志面板就能分清是哪一种
    // (2026-08-26:手机端只显示「处理中」、不见进度条,后端已验证发了 running=true)。
    // 每回合开头也会来一条 status=requesting,它两样都空,不记——那会把面板刷满。
    if (p.running || p.result || p.error) {
      deskLog(`[hs] session.compact ${v.sid} running=${!!p.running}`
              + ` result=${p.result || "-"} trigger=${p.trigger || "-"}`);
    }
    if (p.running) {
      if (p.trigger === "manual" && p.operation_id) {
        v.manualCompact = true;
        v.manualCompactID = p.operation_id;
        hsBusy(v, true);
        v.turnReady = false;
      }
      if (!v.cmpLive) { hsFlush(v); v.cmpAnchor = hsCompactAnchor(v); }
      hsStatus(v, "working", "正在压缩上下文"); hsCompactOn(v, p.started || Date.now());
      v.cmpLive = true;   // 开了一次,收尾那一帧才认(见下)
    } else {
      hsCompactOff(v);
      // 收尾帧要配得上一次「开始」才算数,否则不画线也不报错。
      //
      // 后端是每来一条 system/status 就发一次 session.compact(见 engine_adapters 那一支),
      // 而 status 帧不只在压缩时发——每回合开头还有一条 status=requesting。压缩过之后,
      // 后续 status 帧里 compact_result 仍可能带着上一次的值,前端这边只要帧里有 result
      // 就画一条线,于是压缩早已结束、记录里那条详细的分隔线也画好了,过一会儿又在末尾
      // 冒出一条光秃的(2026-08-26 用户实见「压缩完出现两条线,刷新后恢复」——刷新只按
      // 记录重建,所以恢复成一条)。
      //
      // codex 手动 /compact 那条路也有同样的毛病:compact_session 先在 thread/compacted
      // 的中继里发一次 running=False,返回后又无条件发一次,一次压缩两条收尾帧。
      //
      // 用 v.cmpLive 配对:running=true 开一次,收尾消费掉。落单的收尾帧一律忽略。
      // 它挂在 v 上而不是 DOM 上,所以重建不会把它冲掉;刷新是新视图,记录那条路照常画。
      if (!v.cmpLive) return;
      v.cmpLive = false;
      // 压缩这一回合模型不产出任何内容。不留一条痕迹的话,界面上就是消息发出去、
      // 等了一分多钟、然后什么也没发生。摘要正文要等对账重建才有,先摆一条分隔线。
      // 位置在开始压缩时固定：紧跟最后一块输出，并留在那块输出所属的正文或过程区。
      //
      // 失败要单独说。claude 会因为「消息还不够多」直接拒掉这次压缩(实测原话
      // Not enough messages to compact.),原来这一支与成功走同一条路、又因为没有
      // pre/post/trigger 而什么也不画——界面上就是敲完 /compact 一切如常,
      // 人会以为压过了。
      if (p.result === "retrying") {
        hsStatus(v, "working", "正在恢复上下文");
      } else if (p.result === "failed" || p.error) {
        hsErrorNotice(v, "压缩失败：" + (p.error || "引擎未说明原因"), "compact");
        hsStatus(v, v.busy ? "working" : "error", v.busy ? "处理中" : "已停止");
      } else if (p.result === "cancelled" || p.result === "noop") {
        hsStatus(v, v.busy ? "working" : "done",
          p.result === "cancelled" ? "压缩已取消" : "无需压缩");
      } else if (!(p.result === "failed" || p.error || p.result === "cancelled" || p.result === "noop")
                 && (p.pre || p.post || p.trigger || p.result)) {
        hsCompactRow(v, "", p, v.cmpAnchor);
        hsScroll(v);
        // codex 那条路的收尾帧带着 pre/post,分隔线一次就画全了;claude 只给成败,
        // token 与摘要正文都要等记录落盘。这里先摆一条光秃的线——它至少说明
        // 「这里换了上下文」;那两样由回合收尾那次对账按记录补上(见 hsTurnSettled),
        // 不在这儿另叫一次:此刻 v.busy 还是真的,hsReconcileNow 会当场退出。
      }
      v.cmpAnchor = undefined;
    }
    return;
  }
  // 引擎自己续的那一轮:某个异步子代理交回,harness 往上下文里塞一条通知,引擎就着它
  // 读报告、判断下一步。它不是用户这一轮,不进气泡,而是并进上一个回合——一条分割线
  // 接在那一回合下面,内容长在线下,那一回合的状态灯跟着改回处理中(见 hsAsideRow)。
  // 不碰 v.busy 与消息流那盏灯:用户这一轮有没有跑完是另一件事,由它自己的帧说了算。
  if (type === "session.aside") {
    // 只有 mark 一种:内容一直在本回合里长着,后端在这一刻追认「从第几块起是交回的那一段」。
    // 这里不搬内容、不新建回合,只在分界处插一条线并给那几块标出处。
    // 旧的 open/close/revoke 三态 2026-08-20 连同后端的影子 ctx 一起撤了:那一版内容收在
    // 影子里、要整批铺过来,判错了还要撤回重画。两套并存期间它从别的口子冒出来过——
    // 同一段回答在用户消息上下各铺了一遍,而且两遍的排版不一样(一遍是流式增量渲染,
    // 一遍是 hsAsideFill 的整段成型)。
    if (p.phase === "mark") hsAsideMark(v, p);
    return;
  }
  if (type === "permission.resolved") {
    if (v.ask?.id === p.id) {
      const label = {approved: "已允许", denied: "已拒绝", timeout: "审批已过期，操作未执行",
        cancelled: "审批已取消", connection_error: "审批回执发送失败"}[p.resolution] || "审批已结束";
      const el = v.ask.el;
      if (el) {
        el.querySelector(".hs-acts")?.remove();
        if (p.resolution === "approved") {
          const icon = el.querySelector(".hs-ico"); if (icon) icon.innerHTML = HS_ICONS.unlock;
        }
        const line = el.querySelector(".hs-t2"); if (line) line.textContent = label;
      }
      v.ask = null; v.awaitAsk = false;
      hsStatus(v, "working", "处理中");
    }
    hsAskStateSync(v, p);
    hsStateSync(v).catch(() => {});
    return;
  }
  if (type === "permission.asked") {
    v.awaitAsk = true;
    hsStatus(v, "working", p.tool === "AskUserQuestion" ? "等待你回答…" : "等待你批准…");
    hsAsk(v, p);
    return;
  }
  // 服务端明确报告排队时显示排队，不再用“长时间没字”猜。queue_end 或首个模型内容会清掉。
  if (type === "session.queue") {
    if (!v.busy || !hsStateTake(v, p)) return;
    v.providerQueue = p.waiting ? { waiting: true,
      ...(Number.isInteger(p.position) ? { position: p.position } : {}) } : null;
    hsStatusPaint(v);
    return;
  }
  // 引擎没在动:网络断了 claude 会自己重试,期间一帧都不发。界面上分不出「在想」和
  // 「连不上」,由后端替它报一声。恢复时会补一条 seconds=0。
  if (type === "session.stalled") {
    if (!v.busy || v.compactT0) return;   // 压缩期间本来就不发帧,那不是卡住
    if (!hsStateTake(v, p)) return;
    if (p.activity) hsActivitySync(v, p.activity);
    else v.stallSec = p.seconds || 0;
    hsStatusPaint(v);
    return;
  }
  // 引擎在退避重试:claude 连不上时会重试十次(605ms → 39s,合计约三分钟)才放弃,
  // 期间一帧内容都不发。TUI 那侧一直把「第几次」显示出来,这里照做——正在重试和真的卡死
  // 是两回事,前者只要等着就行,后者才需要人介入。
  if (type === "session.retry") {
    if (!v.busy) return;
    hsRetrySync(v, p);
    return;
  }
  if (type === "session.error") {
    if (!hsStateTake(v, p)) return;
    if (v.manualCompact) return;
    if ((p.error || {}).code === "prompt_not_started" && hsPromptRejected(v, p)) return;
    // Structured errors are diagnostics; ready/idle own terminal projection.
    // Legacy servers retain their former fallback until they advertise contract=1.
    if (p.contract === 1) { hsTurnErrorNotice(v, p); return; }
    v.turnReady = false;
    v.providerQueue = null;
    v.interruptDrop = false;
    hsFlush(v);            // 已流出的内容先定型保留,引擎崩了也不丢可见内容
    hsFinalize(v);
    // 过程区只说这一轮停了；完整错误统一留在输入框上方，可展开查看。
    hsTurnErrorNotice(v, p);
    hsStatus(v, "error", "已停止");
    hsBusy(v, false);
    return;
  }
  if (type === "session.idle") {
    if (!hsStateTake(v, p)) return;
    if (v.manualCompact) return;
    hsTurnSettled(v, p);
    // contract=1 时 idle 只负责展示收尾；队列要等紧随其后的 ready。
    // 旧后端没有状态契约，仍沿用 idle 解锁，支持开发时前后端短暂版本不一致。
    if (!p.contract) hsTurnReady(v, p);
    return;
  }
}
