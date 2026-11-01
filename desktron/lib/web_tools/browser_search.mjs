import { createHash, randomUUID } from 'node:crypto';
import { unwrapDuckDuckGoUrl } from './duckduckgo_search.mjs';
import { SEARCH_TRANSPORT_BROWSER_APP_CONTROL } from './search_provider.mjs';
import { createAppControlClientFromEnv } from './app_control_client.mjs';

export const DDG_BROWSER_ENDPOINT = 'https://duckduckgo.com/';
export const DDG_BROWSER_HOST = 'duckduckgo.com';
export const DDG_BROWSER_PROVIDER = 'duckduckgo-browser';

export class BrowserSearchError extends Error {
  constructor(code, message, options = {}) {
    super(message || code);
    this.name = 'BrowserSearchError';
    this.code = code;
    this.retryable = Boolean(options.retryable);
    this.browserRecommended = false;
  }
}

function cleanText(value, limit) {
  return String(value ?? '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '')
    .replace(/\s+/g, ' ').trim().slice(0, limit);
}

function operationOutput(value, action) {
  if (!value || value.status !== 'succeeded' || !value.output || typeof value.output !== 'object') {
    const reason = cleanText(value?.error, 500) || `${action} failed`;
    const unavailable = /app_control_unavailable|browser_(?:app_not_open|view_not_created|tab_unavailable)/.test(reason);
    throw new BrowserSearchError(unavailable ? 'browser_unavailable' : 'browser_operation_failed',
      unavailable ? 'Desk 后台浏览器不可用' : `后台浏览器${action}失败：${reason}`, { retryable: unavailable });
  }
  return value.output;
}

function safeResultUrl(raw) {
  let value = cleanText(raw, 8192);
  if (!value) return '';
  try { value = unwrapDuckDuckGoUrl(value); } catch { return ''; }
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return '';
    url.hash = '';
    return url.toString();
  } catch { return ''; }
}

function isDuckDuckGoAdvertisement(url, group) {
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase().replace(/\.$/, '');
    const ddg = host === DDG_BROWSER_HOST || host.endsWith(`.${DDG_BROWSER_HOST}`);
    if (ddg && (/\/y\.js$/i.test(parsed.pathname) ||
        ['ad_domain', 'ad_provider', 'ad_type', 'click_metadata'].some(key => parsed.searchParams.has(key)))) return true;
  } catch { return true; }
  return group.some(node => /^(?:ad|sponsored|广告)$/i.test(cleanText(node?.text || node?.name, 40)));
}

function isDescendant(node, ancestorId, byId) {
  let parent = node?.parent_id;
  const seen = new Set();
  while (parent && !seen.has(parent)) {
    if (parent === ancestorId) return true;
    seen.add(parent);
    parent = byId.get(parent)?.parent_id;
  }
  return false;
}

function articleAncestor(node, byId) {
  let parent = node?.parent_id;
  const seen = new Set();
  while (parent && !seen.has(parent)) {
    seen.add(parent);
    const current = byId.get(parent);
    if (!current) return null;
    if (String(current.tag || '').toLowerCase() === 'article') return current;
    parent = current.parent_id;
  }
  return null;
}

function isDuckDuckGoOwned(url) {
  try {
    const host = new URL(url).hostname.toLowerCase().replace(/\.$/, '');
    return host === DDG_BROWSER_HOST || host.endsWith(`.${DDG_BROWSER_HOST}`);
  } catch { return true; }
}

const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
const ABSOLUTE_ZH_DATE = /^(\d{4})年(\d{1,2})月(\d{1,2})日/;
const ABSOLUTE_EN_DATE = /^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),\s*(\d{4})/i;
const VIDEO_DURATION = /^(?:\d{1,2}:)?\d{1,2}:\d{2}\s*(?=\S)/;
const RELATIVE_DATE = /^(?:\d+\s*(?:分钟|小时|天|周|个月|年)前|\d+\s+(?:minutes?|hours?|days?|weeks?|months?|years?)\s+ago)/i;

function isoDate(year, month, day) {
  const m = Number(month);
  const d = Number(day);
  if (!(m >= 1 && m <= 12 && d >= 1 && d <= 31)) return '';
  return `${year}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
}

/** Split DDG's leading result date from the snippet; absolute dates become YYYY-MM-DD. */
export function splitLeadingDate(text) {
  // Video results prefix the date with a duration such as "1:43" or "1:02:10".
  const value = String(text || '').replace(VIDEO_DURATION, '');
  let match = ABSOLUTE_ZH_DATE.exec(value);
  if (match) {
    const date = isoDate(match[1], match[2], match[3]);
    if (date) return { published_at: date, rest: value.slice(match[0].length) };
  }
  match = ABSOLUTE_EN_DATE.exec(value);
  if (match) {
    const date = isoDate(match[3], MONTHS.indexOf(match[1].slice(0, 3).toLowerCase()) + 1, match[2]);
    if (date) return { published_at: date, rest: value.slice(match[0].length) };
  }
  match = RELATIVE_DATE.exec(value);
  if (match) return { published_at: match[0], rest: value.slice(match[0].length) };
  return { published_at: null, rest: value };
}

/** DDG renders the full snippet as text of the result <article>, after host, URL and title. */
function articleSnippet(article, title, breadcrumb) {
  const text = cleanText(article?.text, 4000);
  // Hosts often equal the title (e.g. "docs.example.com"); start after the URL breadcrumb.
  const crumbAt = breadcrumb ? text.indexOf(breadcrumb) : -1;
  const from = crumbAt >= 0 ? crumbAt + breadcrumb.length : 0;
  const at = title ? text.indexOf(title, from) : -1;
  if (at < 0) return null;
  const { published_at, rest } = splitLeadingDate(text.slice(at + title.length).trim());
  return { snippet: cleanText(rest, 2000), published_at };
}

function fallbackSnippet(group, title) {
  const parentIds = new Set(group.map(node => node?.parent_id).filter(Boolean));
  return group.filter(node => {
    const text = cleanText(node?.text || node?.name, 2000);
    if (!text || text === title || /^https?:\/\//i.test(text) ||
        /^[a-z0-9.-]+\.[a-z]{2,}(?:\/\S*)?$/i.test(text)) return false;
    const tag = String(node?.tag || '').toLowerCase();
    const role = String(node?.role || '').toLowerCase();
    return tag === 'p' || role === 'paragraph' || (!parentIds.has(node?.node_id) && tag !== 'a');
  }).map(node => cleanText(node.text || node.name, 2000)).sort((a, b) => b.length - a.length)[0] || '';
}

/** Parse the browser-safe App Control document snapshot for DDG results. */
export function parseDuckDuckGoSnapshot(snapshot, limit = 5) {
  let pageUrl;
  try { pageUrl = new URL(String(snapshot?.url || '')); }
  catch { throw new BrowserSearchError('provider_navigation_mismatch', '搜索页面尚未完成导航', { retryable: true }); }
  const host = pageUrl.hostname.toLowerCase().replace(/\.$/, '');
  if (pageUrl.protocol !== 'https:' || pageUrl.username || pageUrl.password ||
      (pageUrl.port && pageUrl.port !== '443') || host !== DDG_BROWSER_HOST) {
    throw new BrowserSearchError('provider_redirected', '搜索页面离开了固定的 DuckDuckGo 域名');
  }
  const pageText = String(snapshot?.text || '');
  if (/challenge-form|anomaly-modal|unfortunately, bots|verify you are human/i.test(pageText)) {
    throw new BrowserSearchError('provider_challenge', '搜索服务要求浏览器验证');
  }
  const nodes = Array.isArray(snapshot?.nodes) ? snapshot.nodes : [];
  const byId = new Map(nodes.map(node => [node?.node_id, node]));
  const positions = new Map(nodes.map((node, index) => [node, index]));
  const articles = new Map();
  const owners = new Map();
  for (const node of nodes) {
    const article = articleAncestor(node, byId);
    if (!article) continue;
    owners.set(node, article);
    if (!articles.has(article.node_id)) articles.set(article.node_id, []);
    articles.get(article.node_id).push(node);
  }
  // Some DDG variants omit the heading role or use h3 inside a result article.
  // Never interpret arbitrary navigation h3/link nodes as search results.
  const headings = nodes.map((node, index) => ({ node, index })).filter(({ node }) =>
    /^h2$/i.test(String(node?.tag || '')) ||
    (/^h3$/i.test(String(node?.tag || '')) && owners.has(node)));
  const results = [];
  let recognized = 0;
  let skipped = 0;
  const seenUrls = new Set();
  for (let at = 0; at < headings.length && results.length < limit; at += 1) {
    const { node: heading, index } = headings[at];
    const article = owners.get(heading);
    const group = article
      ? articles.get(article.node_id).filter(node => node !== heading)
      : nodes.slice(index + 1, at + 1 < headings.length ? headings[at + 1].index : nodes.length);
    const title = cleanText(heading.name || heading.text, 500);
    const titleLink = group.find(node => node?.role === 'link' && node?.href &&
      isDescendant(node, heading.node_id, byId)) || group.find(node => node?.role === 'link' && node?.href &&
      cleanText(node.name || node.text, 500) === title);
    if (!titleLink?.href || !title) continue;
    recognized += 1;
    if (isDuckDuckGoAdvertisement(titleLink.href, group)) {
      skipped += 1;
      continue;
    }
    const url = safeResultUrl(titleLink.href);
    if (!url) continue;
    // DDG's own modules (videos, news carousels, "more results") are not web results.
    if (isDuckDuckGoOwned(url) || seenUrls.has(url)) {
      skipped += 1;
      continue;
    }
    seenUrls.add(url);
    const headingAt = positions.get(heading);
    const breadcrumb = group.filter(node => node?.role === 'link' && positions.get(node) < headingAt)
      .map(node => cleanText(node.text || node.name, 2000)).filter(text => /^https?:\/\//i.test(text)).pop() || '';
    const parsed = article ? articleSnippet(article, title, breadcrumb) : null;
    results.push({
      title, raw_url: url,
      snippet: parsed ? parsed.snippet : fallbackSnippet(group, title),
      published_at: parsed ? parsed.published_at : null,
    });
  }
  if (!results.length) {
    if ((recognized > 0 && skipped === recognized) ||
        /\bno results\b|没有找到结果|未找到结果/i.test(pageText)) return [];
    throw new BrowserSearchError('provider_malformed_response', '浏览器搜索页面结构无法识别');
  }
  return results;
}

export function createAppControlBrowserSearch(options = {}) {
  const client = options.client || createAppControlClientFromEnv(options.env || process.env);
  const clock = options.clock || (() => performance.now());
  return Object.freeze({
    id: DDG_BROWSER_PROVIDER,
    transport: SEARCH_TRANSPORT_BROWSER_APP_CONTROL,
    limitMax: 10,
    async search(query, limit, callOptions = {}) {
      if (!client) throw new BrowserSearchError('browser_unavailable', 'Desk 后台浏览器不可用', { retryable: true });
      const url = new URL(DDG_BROWSER_ENDPOINT);
      url.searchParams.set('q', query);
      const started = clock();
      const diagnostics = { open_ms: 0, inspect_ms: 0, parse_ms: 0, cleanup_ms: 0,
        provider_ms: 0, inspections: 0, snapshot_nodes: 0, snapshot_truncated: false, cleanup_failed: false };
      const measure = async (key, task) => {
        const at = clock();
        try { return await task(); }
        finally {
          diagnostics[key] += Math.max(0, Math.round(clock() - at));
          diagnostics.provider_ms = Math.max(0, Math.round(clock() - started));
        }
      };
      const checkAbort = () => {
        if (callOptions.signal?.aborted) throw new BrowserSearchError('aborted', '请求已取消');
      };
      let lease = '';
      let primaryError = null;
      try {
        checkAbort();
        const opened = await measure('open_ms', () => client.open(url.toString(), {
          signal: callOptions.signal, timeoutMs: 20_000,
          onLease: value => { lease = cleanText(value, 200) || lease; },
          allowedMainFrameHosts: [DDG_BROWSER_HOST], intent: '后台匿名搜索公开网页',
        }));
        lease = cleanText(opened?.lease_id, 200) || lease;
        operationOutput(opened, '开页');
        if (!lease) throw new BrowserSearchError('browser_operation_failed', '后台浏览器没有返回租约');
        let maxNodes = 1200;
        for (let attempt = 0; attempt < 3; attempt += 1) {
          checkAbort();
          const left = 24_000 - (clock() - started);
          if (left <= 0) throw new BrowserSearchError('browser_timeout', '搜索结果等待超时', { retryable: true });
          // Probe immediately; only incomplete/unknown pages need a bounded same-tab re-observation.
          const observed = await measure('inspect_ms', () => client.inspect(lease,
            { include_offscreen: true, max_nodes: maxNodes },
            { signal: callOptions.signal, timeoutMs: Math.min(15_000, left), settleMs: [0, 300, 800][attempt] }));
          diagnostics.inspections += 1;
          const snapshot = operationOutput(observed, '读取');
          diagnostics.snapshot_nodes = Array.isArray(snapshot.nodes) ? snapshot.nodes.length : 0;
          diagnostics.snapshot_truncated = Boolean(snapshot.truncated);
          if (snapshot.truncated) maxNodes = 4000;
          const at = clock();
          try {
            // Validate the page before trusting its query string or any result nodes.
            const results = parseDuckDuckGoSnapshot(snapshot, limit);
            if (new URL(snapshot.url).searchParams.get('q') !== query) {
              throw new BrowserSearchError('provider_navigation_mismatch', '搜索页面未完成本次查询导航', { retryable: true });
            }
            if (!results.length && (snapshot.settled !== true || snapshot.truncated)) {
              if (attempt < 2) continue;
              throw new BrowserSearchError('provider_results_not_ready', '搜索页面尚未确认无结果', { retryable: true });
            }
            if (results.length < limit && snapshot.settled !== true && attempt < 2) continue;
            checkAbort();
            const stable = JSON.stringify({ url: snapshot.url, title: snapshot.title, results });
            return { results, final_url: snapshot.url, diagnostics,
              content_hash: 'sha256:' + createHash('sha256').update(stable).digest('hex') };
          } catch (error) {
            if (attempt === 2 || !['provider_malformed_response', 'provider_navigation_mismatch'].includes(error.code)) throw error;
          } finally { diagnostics.parse_ms += Math.max(0, Math.round(clock() - at)); }
        }
      } catch (error) {
        primaryError = error;
        if (!lease && !error.status && error.code !== 'aborted') diagnostics.open_outcome_unknown = true;
        error.searchDiagnostics = diagnostics;
        throw error;
      } finally {
        if (lease) {
          await measure('cleanup_ms', async () => {
            try {
              const closed = await client.close(lease, `web-search-close-${randomUUID()}`, { timeoutMs: 6000 });
              operationOutput(closed, '关闭');
            } catch {
              diagnostics.cleanup_failed = true;
              try { await client.yield(lease, { timeoutMs: 3000 }); } catch {}
              if (!primaryError) {
                const error = new BrowserSearchError('browser_cleanup_failed', '后台搜索标签未能可靠关闭', { retryable: true });
                error.searchDiagnostics = diagnostics;
                throw error;
              }
            }
          });
        }
        diagnostics.provider_ms = Math.max(0, Math.round(clock() - started));
      }
    },
  });
}
