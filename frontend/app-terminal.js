// desktron 前端:终端应用(界面模型改造第二步)
// shell 不再各占一张全局标签:它们进这个应用,由它自己带一排标签,一个标签一个 shell。
// 对外仍然是 openShellTerminal(opts) 那一个入口——应用面板、程序坞、会话列表里的临时终端
// 都照旧走它,内部转成「往终端应用里开一个标签」。
//
// 与浏览器应用(app-browser.js)同构,差别只在这几处,都源于终端是本地进程而网页不是:
//   1. 标签背后是一个后端 PTY,关标签要 DELETE /workbench/terminals/<id>,否则进程留在后端;
//   2. 内容是 xterm,对容器尺寸敏感,切标签之后要重新 fit;
//   3. 标签的标题是当前目录,由 /workbench/term/cwd 轮询;
//   4. pane 有保活上限(与主区会话同一档),超出按最近最少用回收,后端仍活着,切回来重连重放。
const TERMINAL_PAGE_KEY = "terminal";              // pages[].url 里的页 key
const TERMINAL_APP_UID = "desktron.shell.0001";   // 与应用注册表里那条一致,程序坞按它认运行态
let termAppTabs = [];        // {id(=后端会话 id), title, host, embed, cwdTimer}
let termAppActiveId = "";
let _termAppLRU = [];        // 已挂载标签的访问顺序(末尾=最近用)
let _termAppRestoring = false;
let _termAppLegacy = [];     // 旧版本存在 cd_pages 里的 term 条目,迁移用
const TERM_APP_PANE_CAP = 16;   // 同时保活的终端标签上限,超出按最近最少用回收

const _TM_ICON_PLUS = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';
// 与应用列表、程序坞共用原创正方形终端图标。_appTilePlain 与 TERMINAL_ART 定义在 /app-assets.js
// 合包，加载顺序在本文件之后；顶层直接调用会在加载时报错，并让本文件其后的常量全部无法初始化。
// 改为首次使用时生成并缓存。
let _tmIconTab = "";
function _tmIconTabHtml() {
  return _tmIconTab || (_tmIconTab = _appTilePlain("shell", TERMINAL_ART));
}

function terminalPage() { return pages.find((x) => x.url === TERMINAL_PAGE_KEY) || null; }
function termAppActiveTab() { return termAppTabs.find((t) => t.id === termAppActiveId) || null; }
function terminalViewEl() { const p = terminalPage(); return p && p.el ? p.el.querySelector(".tm-view") : null; }
// 应用注册表里那条终端的 uid:程序坞按 appid 认运行态,取不到就用内置默认值
function terminalAppUid() {
  let uid = TERMINAL_APP_UID;
  const desk = (typeof desktopApps !== "undefined" ? desktopApps : []) || [];
  const doms = (typeof domains !== "undefined" ? domains : []) || [];
  for (const d of doms) for (const a of d.apps || []) if (a.page === "shell") uid = a.uid || a.id || uid;
  for (const a of desk) if (a.page === "shell") uid = a.uid || a.id || uid;
  return uid;
}

// ---- 应用页宿主 ----
function terminalEnsurePage(show = true) {
  let p = terminalPage();
  if (!p) {
    // 先建后显:showOverlay 要读 p.terminal 才知道该挂载当前标签并重新 fit
    openContentPage(TERMINAL_PAGE_KEY, "终端", _tmIconTabHtml(), _terminalBuild, false, { app: true, appid: terminalAppUid() });
    p = terminalPage();
    if (p) p.terminal = true;
  }
  if (show && p) showOverlay(p);
  return p;
}
// 程序坞/应用页点「终端」:已经开着就切回去,没开过才起一个新的 shell。
// 想再要一个 shell 走标签条上的「+」,与浏览器新建标签同一处。
function openTerminalApp(opts = {}) {
  // 这里跑的是普通 shell,不是引擎 TUI。原来共用 termUsable 那道闸把手机整个拦掉,
  // 理由是「TUI 按 80 列排版,竖屏只剩四十列」——那句话对 claude/codex 的 TUI 成立,
  // 对 ls / git status / tail -f 不成立。闸拦错了对象(2026-08-17 用户实见:手机上点终端
  // 只弹一句用不了)。引擎 TUI 那一路仍然挡着,判据留在 app-chat.js 的 termUsable。
  const show = opts.show !== false;
  const p = terminalPage();
  if (p && termAppTabs.length) {
    termAppActivateTab(termAppActiveId || termAppTabs[0].id, show);
    return p;
  }
  return openShellTerminal({ ...opts, show });
}
function _terminalBuild(host) {
  host.classList.add("terminal-host");
  // iTerm 式两层壳:全局页头只承担窗口标题与 Desk 的收起/关闭动作；会话标签另起一条
  // 紧凑标签栏并与终端正文相连。此前把浏览器式标签直接塞进页头，两套语义挤在一行。
  const head = document.createElement("span");
  head.className = "tm-head";
  head.innerHTML = '<span class="tm-window-title">终端</span>';
  host._headNode = head;
  host.innerHTML = '<div class="terminal-app"><div class="tm-tabbar"><span class="tm-tabs"></span></div><div class="tm-view"></div></div>';
  _termAppKeys(host.querySelector(".terminal-app"));
  termAppRenderTabs();
}

// 手机上那几个键盘没有的键。软键盘只给字母数字与常见标点,而 shell 离不开 Esc、Tab 补全、
// Ctrl+C 中断、上下翻历史——没有它们,终端在手机上打得出字也用不利索。
// 一直建在 DOM 里,由 CSS 的断点决定显不显示(见 .tm-keys),不按 UA 判设备。
const _TM_KEYS = [
  { k: "Paste", paste: true },      // 手机上没有 Cmd+V,长按也唤不出系统菜单(见 termAppPaste)
  { k: "Esc", seq: "\x1b" },
  { k: "Tab", seq: "\t" },
  { k: "Ctrl", ctrl: true },        // 粘滞:按下后下一个字母发对应的控制字符
  { k: "↑", seq: "\x1b[A" },
  { k: "↓", seq: "\x1b[B" },
  { k: "←", seq: "\x1b[D" },
  { k: "→", seq: "\x1b[C" },
  { k: "^C", seq: "\x03" },        // 最常用的一个单独给一枚,不必先按 Ctrl
  { k: "|", seq: "|" },
  { k: "~", seq: "~" },
  { k: "/", seq: "/" },
];
let _tmCtrlArmed = false;

function _termAppKeys(root) {
  if (!root) return;
  const bar = document.createElement("div");
  bar.className = "tm-keys";
  for (const spec of _TM_KEYS) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tm-key" + (spec.ctrl ? " tm-key-mod" : "");
    b.textContent = spec.k;
    // 用 pointerdown 不用 click:click 会先让 xterm 失焦,软键盘跟着收起来,
    // 每按一次特殊键都要重新点一下终端才能接着打字。
    b.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();          // 不抢焦点,软键盘留在原处
      if (spec.ctrl) {
        _tmCtrlArmed = !_tmCtrlArmed;
        b.classList.toggle("on", _tmCtrlArmed);
        return;
      }
      if (spec.paste) { termAppPaste(); return; }
      termAppSend(spec.seq);
    });
    bar.appendChild(b);
  }
  root.appendChild(bar);
}

// 粘滞 Ctrl 落在这里:手机上按 Ctrl 只是「下一个键算控制字符」,真正的转换要在按键流里做。
// attachCustomKeyEventHandler 返回 false 表示这一下由我们接管,xterm 不再自己处理。
function _termAppWireCtrl(h) {
  if (!h || !h.term || !h.term.attachCustomKeyEventHandler) return;
  h.term.attachCustomKeyEventHandler((ev) => {
    if (!_tmCtrlArmed || ev.type !== "keydown") return true;
    const c = (ev.key || "").toLowerCase();
    if (c.length !== 1 || c < "a" || c > "z") return true;
    _tmCtrlArmed = false;
    document.querySelectorAll(".tm-key-mod.on").forEach((b) => b.classList.remove("on"));
    termAppSend(String.fromCharCode(c.charCodeAt(0) - 96));   // a→\x01 … z→\x1a
    return false;
  });
}

// 粘贴。手机上没有 Cmd+V,长按也不行——xterm 真正接输入的是一个隐藏的 textarea,
// 手指长按落在画布上,系统不会给出「粘贴」那一项。所以只能由这枚键去读剪贴板。
// 读剪贴板要安全上下文(https 或 localhost)与一次用户手势;iOS 上还会再弹一次系统确认,
// 那是它的规矩,拦不掉也不该拦。读不到就说清为什么,不要静默失败。
async function termAppPaste() {
  let txt = "";
  try {
    if (navigator.clipboard && navigator.clipboard.readText && window.isSecureContext) {
      txt = await navigator.clipboard.readText();
    }
  } catch { txt = ""; }
  if (!txt) {
    // http 直连(隧道、局域网 IP)不是安全上下文,读剪贴板这条路整个不通;安全上下文下
    // 用户也可能拒绝授权。退回一个真实的输入框:系统只对可编辑区域给「粘贴」菜单,
    // 而 xterm 那个接输入的 textarea 是隐藏的,长按落不进去(2026-08-17 用户实见)。
    return _termAppPasteBox();
  }
  // 多行按 bracketed paste 送:引擎与 shell 都认这对标记,中间的换行不会被当成回车逐条执行。
  // xterm 开了 allowProposedApi,term.paste 会自己判断要不要包起来,比手写标记稳。
  const t = termAppActiveTab();
  if (t && t.embed && t.embed.term && t.embed.term.paste) {
    try { t.embed.term.paste(txt); return true; } catch {}
  }
  return termAppSend(txt);
}

// 退路:摆一个可编辑框,让系统自己的长按粘贴把内容送进来。
// 贴进来即发即关,不必再点一次确认——多一步在手机上就是多一次误触。
function _termAppPasteBox() {
  const t = termAppActiveTab();
  if (!t || !t.host) return false;
  if (t.host.querySelector(".tm-paste")) return true;   // 已经开着,不叠第二个
  const box = document.createElement("div");
  box.className = "tm-paste";
  box.innerHTML = '<div class="tm-paste-tip">长按下面的方框,选「粘贴」</div>'
                + '<textarea class="tm-paste-ta" rows="3" autocapitalize="off" '
                + 'autocomplete="off" autocorrect="off" spellcheck="false"></textarea>'
                + '<div class="tm-paste-acts"><button type="button" class="tm-paste-x">取消</button>'
                + '<button type="button" class="tm-paste-ok">发送</button></div>';
  const ta = box.querySelector(".tm-paste-ta");
  const close = () => { try { box.remove(); } catch {} };
  const commit = () => {
    const v = ta.value;
    close();
    if (!v) return;
    const h = t.embed;
    if (h && h.term && h.term.paste) { try { h.term.paste(v); return; } catch {} }
    termAppSend(v);
  };
  // 贴进来就走。用 setTimeout 让浏览器先把内容写进 value——paste 事件触发时它还是空的。
  ta.addEventListener("paste", () => setTimeout(commit, 0));
  box.querySelector(".tm-paste-ok").onclick = commit;
  box.querySelector(".tm-paste-x").onclick = close;
  t.host.appendChild(box);
  ta.focus();
  return true;
}

// 往当前标签的终端发一串字节。走 ws 而不是 term.input:后者只是本地回显,不进 PTY。
function termAppSend(seq) {
  const t = termAppActiveTab();
  const ws = t && t.embed && t.embed.ws;
  if (!ws || ws.readyState !== 1 || !seq) return false;
  try { ws.send(new TextEncoder().encode(seq)); } catch { return false; }
  return true;
}

// ---- pane 的挂载与回收 ----
// 保活:切标签只把 pane 藏起来,xterm 与 WS 都不销毁,切回来无需重放。
// 上限本来沿用主区会话那一档(_TERM_PANE_CAP)。主区那套 pane 随 TUI 退场撤掉了(2026-08-20),
// 常量搬到这里自己拿着:每个 pane 一个 xterm(回滚 5000,满时约 15MB)
// 加一条 WS,超出按最近最少用回收——被回收的标签后端 PTY 仍活着,切回来重连一次由后端重放。
function _termAppTouch(id) {
  const i = _termAppLRU.indexOf(id);
  if (i >= 0) _termAppLRU.splice(i, 1);
  _termAppLRU.push(id);
}
function _termAppMount(tab) {
  if (!tab || tab.embed || tab._mounting) return;
  tab._mounting = true;
  Promise.resolve(embedTerminal(tab.host, tab.id, "shell")).then((h) => {
    tab._mounting = false;
    if (!termAppTabs.includes(tab)) { try { if (h) h.dispose(); } catch {} return; }   // 挂载途中被关掉
    tab.embed = h;
    _termAppWireCtrl(h);
    _termAppWireTitle(tab, h);
    // 长按整行复制。这套手势本来只绑在主区终端的 pane 上(app-assets.js 的 wireTermTouchSelect),
    // 终端应用是后做的,一直没接——手机上既复制不了也粘贴不了。它只用 host 与 term 两样,
    // 拿标签自己的凑一个同形的传进去即可。
    if (typeof wireTermTouchSelect === "function" && h && h.term) {
      try { wireTermTouchSelect({ host: tab.host, term: h.term }); } catch {}
    }
    if (tab.id === termAppActiveId) termAppRefit();
  }).catch(() => { tab._mounting = false; });
}
function _termAppUnmount(tab) {
  if (!tab) return;
  if (tab.embed) { try { tab.embed.dispose(); } catch {} tab.embed = null; }
  if (tab.host) tab.host.innerHTML = "";
  const i = _termAppLRU.indexOf(tab.id);
  if (i >= 0) _termAppLRU.splice(i, 1);
}
function _termAppEvict() {
  const cap = TERM_APP_PANE_CAP;
  for (;;) {
    const mounted = termAppTabs.filter((t) => t.embed || t._mounting);
    if (mounted.length <= cap) return;
    const victimId = _termAppLRU.find((id) => id !== termAppActiveId
      && termAppTabs.some((t) => t.id === id && t.embed));
    if (!victimId) return;   // 仅剩当前 pane:不回收
    _termAppUnmount(termAppTabs.find((t) => t.id === victimId));
  }
}
// 切标签、开关侧栏、收起再打开之后重新 fit。终端对尺寸敏感:pane 隐藏期间量不出容器尺寸,
// xterm 保持原几何不动(FitAddon 在 display:none 上算出 NaN 会自行放弃),显示出来才重算一次。
// embedTerminal 内部的 ResizeObserver 已经把连发的尺寸变化合并成一次,这里只补「刚显示出来」那一下。
function termAppRefit() {
  const t = termAppActiveTab();
  if (!t || !t.embed || !t.embed.refit) return;
  requestAnimationFrame(() => {
    const cur = termAppActiveTab();
    if (!cur || cur !== t || !t.embed || !t.embed.refit) return;
    try { t.embed.refit(); } catch {}
  });
}
// 应用被显示出来(showOverlay 末尾调):当前标签这时才真正需要一条 WS
function termAppShown() {
  const t = termAppActiveTab();
  if (!t) return;
  _termAppTouch(t.id);
  _termAppMount(t);
  _termAppEvict();
  termAppRefit();
}

// ---- 标签 ----
function _termAppAddTab(id, title, show = true) {
  const p = terminalEnsurePage(false);
  if (!p) return null;
  let t = termAppTabs.find((x) => x.id === id);
  if (!t) {
    const host = document.createElement("div");
    host.className = "tm-pane";
    host.style.display = "none";
    const v = terminalViewEl();
    if (v) v.appendChild(host);
    t = { id, title: title || "终端", host, embed: null, cwdTimer: 0 };
    termAppTabs.push(t);
    termAppCwdPoll(t);
  } else if (title) t.title = title;
  if (show) termAppActivateTab(t.id);
  else {
    if (!termAppActiveId) termAppActiveId = t.id;
    termAppRenderTabs();
    persistTerminalTabs();
  }
  return t;
}
// 新开一个 shell:后端起 PTY,前端添一张标签
async function openShellTerminal(opts = {}) {
  let r;
  try {
    r = await api("/workbench/shell", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ init: opts.init || undefined, cwd: opts.cwd || undefined }),
    });
  } catch { return null; }
  if (!r || !r.id) return null;
  return _termAppAddTab(r.id, opts.title || "终端", opts.show !== false);
}
// 已经存在的 shell 会话(会话列表里点一行、刷新后恢复):只添标签,不再起 PTY
function openShellTab(sessionId, title) {
  if (!sessionId) return null;
  return _termAppAddTab(sessionId, title || "终端", true);
}
function termAppActivateTab(id, show = true) {
  const t = termAppTabs.find((x) => x.id === id);
  if (!t) return;
  termAppActiveId = id;
  termAppTabs.forEach((x) => { if (x.host) x.host.style.display = x === t ? "" : "none"; });
  termAppRenderTabs();
  persistTerminalTabs();
  const p = terminalPage();
  const inSide = typeof appPageInSide === "function" && appPageInSide(p);
  // showOverlay 末尾会挂载并重新 fit;应用已经在前台时不必重走一遍显示流程
  if (show && !inSide && p && (!overlayOpen || pActive !== p)) showOverlay(p);
  else termAppShown();
}
// kill=true:关标签就是结束这个 shell,后端 PTY 一并收掉。
// kill=false:会话已经在别处被结束(会话列表里关的),这里只把标签摘掉,不重复发删除。
function termAppCloseTab(id, kill = true) {
  const i = termAppTabs.findIndex((x) => x.id === id);
  if (i < 0) return;
  const t = termAppTabs[i];
  if (t.cwdTimer) clearInterval(t.cwdTimer);
  _termAppUnmount(t);
  if (t.host) t.host.remove();
  termAppTabs.splice(i, 1);
  // 先摘标签再删后端:删除是异步的,回调回来时这张标签已经不在,不会与新标签抢位置
  if (kill) { try { api(`/workbench/terminals/${encodeURIComponent(t.id)}`, { method: "DELETE" }); } catch {} }
  if (!termAppTabs.length) {   // 最后一张关掉=这个应用没有页面了,按应用语义整个关掉(程序坞运行点熄灭)
    termAppActiveId = "";
    persistTerminalTabs();
    const p = terminalPage();
    if (p) closePage(p);
    return;
  }
  if (termAppActiveId === t.id) termAppActivateTab((termAppTabs[i] || termAppTabs[i - 1]).id);
  else { termAppRenderTabs(); persistTerminalTabs(); }
}
function termAppCloseActiveTab() {
  const t = termAppActiveTab();
  if (!t) return false;
  termAppCloseTab(t.id);
  return true;
}

// ——— 终端应用的键盘能力 ———
// 口径(2026-09-03 与用户确认):对齐 macOS 原生终端。⌘ 系归应用,由这里实现;
// Ctrl 系一律不拦,原样透传给 shell(全局分派在 app-pages.js,入口就把带 ctrlKey 的挡在外面)。
// 这几个函数由 app-pages.js 按「当前聚焦的是哪个应用」调用,自己不绑键。

// ⌘1..8 跳第 n 张,⌘9 跳最后一张(与 macOS 终端一致:9 不是第九张,是末张)。
function termAppActivateNth(n) {
  const t = n < 0 ? termAppTabs[termAppTabs.length - 1] : termAppTabs[n];
  if (!t) return false;
  termAppActivateTab(t.id);
  return true;
}
// ⌘⇧[ / ⌘⇧] 与 ⌃Tab 的循环切换。首尾相接,单张时原地不动。
function termAppCycleTab(dir) {
  if (termAppTabs.length < 2) return false;
  const i = termAppTabs.findIndex((x) => x.id === termAppActiveId);
  if (i < 0) return false;
  const n = (i + dir + termAppTabs.length) % termAppTabs.length;
  termAppActivateTab(termAppTabs[n].id);
  return true;
}
// ⌘K:清屏并清回滚。term.clear() 只留当前行,回滚里的历史一并倒掉——这正是原生终端的语义。
// 不给 shell 发 clear 命令:那样会在提示符上留一行命令回显,且 TUI 占屏时会被当成输入。
function termAppClear() {
  const t = termAppActiveTab();
  if (!t || !t.embed || !t.embed.term) return false;
  try { t.embed.term.clear(); } catch { return false; }
  return true;
}
// ⌘+ / ⌘- / ⌘0:字号。夹在 8~24 之间——再小 TUI 的框线糊成一片,再大标准 80 列放不下。
// 改完必须 refit:列数变了要把新几何告诉 PTY,否则 shell 仍按旧宽度折行。
const TM_FONT_MIN = 8, TM_FONT_MAX = 24, TM_FONT_DEFAULT = 13;
function termAppFontSize(delta) {
  const t = termAppActiveTab();
  if (!t || !t.embed || !t.embed.term) return false;
  const term = t.embed.term;
  const cur = Number(term.options.fontSize) || TM_FONT_DEFAULT;
  const next = delta === 0 ? TM_FONT_DEFAULT : Math.min(TM_FONT_MAX, Math.max(TM_FONT_MIN, cur + delta));
  if (next === cur) return true;   // 已经顶到边:这一下仍算应用接住了,不要漏给浏览器去缩放整页
  try { term.options.fontSize = next; } catch { return false; }
  try { t.embed.refit(); } catch {}
  return true;
}
// ⌘C:有选区才算复制,没选区就不接管——让这一下什么也不发生,与原生终端一致。
// 中断进程是 Ctrl+C 的活,不在这里越权代劳。
function termAppCopySelection() {
  const t = termAppActiveTab();
  if (!t || !t.embed || !t.embed.term) return false;
  let sel = "";
  try { sel = t.embed.term.getSelection() || ""; } catch { sel = ""; }
  if (!sel) return false;
  try { navigator.clipboard.writeText(sel); } catch {}
  return true;
}
function termAppSelectAll() {
  const t = termAppActiveTab();
  if (!t || !t.embed || !t.embed.term) return false;
  try { t.embed.term.selectAll(); } catch { return false; }
  return true;
}

// 会话在别处被结束(侧栏会话列表的 ×):把对应标签摘掉,不再发一次删除
function termAppDropTab(sessionId) { termAppCloseTab(sessionId, false); }
// 应用页被关掉(页头的 × / 程序坞的关闭):每个标签背后都是一个 PTY,一并结束,不留后台进程
function terminalPageClosed() {
  const tabs = termAppTabs;
  termAppTabs = [];
  termAppActiveId = "";
  _termAppLRU = [];
  for (const t of tabs) {
    if (t.cwdTimer) clearInterval(t.cwdTimer);
    _termAppUnmount(t);
    try { api(`/workbench/terminals/${encodeURIComponent(t.id)}`, { method: "DELETE" }); } catch {}
  }
  persistTerminalTabs();
}
function termAppRenderTabs() {
  const p = terminalPage();
  if (!p || !p.el) return;
  const strip = p.el.querySelector(".tm-tabs");
  if (!strip) return;
  const active = termAppActiveTab();
  const windowTitle = p.el._headNode && p.el._headNode.querySelector(".tm-window-title");
  if (windowTitle) windowTitle.textContent = (active && active.title) || "终端";
  strip.innerHTML = "";
  for (const t of termAppTabs) {
    const d = document.createElement("div");
    d.className = "ps-item tm-tab" + (t.id === termAppActiveId ? " active" : "");
    d.dataset.id = t.id;   // 拖动改位按它认标签(见 app-pages.js 的 psTabsBindDrag)
    d.title = t.title + "\n" + t.id;
    d.innerHTML = `<span class="ps-ic">${_tmIconTabHtml()}</span>`
      + `<span class="ps-title">${escapeHtml(t.title)}</span>`
      + `<span class="ps-x" title="关闭">${ICONS.close}</span>`;
    d.onclick = () => termAppActivateTab(t.id);
    d.querySelector(".ps-x").onclick = (ev) => { ev.stopPropagation(); termAppCloseTab(t.id); };
    strip.appendChild(d);
  }
  psTabsBindDrag(strip, (ids) => {
    termAppTabs.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));
    termAppRenderTabs(); persistTerminalTabs();
  });
  const add = document.createElement("button");
  add.className = "tm-new"; add.type = "button"; add.title = "新建终端";
  add.innerHTML = _TM_ICON_PLUS;
  add.onclick = () => openShellTerminal();
  strip.appendChild(add);
}
// ---- 标签标题跟随 shell -----------------------------------------------------
// macOS 的终端从不探测进程,标题是 shell 自己在输出流里夹带的转义序列报上来的:
// OSC 0/1/2 设标题(0=窗口与标签、1=标签、2=窗口),OSC 7 报工作目录,格式是
// file://主机名/路径 —— 带主机名,⌘T 继承目录、代理图标都靠它。Apple 在
// /etc/zshrc_Apple_Terminal 里挂了 precmd/preexec,空闲时报目录、执行时报命令名。
//
// 关键在于 ssh 之后发这些字节的是远端的 shell:它写进远端 pty,顺着 ssh 流原样回到
// 本地终端,本地不必做任何事,标题就跟着主机和目录走。
//
// 我们原来只有轮询:后端 lsof 查本地 PTY 进程的 cwd。一 ssh 出去那个进程的 cwd 就
// 再也不动,标题永远停在起始目录,远端主机名更无从得知(2026-09-03 用户实见)。
// 口径:shell 报了就用 shell 的,没报才落回轮询。
//
// 注意本地这一路多半报不上来:Apple 那份 rc 里的 update_terminal_cwd 只在
// TERM_PROGRAM=Apple_Terminal 时才装,我们的 PTY 不是,所以本机 zsh 不发 OSC 7。
// 真正吃到红利的是 ssh 到 Linux——Debian/Ubuntu 的 bash 默认 PROMPT_COMMAND 就带
// `\e]0;\u@\h:\w\a`,一连上标题就活了。要让本机也报,得在我们起 shell 时补一段
// precmd(后端改动,另说)。

// "user@host: ~/work/x" / "user@host:~/work/x" —— Debian/Ubuntu 的 bash 默认写法
// "~/work/x" —— 只报目录;"vim foo.c" —— 报正在跑的命令(macOS 终端也这样,照实显示)
function _termTitleParse(raw) {
  const s = String(raw || "").trim();
  if (!s) return null;
  const m = s.match(/^[^\s@]+@([^\s:]+)\s*:\s*(.+)$/);
  return m ? { host: m[1], path: m[2].trim() } : { host: "", path: s };
}
// OSC 7 的载荷是一个 file URL,主机段可以为空(file:///path)
function _termOsc7Parse(raw) {
  const m = String(raw || "").match(/^file:\/\/([^/]*)(\/.*)$/);
  if (!m) return null;
  let path = m[2];
  try { path = decodeURIComponent(path); } catch {}
  return { host: m[1] || "", path };
}
// 标签上写什么:目录取末段,不是路径(正在跑的命令)就原样用;主机名只在它不是本机时才带上。
// 「本机」按这张标签第一次报上来的主机认——标签是在本地起的 shell,第一个主机必是本机,
// 不必再问后端要一次 hostname。
const TM_TAB_LABEL_MAX = 28;
function _termTabLabel(tab) {
  // 取最近报上来的那一条,而不是固定优先 OSC 7。固定优先试过,不行:本机 shell 报过一次
  // OSC 7 之后 ssh 出去,远端多半只发 OSC 0(Debian/Ubuntu 的 bash 就只发这个),
  // 旧的本机 cwd 会一直压着远端标题,标签就卡在本机那个目录上(实测复现)。
  // 「shell 最后说的那句就是现状」也更贴 macOS 的语义:空闲时报目录、执行时报命令名,
  // 本来就是同一条通道上的先后覆盖。
  const a = tab.oscCwd, b = tab.oscTitle;
  const src = (a && b) ? ((b.at || 0) >= (a.at || 0) ? b : a) : (a || b);
  if (!src) return "";
  const p = String(src.path || "").trim();
  if (!p) return "";
  const base = /[/~]/.test(p) ? (p.replace(/\/+$/, "").split("/").pop() || p) : p;
  let host = src.host && src.host !== tab.localHost ? src.host.split(".")[0] : "";   // 去掉域名后缀
  // 截断的方向不一样:目录从左边截(末段才是人认的那一截),主机名从右边截(前缀才是它的身份)。
  // 位置不够时先削主机——目录是一直在变的那一半,优先保住它。
  const cutDir = (x, n) => (x.length > n ? "…" + x.slice(x.length - n + 1) : x);
  const cutHost = (x, n) => (x.length > n ? x.slice(0, n - 1) + "…" : x);
  if (!host) return cutDir(base, TM_TAB_LABEL_MAX);
  const room = TM_TAB_LABEL_MAX - 1 - Math.min(base.length, 12);   // 至少给目录留 12 个字位
  host = cutHost(host, Math.max(6, room));
  return host + ":" + cutDir(base, TM_TAB_LABEL_MAX - host.length - 1);
}
function _termAppWireTitle(tab, h) {
  const term = h && h.term;
  if (!term) return;
  // 「本机」只认 OSC 7 报的第一个主机。标签是在本地起的 shell,它报的第一个主机必是本机。
  // 不拿 OSC 0/2 的标题立基线:macOS 本机 zsh 不发 OSC 7(Apple 那份 rc 只在
  // TERM_PROGRAM=Apple_Terminal 时才装),ssh 到 Linux 之后第一个报上来的就是远端主机——
  // 拿它当本机的话,恰恰在最需要显示主机名的那一路上把主机名抹掉了。
  const seenLocal = (o) => { if (o && o.host && !tab.localHost) tab.localHost = o.host; };
  const apply = () => {
    const label = _termTabLabel(tab);
    if (!label) return;
    tab.oscOwned = true;   // 这张标签的标题从此归 shell,轮询让路(见 termAppCwdPoll)
    if (tab.title === label) return;
    tab.title = label;
    termAppRenderTabs(); persistTerminalTabs();
  };
  // OSC 0/2 走 xterm 自带的事件;OSC 1(图标名=标签名)不在这个事件里,单独注册。
  const stamp = (o) => { if (o) o.at = performance.now(); return o; };
  try { term.onTitleChange((t) => { tab.oscTitle = stamp(_termTitleParse(t)); apply(); }); } catch {}
  if (!term.parser || !term.parser.registerOscHandler) return;
  // 处理器返回 false = 只是看一眼,继续交给后面的处理器,不截断 xterm 自己的处理。
  try {
    term.parser.registerOscHandler(1, (t) => {
      tab.oscTitle = stamp(_termTitleParse(t)); apply(); return false;
    });
    term.parser.registerOscHandler(7, (t) => {
      const o = stamp(_termOsc7Parse(t)); if (o) { seenLocal(o); tab.oscCwd = o; apply(); } return false;
    });
  } catch {}
}

// 标签名=当前目录。轮询按会话 id 问后端,与 pane 挂没挂载无关:被 LRU 回收的标签也照样更新标题。
// 这是兜底那一路:shell 一旦自己报过标题(oscOwned),连 lsof 都不必再打。
function termAppCwdPoll(tab) {
  const tick = async () => {
    if (!termAppTabs.includes(tab)) return;
    if (tab.oscOwned) return;
    let cwd = "";
    try { const r = await api(`/workbench/term/cwd?id=${encodeURIComponent(tab.id)}`); cwd = (r && r.cwd) || ""; } catch {}
    if (!cwd || !termAppTabs.includes(tab)) return;
    const base = cwd.replace(/\/+$/, "").split("/").pop() || cwd;
    if (tab.title !== base) { tab.title = base; termAppRenderTabs(); persistTerminalTabs(); }
  };
  tick();
  tab.cwdTimer = setInterval(tick, 2000);   // 与侧栏 startTermPoll 同量级节律,不用更快
}

// ---- 持久化 ----
// 应用页本身不进 cd_pages(persistPages 对 app 页返回 null),标签自己存一份。
// 不存不行:后端会话(task=True 不进 Vibing 列)刷新后仍活着,前端记录一丢就再无入口 → 进程泄露。
function persistTerminalTabs() {
  if (_termAppRestoring || (typeof _restoringPages !== "undefined" && _restoringPages)) return;
  const list = termAppTabs.map((t) => ({ id: t.id, title: t.title }));
  const act = termAppActiveTab();
  try { localStorage.setItem("cd_terminal", JSON.stringify({ list, active: act ? act.id : "" })); } catch {}
}
// 旧版本把终端存在 cd_pages 里(kind: "term"),restorePages 在这里把它们转交过来,迁移只发生一次
function termAppAdoptLegacy(items) { _termAppLegacy = (items || []).slice(); }
async function restoreTerminalTabs() {
  // 窄屏下终端整体不可用(见 app-chat.js 的 termUsable)。这一路是从 localStorage 把上次
  // 开着的终端标签原样恢复出来,不经 openTerminalApp,所以那道拦截拦不到它——手机上首次
  // 打开仍会建出终端页、挂上 xterm,而 PTY 早随 app 重启被杀光,屏幕上就是一句
  // 「终端已退出」(2026-08-18 手机实见)。存的标签不清掉:回到电脑上还要用。
  if (typeof termUsable === "function" && !termUsable()) return;
  let d;
  try { d = JSON.parse(localStorage.getItem("cd_terminal") || "null"); } catch {}
  const saved = (d && Array.isArray(d.list)) ? d.list : [];
  const items = [], seen = new Set();
  for (const x of saved.concat(_termAppLegacy)) {
    const id = String((x && x.id) || "");
    if (!id || seen.has(id)) continue;
    seen.add(id);
    items.push({ id, title: (x && x.title) || "终端" });
  }
  _termAppLegacy = [];
  if (!items.length) return;
  // app 重启会杀光 PTY(kill_all),死会话不留死标签;跳过的条目下次落盘自然从 localStorage 掉队
  let live = [];
  try { live = (await refreshTerms()).filter((t) => t.alive).map((t) => t.id); } catch {}
  const alive = items.filter((x) => live.includes(x.id));
  if (!alive.length) { try { localStorage.removeItem("cd_terminal"); } catch {} return; }
  _termAppRestoring = true;
  terminalEnsurePage(false);
  for (const x of alive) _termAppAddTab(x.id, x.title, false);
  _termAppRestoring = false;
  const act = alive.find((x) => x.id === (d && d.active)) || alive[0];
  termAppActiveId = act.id;
  // 只登记「哪张在前台」,不在这里挂 xterm:应用页此刻是隐藏的,量不出尺寸,
  // 而且刷新后未必会打开它。真正连上后端是在 termAppShown(应用显示出来那一刻)。
  termAppTabs.forEach((t) => { if (t.host) t.host.style.display = t.id === act.id ? "" : "none"; });
  termAppRenderTabs();
  persistTerminalTabs();
}
