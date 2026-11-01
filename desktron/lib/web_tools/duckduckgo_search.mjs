import { createHash } from 'node:crypto';
import { SearchProviderError, SEARCH_TRANSPORT_DIRECT_HTTP } from './search_provider.mjs';

export const DDG_SEARCH_ENDPOINT = 'https://html.duckduckgo.com/html/';
export const DDG_SEARCH_PROVIDER = 'duckduckgo-html';
export const DDG_SEARCH_LIMIT_MAX = 10;

function cleanText(value, limit) {
  return String(value ?? '')
    .replace(/<script\b[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style\b[\s\S]*?<\/style>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (_match, entity) => {
      const value = String(entity).toLowerCase();
      try {
        if (value.startsWith('#x')) return String.fromCodePoint(Number.parseInt(value.slice(2), 16));
        if (value.startsWith('#')) return String.fromCodePoint(Number.parseInt(value.slice(1), 10));
      } catch { return ' '; }
      return { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' }[value] ?? ' ';
    })
    .replace(/[\u0000-\u001f\u007f]/g, ' ')
    .replace(/\s+/g, ' ').trim().slice(0, limit);
}

function attribute(tag, name) {
  const match = new RegExp(`\\b${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s>]+))`, 'i').exec(tag);
  return match ? (match[1] ?? match[2] ?? match[3] ?? '') : '';
}

function decodeUrl(value) {
  return cleanText(value, 8192).replace(/&amp;/gi, '&');
}

export function unwrapDuckDuckGoUrl(raw) {
  let url;
  try { url = new URL(decodeUrl(raw), DDG_SEARCH_ENDPOINT); }
  catch { throw new SearchProviderError('provider_malformed_response', '搜索结果包含无效 URL', { browserRecommended: true }); }
  const host = url.hostname.toLowerCase().replace(/\.$/, '');
  if (host === 'duckduckgo.com' || host.endsWith('.duckduckgo.com')) {
    const target = url.searchParams.get('uddg');
    if (target) return target;
  }
  return url.toString();
}

function detectChallenge(html) {
  const source = String(html);
  return /<form\b[^>]*\bid\s*=\s*["'](?:challenge-form|img-form)["']/i.test(source) ||
    /<[^>]+\bclass\s*=\s*["'][^"']*\banomaly-modal(?:__\w+)?\b[^"']*["']/i.test(source);
}

export function parseDuckDuckGoHtml(html, limit = 5) {
  const source = String(html ?? '');
  if (detectChallenge(source)) {
    throw new SearchProviderError('provider_challenge', '搜索服务要求浏览器验证', { retryable: false, browserRecommended: true });
  }
  const blocks = [...source.matchAll(/<div\b[^>]*\bclass\s*=\s*(?:"[^"]*\bresult\b[^"]*"|'[^']*\bresult\b[^']*')[^>]*>([\s\S]*?)(?=<div\b[^>]*\bclass\s*=\s*(?:"[^"]*\bresult\b[^"]*"|'[^']*\bresult\b[^']*')|<\/body>|$)/gi)]
    .map(match => match[1]);
  const results = [];
  for (const block of blocks) {
    const anchor = /<a\b[^>]*\bclass\s*=\s*(?:"[^"]*\bresult__a\b[^"]*"|'[^']*\bresult__a\b[^']*')[^>]*>[\s\S]*?<\/a>/i.exec(block)?.[0];
    if (!anchor) continue;
    const href = attribute(anchor, 'href');
    const title = cleanText(anchor, 500);
    const snippetTag = /<(?:a|div)\b[^>]*\bclass\s*=\s*(?:"[^"]*\bresult__snippet\b[^"]*"|'[^']*\bresult__snippet\b[^']*')[^>]*>[\s\S]*?<\/(?:a|div)>/i.exec(block)?.[0] || '';
    if (!href || !title) continue;
    results.push({ title, raw_url: unwrapDuckDuckGoUrl(href), snippet: cleanText(snippetTag, 2000), published_at: null });
    if (results.length >= limit) break;
  }
  if (!results.length) {
    const noResults = /\bno results\b|\bno more results\b|class\s*=\s*["'][^"']*no-results/i.test(source);
    if (noResults) return [];
    throw new SearchProviderError('provider_malformed_response', '搜索服务页面结构无法识别', { browserRecommended: true });
  }
  return results;
}

export function classifyDuckDuckGoError(status) {
  if (status === 429) return new SearchProviderError('provider_rate_limited', '搜索服务触发限流', { retryable: true, browserRecommended: true });
  if (status === 403) return new SearchProviderError('provider_challenge', '搜索服务拒绝自动访问', { browserRecommended: true });
  if (status < 200 || status >= 300) return new SearchProviderError('provider_unavailable', '搜索服务暂不可用', { retryable: status >= 500, browserRecommended: true });
  return null;
}

export function createDuckDuckGoSearchProvider() {
  return Object.freeze({
    id: DDG_SEARCH_PROVIDER,
    transport: SEARCH_TRANSPORT_DIRECT_HTTP,
    limitMax: DDG_SEARCH_LIMIT_MAX,
    buildRequest(query) {
      const body = new URLSearchParams({ q: query, b: '', kl: 'wt-wt' }).toString();
      return {
        url: DDG_SEARCH_ENDPOINT,
        method: 'POST',
        maxRedirects: 0,
        headers: {
          'content-type': 'application/x-www-form-urlencoded',
          accept: 'text/html,application/xhtml+xml',
          'content-length': String(Buffer.byteLength(body)),
        },
        body,
      };
    },
    parseResponse(response, limit) {
      const failure = classifyDuckDuckGoError(response.status);
      if (failure) throw failure;
      if (!['text/html', 'application/xhtml+xml'].includes(response.contentType)) {
        throw new SearchProviderError('provider_invalid_content_type', '搜索服务返回类型无效', { browserRecommended: true });
      }
      return {
        results: parseDuckDuckGoHtml(response.body.toString('utf8'), limit),
        provider_request_id: null,
      };
    },
    responseHash(response) {
      return 'sha256:' + createHash('sha256').update(response.body).digest('hex');
    },
  });
}
