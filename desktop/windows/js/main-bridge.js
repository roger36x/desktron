// Windows 壳注入主页面(仅主框架、文档创建时)：提供与 macOS WKScriptMessageHandler 同名的桥，
// 前端照常调用 window.webkit.messageHandlers.<name>.postMessage(body)，无需区分平台。
// 消息经 WebView2 的 chrome.webview.postMessage 送达宿主；宿主只接受 127.0.0.1:7765 主框架来源。
(() => {
  if (window.top !== window) return;
  if (window.webkit && window.webkit.messageHandlers) return;
  const send = window.ipc && window.ipc.postMessage ? window.ipc.postMessage.bind(window.ipc)
    : (window.chrome && window.chrome.webview ? (m) => window.chrome.webview.postMessage(m) : null);
  if (!send) return;
  // nativeControl(macOS 原生应用 L1 观察)在 Windows 上没有实现，不暴露，设置页不显示该区块。
  const names = ['browser', 'windowDrag', 'windowZoom', 'windowChrome', 'startup', 'deskControl', 'deskControlUser',
    'approvalControl', 'desktopNotification'];
  const handlers = {};
  for (const name of names) {
    handlers[name] = Object.freeze({
      postMessage(body) { send(JSON.stringify({ name, body: body === undefined ? null : body })); },
    });
  }
  Object.defineProperty(window, 'webkit', {
    value: Object.freeze({ messageHandlers: Object.freeze(handlers) }),
    configurable: false, writable: false,
  });
  // WebView2 的文档创建脚本可能先于 <html>，不能中断后面的输入监听。
  const markShell = () => document.documentElement.classList.add('desk-windows-shell');
  if (document.documentElement) markShell();
  else document.addEventListener('DOMContentLoaded', markShell, { once: true });
  // 快捷键映射：前端按 macOS 口径处理 ⌘ 系应用快捷键，Ctrl 系一律留给 shell。
  // Windows 没有 ⌘，按 Windows Terminal 的习惯把 Ctrl+Shift+X 视为 ⌘X，Ctrl+Shift+Alt+X 视为 ⌘⇧X。
  // 只转发映射后的事件；页面处理了(preventDefault)才吞掉原始按键，否则原样交给 WebView2。
  const mappedKey = (code, shift) => {
    if (/^Key[A-Z]$/.test(code)) return shift ? code.slice(3) : code.slice(3).toLowerCase();
    if (/^Digit[0-9]$/.test(code)) return code.slice(5);
    const table = { BracketLeft: shift ? '{' : '[', BracketRight: shift ? '}' : ']',
      Equal: shift ? '+' : '=', Minus: shift ? '_' : '-',
      ArrowUp: 'ArrowUp', ArrowDown: 'ArrowDown', ArrowLeft: 'ArrowLeft', ArrowRight: 'ArrowRight' };
    return table[code] || null;
  };
  window.addEventListener('keydown', (event) => {
    if (!event.isTrusted || !event.ctrlKey || !event.shiftKey || event.metaKey) return;
    const shift = event.altKey;
    const key = mappedKey(event.code, shift);
    if (!key) return;
    const mapped = new KeyboardEvent('keydown', { key, code: event.code, metaKey: true, shiftKey: shift,
      ctrlKey: false, altKey: false, bubbles: true, cancelable: true, composed: true });
    (event.target || window).dispatchEvent(mapped);
    if (mapped.defaultPrevented) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  // 真人输入让 Agent 暂停(与 macOS 壳注入的监听相同)。控制面板内的操作不算介入。
  for (const type of ['pointerdown', 'keydown']) {
    document.addEventListener(type, (event) => {
      if (!event.isTrusted) return;
      try {
        handlers.deskControlUser.postMessage({
          controlPlane: !!event.target?.closest?.('[data-app-control-control-plane],.hs-approval,.hs-sit-approvals'),
        });
      } catch (_) {}
    }, true);
  }
})();
