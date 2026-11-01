// desktron 前端(app.js 拆分):日志悬浮窗(LogDock)
// 经典脚本按序加载共享全局作用域,加载顺序见 index.html。勿单独重排(依赖 app-core 的 api)。
//
// 设置里"日志悬浮窗"开关打开后,右下角常驻一个悬浮钮;点开是实时日志面板,
// 轮询 /workbench/logs(ui_server 进程 stdout/stderr 捕获,见 lib/logbuf.py)。
// 自包含:DOM 与样式都在本文件注入,不占 style.css/index.html 结构位。
// 面板打开才轮询(1s 增量,since 游标);关闭即停,不产生后台流量。
(function LogDockModule() {
  const POLL_MS = 1000;
  const POS_KEY = "cd_logdock_pos";   // 悬浮钮位置(右/下边距),拖动后存 localStorage
  let dock = null, panel = null, body = null, timer = null, since = 0, autoScroll = true;
  let pos = { right: 16, bottom: 16 };
  try { const p = JSON.parse(localStorage.getItem(POS_KEY) || "null"); if (p && p.right >= 0 && p.bottom >= 0) pos = p; } catch {}

  const css = `
  #logdock-btn { position: fixed; z-index: 9990;
    padding: 6px 12px; border-radius: 999px; border: 1px solid var(--border-base);
    background: var(--surface-float); color: var(--text-weak); font-size: 12px;
    cursor: grab; box-shadow: 0 2px 10px var(--shade-35); user-select: none; touch-action: none; }
  #logdock-btn:hover { color: var(--text-base); background: var(--surface-active); }
  #logdock-panel { position: fixed; z-index: 9990;
    width: min(560px, calc(100vw - 32px)); height: 340px; display: flex; flex-direction: column;
    background: var(--surface-float); border: 1px solid var(--border-base); border-radius: 10px;
    box-shadow: 0 8px 30px var(--shade-50); overflow: hidden; }
  #logdock-panel .ld-head { display: flex; align-items: center; gap: 8px;
    padding: 6px 10px; border-bottom: 1px solid var(--border-weak-base);
    color: var(--text-base); font-size: 12px; }
  #logdock-panel .ld-head .ld-title { flex: 1; }
  #logdock-panel .ld-head button { border: none; background: none; color: var(--text-weak);
    cursor: pointer; font-size: 12px; padding: 2px 6px; }
  #logdock-panel .ld-head button:hover { color: var(--text-strong); }
  #logdock-panel .ld-head input { width: 130px; padding: 2px 8px; font-size: 11px;
    color: var(--text-base); background: var(--surface-base);
    border: 1px solid var(--border-weak-base); border-radius: 6px; outline: none; }
  #logdock-panel .ld-head input:focus { border-color: var(--border-base); }
  #logdock-body { flex: 1; overflow: auto; padding: 6px 10px;
    font-family: var(--font-family-mono); font-size: 11px; line-height: 1.5;
    color: var(--text-base); white-space: pre-wrap; word-break: break-all; }
  #logdock-body .ld-err { color: #e08f8f; }
  #logdock-panel .ld-ui { color: #7fb7e0; }
  #logdock-body .ld-t { color: var(--text-weaker); }`;

  function ensureStyle() {
    if (document.getElementById("logdock-style")) return;
    const st = document.createElement("style");
    st.id = "logdock-style"; st.textContent = css;
    document.head.appendChild(st);
  }

  let store = [], filter = "";   // 面板会话内的行存量:换过滤词时从这里重渲,不丢已收的行

  function matches(x) {
    return !filter || (x.t + " " + x.ln).toLowerCase().includes(filter);
  }

  // 一行的颜色按来源分档。err 一直是红的;ui 是前端经 deskLog 写进来的诊断(见 app-core.js),
  // 单独给一档蓝——它与后端的 stdout 穿插着显示,同色的话读的人分不清「这句是谁说的」,
  // 而前后端各自看到的状态对不对得上,恰恰是这个面板要回答的问题。
  function makeRow(x) {
    const row = document.createElement("div");
    if (x.s === "err") row.className = "ld-err";
    else if (x.s === "ui") row.className = "ld-ui";
    const t = document.createElement("span"); t.className = "ld-t"; t.textContent = x.t + " ";
    row.appendChild(t); row.appendChild(document.createTextNode(x.ln));
    return row;
  }

  function renderRows(lines) {
    if (!body || !lines.length) return;
    const frag = document.createDocumentFragment();
    for (const x of lines) frag.appendChild(makeRow(x));
    body.appendChild(frag);
    while (body.childNodes.length > 2000) body.removeChild(body.firstChild);  // 与后端缓冲同界,防久开撑爆 DOM
    if (autoScroll) body.scrollTop = body.scrollHeight;
  }

  // 前端长任务:主线程被同步占住超过 120ms 的那些。后端日志看不到它们,而「点开应用整个界面
  // 卡住」这类现象只在这一侧留痕。记进同一个面板,与后端日志按时间穿插着读。
  // 只在面板打开时才记,平时不装观察器。
  let _ltObs = null;
  function longtaskOn(on) {
    if (!on) { if (_ltObs) { _ltObs.disconnect(); _ltObs = null; } return; }
    if (_ltObs || typeof PerformanceObserver !== "function") return;
    try {
      _ltObs = new PerformanceObserver((list) => {
        const rows = [];
        for (const e of list.getEntries()) {
          if (e.duration < 120) continue;
          const who = (e.attribution || []).map((a) => a.containerName || a.containerId
                       || a.containerSrc || a.name).filter(Boolean).join(" ") || "主文档";
          const t = new Date().toTimeString().slice(0, 8);
          rows.push({ t, s: "out", ln: `[前端] 卡顿 ${Math.round(e.duration)}ms · ${who}` });
        }
        if (rows.length) appendLines(rows);
      });
      _ltObs.observe({ entryTypes: ["longtask"] });
    } catch { _ltObs = null; }
  }

  function appendLines(lines) {
    store.push(...lines);
    if (store.length > 2000) store = store.slice(-2000);
    renderRows(lines.filter(matches));
  }

  function rerender() {
    if (!body) return;
    body.innerHTML = "";
    renderRows(store.filter(matches));
  }

  function place() {
    // 钮按存储的右/下边距落位;面板跟随钮,并夹紧视口(拖到边角/顶部时不出界)
    if (dock) { dock.style.right = pos.right + "px"; dock.style.bottom = pos.bottom + "px"; }
    if (panel) {
      const pw = Math.min(560, window.innerWidth - 32);
      panel.style.right = Math.max(8, Math.min(pos.right, window.innerWidth - pw - 8)) + "px";
      panel.style.bottom = Math.max(8, Math.min(pos.bottom + 40, window.innerHeight - 348)) + "px";
    }
  }

  async function poll() {
    try {
      const r = await api(`/workbench/logs?since=${since}`, { timeout: 5000 });
      if (r && Array.isArray(r.lines)) { appendLines(r.lines); since = r.next || since; }
    } catch {}  // ui_server 短暂不可达(如重启)静默,下一轮自续
  }

  function openPanel() {
    if (panel) return;
    panel = document.createElement("div"); panel.id = "logdock-panel";
    const head = document.createElement("div"); head.className = "ld-head";
    // 标题同时当图例用:「前端」两个字就着 ui 那一档的颜色写,不必另占一行说明。
    // 面板里现在是两条流并排,标题只写 ui_server 会让人以为前端那些行是后端打的。
    const title = document.createElement("span"); title.className = "ld-title";
    title.textContent = "实时日志(ui_server · ";
    const uiTag = document.createElement("span"); uiTag.className = "ld-ui"; uiTag.textContent = "前端";
    title.appendChild(uiTag); title.appendChild(document.createTextNode(")"));
    const ft = document.createElement("input"); ft.type = "search"; ft.placeholder = "关键字过滤";
    const clr = document.createElement("button"); clr.textContent = "清屏";
    const x = document.createElement("button"); x.textContent = "×";
    head.appendChild(title); head.appendChild(ft); head.appendChild(clr); head.appendChild(x);
    body = document.createElement("div"); body.id = "logdock-body";
    panel.appendChild(head); panel.appendChild(body);
    document.body.appendChild(panel);
    ft.oninput = () => { filter = ft.value.trim().toLowerCase(); autoScroll = true; rerender(); };   // 大小写不敏感子串;新行按同一过滤词跟进
    clr.onclick = () => { store = []; body.innerHTML = ""; };
    x.onclick = closePanel;
    // 贴底才自动跟进;用户上翻查旧日志时不打扰
    body.onscroll = () => { autoScroll = body.scrollHeight - body.scrollTop - body.clientHeight < 8; };
    autoScroll = true; since = 0; store = []; filter = "";   // 每次打开从缓冲头重放(封顶 500 行),再增量跟进
    longtaskOn(true);
    place();
    poll();
    timer = setInterval(poll, POLL_MS);
  }

  function closePanel() {
    longtaskOn(false);
    if (timer) { clearInterval(timer); timer = null; }
    if (panel) { panel.remove(); panel = null; body = null; }
  }

  function setEnabled(on) {
    ensureStyle();
    if (on && !dock) {
      dock = document.createElement("button");
      dock.id = "logdock-btn"; dock.type = "button"; dock.textContent = "DEBUG";
      dock.title = "实时日志,可拖动(设置·Debug 里可关闭悬浮窗)";
      let dragged = false;
      dock.onclick = () => { if (!dragged) (panel ? closePanel() : openPanel()); };
      // 拖动换位:pointer capture(同 panelResize 的手法),移动超 4px 算拖不算点,松手存位置
      dock.onpointerdown = (ev) => {
        ev.preventDefault();
        dock.setPointerCapture(ev.pointerId);
        const x0 = ev.clientX, y0 = ev.clientY, r0 = pos.right, b0 = pos.bottom;
        dragged = false;
        dock.onpointermove = (e) => {
          if (!dragged && Math.abs(e.clientX - x0) < 4 && Math.abs(e.clientY - y0) < 4) return;
          dragged = true;
          const r = dock.getBoundingClientRect();
          pos.right = Math.max(8, Math.min(r0 - (e.clientX - x0), window.innerWidth - r.width - 8));
          pos.bottom = Math.max(8, Math.min(b0 - (e.clientY - y0), window.innerHeight - r.height - 8));
          place();
        };
        dock.onpointerup = () => {
          dock.onpointermove = dock.onpointerup = null;
          if (dragged) try { localStorage.setItem(POS_KEY, JSON.stringify(pos)); } catch {}
        };
      };
      document.body.appendChild(dock);
      place();
    } else if (!on && dock) {
      closePanel();
      dock.remove(); dock = null;
    }
  }

  window.LogDock = { setEnabled };

  // 自装配:按本机运行态决定是否挂悬浮钮(与 init 的 /workbench/state 拉取重复一次,换取模块自包含)
  // 首页内联的开机数据里已经有 state(见 ui_boot.BOOT_PARTS),先用它;经隧道每一趟都是真往返。
  const _pre = (typeof window !== "undefined" && window.__BOOT__) || null;
  const _st0 = _pre && _pre["/workbench/state"];
  (_st0 ? Promise.resolve(_st0) : api("/workbench/state", { timeout: 8000 }))
    .then((st) => { if (st && st.ui && st.ui.log_overlay) setEnabled(true); })
    .catch(() => {});
})();
