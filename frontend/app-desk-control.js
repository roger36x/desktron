// Desk 自身的 App Control 首期只委托 navigation surface。
// 可观察项必须在这里逐个登记；未登记的主页面内容一律不可见、不可操作。
(function initDeskAppControl() {
  const bridge = window.webkit?.messageHandlers?.deskControl;
  if (!bridge) return;

  const protectedSelectors = [
    "#panel-list", "#main-stage", "#settings-modal", "#settings-btn",
    "#side-search", "#side-search-inp", "#new-session", "#fx-new",
    ".hs-sit-approvals", ".hs-approval", "[data-sensitive]", "[data-private]",
  ];
  document.querySelectorAll(protectedSelectors.join(",")).forEach((node) => {
    node.setAttribute("data-app-control-protected", "true");
  });

  const safeItems = [
    ["#tn-side", "toggle_panel", "收起或展开侧栏"],
    ["#fx-tasks", "arrangements", "安排"],
    ["#fx-apps", "apps", "应用"],
  ];
  safeItems.forEach(([selector, id, label]) => {
    const node = document.querySelector(selector);
    if (!node) return;
    node.setAttribute("data-app-control-surface-id", "navigation");
    node.setAttribute("data-app-control-item", id);
    node.setAttribute("data-app-control-label", label);
  });

  // 与「调试」小节同一套版式:标题行 = 图标 + 文字 + 右侧开关(.sm-switch),说明与状态落在
  // 标题下的 .sm-hint。原先是卡片里一块「标题 / 状态 / 长说明 / 矩形按钮」,与上一行的
  // 调试开关并排时像两种控件(2026-09-07 用户实报)。开关本身仍是那个控制面按钮:
  // 委托 / 收回两个动作由 on/off 表达,pending 期间禁用;错误态 off,再点即重试。
  const button = document.createElement("button");
  button.id = "desk-agent-control";
  button.className = "sm-switch";
  button.type = "button";
  button.title = "把当前导航区域交给本会话 Agent 观察";
  button.setAttribute("role", "switch");
  button.setAttribute("aria-label", button.title);
  button.setAttribute("aria-checked", "false");
  button.setAttribute("data-app-control-protected", "true");
  button.setAttribute("data-app-control-control-plane", "true");
  const row = document.createElement("div");
  row.className = "sm-section sm-desk-control";
  row.dataset.settingsView = "debug";
  const title = document.createElement("div");
  title.className = "sm-title";
  const icon = document.createElement("span");
  icon.className = "sm-title-ic";
  icon.innerHTML = (typeof ICONS !== "undefined" && ICONS.sidebar) || "";
  title.append(icon, document.createTextNode("Desk 导航控制"), button);
  const info = document.createElement("div");
  info.className = "sm-hint";
  info.setAttribute("role", "status");
  info.setAttribute("aria-live", "polite");
  row.append(title, info);
  const description = "仅开放侧栏导航；截图需逐次确认，并隐藏会话正文、设置和审批内容。切换会话后自动收回。";

  let state = "idle", delegatedSession = "", pendingSession = "", interactionEpoch = 0;
  // 与原生 atDocumentStart 监听同处于捕获阶段。真人输入先同步推进不可回退的 epoch，
  // 再通知原生暂停；已经排队但尚未进入 __deskAgentCommand 的动作会立即失效。
  for (const type of ["pointerdown", "keydown"]) {
    document.addEventListener(type, (event) => {
      if (!event.isTrusted || event.target?.closest?.(
        "[data-app-control-control-plane],.hs-approval,.hs-sit-approvals")) return;
      if (state === "delegated") {
        interactionEpoch += 1;
        state = "paused";
        render();
      }
    }, true);
  }
  const stateLabels = { idle: "未开启", pending: "等待确认", delegated: "已开启", paused: "已暂停", error: "开启失败" };
  const currentSession = () => {
    const page = (typeof pActive !== "undefined" && pActive &&
      String(pActive.url || "").startsWith("chat:")) ? pActive : null;
    return page ? String(page.url).slice(5) : "";
  };
  const render = () => {
    const session = currentSession();
    button.disabled = !session || state === "pending";
    const on = state === "delegated" || state === "paused";
    button.classList.toggle("on", on);
    button.dataset.state = state;
    button.setAttribute("aria-checked", on ? "true" : "false");
    info.textContent = `${!session ? "请先打开一个会话" : stateLabels[state] || stateLabels.idle}。${description}`;
    button.title = !session ? "请先打开要使用此能力的会话" :
      (state === "delegated" ? "收回 Desk 导航观察权" :
      (state === "paused" ? "你正在操作，Agent 已暂停；点此彻底收回" :
      (state === "pending" ? "正在委托 Desk 导航观察权" :
      (state === "error" ? "委托失败，点此重试" :
      "把当前导航区域交给本会话 Agent 观察"))));
    button.setAttribute("aria-label", button.title);
  };
  // 设置页每次重绘都重新挂载同一个入口，不创建重复监听器或丢失委托状态。
  // host 是设置页的分组容器,本节作为独立小节接在「调试」之后,与它共用 debug 视图。
  window.mountDeskControlSettings = (host) => { host.appendChild(row); render(); };
  button.addEventListener("click", () => {
    const sessionId = currentSession();
    if (!sessionId) return;
    const action = state === "delegated" || state === "paused" ? "takeover" : "delegate";
    if (action === "delegate") { pendingSession = sessionId; state = "pending"; render(); }
    bridge.postMessage({action, sessionId});
  });
  window.__deskControlEvent = (event) => {
    if (!event || event.surface !== "navigation") return;
    interactionEpoch = Math.max(interactionEpoch, Number(event.interaction_epoch) || 0);
    const eventSession = String(event.session_id || pendingSession || delegatedSession || "");
    if ((event.type === "delegated" || event.type === "paused") &&
        (!eventSession || eventSession !== currentSession())) {
      bridge.postMessage({action: "resourceChanged", sessionId: eventSession});
      pendingSession = ""; delegatedSession = ""; state = "idle"; render(); return;
    }
    state = event.type === "delegated" ? "delegated" :
      (event.type === "paused" ? "paused" : (event.type === "error" ? "error" : "idle"));
    delegatedSession = state === "delegated" || state === "paused"
      ? eventSession : "";
    pendingSession = "";
    render();
  };
  window.__deskAgentCommand = (command) => {
    const action = String(command?.action || ""), input = command?.input || {};
    const commandSession = String(command?.session_id || "");
    // 定时收回只是界面兜底；动作执行本身必须再次确认仍是原会话。
    if (state !== "delegated" || Number(command?.interaction_epoch) !== interactionEpoch ||
        !commandSession || commandSession !== delegatedSession ||
        commandSession !== currentSession()) return false;
    if (action === "open_page") {
      const routes = {
        arrangements: () => typeof openArrangementsPage === "function" && openArrangementsPage(),
        apps: () => typeof openStore === "function" && openStore("app"),
      };
      if (!Object.prototype.hasOwnProperty.call(routes, input.page)) return false;
      routes[input.page]();
      return true;
    }
    if (action === "toggle_panel" && typeof input.open === "boolean") {
      const isOpen = !document.body.classList.contains("side-collapsed");
      if (isOpen !== input.open) document.querySelector("#tn-side")?.click();
      return true;
    }
    return false;
  };

  // 委托只属于当时的 Agent 会话。切换会话后立即收回，不能沿用租约观察别的会话。
  setInterval(() => {
    const session = currentSession();
    const boundSession = delegatedSession || pendingSession;
    if (boundSession && session !== boundSession) {
      bridge.postMessage({action: "resourceChanged", sessionId: boundSession});
      delegatedSession = ""; pendingSession = ""; state = "idle";
    }
    render();
  }, 500);
  render();
})();
