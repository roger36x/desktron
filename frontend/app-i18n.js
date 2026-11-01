// 界面语言模块：翻译查找、节点绑定与语言切换。译文按语言分包，见 app-i18n-<语言>.js。
// 服务端把本文件、各语言包与 app-core.js 合成同一个 /app-core.js 请求（frontend_bundle.py），
// 所以其后所有脚本都能直接使用 DeskI18n，首屏请求数不变。
//
// 新增一种语言：复制 app-i18n-en.js，改语言代码、label、tag 并翻译各值，再把文件名加进
// frontend_bundle.py 的 /app-core.js 清单（app-core.js 之前）。不需要改动本文件或业务代码；
// 缺少的键先回落到该包声明的 fallback，再回落到中文参照包。
//
// 只翻译显式绑定的节点，不扫描用户内容；切换语言不重载页面、不请求接口、不重建表单。
// 调用方按状态生成的中文文字通过参数 zh 传入，中文界面原样显示，其他语言才查语言包。
//
// 两种查法：t(key) 按键查 messages，适合跨页面复用的固定说法；tx("中文原文") 按原文查
// phrases，适合整页成批接入（如设置页），中文包不必重复原文。整页按原文接入的界面在
// 语言变化时自行重绘：setLocale 会派发 desk-locale 事件。
const DeskI18n = (() => {
  const BASE = 'zh';
  const ATTRS = ['title', 'placeholder', 'aria-label', 'data-ph', 'data-tip'];
  const packs = {};
  const reverse = {};            // 前缀 -> {中文说法: 键}，供 label() 反查；注册语言包时清空
  let requested = BASE;
  try { requested = localStorage.getItem('cd_locale') || BASE; } catch (_) {}
  const current = () => (packs[requested] ? requested : BASE);

  // pack: {label, tag, fallback, messages}; tag 是 BCP 47 语言标记，用于 html.lang、日期与复数规则。
  function register(code, pack) {
    packs[code] = { tag: code, fallback: BASE, ...pack, messages: { ...(pack && pack.messages) },
                    phrases: { ...(pack && pack.phrases) } };
    for (const prefix of Object.keys(reverse)) delete reverse[prefix];
  }
  function lookup(code, key) {
    const chain = [code, packs[code] && packs[code].fallback, BASE].filter((c, i, all) => c && all.indexOf(c) === i);
    for (const c of chain) {
      const value = packs[c] && packs[c].messages[key];
      if (value !== undefined) return [c, value];
    }
    return [BASE, key];
  }
  function plural(code, value, params) {
    if (!value || typeof value !== 'object') return value;
    let form = 'other';
    try { form = new Intl.PluralRules((packs[code] || {}).tag || code).select(Number(params.count ?? params.n)); } catch (_) {}
    return value[form] ?? value.other ?? '';
  }
  function format(text, params) {
    return String(text).replace(/\{(\w+)\}/g, (match, name) =>
      Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match);
  }
  function t(key, params = {}, zh) {
    const code = current();
    if (code === BASE && typeof zh === 'string') return zh;
    const [from, value] = lookup(code, key);
    if (from === BASE && typeof zh === 'string') return zh;
    return format(plural(from, value, params), params);
  }
  // 有些代码把固定的中文说法存进状态并按中文比较（如状态行的“处理中”）。显示时按中文参照包
  // 在同一前缀下反查键，再给出当前语言的文字；查不到的（用户或后端文字）原样返回。
  function label(text, prefix) {
    if (!text || current() === BASE) return text;
    const index = reverse[prefix] || (reverse[prefix] = Object.fromEntries(
      Object.entries((packs[BASE] || {}).messages || {})
        .filter(([key, value]) => key.startsWith(prefix) && typeof value === 'string')
        .map(([key, value]) => [value, key])));
    return index[text] ? t(index[text]) : text;
  }
  // 按中文原文查译文；中文界面或查不到时返回原文。原文里的 {name} 按 params 填入。
  function tx(zh, params = {}) {
    const code = current();
    let text = zh;
    if (code !== BASE) {
      for (const c of [code, packs[code] && packs[code].fallback].filter(Boolean)) {
        const value = packs[c] && packs[c].phrases[zh];
        if (value !== undefined) { text = plural(c, value, params); break; }
      }
    }
    return format(text, params);
  }
  // 刚插入的界面模板（尚未填入用户或后端数据）按原文就地翻译：文本节点与 aria-label、title、
  // placeholder。只认 phrases 里的整段原文，查不到的保持不变。记住每处的中文原文，同一棵树
  // 在语言切换后再调用一次即可换成新语言（含切回中文）。带 translate="no" 的子树（如终端输出）
  // 不处理。
  const textOrigin = new WeakMap(), attrOrigin = new WeakMap();
  const LOCALIZED_ATTRS = ['aria-label', 'title', 'placeholder'];
  function phraseIn(code, zh) {
    for (const c of [code, packs[code] && packs[code].fallback].filter(Boolean)) {
      if (packs[c] && typeof packs[c].phrases[zh] === 'string') return packs[c].phrases[zh];
    }
    return undefined;
  }
  function sourceOf(stored, value) {
    if (stored && (value === stored || Object.keys(packs).some((c) => phraseIn(c, stored) === value))) return stored;
    return value;
  }
  function localize(root) {
    if (!root || typeof document.createTreeWalker !== 'function') return root;
    const code = current();
    const target = (zh) => (code === BASE ? zh : (phraseIn(code, zh) ?? zh));
    const walker = document.createTreeWalker(root, 4 /* NodeFilter.SHOW_TEXT */, {
      acceptNode: (node) => (node.parentElement && node.parentElement.closest('[translate="no"]') ? 2 : 1),
    });
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const value = node.nodeValue.trim();
      if (!value) continue;
      const zh = sourceOf(textOrigin.get(node), value), text = target(zh);
      if (text !== zh || textOrigin.has(node)) textOrigin.set(node, zh);
      if (text !== value) node.nodeValue = node.nodeValue.replace(value, text);
    }
    const elements = [root, ...root.querySelectorAll('[aria-label],[title],[placeholder]')];
    for (const el of elements) {
      if (!el.getAttribute || (el.closest && el.closest('[translate="no"]'))) continue;
      const stored = attrOrigin.get(el) || {};
      for (const attr of LOCALIZED_ATTRS) {
        const value = el.getAttribute(attr);
        if (!value) continue;
        const zh = sourceOf(stored[attr], value), text = target(zh);
        if (text !== zh || stored[attr]) stored[attr] = zh;
        if (text !== value) el.setAttribute(attr, text);
      }
      if (Object.keys(stored).length) attrOrigin.set(el, stored);
    }
    return root;
  }
  function txh(zh, params = {}) {
    return tx(zh, params).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function paint(node) {
    const spec = JSON.parse(node.getAttribute('data-desk-i18n') || '{}');
    for (const [attr, [key, params, zh]] of Object.entries(spec)) {
      const text = key ? t(key, params, zh) : tx(zh, params);   // 空键表示按原文绑定
      if (attr === 'text') node.textContent = text;
      else if (ATTRS.includes(attr)) node.setAttribute(attr, text);
    }
  }
  function bind(node, key, params = {}, attr = 'text', zh) {
    if (!node) return;
    const spec = JSON.parse(node.getAttribute('data-desk-i18n') || '{}');
    spec[attr] = typeof zh === 'string' ? [key, params, zh] : [key, params];
    node.setAttribute('data-desk-i18n', JSON.stringify(spec));
    paint(node);
  }
  function bindText(node, zh, params = {}, attr = 'text') { bind(node, '', params, attr, zh); }
  // 用户内容或后端文字不随语言变化：先去掉该属性上较早的绑定，再写入。
  function plain(node, text, attr = 'text') {
    if (!node) return;
    const spec = JSON.parse(node.getAttribute('data-desk-i18n') || '{}');
    if (attr in spec) {
      delete spec[attr];
      if (Object.keys(spec).length) node.setAttribute('data-desk-i18n', JSON.stringify(spec));
      else node.removeAttribute('data-desk-i18n');
    }
    if (attr === 'text') node.textContent = text; else node.setAttribute(attr, text);
  }
  function apply(root = document) {
    root.querySelectorAll('[data-desk-key]').forEach(node => bind(node, node.getAttribute('data-desk-key')));
    root.querySelectorAll('[data-desk-i18n]').forEach(paint);
    document.documentElement.lang = (packs[current()] || {}).tag || 'zh-CN';
    root.querySelectorAll('[data-desk-language]').forEach(node => { node.value = current(); });
  }
  function setLocale(value) {
    requested = packs[value] ? value : BASE;
    try { localStorage.setItem('cd_locale', requested); } catch (_) {}
    apply();
    try { globalThis.dispatchEvent(new CustomEvent('desk-locale', { detail: { locale: current() } })); } catch (_) {}
  }
  function picker() {
    const label = document.createElement('label');
    const title = document.createElement('span'); bind(title, 'language');
    const select = document.createElement('select');
    select.setAttribute('data-desk-language', ''); select.setAttribute('aria-label', 'Language / 语言');
    for (const [code, pack] of Object.entries(packs)) {
      const option = document.createElement('option'); option.value = code; option.textContent = pack.label || code;
      select.appendChild(option);
    }
    select.value = current(); select.onchange = () => setLocale(select.value);
    label.append(title, select); return label;
  }
  return {
    t, tx, txh, localize, label, bind, bindText, plain, apply, setLocale, picker, register, packs,
    get locale() { return current(); },
    get tag() { return (packs[current()] || {}).tag || 'zh-CN'; },
  };
})();
// 同源内嵌页（文件浏览器）经 parent.DeskI18n 读取；顶层 const 不是 window 属性。
globalThis.DeskI18n = DeskI18n;
