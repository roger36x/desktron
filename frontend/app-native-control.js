// 原生应用 L1 观察入口：只列发行版内置清单里的应用，窗口由原生选择框让人选定。
// Agent 只能读取窗口结构与开关状态；页面这里不接触任何 AX 内容。
(function initNativeAppControl() {
  const bridge = window.webkit?.messageHandlers?.nativeControl;
  if (!bridge) return;

  const section = document.createElement("div");
  section.className = "sm-section sm-native-control";
  section.dataset.settingsView = "debug";
  section.setAttribute("data-app-control-protected", "true");
  const list = document.createElement("div");
  list.className = "sm-native-control-list";
  section.append(list);
  const description = "Agent 只能读取窗口结构和开关状态，不能点击或输入；你操作该窗口时自动暂停。切换会话后自动收回。";
  const stateLabels = { idle: "未开启", pending: "请在弹窗中选择窗口", delegated: "已开启",
    paused: "你正在操作，已暂停", revoked: "目标已变化，已收回", error: "开启失败" };
  const apps = new Map();   // app_id -> {label, state, session, error}
  let trusted = false;

  const currentSession = () => {
    const page = (typeof pActive !== "undefined" && pActive &&
      String(pActive.url || "").startsWith("chat:")) ? pActive : null;
    return page ? String(page.url).slice(5) : "";
  };
  const render = () => {
    const session = currentSession();
    list.replaceChildren();
    for (const [appId, app] of apps) {
      const row = document.createElement("div");
      const title = document.createElement("div");
      title.className = "sm-title";
      const button = document.createElement("button");
      button.className = "sm-switch";
      button.type = "button";
      button.setAttribute("role", "switch");
      button.setAttribute("data-app-control-control-plane", "true");
      const on = app.state === "delegated" || app.state === "paused";
      button.classList.toggle("on", on);
      button.dataset.state = app.state;
      button.setAttribute("aria-checked", on ? "true" : "false");
      button.disabled = !session || app.state === "pending" || (!on && !trusted);
      button.title = !session ? "请先打开要使用此能力的会话" :
        (!trusted && !on ? "Desk 尚未获得辅助功能权限" :
        (on ? `收回 ${app.label} 观察权` : `把 ${app.label} 窗口交给本会话 Agent 观察`));
      button.setAttribute("aria-label", button.title);
      button.addEventListener("click", () => {
        const sessionId = currentSession();
        if (!sessionId) return;
        if (on) {
          bridge.postMessage({ action: "takeover", appId, sessionId });
          return;
        }
        app.state = "pending"; app.session = sessionId; app.error = ""; render();
        bridge.postMessage({ action: "delegate", appId, sessionId });
      });
      title.append(document.createTextNode(`${app.label} 观察`), button);
      const info = document.createElement("div");
      info.className = "sm-hint";
      info.setAttribute("role", "status");
      info.setAttribute("aria-live", "polite");
      const status = !session ? "请先打开一个会话" :
        (!trusted && !on ? "需先在系统设置中手动授予 Desk 辅助功能权限" :
        (app.state === "error" && app.error ? `开启失败：${app.error}` :
        (stateLabels[app.state] || stateLabels.idle)));
      info.textContent = `${status}。${description}`;
      row.append(title, info);
      list.append(row);
    }
    section.hidden = apps.size === 0;
  };

  window.__nativeControlEvent = (event) => {
    if (!event) return;
    if (event.type === "list") {
      trusted = !!event.trusted;
      const seen = new Set();
      for (const row of event.apps || []) {
        const id = String(row.app_id || "");
        if (!id) continue;
        seen.add(id);
        const prior = apps.get(id) || {};
        apps.set(id, { label: String(row.label || id), state: String(row.state || "idle"),
          session: String(row.session_id || ""), error: prior.error || "" });
      }
      for (const id of [...apps.keys()]) if (!seen.has(id)) apps.delete(id);
      render();
      return;
    }
    const app = apps.get(String(event.app_id || ""));
    if (!app) return;
    const session = String(event.session_id || app.session || "");
    // 委托只属于当时的会话；事件到达时已切走则立即收回。
    if ((event.type === "delegated" || event.type === "paused") && session !== currentSession()) {
      bridge.postMessage({ action: "resourceChanged", appId: event.app_id, sessionId: session });
      app.state = "idle"; app.session = ""; render(); return;
    }
    app.state = ["delegated", "paused", "error", "revoked"].includes(event.type) ? event.type : "idle";
    app.session = app.state === "delegated" || app.state === "paused" ? session : "";
    app.error = event.type === "error" ? String(event.error || "") : "";
    render();
  };

  window.mountNativeControlSettings = (host) => {
    host.appendChild(section);
    bridge.postMessage({ action: "list" });
    render();
  };

  setInterval(() => {
    const session = currentSession();
    let changed = false;
    for (const [appId, app] of apps) {
      if (app.session && session !== app.session &&
          (app.state === "delegated" || app.state === "paused" || app.state === "pending")) {
        bridge.postMessage({ action: "resourceChanged", appId, sessionId: app.session });
        app.state = "idle"; app.session = ""; changed = true;
      }
    }
    if (changed) render();
  }, 500);
})();
