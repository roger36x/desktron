// 会话页右侧信息卡：项目与制品取工作流整体，回顾与变更只取当前会话。
// 卡片只在宽屏出现；窄屏不额外占用正文宽度。

const HS_SIT_PROJECT_LIMIT = 3;
const HS_SIT_ARTIFACT_LIMIT = 4;
// 类别只用来在行尾标一枚小字,认不出的一律不标——标成「其他」等于没说。
const HS_SIT_ARTIFACT_KINDS = { doc: '文档', demo: 'demo', dir: '目录', page: '页面', image: '图', data: '数据', link: '链接' };
const HS_SIT_DISABLED_MQ = '(hover: none) and (pointer: coarse), (max-width: 820px)';

function hsSituationEnabled() {
  const mediaMobile = !!(window.matchMedia && window.matchMedia(HS_SIT_DISABLED_MQ).matches);
  // iOS 主屏窗口偶尔把 pointer 媒体特征报成桌面值；屏幕短边与触点共同兜住真机，
  // 不靠 UA，也不会误伤普通窄桌面窗口。
  const screenShort = Math.min(Number(screen.width) || innerWidth, Number(screen.height) || innerHeight);
  const touchMobile = Number(navigator.maxTouchPoints || 0) > 0 && screenShort <= 820;
  return !(mediaMobile || touchMobile);
}

function hsSituationMarkup() {
  // 所有版面都创建同一份信息栏。空间足够时常驻；空间不足时由页头按钮把同一节点
  // 作为浮层打开，不另造一套移动端内容与状态。
  return '<button type="button" class="hs-sit-scrim" aria-label="收起信息栏"></button>'
    + '<aside class="hs-sit" aria-label="会话上下文">'
    // 审批住在首区:有待办时显示数量，没有待办时保留最近结果。它和项目、制品同级，
    // 不把整组摘掉，避免信息卡结构随状态跳动。
    + '<section class="hs-sit-sec hs-sit-approvals collapsed">'
      + '<div class="hs-sit-approval-bar">'
        + '<button type="button" class="hs-sit-head hs-sit-approval-head" aria-expanded="false">'
          + '<h2>待审批</h2>'
          + '<span class="hs-sit-approval-count" aria-live="polite">0</span>'
          + '<svg class="appr-chev" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg>'
        + '</button>'
        + '<button type="button" class="hs-sit-approval-clear" hidden>清空</button>'
      + '</div>'
      + '<div class="hs-sit-approval-host"></div>'
    + '</section>'
    + '<section class="hs-sit-sec hs-sit-projects">'
      + '<span class="hs-sit-head"><h2>项目</h2>'
        + '<button type="button" class="hs-sit-project-add" aria-label="添加项目" disabled>+</button></span>'
      + '<div class="hs-sit-project-list"><div class="hs-sit-muted">加载中…</div></div>'
      + '<button type="button" class="hs-sit-project-more" hidden></button>'
    + '</section>'
    // 制品会堆多,标题整块可折叠——形制与待审批那一区同一套(按标题、箭头转九十度)。
    // 折叠态记在本地:折起来是个持续的意愿,不该每开一页又展开一次。
    + '<section class="hs-sit-sec hs-sit-artifacts' + (hsSituationArtifactsFolded() ? ' collapsed' : '') + '">'
      + '<button type="button" class="hs-sit-head hs-sit-artifact-head" aria-expanded="'
        + (hsSituationArtifactsFolded() ? 'false' : 'true') + '">'
        + '<h2>制品</h2><span class="hs-sit-artifact-count" hidden></span>'
        + '<svg class="appr-chev" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg>'
      + '</button>'
      + '<div class="hs-sit-artifact-list"><div class="hs-sit-muted">加载中…</div></div>'
      + '<button type="button" class="hs-sit-artifact-more" hidden></button>'
    + '</section>'
    + '<section class="hs-sit-sec hs-sit-recap">'
      + '<span class="hs-sit-head"><h2>回顾</h2><span class="hs-sit-recap-time"></span></span>'
      + '<div class="hs-sit-recap-text hs-sit-muted">当前会话还没有回顾</div>'
    + '</section>'
    + '<button type="button" class="hs-sit-sec hs-sit-changes" disabled>'
      + '<span class="hs-sit-change-title">变更</span><span class="hs-sit-change-stat">'
        + '<span class="hs-cadd">+0</span><span class="hs-cdel">-0</span>'
      + '</span>'
    + '</button>'
  + '</aside>';
}

const HS_SIT_ARTIFACT_FOLD_KEY = 'cd_sit_artifacts_folded';

// Static labels of the info panel. Dynamic rows bind their own text when painted.
function hsSituationI18n(el) {
  const bind = (selector, key, attr = 'text') => DeskI18n.bind(el.querySelector(selector), key, {}, attr);
  bind('.hs-sit-scrim', 'sit.dismiss', 'aria-label');
  bind('.hs-sit', 'sit.label', 'aria-label');
  bind('.hs-sit-approval-head h2', 'sit.approvals.pending');
  bind('.hs-sit-approval-clear', 'sit.clear');
  bind('.hs-sit-projects .hs-sit-head h2', 'sit.projects');
  bind('.hs-sit-artifact-head h2', 'sit.artifacts');
  bind('.hs-sit-recap .hs-sit-head h2', 'sit.recap');
  bind('.hs-sit-change-title', 'sit.changes');
  el.querySelectorAll('.hs-sit-project-list > .hs-sit-muted, .hs-sit-artifact-list > .hs-sit-muted')
    .forEach((node) => DeskI18n.bind(node, 'common.loading'));
  bind('.hs-sit-recap-text', 'sit.noRecap');
}

function hsSituationArtifactsFolded() {
  try { return localStorage.getItem(HS_SIT_ARTIFACT_FOLD_KEY) === '1'; } catch { return false; }
}

function hsSituationApplyFold(el, folded) {
  const sec = el && el.querySelector('.hs-sit-artifacts');
  if (!sec) return;
  sec.classList.toggle('collapsed', folded);
  const head = sec.querySelector('.hs-sit-artifact-head');
  if (head) head.setAttribute('aria-expanded', String(!folded));
}

function hsSituationFoldArtifacts(v, folded) {
  try { localStorage.setItem(HS_SIT_ARTIFACT_FOLD_KEY, folded ? '1' : '0'); } catch {}
  hsSituationApplyFold(v && v.el, folded);
  // 每个会话页各有一张卡。折叠是这一栏的意愿,不是这一页的:开着的其它页一起跟上,
  // 否则切过去看到的还是上一个状态,像是没记住。
  if (typeof _hsViews === 'undefined') return;
  for (const view of _hsViews.values()) {
    if (view && view.el && !(v && view.el === v.el)) hsSituationApplyFold(view.el, folded);
  }
}

function hsSituationCurrentView() {
  if (typeof pActive === 'undefined' || !pActive || !pActive.el
      || typeof _hsViews === 'undefined') return null;
  for (const v of _hsViews.values()) if (v && v.el === pActive.el) return v;
  return null;
}

// CSS 自己掌握容器查询与设备断点，JS 只读它给出的结果，不再复制一套宽度判断。
function hsSituationCornerState() {
  const v = hsSituationCurrentView();
  if (!v || !v.el || !v.el.querySelector('.hs-sit'))
    return { available: false, inline: false, open: false };
  const review = v.el.classList.contains('rv-on') || v.el.classList.contains('lr-on');
  const inline = !review && getComputedStyle(v.el.querySelector('.hs-sit'))
    .getPropertyValue('--hs-sit-inline').trim() === '1';
  if (inline) v.el.classList.remove('hs-sit-open');
  return { available: !review, inline,
    open: !review && !inline && v.el.classList.contains('hs-sit-open') };
}

function hsSituationToggleActive() {
  const v = hsSituationCurrentView();
  const state = hsSituationCornerState();
  if (!v || !state.available || state.inline) return false;
  const open = v.el.classList.toggle('hs-sit-open');
  if (open) hsSituationSync(v);
  if (typeof apprRehost === 'function') apprRehost(true);
  if (typeof syncOverlayCornerAction === 'function') syncOverlayCornerAction();
  return true;
}

function hsSituationAgo(ts) {
  const n = Number(ts || 0);
  if (!n) return '';
  const sec = Math.max(0, Math.round((Date.now() - n) / 1000));
  if (sec < 10) return DeskI18n.t('time.justNow');
  if (sec < 60) return DeskI18n.t('time.seconds', { n: sec });
  if (sec < 3600) return DeskI18n.t('time.minutes', { n: Math.round(sec / 60) });
  if (sec < 86400) return DeskI18n.t('time.hours', { n: Math.round(sec / 3600) });
  return new Date(n).toLocaleDateString(DeskI18n.tag, { month: 'numeric', day: 'numeric' });
}

let _hsSituationTip = null;
let _hsSituationTipTimer = 0;
let _hsSituationTipAnchor = null;

function hsSituationTipHide(anchor) {
  if (anchor && _hsSituationTipAnchor !== anchor) return;
  clearTimeout(_hsSituationTipTimer);
  _hsSituationTipTimer = 0;
  _hsSituationTipAnchor = null;
  if (!_hsSituationTip) return;
  _hsSituationTip.classList.remove('show');
  _hsSituationTip.setAttribute('aria-hidden', 'true');
}

function hsSituationTipShow(el, text) {
  hsSituationTipHide();
  _hsSituationTipAnchor = el;
  _hsSituationTipTimer = setTimeout(() => {
    if (!el.isConnected || _hsSituationTipAnchor !== el) return;
    if (!_hsSituationTip) {
      _hsSituationTip = document.createElement('div');
      _hsSituationTip.id = 'hs-sit-tooltip';
      _hsSituationTip.className = 'hs-sit-tooltip';
      _hsSituationTip.setAttribute('role', 'tooltip');
      _hsSituationTip.setAttribute('aria-hidden', 'true');
      document.body.appendChild(_hsSituationTip);
      addEventListener('resize', () => hsSituationTipHide(), { passive: true });
      addEventListener('scroll', () => hsSituationTipHide(), { capture: true, passive: true });
    }
    _hsSituationTip.textContent = text;
    _hsSituationTip.classList.remove('show');
    _hsSituationTip.style.width = 'max-content';
    _hsSituationTip.style.maxWidth = Math.min(360, innerWidth - 24) + 'px';
    _hsSituationTip.style.left = '0px';
    _hsSituationTip.style.top = '0px';
    _hsSituationTip.setAttribute('aria-hidden', 'false');
    const rect = el.getBoundingClientRect();
    const width = _hsSituationTip.offsetWidth;
    const height = _hsSituationTip.offsetHeight;
    const pad = 12, gap = 10;
    const left = Math.max(pad, Math.min(innerWidth - width - pad, rect.right - width));
    const below = rect.bottom + gap;
    const above = rect.top - height - gap;
    const useAbove = below + height > innerHeight - pad && above >= pad;
    _hsSituationTip.dataset.side = useAbove ? 'above' : 'below';
    _hsSituationTip.style.left = Math.round(left) + 'px';
    _hsSituationTip.style.top = Math.round(useAbove ? above : Math.min(below, innerHeight - height - pad)) + 'px';
    requestAnimationFrame(() => _hsSituationTip && _hsSituationTip.classList.add('show'));
  }, 260);
}

function hsSituationTipBind(el, text, truncatedOnly = false) {
  if (!el) return;
  const full = String(text || '');
  el.removeAttribute('title');
  el.removeAttribute('aria-label');
  el.onmouseenter = null;
  el.onmouseleave = null;
  hsSituationTipHide(el);
  if (!full.trim()) return;
  const bind = () => {
    if (!el.isConnected) return;
    if (truncatedOnly) {
      const clipped = el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1;
      if (!clipped) return;
    }
    el.setAttribute('aria-label', full);
    el.onmouseenter = () => hsSituationTipShow(el, full);
    el.onmouseleave = () => hsSituationTipHide(el);
  };
  if (truncatedOnly) requestAnimationFrame(bind);
  else bind();
}

function hsSituationTruncatedTitle(el, text) {
  hsSituationTipBind(el, text, true);
}

function hsSituationCombinedTurn(v) {
  const parts = new Map();
  let i = 0;
  for (const turn of v.turns || []) {
    if (!turn || !(turn.parts instanceof Map)) continue;
    for (const part of turn.parts.values()) parts.set('sit-' + (i++), part);
  }
  return { parts };
}

function hsSituationPaintChanges(v) {
  const row = v.el.querySelector('.hs-sit-changes');
  if (!row || typeof hsRvCollect !== 'function') return;
  const turn = hsSituationCombinedTurn(v);
  const files = hsRvCollect(turn);
  const add = files.reduce((n, file) => n + file.add, 0);
  const del = files.reduce((n, file) => n + file.del, 0);
  const stat = row.querySelector('.hs-sit-change-stat');
  stat.innerHTML = '<span class="hs-cadd">+' + add + '</span>'
    + '<span class="hs-cdel">-' + del + '</span>';
  row.disabled = !files.length;
  row._turn = turn;
}

function hsSituationOpenProject(project) {
  const path = String((project && (project.path || project.dir)) || '');
  if (!path || typeof openFilesPage !== 'function') return;
  const id = path.startsWith('abs:') ? path : 'abs:' + path;
  const name = String(project.name || path.replace(/\/+$/, '').split('/').pop() || path);
  // 文件应用先留在会话右侧，用户需要时再从侧栏扩成主页面。
  const page = openFilesPage({ id, name }, false);
  if (page && typeof showAppSide === 'function') showAppSide(page);
}

function hsSituationPaint(v, data) {
  v.situation = data || {};
  const projects = Array.isArray(v.situation.projects) ? v.situation.projects : [];
  const list = v.el.querySelector('.hs-sit-project-list');
  list.innerHTML = projects.length ? projects.map((project, i) =>
    '<button type="button" class="hs-sit-project' + (i >= HS_SIT_PROJECT_LIMIT ? ' hs-sit-project-extra' : '')
      + '" data-project-index="' + i + '">'
      + '<span>' + hsEsc(project.name || project.path || '未命名项目') + '</span>'
      + (project.branch ? '<span class="hs-sit-branch">' + hsEsc(project.branch) + '</span>' : '')
    + '</button>').join('') : '<div class="hs-sit-muted">还没有关联项目</div>';
  if (!projects.length) DeskI18n.bind(list.firstChild, 'sit.noProjects');
  list.querySelectorAll('.hs-sit-project').forEach((row) => {
    row.onclick = () => hsSituationOpenProject(projects[Number(row.dataset.projectIndex)]);
    hsSituationTipBind(row, DeskI18n.t('sit.openInIde'));
  });
  const more = v.el.querySelector('.hs-sit-project-more');
  more.hidden = projects.length <= HS_SIT_PROJECT_LIMIT;
  DeskI18n.bind(more, 'sit.moreProjects', { count: projects.length });
  v.el.querySelector('.hs-sit-projects').classList.remove('open');
  v.el.querySelector('.hs-sit-project-add').disabled = !v.situation.domain;

  hsSituationSyncArtifacts(v);

  const recap = v.situation.recap || null;
  const recapText = recap && recap.text ? String(recap.text) : '';
  const recapEl = v.el.querySelector('.hs-sit-recap-text');
  if (recapText) DeskI18n.plain(recapEl, recapText);
  else DeskI18n.bind(recapEl, 'sit.noRecap');
  hsSituationTruncatedTitle(recapEl, recapText);
  v.el.querySelector('.hs-sit-recap-time').textContent = recap ? hsSituationAgo(recap.updated) : '';
  hsSituationPaintChanges(v);
}

// 制品:这个工作流做出过什么——文档、demo、预览页,不含代码。清单来自
// domains/<工作流>/artifacts/,目录里有什么就列什么;外部链接(预览页、飞书文档)由
// 同目录的 index.jsonl 补进来。写文件那一步本身就是登记,不另设一个要人记得调的动作。
function hsSituationArtifactKind(item) {
  return HS_SIT_ARTIFACT_KINDS[String((item || {}).kind || '')] || '';
}

function hsSituationOpenArtifact(v, item) {
  if (!item) return;
  if (item.external) {
    if (typeof openExternal === 'function') openExternal(item.ref);
    return;
  }
  const domain = String(((v.situation || {}).domain) || '').split(':', 1)[0];
  const path = item.entry || item.path || '';
  if (domain && path && typeof openFileBrowser === 'function') {
    // 制品始终落在工作区的 domains/<工作流>/artifacts/，不在工作流引用的代码仓。
    // _desktop 的文件根也是整个工作区，不能直接拿工作流相对路径去读。
    openFileBrowser('_desktop', 'domains/' + domain + '/' + path, '', true, !item.entry && item.kind === 'dir');
  }
}

function hsSituationPaintArtifacts(v) {
  const list = v.el.querySelector('.hs-sit-artifact-list');
  const more = v.el.querySelector('.hs-sit-artifact-more');
  const count = v.el.querySelector('.hs-sit-artifact-count');
  if (!list) return;
  const items = Array.isArray(v.artifacts) ? v.artifacts : [];
  // 数出来才显示。加载中与取不到时藏起来——空胶囊是一枚没有内容的灰块,比不显示更费解。
  count.textContent = String(items.length);
  count.hidden = false;
  list.innerHTML = items.length ? items.map((item, i) =>
    '<button type="button" class="hs-sit-artifact' + (i >= HS_SIT_ARTIFACT_LIMIT ? ' hs-sit-artifact-extra' : '')
      + '" data-artifact-index="' + i + '">'
      + '<span>' + hsEsc(item.title || item.ref) + '</span>'
      + (hsSituationArtifactKind(item)
          ? '<span class="hs-sit-artifact-kind" data-kind="' + hsEsc(item.kind) + '">'
            + hsEsc(hsSituationArtifactKind(item)) + '</span>' : '')
    + '</button>').join('')
    : '<div class="hs-sit-muted">还没有制品。写进工作流的 artifacts/ 目录即可出现在这里。</div>';
  if (!items.length) DeskI18n.bind(list.firstChild, 'sit.noArtifacts');
  list.querySelectorAll('.hs-sit-artifact-kind').forEach((tag) => {
    DeskI18n.bind(tag, 'sit.kind.' + tag.dataset.kind, {}, 'text', tag.textContent);
  });
  list.querySelectorAll('.hs-sit-artifact').forEach((row) => {
    const item = items[Number(row.dataset.artifactIndex)];
    row.onclick = () => hsSituationOpenArtifact(v, item);
    hsSituationTipBind(row, item && item.note ? item.note : (item ? item.ref : ''));
  });
  more.hidden = items.length <= HS_SIT_ARTIFACT_LIMIT;
  DeskI18n.bind(more, 'sit.moreArtifacts', { count: items.length });
  v.el.querySelector('.hs-sit-artifacts').classList.remove('open');
}

async function hsSituationSyncArtifacts(v) {
  const list = v.el.querySelector('.hs-sit-artifact-list');
  if (!list) return;
  const domain = String(((v.situation || {}).domain) || '');
  // 后端把制品与项目放在同一条 situation 里,正常情况下不必再请求一次。
  // 单独那条接口留给两种情形:后端还没重启(payload 里没有这一项)、写入后就地刷新。
  if (Array.isArray((v.situation || {}).artifacts)) {
    v.artifacts = v.situation.artifacts;
    hsSituationPaintArtifacts(v);
    return;
  }
  if (!domain) {
    v.artifacts = [];
    hsSituationPaintArtifacts(v);
    return;
  }
  // 卡片可能在取数途中被切到另一个工作流:世代号对不上就丢弃,不把上一个的清单画上去。
  const gen = (v.artifactGen = (v.artifactGen || 0) + 1);
  try {
    const data = await api('/workbench/artifacts?domain=' + encodeURIComponent(domain), { timeout: 30000 });
    if (gen !== v.artifactGen || !list.isConnected) return;
    v.artifacts = (data && data.items) || [];
    hsSituationPaintArtifacts(v);
  } catch {
    if (gen !== v.artifactGen || !list.isConnected) return;
    list.innerHTML = '<div class="hs-sit-muted">制品暂不可用</div>';
    DeskI18n.bind(list.firstChild, 'sit.artifactsUnavailable');
    const count = v.el.querySelector('.hs-sit-artifact-count');
    if (count) count.hidden = true;
  }
}


async function hsSituationSync(v) {
  if (!v || !v.el || v.situationBusy || !v.el.querySelector('.hs-sit')) return;
  v.situationBusy = true;
  try {
    let data = null;
    try {
      data = await api('/session/' + encodeURIComponent(v.sid) + '/situation', { timeout: 30000 });
    } catch {
      // 前端资源可热更新，后端接口要等进程重启。两者短暂跨版本时不能让卡片整块失效：
      // 用已经存在的只读接口拼回项目；Recap 仍只认新接口的会话级记录，
      // 绝不从工作流 journal 的标题猜归属。制品另有自己的接口，由 paint 单独去取。
      const sessions = await api('/session', { timeout: 30000 });
      const row = (sessions || []).find((item) => item && item.id === v.sid) || {};
      const refs = Array.isArray(row.refs) ? row.refs.filter(Boolean) : [];
      let domain = String(row.domain || '');
      if ((!domain || domain === '_desktop') && refs.length) domain = String(refs[refs.length - 1]);
      const workspace = (domain || '_desktop').split(':', 1)[0] || '_desktop';
      let projects = [];
      try {
        const result = await api('/workbench/domains/' + encodeURIComponent(workspace) + '/repos',
                                 { timeout: 30000 });
        projects = (result && result.repos) || [];
      } catch {}
      data = { domain: workspace, projects, recap: null };
    }
    if (v.el.isConnected) hsSituationPaint(v, data);
  } catch {
    const list = v.el.querySelector('.hs-sit-project-list');
    if (list) list.innerHTML = '<div class="hs-sit-muted">信息暂不可用</div>';
    const artifacts = v.el.querySelector('.hs-sit-artifact-list');
    if (artifacts) artifacts.innerHTML = '<div class="hs-sit-muted">信息暂不可用</div>';
  } finally {
    v.situationBusy = false;
  }
}

function hsSituationWire(v) {
  const el = v.el;
  if (!el.querySelector('.hs-sit')) return;
  hsSituationI18n(el);
  el.querySelector('.hs-sit-scrim').onclick = () => {
    el.classList.remove('hs-sit-open');
    if (typeof apprRehost === 'function') apprRehost(true);
    if (typeof syncOverlayCornerAction === 'function') syncOverlayCornerAction();
  };
  const projects = el.querySelector('.hs-sit-projects');
  const projectAdd = el.querySelector('.hs-sit-project-add');
  hsSituationTipBind(projectAdd, DeskI18n.t('sit.addProject'));
  projectAdd.onclick = async () => {
    const domain = String(((v.situation || {}).domain) || '');
    if (!domain || typeof _wfRepoPick !== 'function') return;
    projectAdd.disabled = true;
    try {
      // 复用工作流页的系统目录选择器与新增接口；完成后重读卡片自己的项目清单。
      await _wfRepoPick(null, domain);
      await hsSituationSync(v);
    } finally {
      if (projectAdd.isConnected) projectAdd.disabled = !((v.situation || {}).domain);
    }
  };
  el.querySelector('.hs-sit-project-more').onclick = () => {
    const open = projects.classList.toggle('open');
    DeskI18n.bind(el.querySelector('.hs-sit-project-more'), open ? 'common.less' : 'sit.moreProjects',
      { count: ((v.situation || {}).projects || []).length });
  };
  const artifacts = el.querySelector('.hs-sit-artifacts');
  el.querySelector('.hs-sit-artifact-head').onclick = () => {
    hsSituationFoldArtifacts(v, !artifacts.classList.contains('collapsed'));
  };
  el.querySelector('.hs-sit-artifact-more').onclick = () => {
    const open = artifacts.classList.toggle('open');
    DeskI18n.bind(el.querySelector('.hs-sit-artifact-more'), open ? 'common.less' : 'sit.moreArtifacts',
      { count: (v.artifacts || []).length });
  };
  el.querySelector('.hs-sit-changes').onclick = () => {
    const row = el.querySelector('.hs-sit-changes');
    if (row._turn) {
      row._turn._rvOpts = { title: '会话变更', session: true };
      hsRvOpen(v, row._turn, '');
    }
  };
  const observer = new MutationObserver(() => {
    if (v.situationPaintRaf) return;
    v.situationPaintRaf = requestAnimationFrame(() => {
      v.situationPaintRaf = 0;
      hsSituationPaintChanges(v);
    });
  });
  observer.observe(v.stream, { childList: true, subtree: true });
  v.situationObserver = observer;
  // 盖住的页不在这里取:建页时每一页都经过这里,九页刷新就是九条 situation,而每条
  // 要为工作流里的每个项目起一次 git 子进程。露面时 hsShown 会取(2026-09-05)。
  if (typeof hsHidden !== "function" || !hsHidden(v)) hsSituationSync(v);
}
