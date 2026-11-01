// Windows 壳注入内置浏览器页(主框架)：真人点击、按键或滚轮时通知宿主让 Agent 暂停。
// isTrusted 过滤页面自己合成的事件；宿主经 DevTools 协议送达的输入在短窗口内不计为人操作。
(() => {
  if (globalThis.__deskAppControlGuard) return;
  globalThis.__deskAppControlGuard = true;
  const send = window.ipc && window.ipc.postMessage ? window.ipc.postMessage.bind(window.ipc)
    : (window.chrome && window.chrome.webview ? (m) => window.chrome.webview.postMessage(m) : null);
  if (!send) return;
  for (const type of ['pointerdown', 'keydown', 'wheel']) {
    document.addEventListener(type, (event) => {
      if (event.isTrusted) {
        try { send(JSON.stringify({ name: 'appControlUser', body: { kind: 'interaction' } })); } catch (_) {}
      }
    }, { capture: true, passive: true });
  }
})();
