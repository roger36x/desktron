const state = globalThis.__deskAgentControl || (globalThis.__deskAgentControl = (() => {
  const state = {seq: 0, rev: 1, nodes: new Map(), ids: new WeakMap(), observed: new WeakSet()};
  return state;
})());
const norm = (value, max = 500) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, max);
// 两条脱敏正则是唯一的密钥剔除口径，单行与保留换行两条清洗路径共用同一份。
const scrub = (value) => String(value)
  .replace(/\b(?:sk|pk|api|token)[-_][A-Za-z0-9_-]{16,}\b/gi, '[redacted]')
  .replace(/\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]{10,})?\b/g, '[redacted]');
const redact = (value, max = 20000) => scrub(norm(value, max));
const safeHref = (el) => {
  const raw = el?.getAttribute?.('href');
  if (!raw) return '';
  try {
    const u = new URL(String(raw), el.ownerDocument?.baseURI || location.href);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return '';
    if (u.username || u.password) return '';
    u.hash = '';
    const sensitiveKey = /^(?:access_token|api_key|auth|authorization|code|credential|id_token|jwt|key|oauth_token|password|refresh_token|secret|session|sessionid|signature|sig|state|ticket|token)$/i;
    if (Array.from(u.searchParams.keys()).some((key) => sensitiveKey.test(key))) return '';
    return u.toString().slice(0, 4096);
  } catch (_) { return ''; }
};
// 正文单独走保留换行的清洗：innerText 已按排版给出换行，norm 的 \s+ 会把标题和
// 正文压成一条长串。这里只压合行内空白与连续空行，不改 norm 的语义（它被多处复用）。
const redactBlock = (value, max = 20000) => scrub(String(value || '')
  .replace(/\r\n?/g, '\n').replace(/[^\S\n]+/g, ' ')
  .replace(/ ?\n ?/g, '\n').replace(/\n{3,}/g, '\n\n').trim().slice(0, max));
const observe = (root) => {
  if (!root || state.observed.has(root)) return;
  try {
    new MutationObserver(records => {
      if (records.some(r => r.type !== 'attributes'
        || !String(r.attributeName || '').startsWith('data-desk-network-'))) state.rev += 1;
    }).observe(root,
      {subtree: true, childList: true, attributes: true, characterData: true});
    state.observed.add(root);
  } catch (_) {}
};
observe(document);
const deepQuery = (selector, root = document) => {
  const hit = root.querySelector(selector); if (hit) return hit;
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) { const shadowHit = deepQuery(selector, el.shadowRoot); if (shadowHit) return shadowHit; }
    if (el.tagName === 'IFRAME') {
      try { const frameHit = deepQuery(selector, el.contentDocument); if (frameHit) return frameHit; } catch (_) {}
    }
  }
  return null;
};
const deepQueryAll = (selector, root = document, out = []) => {
  for (const hit of root.querySelectorAll(selector)) out.push(hit);
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) deepQueryAll(selector, el.shadowRoot, out);
    if (el.tagName === 'IFRAME') {
      try { if (el.contentDocument) deepQueryAll(selector, el.contentDocument, out); } catch (_) {}
    }
  }
  return [...new Set(out)];
};
const rendered = (el) => {
  if (!el || !el.isConnected) return false;
  const r = el.getBoundingClientRect();
  const s = (el.ownerDocument?.defaultView || window).getComputedStyle(el);
  return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
};
const inViewport = (el) => {
  if (!rendered(el)) return false;
  const r = absoluteRect(el);
  return r.bottom >= 0 && r.right >= 0 && r.top <= innerHeight && r.left <= innerWidth;
};
const sensitive = (el) => {
  const text = [el.type, el.name, el.id, el.autocomplete, el.getAttribute?.('aria-label')].join(' ');
  return el.type === 'password' || /password|passwd|secret|token|cookie|credential|authorization/i.test(text);
};
const nodeId = (el) => {
  let id = state.ids.get(el);
  if (!id) { id = 'n' + (++state.seq); state.ids.set(el, id); state.nodes.set(id, el); }
  return id;
};
const version = () => String(epoch) + ':' + String(state.rev);
const absoluteRect = (el) => {
  const r = el.getBoundingClientRect();
  let x = r.x, y = r.y, win = el.ownerDocument?.defaultView;
  while (win && win !== window) {
    const frame = win.frameElement;
    if (!frame) break;
    const fr = frame.getBoundingClientRect(); x += fr.x; y += fr.y; win = frame.ownerDocument?.defaultView;
  }
  return {x, y, left: x, top: y, right: x + r.width, bottom: y + r.height,
    width: r.width, height: r.height};
};
const classHasState = (el, stateName) => {
  const tokens = String(el?.className || '').toLowerCase().split(/\s+/);
  return tokens.some(token => token === stateName || token === 'is-' + stateName
    || token.endsWith('-' + stateName));
};
// 自定义组件通常把隐藏 input 放在 label 内，并把状态镜像到 aria/class。
// 这里只读浏览器可验证的关联关系，不执行站点脚本，也不改 DOM。
const checkControl = (el) => {
  if (!el) return null;
  const native = el.matches?.('input[type=checkbox],input[type=radio]') ? el : null;
  const label = el.tagName === 'LABEL' ? el : el.closest?.('label');
  const associated = native || label?.control
    || label?.querySelector?.('input[type=checkbox],input[type=radio]') || null;
  const surface = label || el;
  const explicitRole = norm(el.getAttribute?.('role') || surface.getAttribute?.('role')).toLowerCase();
  const role = associated?.type === 'radio' || explicitRole === 'radio' ? 'radio'
    : associated?.type === 'checkbox' || explicitRole === 'checkbox' ? 'checkbox' : '';
  if (!role) return null;
  const ariaChecked = el.getAttribute?.('aria-checked') ?? surface.getAttribute?.('aria-checked')
    ?? associated?.getAttribute?.('aria-checked');
  let checked = null, mixed = false, checkedSource = '';
  if (associated && 'checked' in associated) {
    checked = !!associated.checked; mixed = !!associated.indeterminate;
    checkedSource = native ? 'native_control' : 'associated_control';
  } else if (ariaChecked !== null && ariaChecked !== undefined) {
    mixed = ariaChecked === 'mixed'; checked = ariaChecked === 'true'; checkedSource = 'aria';
  } else if (classHasState(surface, 'checked') || classHasState(surface, 'indeterminate')) {
    mixed = classHasState(surface, 'indeterminate'); checked = !mixed; checkedSource = 'class';
  }
  const disabled = !!associated?.disabled
    || el.getAttribute?.('aria-disabled') === 'true' || surface.getAttribute?.('aria-disabled') === 'true'
    || classHasState(surface, 'disabled');
  const disabledSource = associated?.disabled ? (native ? 'native_control' : 'associated_control')
    : (el.getAttribute?.('aria-disabled') !== null || surface.getAttribute?.('aria-disabled') !== null)
    ? 'aria' : classHasState(surface, 'disabled') ? 'class' : '';
  return {role, checked, mixed, disabled, surface, control: associated,
    sources: {checked: checkedSource, disabled: disabledSource}};
};
const enabled = (el) => !checkControl(el)?.disabled
  && !el.matches?.(':disabled,[aria-disabled=true]');
// HTML-AAM 的常规映射。文档结构元素以前一律返回空角色，正文、列表、表格因此
// 完全进不了节点树。取舍：
// - 纯排版行内元素（strong/em/code/sub/sup/time/span）仍不给角色。它们数量极大，
//   内容已由 text 字段带出，进树只会挤占节点预算。
// - figcaption 在 HTML-AAM 里无对应角色，这里按 caption 收录，让图注单独成节点。
// - pre 在 HTML-AAM 里是 generic，这里按 code 收录，让代码块单独成节点。
// - canvas/video/audio/embed/object/iframe 在 HTML-AAM 里也无对应角色。它们的内部
//   本就没有可访问 DOM，但"此处有一块 640x400 的视觉内容"这个事实必须可见，
//   否则 agent 会以为这一页是空的，不会想到改用截图。角色名沿用浏览器 AX 树的叫法。
const DOC_ROLES = {p: 'paragraph', ul: 'list', ol: 'list', menu: 'list', dl: 'list',
  li: 'listitem', dt: 'term', dd: 'definition', table: 'table', thead: 'rowgroup',
  tbody: 'rowgroup', tfoot: 'rowgroup', tr: 'row', td: 'cell', caption: 'caption',
  figcaption: 'caption', article: 'article', aside: 'complementary', main: 'main',
  nav: 'navigation', search: 'search', blockquote: 'blockquote', figure: 'figure',
  hr: 'separator', pre: 'code', details: 'group', fieldset: 'group', optgroup: 'group',
  option: 'option', output: 'status', progress: 'progressbar', meter: 'meter',
  dialog: 'dialog', svg: 'graphics-document', canvas: 'img', video: 'video',
  audio: 'audio', embed: 'embed', object: 'embed', iframe: 'iframe'};
// aria-label / aria-labelledby / title 给出的名字。section、form 依 HTML-AAM 只有
// 带可访问名时才是 region / form，而这里的"名"不看正文，否则每个 section 都成 region。
const ariaName = (el) => {
  const labelled = norm(el.getAttribute?.('aria-labelledby') || '');
  if (labelled) {
    const doc = el.ownerDocument || document;
    const text = norm(labelled.split(/\s+/).map(id => doc.getElementById(id)?.textContent || '').join(' '));
    if (text) return text;
  }
  const svgTitle = el.tagName === 'svg'
    ? Array.from(el.children || []).find(child => child.tagName === 'title')?.textContent : '';
  return norm(el.getAttribute?.('aria-label') || el.getAttribute?.('alt')
    || el.getAttribute?.('title') || svgTitle || el.getAttribute?.('placeholder')
    || el.getAttribute?.('name') || '');
};
// header/footer 只在不位于 article/aside/main/nav/section 内时才是 banner/contentinfo；
// th 的行列表头按 scope 判断，缺 scope 时按所在位置从简推断。
const contextualRole = (el, tag) => {
  if (tag === 'section') return ariaName(el) ? 'region' : '';
  if (tag === 'form') return ariaName(el) ? 'form' : '';
  if (tag === 'header' || tag === 'footer')
    return el.closest?.('article,aside,main,nav,section') ? '' : (tag === 'header' ? 'banner' : 'contentinfo');
  if (tag === 'th') {
    const scope = String(el.getAttribute('scope') || '').toLowerCase();
    if (scope === 'row' || scope === 'rowgroup') return 'rowheader';
    if (scope === 'col' || scope === 'colgroup') return 'columnheader';
    if (el.closest?.('thead')) return 'columnheader';
    const row = el.parentElement;
    return row?.firstElementChild === el && row.querySelector?.('td') ? 'rowheader' : 'columnheader';
  }
  return '';
};
const implicitRole = (el) => {
  const tag = el.tagName?.toLowerCase() || '';
  if (tag === 'a' && el.hasAttribute('href')) return 'link';
  if (tag === 'button') return 'button';
  if (tag === 'textarea') return 'textbox';
  if (tag === 'select') return el.multiple ? 'listbox' : 'combobox';
  if (tag === 'img') return 'img';
  if (/^h[1-6]$/.test(tag)) return 'heading';
  if (tag === 'input') {
    const t = String(el.type || 'text').toLowerCase();
    if (['button','submit','reset'].includes(t)) return 'button';
    if (t === 'checkbox') return 'checkbox';
    if (t === 'radio') return 'radio';
    if (t === 'range') return 'slider';
    return t === 'hidden' ? '' : 'textbox';
  }
  if (tag === 'label') return checkControl(el)?.role || '';
  return DOC_ROLES[tag] || contextualRole(el, tag);
};
const accessibleName = (el) => ariaName(el) || norm(el.innerText || el.textContent || '');
const locatorMatches = (el, locator) => {
  const exact = !!locator.exact;
  const same = (actual, wanted) => exact ? actual === wanted : actual.includes(wanted);
  if (locator.role && String(el.getAttribute?.('role') || implicitRole(el)).toLowerCase()
      !== String(locator.role).toLowerCase()) return false;
  if (locator.name && !same(accessibleName(el).toLowerCase(), norm(locator.name, 1000).toLowerCase())) return false;
  if (locator.text && !same(norm(el.innerText || el.textContent || '', 20000).toLowerCase(),
                            norm(locator.text, 1000).toLowerCase())) return false;
  if (locator.test_id) {
    const id = norm(el.getAttribute?.('data-testid') || el.getAttribute?.('data-test-id')
      || el.getAttribute?.('data-test') || '', 1000);
    if (!same(id, norm(locator.test_id, 1000))) return false;
  }
  return true;
};
const locate = (locator) => {
  let candidates;
  try { candidates = deepQueryAll(String(locator.selector || '*')); }
  catch (_) { return {error: 'invalid_selector', matches: []}; }
  candidates = candidates.filter(el => locatorMatches(el, locator)
    && (locator.visible === undefined || rendered(el) === locator.visible));
  // 纯文本定位优先最深的命中，避免 body/容器/按钮同时包含同一段文字。
  if (locator.text && !locator.role && !locator.name && !locator.test_id) {
    candidates = candidates.filter(el => !candidates.some(other => other !== el && el.contains?.(other)));
  }
  if (Number.isInteger(locator.nth)) {
    const at = locator.nth < 0 ? candidates.length + locator.nth : locator.nth;
    candidates = at >= 0 && at < candidates.length ? [candidates[at]] : [];
  }
  return {matches: candidates};
};
// 文档结构角色让正文、列表、表格进入节点树。纯交互观察（如动作后的弹窗探测）
// 不需要这些节点，可用 snapshot({structure: false}) 关掉，保持原来的口径。
const STRUCTURE_ROLES = new Set(['paragraph', 'list', 'listitem', 'term', 'definition',
  'table', 'rowgroup', 'row', 'cell', 'columnheader', 'rowheader', 'caption', 'article',
  'complementary', 'main', 'navigation', 'search', 'region', 'banner', 'contentinfo',
  'blockquote', 'figure', 'group', 'form', 'separator', 'code', 'status']);
// 这些角色的正文一定由子节点重新带出，节点自身只留一段摘要，避免 article/table
// 把整篇内容在每一层重复一遍。paragraph、cell、code 这类内容落脚点不在其中。
const SUMMARY_TEXT_ROLES = new Set(['list', 'table', 'rowgroup', 'row', 'article',
  'complementary', 'main', 'navigation', 'search', 'region', 'banner', 'contentinfo',
  'figure', 'group', 'form', 'document']);
// 这些角色的名字只认 aria，不认内容：否则整段正文会在 name 与 text 里重复一遍。
const ARIA_ONLY_NAME = new Set(['paragraph', 'list', 'table', 'rowgroup', 'row', 'article',
  'complementary', 'main', 'navigation', 'search', 'region', 'banner', 'contentinfo',
  'blockquote', 'figure', 'group', 'form', 'separator', 'code', 'graphics-document',
  'video', 'audio', 'embed', 'iframe', 'document']);
const semantic = (el, structure = true) => {
  const explicit = norm(el.getAttribute?.('role') || '');
  const role = explicit || implicitRole(el);
  if (role && (structure || explicit || !STRUCTURE_ROLES.has(role.toLowerCase()))) return true;
  return el.matches?.('[tabindex],summary,dialog,[aria-modal=true],[contenteditable=true],[onclick]')
    || (rendered(el) && (el.ownerDocument?.defaultView || window).getComputedStyle(el).cursor === 'pointer'
        && !Array.from(el.children || []).some(child => rendered(child)
          && (child.ownerDocument?.defaultView || window).getComputedStyle(child).cursor === 'pointer'));
};
// 纯包装容器折叠：隐式结构容器若没有可访问名、没有直接文本、且最多只有一个渲染
// 子元素，就不单独建节点，子节点直接挂到祖先上。可访问性树对 generic 容器就是
// 这么做的，避免 li > div > span > a 这类多层包装各占一个节点把预算吃光。
// 带显式 role 属性的元素一律保留，不折叠，以免改变改动前的可见节点集合。
const WRAPPER_ROLES = new Set(['paragraph', 'list', 'listitem', 'term', 'definition',
  'rowgroup', 'region', 'group', 'article', 'complementary', 'banner', 'contentinfo',
  'figure', 'blockquote', 'navigation', 'main', 'search', 'form', 'caption']);
const directText = (el) => Array.from(el.childNodes || [])
  .filter(node => node.nodeType === 3).map(node => node.textContent).join('').trim();
const wrapperOnly = (el) => {
  if (norm(el.getAttribute?.('role') || '')) return false;
  if (!WRAPPER_ROLES.has(implicitRole(el))) return false;
  if (ariaName(el) || directText(el)) return false;
  return Array.from(el.children || []).filter(rendered).length <= 1;
};
// aria-hidden 的子树不进可访问性树。站点里的装饰性图标（尤其是 svg）大量带这个
// 属性，放进来只会淹没有效节点。但也有站点把可交互元素误标 aria-hidden，因此
// 子树里还有可聚焦或带点击处理的元素时照常下探。
const ariaHidden = (el) => el.getAttribute?.('aria-hidden') === 'true'
  && !el.matches?.('a[href],button,input,select,textarea,[tabindex],[onclick]')
  && !el.querySelector?.('a[href],button,input,select,textarea,[tabindex],[onclick]')
  && !checkControl(el);
// svg 内部可能有上千个 path/g。svg 本身进树即可，除非内部带显式 role 或 aria 标注，
// 否则不再下探；canvas 内部没有可访问 DOM，同样不下探。
const opaqueSubtree = (el) => (el.tagName === 'svg' || el.tagName === 'CANVAS')
  && !el.querySelector?.('[role],[aria-label],[aria-labelledby]');
const selectedValues = (el) => {
  if (el instanceof HTMLSelectElement) return Array.from(el.selectedOptions || [])
    .map(option => String(option.value || norm(option.label || option.textContent || '', 1000)));
  const role = norm(el.getAttribute?.('role') || implicitRole(el)).toLowerCase();
  if (role !== 'combobox' && role !== 'listbox') return null;
  let root = el;
  const controls = norm(el.getAttribute?.('aria-controls') || '');
  if (controls) root = el.ownerDocument?.getElementById(controls) || root;
  const options = Array.from(root.querySelectorAll?.('[role=option]') || [])
    .filter(option => option.getAttribute('aria-selected') === 'true');
  if (options.length) return options.map(option => norm(option.getAttribute('data-value')
    || option.getAttribute('value') || accessibleName(option), 1000));
  const value = sensitive(el) ? '' : norm(el.value || '', 1000);
  return value ? [value] : [];
};
const handoffState = () => {
  const current = deepQueryAll('[aria-current=step],[role=tab][aria-selected=true]')
    .find(rendered) || null;
  const fields = deepQueryAll('input,textarea,select').filter(el => !sensitive(el));
  const dirty = fields.some(el => el instanceof HTMLSelectElement
    ? Array.from(el.options).some(option => option.selected !== option.defaultSelected)
    : el.matches?.('input[type=checkbox],input[type=radio]')
    ? !!el.checked !== !!el.defaultChecked
    : String(el.value ?? '') !== String(el.defaultValue ?? ''));
  return {current_step: current ? accessibleName(current) : '', unsaved: dirty,
    submission_status: 'unknown', ticket_id: '', basis: 'dom_form_state'};
};
const frameName = (frame, index) => norm(frame.getAttribute('title') || frame.name || frame.id || ('frame-' + index));
const snapshot = (input = {}) => {
  // 结构元素进树后同一页面的节点数会显著上升（长文尤其明显），默认与上限相应放宽。
  const limit = Math.max(1, Math.min(Number(input.max_nodes || 1500), 4000));
  const includeOffscreen = !!input.include_offscreen;
  const structure = input.structure !== false;
  const rootNode = input.root_node_id ? resolve(input.root_node_id) : null;
  if (input.root_node_id && !rootNode) return {error: 'stale_or_missing_root'};
  const nodes = [];
  const opaqueFrames = [];
  const texts = [];
  const visit = (root, parentId = '', depth = 0, frameId = 'top', shadow = false) => {
    if (!root || nodes.length >= limit) return;
    observe(root);
    const list = Array.from(root.children || root.childNodes || []);
    let frameIndex = 0;
    for (const el of list) {
      if (!(el instanceof Element) || nodes.length >= limit || ariaHidden(el)) continue;
      // depth 与 parent_id 同口径：只在真正建节点时加一层，未收录的包装元素不占层级，
      // 否则缩进读起来与父子关系对不上。
      let nextParent = parentId, nextDepth = depth;
      if (rendered(el) && (includeOffscreen || inViewport(el))
          && semantic(el, structure) && !wrapperOnly(el)) {
        const id = String(epoch) + ':' + nodeId(el);
        const r = absoluteRect(el), secret = sensitive(el);
        const role = norm(el.getAttribute('role') || implicitRole(el));
        const states = {};
        const stateSources = {};
        for (const key of ['expanded','selected','pressed','required','invalid','readonly','busy','current']) {
          const value = el.getAttribute('aria-' + key);
          if (value !== null) states[key] = value;
        }
        if ('disabled' in el) states.disabled = !!el.disabled;
        if ('checked' in el) states.checked = !!el.checked;
        const check = checkControl(el);
        if (check) {
          if (check.checked !== null) states.checked = check.checked;
          if (check.mixed) states.mixed = true;
          states.disabled = check.disabled;
          if (check.sources.checked) stateSources.checked = check.sources.checked;
          if (check.sources.disabled) stateSources.disabled = check.sources.disabled;
        }
        const selected = selectedValues(el);
        const href = el.tagName === 'A' && role.toLowerCase() === 'link' ? safeHref(el) : '';
        nodes.push({node_id: id, parent_id: parentId, depth, frame_id: frameId,
          shadow_root: shadow, tag: el.tagName.toLowerCase(), role,
          name: ARIA_ONLY_NAME.has(role.toLowerCase()) ? ariaName(el) : accessibleName(el),
          type: norm(el.getAttribute('type') || ''),
          ...(href ? {href} : {}),
          // 容器节点的正文已经由子节点与顶层 text 字段带出，这里只留一段摘要，
          // 避免 article/section 把整篇文章在每一层重复一遍。
          text: redact(el.innerText || el.textContent || '',
            SUMMARY_TEXT_ROLES.has(role.toLowerCase()) && el.childElementCount > 0 ? 2000 : 20000),
          value: secret ? '[redacted]' : norm(el.value || '', 1000),
          states, ...(Object.keys(stateSources).length ? {state_sources: stateSources} : {}),
          ...(selected === null ? {} : {selected_values: selected}),
          sensitive: secret, visible: rendered(el), in_viewport: inViewport(el),
          needs_user: el.matches('input[type=file]') ? 'file_upload' : '',
          rect: {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)}});
        nextParent = id; nextDepth = depth + 1;
      }
      if (opaqueSubtree(el)) continue;
      if (el.shadowRoot) visit(el.shadowRoot, nextParent, nextDepth, frameId, true);
      if (el.tagName === 'IFRAME') {
        frameIndex += 1;
        const childFrameId = frameId + '/' + frameName(el, frameIndex);
        try {
          const doc = el.contentDocument;
          if (doc?.documentElement) visit(doc.documentElement, nextParent, nextDepth, childFrameId, false);
          else opaqueFrames.push({frame_id: childFrameId, reason: 'unavailable'});
        } catch (_) { opaqueFrames.push({frame_id: childFrameId, reason: 'cross_origin'}); }
      }
      visit(el, nextParent, nextDepth, frameId, shadow);
    }
  };
  visit(rootNode || document.documentElement);
  try { texts.push(document.body?.innerText || ''); } catch (_) {}
  let filtered = nodes;
  const wanted = norm(input.find || '', 1000).toLowerCase();
  if (wanted) {
    const matches = new Set(nodes.filter(n => [n.name,n.text,n.value,n.role,n.tag]
      .some(v => String(v || '').toLowerCase().includes(wanted))).map(n => n.node_id));
    const parents = new Map(nodes.map(n => [n.node_id, n.parent_id]));
    for (const id of [...matches]) { let p = parents.get(id); while (p) { matches.add(p); p = parents.get(p); } }
    filtered = nodes.filter(n => matches.has(n.node_id));
  }
  const stackZ = (el) => {
    const raw = (el.ownerDocument?.defaultView || window).getComputedStyle(el).zIndex;
    const value = Number(raw); return Number.isFinite(value) ? value : 0;
  };
  const modals = nodes.filter(n => n.role === 'dialog' || n.role === 'alertdialog'
    || n.tag === 'dialog').map(n => ({...n,
      z_index: stackZ(state.nodes.get(String(n.node_id).split(':')[1]))}))
    .sort((a, b) => a.z_index - b.z_index).map((n, index) => ({...n, stack_index: index}));
  const overlayStack = (kind) => deepQueryAll('*').filter(rendered).filter(el => {
    const role = norm(el.getAttribute?.('role') || implicitRole(el)).toLowerCase();
    const classes = String(el.className || '').toLowerCase();
    return kind === 'menu' ? ['menu','listbox','tree'].includes(role)
      : role === 'tooltip' || /(?:^|[-_])(popover|popup|dropdown|trigger-popup)(?:$|[-_])/.test(classes);
  }).sort((a, b) => stackZ(a) - stackZ(b)).slice(0, 40).map((el, index) => {
    const r = absoluteRect(el);
    return {node_id: String(epoch) + ':' + nodeId(el), stack_index: index,
      z_index: stackZ(el),
      role: norm(el.getAttribute?.('role') || implicitRole(el)), name: redact(accessibleName(el)),
      rect: {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)}};
  });
  const menus = overlayStack('menu'), popovers = overlayStack('popover');
  return {schema: 'desk.browser.document/v1', observation_version: version(),
    url: String(location.href), title: String(document.title || ''),
    handoff: handoffState(),
    viewport: {width: innerWidth, height: innerHeight, scroll_x: scrollX, scroll_y: scrollY},
    text: redactBlock(texts.join('\n')), nodes: filtered, total_nodes: nodes.length,
    truncated: nodes.length >= limit, modal_stack: modals, menu_stack: menus,
    popover_stack: popovers, opaque_frames: opaqueFrames,
    capabilities: {open_shadow_roots: 'readable', same_origin_frames: 'readable',
      cross_origin_frames: 'opaque', javascript_dialogs: 'needs_user',
      file_upload: 'needs_user', downloads: 'needs_user',
      locators: ['selector','role','name','text','test_id'],
      pointer_input: 'native', network_idle: 'fetch_xhr_and_dom_quiet'}};
};
const resolve = (raw) => {
  const parts = String(raw || '').split(':');
  if (parts.length !== 2 || Number(parts[0]) !== Number(epoch)) return null;
  const el = state.nodes.get(parts[1]);
  return el && el.isConnected ? el : null;
};
// 只在隔离世界保留完整比对材料，回执仅返回不透明凭据。任何不完整/敏感上下文
// 都不提供自动续办；不截断后比较，以免漏掉屏幕外的实质变化。
const approvalSignature = () => {
  const elements = deepQueryAll('*');
  if (elements.length > 4000 || elements.some(el => sensitive(el)
      || el.tagName === 'IFRAME' || el.shadowRoot)) return null;
  const facts = elements.filter(el => !['SCRIPT','STYLE','NOSCRIPT'].includes(el.tagName)).map(el => [
    el.tagName, Array.from(el.attributes || []).filter(a => a.name !== 'style'
      && !a.name.startsWith('data-desk-network-')).map(a => [a.name, a.value]),
    rendered(el), 'value' in el ? el.value : '', 'checked' in el ? el.checked : null,
    'selected' in el ? el.selected : null]);
  const value = JSON.stringify([String(location.href), document.title, document.body?.innerText || '', facts]);
  return value.length <= 500000 ? value : null;
};
const rememberApproval = (output) => {
  if (output.truncated || output.opaque_frames.length) return;
  const signature = approvalSignature();
  if (signature === null) return;
  state.guardSeq = (state.guardSeq || 0) + 1;
  const token = String(epoch) + ':' + state.guardSeq;
  const guards = state.approvalGuards || (state.approvalGuards = new Map());
  guards.set(token, {version: output.observation_version, signature,
    // 文本 locator 可能命中按钮内的 span；它没有独立语义节点，但仍须保留原身份。
    elements: new Set(deepQueryAll('*')), doc: document});
  while (guards.size > 8) guards.delete(guards.keys().next().value);
  output.approval_guard = {token, version: output.observation_version};
};
const verifyApproval = (el, proof) => {
  if (!proof) return '';
  const prior = state.approvalGuards?.get(proof.token);
  if (!prior || prior.doc !== document || prior.version !== proof.version)
    return 'approval_revalidation_unavailable';
  if (!prior.elements.has(el) || prior.signature !== approvalSignature())
    return 'approval_target_changed';
  return '';
};
const fire = (el, type) => el.dispatchEvent(new Event(type, {bubbles: true, composed: true}));
const networkActivity = () => {
  let pending = 0, last = 0, trackedFrames = 0, opaqueFrames = 0;
  const visit = (doc) => {
    const root = doc?.documentElement; if (!root) return;
    const rawPending = root.getAttribute('data-desk-network-pending');
    const rawLast = root.getAttribute('data-desk-network-last');
    if (rawPending !== null) { pending += Number(rawPending || 0); trackedFrames += 1; }
    last = Math.max(last, Number(rawLast || 0));
    for (const frame of doc.querySelectorAll('iframe')) {
      try { if (frame.contentDocument) visit(frame.contentDocument); else opaqueFrames += 1; }
      catch (_) { opaqueFrames += 1; }
    }
  };
  visit(document);
  return {pending, last, tracked_frames: trackedFrames, opaque_frames: opaqueFrames};
};
const safeResourceUrl = (raw) => {
  try { const u = new URL(String(raw || ''), location.href); return u.origin + u.pathname; }
  catch (_) { return ''; }
};
const networkSnapshot = (input = {}) => {
  const limit = Math.max(1, Math.min(Number(input.limit || 200), 1000));
  const since = Math.max(0, Number(input.since_ms || 0));
  const initiator = norm(input.initiator || '', 100).toLowerCase();
  const wanted = norm(input.url_contains || '', 1000).toLowerCase();
  const entries = [], opaqueFrames = [];
  const visit = (doc, frameId = 'top') => {
    if (!doc || entries.length >= limit) return;
    const win = doc.defaultView; if (!win) return;
    let resources = [];
    try { resources = win.performance?.getEntriesByType?.('resource') || []; } catch (_) {}
    for (const row of resources) {
      const url = safeResourceUrl(row.name);
      const kind = norm(row.initiatorType || 'other', 100).toLowerCase();
      if (Number(row.startTime || 0) < since || (initiator && kind !== initiator)
          || (wanted && !url.toLowerCase().includes(wanted))) continue;
      entries.push({frame_id: frameId, url, initiator: kind,
        start_ms: Math.round(Number(row.startTime || 0)),
        duration_ms: Math.round(Number(row.duration || 0)),
        response_end_ms: Math.round(Number(row.responseEnd || 0)),
        transfer_size: Number(row.transferSize || 0),
        encoded_body_size: Number(row.encodedBodySize || 0),
        decoded_body_size: Number(row.decodedBodySize || 0),
        protocol: norm(row.nextHopProtocol || '', 100),
        ...(Number(row.responseStatus || 0) > 0 ? {status: Number(row.responseStatus)} : {})});
      if (entries.length >= limit) break;
    }
    let index = 0;
    for (const frame of doc.querySelectorAll('iframe')) {
      index += 1; const childId = frameId + '/' + frameName(frame, index);
      try {
        if (frame.contentDocument?.documentElement) visit(frame.contentDocument, childId);
        else opaqueFrames.push({frame_id: childId, reason: 'unavailable'});
      } catch (_) { opaqueFrames.push({frame_id: childId, reason: 'cross_origin'}); }
    }
  };
  visit(document);
  return {schema: 'desk.browser.network/v1', url: safeResourceUrl(location.href),
    captured_at_ms: Math.round(performance.now()), entries, truncated: entries.length >= limit,
    opaque_frames: opaqueFrames, redaction: 'query_fragment_headers_bodies_omitted',
    capabilities: {resource_timing: 'available', same_origin_frames: 'readable',
      cross_origin_frames: 'opaque', request_headers: 'unavailable', response_bodies: 'unavailable'}};
};
if (command.kind === 'inspect' && command.name === 'document') {
  const output = snapshot(command.input || {});
  if (output.error) return {ok: false, error: output.error, observation_version: version()};
  rememberApproval(output);
  return {ok: true, output, observation_version: output.observation_version};
}
if (command.kind === 'inspect' && command.name === 'network') {
  return {ok: true, output: networkSnapshot(command.input || {}), observation_version: version()};
}
const input = command.input || {};
if (command.name === 'wait') {
  const delay = Math.max(0, Math.min(Number(input.milliseconds ?? 0), 10000));
  const wanted = norm(input.text || '', 1000);
  const selector = norm(input.selector || '', 1000);
  const locator = input.locator && typeof input.locator === 'object' ? input.locator : null;
  const targetState = String(input.state || 'attached');
  const targetSpecified = !!selector || !!locator;
  const wantedUrl = String(input.url || '');
  const mode = String(input.url_mode || 'contains');
  const timeout = Math.max(100, Math.min(Number(input.timeout_ms || 10000), 60000));
  const started = Date.now(), deadline = started + Math.max(delay, timeout);
  let stableRev = state.rev, stableSince = Date.now();
  do {
    const now = Date.now();
    if (input.guard_url && String(location.href) !== input.guard_url)
      return {ok: false, error: 'resource_changed', observation_version: version()};
    if (input.abort_if) {
      let failed;
      try { failed = deepQuery(input.abort_if); } catch (_) {
        return {ok: false, error: 'invalid_selector', observation_version: version()};
      }
      if (rendered(failed)) return {ok: false, error: 'wait_aborted',
        output: {reason: 'failure_condition_visible'}, observation_version: version()};
    }
    if (state.rev !== stableRev) { stableRev = state.rev; stableSince = now; }
    let target = null;
    if (locator) {
      const found = locate(locator);
      if (found.error) return {ok: false, error: found.error, observation_version: version()};
      if (found.matches.length > 1) return {ok: false, error: 'locator_ambiguous',
        output: {matches: found.matches.length}, observation_version: version()};
      target = found.matches[0] || null;
    } else if (selector) {
      try { target = deepQuery(selector); } catch (_) {
        return {ok: false, error: 'invalid_selector', observation_version: version()};
      }
    }
    const targetEnabled = !!target && enabled(target);
    const control = target ? checkControl(target) : null;
    const values = target ? selectedValues(target) : null;
    const actualValue = target && !sensitive(target) ? norm(target.value || '', 1000) : '';
    const targetMatched = !targetSpecified ? true
      : targetState === 'detached' ? !target
      : targetState === 'visible' ? rendered(target)
      : targetState === 'hidden' ? (!target || !rendered(target))
      : targetState === 'enabled' ? (rendered(target) && targetEnabled)
      : targetState === 'disabled' ? (!!target && !targetEnabled)
      : targetState === 'checked' ? (rendered(target) && control?.checked === true)
      : targetState === 'unchecked' ? (rendered(target) && control?.checked === false)
      : targetState === 'mixed' ? (rendered(target) && control?.mixed === true)
      : !!target;
    const valueMatched = input.value === undefined || (target && actualValue === String(input.value));
    const valuesMatched = !Array.isArray(input.values) || (target && Array.isArray(values)
      && input.values.length === values.length
      && input.values.every(value => values.includes(String(value))));
    const url = String(location.href);
    const urlMatched = !wantedUrl || (mode === 'equals' ? url === wantedUrl
      : mode === 'prefix' ? url.startsWith(wantedUrl) : url.includes(wantedUrl));
    const textMatched = !wanted || norm(document.body?.innerText || '', 20000).includes(wanted);
    const network = networkActivity();
    const idleMatched = !input.network_idle
      || (document.readyState === 'complete' && network.pending === 0
          && now - Math.max(stableSince, network.last || 0) >= 500);
    const delayMatched = !delay || now - started >= delay;
    if (targetMatched && valueMatched && valuesMatched && urlMatched && textMatched
        && idleMatched && delayMatched) {
      return {ok: true, output: {text: wanted, selector, target_state: targetState,
        target_matched: targetMatched, value_matched: valueMatched,
        selected_values_matched: valuesMatched, url, network_idle: idleMatched,
        ...(target && !sensitive(target) ? {target_value: actualValue} : {}),
        ...(Array.isArray(values) ? {selected_values: values} : {}),
        network_pending: network.pending, network_tracked_frames: network.tracked_frames,
        network_opaque_frames: network.opaque_frames,
        network_idle_basis: input.network_idle ? 'fetch_xhr_idle_and_dom_quiet_500ms' : ''},
        observation_version: version()};
    }
    await new Promise(resolve => setTimeout(resolve, 200));
  } while (Date.now() < deadline);
  return {ok: false, error: 'wait_timeout', observation_version: version()};
}
const deepElementFromPoint = (doc, x, y) => {
  let el = doc.elementFromPoint(x, y);
  if (!el) return null;
  if (el.shadowRoot) el = el.shadowRoot.elementFromPoint(x, y) || el;
  if (el.tagName === 'IFRAME') {
    try { const r = el.getBoundingClientRect(); return deepElementFromPoint(el.contentDocument, x-r.x, y-r.y) || el; }
    catch (_) { return el; }
  }
  return el;
};
const composedDescendant = (node, ancestor) => {
  for (let at = node; at; at = at.assignedSlot || at.parentNode || at.host) if (at === ancestor) return true;
  return false;
};
const findTarget = (spec = input) => {
  if (spec.node_id) {
    const node = resolve(spec.node_id);
    return node ? {element: node} : {error: 'stale_or_missing_node'};
  }
  if (spec.locator) {
    // 动作默认只匹配已渲染元素。等待 attached/hidden 仍使用 locate 原始语义。
    const result = locate({visible: true, ...spec.locator});
    if (result.error) return result;
    if (result.matches.length > 1) return {error: 'locator_ambiguous', count: result.matches.length};
    return result.matches.length ? {element: result.matches[0]} : {error: 'locator_not_found'};
  }
  if (spec.selector) {
    try {
      const matches = deepQueryAll(String(spec.selector)).filter(rendered);
      if (matches.length > 1) return {error: 'locator_ambiguous', count: matches.length};
      return matches.length ? {element: matches[0]} : {error: 'selector_not_found'};
    }
    catch (_) { return {error: 'invalid_selector'}; }
  }
  return {element: null};
};
const waitForTarget = async (spec, required, actionable) => {
  const timeout = Math.max(100, Math.min(Number(spec.timeout_ms || input.timeout_ms || 5000), 60000));
  const started = Date.now(), deadline = started + timeout;
  let lastError = required ? 'target_not_found' : '';
  let attempts = 0, lastRect = null, hitTag = '', candidates = [], occluder = null;
  do {
    attempts += 1;
    const found = findTarget(spec);
    if (found.error === 'invalid_selector' || found.error === 'stale_or_missing_node'
        || found.error === 'locator_ambiguous') return found;
    const target = found.element;
    if (!target) { if (!required) return {element: null}; lastError = found.error || lastError; }
    else if (!rendered(target)) lastError = 'element_not_visible';
    else if (!enabled(target)) lastError = 'element_disabled';
    else {
      if (!actionable) return {element: target};
      try { target.scrollIntoView?.({block: 'center', inline: 'nearest'}); } catch (_) {}
      const first = absoluteRect(target);
      await new Promise(resolve => setTimeout(resolve, 100));
      const second = absoluteRect(target);
      lastRect = {x: Math.round(second.x), y: Math.round(second.y),
        width: Math.round(second.width), height: Math.round(second.height)};
      if (Math.abs(first.x-second.x) > 0.5 || Math.abs(first.y-second.y) > 0.5
          || Math.abs(first.width-second.width) > 0.5 || Math.abs(first.height-second.height) > 0.5) {
        lastError = 'element_not_stable';
      } else {
        const left = Math.max(0, second.left), right = Math.min(innerWidth, second.right);
        const top = Math.max(0, second.top), bottom = Math.min(innerHeight, second.bottom);
        candidates = [];
        for (const ry of [0.5, 0.25, 0.75]) for (const rx of [0.5, 0.2, 0.8]) {
          const x = left + (right-left) * rx, y = top + (bottom-top) * ry;
          if (right <= left || bottom <= top) continue;
          const hit = deepElementFromPoint(document, x, y);
          hitTag = String(hit?.tagName || '').toLowerCase();
          candidates.push({x: Math.round(x), y: Math.round(y), hit: hitTag});
          if (hit && composedDescendant(hit, target)) return {element: target, x, y,
            diagnostic: {attempts, elapsed_ms: Date.now() - started, rect: lastRect,
              clickable_region: {x: Math.round(left), y: Math.round(top),
                width: Math.round(right-left), height: Math.round(bottom-top)},
              hit: hitTag, point_strategy: rx === 0.5 && ry === 0.5 ? 'center' : 'visible_internal'}};
          if (hit) {
            const hr = absoluteRect(hit);
            occluder = {tag: hitTag, role: norm(hit.getAttribute?.('role') || implicitRole(hit)),
              name: accessibleName(hit), rect: {x: Math.round(hr.x), y: Math.round(hr.y),
                width: Math.round(hr.width), height: Math.round(hr.height)}};
          }
        }
        lastError = 'element_occluded';
      }
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  } while (Date.now() < deadline);
  return {error: lastError === 'locator_not_found' ? 'locator_timeout'
    : lastError === 'selector_not_found' ? 'selector_timeout' : lastError,
    diagnostic: {attempts, elapsed_ms: Date.now() - started, last_error: lastError,
      rect: lastRect, hit: hitTag, candidate_points: candidates, occluder}};
};
let el = null;
if (command.name === 'prepare_drag') {
  const source = await waitForTarget(input.source || {}, true, true);
  if (source.error) return {ok: false, error: source.error,
    output: {side: 'source', matches: source.count || 0, diagnostic: source.diagnostic || {}},
    observation_version: version()};
  const target = await waitForTarget(input.target || {}, true, true);
  if (target.error) return {ok: false, error: target.error,
    output: {side: 'target', matches: target.count || 0, diagnostic: target.diagnostic || {}},
    observation_version: version()};
  return {ok: true, output: {source_x: source.x, source_y: source.y,
    target_x: target.x, target_y: target.y, rev: state.rev, url: String(location.href),
    diagnostic: {source: source.diagnostic || {}, target: target.diagnostic || {}}},
    observation_version: version()};
}
if (command.name === 'prepare_pointer') {
  if (Number.isFinite(Number(input.x)) && Number.isFinite(Number(input.y))) {
    const x = Number(input.x), y = Number(input.y);
    el = deepElementFromPoint(document, x, y);
    if (!el) return {ok: false, error: 'coordinate_has_no_target', observation_version: version()};
    if (el.matches?.('input[type=file]')) return {ok: false, error: 'file_upload_needs_user',
      output: {needs_user: 'file_upload'}, observation_version: version()};
    return {ok: true, output: {x, y, rev: state.rev, url: String(location.href),
      target: String(el.tagName || '').toLowerCase()}, observation_version: version()};
  }
  const prepared = await waitForTarget(input, true, true);
  if (prepared.error) return {ok: false, error: prepared.error,
    output: {matches: prepared.count || 0, diagnostic: prepared.diagnostic || {}},
    observation_version: version()};
  el = prepared.element;
  const approvalError = verifyApproval(el, input._approval_guard);
  if (approvalError) return {ok: false, error: approvalError,
    output: {action_executed: false, requires_reinspect: true,
      next_step: 'inspect_then_request_new_approval'}, observation_version: version()};
  if (el.matches?.('input[type=file]')) return {ok: false, error: 'file_upload_needs_user',
    output: {needs_user: 'file_upload'}, observation_version: version()};
  const pointerAction = String(input.pointer_action || '');
  if (pointerAction === 'check' || pointerAction === 'uncheck') {
    const check = checkControl(el);
    if (!check) return {ok: false, error: 'element_not_checkable',
      output: {diagnostic: prepared.diagnostic || {}}, observation_version: version()};
    const current = check.checked;
    const desired = pointerAction === 'check';
    if (!desired && check.role === 'radio')
      return {ok: false, error: 'radio_cannot_uncheck', observation_version: version()};
    if (current === desired) return {ok: true, output: {skip: true, checked: desired,
      effect_verified: true, state_source: check.sources.checked,
      action_executed: false, delivery: 'skipped', outcome: 'effect_verified', condition_met: true,
      rev: state.rev, url: String(location.href), input_path: 'native_pointer',
      diagnostic: prepared.diagnostic || {}}, observation_version: version()};
  }
  return {ok: true, output: {x: prepared.x, y: prepared.y, rev: state.rev,
    url: String(location.href), target: String(el.tagName || '').toLowerCase(),
    approval_revalidated: !!input._approval_guard,
    diagnostic: prepared.diagnostic || {}},
    observation_version: version()};
}
if (command.name === 'settle') {
  await new Promise(resolve => setTimeout(resolve, 250));
  const url = String(location.href), changes = [];
  if (url !== String(input.before_url || '')) changes.push('url_changed');
  if (state.rev !== Number(input.before_rev || 0)) changes.push('document_changed');
  const modal = snapshot({max_nodes: 200, structure: false}).modal_stack || [];
  if (modal.length) changes.push('dialog_opened');
  let checked;
  if (typeof input.desired_checked === 'boolean') {
    const prepared = await waitForTarget(input, true, false);
    if (prepared.error) return {ok: false, error: prepared.error,
      output: {diagnostic: prepared.diagnostic || {}}, observation_version: version()};
    const target = prepared.element;
    const check = checkControl(target);
    checked = check?.checked;
    if (checked !== input.desired_checked) return {ok: false,
      error: 'check_state_not_reached', output: {checked, desired_checked: input.desired_checked,
        url, changes}, observation_version: version()};
  }
  state.rev += 1;
  const effectVerified = checked !== undefined;
  return {ok: true, output: {url, changes, requires_reinspect: true,
    action_executed: true, delivery: 'dispatched', effect_verified: effectVerified,
    outcome: effectVerified ? 'effect_verified' : 'delivery_only',
    condition_met: effectVerified ? true : null,
    next_step: effectVerified ? 'inspect_for_followup' : 'inspect_or_wait_for_postcondition',
    diagnostic: input.diagnostic || {},
    approval_revalidated: !!input.approval_revalidated,
    modal_count: modal.length, input_path: String(input.input_path || 'native'),
    action: String(input.action || ''), ...(checked === undefined ? {} : {checked})},
    observation_version: version()};
}
const hasTarget = !!(input.node_id || input.selector || input.locator);
const needsTarget = ['fill','select'].includes(command.name);
if (hasTarget || needsTarget) {
  const prepared = await waitForTarget(input, hasTarget || needsTarget, command.name !== 'scroll');
  if (prepared.error) return {ok: false, error: prepared.error,
    output: {matches: prepared.count || 0, diagnostic: prepared.diagnostic || {}},
    observation_version: version()};
  el = prepared.element;
}
if (command.name === 'focus') {
  // 原生按键前把焦点放到目标上。没指定目标就用当前焦点(没有则 body)。
  const target = el || document.activeElement || document.body;
  if (typeof input.text === 'string' && sensitive(target))
    return {ok: false, error: 'sensitive_field_needs_user', observation_version: version()};
  if (target.matches?.('input[type=file]')) return {ok: false, error: 'file_upload_needs_user',
    output: {needs_user: 'file_upload'}, observation_version: version()};
  try { target.scrollIntoView?.({block: 'center', inline: 'nearest'}); } catch (_) {}
  try { target.focus?.(); } catch (_) {}
  return {ok: true, output: {rev: state.rev, url: String(location.href),
    focused: String(target.tagName || '').toLowerCase()}, observation_version: version()};
}
if (command.name === 'blur') {
  const target = el || document.activeElement;
  if (!target || target === document.body) return {ok: true, output: {rev: state.rev,
    url: String(location.href), blurred: ''}, observation_version: version()};
  try { target.blur?.(); } catch (_) {}
  await new Promise(resolve => setTimeout(resolve, 250));
  return {ok: true, output: {rev: state.rev, url: String(location.href),
    blurred: String(target.tagName || '').toLowerCase()}, observation_version: version()};
}
const beforeRev = state.rev, beforeUrl = String(location.href);
let selectedCount, selectedResult;
if (command.name === 'fill') {
  if (sensitive(el)) return {ok: false, error: 'sensitive_field_needs_user'};
  el.focus();
  if (el.isContentEditable) {
    el.textContent = String(input.value ?? '');
  } else {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, String(input.value ?? '')); else el.value = String(input.value ?? '');
  }
  fire(el, 'input'); fire(el, 'change');
} else if (command.name === 'select') {
  if (!(el instanceof HTMLSelectElement)) return {ok: false, error: 'select_target_invalid',
    observation_version: version()};
  const desired = Array.isArray(input.values)
    ? input.values.map(value => String(value)) : [String(input.value ?? '')];
  if (!el.multiple && desired.length > 1) return {ok: false, error: 'select_multiple_not_allowed',
    output: {requested_count: desired.length}, observation_version: version()};
  const options = Array.from(el.options), picked = [];
  for (const wantedOption of desired) {
    const option = options.find(row => String(row.value) === wantedOption)
      || options.find(row => norm(row.label || row.textContent || '', 1000) === norm(wantedOption, 1000));
    if (!option) return {ok: false, error: 'select_option_not_found',
      output: {requested_count: desired.length, matched_count: picked.length},
      observation_version: version()};
    picked.push(option);
  }
  const chosen = new Set(picked);
  for (const option of options) option.selected = chosen.has(option);
  selectedCount = options.filter(option => option.selected).length;
  fire(el, 'input'); fire(el, 'change');
  selectedResult = selectedValues(el);
  if (!Array.isArray(selectedResult) || selectedResult.length !== desired.length
      || picked.some(option => !option.selected)) {
    return {ok: false, error: 'select_state_not_reached',
      output: {selected_values: selectedResult || [], requested_values: desired},
      observation_version: version()};
  }
} else if (command.name === 'click' || command.name === 'coordinate_click') {
  return {ok: false, error: 'native_pointer_requires_macos11', observation_version: version()};
} else if (command.name === 'press') {
  const target = el || document.activeElement || document.body;
  const key = String(input.key || '');
  const init = {key, code: key, bubbles: true, cancelable: true, composed: true};
  const proceed = target.dispatchEvent(new KeyboardEvent('keydown', init));
  target.dispatchEvent(new KeyboardEvent('keyup', init));
  if (proceed && key === 'Enter') {
    if (target.form) target.form.requestSubmit?.();
    else if (target.matches?.('button,[role=button],a[href]')) target.click();
  } else if (proceed && key === 'Tab') {
    const focusable = Array.from(document.querySelectorAll('a[href],button,input,textarea,select,[tabindex]'))
      .filter(x => rendered(x) && !x.disabled && x.tabIndex >= 0);
    const at = focusable.indexOf(target); focusable[(at + 1) % Math.max(1, focusable.length)]?.focus();
  } else if (proceed && (key === ' ' || key === 'Space')
             && target.matches?.('button,[role=button],input[type=checkbox],input[type=radio]')) target.click();
  else if (key === 'Escape') target.blur?.();
} else if (command.name === 'type') {
  // 原生按键映射不了时的兜底:execCommand 走的是编辑器路径,会正常发 beforeinput/input
  const target = el || document.activeElement || document.body;
  if (sensitive(target)) return {ok: false, error: 'sensitive_field_needs_user'};
  target.focus?.();
  const text = String(input.text ?? '');
  let inserted = false;
  try { inserted = document.execCommand('insertText', false, text); } catch (_) {}
  if (!inserted) {
    if (!('value' in target)) return {ok: false, error: 'type_target_not_editable'};
    const proto = target instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    const next = String(target.value || '') + text;
    if (setter) setter.call(target, next); else target.value = next;
    fire(target, 'input'); fire(target, 'change');
  }
} else if (command.name === 'hover') {
  return {ok: false, error: 'native_pointer_requires_macos11', observation_version: version()};
} else if (command.name === 'scroll') {
  if (el) el.scrollIntoView({block: 'center', inline: 'nearest'});
  else window.scrollBy({left: Number(input.x || 0), top: Number(input.y || 0), behavior: 'instant'});
} else {
  return {ok: false, error: 'unsupported_action'};
}
await new Promise(resolve => setTimeout(resolve, 250));
const afterUrl = String(location.href), changes = [];
if (afterUrl !== beforeUrl) changes.push('url_changed');
if (state.rev !== beforeRev) changes.push('document_changed');
const modal = snapshot({max_nodes: 200, structure: false}).modal_stack || [];
if (modal.length) changes.push('dialog_opened');
state.rev += 1;
return {ok: true, output: {url: afterUrl, changes, requires_reinspect: changes.length > 0,
  modal_count: modal.length, action_executed: true,
  effect_verified: selectedCount === undefined ? false : true,
  outcome: selectedCount === undefined ? 'delivery_only' : 'effect_verified',
  ...(selectedCount === undefined ? {} : {selected_count: selectedCount,
    selected_values: selectedResult, condition_met: true})},
  observation_version: version()};
