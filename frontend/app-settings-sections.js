// 由 app-settings.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 2/4 片
// 浏览器操控小节：Desk 内置浏览器是常驻能力，不需要逐引擎开关；这里呈现真实通道、
// 三种授权路径和已有站点授权。外部 Chrome/Playwright 的旧接口仅保留兼容，不再占默认页。
async function renderBrowserSection(box) {
  const card = box.querySelector(".sm-card");
  if (card) card.replaceChildren();
  const loading = document.createElement("div"); loading.className = "sm-prov-line"; loading.textContent = DeskI18n.tx("读取中…");
  box.appendChild(loading);
  let probe, grantResult, grantsAvailable = true;
  try {
    probe = await api("/workbench/app-control/probe?app_id=browser", { timeout: 8000 });
    try { grantResult = await _apprNative("grantsList", { appId: "browser" }); }
    catch (_) { grantsAvailable = false; grantResult = { grants: [] }; }
  }
  catch (e) { loading.textContent = DeskI18n.tx("读取失败：{error}（后端路由需重启 Desk 生效）", { error: e.message }); return; }
  loading.remove();
  const grants = Array.isArray(grantResult.grants) ? grantResult.grants : [];
  const ready = probe?.transport?.status === "ready" && probe?.app?.available === true;

  const status = document.createElement("div"); status.className = "brw-status-row";
  status.innerHTML = `<span class="pv-dot ${ready ? "on" : ""}"></span>
    <span class="it-name-cell"><span class="pv-name">${DeskI18n.txh("内置浏览器")}</span>
    <span class="pv-id-sub">Desk App Control</span></span>
    ${_pvState(ready, ready ? "已就绪" : "不可用")}`;
  const test = document.createElement("button"); test.type = "button"; test.className = "sm-link-btn";
  test.textContent = DeskI18n.tx("测试连接");
  test.onclick = async () => {
    test.disabled = true; test.textContent = DeskI18n.tx("测试中…");
    try {
      const latest = await api("/workbench/app-control/probe?app_id=browser", { timeout: 8000 });
      test.textContent = latest?.transport?.status === "ready" && latest?.app?.available ? DeskI18n.tx("连接正常") : DeskI18n.tx("连接异常");
    } catch (e) { test.textContent = DeskI18n.tx("测试失败"); test.title = e.message; }
    finally { setTimeout(() => { if (test.isConnected) { test.disabled = false; test.textContent = DeskI18n.tx("测试连接"); } }, 1800); }
  };
  status.appendChild(test); box.appendChild(status);

  const paths = document.createElement("div"); paths.className = "brw-paths";
  const controlState = probe?.control?.status || "";
  const visibleState = ({ ready: "控制中", available_to_claim: "可以接管", busy: "使用中", paused: "已暂停" })[controlState]
    || "点击后可用";
  const rows = [
    ["隔离网页", "不使用你的 Cookie 与登录状态，Agent 可直接打开", DeskI18n.tx("直接可用")],
    ["登录态网页", "首次按站点确认，授权默认保留一天",
      grantsAvailable ? (grants.length ? DeskI18n.tx("已授权 {count} 个站点", { count: grants.length }) : DeskI18n.tx("使用时确认"))
        : DeskI18n.tx("桌面端管理")],
    ["当前标签页", "你点击“交给 Agent”后，Agent 才能接管", DeskI18n.tx(visibleState)],
  ];
  for (const [name, detail, state] of rows) {
    const row = document.createElement("div"); row.className = "brw-path-row";
    row.innerHTML = `<span class="it-name-cell"><span class="pv-name">${DeskI18n.txh(name)}</span>
      <span class="pv-id-sub">${DeskI18n.txh(detail)}</span></span><span class="pv-state">${escapeHtml(state)}</span>`;
    paths.appendChild(row);
  }
  box.appendChild(paths);

  const grantsBox = document.createElement("div"); grantsBox.className = "brw-grants";
  const grantsHead = document.createElement("div"); grantsHead.className = "brw-grants-head";
  grantsHead.innerHTML = '<span class="sm-sub">' + DeskI18n.txh("已授权站点") + '</span><span class="pv-id-sub">'
    + DeskI18n.txh("仅用于带登录态开页") + '</span>';
  grantsBox.appendChild(grantsHead);
  if (!grantsAvailable || !grants.length) {
    const empty = document.createElement("div"); empty.className = "sm-prov-line";
    empty.textContent = grantsAvailable ? DeskI18n.tx("暂无站点授权。") : DeskI18n.tx("授权记录仅可在 Desk 桌面端查看和撤销。");
    grantsBox.appendChild(empty);
  } else {
    for (const grant of grants) {
      const row = document.createElement("div"); row.className = "brw-grant-row";
      const expiry = grant.expires_at ? new Date(grant.expires_at * 1000).toLocaleString(DeskI18n.tag, { hour12: false }) : "";
      row.innerHTML = `<span class="it-name-cell"><span class="pv-name">${escapeHtml(grant.origin || DeskI18n.tx("未知站点"))}</span>
        <span class="pv-id-sub">${expiry ? DeskI18n.txh("有效至 {time}", { time: expiry }) : DeskI18n.txh("授权有效")}</span></span>`;
      const revoke = document.createElement("button"); revoke.type = "button"; revoke.className = "sm-link-btn";
      revoke.textContent = DeskI18n.tx("撤销");
      revoke.onclick = async () => {
        revoke.disabled = true;
        try {
          await _apprNative("grantRevoke", { origin: grant.origin, scopeKey: grant.scope_key || "" });
          await renderBrowserSection(box);
        } catch (e) { revoke.disabled = false; alert(DeskI18n.tx("撤销失败：{error}", { error: e.message })); }
      };
      row.appendChild(revoke); grantsBox.appendChild(row);
    }
  }
  box.appendChild(grantsBox);
}
const EYE_SVG = '<svg viewBox="0 0 32 32" width="15" height="15" data-desk-scattered="12" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M3.8 16Q8.4 8.6 16 8.6T28.2 16Q23.6 23.4 16 23.4T3.8 16Z"/><circle cx="16" cy="16" r="3.2"/></g></svg>';
// 单个密钥输入框 + 框内显示/隐藏切换。已设 key:直接显示掩码内容(头4+星+尾4),
// 眼睛在掩码/明文间切换;用户改动后(值既非掩码也非明文)按新 key 处理。
function keyField(placeholder, masked, full) {
  masked = masked || ""; full = full || "";
  const wrap = document.createElement("div"); wrap.className = "sm-key-wrap";
  const inp = document.createElement("input"); inp.className = "sm-input pv-newkey";
  inp.autocomplete = "off"; inp.placeholder = DeskI18n.tx(placeholder);
  if (masked) { inp.type = "text"; inp.value = masked; }
  else { inp.type = "password"; }
  const eye = document.createElement("button"); eye.className = "sm-eye"; eye.type = "button";
  eye.innerHTML = EYE_SVG; eye.title = DeskI18n.tx("显示 / 隐藏");
  eye.onclick = () => {
    if (masked && (inp.value === masked || inp.value === full)) {
      const showFull = inp.value === masked;
      inp.value = showFull ? full : masked;
      eye.classList.toggle("on", showFull);
    } else {
      inp.type = inp.type === "password" ? "text" : "password";
      eye.classList.toggle("on");
    }
  };
  wrap.append(inp, eye);
  wrap._input = inp; wrap._masked = masked; wrap._full = full;
  // 取用户输入的新 key:值非空且不等于掩码/明文才算改动
  wrap.newKey = () => { const v = inp.value; return (v && v !== masked && v !== full) ? v : ""; };
  return wrap;
}
// 引擎凭证登录通用 TUI:app 内嵌 xterm 跑引擎原生 login(如 codex login)
// 登录终端弹窗：保留原有 shell、关闭与断线重连交互，只展示任务标题和终端。
async function _authTuiModal(termId, headTitle, engineTag, onClose) {
  const back = document.createElement("div"); back.className = "auth-tui-back";
  const panel = document.createElement("div"); panel.className = "auth-tui-panel auth-tui-terminal";
  const titleParts = String(headTitle || DeskI18n.tx("操作终端")).split(" · ");
  const titleText = (titleParts.shift() || DeskI18n.tx("操作终端"))
    .replace(/^claude\b/i, "Claude").replace(/^codex\b/i, "Codex").replace(/^pi\b/i, "Pi");
  const titleId = "auth-tui-title-" + String(termId).replace(/[^a-zA-Z0-9_-]/g, "");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-labelledby", titleId);
  const head = document.createElement("div"); head.className = "auth-tui-head";
  const copy = document.createElement("div"); copy.className = "auth-tui-copy";
  const title = document.createElement("div"); title.className = "auth-tui-title";
  const strong = document.createElement("strong"); strong.id = titleId; strong.textContent = titleText;
  title.appendChild(strong);
  // 不展示命令副标题或运行时诊断。
  copy.append(title);
  const controls = document.createElement("div"); controls.className = "auth-tui-controls";
  const x = document.createElement("button"); x.className = "auth-tui-x"; x.type = "button";
  x.textContent = "×"; x.setAttribute("aria-label", DeskI18n.tx("关闭终端"));
  controls.append(x); head.append(copy, controls);
  const stage = document.createElement("div"); stage.className = "auth-tui-stage";
  const host = document.createElement("div"); host.className = "auth-tui-host";
  stage.appendChild(host);
  const foot = document.createElement("footer"); foot.className = "auth-tui-foot";
  const done = document.createElement("button"); done.type = "button"; done.className = "auth-tui-done";
  done.textContent = DeskI18n.tx("完成并返回");
  foot.append(done);
  panel.append(head, stage, foot); back.appendChild(panel); document.body.appendChild(back);
  let inst = null, closed = false, checking = false;
  const finish = () => {
    if (closed) return; closed = true;
    document.removeEventListener("visibilitychange", onVis);
    try { inst && inst.dispose(); } catch {}
    api(`/workbench/terminals/${encodeURIComponent(termId)}`, { method: "DELETE" }).catch(() => null);
    back.remove();
    // 登录发生在终端子进程里，Desk 收不到 CLI 的完成事件。关闭终端就是用户明确返回
    // 设置页的时刻：先失效后端五分钟登录缓存，再重画；否则刚登录仍会沿用旧的“未登录”。
    _smCache = null;
    api("/workbench/setup", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ refresh: true }) }).catch(() => null).finally(() => {
        if (typeof renderSettings === "function" && document.getElementById("sm-host")) renderSettings();
    });
    if (onClose) { try { onClose(); } catch {} }
  };
  x.onclick = finish; done.onclick = finish;
  back.addEventListener("mousedown", (e) => { if (e.target === back) finish(); });   // 点遮罩关闭

  // 断线 ≠ 进程退出。原来 ws 一 close 就 finish(收浮层 + 删终端):手机切去别的 app,
  // 浏览器把页面挂起、掐断 WebSocket,回来时跑了一半的向导连进程带浮层全没了;
  // PC 休眠、网络抖动同理(2026-08-26 用户实测)。所以 close 时先问后端终端还活着吗:
  // 活着就重连接回去，确认进程退出后按原有方式收起并复查。
  const stillAlive = async () => {
    try {
      const ls = await api("/workbench/terminals", { timeout: 8000 });
      const t = (Array.isArray(ls) ? ls : []).find((it) => it.id === termId);
      return !!(t && t.alive !== false);
    } catch {
      // 查不到状态(刚恢复网络还没通)按活着处理:误留一个浮层可以再点 ×,
      // 误删一个跑了一半的向导找不回来。
      return true;
    }
  };
  const onDrop = async () => {
    if (closed || checking) return;
    checking = true;
    const alive = await stillAlive();
    checking = false;
    if (closed) return;
    if (alive) attach();
    else finish();
  };
  const attach = async () => {
    const got = await embedTerminal(host, termId, engineTag);
    if (closed) { try { got && got.dispose(); } catch {} return; }
    if (!got) { alert(DeskI18n.tx("无法显示终端，请返回设置检查。")); finish(); return; }
    inst = got;
    const socket = inst.ws;
    socket.addEventListener("close", () => { if (!socket._intentional) onDrop(); });
  };
  // 切回页面时主动查一次:有的浏览器挂起期间不派 close,回来 socket 半死,
  // 界面看着还在、敲什么都没反应。
  const onVis = () => {
    if (document.visibilityState === "visible" && inst && inst.ws.readyState > 1) onDrop();
  };
  document.addEventListener("visibilitychange", onVis);
  await attach();
}

// 保留普通 shell 与预填命令的登录方式，由用户按原有方式执行和输入。

async function openLoginTUI(initCmd, headTitle, source) {
  let r;
  try {
    r = await api("/workbench/shell", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ init: initCmd + " " }) });   // 末尾空格保留命令与光标间距
  } catch (e) { alert(DeskI18n.tx("打开登录终端失败：{error}", { error: e.message })); return; }
  if (!r || !r.id) { alert(DeskI18n.tx("打开登录终端失败")); return; }
  const engine = source?.engine || ({ "claude auth login": "claude", "codex login": "codex",
    "pi": "pi" })[initCmd.trim()];
  await _authTuiModal(r.id, headTitle, "shell", async () => {
    if (!engine) return;
    try {
      await api("/workbench/auth-refresh", { method: "POST",
        body: JSON.stringify({ engine, provider: source?.provider || engine }) });
      if (typeof _hsViews !== "undefined") {
        for (const v of _hsViews.values()) if (typeof hsEngineSync === "function") hsEngineSync(v);
      }
    } catch { /* A failed probe preserves the error; closing a terminal is not login success. */ }
  });
}

let _updateStatusTimer = 0;
let _updateActionBusy = false;
let _lastUpdateStatus = null;
let _updateCompletionTimer = 0;

function _notifyUpdateCompletion(st) {
  if (!st?.completion_id || !["succeeded", "failed"].includes(st.completion_outcome)) return;
  const kind = st.completion_outcome === "succeeded" ? "update_succeeded" : "update_failed";
  try { window.webkit.messageHandlers.desktopNotification.postMessage({
    eventId: `update:${st.completion_id}:${st.completion_outcome}`, kind,
  }); } catch {}
}

async function watchUpdateCompletions() {
  clearTimeout(_updateCompletionTimer);
  if (typeof isLocalView === "function" && !isLocalView()) return;
  let busy = false;
  try {
    const st = await api("/workbench/update-check");
    _notifyUpdateCompletion(st);
    busy = _updateInProgress(st);
  } catch {}
  _updateCompletionTimer = setTimeout(watchUpdateCompletions, busy ? 2000 : 30000);
}

function _updateInProgress(st) {
  return !!(st && (st.checking || ["downloading", "applying", "switching", "verifying", "apply_unknown"].includes(st.state)));
}

function _pollUpdateStatus(st) {
  clearTimeout(_updateStatusTimer);
  _updateStatusTimer = 0;
  if (!_updateInProgress(st)) return;
  _updateStatusTimer = setTimeout(() => {
    if (document.getElementById("smp-update-status") || document.getElementById("sm-update-status"))
      refreshUpdateStatus();
  }, 1000);
}

// 每次点击只执行当前阶段；下载绝不连带安装，安装仍需明确确认中断会话。
async function openUpdateTUI() {
  if (_updateActionBusy) return;
  _updateActionBusy = true;
  let action = "检查更新";
  _paintUpdateStatus(_lastUpdateStatus);
  try {
    const st = await api("/workbench/update-check");
    _paintUpdateStatus(st);
    if (_updateInProgress(st)) return;
    if (st && (st.dev || st.state === "dev")) {
      alert(DeskI18n.tx("当前为开发版，版本由源码同步管理；此入口不会下载或安装发行包。"));
      return;
    }
    if (st && st.state === "staged") {
      action = "启动更新";
      let busy = 0;
      try { busy = (await api("/workbench/terminals")).filter((x) => x.busy || x.agents).length; } catch {}
      const suffix = busy ? "\n\n" + DeskI18n.tx("当前检测到 {count} 个任务仍在运行。", { count: busy }) : "";
      if (!confirm(DeskI18n.tx("更新到 {version} 并重启 Desk？重启会中断所有会话。",
        { version: st.latest || st.staged_rev || DeskI18n.tx("新版本") }) + suffix)) return;
      const r = await api("/workbench/update/apply", { method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ confirm: st.staged_rev }) });
      if (!r || !r.ok) throw new Error((r && r.error) || DeskI18n.tx("启动失败"));
      return;
    }
    const download = !!(st && st.hasUpdate);
    action = download ? "下载更新" : "检查更新";
    const r = await api(download ? "/workbench/update/download" : "/workbench/update/check", { method: "POST" });
    if (!r || r.ok === false) throw new Error((r && r.error) || DeskI18n.tx("请求失败"));
    _paintUpdateStatus(r);
  } catch (e) { alert(DeskI18n.tx("{action}失败：{error}", { action: DeskI18n.tx(action), error: e.message })); }
  finally {
    _updateActionBusy = false;
    await refreshUpdateStatus();
  }
}

// 查更新:对比本地与远端 dist 版本，更新设置目录底部的版本状态。返回检查结果。
async function refreshUpdateStatus() {
  let st = null;
  try { st = await api("/workbench/update-check"); }
  catch { st = { ...(_lastUpdateStatus || {}), statusUnavailable: true }; }
  _paintUpdateStatus(st);
  return st;
}

function _paintUpdateStatus(st) {
  _lastUpdateStatus = st;
  const el = document.getElementById("smp-update-status") || document.getElementById("sm-update-status");
  _pollUpdateStatus(st);
  if (!el) return;
  const button = document.getElementById("smp-update");
  if (button) button.disabled = _updateActionBusy || (!st?.statusUnavailable && _updateInProgress(st));
  el.classList.toggle("has-update", !!(st && st.hasUpdate));
  if (button) button.classList.toggle("has-update", !!(st && (st.hasUpdate || st.state === "staged")));
  let key = "settings.update", params = {};
  if (st?.statusUnavailable) key = "update.offline";
  else if (st?.acceptance_unknown) key = "update.unconfirmed";
  else if (st && st.state === "downloading") { key = "update.downloading"; params = {progress: st.progress || 0}; }
  else if (st && st.checking) key = "update.checking";
  else if (st && (st.dev || st.state === "dev")) key = "update.dev";
  else if (st?.host_outdated) key = "update.hostMismatch";
  else if (st && st.state === "staged") key = "update.install";
  else if (st && st.state === "apply_unknown") key = "update.unconfirmed";
  else if (!st) key = "settings.update";
  else if (["applying", "switching", "verifying"].includes(st.state)) key = "update.applying";
  else if (st.state === "rolled_back" || st.state === "error") key = st.hasUpdate ? "update.retryDownload" : "update.recheck";
  else if (st.hasUpdate) key = "update.download";
  else if (st.reachable && !st.unknown) key = "update.latest";
  DeskI18n.bind(el, key, params);
  if (button) {
    const action = el.textContent;
    button.setAttribute("aria-label", action);
    const versions = st?.host_identity_known
      ? DeskI18n.tx("载荷 {payload} · 宿主 {host}", { payload: st.current || DeskI18n.tx("未知"), host: st.host_current || DeskI18n.tx("未知") }) : "";
    button.title = [action, versions, st?.error, st?.fix].filter(Boolean).join(" · ");
  }
}
