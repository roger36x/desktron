// desktron 前端(app.js 拆分):引擎选择 + 全局点击绑定
// 经典脚本按序加载共享全局作用域,加载顺序见 index.html。勿单独重排。

let _statusTimer = 0;
let _lastStatus = true;
// 引擎层连接:启动拉引擎配置成功=已连接,失败=未连接。不跟单个会话 WS 翻转。
function setStatus(ok) {
  const el = $("#engine-status");
  el.className = ok ? "ok" : "bad";
  el.textContent = ok ? "引擎已连接" : "引擎未连接";
  _lastStatus = ok;
}
function setStatusMsg(msg) {
  const el = $("#engine-status");
  el.className = "warn";
  el.textContent = msg;
  clearTimeout(_statusTimer);
  _statusTimer = setTimeout(() => setStatus(_lastStatus), 3500);   // 恢复上次引擎连接状态
}
// 长按 = 右键。桌面那侧十几处菜单都挂在 oncontextmenu 上,而手机上这个事件根本不发:
// iOS 长按给的是自己的选择/预览浮层,Android 也只在可选文本上发。于是「右键能做的事」
// 在手机上一件都做不了——改名、移动工作区、关闭会话,都没有入口
// (2026-08-24 用户实见)。
//
// 不去逐处补触摸处理:那是十几个调用点各写一遍,迟早只改其中几处。这里在 document 上
// 认一次——按住不动 500ms,就朝那个元素自己的 oncontextmenu 递一个带坐标的事件。
// 谁挂了右键菜单,谁就自动有了长按,不必登记。
//
// 菜单弹出之后手指没抬、还在推,视作「其实想拖拽排序,不是想点菜单项」:收菜单,
// 派发 cd-longpress-drag 交给 makeReorderable(frontend/app-rail.js)接手——挂了右键菜单
// 的会话行/项目行,同时也是可排序的行,长按只有一次,菜单与拖拽必须二选一
// (2026-08-25 用户实见,手机端一直没有排序入口)。
const LONG_PRESS_MS = 500;
const LONG_PRESS_SLOP = 10;   // 手指按住时的自然抖动,超过就当成滚动
function _ctxHostOf(node) {
  for (let el = node; el && el !== document; el = el.parentNode) {
    if (typeof el.oncontextmenu === "function") return el;
  }
  return null;
}
function bindLongPressMenu() {
  let timer = 0, host = null, sx = 0, sy = 0, fired = false, firedEl = null, handedOff = false;
  // Safari 在长按的手指抬起后会朝原触点补发一组兼容鼠标事件。菜单此时已经打开，
  // 那个 mousedown 会被 openCtxMenu 当成“点了菜单外面”并立刻关闭，看起来就像长按无效。
  // 只吞掉长按之后紧随的第一枚 mousedown；真实点击菜单项仍由 pointerdown/click 正常处理。
  let compatMouse = false, compatMouseTimer = 0;
  const clearCompatMouse = () => {
    clearTimeout(compatMouseTimer); compatMouseTimer = 0; compatMouse = false;
  };
  const fromLongPressHost = (ev) => !!(firedEl && ev?.target
    && (ev.target === firedEl || firedEl.contains?.(ev.target)));
  const clear = () => {
    clearTimeout(timer); timer = 0; host = null;
    document.body?.classList.remove("reorder-pending");
  };
  document.addEventListener("touchstart", (ev) => {
    clear();   // 上一次若因系统手势丢了结束事件，先解除残留候选态
    fired = false; firedEl = null; handedOff = false;
    clearCompatMouse();
    if (!ev.touches || ev.touches.length !== 1) return clear();
    const t = ev.touches[0];
    host = _ctxHostOf(ev.target);
    if (!host) return;
    // 会话行会被状态轮询就地刷新，集合变化时甚至会整行重建。工作流行没有这层抖动，
    // 所以从按下开始暂停会话列表重绘，保证 500ms 后仍是同一个触点宿主。
    document.body?.classList.add("reorder-pending");
    sx = t.clientX; sy = t.clientY;
    timer = setTimeout(() => {
      timer = 0;
      const el = host;
      host = null;
      if (!el || !el.isConnected) return;
      fired = true; firedEl = el;
      compatMouse = true;
      compatMouseTimer = setTimeout(() => {
        clearCompatMouse(); fired = false; firedEl = null;
      }, 900);
      // 造一个右键事件递过去。菜单那侧读的是 clientX/clientY,并会调 preventDefault /
      // stopPropagation——都得是真方法,所以用真的 MouseEvent,不是字面量对象。
      const fake = new MouseEvent("contextmenu", {
        bubbles: false, cancelable: true, clientX: sx, clientY: sy, button: 2,
      });
      try { el.oncontextmenu(fake); } catch {}
    }, LONG_PRESS_MS);
  }, { passive: true, capture: true });
  document.addEventListener("touchmove", (ev) => {
    const t = ev.touches && ev.touches[0];
    if (!t) return;
    if (timer) {
      if (Math.abs(t.clientX - sx) > LONG_PRESS_SLOP
          || Math.abs(t.clientY - sy) > LONG_PRESS_SLOP) clear();   // 在滚,不是长按
      return;
    }
    // 菜单已经弹出(fired)之后手指还在推:不是要点菜单项,是想拖拽这一行去排序
    // (2026-08-25 用户实见「长摁后推动就变成拖拽排序,菜单如果已弹出,就让它消失」)。
    // 收起菜单,把这次触摸转交给 makeReorderable——它自己认 firedEl 归不归它管,
    // 这一层不必知道"排序"是什么。handedOff 挡同一次触摸重复转发;fired 保持不变,
    // 抬手时那个补发的 click 仍要吞掉(道理同长按弹菜单:不吞的话拖完一放手,
    // 这一行的 onclick 又把会话切走一次)。
    if (!fired || !firedEl) return;
    // 拦默认行为要排在判阈值之前,不能反过来——
    //
    // 手指是连续移动的,2px、5px、8px、11px 一帧一帧地来。浏览器自己的滚动起判阈值
    // (Android 的 touch slop 8dp、iOS 相近)比我们这里的 LONG_PRESS_SLOP=10 更小,
    // 于是它在 8px 那一帧就把这次触摸判给了滚动、把指针收走,等我们到 10px 再喊停,
    // 滚动已经开始,preventDefault 不再被理会,紧接着一个 pointercancel 把刚起势的
    // 拖拽掐掉。之前那版就是这么写的,CDP 里测不出来——合成事件是一步跨 20px,
    // 两个阈值在同一帧里判,我们恰好赢了;真机上一帧一帧走,必输(2026-08-25 用户实见)。
    //
    // 拦下之后浏览器不会启动滚动,后面再判要不要转拖拽就还来得及。菜单开着的时候
    // 底下那一列本来也不该跟着滚,所以对不可排序的行(长按只弹菜单)一并拦,不是副作用。
    // 监听器必须是非 passive,否则 preventDefault 直接被忽略。
    ev.preventDefault();
    if (handedOff) return;
    if (Math.abs(t.clientX - sx) > LONG_PRESS_SLOP || Math.abs(t.clientY - sy) > LONG_PRESS_SLOP) {
      handedOff = true;
      openCtxMenu.close?.();
      window.dispatchEvent(new CustomEvent("cd-longpress-drag", {
        detail: { el: firedEl, x: t.clientX, y: t.clientY, touchId: t.identifier },
      }));
    }
  }, { passive: false, capture: true });
  document.addEventListener("touchend", clear, { passive: true, capture: true });
  document.addEventListener("touchcancel", clear, { passive: true, capture: true });
  document.addEventListener("mousedown", (ev) => {
    if (!compatMouse || !fromLongPressHost(ev)) return;
    clearCompatMouse();
    // click 由 fired 单独拦；这里仅吞 Safari 补到原触摸宿主上的第一枚兼容 mousedown。
    ev.preventDefault();
    ev.stopImmediatePropagation();
  }, true);
  // 只认窗口自己的失焦。捕获阶段挂在 window 上会收到页面里任何元素的 blur(见 app-rail-nav.js
  // 的 winBlur),手机上点会话行时输入框失焦,长按候选就被误清。
  window.addEventListener("blur", (e) => { if (e.target === window) clear(); }, true);
  // 长按已经弹了菜单,手指抬起时浏览器还会补一次 click。不吞掉的话,菜单弹出的同时
  // 那一行也被点开了——人看到的是「长按把会话切走了,菜单一闪而过」。
  document.addEventListener("click", (ev) => {
    if (!fired) return;
    if (!fromLongPressHost(ev)) {
      fired = false; firedEl = null; clearCompatMouse();
      return;   // 菜单项的真实点击照常执行，但这次长按的兼容期到此结束
    }
    fired = false; firedEl = null;
    clearCompatMouse();
    ev.preventDefault();
    ev.stopPropagation();
  }, true);
}

function bind() {
  bindLongPressMenu();
  // 点空白处收起引擎菜单。pointerdown 一并监听:触屏上「点在别处」先到的是 pointer 事件,
  // 只听 click 的话,手指抬起前菜单还挂着,而抽屉那侧的收起判定已经跑过一轮。
  const shutEngineMenu = (e) => {
    const m = $("#engine-menu");
    if (!m || m.hidden) return;
    if (e && e.target && e.target.closest && e.target.closest("#engine-menu, #engine-pick")) return;
    m.hidden = true;
  };
  document.addEventListener("click", shutEngineMenu);
  document.addEventListener("pointerdown", shutEngineMenu);
  // 手机上引擎菜单会被挪到 body 下以越过抽屉遮罩。选项点击先触发抽屉收起，
  // 菜单却不再是 #panel 的子节点，若不跟着关闭就会失去依托、悬在会话正文上。
  window.addEventListener("cd-drawer-closed", shutEngineMenu);
  // overlay 已改为主区平级视图,不再需要"点外收起"
  // 资产视图里的链接全部拦截：外链开弹层，内链/相对路径阻止整窗跳走
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (!a || a.closest("#overlay")) return;
    const href = a.getAttribute("href") || "";
    if (!href) return;
    e.preventDefault();
    if (/^https?:\/\//i.test(href)) {
      let title = href;
      try { title = new URL(href).hostname; } catch {}
      openPage(href, title);
    } else if (/^\/workbench\/view\?/.test(href)) {
      // Desk 资产查看器链接：跟进
      const sp = new URLSearchParams(href.split("?")[1]);
      const p = sp.get("path") || "";
      if (p) { openFilesPage(p); return; }
      setStatusMsg("无法打开链接：" + href);
    } else {
      setStatusMsg("无法打开链接：" + href);
    }
  });
}

let curEngine = "claude";   // 缺省引擎(见 deskcfg.ENGINES)
let engineOpts = {};
let shepherdMode = false;
let shepherdTrial = false;
// Shepherd 是独立的会话书桌。开启时只认 Shepherd Conversation；普通模式按
// 当前原生引擎取会话，并显式排除 Shepherd，避免其 active engine 造成串台。
function sessionVisibleInCurrentMode(row) {
  if (!row || row.task) return false;
  if (shepherdMode) return row.shepherd_mode === true;
  return row.shepherd_mode !== true && (!row.engine || row.engine === curEngine);
}
let engineManifest = {};   // 后端 /workbench/engine 下发的 per-engine 元数据(desc/accent/icon/settingsShape 等)
let engineInfo = null;     // /workbench/engine 全量应答(欢迎页渲染引擎卡片用)

// 下列三张表是回退默认;initEnginePick 会用后端 manifest 覆盖同 key 项——
// 加引擎时后端描述符带上 desc/accent/icon 即可,前端这里不必再改(BRAND_ICONS 的 SVG 仍需前端补)。
const ENGINE_DESC = {
  claude: "本机订阅（claude auth login）,Anthropic 模型",
  codex: "本机登录（codex login）,OpenAI 模型",
  pi: "开源多 provider，本机凭证由 Pi 管理",
};
const ENGINE_ICON = { claude: "layers", codex: "code", pi: "terminal" };
// 引擎识别符：Claude/Codex 使用本项目自绘 A/C 与细短斜线外圈，不复刻官方标识。
// Pi 与 Shepherd 保持现有图形；制作摘要见 vendor/original-inline-icon-manifest.json。
const BRAND_ICONS = {
  claude: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" width=\"15\" height=\"15\" class=\"brand-claude desk-reviewed-icon\" data-desk-icon=\"BRAND_ICONS.claude\" role=\"img\" aria-label=\"Claude\"><g fill=\"none\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"0.7\" stroke-linecap=\"round\"><path d=\"M15.3636 3.2864l1.2728 -1.2728\" /><path d=\"M18.6758 3.5549l1.5588 -0.9000\" /><path d=\"M21.8057 4.6715l1.7387 -0.4659\" /><path d=\"M24.5399 6.5601l1.8000 0.0000\" /><path d=\"M26.6921 9.0921l1.7387 0.4659\" /><path d=\"M28.1157 12.0948l1.5588 0.9000\" /><path d=\"M28.7136 15.3636l1.2728 1.2728\" /><path d=\"M28.4451 18.6758l0.9000 1.5588\" /><path d=\"M27.3285 21.8057l0.4659 1.7387\" /><path d=\"M25.4399 24.5399l0.0000 1.8000\" /><path d=\"M22.9079 26.6921l-0.4659 1.7387\" /><path d=\"M19.9052 28.1157l-0.9000 1.5588\" /><path d=\"M16.6364 28.7136l-1.2728 1.2728\" /><path d=\"M13.3242 28.4451l-1.5588 0.9000\" /><path d=\"M10.1943 27.3285l-1.7387 0.4659\" /><path d=\"M7.4601 25.4399l-1.8000 0.0000\" /><path d=\"M5.3079 22.9079l-1.7387 -0.4659\" /><path d=\"M3.8843 19.9052l-1.5588 -0.9000\" /><path d=\"M3.2864 16.6364l-1.2728 -1.2728\" /><path d=\"M3.5549 13.3242l-0.9000 -1.5588\" /><path d=\"M4.6715 10.1943l-0.4659 -1.7387\" /><path d=\"M6.5601 7.4601l-0.0000 -1.8000\" /><path d=\"M9.0921 5.3079l0.4659 -1.7387\" /><path d=\"M12.0948 3.8843l0.9000 -1.5588\" /></g><g transform=\"translate(4 4)\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.8\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M5.6 19.2 10.7 5.5c.4-1.1 2.2-1.1 2.6 0l5.1 13.7M8.2 13.1h7.6\" /></g></g></svg>",
  codex: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" width=\"15\" height=\"15\" class=\"brand-codex desk-reviewed-icon\" data-desk-icon=\"BRAND_ICONS.codex\" role=\"img\" aria-label=\"Codex\"><g fill=\"none\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"0.7\" stroke-linecap=\"round\"><path d=\"M15.3636 3.2864l1.2728 -1.2728\" /><path d=\"M18.6758 3.5549l1.5588 -0.9000\" /><path d=\"M21.8057 4.6715l1.7387 -0.4659\" /><path d=\"M24.5399 6.5601l1.8000 0.0000\" /><path d=\"M26.6921 9.0921l1.7387 0.4659\" /><path d=\"M28.1157 12.0948l1.5588 0.9000\" /><path d=\"M28.7136 15.3636l1.2728 1.2728\" /><path d=\"M28.4451 18.6758l0.9000 1.5588\" /><path d=\"M27.3285 21.8057l0.4659 1.7387\" /><path d=\"M25.4399 24.5399l0.0000 1.8000\" /><path d=\"M22.9079 26.6921l-0.4659 1.7387\" /><path d=\"M19.9052 28.1157l-0.9000 1.5588\" /><path d=\"M16.6364 28.7136l-1.2728 1.2728\" /><path d=\"M13.3242 28.4451l-1.5588 0.9000\" /><path d=\"M10.1943 27.3285l-1.7387 0.4659\" /><path d=\"M7.4601 25.4399l-1.8000 0.0000\" /><path d=\"M5.3079 22.9079l-1.7387 -0.4659\" /><path d=\"M3.8843 19.9052l-1.5588 -0.9000\" /><path d=\"M3.2864 16.6364l-1.2728 -1.2728\" /><path d=\"M3.5549 13.3242l-0.9000 -1.5588\" /><path d=\"M4.6715 10.1943l-0.4659 -1.7387\" /><path d=\"M6.5601 7.4601l-0.0000 -1.8000\" /><path d=\"M9.0921 5.3079l0.4659 -1.7387\" /><path d=\"M12.0948 3.8843l0.9000 -1.5588\" /></g><g transform=\"translate(4 4)\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.8\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M18.4 6.7c-1.4-1.5-3.5-2.2-5.9-2.2-5 0-8.1 2.9-8.1 7.5s3.1 7.5 8.1 7.5c2.4 0 4.5-.7 5.9-2.2\" /></g></g></svg>",
  pi: '<svg class="brand-pi" viewBox="0 0 24 24" width="15" height="15" fill="currentColor" aria-label="pi"><rect x="5" y="5" width="10.5" height="3.5"/><rect x="5" y="8.5" width="3.5" height="3.5"/><rect x="12" y="8.5" width="3.5" height="3.5"/><rect x="5" y="12" width="7" height="3.5"/><rect x="15.5" y="12" width="3.5" height="3.5"/><rect x="5" y="15.5" width="3.5" height="3.5"/><rect x="15.5" y="15.5" width="3.5" height="3.5"/></svg>',
  shepherd: '<span class="brand-shepherd img-ic" role="img" aria-label="Shepherd"></span>',
};
// 引擎强调色：取各品牌图标的主色，作为网址框焦点边框等局部强调，随当前引擎切换。
const ENGINE_ACCENT = {
  claude: "rgba(217,119,87,.7)",     // #D97757 珊瑚
  codex: "rgba(16,163,127,.75)",     // #10A37F 青绿
  pi: "rgba(232,232,234,.55)",
};

// 取数与应用拆开:开机那份 /workbench/engine 会随合并端点(/workbench/boot)一起回来,
// 拿到之后直接走 applyEnginePick,不必再单独打一趟——经隧道时一趟就是一秒多。
async function initEnginePick() {
  let e = null;
  try { e = await api("/workbench/engine"); } catch { setStatus(false); return; }
  return applyEnginePick(e);
}

async function applyEnginePick(e) {
  if (!e || typeof e !== "object") { setStatus(false); return; }
  try {
    setStatus(true);   // 引擎配置拉到 = 引擎层已连接
    engineManifest = e.manifest || {};
    for (const [id, m] of Object.entries(engineManifest)) {   // manifest 覆盖回退默认(值一致;加引擎时由后端供给)
      if (m.desc) ENGINE_DESC[id] = m.desc;
      if (m.accent) ENGINE_ACCENT[id] = m.accent;
      if (m.icon) ENGINE_ICON[id] = m.icon;
    }
    curEngine = e.engine;
    shepherdMode = !!e.shepherd_mode;
    shepherdTrial = shepherdMode && !!e.shepherd_trial;
    engineInfo = e;
    // 欢迎页此刻若已渲染(占位引擎列表),用真实 label/可用态重绘
    if (typeof _termWatermark !== "undefined" && _termWatermark && _termWatermark.style.display !== "none") _showWatermark();
    // 欢迎页点了非当前引擎:切引擎重载后落到这里,续开新会话
    if (localStorage.getItem("cd_pending_new_session") !== null) {
      try { localStorage.removeItem("cd_pending_new_session"); } catch {}
      newTerminal(curEngine);
    }
    // 欢迎页第二步选了工作流且切了引擎:重载后续做对应动作(项目实体已退役)
    const _pa = localStorage.getItem("cd_pending_action");
    if (_pa !== null) {
      try { localStorage.removeItem("cd_pending_action"); } catch {}
      if (_pa === "new-workflow") openNewWorkspace();
    }
    document.documentElement.style.setProperty("--engine-accent", ENGINE_ACCENT[curEngine] || ENGINE_ACCENT.claude);
    engineOpts = e.opts || {};
    const btn = $("#engine-pick");
    const curLabel = e.engines[curEngine]?.label || curEngine;
    const ico = BRAND_ICONS[curEngine] || ICONS[ENGINE_ICON[curEngine]] || "";
    const pickIcon = shepherdMode ? BRAND_ICONS.shepherd : ico;
    const pickLabel = shepherdMode ? "Shepherd" : curLabel;
    btn.innerHTML = `<span class="fx-ic">${pickIcon}</span><span>${escapeHtml(pickLabel)}</span>`;   // 模式开启后只显示 Shepherd，避免与当前执行引擎形成双选中
    DeskI18n.bind(btn, shepherdMode ? "engineMenu.titleShepherd" : "engineMenu.title", { engine: curLabel }, "title");
    btn.onclick = (ev) => {
      ev.stopPropagation();
      const activeView = typeof hsSituationCurrentView === "function" ? hsSituationCurrentView() : null;
      const conversationMode = !!(activeView && !activeView.ephemeral && activeView.shepherdMode);
      const shepherdActive = conversationMode || shepherdMode;
      const dock = document.getElementById("task-dock");   // 互斥:开引擎菜单时收起任务面板
      if (dock) { dock.classList.remove("open"); document.getElementById("fx-tasks")?.classList.remove("active"); }
      const menu = $("#engine-menu");
      if (!menu.hidden) { menu.hidden = true; return; }
      menu.innerHTML = '<div class="fm-head"></div>';
      menu.firstChild.textContent = DeskI18n.t("engineMenu.standalone");
      for (const [id, meta] of Object.entries(e.engines)) {
        const ok = e.available[id];
        const row = document.createElement("div");
        const currentEngine = id === curEngine;
        row.className = "fm-row" + (currentEngine && !shepherdActive ? " on" : "") + (ok ? "" : " disabled");
        row.innerHTML = `<span class="fm-icon">${BRAND_ICONS[id] || ICONS[ENGINE_ICON[id]] || ""}</span>
          <span class="fm-main"><span class="fm-title">${escapeHtml(meta.label)}</span>
          <span class="fm-desc">${escapeHtml(ok ? (ENGINE_DESC[id] ? DeskI18n.t("engineMenu.desc." + id, {}, ENGINE_DESC[id]) : "")
            : DeskI18n.t("engineMenu.notInstalled"))}</span></span>
          <span class="fm-check" aria-hidden="true">${currentEngine && !shepherdActive ? RP_CHECK : ""}</span>`;
        if (ok) {
          row.onclick = async () => {
            // 请求可能要等一会儿；先收菜单，避免桌面端也一直留着旧选中态。
            // 点当前引擎没有切换动作，但同样应当收起菜单。
            menu.hidden = true;
            if (!shepherdActive && id === curEngine) return;
            // 每个引擎是一张独立书桌：先记住旧引擎此刻停在会话、应用还是功能页，
            // 再改后端偏好。放在 PUT 前面，失败时也只是多留一份可复用的本地快照。
            if (typeof rememberEngineView === "function") rememberEngineView(curEngine);
            await api("/workbench/engine", { method: "PUT", headers: { "content-type": "application/json" },
              // 普通引擎与 Shepherd 是互斥书桌；内部执行引擎改在聊天框模型菜单切换。
              body: JSON.stringify({ engine: id, shepherd_mode: false }) });
            // 换引擎是换上下文,主区不该停在上一个引擎的那段会话上。
            //
            // 恢复那侧已经按引擎筛过一道,但它挡不住这一页:persistPages 只在视图已经解析出
            // engine 时才写这个字段,而恢复出来没打开过的冷页一直没解析,于是记录里多数条目
            // 根本没有 engine,筛选对它们一律放行(2026-08-25 用户实见:切引擎之后,上一个
            // 引擎的最后一个会话还摆在主区)。
            //
            // 补上这一条的归属即可:此刻正看着它,它当然属于切换前的这个引擎。补完之后
            // 恢复那侧自己就筛得掉,不必在这里把 active 抹掉——抹掉的话切回这个引擎时
            // 也回不到它了,而那正是人想要的。
            try {
              const d = JSON.parse(localStorage.getItem("cd_pages") || "null");
              const row = d && d.active && (d.list || []).find((x) => "chat:" + x.id === d.active);
              if (row && !row.engine) {
                row.engine = curEngine;
                localStorage.setItem("cd_pages", JSON.stringify(d));
              }
            } catch {}
            if (typeof deskStartupMark === "function") deskStartupMark("reload", "engine_switch");
            location.reload();   // 会话存储与事件流随引擎切换，整页重载最干净
          };
        }
        menu.appendChild(row);
      }
      const separator = document.createElement("div");
      separator.className = "engine-menu-separator";
      separator.setAttribute("role", "separator");
      menu.appendChild(separator);
      const jointHead = document.createElement("div");
      jointHead.className = "fm-head";
      jointHead.textContent = DeskI18n.t("engineMenu.joint");
      menu.appendChild(jointHead);
      const selected = shepherdActive;
      const shepherd = document.createElement("div");
      shepherd.className = "fm-row fm-shepherd" + (selected ? " on" : "");
      shepherd.innerHTML = `<span class="fm-icon">${BRAND_ICONS.shepherd}</span>
        <span class="fm-main"><span class="fm-title">Shepherd</span>
        <span class="fm-desc">${escapeHtml(DeskI18n.t("engineMenu.shepherdDesc"))}</span></span>
        <span class="fm-check" aria-hidden="true">${selected ? RP_CHECK : ""}</span>`;
      shepherd.onclick = async () => {
        menu.hidden = true;
        try {
          const result = await api("/workbench/engine", {method:"PUT",
            headers:{"content-type":"application/json"},
            body:JSON.stringify({shepherd_mode:!selected, shepherd_trial:false})});
          if (typeof rememberEngineView === "function") rememberEngineView(curEngine);
          shepherdTrial = false;
          shepherdMode = !!result.shepherd_mode;
          e.shepherd_mode = shepherdMode;
          if (typeof deskStartupMark === "function") deskStartupMark("reload", "shepherd_mode_switch");
          location.reload();
        } catch (error) {
          if (typeof setStatusMsg === "function") setStatusMsg("Shepherd 模式未能保存：" + (error?.message || "请稍后重试"));
        }
      };
      menu.appendChild(shepherd);
      menu.hidden = false;
      // 挪到 body 再 fixed 定位锚在引擎行正上方:#panel 的 backdrop-filter 会把面板内的浮层裁在面板右缘
      if (menu.parentElement !== document.body) document.body.appendChild(menu);
      const br = btn.getBoundingClientRect();
      menu.style.position = "fixed";
      const mw = Math.min(menu.offsetWidth || 320, window.innerWidth - 16);
      menu.style.left = Math.max(8, Math.min(br.left, window.innerWidth - mw - 8)) + "px";
      menu.style.right = "auto";
      menu.style.top = "auto";
      menu.style.bottom = (window.innerHeight - br.top + 6) + "px";
    };
    // 仅退役额外侧栏的布局偏好；不触碰会话、主侧栏或页面状态。
    try { localStorage.removeItem("cd_multi_sidebar_v1"); } catch {}
  } catch { setStatus(false); }   // 拉不到引擎配置 = 引擎层未连接
}
