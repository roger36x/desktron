// 由 app-assets.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 3/6 片
// ---- 主区：资产查看器 ----
function showView(mode) {
  if (typeof closeResourcePage === "function") closeResourcePage(false);
  const ca = $("#conv-actions"); if (ca) ca.innerHTML = "";   // 页头动作区随视图切换清空(由各页面自装,如设置页的「更新」)
  document.body.classList.toggle("term-on", mode === "terminal");   // 手机端附加键栏只在终端在场时出现
  $("#conv-head").hidden = mode !== "asset";
  $("#asset-view").hidden = mode !== "asset";
  $("#terminal").hidden = mode !== "terminal";
  const ov = $("#overlay");
  if (mode !== "overlay") {
    ov.hidden = true; ov.classList.remove("open");
    if (typeof closeAllNativeEmbeds === "function") closeAllNativeEmbeds();
    if (overlayOpen) {
      overlayOpen = false;
      if (NATIVE_BROWSER) nb({ action: "hide" });
    }
  }
  // 欢迎页与资产页没有 showOverlay，统一从主视图切换点补记。具体导航项会在随后
  // navPush 再写一次完整 entry；两句同步执行，中间不会留下半态。
  if (mode !== "overlay" && !_restoringEngineViewState && typeof curEngine === "string")
    rememberEngineView(curEngine);
  // 右侧应用与主区并存。主区切视图时原生浏览器可能被全局 hide 按下去；布局安定后
  // 让侧栏当前页重新同步一次即可，页面节点与内部状态都不动。
  if (typeof appSidePages === "function" && typeof appPageShown === "function")
    requestAnimationFrame(() => appSidePages().forEach(appPageShown));
}

// codex 家族:备用屏关掉、重放慢、缓存重现时要抖一行让它重画。前端凡是「因为对方
// 是 codex 才这么做」的分流都问这一处,不写死单个引擎名,再来一个同构引擎只改这里。
// 与档位无关的地方(例如按引擎名存全局 opts)不适用,那些仍按各自的 key 走。
const CODEX_LIKE_ENGINES = ["codex"];
function engineIsCodexLike(engine) { return CODEX_LIKE_ENGINES.includes(engine); }

// 终端嵌入:claude/codex 走真实 TUI。懒加载 xterm,连 /term WebSocket(后端 PTY 起引擎)。
// 曾经列的是「以嵌入式 TUI 形态跑」的引擎。TUI 2026-08-20 退场,表恒空——留着是因为
// 几处判断按它分流(任务执行记录的删除路径、执行入口),空表让它们一律落在 GUI 那一侧。
const TERMINAL_ENGINES = [];
let _termLoaded = null, _term = null, _termWs = null, _termRo = null;   // _term/_termWs/_termRo:指向当前活动 pane(兼容旧引用)
const _termPanes = {};   // sid → pane:{ id, engine, host, term, fit, ws, ro, enc, sendResize, _sentGeom }。保活:切会话只隐藏不销毁,切回直接显示,无重放→无空白污染
let _termWatermark = null;   // 无活动终端时的水印元素(与 pane 同级,挂在 #term-host 下)
const _lastTermByNav = {};     // 工作区(curDomainId)→ 上次在该区看的会话 id;切回来自动恢复,免手点
let _handoffRun = 0;          // 跨引擎恢复的前端任务代号:用户取消/切走后防止旧异步流程抢回主区
let _handoffPreviousTerm = null;
let _handoffDomain = null;
const TERM_SAFE_ROWS = 1;       // xterm 在全屏/高 DPI 下 rows 可能多算一行;保守少报 1 行,防底部输入框被裁
// 拖动窗口时 ResizeObserver 连发几十次。逐次 fit + 通知 PTY 会让 TUI 每帧收到 SIGWINCH、
// 互相打断而错位,所以「告诉 PTY 新尺寸」这一步仍然收敛成停手后一次。
//
// 但 fit 本身(重排 xterm 的行列,纯前端)必须立刻做:只在停手后做的话,拖动过程中画面
// 一直按旧列数排版,窗口变宽时右边空一条、变窄时长行被切掉(2026-08-15 用户实见:
// 终端不跟着页面宽度换行)。分成两半——每帧合并一次 fit,停手后再补一次 fit 加通知。
function debouncedFit(fit, sendResize, ms = 250) {
  let t = 0, raf = 0;
  return () => {
    if (!raf) raf = requestAnimationFrame(() => { raf = 0; try { fitSafe(fit); } catch {} });
    clearTimeout(t);
    t = setTimeout(() => { try { fitSafe(fit); sendResize(); } catch {} }, ms);
  };
}
function loadXterm() {
  if (_termLoaded) return _termLoaded;
  // 失败要把缓存清掉再往外抛。留着的话这个被拒的 Promise 就是终身的:隧道下取一次没取到,
  // 之后每次调用拿到的都是同一个拒绝,只有整页重载才解得开。
  const _p = new Promise((resolve, reject) => {
    const css = document.createElement("link"); css.rel = "stylesheet"; css.href = "/vendor/xterm/xterm.css";
    document.head.appendChild(css);
    const s1 = document.createElement("script"); s1.src = "/vendor/xterm/xterm.js";
    s1.onload = () => {
      const s2 = document.createElement("script"); s2.src = "/vendor/xterm/addon-fit.js";
      s2.onload = () => resolve(!!window.Terminal); s2.onerror = reject;
      document.body.appendChild(s2);
    };
    s1.onerror = reject;
    document.body.appendChild(s1);
  });
  _termLoaded = _p.catch((e) => { if (_termLoaded === _p) _termLoaded = null; throw e; });
  return _termLoaded;
}
function nextFrame() {
  return new Promise((resolve) => requestAnimationFrame(resolve));
}
function fitSafe(fit, reserveRows = TERM_SAFE_ROWS) {
  const term = fit && fit._terminal;
  const dims = fit && fit.proposeDimensions ? fit.proposeDimensions() : null;
  if (!term || !dims || isNaN(dims.cols) || isNaN(dims.rows)) {
    try { fit.fit(); } catch {}
    return;
  }
  const rows = Math.max(1, Math.floor(dims.rows) - reserveRows);
  const cols = Math.max(2, Math.floor(dims.cols));
  if (term.cols !== cols || term.rows !== rows) term.resize(cols, rows);
}
// stableTermGeometry 随 TUI 撤(2026-08-20):它测的是「TUI 该按多少列排版」,
// 起会话时要连同 cols/rows 一起送给后端。GUI 会话没有字符网格,不需要这一步。

function termWsUrl(sessionId, engine, term, contextDomain) {
  // 协议跟随页面:https 直访时必须 wss,写死 ws 会被浏览器拦掉(隧道下的 http 不受影响)
  let url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/term?session=${encodeURIComponent(sessionId)}`;
  if (contextDomain) url += `&context_domain=${encodeURIComponent(contextDomain)}`;
  // 全引擎(含 claude)都带上 fit 后的几何:后端 attach 前据此先 resize PTY 再回放,
  // 使回放字节与客户端列宽一致,避免按旧几何回放导致的折行/定位污染(尤其流式输出窗口)。
  if (term && term.cols > 0 && term.rows > 0)
    url += `&cols=${encodeURIComponent(term.cols)}&rows=${encodeURIComponent(term.rows)}`;
  return url;
}
// 终端键入。修中文输入法掉字/重复:某些输入法(尤其标点)keydown 报 keyCode 229 却不发
// compositionstart/end,直接用 input 事件上屏。xterm 的 _inputEvent 按内部 _keyDownSeen 标志
// 时发时不发(实测 10 次只发 6 次),无条件补发又会重复。且 xterm 的 input 监听是捕获阶段、
// 注册在前,同阶段拦不住。这里在 textarea 的祖先(term.element)上捕获,先于 xterm 把这条坏路径
// 整个截断:229 的 keydown 与对应 input 都 stopPropagation,xterm 不经手,自己发一次。
// 正常英文(keyCode 非 229)、正常中文组词(isComposing=true)都不走此路,放行 xterm。
function wireTermInput(term, ws, enc) {
  const send = (s) => { if (s && ws.readyState === 1) ws.send(enc.encode(s)); };
  const root = term.element;   // textarea 的祖先:捕获阶段在 xterm 的 textarea 监听之前
  let lastKc = 0;
  if (root) {
    // 复制时去掉每行尾部空白:TUI 为铺背景色把整行用空格填满到终端宽,选区会把这些填充空格也带上。
    // 捕获阶段接管 copy,自己写裁剪后的文本(stopPropagation 切掉 xterm 自带的复制)。
    root.addEventListener("copy", (e) => {
      const sel = term.getSelection();
      if (!sel) return;
      e.preventDefault(); e.stopPropagation();
      try { e.clipboardData.setData("text/plain", sel.replace(/[ \t]+$/gm, "")); } catch {}
    }, true);
    root.addEventListener("keydown", (e) => {
      lastKc = e.keyCode;
      if (e.keyCode === 229 && !e.isComposing) e.stopPropagation();   // 切断 xterm 的 229 延迟检查
    }, true);
    root.addEventListener("input", (e) => {
      if (!e.isComposing && lastKc === 229 && e.inputType === "insertText" && e.data) {
        send(e.data);
        try { term.textarea.value = ""; } catch {}   // 防 textarea 累积
        e.stopPropagation();   // 不让 xterm 的 _inputEvent 再发一次
      }
    }, true);
  }
  term.onData((d) => send(d));   // 输入原样送进 PTY
}
// 手机判据:窄屏,或者矮屏 + 触摸(手机横屏宽度能到 844,按宽度判会漏掉)。
// CSS 侧用同一条,两边改要一起改。
const MOBILE_MQ = "(max-width: 820px), (max-height: 520px) and (pointer: coarse)";
function isMobileView() { return window.matchMedia(MOBILE_MQ).matches; }
const TERM_THEMES = {
  dark: {
    // 底色即右栏正文 --ink-10;前景取标题那一档 --ink-77,不再满亮。ANSI 八色降饱和,
    // 蓝/品红向 Desk 强调色 #97a3f7 靠拢——One Dark 那套高饱和色与侧栏并排时像另一个应用。
    background: "#191919", foreground: "#d1d1d1", cursor: "#c8c8c8",
    black: "#3a3a3d", red: "#e2787e", green: "#9ec48a", yellow: "#d9c089",
    blue: "#8ea4f2", magenta: "#bfa0ee", cyan: "#7fc0c6", white: "#cfcfcf",
    brightBlack: "#6d6d72", brightRed: "#ee9096", brightGreen: "#b2d4a0", brightYellow: "#e6cf9e",
    brightBlue: "#a9b7f8", brightMagenta: "#d2b8f6", brightCyan: "#98d1d6", brightWhite: "#ebebeb",
    selectionBackground: "rgba(255,255,255,0.26)",
    selectionInactiveBackground: "rgba(255,255,255,0.13)",
  },
  light: {
    background: "#fbfbfb", foreground: "#343434", cursor: "#5b3f88",
    black: "#3f3f3f", red: "#b93643", green: "#4f793e", yellow: "#846000",
    blue: "#356fc7", magenta: "#80509e", cyan: "#237b84", white: "#e5e5e5",
    brightBlack: "#747474", brightRed: "#cf4652", brightGreen: "#618d4e", brightYellow: "#9a7000",
    brightBlue: "#437dd1", brightMagenta: "#925fae", brightCyan: "#318b94", brightWhite: "#ffffff",
    selectionBackground: "rgba(79,70,184,0.20)",
    selectionInactiveBackground: "rgba(79,70,184,0.11)",
  },
};
function termThemeId() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}
function termTheme(hostEl) {
  const theme = { ...TERM_THEMES[termThemeId()] };
  // 嵌入终端可局部调整底色；xterm 画布与宿主保持一致，普通终端不变。
  if (hostEl) {
    const background = getComputedStyle(hostEl).getPropertyValue("--terminal-background").trim();
    if (background) theme.background = background;
  }
  return theme;
}
function watchTermTheme(term, hostEl) {
  let current = termThemeId();
  const observer = new MutationObserver(() => {
    const next = termThemeId();
    if (next === current) return;
    current = next;
    term.options.theme = termTheme(hostEl);
  });
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  return observer;
}
function termOpts(engine) {   // xterm 统一外观:字体与 Desk 等宽处同源,正常/粗体走真实字重,不强推全粗
  const o = {
    // 与 Desk 其余等宽处同一套(--mono-code):网页取不到 SF Mono,原栈实际落到 Menlo,
    // 汉字又退回无调整的苹方,半格空隙让中文读成「字 间 带 缝」。Desk Mono CJK 把汉字校到两格宽。
    fontFamily: '"Desk Mono", "Desk Mono CJK", Menlo, monospace',
    // 不压缩字距：负字距会让中文、路径和编译输出粘连。桌面与手机同一字号，窄屏靠列数换行。
    // 行距取 1.0(xterm 下限)。Desk Mono 自带行高约 1.32em,13.5px 时一行 18px,与 macOS
    // 终端(SF Mono,约 16.7px/行)同一密度档。此前 1.28 是按 Menlo 调的,换字体后一行涨到
    // 23px,同样高度少显示两成行数(2026-09-23 用户实见,撤回 1.28)。
    fontSize: 13.5,
    lineHeight: 1.0, letterSpacing: 0,
    fontWeight: "normal", fontWeightBold: "bold",
    // 创建时读取当前外观；已打开的终端由 watchTermTheme 跟随切换，不重建 PTY。
    theme: termTheme(), cursorBlink: true,
    // 回滚 5000 行。xterm 默认只有 1000,一次 build 或一段长 diff 就把之前的顶没了
    // (内嵌终端 term-embed.js 一直是 5000,应用终端当初漏了,两份配置就此不一致)。
    scrollback: 5000,
    // 右键双击选词:原生终端的既有行为,xterm 默认关着。
    rightClickSelectsWord: true,
    allowProposedApi: true,   // 含 bracketed paste:引擎启用后粘贴多行文本不会被回车截断
  };
  // windowsMode 关掉 reflow:resize 时文本不软折行,保持绝对光标定位与 buffer 行一一对应。
  // 各引擎都有绝对定位绘制的 TUI 区域(claude 欢迎横幅、codex 输入框),
  // reflow 会把内联 TUI 按软折行重排→绝对坐标映射漂移→底部区域逐次下坠出视口。
  o.windowsMode = true;
  // 鼠标上报的 TUI 会吞掉拖拽选区;Option(mac)按住强制选区,使链接/任意文本可选中、复制。
  o.macOptionClickForcesSelection = true;
  return o;
}
// 终端里的链接路由:网页 URL → 悬浮层 iframe;文件/目录路径 → 悬浮层内嵌查看(/workbench/view)。
function termOpenTarget(raw) {
  let t = String(raw || "").trim().replace(/^['"<(]+/, "").replace(/['">).,;:]+$/, "");
  if (!t) return;
  if (/^https?:\/\//i.test(t)) {
    let title = t; try { title = new URL(t).hostname; } catch {}
    openPage(t, title);   // 网页:右侧悬浮标签(iframe);被禁内嵌时悬浮层有"在浏览器打开"出口
    return;
  }
  const fp = t.replace(/:\d+(:\d+)?$/, "");   // 剥掉 path:line[:col] 后缀
  const name = (fp.replace(/\/+$/, "").split("/").pop()) || fp;
  const glyph = (name.trim()[0] || "·").toUpperCase();   // 叶子名首字母(否则原生标签按本地 hostname 取到 "1")
  // 相对路径是相对当前终端所在工作区的 cwd 打的(不是桌面根),带 base 让后端按该工作区目录拼,
  // 否则会话开在别的工作区/外部项目里时,点相对路径链接一律按桌面根解析→查不到、报"无法查看"。
  const base = `&base=${encodeURIComponent(curDomainId())}`;
  // 绝对 URL:原生 WKWebView 加载不了相对地址(会空白);同源页带 origin 仍走 /workbench/view 内嵌查看
  openPage(`${location.origin}/workbench/view?path=${encodeURIComponent(fp)}${base}`, name, { glyph });
}
// 给一个 xterm 实例接上链接识别:OSC 8(引擎显式标的)+ 启发式扫描(纯文本 URL/路径)。
// 全屏 TUI 多半开了鼠标上报,普通点击会被引擎吃掉——这时用 ⌘ + 点击打开(xterm 有修饰键时不转发给 app);
// Option 留给强制选区(macOptionClickForcesSelection),故打开用 ⌘ 不用 Alt。
const _TERM_LINK_RE = /(https?:\/\/[^\s'"`()<>\][]+)|((?:~|\.{0,2})\/[\w@.+~/-]+(?::\d+(?::\d+)?)?)|(\b[\w.-]+\/[\w@.+/-]+\.[A-Za-z0-9]+(?::\d+(?::\d+)?)?)/g;
// 字符串下标 → 终端列号:translateToString 用字符下标,xterm 链接装饰用列号。全角中文一个字符占
// 2 列(宽字符的第二格 getWidth()===0、不产出字符),链接前有中文时列号被低估、下划线左移。
// 按 buffer 单元格累计字符,把目标字符下标映射回真实起始列。
function _cellColOfStrIndex(line, cols, target) {
  let strIdx = 0;
  for (let col = 0; col < cols; col++) {
    const cell = line.getCell(col);
    if (!cell) break;
    if (cell.getWidth() === 0) continue;        // 宽字符占位第二格,无对应字符
    if (strIdx >= target) return col;
    strIdx += cell.getChars().length || 1;      // 空 cell(空格)记 1
  }
  return target;
}
function wireTermLinks(term) {
  try {
    term.options.linkHandler = { activate: (ev, uri) => { try { ev.preventDefault(); } catch {} termOpenTarget(uri); } };
  } catch {}
  try {
    term.registerLinkProvider({
      provideLinks(y, cb) {
        const ln = term.buffer.active.getLine(y - 1);
        if (!ln) { cb(undefined); return; }
        const text = ln.translateToString(true);
        const links = [];
        _TERM_LINK_RE.lastIndex = 0;
        let m;
        while ((m = _TERM_LINK_RE.exec(text))) {
          const target = m[0];
          // 起点按真实列号(全角中文计 2 列);链接是 ASCII(每字符 1 列),终点=起点+长度。
          // 本 build 的链接装饰把 start.x/end.x 当 0-based 用(不减 1):官方 1-based 会整体右移一格、漏掉开头字符。
          const sx = _cellColOfStrIndex(ln, term.cols, m.index);
          links.push({
            range: { start: { x: sx, y }, end: { x: sx + m[0].length, y } },
            text: target,
            activate: (ev, t) => { try { ev.preventDefault(); } catch {} termOpenTarget(t); },
          });
        }
        cb(links.length ? links : undefined);
      },
    });
  } catch {}
  // 不再劫持右键:之前选中 URL 文本后右键会直接打开链接、吞掉原生"复制"菜单,导致超链接文字无法复制。
  // 打开链接走点击下划线(普通态直接点;鼠标上报的 TUI 用 ⌘+点击);选区/复制用 Option 拖拽(见 termOpts)。
}
// 把一个终端会话嵌进任意容器(用于 agentic 执行记录体);独立实例,不碰主区全局态
// xterm 在 open 时按当前字体量一次格宽,之后不再重量。Desk Mono 是按需下载的网页字体,
// 首次打开终端时往往还没到,量出来的是 Menlo 的格宽,换上真字体后列就错开。先等它,
// 最多 800ms:字体坏了也只是退回 Menlo,不能因此打不开终端。
function termFontsReady() {
  if (!document.fonts || !document.fonts.load) return Promise.resolve();
  const load = Promise.all([
    document.fonts.load('13.5px "Desk Mono"'),
    document.fonts.load('13.5px "Desk Mono CJK"', "中"),
  ]).catch(() => {});
  return Promise.race([load, new Promise((r) => setTimeout(r, 800))]);
}
async function embedTerminal(hostEl, sessionId, engine, opts) {
  // 首次加载 xterm 是异步的,期间同一个容器可能已经被改挂成别的会话,续体再往下走会把画面抢回旧的。
  // 判断按容器自己记的序号,不看主区的 activeTermId:本函数是独立实例(终端应用的标签、执行记录体),
  // 它挂的会话与主区当前看的那一段本就没有关系,拿全局比会让所有非主区的挂载一律落空。
  const token = (hostEl._embedToken = (hostEl._embedToken || 0) + 1);
  let ok = false;
  try { ok = await loadXterm(); } catch { ok = false; }
  if (hostEl._embedToken !== token) return null;
  if (!ok || !window.Terminal) { hostEl.innerHTML = '<div class="term-miss">缺 xterm.js 资产</div>'; return null; }
  await termFontsReady();
  if (hostEl._embedToken !== token) return null;
  hostEl.innerHTML = "";
  const enc = new TextEncoder();
  // onOutputLink 是嵌入层回调,不是 xterm option。登录向导用它识别 CLI 打出的授权地址；
  // 其余调用方不传时行为不变。
  const embedOpts = Object.assign({}, opts || {});
  const onOutputLink = typeof embedOpts.onOutputLink === "function" ? embedOpts.onOutputLink : null;
  const onSessionClose = typeof embedOpts.onSessionClose === "function" ? embedOpts.onSessionClose : null;
  delete embedOpts.onOutputLink;
  delete embedOpts.onSessionClose;
  const term = new Terminal(Object.assign(termOpts(engine), { theme: termTheme(hostEl) }, embedOpts));   // opts 可覆盖字号等
  const themeObserver = watchTermTheme(term, hostEl);
  const fit = new FitAddon.FitAddon(); term.loadAddon(fit);
  term.open(hostEl);
  wireTermLinks(term);
  // 颜色查询不回话。OSC 10/11/12(前景/背景/光标色)与 OSC 4(调色板)的问句,xterm 默认会答;
  // 答复是作为输入写回 PTY 的,而发问的程序往往问完就走(探一下深浅底色再决定配色)。
  // 它一走,答复就落在提示符上被回显成字面,还会被 shell 当成命令执行——
  // 「11;rgb:1919/1919/1919」加一句 command not found(2026-09-03 用户实见)。
  //
  // 真实终端也答,只是答得快(本机管道,亚毫秒),问的程序还在等,答复轮不到落进 shell。
  // 我们这一路要绕一圈浏览器:PTY→后端→WebSocket→xterm 生成答复→再原路回去。实测
  // 光是浏览器侧生成答复就 1.3ms,加上两跳网络,隧道上更长,发问方的超时窗常常先到。
  //
  // 所以不答。代价近乎为零:终端底色是写死的 #191919(见 termOpts),永远是深色,
  // 问与不问得到的都是同一个结论;拿不到答复的程序退回自己的深色默认,与实情一致。
  // 若哪天终端底色跟着主题变,这个取舍要重新算——那时答案才真的带信息。
  //
  // 光标位置查询(CSI 6n)不在此列:那是 TUI 用来量位置的,答案只有终端知道,
  // 不答会让它们排版错乱,比偶尔漏一段字面严重得多。
  const oscQuery = (d) => String(d || "").trim().endsWith("?");   // true=问句,吞掉不往下传
  try {
    for (const n of [4, 10, 11, 12]) term.parser.registerOscHandler(n, oscQuery);
  } catch {}
  const isCodexLike = engineIsCodexLike(engine);   
  // 重放期要主动滚到底的引擎:codex 系。它在普通缓冲区里出字、有回滚,
  // 刷新后重放完停在哪由 write 时序决定,不补这一下就会卡在会话中部。claude 走备用屏,没有回滚,不需要。
  const needsReplayBottom = isCodexLike;
  let replayUntil = 0, replayToken = 0, settleTimer = 0, disposed = false, promptRedrawDone = false;
  let userScrolled = false;   // 用户重放期内主动往上翻就停止自动回底,不跟他抢滚动条
  const keepReplayAtBottom = () => {
    if (!needsReplayBottom) return;
    const token = ++replayToken;
    for (const delay of [500, 1600, 4200, 8200]) {
      setTimeout(() => {
        if (disposed || userScrolled || token !== replayToken || ws._intentional) return;
        _termScrollBottom(term);
      }, delay);
    }
  };
  term.element?.addEventListener("wheel", (e) => { if (e.deltaY < 0) userScrolled = true; }, { passive: true });
  term.element?.addEventListener("keydown", (e) => {
    if (!isCodexLike || !e.metaKey || e.shiftKey || e.altKey || e.ctrlKey || e.key !== "ArrowDown") return;
    e.preventDefault(); e.stopImmediatePropagation();
    _termScrollBottom(term);
    term.focus();
  }, true);
  const doFit = () => { try { fitSafe(fit); } catch {} };
  doFit();   // embed 场景下容器已在 DOM 且可见,同步 fit 可拿到正确尺寸
  const ws = new WebSocket(termWsUrl(sessionId, engine, term));
  ws.binaryType = "arraybuffer";
  let sentGeom = null;
  const sendResize = (redraw = false) => {
    if (disposed || ws.readyState !== 1) return;
    const cols = term.cols, rows = term.rows;
    if (!cols || !rows) return;
    if (!redraw && sentGeom && sentGeom[0] === cols && sentGeom[1] === rows) return;
    sentGeom = [cols, rows];
    const payload = { resize: [cols, rows] };
    if (redraw && isCodexLike) payload.redraw = true;
    ws.send(JSON.stringify(payload));
  };
  const settleCodexTui = () => {
    if (!isCodexLike || disposed) return;
    clearTimeout(settleTimer);
    // ResizeObserver 的 fit 在 250ms 防抖后才收敛；再等一帧向 PTY 请求重画。
    // xterm.refresh 只能重画本地 buffer，补不回 Codex 当前帧的输入框/状态栏。
    settleTimer = setTimeout(() => {
      if (disposed) return;
      doFit();
      sendResize(true);
      _termScrollBottom(term);
    }, 320);
  };
  const redrawCodexPromptWhenReady = () => {
    if (!isCodexLike || disposed || promptRedrawDone) return;
    const buffer = term.buffer && term.buffer.active;
    const line = buffer && buffer.getLine((buffer.baseY || 0) + (buffer.cursorY || 0));
    const text = line && line.translateToString(true);
    if (!/^\s*[›>]/.test(text || "")) return;
    // 休眠会话恢复时 Codex 会先启动 MCP，输入区可能在首轮几何稳定之后才出现。
    // 等真实光标行进入 prompt 后只补一次重画，避免按固定秒数猜 MCP 启动时长。
    promptRedrawDone = true;
    clearTimeout(settleTimer);
    settleTimer = setTimeout(() => {
      if (disposed) return;
      doFit();
      sendResize(true);
      _termScrollBottom(term);
    }, 80);
  };
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => {
    if (disposed) return;
    doFit();
    sendResize();
    settleCodexTui();
  });
  ws.onopen = () => {
    sendResize();
    if (needsReplayBottom) {
      replayUntil = Date.now() + 9000;
      userScrolled = false;
      keepReplayAtBottom();
      _termScrollBottom(term);
    }
    settleCodexTui();   // 自带 isCodexLike 守卫,只对 codex 系的几何收敛生效
    term.focus();
  };
  const outputDecoder = onOutputLink ? new TextDecoder() : null;
  let outputTail = "", outputLinks = new Set();
  const inspectOutputLinks = (data) => {
    if (!onOutputLink || !outputDecoder) return;
    // PTY 可能把一个 URL 拆成多个 WS 帧；保留短尾巴拼接后扫描。终端的视觉换行不会
    // 写进原始字节，所以截图中跨行的长授权地址仍能完整识别。
    const chunk = outputDecoder.decode(data, { stream: true });
    const scan = (outputTail + chunk).replace(/\x1b\[[0-?]*[ -\/]*[@-~]/g, "");
    const re = /https?:\/\/[^\s'"`<>\[\]()]+/g;
    let match;
    while ((match = re.exec(scan))) {
      // 帧刚好断在 URL 中间时，末尾这一段还不能使用；等下一帧带来换行或后续文字再认。
      if (match.index + match[0].length === scan.length) continue;
      const url = match[0].replace(/[.,;:!?]+$/, "");
      if (!outputLinks.has(url)) {
        outputLinks.add(url);
        try { onOutputLink(url); } catch {}
      }
    }
    outputTail = scan.slice(-4096);
    if (outputLinks.size > 32) outputLinks = new Set(Array.from(outputLinks).slice(-16));
  };
  ws.onmessage = (e) => {
    const data = new Uint8Array(e.data);
    inspectOutputLinks(data);
    term.write(data, () => {
    if (needsReplayBottom && !userScrolled && Date.now() < replayUntil) _termScrollBottom(term);
    redrawCodexPromptWhenReady();
    });
  };
  ws.onclose = (event) => {
    if (ws._intentional) return;
    try { term.write("\r\n\x1b[90m[" + (event.reason || "终端连接已断开") + "]\x1b[0m\r\n"); } catch {}
    if (onSessionClose) { try { onSessionClose(event); } catch {} }
  };
  wireTermInput(term, ws, enc);
  const ro = window.ResizeObserver ? new ResizeObserver(debouncedFit(fit, () => {
    sendResize();
    settleCodexTui();
  })) : null;
  if (ro) ro.observe(hostEl);
  // 显式重新 fit:容器从隐藏切回显示时用(终端应用切标签、应用页收起再打开)。
  // ResizeObserver 那条路要等 250ms 防抖,切标签当场就该是对的尺寸,不等它。
  // 容器还没有尺寸(仍隐藏)就不动:按 0 算几何会把 PTY 的 winsize 打坏。
  const refit = (redraw = false) => {
    if (disposed || !hostEl.clientWidth || !hostEl.clientHeight) return;
    doFit();
    sendResize(redraw);
    settleCodexTui();
  };
  return { term, ws, refit, dispose: () => {
    disposed = true;
    replayToken++;
    clearTimeout(settleTimer);
    ws._intentional = true;
    try { ws.close(); } catch {}
    try { if (ro) ro.disconnect(); } catch {}
    try { themeObserver.disconnect(); } catch {}
    try { term.dispose(); } catch {}
  } };
}
