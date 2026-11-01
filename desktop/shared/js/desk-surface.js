const clean = value => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 120);
const visible = node => {
  const style = getComputedStyle(node), box = node.getBoundingClientRect();
  return !node.hidden && style.display !== 'none' && style.visibility !== 'hidden'
    && box.width > 0 && box.height > 0;
};
const nodes = Array.from(document.querySelectorAll(
  '[data-app-control-surface-id=\"navigation\"][data-app-control-item]'));
const items = nodes.filter(node => visible(node) && !node.closest('[data-app-control-protected]'))
  .map(node => ({id: clean(node.getAttribute('data-app-control-item')),
    label: clean(node.getAttribute('data-app-control-label')),
    role: node.getAttribute('role') || (node.tagName === 'BUTTON' ? 'button' : 'link'),
    enabled: !node.disabled && node.getAttribute('aria-disabled') !== 'true',
    selected: node.getAttribute('aria-current') === 'page' || node.classList.contains('active')}));
const sidebarOpen = !document.body.classList.contains('side-collapsed');
const stable = JSON.stringify({items, sidebarOpen});
let hash = 2166136261;
for (let i = 0; i < stable.length; i += 1) { hash ^= stable.charCodeAt(i); hash = Math.imul(hash, 16777619); }
return {ok: true, output: {schema: 'desk.surface/v1', route: 'workspace',
  surface: 'navigation', title: 'Desktron', items, modal_stack: [],
  sidebar_open: sidebarOpen,
  capabilities: {inspect: true, actions: ['open_page', 'toggle_panel']}},
  observation_version: 'navigation:' + (hash >>> 0).toString(16)};
