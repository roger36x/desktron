(() => {
  if (window.__desktronLarkReference) return;
  window.__desktronLarkReference = {status: 'waiting'};
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const composer = () => Array.from(document.querySelectorAll(
    "textarea,[contenteditable='true'],[role='textbox']"))
    .filter(visible)
    .filter((el) => {
      const r = el.getBoundingClientRect();
      return r.width > 120 && r.top > innerHeight * 0.45 && r.right > innerWidth * 0.55;
    })
    .sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom)[0];
  const paneFor = (input) => {
    if (!input) return null;
    const rows = [];
    for (let el = input; el && el !== document.body; el = el.parentElement) {
      const r = el.getBoundingClientRect();
      if (r.width >= 280 && r.height >= innerHeight * 0.55
          && r.right >= innerWidth * 0.88 && r.left >= 40) rows.push({el, r});
    }
    rows.sort((a, b) => (a.r.width * a.r.height) - (b.r.width * b.r.height));
    return rows.length ? rows[0].el : null;
  };
  const focus = (pane) => {
    if (!pane) return false;
    let style = document.getElementById('__desktron_lark_reference_css');
    if (!style) {
      style = document.createElement('style');
      style.id = '__desktron_lark_reference_css';
      document.head.appendChild(style);
    }
    style.textContent = `
      .desktron-lark-reference-hidden{display:none!important;visibility:hidden!important;pointer-events:none!important}
      .desktron-focused-chat-pane{display:flex!important;flex:1 1 auto!important;width:100%!important;max-width:none!important;min-width:0!important}
    `;
    document.querySelectorAll('.desktron-focused-chat-pane').forEach((el) => {
      if (el !== pane) el.classList.remove('desktron-focused-chat-pane');
    });
    const pr = pane.getBoundingClientRect();
    Array.from(document.querySelectorAll('aside,nav,section,div'))
      .filter((el) => el !== pane && !el.contains(pane) && visible(el))
      .filter((el) => {
        const r = el.getBoundingClientRect();
        return r.left < pr.left && r.right <= pr.left + 12
          && r.height > innerHeight * 0.45 && r.width >= 32;
      })
      .forEach((el) => el.classList.add('desktron-lark-reference-hidden'));
    pane.classList.add('desktron-focused-chat-pane');
    for (let el = pane.parentElement; el && el !== document.body; el = el.parentElement) {
      el.style.minWidth = '0'; el.style.maxWidth = 'none';
      el.style.flex = '1 1 auto'; el.style.width = '100%';
    }
    window.__desktronLarkReference.status = 'focused';
    return true;
  };
  let ticks = 0;
  const run = () => {
    ticks += 1;
    const pane = paneFor(composer());
    if (pane) focus(pane);
    if (ticks >= 120) clearInterval(window.__desktronLarkReference.timer);
  };
  window.__desktronLarkReference.timer = setInterval(run, 500);
  run();
})();
