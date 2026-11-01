// 共享终端嵌入模块:把一个引擎会话挂进任意容器(iframe 应用页 / 主 app 执行记录体)。
// 单一来源——逻辑搬自 app-assets.js 的 embedTerminal 及其辅助函数,链接处理参数化,
// 不依赖主 app 全局(openPage/showView 等)。工作区应用页(meeting-digest 等)经 <script src="/term-embed.js"> 复用,
// 避免各页手抄终端代码后与 vibing 分叉(IME 掉字、复制带尾空格、fit 时序、codex 几何等修复跟不上)。
//
// 用法:
//   const t = await desktronTerm.embed(hostEl, sessionId, {
//     engine: 'claude',                       // claude/codex;影响 ws 初始 cols/rows
//     onLink: (url, label) => {...},           // 终端里的链接/路径被点击;url 已规整(文件路径→/workbench/view)
//     closedBanner: '[会话结束]',              // 非主动关闭时在终端尾部打印的提示
//   });
//   t.send('文本');   // 键入并回车(ws 未就绪则排队,连上后补发)
//   t.dispose();      // 主动关闭(不打印 closedBanner)
(function () {
  "use strict";

  // codex 家族:要初始几何、缓存重现时要抖一行重画。
  // 这里另存一份而不是引主 app 的 engineIsCodexLike:本模块刻意不依赖主 app 全局(见文件头),
  // 工作区应用页是单独一张 iframe,那些全局在那边不存在。
  const CODEX_LIKE = ["codex"];

  // 字体与主 app style.css 顶部两条 @font-face 同源。应用页 iframe 没有那份样式表,
  // 这里按需补注册;主 app 里已有同名字体则不重复。xterm 只在 open 时量一次格宽,
  // 所以要等字体到位再建终端,最多 800ms,取不到就退回 Menlo。
  let _fonts = null;
  function ensureTermFonts() {
    if (_fonts) return _fonts;
    const fonts = document.fonts;
    if (!fonts || !fonts.load || typeof FontFace !== "function") return (_fonts = Promise.resolve());
    const has = (name) => { for (const f of fonts) if (f.family.replace(/"/g, "") === name) return true; return false; };
    try {
      if (!has("Desk Mono"))
        fonts.add(new FontFace("Desk Mono", 'url("/vendor/fonts/JetBrainsMono-Regular.woff2") format("woff2")',
                               { weight: "400", style: "normal", display: "swap" }));
      if (!has("Desk Mono CJK"))
        fonts.add(new FontFace("Desk Mono CJK", 'local("PingFang SC"), local("PingFangSC-Regular"), local("Microsoft YaHei UI"), local("MicrosoftYaHeiUI")',
                               { sizeAdjust: "120%", unicodeRange: "U+2E80-303F, U+3040-9FFF, U+AC00-D7AF, U+F900-FAFF, U+FF00-FFEF" }));
    } catch {}
    const load = Promise.all([
      fonts.load('13.5px "Desk Mono"'),
      fonts.load('13.5px "Desk Mono CJK"', "中"),
    ]).catch(() => {});
    return (_fonts = Promise.race([load, new Promise((r) => setTimeout(r, 800))]));
  }

  let _loaded = null;
  function loadXterm() {
    if (window.Terminal && window.FitAddon) return Promise.resolve(true);
    if (_loaded) return _loaded;
    _loaded = new Promise((resolve, reject) => {
      const css = document.createElement("link");
      css.rel = "stylesheet"; css.href = "/vendor/xterm/xterm.css";
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
    return _loaded;
  }

  const TERM_SAFE_ROWS = 1;   // 全屏/高 DPI 下 rows 可能多算一行;保守少报 1 行,防底部输入框被裁
  const TERM_THEMES = {
    dark: {
      // 与 app-assets-term.js 同一份配色(两份 xterm 初始化须同步改)。
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
  function termTheme() { return { ...TERM_THEMES[termThemeId()] }; }
  function watchTermTheme(term) {
    let current = termThemeId();
    const observer = new MutationObserver(() => {
      const next = termThemeId();
      if (next === current) return;
      current = next;
      term.options.theme = termTheme();
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return observer;
  }
  function termOpts(engine) {
    const o = {
      // 与主 app 的 --mono-code 同一套;本模块也跑在不带 style.css 的应用页 iframe 里,
      // 字体由 ensureTermFonts() 自行注册。
      fontFamily: '"Desk Mono", "Desk Mono CJK", Menlo, monospace',
      // 与终端应用共用 iTerm 式字面：不挤字，靠适度行距区分命令、输出与提示符。
      fontSize: 13.5, lineHeight: 1.0, letterSpacing: 0,   // 与 app-assets-term.js 同值，理由见该处
      fontWeight: "normal", fontWeightBold: "bold",
      // 创建时读取当前外观；已打开的终端由 watchTermTheme 跟随切换，不重建 PTY。
      theme: termTheme(), cursorBlink: true,
      scrollback: 5000,
      rightClickSelectsWord: true,
      allowProposedApi: true,   // 含 bracketed paste:引擎启用后粘贴多行文本不会被回车截断
    };
    // windowsMode 关掉 reflow:resize 时文本不软折行,保持绝对光标定位与 buffer 行一一对应。
    // 三引擎都有绝对定位绘制的 TUI 区域,reflow 会把内联 TUI 按软折行重排→坐标漂移→底部下坠出视口。
    o.windowsMode = true;
    // 鼠标上报的 TUI 会吞掉拖拽选区;Option(mac)按住强制选区,使链接/任意文本可选中、复制。
    o.macOptionClickForcesSelection = true;
    return o;
  }

  function fitSafe(fit, reserveRows) {
    if (reserveRows == null) reserveRows = TERM_SAFE_ROWS;
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

  // 拖窗时 ResizeObserver 连发几十次,逐次 fit+winsize 让 TUI 每帧 SIGWINCH 互相打断(错位)。
  // 收敛成停手后一次干净的 fit+resize。
  function debouncedFit(fit, sendResize, ms) {
    if (ms == null) ms = 250;
    let t = 0;
    return () => { clearTimeout(t); t = setTimeout(() => { try { fitSafe(fit); sendResize(); } catch {} }, ms); };
  }

  function scrollTermBottom(term) {
    if (!term) return;
    const go = () => { try { term.scrollToBottom(); } catch {} };
    go();
    requestAnimationFrame(go);
    setTimeout(go, 80);
    setTimeout(go, 260);
  }

  function termWsUrl(sessionId, engine, term, contextDomain) {
    // 协议跟随页面(同 app-assets.js):https 直访要 wss
    let url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/term?session=${encodeURIComponent(sessionId)}`;
    if (contextDomain) url += `&context_domain=${encodeURIComponent(contextDomain)}`;
    // codex 家族的 TUI 按首帧几何布版,连线时就把尺寸带上,免得开屏错位
    if (CODEX_LIKE.includes(engine) && term && term.cols > 0 && term.rows > 0)
      url += `&cols=${encodeURIComponent(term.cols)}&rows=${encodeURIComponent(term.rows)}`;
    return url;
  }

  // 终端键入。修中文输入法掉字/重复:某些输入法(尤其标点)keydown 报 keyCode 229 却不发
  // compositionstart/end,直接用 input 事件上屏。在 term.element 上捕获,先于 xterm 截断坏路径,
  // 229 的 keydown 与对应 input 都 stopPropagation,自己发一次。正常英文/正常组词放行 xterm。
  function wireTermInput(term, ws, enc) {
    const send = (s) => { if (s && ws.readyState === 1) ws.send(enc.encode(s)); };
    const root = term.element;
    let lastKc = 0;
    if (root) {
      // 复制去掉行尾空白:TUI 为铺背景把整行用空格填满,选区会带上这些填充空格。
      root.addEventListener("copy", (e) => {
        const sel = term.getSelection();
        if (!sel) return;
        e.preventDefault(); e.stopPropagation();
        try { e.clipboardData.setData("text/plain", sel.replace(/[ \t]+$/gm, "")); } catch {}
      }, true);
      root.addEventListener("keydown", (e) => {
        lastKc = e.keyCode;
        if (e.keyCode === 229 && !e.isComposing) e.stopPropagation();
      }, true);
      root.addEventListener("input", (e) => {
        if (!e.isComposing && lastKc === 229 && e.inputType === "insertText" && e.data) {
          send(e.data);
          try { term.textarea.value = ""; } catch {}
          e.stopPropagation();
        }
      }, true);
    }
    term.onData((d) => send(d));
  }

  // 字符串下标 → 终端列号:全角中文一个字符占 2 列(宽字符第二格 getWidth()===0、不产出字符),
  // 链接前有中文时列号被低估、下划线左移。按单元格累计字符,把目标字符下标映射回真实起始列。
  const _LINK_RE = /(https?:\/\/[^\s'"`()<>\][]+)|((?:~|\.{0,2})\/[\w@.+~/-]+(?::\d+(?::\d+)?)?)|(\b[\w.-]+\/[\w@.+/-]+\.[A-Za-z0-9]+(?::\d+(?::\d+)?)?)/g;
  function _cellColOfStrIndex(line, cols, target) {
    let strIdx = 0;
    for (let col = 0; col < cols; col++) {
      const cell = line.getCell(col);
      if (!cell) break;
      if (cell.getWidth() === 0) continue;
      if (strIdx >= target) return col;
      strIdx += cell.getChars().length || 1;
    }
    return target;
  }
  // 终端里的链接路由:规整后交给调用方 onLink(url, label)。
  // 网页 URL 原样;文件/目录路径 → /workbench/view 内嵌查看的绝对 URL。
  function openTarget(raw, onLink) {
    let t = String(raw || "").trim().replace(/^['"<(]+/, "").replace(/['">).,;:]+$/, "");
    if (!t || typeof onLink !== "function") return;
    if (/^https?:\/\//i.test(t)) {
      let label = t; try { label = new URL(t).hostname; } catch {}
      onLink(t, label);
      return;
    }
    const fp = t.replace(/:\d+(:\d+)?$/, "");   // 剥掉 path:line[:col] 后缀
    const label = (fp.replace(/\/+$/, "").split("/").pop()) || fp;
    onLink(`${location.origin}/workbench/view?path=${encodeURIComponent(fp)}`, label);
  }
  function wireTermLinks(term, onLink) {
    try {
      term.options.linkHandler = { activate: (ev, uri) => { try { ev.preventDefault(); } catch {} openTarget(uri, onLink); } };
    } catch {}
    try {
      term.registerLinkProvider({
        provideLinks(y, cb) {
          const ln = term.buffer.active.getLine(y - 1);
          if (!ln) { cb(undefined); return; }
          const text = ln.translateToString(true);
          const links = [];
          _LINK_RE.lastIndex = 0;
          let m;
          while ((m = _LINK_RE.exec(text))) {
            const target = m[0];
            const sx = _cellColOfStrIndex(ln, term.cols, m.index);
            links.push({
              range: { start: { x: sx, y }, end: { x: sx + m[0].length, y } },
              text: target,
              activate: (ev, t) => { try { ev.preventDefault(); } catch {} openTarget(t, onLink); },
            });
          }
          cb(links.length ? links : undefined);
        },
      });
    } catch {}
  }

  async function embed(hostEl, sessionId, opts) {
    opts = opts || {};
    const engine = opts.engine || "claude";
    const closedBanner = opts.closedBanner || "[会话结束]";
    let ok = false;
    try { ok = await loadXterm(); } catch { ok = false; }
    if (!ok || !window.Terminal) {
      hostEl.innerHTML = '<div style="color:#666;padding:20px">缺 xterm.js 资产</div>';
      return null;
    }
    await ensureTermFonts();
    hostEl.innerHTML = "";
    const enc = new TextEncoder();
    const term = new Terminal(termOpts(engine));
    const themeObserver = watchTermTheme(term);
    const fit = new FitAddon.FitAddon(); term.loadAddon(fit);
    term.open(hostEl);
    // 颜色问句(OSC 4/10/11/12)不回话,理由与应用终端那份相同:答复会落到提示符上变成字面。
    // 两份 xterm 初始化是各自独立的副本,改一处必须同时改另一处(见 app-assets-term.js)。
    try {
      const oscQuery = (d) => String(d || "").trim().endsWith("?");
      for (const n of [4, 10, 11, 12]) term.parser.registerOscHandler(n, oscQuery);
    } catch {}
    wireTermLinks(term, opts.onLink);
    const isCodexLike = CODEX_LIKE.includes(engine);
    let settleTimer = 0, disposed = false, promptRedrawDone = false;
    const doFit = () => { try { fitSafe(fit); } catch {} };
    doFit();   // 容器已在 DOM 且可见,同步 fit 可拿到正确尺寸
    let pending = null;   // ws 未就绪时排队的待发文本(含回车)
    const ws = new WebSocket(termWsUrl(sessionId, engine, term, opts.domain));
    ws.binaryType = "arraybuffer";
    let _sentGeom = null;   // 去重:尺寸没变不再发 resize,免得流中途无谓改 winsize → SIGWINCH 打乱在途字节(spinner 行错位、要刷新才恢复)
    const sendResize = (redraw = false) => {
      if (disposed || ws.readyState !== 1) return;
      const c = term.cols, r = term.rows;
      if (!redraw && _sentGeom && _sentGeom[0] === c && _sentGeom[1] === r) return;
      _sentGeom = [c, r];
      const payload = { resize: [c, r] };
      if (redraw && isCodexLike) payload.redraw = true;
      ws.send(JSON.stringify(payload));
    };
    const settleCodexTui = () => {
      if (!isCodexLike || disposed) return;
      clearTimeout(settleTimer);
      settleTimer = setTimeout(() => {
        if (disposed) return;
        doFit();
        sendResize(true);
        scrollTermBottom(term);
      }, 320);
    };
    const redrawCodexPromptWhenReady = () => {
      if (!isCodexLike || disposed || promptRedrawDone) return;
      const buffer = term.buffer && term.buffer.active;
      const line = buffer && buffer.getLine((buffer.baseY || 0) + (buffer.cursorY || 0));
      const text = line && line.translateToString(true);
      if (!/^\s*[›>]/.test(text || "")) return;
      promptRedrawDone = true;
      clearTimeout(settleTimer);
      settleTimer = setTimeout(() => {
        if (disposed) return;
        doFit();
        sendResize(true);
        scrollTermBottom(term);
      }, 80);
    };
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => {
      if (disposed) return;
      doFit();
      sendResize();
      settleCodexTui();
    });
    let autoBottomUntil = 0, userScrolled = false;
    // 有回滚的引擎(codex 系)重放可能超过 1.6s,窗口给到 9s;用户主动上翻则让位
    const replayWindow = isCodexLike ? 9000 : 1600;
    term.element?.addEventListener("wheel", (e) => { if (e.deltaY < 0) userScrolled = true; }, { passive: true });
    ws.onopen = () => {
      autoBottomUntil = Date.now() + replayWindow;
      userScrolled = false;
      sendResize(); scrollTermBottom(term); term.focus();
      settleCodexTui();
      if (pending != null) {
        const p = pending; pending = null;
        setTimeout(() => { if (ws.readyState === 1) ws.send(enc.encode(p)); }, 300);
      }
    };
    ws.onmessage = (e) => {
      term.write(new Uint8Array(e.data), () => {
        if (!userScrolled && Date.now() < autoBottomUntil) scrollTermBottom(term);
        redrawCodexPromptWhenReady();
      });
    };
    ws.onclose = () => { if (ws._intentional) return; try { term.write(`\r\n\x1b[90m${closedBanner}\x1b[0m\r\n`); } catch {} };
    wireTermInput(term, ws, enc);
    const ro = window.ResizeObserver ? new ResizeObserver(debouncedFit(fit, () => {
      sendResize();
      settleCodexTui();
    })) : null;
    if (ro) ro.observe(hostEl);
    return {
      term, ws,
      send(text) {
        const s = text + "\r";
        if (ws.readyState === 1) ws.send(enc.encode(s));
        else pending = s;
      },
      dispose() {
        disposed = true;
        clearTimeout(settleTimer);
        ws._intentional = true;
        try { ws.close(); } catch {}
        try { if (ro) ro.disconnect(); } catch {}
        try { themeObserver.disconnect(); } catch {}
        try { term.dispose(); } catch {}
      },
    };
  }

  window.desktronTerm = { embed, loadXterm };
})();
