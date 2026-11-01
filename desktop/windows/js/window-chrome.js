// 仅由支持原生双顶栏的新 Windows 壳注入主框架；不改变 macOS、普通浏览器或旧壳。
(() => {
  if (window.top !== window) return;
  // 启动占位页不是工作台，不为其开启布局或报告缺少 #main。
  if (window.location.origin !== 'http://127.0.0.1:7765' || !['/', '/index.html'].includes(window.location.pathname)) return;
  const bridge = window.webkit?.messageHandlers?.windowChrome;
  if (!bridge) { console.error('window_chrome_bridge_unavailable'); return; }
  let pending = false, previous = null;
  function sync() {
    pending = false;
    const main = document.getElementById('main');
    if (!main) return;
    const left = Math.max(0, main.getBoundingClientRect().left);
    if (!Number.isFinite(left) || left === previous) return;
    previous = left;
    document.documentElement.style.setProperty('--desk-main-left', left + 'px');
    bridge.postMessage({ left });
  }
  function schedule() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(sync);
  }
  function start() {
    const main = document.getElementById('main');
    if (!main) { console.error('window_chrome_main_unavailable'); return; }
    // 文档创建时 <html> 可能尚未出现；等解析完成再启用 CSS 并上报裁剪左缘。
    document.documentElement.classList.add('desk-windows-chrome');
    new ResizeObserver(schedule).observe(main);
    new MutationObserver(schedule).observe(document.body, { attributes: true, attributeFilter: ['class'] });
    window.addEventListener('resize', schedule);
    sync();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
