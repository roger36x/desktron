// Network policy uses the existing settings card, fields, buttons and theme tokens.
async function renderNetworkSection(box) {
  const card = box.querySelector('.sm-card');
  card.replaceChildren();
  const loading = document.createElement('div'); loading.className = 'sm-prov-line';
  loading.textContent = DeskI18n.tx('读取网络设置…'); card.appendChild(loading);
  let state;
  try { state = await api('/workbench/network-proxy'); }
  catch (error) { loading.textContent = DeskI18n.tx('网络设置读取失败：{error}。请在本机使用已更新的 Desk。', {error: error.message}); return; }
  if (!box.isConnected) return;
  card.replaceChildren();
  const form = document.createElement('form'); form.className = 'sm-network-form';
  const field = (label, control, id, parent = form) => {
    const wrap = document.createElement('div'); wrap.className = 'sm-field';
    const text = document.createElement('label'); text.className = 'sm-flabel';
    text.htmlFor = id; text.textContent = DeskI18n.tx(label); control.id = id;
    wrap.append(text, control); parent.appendChild(wrap); return wrap;
  };
  const mode = document.createElement('select'); mode.className = 'sm-select';
  for (const [value, label] of [['system', '跟随系统'], ['direct', '直连'], ['manual', '手动配置']]) {
    const option = document.createElement('option'); option.value = value; option.textContent = DeskI18n.tx(label);
    mode.appendChild(option);
  }
  mode.value = state.config.mode; field('代理模式', mode, 'sm-network-mode');
  const endpoint = document.createElement('div'); endpoint.className = 'sm-row sm-network-endpoint';
  form.appendChild(endpoint);
  const host = document.createElement('input'); host.className = 'sm-input';
  host.type = 'text'; host.autocomplete = 'off'; host.spellcheck = false;
  host.placeholder = DeskI18n.tx('例如 127.0.0.1'); host.value = state.config.host;
  const hostField = field('HTTP 代理地址', host, 'sm-network-host', endpoint);
  const port = document.createElement('input'); port.className = 'sm-input';
  port.type = 'number'; port.min = '1'; port.max = '65535'; port.step = '1';
  port.placeholder = DeskI18n.tx('代理客户端的实际端口'); port.value = state.config.port || '';
  const portField = field('端口', port, 'sm-network-port', endpoint);
  const bypass = document.createElement('input'); bypass.className = 'sm-input';
  bypass.autocomplete = 'off'; bypass.spellcheck = false; bypass.value = state.config.bypass.join(',');
  const bypassField = field('绕过地址（逗号分隔）', bypass, 'sm-network-bypass');
  const description = document.createElement('div'); description.className = 'sm-prov-line';
  form.appendChild(description);
  const refreshMode = () => {
    const manual = mode.value === 'manual';
    endpoint.hidden = hostField.hidden = portField.hidden = !manual;
    host.disabled = port.disabled = !manual || !state.supported;
    host.required = port.required = manual;
    bypassField.hidden = mode.value === 'direct';
    description.textContent = manual
      ? DeskI18n.tx('填写 HTTP 或混合端口，不是 SOCKS 端口或订阅地址。暂不支持代理用户名与密码。')
      : mode.value === 'system'
        ? DeskI18n.tx('读取 Windows 固定 HTTP 系统代理；PAC、自动发现或不等价的绕过规则会明确报错，不回退直连。')
        : DeskI18n.tx('Desk 不使用应用层代理；系统 VPN / TUN 仍可能影响网络。');
  };
  const notice = document.createElement('div'); notice.className = 'sm-prov-line';
  notice.textContent = DeskI18n.tx('本机地址始终绕过代理。保存不自动重启；需正常退出并重新打开 Desk，浏览器、引擎和安装下载才能统一生效。');
  form.appendChild(notice);
  const status = document.createElement('div'); status.className = 'sm-prov-line';
  status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const paint = () => {
    status.textContent = !state.supported ? DeskI18n.tx('此功能目前仅支持 Windows；其他平台配置保持不变。')
      : state.resolution_error || (!state.config.configured
        ? DeskI18n.tx('尚未保存统一配置，继续使用原有系统与环境设置。')
        : state.pending ? DeskI18n.tx('已保存，尚未完整生效。')
          : DeskI18n.tx('浏览器与后端已加载同一配置；不代表网页或模型已通过联网验收。'));
    if (state.supported && !state.browser_capable)
      status.textContent += ' ' + DeskI18n.tx('当前原生壳不支持统一配置，需要安装新版壳后重新打开。');
  };
  const result = document.createElement('div'); result.className = 'sm-prov-line';
  result.setAttribute('role', 'status'); result.setAttribute('aria-live', 'polite');
  const actions = document.createElement('div'); actions.className = 'sm-row sm-prow-actions';
  const save = document.createElement('button'); save.type = 'submit'; save.className = 'sm-save';
  save.textContent = DeskI18n.tx('保存配置');
  const test = document.createElement('button'); test.type = 'button'; test.className = 'sm-link-btn';
  test.textContent = DeskI18n.tx('测试代理端口');
  const reload = document.createElement('button'); reload.type = 'button'; reload.className = 'sm-link-btn';
  reload.textContent = DeskI18n.tx('读取最新配置');
  let busy = false, dirty = false;
  const controls = [mode, host, port, bypass, save, test, reload];
  const enable = () => {
    for (const el of controls) el.disabled = busy || !state.supported;
    refreshMode();
    host.disabled = port.disabled = busy || mode.value !== 'manual' || !state.supported;
  };
  mode.onchange = () => { dirty = true; result.textContent = ''; refreshMode(); };
  form.oninput = () => { dirty = true; result.textContent = ''; };
  const draft = () => ({mode: mode.value, host: host.value.trim(), port: port.value,
                         bypass: bypass.value});
  form.onsubmit = async (event) => {
    event.preventDefault();
    if (busy || !state.supported || !form.reportValidity()) return;
    busy = true; enable(); save.textContent = DeskI18n.tx('保存中…');
    try {
      state = await api('/workbench/network-proxy', {method: 'PUT',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({config: draft(), expected_revision: state.config.revision})});
      dirty = false; bypass.value = state.config.bypass.join(','); paint();
      result.textContent = DeskI18n.tx('保存成功。配置保存在本机，不修改系统代理；正在运行的回合不会被中断。');
    } catch (error) { result.textContent = DeskI18n.tx('保存失败：{error} 草稿已保留。', {error: error.message}); }
    finally { busy = false; enable(); save.textContent = DeskI18n.tx('保存配置'); }
  };
  test.onclick = async () => {
    if (busy || !state.supported || !form.reportValidity()) return;
    busy = true; enable(); test.textContent = DeskI18n.tx('测试中…');
    try {
      const checked = await api('/workbench/network-proxy/test', {method: 'POST',
        headers: {'content-type': 'application/json'}, body: JSON.stringify({config: draft()}), timeout: 8000});
      result.textContent = checked.message;
    } catch (error) { result.textContent = DeskI18n.tx('测试失败：{error}', {error: error.message}); }
    finally { busy = false; enable(); test.textContent = DeskI18n.tx('测试代理端口'); }
  };
  reload.onclick = () => {
    if (dirty && !confirm(DeskI18n.tx('读取最新配置会丢弃此页尚未保存的草稿，是否继续？'))) return;
    renderNetworkSection(box);
  };
  actions.append(save, test, reload); form.append(status, actions, result); card.appendChild(form);
  enable(); paint();
}
